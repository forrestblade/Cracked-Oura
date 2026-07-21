import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.src.api.routes import router
from backend.src.api.ring import ring_router
from backend.src.api.claude import claude_router
from backend.src.automation import automator
from backend.src.config import config_manager
from backend.src.database import SessionLocal, init_db
from backend.src.ingestion import OuraParser
from backend.src.paths import get_user_data_dir

# Configure logging
log_dir = get_user_data_dir()
log_file = os.path.join(log_dir, "backend_debug.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
logger = logging.getLogger("API")
logger.info(f"API Starting... Logging to {log_file}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()

    # Reset status on startup in case it was stuck
    cfg = config_manager.get_config()
    if cfg.get("status") not in ["Idle", "Error"]:
        logger.info("Startup: Resetting stuck status to Idle.")
        config_manager.update_status("Idle")

    # Start background worker (keep the reference so it isn't GC'd)
    task = asyncio.create_task(background_worker())

    yield

    # Shutdown: stop the worker cleanly
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Cracked Oura API",
    description="API for accessing Oura Ring data stored in local SQLite database.",
    version="0.1.0",
    lifespan=lifespan,
)

# Configure CORS. This is a localhost-only app and the frontend never sends
# cookies, so a wildcard origin without credentials is both valid and safe
# (wildcard + allow_credentials=True is rejected by browsers per the spec).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router)
app.include_router(ring_router)
app.include_router(claude_router)


# --- API Models for Automation ---
class AutomationConfig(BaseModel):
    email: str
    schedule_time: str
    is_active: bool
    headless: bool = True


# --- Endpoints ---


@app.get("/api/automation/status")
async def get_automation_status():
    """Returns the current automation configuration and status."""
    return config_manager.get_config()


@app.post("/api/automation/config")
async def update_automation_config(config: AutomationConfig):
    """Updates automation settings."""
    config_manager.update_config(
        email=config.email,
        schedule_time=config.schedule_time,
        is_active=config.is_active,
        headless=config.headless,
    )
    # Configure automator with new email settings immediately
    automator.email = config.email

    return {"status": "success", "message": "Configuration updated."}


# NOTE: /api/automation/submit-otp and /api/automation/clear-session are
# served by routes.py (registered first, so it always won the route match).
# The duplicate, unreachable definitions that used to live here were removed.


@app.post("/api/automation/run-now")
async def run_automation(background_tasks: BackgroundTasks):
    """
    Manually triggers the full "Request New + Download" flow.
    """
    logger.info("Manual automation trigger received.")
    config_manager.update_status("Starting manual run...")

    try:
        # Initialize if needed
        cfg = config_manager.get_config()
        await automator.initialize(headless=cfg.get("headless", False))
        automator.email = cfg.get("email", "")

        background_tasks.add_task(run_ingestion_task, force=True)
        return {"status": "started", "message": "Automation started."}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/automation/test-login")
async def test_login():
    """Tests the login functionality with current credentials."""
    try:
        config_manager.update_status("Testing Login...")
        cfg = config_manager.get_config()
        await automator.initialize(headless=cfg.get("headless", False))
        automator.email = cfg.get("email", "")
        res = await automator.login()
        if res and res.get("status") == "otp_required":
            config_manager.update_status("Waiting for OTP...")
            return {"status": "otp_required", "message": "OTP Required"}

        config_manager.update_status("Login Check Complete.")
        await automator.cleanup()  # Close browser if successful
        return res
    except Exception as e:
        config_manager.update_status(f"Login Error: {str(e)}")
        return {"status": "error", "message": str(e)}


async def run_download_existing_task():
    """
    Standalone task for downloading existing export.
    """
    logger.info("Starting download existing task...")
    try:
        cfg = config_manager.get_config()
        # Ensure automator is initialized and configured
        if not automator._is_initialized:
            await automator.initialize(headless=cfg.get("headless", True))

        automator.email = cfg.get("email", "")

        # Use user data dir for downloads
        save_dir = str(get_user_data_dir())

        result = await automator.download_existing_export(save_dir=save_dir)

        if isinstance(result, dict) and result.get("status") == "otp_required":
            config_manager.update_status("Waiting for OTP...")
            return

        file_path = result

        if file_path:
            logger.info(f"Export downloaded to {file_path}. Starting ingestion...")
            await process_ingestion(file_path)
        else:
            logger.info("No existing export found.")

        # Cleanup on success (if not waiting for OTP)
        await automator.cleanup()

    except Exception as e:
        logger.error(f"Download task failed: {e}")
        await automator.cleanup()  # Cleanup on error


@app.post("/api/automation/download-latest")
async def download_latest_existing(background_tasks: BackgroundTasks):
    """Downloads the latest EXISTING export (if any). Does NOT request new."""
    background_tasks.add_task(run_download_existing_task)
    return {"status": "started", "message": "Checking for existing downloads..."}


# --- Background Logic ---


async def run_ingestion_task(force=False):
    """
    The core logic for checking, requesting, and downloading data.
    """
    cfg = config_manager.get_config()
    if not force and not cfg.get("is_active", True):
        return

    logger.info("Background worker: Starting ingestion task...")
    config_manager.update_status("Starting...")

    try:
        # 1. Initialize
        config_manager.update_status("Initializing...")
        headless_mode = cfg.get("headless", True)
        await automator.initialize(headless=headless_mode)

        # Configure credentials
        automator.email = cfg.get("email", "")

        # Check login first
        login_res = await automator.login()
        if login_res and login_res.get("status") == "otp_required":
            logger.info("Background worker: OTP Required.")
            config_manager.update_status("Waiting for OTP...")
            return

        # 2. Run Full Automation (Request -> Wait -> Download)
        config_manager.update_status("Running Automation...")

        # Use user data dir for downloads
        save_dir = str(get_user_data_dir())

        # This function handles login, requesting, waiting, and downloading
        result = await automator.request_new_export_and_download(save_dir=save_dir)

        if isinstance(result, dict) and result.get("status") == "otp_required":
            config_manager.update_status("Waiting for OTP...")
            return

        file_path = result

        if file_path:
            logger.info(f"Background worker status: Downloaded to {file_path}")
            config_manager.update_status("Downloading...")

            # 3. Ingest
            await process_ingestion(file_path)
        else:
            logger.info("Background worker: No file downloaded (Timeout or Error).")
            config_manager.update_status("Failed to download export.")

        # Cleanup on success
        await automator.cleanup()

    except Exception as e:
        logger.error(f"Background worker error: {e}")
        config_manager.update_status(f"Error: {str(e)}")
        await automator.cleanup()  # Cleanup on error


async def process_ingestion(zip_path):
    logger.info(f"Background worker: Downloaded to {zip_path}")

    # Ingest
    config_manager.update_status("Ingesting...")
    db = SessionLocal()
    try:
        parser = OuraParser(db)
        parser.parse_zip(zip_path)
        logger.info("Background worker: Ingestion successful.")

        # Success!
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        config_manager.update_status("Idle", last_run=now_str)

    except Exception as e:
        logger.error(f"Background worker: Ingestion failed: {e}")
        config_manager.update_status(f"Ingestion Failed: {str(e)}")
    finally:
        db.close()


async def background_worker():
    logger.info("Background worker started.")
    # Date we last fired the daily run for. Seeded to "today" when the
    # backend starts after today's scheduled time so a restart doesn't
    # immediately kick off a surprise cloud-export run.
    last_scheduled_run_date = None
    first_check = True
    while True:
        try:
            # Check every minute if it's time to run
            now = datetime.now()
            cfg = config_manager.get_config()

            # Calculate next run time for display
            schedule_time_str = cfg.get("schedule_time", "11:00")
            try:
                sh, sm = map(int, schedule_time_str.split(":"))
                run_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
                if now > run_today:
                    next_run = run_today + timedelta(days=1)
                else:
                    next_run = run_today

                # Only rewrite the config file when next_run actually changes
                # (this used to rewrite it every 60 seconds).
                next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S")
                if cfg.get("next_run") != next_run_str:
                    config_manager.update_config(next_run=next_run_str)

                if first_check:
                    first_check = False
                    if now >= run_today:
                        last_scheduled_run_date = now.date()

                # Fire the daily run once we're past the scheduled time.
                # Comparing >= (not ==) means a skipped/slow loop iteration
                # can no longer silently miss the scheduled minute.
                if now >= run_today and last_scheduled_run_date != now.date():
                    last_scheduled_run_date = now.date()
                    await run_ingestion_task()

                # If in "Waiting" state, poll every 5 minutes
                elif "Waiting" in cfg.get("status", ""):
                    if now.minute % 5 == 0:
                        logger.info("Background worker: Polling for export status...")
                        await run_ingestion_task()

            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Sleep 60 seconds
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("Background worker cancelled; shutting down.")
            raise
        except Exception as e:
            logger.error(f"Background worker loop error: {e}")
            await asyncio.sleep(60)


# Mount Static Files
# Robustly find the frontend/dist directory relative to this file
# backend/src/api/main.py -> ../../../frontend/dist
current_dir = os.path.dirname(os.path.abspath(__file__))
dist_dir = os.path.join(current_dir, "../../../frontend/dist")

if os.path.exists(dist_dir):
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    import sys

    # Check if running as a PyInstaller bundle
    if getattr(sys, "frozen", False):
        try:
            # Production (Frozen)
            uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
        except Exception as e:
            # Emergency logging if startup fails
            import traceback

            try:
                log_path = os.path.join(get_user_data_dir(), "startup_crash.log")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"Startup Crash: {e}\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass  # Failed to write log
            raise e
    else:
        # Development. NO --reload: on Windows the reloader wedges and
        # orphans workers that keep serving stale code on :8000
        # (see SESSION-HANDOFF-2026-07-21.md, gotcha #1).
        uvicorn.run(
            "backend.src.api.main:app", host="127.0.0.1", port=8000, reload=False
        )
