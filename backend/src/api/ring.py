"""Local BLE ring integration (ringlink/): status + manual sync trigger.

The ring is connect-on-demand BLE — there is no persistent connection. The
sync pipeline (ringlink/sync_ring.py, its own py3.10 venv for the Nordic
dongle driver) writes ringlink/ring_status.json at every stage; this router
exposes it plus a manual sync trigger for the frontend indicator.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("RingLink")

ring_router = APIRouter()

# backend/src/api/ring.py -> repo root -> ringlink/. The relative walk breaks
# in a frozen (PyInstaller) build, so allow an env/config override.
_default_ringlink = Path(__file__).resolve().parents[3] / "ringlink"
RINGLINK = Path(os.environ.get("RINGLINK_DIR", str(_default_ringlink)))
STATUS_FILE = RINGLINK / "ring_status.json"
LOCK_DIR = RINGLINK / ".sync.lock"
SYNC_SCRIPT = RINGLINK / "sync_ring.py"
VENV_PY = RINGLINK / "venv310" / "Scripts" / "python.exe"

# A healthy sync run now finishes in < 3 min (fail-fast connect cycles +
# incremental drains). Beyond these windows the run is considered dead.
LOCK_STALE_S = 420  # lock older than this = crashed run; allow new sync
HEARTBEAT_STALE_S = 240  # status not updated for this long = wedged


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _daemon_alive() -> bool:
    lock = RINGLINK / ".daemon.lock"
    if not lock.exists():
        return False
    try:
        return _pid_alive(int(lock.read_text().strip()))
    except Exception:
        return False


@ring_router.get("/api/ring/status")
def ring_status():
    """Current ring link state for the UI indicator."""
    st = _read_status()
    lock_active = (
        LOCK_DIR.exists() and (time.time() - LOCK_DIR.stat().st_mtime) < LOCK_STALE_S
    )

    heartbeat_fresh = False
    if st.get("updated_at"):
        try:
            hb = datetime.fromisoformat(st["updated_at"])
            heartbeat_fresh = (
                datetime.now(timezone.utc) - hb
            ).total_seconds() < HEARTBEAT_STALE_S
        except ValueError:
            pass

    live = st.get("state") == "live" and _daemon_alive()
    live_connected = (
        live and heartbeat_fresh and st.get("phase") in ("live", "ingesting")
    )
    wedged = (
        (st.get("state") == "syncing" and lock_active)
        or (st.get("state") == "live" and _daemon_alive())
    ) and not heartbeat_fresh
    # Hunting = daemon alive but the ring's radio is between advertising
    # waves (normal while worn — it can nap for many minutes). This is NOT
    # "syncing": surfacing every connect/reconnect_wait flip made the
    # indicator flap. Data is never lost; the ring buffers and back-fills.
    hunting = (
        live and heartbeat_fresh
        and st.get("phase") in ("connecting", "retry_wait",
                                "reconnect_wait", "dongle_reset")
    )
    syncing = (
        st.get("state") == "syncing" and lock_active and heartbeat_fresh
    ) or (live and heartbeat_fresh and not live_connected and not hunting)

    # Derive a coarse indicator the frontend can color directly.
    last_sync = st.get("last_sync_time")
    stale = True
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            stale = (datetime.now(timezone.utc) - dt).total_seconds() > 40 * 60
        except ValueError:
            pass

    if wedged:
        indicator = "error"
    elif live_connected:
        indicator = "ok"  # persistent link up, data flowing
    elif syncing:
        indicator = "syncing"
    elif hunting:
        indicator = "waiting"  # daemon fine; ring radio napping (dock to catch)
    elif st.get("last_sync_ok") and not stale:
        indicator = "ok"  # synced within ~2 scheduled cycles
    elif st.get("last_sync_ok") and stale:
        indicator = "stale"  # was fine, but no recent sync
    else:
        indicator = "error"

    return {
        "available": SYNC_SCRIPT.exists() and VENV_PY.exists(),
        "indicator": indicator,
        "syncing": syncing,
        "waiting": hunting,
        "live": live_connected,
        "phase": st.get("phase"),
        "attempt": st.get("attempt"),
        "battery": st.get("battery"),
        "last_seen": st.get("last_seen"),
        "last_sync_ok": st.get("last_sync_ok"),
        "last_sync_time": last_sync,
        "last_frames": st.get("last_frames"),
        "last_error": (
            "sync process wedged or killed (no heartbeat)"
            if wedged
            else st.get("last_error")
        ),
        "updated_at": st.get("updated_at"),
    }


@ring_router.post("/api/ring/sync")
def ring_sync():
    """Manual 'Sync now': spawn the sync pipeline detached (it self-locks)."""
    if not (SYNC_SCRIPT.exists() and VENV_PY.exists()):
        raise HTTPException(status_code=503, detail="ringlink not installed")
    if _daemon_alive():
        st = _read_status()
        if st.get("connected"):
            return {
                "message": "Ring is live-connected — data refreshes "
                "every 20 s automatically."
            }
        return {
            "message": "Daemon is hunting — the ring's radio sleeps "
            "between advertising waves. Dock the ring ~5 s to "
            "force an instant catch; missed chart data "
            "back-fills automatically."
        }
    if LOCK_DIR.exists() and (time.time() - LOCK_DIR.stat().st_mtime) < LOCK_STALE_S:
        raise HTTPException(status_code=409, detail="sync already running")
    try:
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        with (RINGLINK / "sync.log").open("a") as log:
            subprocess.Popen(
                [str(VENV_PY), "-u", str(SYNC_SCRIPT)],
                cwd=str(RINGLINK),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creation,
            )
    except Exception as e:
        logger.error(f"Failed to spawn ring sync: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "sync started"}
