"""Read Claude Code's native model picker without sending a user/model turn.

This is the SDK's initialize control exchange (supportedModels in TypeScript,
get_server_info in Python). Keep only model identifiers and display labels:
the rest of the response can contain private account and command metadata.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
from contextlib import suppress
from typing import Any


MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_MODELS = 512
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,255}")
VERSIONED_ID = re.compile(
    r"claude-([a-z][a-z0-9]*)-(\d+(?:-\d{1,2})*)(?:-\d{8})?(\[[a-z0-9]+\])?"
)
NATIVE_DESCRIPTION = re.compile(
    r"(?:Claude )?((?:Opus|Sonnet|Haiku|Fable|Mythos) \d+(?:\.\d+)*)"
    r"(?: with (\w+) context)?(?:\s*[·|—]|$)"
)
_PROBE_LOCK = threading.Lock()
# Candidates, not advertised options. Claude's own picker must accept each
# one before it reaches our catalog. The native default lineup omits supported
# older versions. Keep this seed aligned with Claude's supported-model docs;
# API-key installs can supply the account's Models API IDs instead.
SUPPORTED_MODEL_CANDIDATES = (
    "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5",
    "claude-haiku-4-5-20251001", "claude-opus-5", "claude-fable-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-4-6", "claude-opus-4-5-20251101",
    "claude-sonnet-4-5-20250929",
    "claude-opus-5[1m]", "claude-opus-4-8[1m]",
    "claude-opus-4-7[1m]", "claude-opus-4-6[1m]",
    "claude-sonnet-4-6[1m]",
)


class ClaudeModelCatalogUnavailable(RuntimeError):
    """A metadata probe failed; never include provider output in this error."""


def _safe_label(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in value):
        return ""
    return value.strip()


def _model_label(model: dict[str, Any], value: str) -> str:
    display = _safe_label(model.get("displayName")) or value[:160]
    # New CLI versions identify the alias target explicitly. Do not guess it
    # from the CLI's version, the newest website announcement, or our fallback.
    resolved = model.get("resolvedModel")
    matched = VERSIONED_ID.fullmatch(resolved) if isinstance(resolved, str) and len(resolved) <= 256 else None
    if matched:
        family, version, context = matched.groups()
        label = f"{family.capitalize()} {version.replace('-', '.')}"
        if context or value.endswith("[1m]"):
            label += f" ({(context or '[1m]')[1:-1].upper()} context)"
    elif not resolved:
        # Older SDK initialize responses expose the version only in the
        # picker description. Accept its leading native model name, not prose
        # mentioning an unrelated model or arbitrary numbers in custom names.
        description = model.get("description")
        match = NATIVE_DESCRIPTION.match(description) if isinstance(description, str) and len(description) <= 800 else None
        if match:
            label = match[1]
            if match[2] or value.endswith("[1m]"):
                label += f" ({(match[2] or '1M').upper()} context)"
        else:
            label = display
    else:
        # Gateway deployment IDs are opaque: retain the provider's display
        # name rather than inventing an Anthropic version for them.
        label = display
    if value == "default" and label != display:
        label = f"Default — {label}"
    return _safe_label(label) or display


def parse_native_models(info: Any) -> list[dict[str, str]]:
    if not isinstance(info, dict) or not isinstance(info.get("models"), list):
        raise ClaudeModelCatalogUnavailable("Native model metadata is unavailable")
    models = info["models"]
    if len(models) > MAX_MODELS:
        raise ClaudeModelCatalogUnavailable("Native model metadata exceeds its limit")
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        value = model.get("value")
        if not isinstance(value, str) or not MODEL_ID.fullmatch(value) or value in seen:
            continue
        seen.add(value)
        # Never replace an alias with its resolved ID: that would pin new
        # chats to today's version instead of following native updates.
        if model.get("disabled") is not True:
            options.append({"value": value, "label": _model_label(model, value)})
    if models and not seen:
        raise ClaudeModelCatalogUnavailable("Native model metadata is invalid")
    return options


def _expansion_settings(effective: Any, env: dict[str, str], candidates: tuple[str, ...]) -> dict[str, Any] | None:
    """Build a process-only picker; never replace enforcement or credentials.

    Reading the effective settings through the CLI retains its full managed
    settings precedence. Unknown schemas and third-party endpoints fail closed.
    """
    if not isinstance(effective, dict):
        return None
    settings_env = effective.get("env", {})
    if not isinstance(settings_env, dict):
        return None
    runtime_env = {**env, **settings_env}
    base = runtime_env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    if not isinstance(base, str) or base.rstrip("/") not in (
        "https://api.anthropic.com", "https://api.anthropic.com/v1",
    ):
        return None
    if any(runtime_env.get(key) for key in (
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    )):
        return None
    picker = effective.get("modelPicker", {"options": []})
    if not isinstance(picker, dict) or picker.get("replaceBuiltInOptions"):
        return None  # Respect an explicitly curated replacement lineup.
    options = picker.get("options")
    if not isinstance(options, list) or len(options) > MAX_MODELS:
        return None
    if any(not isinstance(row, dict) or not isinstance(row.get("model"), str) for row in options):
        return None
    seen = {row["model"] for row in options}
    additional = [{"model": value} for value in candidates
                  if isinstance(value, str) and MODEL_ID.fullmatch(value) and value not in seen]
    if not additional or len(options) + len(additional) > MAX_MODELS:
        return None
    settings = {"disableAllHooks": True,
                "modelPicker": {**picker, "options": [*options, *additional]}}
    if len(json.dumps(settings).encode("utf-8")) > 65536:
        return None
    return settings


def _read_initialization(
    process: subprocess.Popen, request_id: str, deadline: float,
    *, env: dict[str, str], candidates: tuple[str, ...] | None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    buffer = bytearray()
    total = 0
    models = None
    first_party = False
    effective = None
    settings_received = False
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if models is not None:
                    return models, None
                raise ClaudeModelCatalogUnavailable("Native model discovery timed out")
            if not selector.select(remaining):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                if models is not None:
                    return models, None
                raise ClaudeModelCatalogUnavailable("Native model discovery ended without metadata")
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise ClaudeModelCatalogUnavailable("Native model metadata exceeds its limit")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "control_response":
                    continue
                response = event.get("response")
                if not isinstance(response, dict):
                    continue
                if response.get("request_id") == request_id:
                    if response.get("subtype") != "success":
                        raise ClaudeModelCatalogUnavailable("Native model discovery was rejected")
                    info = response.get("response")
                    models = parse_native_models(info)
                    account = info.get("account")
                    first_party = isinstance(account, dict) and account.get("apiProvider") == "firstParty"
                    if not models or not first_party or candidates is None:
                        return models, None
                elif candidates is not None and response.get("request_id") == request_id + "-settings":
                    settings_received = True
                    info = response.get("response")
                    if response.get("subtype") == "success" and isinstance(info, dict):
                        effective = info.get("effective")
                if models is not None and settings_received:
                    return models, _expansion_settings(effective, env, candidates) if first_party else None


def probe_native_models(
    executable: str, *, env: dict[str, str], timeout: float,
    candidates: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    """Bound one private, tool-free metadata process on supported server OSes.

    No prompt, resume ID, model request, or account information is returned.
    The caller supplies the same scrubbed runtime environment as real turns.
    A failed probe is optional discovery, not an authentication failure.
    """
    if os.name != "posix":
        raise ClaudeModelCatalogUnavailable("Native model discovery is unavailable on this platform")
    deadline = time.monotonic() + timeout
    # Multiple devices refreshing must not launch a herd of Claude processes.
    if timeout <= 0 or not _PROBE_LOCK.acquire(timeout=timeout):
        raise ClaudeModelCatalogUnavailable("Native model discovery is busy")
    try:
        if time.monotonic() >= deadline:
            raise ClaudeModelCatalogUnavailable("Native model discovery timed out")
        # Resolve relative executable paths before moving to a disposable cwd.
        if os.sep in executable:
            executable = str(Path(executable).absolute())
        args = [
            executable, "--print", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools", "",
            "--setting-sources", "user", "--settings", '{"disableAllHooks":true}',
        ]
        request_id = "agentsdock-model-catalog"
        request = {"type": "control_request", "request_id": request_id,
                   "request": {"subtype": "initialize", "hooks": {}, "agents": {}, "skills": []}}
        settings_request = {"type": "control_request", "request_id": request_id + "-settings",
                            "request": {"subtype": "get_settings"}}
        # Reading the catalog must not silently upgrade the user's runtime.
        child_env = {**env, "DISABLE_AUTOUPDATER": "1"}

        def run_probe(settings=None):
            probe_args = list(args)
            if settings is not None:
                probe_args[-1] = json.dumps(settings)
            requests = [request] if settings is not None else [request, settings_request]
            return _run_probe(probe_args, requests, child_env, request_id, deadline,
                              candidates=None if settings is not None else (
                                  SUPPORTED_MODEL_CANDIDATES if candidates is None else candidates))

        models, expansion = run_probe()
        if expansion is not None and time.monotonic() < deadline:
            try:
                expanded, _ = run_probe(expansion)
            except (ClaudeModelCatalogUnavailable, OSError, subprocess.SubprocessError):
                return models  # Optional expansion must not lose native choices.
            # Only append requested candidates that survived the native picker;
            # never union the seed directly or change existing alias/default IDs.
            allowed = {row["model"] for row in expansion["modelPicker"]["options"]}
            allowed.update(row["value"] for row in models)
            # The second result also wins if policy changed between probes:
            # don't resurrect a baseline row the native picker now excludes.
            models = [row for row in expanded if row["value"] in allowed]
        return models
    finally:
        _PROBE_LOCK.release()


def _run_probe(args, requests, child_env, request_id, deadline, *, candidates):
    with tempfile.TemporaryDirectory(prefix="agentsdock-model-catalog-") as cwd:
        process = subprocess.Popen(
            args, cwd=cwd, env=child_env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write("".join(json.dumps(request) + "\n" for request in requests).encode())
            process.stdin.close()
            return _read_initialization(process, request_id, deadline, env=child_env, candidates=candidates)
        finally:
            try:
                # Only this fresh probe's process group; never a live chat.
                # Kill/reap even after a response so unexpected descendants
                # cannot outlive a catalog HTTP request or hold its pipes.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS can return EPERM for an already-exited group.
                    # If the child is still alive, explicitly kill it; do
                    # not let cleanup turn into an unbounded wait.
                    if process.poll() is None:
                        process.kill()
                process.wait(timeout=1.0)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        with suppress(OSError):
                            stream.close()
