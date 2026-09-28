"""Cursor-issued keys, not OpenAI-compatible endpoints or a machine login.

Cursor exchanges API keys for tokens and normally persists those tokens in the
user's credential store. Use its in-memory store for every key-bound process;
an inherited auth token must never take precedence over the selected API key.
"""
from __future__ import annotations

import re
import tempfile

from fastapi import HTTPException
from cursor_agent_client import parse_cursor_models_list
from side_questions import run_isolated_command, SideQuestionError

ENDPOINT = "https://api2.cursor.sh"
MIN_VERSION = (2026, 9, 26)  # First build verified with memory credential isolation.


def overrides(key: str) -> dict[str, str]:
    return {
        "CURSOR_API_KEY": key, "CURSOR_AUTH_TOKEN": "",
        "AGENT_CLI_CREDENTIAL_STORE": "memory",
        "CURSOR_API_ENDPOINT": ENDPOINT, "CURSOR_AGENT_CLI_LOCAL_MODE": "false",
        "DIRENV_DISABLE": "1",
    }


async def require_isolation(executable: str | None, env: dict) -> None:
    if not executable:
        raise HTTPException(409, "Install Cursor CLI to use a Cursor API key.")
    try:
        with tempfile.TemporaryDirectory(prefix="agentsdock-cursor-version-") as cwd:
            output = await run_isolated_command([executable, "--version"], prompt="", cwd=cwd,
                env={**env, **overrides("")}, timeout=5)
        match = re.search(r"\b(20\d\d)\.(\d{2})\.(\d{2})\b", output)
        if not match or tuple(map(int, match.groups())) < MIN_VERSION:
            raise ValueError("unsupported build")
    except (SideQuestionError, ValueError):
        raise HTTPException(409, "Update Cursor CLI to 2026.09.26 or later to isolate API keys from CLI login.") from None


async def catalog(selected: dict, *, executable: str | None, env: dict) -> dict:
    try:
        await require_isolation(executable, env)
    except HTTPException:
        return {"models": [], "discovery_status": "unavailable", "cli_update_required": True}
    try:
        # Read-only authenticated request; no conversation, model inference,
        # project tools, or native credential writes. Never reflect CLI stderr.
        with tempfile.TemporaryDirectory(prefix="agentsdock-cursor-key-") as cwd:
            output = await run_isolated_command([executable, "--list-models"], prompt="", cwd=cwd,
                env={**env, **overrides(selected["api_key"])}, timeout=20)
        models = [{"value": item["id"], "label": item["label"]}
                  for item in parse_cursor_models_list(output)
                  if re.fullmatch(r"[\x21-\x7e]{1,256}", item["id"])
                  and selected["api_key"] not in item["id"] + item["label"]]
        if not models:
            return {"models": [], "discovery_status": "unavailable"}
        return {"models": models[:256], "discovery_status": "ready"}
    except SideQuestionError:
        # Offline and invalid keys can look identical in CLI output. Do not
        # misclassify an outage as a logout or return key-bearing stderr.
        return {"models": [], "discovery_status": "unavailable"}
