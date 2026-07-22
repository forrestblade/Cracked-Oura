import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.src.api.routes import router
from backend.src.api.ring import ring_router
from backend.src.api.claude import claude_router
from backend.src.database import init_db
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
    init_db()
    yield


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
