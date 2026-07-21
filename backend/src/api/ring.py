"""Local BLE ring integration (ringlink/): status + manual sync trigger.

The ring is connect-on-demand BLE — there is no persistent connection. The
sync pipeline (ringlink/sync_ring.py, its own py3.10 venv for the Nordic
dongle driver) writes ringlink/ring_status.json at every stage; this router
exposes it plus a manual sync trigger for the frontend indicator.
"""
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("RingLink")

ring_router = APIRouter()

# backend/src/api/ring.py -> repo root -> ringlink/
RINGLINK = Path(__file__).resolve().parents[3] / "ringlink"
STATUS_FILE = RINGLINK / "ring_status.json"
LOCK_DIR = RINGLINK / ".sync.lock"
SYNC_SCRIPT = RINGLINK / "sync_ring.py"
VENV_PY = RINGLINK / "venv310" / "Scripts" / "python.exe"


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


@ring_router.get("/api/ring/status")
def ring_status():
    """Current ring link state for the UI indicator."""
    st = _read_status()
    lock_active = LOCK_DIR.exists() and (time.time() - LOCK_DIR.stat().st_mtime) < 900
    syncing = st.get("state") == "syncing" and lock_active

    # Derive a coarse indicator the frontend can color directly.
    last_sync = st.get("last_sync_time")
    stale = True
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            stale = (datetime.now(timezone.utc) - dt).total_seconds() > 40 * 60
        except ValueError:
            pass

    if syncing:
        indicator = "syncing"
    elif st.get("last_sync_ok") and not stale:
        indicator = "ok"          # synced within ~2 scheduled cycles
    elif st.get("last_sync_ok") and stale:
        indicator = "stale"       # was fine, but no recent sync
    else:
        indicator = "error"

    return {
        "available": SYNC_SCRIPT.exists() and VENV_PY.exists(),
        "indicator": indicator,
        "syncing": syncing,
        "phase": st.get("phase"),
        "battery": st.get("battery"),
        "last_seen": st.get("last_seen"),
        "last_sync_ok": st.get("last_sync_ok"),
        "last_sync_time": last_sync,
        "last_frames": st.get("last_frames"),
        "last_error": st.get("last_error"),
        "updated_at": st.get("updated_at"),
    }


@ring_router.post("/api/ring/sync")
def ring_sync():
    """Manual 'Sync now': spawn the sync pipeline detached (it self-locks)."""
    if not (SYNC_SCRIPT.exists() and VENV_PY.exists()):
        raise HTTPException(status_code=503, detail="ringlink not installed")
    if LOCK_DIR.exists() and (time.time() - LOCK_DIR.stat().st_mtime) < 900:
        raise HTTPException(status_code=409, detail="sync already running")
    try:
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        with (RINGLINK / "sync.log").open("a") as log:
            subprocess.Popen([str(VENV_PY), str(SYNC_SCRIPT)], cwd=str(RINGLINK),
                             stdout=log, stderr=subprocess.STDOUT,
                             creationflags=creation)
    except Exception as e:
        logger.error(f"Failed to spawn ring sync: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": "sync started"}
