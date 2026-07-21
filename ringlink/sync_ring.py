#!/usr/bin/env python3
"""One-shot ring sync with status reporting (runs in ringlink/venv310).

Single BLE connection: battery + time-sync + event drain, then decode ->
export -> ingest into Cracked-Oura's SQLite DB. Writes ring_status.json at
every stage so the backend/frontend can show a live connection indicator.

Called by: Task Scheduler (every 15 min via sync_dashboard.sh) and
POST /api/ring/sync (manual "Sync now" button).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STATUS_FILE = HERE / "ring_status.json"
LOCK_DIR = HERE / ".sync.lock"
BATTERY_LOG = HERE / "battery_log.jsonl"
EVENTS_FILE = HERE / "events.jsonl"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def write_status(state: str, **fields):
    prev = read_status()
    prev.update(fields)
    prev["state"] = state
    prev["updated_at"] = now_iso()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(prev, indent=2))
    os.replace(tmp, STATUS_FILE)


def main():
    try:
        LOCK_DIR.mkdir()
    except FileExistsError:
        # Stale lock (>15 min) is broken; otherwise a sync is already running.
        age = time.time() - LOCK_DIR.stat().st_mtime
        if age < 900:
            print("another sync is running, skipping")
            return 0
        LOCK_DIR.rmdir()
        LOCK_DIR.mkdir()

    try:
        write_status("syncing", phase="connecting", last_error=None)

        from oura_ring import OuraRing, pick_port, load_key  # slow import (driver)
        import decode_events

        ring = OuraRing(pick_port(None))
        ring.open()
        try:
            ring.connect()
            key = load_key()
            ring.authenticate(key)
            write_status("syncing", phase="connected", connected=True,
                         last_seen=now_iso())

            b = ring.battery()
            level = b[0]
            with BATTERY_LOG.open("a") as f:
                f.write(json.dumps({"ts": int(time.time()), "level": level}) + "\n")
            write_status("syncing", phase="draining", battery=level)

            ring.sync_time()  # keep ring clock anchored (enables 0x42 anchors)

            n = 0
            with EVENTS_FILE.open("a") as f:
                for tag, payload, raw in ring.drain_events(0):
                    f.write(json.dumps({"ts": time.time(), "tag": tag,
                                        "raw": raw.hex()}) + "\n")
                    n += 1
            print(f"{n} event frame(s) drained")
        finally:
            ring.close()

        write_status("syncing", phase="ingesting", frames_drained=n)
        decode_events.cmd_decode(EVENTS_FILE, decode_events.DECODED_FILE)
        decode_events.cmd_export(EVENTS_FILE, min_temp_c=20.0)
        decode_events.cmd_ingest()

        write_status("idle", phase=None, connected=False,
                     last_sync_ok=True, last_sync_time=now_iso(),
                     last_frames=n)
        print("sync complete")
        return 0
    except SystemExit as e:
        write_status("idle", phase=None, connected=False, last_sync_ok=False,
                     last_error=str(e))
        raise
    except Exception as e:
        traceback.print_exc()
        write_status("idle", phase=None, connected=False, last_sync_ok=False,
                     last_error=f"{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
