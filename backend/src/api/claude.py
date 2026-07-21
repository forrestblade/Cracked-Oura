"""Claude OAuth endpoints for the frontend connect flow."""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.src import claude_auth

logger = logging.getLogger("ClaudeAPI")
claude_router = APIRouter()


class FinishRequest(BaseModel):
    code: str


@claude_router.get("/api/claude/auth/status")
def auth_status():
    return claude_auth.status()


@claude_router.post("/api/claude/auth/start")
def auth_start():
    """Begin OAuth: returns the claude.ai authorize URL to open in a browser."""
    return {"auth_url": claude_auth.start_auth()}


@claude_router.post("/api/claude/auth/finish")
def auth_finish(req: FinishRequest):
    """Complete OAuth with the code the user pasted from the callback page."""
    try:
        return claude_auth.finish_auth(req.code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@claude_router.post("/api/claude/auth/logout")
def auth_logout():
    claude_auth.logout()
    return {"connected": False}
