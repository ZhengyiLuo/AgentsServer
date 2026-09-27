"""Passive Claude model metadata from real SDK connections.

Catalog reads never start a CLI, renew credentials, or terminate a process.
Only bounded model IDs/labels are retained; account/command data is discarded.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import unicodedata
from typing import Any

MAX_MODELS = 512
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,255}")
VERSIONED_ID = re.compile(
    r"claude-([a-z][a-z0-9]*)-(\d+(?:-\d{1,2})*)(?:-\d{8})?(\[[a-z0-9]+\])?"
)
NATIVE_DESCRIPTION = re.compile(
    r"(?:Claude )?((?:Opus|Sonnet|Haiku|Fable|Mythos) \d+(?:\.\d+)*)"
    r"(?: with (\w+) context)?(?:\s*[·|—]|$)"
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



CACHE_TTL_SECONDS = 300.0
CACHE_LIMIT = 16
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[str, tuple[float, list[dict[str, str]]]] = OrderedDict()


def _file_revision(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        return (str(path.absolute()), None)


def _account_identity(config: Path, home: Path) -> list:
    # Claude writes counters and picker caches to .claude.json on every
    # startup. Its mtime is not a settings revision. Only fingerprint the
    # account identity; never retain email, tokens or the whole document.
    paths = {home / ".claude.json", config / ".claude.json"}
    result = []
    for path in sorted(paths):
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Native identity metadata exceeds its limit")
            value = json.loads(raw)
        except FileNotFoundError:
            value = {}
        account = value.get("oauthAccount", {}) if isinstance(value, dict) else {}
        result.append([str(path), *(
            str(account.get(field) or "")[:256] if isinstance(account, dict) else ""
            for field in ("accountUuid", "organizationUuid")
        )])
    return result


def native_catalog_key(executable: str, env: dict[str, str]) -> str:
    """Fingerprint runtime/config identity without reading native credentials.

    Environment credentials are hashed, never retained in plaintext. Native
    credential rotation alone is not evidence of login or logout; this cache
    is short lived and is never used as authentication/authorization evidence.
    """
    home = Path(env.get("HOME") or str(Path.home()))
    config = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    relevant = {key: value for key, value in env.items()
                if key.startswith(("ANTHROPIC_", "CLAUDE_")) or key == "HOME"}
    resolved = shutil.which(executable, path=env.get("PATH")) or executable
    revisions = [_file_revision(Path(resolved)), *(
        _file_revision(path) for path in (
            config / "settings.json", config / ".credentials.json",
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
            Path("/etc/claude-code/managed-settings.json"),
        )
    )]
    return hashlib.sha256(json.dumps([relevant, revisions, _account_identity(config, home)], sort_keys=True).encode()).hexdigest()


def _has_project_settings(cwd: str, env: dict[str, str]) -> bool:
    """Do not promote a workspace-specific picker to the server-wide catalog."""
    root = Path(cwd).resolve()
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME") or str(Path.home())) / ".claude").resolve()
    for parent in (root, *root.parents):
        folder = parent / ".claude"
        for name in ("settings.json", "settings.local.json"):
            path = folder / name
            if name == "settings.json" and folder.resolve() == config:
                continue  # This is the fingerprinted user config, not project config.
            if path.exists():
                return True
    return False


def remember_native_models(info: Any, *, key: str, executable: str,
                           env: dict[str, str], cwd: str) -> None:
    # Recheck the pre-connect revision: a slow/old connection must not publish
    # a model list under a newer CLI, provider, or user-settings configuration.
    if key != native_catalog_key(executable, env) or _has_project_settings(cwd, env):
        return
    models = parse_native_models(info)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), models)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_LIMIT:
            _CACHE.popitem(last=False)


def cached_native_models(executable: str, *, env: dict[str, str]) -> list[dict[str, str]] | None:
    key = native_catalog_key(executable, env)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[0] >= CACHE_TTL_SECONDS:
            del _CACHE[key]
            return None
        return [dict(row) for row in entry[1]]


def clear_native_models() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
