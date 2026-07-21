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
CURSOR_FILE = HERE / "ring_cursor.txt"

# Full connect->drain cycles per sync run. Each failed cycle closes and
# reopens the dongle driver (a wedged serial/dongle state after extended
# idle persists across connect attempts within one driver session).
# Kept small so a down/unreachable ring fails the run in ~90 s instead of
# pinning the UI on "Syncing…" for many minutes — the scheduler retries
# every 15 min anyway.
CONNECT_CYCLES = 2
# Re-drain 1 h (in deciseconds) behind the saved cursor — decode dedupes,
# and the overlap absorbs small ring-clock adjustments after re-anchoring.
CURSOR_OVERLAP_DS = 36_000

# The pc-ble-driver H5 serial transport can wedge permanently (known
# upstream bug, blatann#75) — open() then fails with
# NRF_ERROR_SD_RPC_H5_TRANSPORT_STATE until the dongle is re-enumerated at
# the USB level. The RingDongleReset scheduled task (install once with
# ringlink/install_dongle_reset_task.sh) performs that replug-equivalent
# reset on demand, without elevation.
DONGLE_WEDGE_MARKERS = ("H5_TRANSPORT", "SD_RPC", "heard nothing")


def is_dongle_wedge(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in DONGLE_WEDGE_MARKERS)


def reset_dongle_usb() -> bool:
    """Trigger the elevated USB restart task; True if it ran."""
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", "RingDongleReset"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    if r.returncode != 0:
        print("[sync] RingDongleReset task not available "
              "(run ringlink/install_dongle_reset_task.sh once as admin)")
        return False
    print("[sync] dongle USB reset triggered; waiting for re-enumeration")
    time.sleep(12)
    return True


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


def pid_alive(pid: int) -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def daemon_alive() -> bool:
    lock = HERE / ".daemon.lock"
    if not lock.exists():
        return False
    try:
        return pid_alive(int(lock.read_text().strip()))
    except Exception:
        return False


def read_cursor() -> int:
    try:
        return max(0, int(CURSOR_FILE.read_text().strip()))
    except Exception:
        return 0


def write_cursor(cursor: int):
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(str(int(cursor)) + "\n")
    os.replace(tmp, CURSOR_FILE)


def run_ring_session() -> int:
    """One BLE session: connect -> auth -> battery -> time-sync -> drain.

    Returns the number of event frames drained. Raises on any failure;
    the caller retries with a fresh driver."""
    from oura_ring import OuraRing, pick_port, load_key  # slow import (driver)

    ring = OuraRing(pick_port(None))
    ring.open()
    try:
        ring.connect(seconds=15, attempts=2)
        key = load_key()
        ring.authenticate(key)
        write_status("syncing", phase="connected", connected=True,
                     last_seen=now_iso())

        # Phone-faithful session order (open_ring §6.1/§10, btsnoop-verified):
        # subscribe+state_cmd+caps -> battery -> data_flush -> drain ->
        # ack-fetch (inside drain_events) -> time-sync LAST (elicits a fresh
        # 0x42 anchor that lands in the next drain).
        ring.engage_data_plane()

        b = ring.battery()
        level = b[0]
        with BATTERY_LOG.open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "level": level}) + "\n")
        write_status("syncing", phase="draining", battery=level)

        ring.data_flush()  # release flash-buffered events before GetEvent

        def on_batch(cursor: int):
            write_cursor(cursor)
            # Heartbeat: refresh updated_at every batch so the backend can
            # distinguish a long (legit) drain from a wedged/killed run.
            write_status("syncing", phase="draining", cursor=cursor)

        def drain(start_ds: int) -> int:
            count = 0
            with EVENTS_FILE.open("a") as f:
                for tag, payload, raw in ring.drain_events(start_ds,
                                                           on_batch=on_batch):
                    f.write(json.dumps({"ts": time.time(), "tag": tag,
                                        "raw": raw.hex()}) + "\n")
                    count += 1
            return count

        # Incremental drain from the saved cursor (minus overlap) — keeps the
        # BLE session short as history grows (long full drains were dropping
        # the link mid-drain with connection_timeout).
        saved = read_cursor()
        start_ds = max(0, saved - CURSOR_OVERLAP_DS)
        n = drain(start_ds)
        if n == 0 and start_ds > 0:
            # A ring reboot / clock regression can leave the saved cursor
            # beyond ring time -> empty drains forever. Full drain once;
            # decode-side dedupe makes this safe.
            print("incremental drain empty; falling back to full drain")
            write_cursor(0)
            n = drain(0)
        print(f"{n} event frame(s) drained")
        ring.sync_time()  # keep ring clock anchored (enables 0x42 anchors)
        return n
    finally:
        try:
            ring.close()
        except Exception:
            pass


def main():
    # The persistent daemon (ring_daemon.py) owns the dongle while alive —
    # a one-shot sync would fight it over the serial port and the link.
    if daemon_alive():
        print("ring daemon is running — one-shot sync skipped")
        return 0
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
        import decode_events

        n = None
        last_exc = None
        cycle = 0
        max_cycles = CONNECT_CYCLES
        dongle_reset_done = False
        while cycle < max_cycles:
            cycle += 1
            write_status("syncing", phase="connecting", attempt=cycle,
                         last_error=None)
            try:
                n = run_ring_session()
                break
            except Exception as e:
                last_exc = e
                traceback.print_exc()
                if is_dongle_wedge(e) and not dongle_reset_done:
                    # Dongle serial transport is dead — plain reopen won't
                    # help. USB-restart it (replug equivalent), then retry
                    # (grants one extra cycle even if this was the last).
                    write_status("syncing", phase="dongle_reset", attempt=cycle,
                                 last_error=f"{type(e).__name__}: {e}")
                    dongle_reset_done = True
                    if reset_dongle_usb():
                        max_cycles = max(max_cycles, cycle + 1)
                        continue
                    raise RuntimeError(
                        "dongle transport wedged — unplug/replug the nRF "
                        "dongle, or run ringlink/install_dongle_reset_task.sh "
                        "once (admin) to enable automatic recovery") from e
                if cycle < max_cycles:
                    delay = 8
                    print(f"[sync] cycle {cycle}/{max_cycles} failed "
                          f"({type(e).__name__}: {e}); reopening driver in {delay}s")
                    write_status("syncing", phase="retry_wait", attempt=cycle,
                                 last_error=f"{type(e).__name__}: {e}")
                    time.sleep(delay)
        if n is None:
            raise last_exc

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
