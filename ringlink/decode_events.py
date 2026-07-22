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
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
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

# A drain always reads flash up to "now", so the LAST record of a segment
# was emitted at/just before the moment the host received it. If a ring-
# clock anchor (0x42/0x85) maps that tail record further than this from its
# host receive time, the ring's clock is stale (it PAUSES during crash/
# hibernate states and falls behind real time) — reject the anchor and
# anchor on the host clock instead. The official app validates anchors the
# same way (open_ring PROTOCOL.md §12, native anchor validator).
ANCHOR_HOST_TOLERANCE_MS = 15 * 60 * 1000


# ---------------------------------------------------------------------------
# Load + decode
# ---------------------------------------------------------------------------

def load_records(events_path: Path):
    """Yield dicts: host_ts, type, name, ring_time, payload(bytes), generation.

    Drains APPEND to events.jsonl, so repeated runs duplicate frames — dedupe
    on (generation, type, ring_time, payload).

    GENERATION = boot lineage. A ring reboot restarts the ring_time counter,
    so a rebooted ring's early frames can be BYTE-IDENTICAL to the previous
    boot's early frames (including the 0x41 RING_START itself). A naive
    global dedupe swallows them and the reboot becomes invisible — post-
    reboot data then gets anchored into the OLD boot's timeline (hours off)
    or dropped entirely. Detection: within one boot, ring history only ever
    GROWS, so a drain session whose max ring_time is LOWER than the previous
    session's max means the ring restarted (and possibly wiped history).
    """
    # Pass 1: raw rows -> drain sessions (split on >120 s host-ts gaps)
    rows = []
    with events_path.open() as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["lineno"] = lineno
            rows.append(row)

    sessions = []
    for row in rows:
        if not sessions or row["ts"] - sessions[-1][-1]["ts"] > 120:
            sessions.append([])
        sessions[-1].append(row)

    # Pass 2: assign boot generations, then dedupe within each generation
    generation = 0
    prev_max = None
    seen = set()
    records = []
    for sess in sessions:
        parsed = []
        sess_max = None
        for row in sess:
            raw = bytes.fromhex(row["raw"])
            inner = framing.parse_inner_records(raw)
            parsed.append((row, raw, inner))
            for rec in (inner or []):
                if rec.ring_time is not None:
                    sess_max = rec.ring_time if sess_max is None \
                        else max(sess_max, rec.ring_time)
        if sess_max is not None and prev_max is not None and sess_max < prev_max:
            generation += 1
        if sess_max is not None:
            prev_max = sess_max

        for row, raw, inner in parsed:
            if not inner:
                records.append({
                    "host_ts": row["ts"], "type": row.get("tag"),
                    "name": "UNPARSED", "ring_time": None,
                    "payload": raw, "lineno": row["lineno"],
                    "generation": generation,
                })
                continue
            for rec in inner:
                key = (generation, rec.type_byte, rec.ring_time, rec.payload)
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    "host_ts": row["ts"], "type": rec.type_byte,
                    "name": canonical_type(rec.type_byte),
                    "ring_time": rec.ring_time, "payload": rec.payload,
                    "lineno": row["lineno"], "generation": generation,
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
    # --- segment on restarts: boot-generation change (reboot detected at
    # drain-session level) OR in-stream 0x41 with ring_time regression ---
    segments, cur, prev_rt, cur_gen = [], [], None, None
    for rec in records:
        rt = rec["ring_time"]
        gen = rec.get("generation", 0)
        in_stream_reboot = (rec["type"] == 0x41 and prev_rt is not None
                            and rt is not None and rt < prev_rt)
        if cur and (gen != cur_gen or in_stream_reboot):
            segments.append(cur)
            cur, prev_rt = [], None
        cur_gen = gen
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
        # Tail = the record with MAX ring_time. Drain replays (cursor
        # overlap) append out of stream order, so "last line in the file"
        # can have a LOWER ring_time than earlier lines — anchoring on it
        # projected higher-ring_time records into the FUTURE (bug seen
        # 2026-07-22: HR rows +1h49m ahead of wall clock).
        timed = [r for r in seg if r["ring_time"] is not None]
        last = max(timed, key=lambda r: r["ring_time"], default=None)

        def host_anchor(source: str):
            # Anchor so that NO record lands after its own host receive time
            # (causality: we can't receive a frame before it was emitted).
            # The min offset comes from the freshest live frames, which is
            # exactly the right reference.
            offset = min(int(r["host_ts"] * 1000) - TICK_MS_DEFAULT * r["ring_time"]
                         for r in timed)
            return (0, offset, TICK_MS_DEFAULT, source)

        if anchor is not None and last is not None:
            a_rt, a_utc, tick, src = anchor
            tail_utc = a_utc + tick * (last["ring_time"] - a_rt)
            drift_ms = tail_utc - int(last["host_ts"] * 1000)
            if abs(drift_ms) > ANCHOR_HOST_TOLERANCE_MS:
                print(f"  [decode] rejected {src} anchor: segment tail lands "
                      f"{drift_ms / 60000:+.1f} min from host receive time "
                      f"(stale ring clock); anchoring on host clock")
                anchor = host_anchor(f"host_corrected({src})")
        if anchor is None:
            if last is not None:
                anchor = host_anchor("host_fallback")
        for rec in seg:
            rt = rec["ring_time"]
            if anchor is None or rt is None:
                rec["utc_ms"], rec["utc_source"] = None, None
                continue
            a_rt, a_utc, tick, src = anchor
            utc_ms = a_utc + tick * (rt - a_rt)
            # Universal causality clamp: never later than its receive time.
            utc_ms = min(utc_ms, int(rec["host_ts"] * 1000))
            rec["utc_ms"] = utc_ms
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
                "ring_time": rec["ring_time"], "gen": rec.get("generation", 0),
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

    # --- sleep / readiness / activity (ring_analysis) ------------------------
    import ring_analysis
    analysis = ring_analysis.analyze(records)

    _write_csv(EXPORT_DIR / "dailysleep.csv",
               ["id", "day", "score", "timestamp", "contributors",
                "recommendation", "status"],
               [[d["id"], d["day"], d["score"], d["timestamp"],
                 json.dumps(d["contributors"]), "", ""]
                for d in analysis["daily_sleep"]])

    _write_csv(EXPORT_DIR / "dailyreadiness.csv",
               ["id", "day", "score", "temperature_deviation",
                "temperature_trend_deviation", "contributors"],
               [[d["id"], d["day"], d["score"],
                 "" if d["temperature_deviation"] is None else d["temperature_deviation"],
                 "" if d["temperature_trend_deviation"] is None else d["temperature_trend_deviation"],
                 json.dumps(d["contributors"])]
                for d in analysis["daily_readiness"]])

    _write_csv(EXPORT_DIR / "dailyresilience.csv",
               ["id", "day", "level", "contributors"],
               [[d["id"], d["day"], d["level"], json.dumps(d["contributors"])]
                for d in analysis["daily_resilience"]])

    def _seq(items, start_dt, interval):
        return json.dumps({"interval": interval, "items": items,
                           "timestamp": start_dt.isoformat()})

    _write_csv(
        EXPORT_DIR / "sleepmodel.csv",
        ["id", "day", "bedtime_start", "bedtime_end", "type", "efficiency",
         "latency", "total_sleep_duration", "deep_sleep_duration",
         "rem_sleep_duration", "light_sleep_duration", "awake_time",
         "average_heart_rate", "average_hrv", "sleep_phase_5_min",
         "sleep_phase_30_sec", "movement_30_sec", "average_breath",
         "lowest_heart_rate", "low_battery_alert", "period",
         "restless_periods", "sleep_algorithm_version", "sleep_score_delta",
         "time_in_bed", "heart_rate", "hrv", "readiness",
         "readiness_score_delta"],
        [[s["id"], s["day"], s["bedtime_start"].isoformat(),
          s["bedtime_end"].isoformat(), s["type"], s["efficiency"],
          s["latency"], s["total_sleep_duration"], s["deep_sleep_duration"],
          s["rem_sleep_duration"], s["light_sleep_duration"], s["awake_time"],
          s["average_heart_rate"], s["average_hrv"], s["stages_5min"],
          s["stages_30s"], s["movement_30s"],
          "" if s["average_breath"] is None else s["average_breath"],
          "" if s["lowest_heart_rate"] is None else s["lowest_heart_rate"],
          "", 1, s["restless_periods"], "ringlink-v1", "",
          s["time_in_bed"],
          _seq(s["hr_items"], s["bedtime_start"], 300),
          _seq(s["hrv_items"], s["bedtime_start"], 300),
          json.dumps(s.get("readiness")) if s.get("readiness") else "", ""]
         for s in analysis["sessions"]])

    _write_csv(
        EXPORT_DIR / "dailyactivity.csv",
        ["id", "day", "score", "steps", "total_calories", "active_calories",
         "average_met_minutes", "equivalent_walking_distance", "contributors",
         "class_5_min", "met", "high_activity_met_minutes",
         "high_activity_time", "inactivity_alerts", "low_activity_met_minutes",
         "low_activity_time", "medium_activity_met_minutes",
         "medium_activity_time", "meters_to_target", "non_wear_time",
         "resting_time", "sedentary_met_minutes", "sedentary_time",
         "target_calories", "target_meters"],
        [[d["id"], d["day"], d["score"], "", d["total_calories"],
          d["active_calories"], d["average_met_minutes"], "",
          json.dumps(d["contributors"]), d["class_5_min"],
          json.dumps({"interval": 60, "items": d["met_items"],
                      "timestamp": d["day"] + "T00:00:00"}),
          "", d["high_activity_time"], "", "", d["low_activity_time"], "",
          d["medium_activity_time"], "", d["non_wear_time"],
          d["resting_time"], "", d["sedentary_time"], d["target_calories"],
          ""]
         for d in analysis["daily_activity"]])

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


