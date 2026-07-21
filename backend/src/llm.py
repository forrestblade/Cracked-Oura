"""AI Health Analyst backed by Claude via subscription OAuth (no API key).

Replaces the previous LangChain + Ollama SQL agent with a direct Anthropic
Messages API tool-use loop:
  - auth: Bearer token from claude_auth (Claude Code-style OAuth, PKCE)
  - one read-only `sql_query` tool over the local SQLite health DB
  - thoughts are reported in the same shape the frontend already renders
    ({step, type: tool_call|tool_result, tool, params, content})
"""
import json
import logging
import os
import sqlite3
from datetime import date
from typing import Any, Dict, List

import requests

from backend.src import claude_auth
from backend.src.config import config_manager
from backend.src.paths import get_user_data_dir

logger = logging.getLogger("ClaudeAnalyst")

API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=50"
DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOOL_ROUNDS = 12

# OAuth (subscription) tokens require the Claude Code identity as the FIRST
# system block — same convention the pi harness uses.
OAUTH_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

SQL_TOOL = {
    "name": "sql_query",
    "description": (
        "Run a read-only SQL query (SQLite dialect) against the local Oura "
        "health database and get rows back as JSON. SELECT statements only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A single SELECT statement."}
        },
        "required": ["query"],
    },
}


class DataAnalyst:
    def __init__(self):
        cfg = config_manager.get_config()
        self.model = cfg.get("claude_model", DEFAULT_MODEL)
        self.db_path = os.path.join(get_user_data_dir(), "oura_database.db")
        self.system = self._build_system()

    # -- DB ------------------------------------------------------------------

    def _schema_summary(self) -> str:
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()
            con.close()
            return "\n".join(sql for _, sql in rows if sql)
        except Exception as e:
            return f"(schema unavailable: {e})"

    def _run_sql(self, query: str) -> str:
        q = query.strip().rstrip(";")
        if not q.lower().startswith(("select", "with")):
            return json.dumps({"error": "Only SELECT queries are allowed."})
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.execute(q)
            rows = [dict(r) for r in cur.fetchmany(200)]
            con.close()
            return json.dumps({"rows": rows, "row_count": len(rows),
                               "truncated": len(rows) == 200}, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    # -- Prompt --------------------------------------------------------------

    def _build_system(self) -> list:
        today = date.today().strftime("%Y-%m-%d (%A)")
        instructions = f"""You are an expert health-data analyst for the owner's Oura Ring 4.
All data is local, collected over BLE from the owner's own ring; you query it with the sql_query tool.

Database schema (SQLite):
{self._schema_summary()}

Rules:
- Today is {today}. Timestamps in the DB are LOCAL time, naive.
- Temperatures in the `temperature` table are in FAHRENHEIT.
- Use table prefixes for ambiguous columns; use strftime for date math.
- Query the data before answering; never invent numbers. If a table is empty, say so.
- Sleep/readiness/activity summary tables may be empty until the owner has worn the ring overnight — heart_rate, temperature, and ring_battery are the live tables.
- Be concise and concrete: cite the actual values and time ranges you queried.
- Format answers in Markdown."""
        return [
            {"type": "text", "text": OAUTH_SYSTEM_PREFIX},
            {"type": "text", "text": instructions},
        ]

    # -- Claude call ---------------------------------------------------------

    def _headers(self, token: str) -> dict:
        return {
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        }

    def _resolve_model(self, token: str) -> str | None:
        """Pick the best available model from the account's model list
        (newest opus, else newest sonnet, else first)."""
        try:
            r = requests.get(MODELS_URL, headers=self._headers(token), timeout=30)
            ids = [m["id"] for m in r.json().get("data", [])]
            for family in ("opus", "sonnet"):
                fam = [i for i in ids if family in i]
                if fam:
                    return fam[0]  # API lists newest first
            return ids[0] if ids else None
        except Exception as e:
            logger.error(f"Model list failed: {e}")
            return None

    def _call_claude(self, messages: list) -> dict:
        token = claude_auth.get_access_token()
        if not token:
            raise RuntimeError("Claude is not connected. Open Settings and connect your Claude account.")
        body = {
            "model": self.model,
            "max_tokens": 4096,
            "system": self.system,
            "tools": [SQL_TOOL],
            "messages": messages,
        }
        resp = requests.post(API_URL, headers=self._headers(token), json=body, timeout=120)
        if resp.status_code == 404 and "model" in resp.text:
            # Stale/unknown model id — resolve a real one and retry once.
            fallback = self._resolve_model(token)
            if fallback and fallback != self.model:
                logger.warning(f"Model '{self.model}' not found; retrying with '{fallback}'")
                self.model = fallback
                config_manager.update_config(claude_model=fallback)
                body["model"] = fallback
                resp = requests.post(API_URL, headers=self._headers(token), json=body, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # -- Agent loop ----------------------------------------------------------

    def chat(self, history: List[Dict[str, str]]) -> Dict[str, Any]:
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
        thoughts: list = []
        step = 0
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                reply = self._call_claude(messages)
                content = reply.get("content", [])
                messages.append({"role": "assistant", "content": content})

                if reply.get("stop_reason") != "tool_use":
                    text = "".join(b.get("text", "") for b in content
                                   if b.get("type") == "text")
                    return {"response": text or "(no answer)", "thoughts": thoughts}

                results = []
                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    step += 1
                    query = (block.get("input") or {}).get("query", "")
                    thoughts.append({"step": step, "type": "tool_call",
                                     "tool": "sql_query", "params": query,
                                     "content": f"SQL: {query}"})
                    out = self._run_sql(query)
                    step += 1
                    thoughts.append({"step": step, "type": "tool_result",
                                     "content": out[:2000]})
                    results.append({"type": "tool_result",
                                    "tool_use_id": block["id"], "content": out})
                messages.append({"role": "user", "content": results})

            return {"response": "I hit the tool-call limit before finishing — try a narrower question.",
                    "thoughts": thoughts}
        except Exception as e:
            logger.error(f"Claude analyst error: {e}")
            return {"response": f"I encountered an error: {e}",
                    "thoughts": thoughts + [{"step": 99, "type": "error", "content": str(e)}]}
