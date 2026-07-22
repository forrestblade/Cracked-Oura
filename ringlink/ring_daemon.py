#!/usr/bin/env python3
"""Persistent ring connection daemon (the architecture the ring expects).

The official Oura app HOLDS a BLE connection and refreshes every ~20 s
(open_ring PROTOCOL.md §10 steady state). A worn ring can go radio-silent
for long stretches when nothing is connected — polling every 15 min misses
it. This daemon connects ONCE and never lets go:

  connect -> auth -> engage data plane -> steady state:
      every 20 s   : data_flush + GetEvent(cursor) -> events.jsonl
      every 3rd    : subscribe toggle (16 01 00 / sleep 2.5 / 16 01 02)
      every 5 min  : decode -> export -> ingest (only if new frames)
      every 15 min : battery probe -> battery_log + status
      every 60 min : time-sync (0x12, elicits fresh 0x42 anchors)
  on link loss     : reconnect loop (scan; ring advertises ~150 ms when idle)
  on dongle wedge  : RingDongleReset USB reset ladder, then reconnect

Run via sync_dashboard.sh (the 15-min scheduled task acts as a WATCHDOG that
restarts this daemon if it died). Writes ring_status.json heartbeats so the
dashboard indicator stays truthful.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sync_ring import (  # noqa: E402  (shared helpers, no side effects)
    write_status, read_cursor, write_cursor, now_iso, is_dongle_wedge,
    reset_dongle_usb, pid_alive, BATTERY_LOG, EVENTS_FILE, CURSOR_OVERLAP_DS,
)

DAEMON_LOCK = HERE / ".daemon.lock"          # holds our PID
# Touch this file to make the daemon send the ring a clean soft reset
# (0e 01 ff, open_ring §6.8) on its next cycle — used to clear a crash-
# degraded firmware state without losing pairing. Ring reboots in ~22-35 s,
# the link drops, and the daemon simply re-catches the fresh boot.
SOFT_RESET_FLAG = HERE / "soft_reset.request"
FLUSH_INTERVAL_S = 20                         # §12 FLUSH_INTERVAL_S_DEFAULT
SUBSCRIBE_TOGGLE_EVERY = 3                    # §12 phone-observed
INGEST_INTERVAL_S = 5 * 60
BATTERY_INTERVAL_S = 15 * 60
TIME_SYNC_INTERVAL_S = 60 * 60
RECONNECT_BACKOFF_S = (5, 10, 20, 40, 60)     # then stays at 60


def acquire_lock() -> bool:
    if DAEMON_LOCK.exists():
        try:
            pid = int(DAEMON_LOCK.read_text().strip())
            if pid_alive(pid):
                return False
        except Exception:
            pass
        DAEMON_LOCK.unlink(missing_ok=True)
    DAEMON_LOCK.write_text(str(os.getpid()) + "\n")
    return True


def release_lock():
    try:
        if int(DAEMON_LOCK.read_text().strip()) == os.getpid():
            DAEMON_LOCK.unlink()
    except Exception:
        pass


def run_pipeline():
    """decode -> export -> ingest (same pipeline as one-shot sync)."""
    import decode_events
    decode_events.cmd_decode(EVENTS_FILE, decode_events.DECODED_FILE)
    decode_events.cmd_export(EVENTS_FILE, min_temp_c=20.0)
    decode_events.cmd_ingest()
    decode_events.rotate_events(EVENTS_FILE)


# Shared across steady-state AND the reconnect loop: drained frames must
# reach the DB even while the ring is unreachable. Before this, ingest only
# ran inside the connected loop — frames sat on disk for HOURS during long
# radio naps / dongle wedges and the dashboard looked dead (2026-07-22).
LAST_INGEST_MONO = 0.0
LAST_INGEST_SIZE = -1


def maybe_ingest_offline():
    """Ingest new on-disk frames while disconnected (throttled)."""
    global LAST_INGEST_MONO, LAST_INGEST_SIZE
    if time.monotonic() - LAST_INGEST_MONO < INGEST_INTERVAL_S:
        return
    try:
        size = EVENTS_FILE.stat().st_size
    except OSError:
        return
    if size == LAST_INGEST_SIZE:
        LAST_INGEST_MONO = time.monotonic()  # nothing new; re-check in 5 min
        return
    write_status("live", phase="ingesting", connected=False)
    try:
        run_pipeline()
        LAST_INGEST_SIZE = EVENTS_FILE.stat().st_size
        write_status("live", phase="reconnect_wait", connected=False,
                     last_sync_ok=True, last_sync_time=now_iso(),
                     last_error=None)
        print("[daemon] offline ingest complete (ring still unreachable)")
    except Exception as e:
        traceback.print_exc()
        write_status("live", phase="reconnect_wait", connected=False,
                     last_error=f"ingest: {type(e).__name__}: {e}")
    LAST_INGEST_MONO = time.monotonic()


def steady_state(ring) -> None:
    """Hold the link; drain every FLUSH_INTERVAL_S. Raises on link loss."""
    global LAST_INGEST_MONO, LAST_INGEST_SIZE
    cycle = 0
    pending_frames = 0
    last_battery = 0.0
    last_timesync = time.monotonic()

    while True:
        cycle += 1
        now_mono = time.monotonic()

        # --- operator-requested clean ring reboot ---------------------------
        if SOFT_RESET_FLAG.exists():
            SOFT_RESET_FLAG.unlink()
            print("[daemon] soft-reset requested — sending 0e 01 ff")
            try:
                ring.request(bytes([0x0E, 0x01, 0xFF]),
                             done=lambda t, p: t == 0x0F, timeout=5.0)
                print("[daemon] soft reset acked; ring reboots in ~22-35 s")
            except RuntimeError:
                print("[daemon] soft reset: no ack")

        # --- battery probe -------------------------------------------------
        if now_mono - last_battery >= BATTERY_INTERVAL_S or last_battery == 0:
            b = ring.battery()
            level = b[0]
            with BATTERY_LOG.open("a") as f:
                f.write(json.dumps({"ts": int(time.time()),
                                    "level": level}) + "\n")
            write_status("live", phase="live", connected=True, battery=level,
                         last_seen=now_iso())
            last_battery = now_mono

        # --- flush + incremental drain --------------------------------------
        ring.data_flush()
        saved = read_cursor()
        n = 0
        with EVENTS_FILE.open("a") as f:
            for tag, payload, raw in ring.drain_events(
                    max(0, saved - (CURSOR_OVERLAP_DS if saved else 0)),
                    on_batch=write_cursor):
                f.write(json.dumps({"ts": time.time(), "tag": tag,
                                    "raw": raw.hex()}) + "\n")
                n += 1
        pending_frames += n
        write_status("live", phase="live", connected=True,
                     last_seen=now_iso(), pending_frames=pending_frames)

        # --- ingest (throttled, only when there is something new) -----------
        if pending_frames > 0 and now_mono - LAST_INGEST_MONO >= INGEST_INTERVAL_S:
            write_status("live", phase="ingesting", connected=True)
            try:
                run_pipeline()
                LAST_INGEST_SIZE = EVENTS_FILE.stat().st_size
                write_status("live", phase="live", connected=True,
                             last_sync_ok=True, last_sync_time=now_iso(),
                             last_frames=pending_frames, last_error=None)
                pending_frames = 0
            except Exception as e:
                traceback.print_exc()
                write_status("live", phase="live", connected=True,
                             last_error=f"ingest: {type(e).__name__}: {e}")
            LAST_INGEST_MONO = now_mono

        # --- hourly time-sync ------------------------------------------------
        if now_mono - last_timesync >= TIME_SYNC_INTERVAL_S:
            ring.sync_time()
            last_timesync = now_mono

        # --- subscribe toggle every 3rd cycle (§6.6) -------------------------
        if cycle % SUBSCRIBE_TOGGLE_EVERY == 0:
            try:
                ring.request(bytes([0x16, 0x01, 0x00]), timeout=3.0)
                time.sleep(2.5)
                ring.request(bytes([0x16, 0x01, 0x02]), timeout=3.0)
            except RuntimeError:
                pass  # non-fatal; next cycle re-probes the link

        time.sleep(FLUSH_INTERVAL_S)


def main() -> int:
    if not acquire_lock():
        print("daemon already running")
        return 0

    backoff_i = 0
    dongle_resets = 0
    try:
        while True:
            ring = None
            try:
                write_status("live", phase="connecting", connected=False,
                             last_error=None)
                from oura_ring import OuraRing, pick_port, load_key
                ring = OuraRing(pick_port(None))
                ring.open()
                # Long scan windows are fine here: we are a daemon, and one
                # catch = connected until the link drops.
                ring.connect(seconds=25, attempts=4)
                key = load_key()
                ring.authenticate(key)
                ring.engage_data_plane()
                ring.sync_time()
                backoff_i = 0
                dongle_resets = 0
                print(f"[daemon] connected; entering steady state "
                      f"({FLUSH_INTERVAL_S}s flush loop)")
                steady_state(ring)  # returns only by raising
            except KeyboardInterrupt:
                return 0
            except Exception as e:
                traceback.print_exc()
                if is_dongle_wedge(e) and dongle_resets < 2:
                    dongle_resets += 1
                    write_status("live", phase="dongle_reset",
                                 connected=False,
                                 last_error=f"{type(e).__name__}: {e}")
                    reset_dongle_usb()
                else:
                    delay = RECONNECT_BACKOFF_S[
                        min(backoff_i, len(RECONNECT_BACKOFF_S) - 1)]
                    backoff_i += 1
                    write_status("live", phase="reconnect_wait",
                                 connected=False,
                                 last_error=f"{type(e).__name__}: {e}")
                    print(f"[daemon] link lost ({type(e).__name__}: {e}); "
                          f"reconnecting in {delay}s")
                    maybe_ingest_offline()
                    time.sleep(delay)
            finally:
                if ring is not None:
                    try:
                        ring.close()
                    except Exception:
                        pass
    finally:
        release_lock()
        write_status("idle", phase=None, connected=False)


if __name__ == "__main__":
    sys.exit(main())
