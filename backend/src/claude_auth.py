"""Claude OAuth connector — the same subscription OAuth flow the pi coding
harness / Claude Code CLI use (PKCE, no API key required).

Flow:
  1. start_auth()  -> authorize URL on claude.ai (user logs in with their
                      Claude Pro/Max account) + PKCE verifier kept server-side.
  2. User is shown a code ("code#state") on the callback page and pastes it
     into the app.
  3. finish_auth(code) -> exchanges it at console.anthropic.com for
     access + refresh tokens, persisted in %APPDATA%/CrackedOura/.
  4. get_access_token() -> auto-refreshes when expired.

Tokens grant `user:inference` — Messages API calls authenticate with
`Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20`.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .paths import get_user_data_dir

logger = logging.getLogger("ClaudeAuth")

# Public client id used by Claude Code / pi for subscription OAuth
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"

TOKEN_FILE = Path(get_user_data_dir()) / "claude_oauth.json"
_pending_verifier: str | None = None


def _save_tokens(data: dict):
    payload = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60,
    }
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, TOKEN_FILE)


def _load_tokens() -> dict | None:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def start_auth() -> str:
    """Generate PKCE pair and return the authorize URL to open in a browser."""
    global _pending_verifier
    _pending_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(_pending_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return (
        AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "code": "true",
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": _pending_verifier,
            }
        )
    )


def finish_auth(pasted_code: str) -> dict:
    """Exchange the pasted `code#state` for tokens and persist them."""
    global _pending_verifier
    if not _pending_verifier:
        raise RuntimeError("No auth in progress — click Connect first")
    pasted_code = pasted_code.strip()
    code, _, state = pasted_code.partition("#")
    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "state": state or _pending_verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": _pending_verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:200]}"
        )
    _save_tokens(resp.json())
    _pending_verifier = None
    logger.info("Claude OAuth connected.")
    return {"connected": True}


def _refresh(tokens: dict) -> dict | None:
    if not tokens.get("refresh_token"):
        return None
    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        logger.error(
            f"Claude token refresh failed: {resp.status_code} {resp.text[:200]}"
        )
        return None
    data = resp.json()
    if "refresh_token" not in data:
        data["refresh_token"] = tokens["refresh_token"]
    _save_tokens(data)
    return _load_tokens()


def get_access_token() -> str | None:
    """Valid access token, refreshing if needed. None if not connected."""
    tokens = _load_tokens()
    if not tokens:
        return None
    if time.time() >= tokens.get("expires_at", 0):
        tokens = _refresh(tokens)
        if not tokens:
            return None
    return tokens["access_token"]


def status() -> dict:
    tokens = _load_tokens()
    if not tokens:
        return {"connected": False}
    return {
        "connected": True,
        "expires_at": tokens.get("expires_at"),
        "needs_refresh": time.time() >= tokens.get("expires_at", 0),
    }


def logout():
    global _pending_verifier
    _pending_verifier = None
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
