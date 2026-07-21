#!/usr/bin/env python3
"""Decode ring-local/events.jsonl raw BLE frames into metrics and export them
in Oura cloud-export CSV format so Cracked-Oura (../Cracked-Oura) can import.

Pipeline:
  events.jsonl (raw inner-TLV frames from `./oura.sh events`)
    -> decode   : decoded_events.jsonl (typed records + resolved UTC time)
    -> export   : export/oura-export/*.csv + export/oura_export_ring.zip
    -> ingest   : run Cracked-Oura's own OuraParser on the zip (its venv,
                  its SQLite DB in %APPDATA%/CrackedOura)

Decoders vendored from LogosIsLife/open_ring (GPL-3.0) in ./openring/.
Time resolution per open_ring PROTOCOL.md §7 (single anchor, 100 ms/tick,
0x42 sets anchor, 0x41 with regressed ring_time invalidates). When no 0x42
anchor exists in a capture (e.g. right after factory reset), we fall back to
anchoring the LAST record of each boot-session at the host receive time —
timestamps are then approximate and flagged `utc_source: "host_fallback"`.

Usage:
  python decode_events.py decode            # -> decoded_events.jsonl + summary
  python decode_events.py export            # decode + write CSVs + zip
  python decode_events.py ingest            # export + import into Cracked-Oura DB
  options: --events PATH  --min-temp-c 20   (drop non-worn temp rows below this)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from openring import framing, decoders  # noqa: E402
from openring.decoders import canonical_type  # noqa: E402

CRACKED = HERE.parent  # ringlink lives inside the Cracked-Oura repo
EVENTS_FILE = HERE / "events.jsonl"
DECODED_FILE = HERE / "decoded_events.jsonl"
EXPORT_DIR = HERE / "export" / "oura-export"
EXPORT_ZIP = HERE / "export" / "oura_export_ring.zip"

TICK_MS_DEFAULT = 100  # 0x42 factor_flag==0 -> 100 ms/tick


# ---------------------------------------------------------------------------
# Load + decode
# ---------------------------------------------------------------------------

def load_records(events_path: Path):
    """Yield dicts: host_ts, type, name, ring_time, payload(bytes) — deduped.

    `oura.sh events` APPENDS and always drains from cursor 0, so repeated runs
    duplicate frames. Dedupe on (type, ring_time, payload).
    """
    seen = set()
    records = []
    with events_path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            raw = bytes.fromhex(row["raw"])
            inner = framing.parse_inner_records(raw)
            if not inner:
                records.append({
                    "host_ts": row["ts"], "type": row.get("tag"),
                    "name": "UNPARSED", "ring_time": None,
                    "payload": raw, "lineno": lineno,
                })
                continue
            for rec in inner:
                key = (rec.type_byte, rec.ring_time, rec.payload)
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    "host_ts": row["ts"], "type": rec.type_byte,
                    "name": canonical_type(rec.type_byte),
                    "ring_time": rec.ring_time, "payload": rec.payload,
                    "lineno": lineno,
                })
    return records


def resolve_times(records):
    """Attach `utc_ms` + `utc_source` to each record.

    Segments the stream on ring restarts (0x41 with ring_time regression),
    then per segment:
      - official: anchor from the LAST 0x42 in the segment (100 or 1 ms/tick)
      - bonus:    0x85 RTC beacon (1 s precision) wins over 0x42 if present
      - fallback: anchor last record of the segment at its host receive time
    """
    # --- segment on restarts ---
    segments, cur, prev_rt = [], [], None
    for rec in records:
        rt = rec["ring_time"]
        if rec["type"] == 0x41 and prev_rt is not None and rt is not None and rt < prev_rt:
            segments.append(cur)
            cur = []
        cur.append(rec)
        if rt is not None:
            prev_rt = rt
    if cur:
        segments.append(cur)

    for seg in segments:
        anchor = None  # (ring_time, utc_ms, tick_ms, source)
        for rec in seg:
            rt, p = rec["ring_time"], rec["payload"]
            if rec["type"] == 0x42 and len(p) == 9:
                counter = p[1] | (p[2] << 8) | (p[3] << 16)
                tick = 1 if p[0] == 0xFD else TICK_MS_DEFAULT
                anchor = (rt, counter * 256 * 1000, tick, "time_sync_0x42")
            elif rec["type"] == 0x85 and len(p) >= 4:
                unix_s = int.from_bytes(p[0:4], "little")
                anchor = (rt, unix_s * 1000, TICK_MS_DEFAULT, "rtc_beacon_0x85")
        if anchor is None:
            last = next((r for r in reversed(seg) if r["ring_time"] is not None), None)
            if last is not None:
                anchor = (last["ring_time"], int(last["host_ts"] * 1000),
                          TICK_MS_DEFAULT, "host_fallback")
        for rec in seg:
            rt = rec["ring_time"]
            if anchor is None or rt is None:
                rec["utc_ms"], rec["utc_source"] = None, None
                continue
            a_rt, a_utc, tick, src = anchor
            rec["utc_ms"] = a_utc + tick * (rt - a_rt)
            rec["utc_source"] = src
    return records


def decode_all(records):
    for rec in records:
        if rec["name"] == "UNPARSED":
            rec["data"] = {"hex": rec["payload"].hex()}
        else:
            rec["data"] = decoders.decode(rec["type"], rec["payload"])
    return records


def iso(utc_ms):
    if utc_ms is None:
        return None
    return datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc).isoformat()


def iso_local(utc_ms):
    """Local-naive timestamp for CSV export. Oura's own cloud exports use
    local time, and Cracked-Oura's UI parses naive timestamps as local —
    exporting UTC made every chart 5 h off and broke day boundaries."""
    if utc_ms is None:
        return None
    return datetime.fromtimestamp(utc_ms / 1000).isoformat()


def cmd_decode(events_path: Path, out_path: Path):
    records = decode_all(resolve_times(load_records(events_path)))
    with out_path.open("w") as f:
        for rec in records:
            f.write(json.dumps({
                "utc": iso(rec["utc_ms"]), "utc_source": rec["utc_source"],
                "ring_time": rec["ring_time"],
                "type": f"0x{rec['type']:02x}" if isinstance(rec["type"], int) else rec["type"],
                "name": rec["name"], "data": rec["data"],
            }) + "\n")
    counts = {}
    for rec in records:
        counts[rec["name"]] = counts.get(rec["name"], 0) + 1
    print(f"{len(records)} unique records -> {out_path}")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {name}")
    return records


# ---------------------------------------------------------------------------
# Export to Oura cloud-export CSV layout (what Cracked-Oura ingests)
# ---------------------------------------------------------------------------

def _write_csv(path: Path, header: list[str], rows: list[list]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
    print(f"  wrote {path.name}: {len(rows)} row(s)")


def cmd_export(events_path: Path, min_temp_c: float):
    records = decode_all(resolve_times(load_records(events_path)))
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    # --- heartrate.csv: timestamp;bpm;source ---------------------------------
    hr_rows = {}
    for rec in records:
        if rec["utc_ms"] is None:
            continue
        if rec["type"] == 0x5D:  # HRV event: (hr,rmssd) pairs, 5-min spacing
            pairs = rec["data"].get("samples_5min") or []
            n = len(pairs)
            for i, pair in enumerate(pairs):
                bpm = pair.get("hr_bpm")
                if not bpm:
                    continue
                ts = rec["utc_ms"] - (n - 1 - i) * 300_000
                hr_rows[ts] = [iso_local(ts), bpm, "rest"]
        elif rec["type"] == 0x60:  # IBI+amplitude: 6 IBIs -> mean bpm
            ibis = [v for v in rec["data"].get("ibi_ms", []) if 250 <= v <= 2000]
            if ibis:
                bpm = round(60000 / (sum(ibis) / len(ibis)))
                if 25 <= bpm <= 250:
                    hr_rows[rec["utc_ms"]] = [iso_local(rec["utc_ms"]), bpm, "awake"]
        elif rec["type"] == 0x80:  # green-LED IBI+quality (awake HR mode)
            # PROTOCOL.md §5.1: value_11bit = IBI ms; strict quality filter
            # qual_a <= 1 and qual_b == 0.
            good = [s["value_11bit"] for s in rec["data"].get("samples", [])
                    if s.get("quality_a", 9) <= 1 and s.get("quality_b", 9) == 0
                    and 250 <= s["value_11bit"] <= 2000]
            if len(good) >= 3:
                bpm = round(60000 / (sum(good) / len(good)))
                if 25 <= bpm <= 250:
                    hr_rows[rec["utc_ms"]] = [iso_local(rec["utc_ms"]), bpm, "awake"]
    _write_csv(EXPORT_DIR / "heartrate.csv", ["timestamp", "bpm", "source"],
               [hr_rows[k] for k in sorted(hr_rows)])

    # --- temperature.csv: timestamp;skin_temp --------------------------------
    temp_rows = {}
    for rec in records:
        if rec["type"] != 0x46 or rec["utc_ms"] is None:
            continue
        skin = rec["data"].get("temp1_c")
        if skin is None or skin < min_temp_c:
            continue  # sentinel / ring not worn (room temp)
        skin_f = round(skin * 9 / 5 + 32, 2)  # owner preference: Fahrenheit
        temp_rows[rec["utc_ms"]] = [iso_local(rec["utc_ms"]), skin_f]
    _write_csv(EXPORT_DIR / "temperature.csv", ["timestamp", "skin_temp"],
               [temp_rows[k] for k in sorted(temp_rows)])

    # --- ringbatterylevel.csv: timestamp;level;charging;in_charger -----------
    # Sources: 0x61/0x24 change events + live samples from sync_dashboard.sh
    batt_rows = {}
    batt_log = HERE / "battery_log.jsonl"
    if batt_log.exists():
        for line in batt_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ts_ms = int(row["ts"]) * 1000
            batt_rows[ts_ms] = [iso_local(ts_ms), int(row["level"]), 0, 0]
    charging = 0
    for rec in records:
        if rec["type"] in (0x45, 0x53):  # state change / wear event
            charging = 1 if rec["data"].get("state") == 8 else 0
        if rec["type"] == 0x61 and rec["data"].get("_dd") == "DebugDataBatteryLevelChanged" \
                and rec["utc_ms"] is not None:
            batt_rows[rec["utc_ms"]] = [iso_local(rec["utc_ms"]),
                                        rec["data"]["battery_percentage"],
                                        charging, charging]
    _write_csv(EXPORT_DIR / "ringbatterylevel.csv",
               ["timestamp", "level", "charging", "in_charger"],
               [batt_rows[k] for k in sorted(batt_rows)])

    # --- dailysleep.csv: header-only marker ----------------------------------
    # Cracked-Oura's zip importer locates the data directory by the PRESENCE of
    # dailysleep.csv (or dailyactivity.csv). Header-only -> detected, 0 rows.
    _write_csv(EXPORT_DIR / "dailysleep.csv",
               ["id", "day", "score", "timestamp", "contributors",
                "recommendation", "status"], [])

    # --- zip -----------------------------------------------------------------
    with zipfile.ZipFile(EXPORT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(EXPORT_DIR.glob("*.csv")):
            z.write(p, f"oura-export/{p.name}")
    print(f"zip -> {EXPORT_ZIP}")
    return records


# ---------------------------------------------------------------------------
# Ingest into Cracked-Oura's own SQLite DB using its venv + parser
# ---------------------------------------------------------------------------

INGEST_SNIPPET = r"""
import sys
sys.path.insert(0, r"{cracked}")
from backend.src.database import SessionLocal, init_db
from backend.src.ingestion.manager import OuraParser
init_db()
s = SessionLocal()
try:
    OuraParser(s).parse_zip(r"{zip}")