# ---------------------------------------------------------------------------
# Log rotation — events.jsonl grows ~10 MB/day and the pipeline re-decodes
# the whole file every cycle. Rotate decoded-and-ingested history out to
# events.archive.jsonl; the DB is the durable store (upserts never delete),
# and baselines.json carries the nightly aggregates across rotation.
# ---------------------------------------------------------------------------

ROTATE_MAX_BYTES = 20 * 1024 * 1024
ROTATE_KEEP_S = 3 * 24 * 3600      # keep 3 days (cursor overlap is only 1 h)


def rotate_events(events_path: Path = EVENTS_FILE):
    """Call AFTER a successful ingest. Moves lines older than ROTATE_KEEP_S
    to events.archive.jsonl once the live file exceeds ROTATE_MAX_BYTES."""
    import time as _time
    if not events_path.exists() or events_path.stat().st_size < ROTATE_MAX_BYTES:
        return
    cutoff = _time.time() - ROTATE_KEEP_S
    archive = events_path.with_name("events.archive.jsonl")
    keep, moved = [], 0
    with events_path.open() as fh, archive.open("a") as arch:
        for line in fh:
            try:
                old = json.loads(line)["ts"] < cutoff
            except Exception:
                old = False
            if old:
                arch.write(line)
                moved += 1
            else:
                keep.append(line)
    tmp = events_path.with_suffix(".tmp")
    tmp.write_text("".join(keep))
    tmp.replace(events_path)
    print(f"  [rotate] {moved} old frames -> {archive.name}; "
          f"{len(keep)} kept live")


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