finally:
    s.close()
from backend.src.database import DB_PATH
print("ingested into", DB_PATH)
"""


def cmd_ingest():
    venv_py = CRACKED / "backend" / "venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        sys.exit(f"Cracked-Oura venv python not found: {venv_py}")
    if not EXPORT_ZIP.exists():
        sys.exit(f"run `export` first — {EXPORT_ZIP} missing")
    code = INGEST_SNIPPET.format(cracked=CRACKED, zip=EXPORT_ZIP)
    r = subprocess.run([str(venv_py), "-c", code], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        sys.exit(f"ingest failed (exit {r.returncode})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["decode", "export", "ingest"], nargs="?",
                    default="decode")
    ap.add_argument("--events", type=Path, default=EVENTS_FILE)
    ap.add_argument("--min-temp-c", type=float, default=20.0,
                    help="drop temperature rows below this (ring off finger)")
    args = ap.parse_args()

    if args.command == "decode":
        cmd_decode(args.events, DECODED_FILE)
    elif args.command == "export":
        cmd_decode(args.events, DECODED_FILE)
        cmd_export(args.events, args.min_temp_c)
    elif args.command == "ingest":
        cmd_decode(args.events, DECODED_FILE)
        cmd_export(args.events, args.min_temp_c)
        cmd_ingest()


if __name__ == "__main__":
    main()
