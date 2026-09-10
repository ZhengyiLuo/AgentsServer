"""Bounded, display-safe provider command inventories.

Provider discovery returns filesystem paths and other provider-owned metadata.
This module keeps that material on AgentsServer and projects only the small
allowlist needed by clients.  A client selects an opaque id plus the inventory
revision; it never sends a path or provider command name back to the server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MAX_PROVIDER_COMMANDS = 512
MAX_PROVIDER_COMMAND_NAME_CHARS = 128
MAX_PROVIDER_COMMAND_LABEL_CHARS = 160
MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS = 800
MAX_PROVIDER_COMMAND_PATH_CHARS = 4096

_COMMAND_NAME_RE = re.compile(
    rf"[A-Za-z0-9_][A-Za-z0-9_.:-]{{0,{MAX_PROVIDER_COMMAND_NAME_CHARS - 1}}}"
)
_LOCATION_RE = re.compile(
    # Provider metadata is optional display copy, so redact the entire suffix
    # after a URL/path marker. Local paths may contain spaces and punctuation;
    # trying to guess their endpoint can expose the unredacted tail.
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[\s\S]*)"
    r"|(?P<path>(?<![A-Za-z0-9_])(?:~[/\\]|\.\.?[/\\]|[A-Za-z]:[/\\]|/|\\\\)[\s\S]*)"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
    r"(?![A-Za-z0-9.-])"
)
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_BIDI_CONTROLS = frozenset({
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
})


@dataclass(frozen=True)
class ProviderCommandRecord:
    public: dict[str, Any]
    native: dict[str, str]


@dataclass(frozen=True)
class ProviderCommandInventory:
    backend: str
    revision: str
    records: tuple[ProviderCommandRecord, ...]
    truncated: bool = False

    @property
    def commands(self) -> list[dict[str, Any]]:
        return [dict(record.public) for record in self.records]

    def resolve(self, command_id: str) -> ProviderCommandRecord | None:
        for record in self.records:
            if record.public["id"] == command_id:
                return record
        return None


def canonical_provider_command_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = unicodedata.normalize("NFC", value)
    if candidate != value or candidate != candidate.strip():
        return None
    if _COMMAND_NAME_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _replace_local_path(match: re.Match[str]) -> str:
    url = match.group("url")
    if url is not None:
        # Even HTTP(S) user info, hosts, paths and query parameters can contain
        # credentials or personally identifying local infrastructure data.
        return "<url>"
    return "<path>"


def sanitize_provider_command_text(value: Any, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value)
    safe = "".join(
        character
        for character in normalized
        if character not in _BIDI_CONTROLS
        and (
            character in {"\n", "\r", "\t"}
            or unicodedata.category(character) not in _UNSAFE_CATEGORIES
        )
    )
    safe = _EMAIL_RE.sub("<email>", safe)
    safe = _LOCATION_RE.sub(_replace_local_path, safe)
    safe = " ".join(safe.split())
    if len(safe) <= max_chars:
        return safe
    return safe[: max(0, max_chars - 1)].rstrip() + "…"


def _opaque_command_id(
    selector_secret: str,
    binding_context: str,
    backend: str,
    cwd: str,
    name: str,
    native_identity: str,
) -> str:
    payload = json.dumps(
        [
            "provider-command-v1",
            binding_context,
            backend,
            cwd,
            name,
            native_identity,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "pcmd_" + hmac.new(
        selector_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()[:32]


def _inventory_revision(
    selector_secret: str,
    binding_context: str,
    backend: str,
    cwd: str,
    records: Iterable[ProviderCommandRecord],
    *,
    truncated: bool,
) -> str:
    payload = {
        "version": 1,
        "binding_context": binding_context,
        "backend": backend,
        "cwd": cwd,
        "truncated": truncated,
        # Provider order is presentation-only and may change across otherwise
        # identical cold connects. A semantic inventory revision must not
        # invalidate a queued selection solely because that order changed.
        "commands": sorted(
            (record.public for record in records),
            key=lambda item: str(item.get("id") or ""),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "pcmdrev_" + hmac.new(
        selector_secret.encode("utf-8"),
        encoded,
        hashlib.sha256,
    ).hexdigest()[:32]


def _inventory(
    selector_secret: str,
    binding_context: str,
    backend: str,
    cwd: str,
    records: list[ProviderCommandRecord],
    *,
    truncated: bool,
) -> ProviderCommandInventory:
    frozen = tuple(records)
    return ProviderCommandInventory(
        backend=backend,
        revision=_inventory_revision(
            selector_secret,
            binding_context,
            backend,
            cwd,
            frozen,
            truncated=truncated,
        ),
        records=frozen,
        truncated=truncated,
    )


def codex_provider_command_inventory(
    value: Any,
    *,
    cwd: str,
    selector_secret: str,
    binding_context: str,
    limit: int = MAX_PROVIDER_COMMANDS,
) -> ProviderCommandInventory:
    """Project one cwd entry from Codex ``skills/list``.

    The provider-resolved SKILL.md path remains only in ``record.native`` and
    is never part of ``record.public``.
    """

    raw_entries = value.get("data") if isinstance(value, dict) else None
    matching: dict[str, Any] | None = None
    if isinstance(raw_entries, list):
        normalized_cwd = str(Path(cwd).resolve())
        for entry in raw_entries[:32]:
            if not isinstance(entry, dict):
                continue
            raw_cwd = entry.get("cwd")
            if not isinstance(raw_cwd, str):
                continue
            try:
                candidate_cwd = str(Path(raw_cwd).resolve())
            except (OSError, RuntimeError):
                continue
            if candidate_cwd == normalized_cwd:
                matching = entry
                break
    raw_skills = matching.get("skills") if isinstance(matching, dict) else None
    if not isinstance(raw_skills, list):
        raw_skills = []
    bounded_limit = max(1, min(int(limit), MAX_PROVIDER_COMMANDS))
    records: list[ProviderCommandRecord] = []
    seen_ids: set[str] = set()
    scanned = 0
    for raw in raw_skills:
        if scanned >= bounded_limit:
            break
        scanned += 1
        if not isinstance(raw, dict) or raw.get("enabled") is False:
            continue
        name = canonical_provider_command_name(raw.get("name"))
        raw_path = raw.get("path")
        if (
            name is None
            or not isinstance(raw_path, str)
            or not raw_path
            or len(raw_path) > MAX_PROVIDER_COMMAND_PATH_CHARS
            or "\x00" in raw_path
            or not Path(raw_path).is_absolute()
        ):
            continue
        interface = raw.get("interface")
        if not isinstance(interface, dict):
            interface = {}
        label = sanitize_provider_command_text(
            interface.get("displayName") or name,
            MAX_PROVIDER_COMMAND_LABEL_CHARS,
        ) or name
        description = sanitize_provider_command_text(
            interface.get("shortDescription") or raw.get("description"),
            MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS,
        )
        raw_scope = str(raw.get("scope") or "").strip().lower()
        scope = raw_scope if raw_scope in {"project", "user", "admin", "system"} else None
        command_id = _opaque_command_id(
            selector_secret,
            binding_context,
            "codex",
            cwd,
            name,
            raw_path,
        )
        if command_id in seen_ids:
            continue
        seen_ids.add(command_id)
        public = {
            "id": command_id,
            "name": name,
            "label": label,
            "description": description,
            "scope": scope,
            "source": "plugin" if raw.get("pluginId") else "codex",
            "kind": "skill",
            # AgentsDock presents one slash-command palette for every provider.
            # The server converts this slash token into Codex's native text +
            # structured skill input only after validating the opaque id.
            "invocation": f"/{name}",
        }
        records.append(
            ProviderCommandRecord(
                public=public,
                native={"name": name, "path": raw_path},
            )
        )
    return _inventory(
        selector_secret,
        binding_context,
        "codex",
        cwd,
        records,
        truncated=len(raw_skills) > bounded_limit,
    )


def claude_provider_command_inventory(
    value: Any,
    *,
    cwd: str,
    selector_secret: str,
    binding_context: str,
    control_generation: str | None = None,
    limit: int = MAX_PROVIDER_COMMANDS,
) -> ProviderCommandInventory:
    """Project only Claude's provider-reported command metadata.

    ``get_server_info`` also contains account data.  Callers should already
    project it inside the SDK actor, and this second allowlist prevents an
    accidental raw top-level return from reaching the API.
    """

    raw_commands = value.get("commands") if isinstance(value, dict) else None
    if not isinstance(raw_commands, list):
        raw_commands = []
    bounded_limit = max(1, min(int(limit), MAX_PROVIDER_COMMANDS))
    records: list[ProviderCommandRecord] = []
    seen_names: set[str] = set()
    scanned = 0
    for raw in raw_commands:
        if scanned >= bounded_limit:
            break
        scanned += 1
        if not isinstance(raw, dict):
            continue
        name = canonical_provider_command_name(raw.get("name"))
        if name is None or name in seen_names:
            continue
        seen_names.add(name)
        description = sanitize_provider_command_text(
            raw.get("description"),
            MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS,
        )
        argument_hint = sanitize_provider_command_text(
            raw.get("argumentHint"),
            MAX_PROVIDER_COMMAND_LABEL_CHARS,
        )
        if argument_hint:
            description = (
                f"{description} {argument_hint}".strip()
            )[:MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS]
        command_id = _opaque_command_id(
            selector_secret,
            binding_context,
            "claude",
            cwd,
            name,
            name,
        )
        public = {
            "id": command_id,
            "name": name,
            "label": name,
            "description": description,
            "scope": None,
            "source": "claude",
            "kind": "command",
            "invocation": f"/{name}",
        }
        records.append(
            ProviderCommandRecord(
                public=public,
                native={
                    "name": name,
                    **(
                        {"control_generation": control_generation}
                        if control_generation
                        else {}
                    ),
                },
            )
        )
    return _inventory(
        selector_secret,
        binding_context,
        "claude",
        cwd,
        records,
        truncated=bool(
            isinstance(value, dict)
            and value.get("_agentsdock_provider_commands_truncated")
        )
        or len(raw_commands) > bounded_limit,
    )


def empty_provider_command_inventory(
    backend: str,
    *,
    cwd: str,
    selector_secret: str,
    binding_context: str,
) -> ProviderCommandInventory:
    return _inventory(
        selector_secret,
        binding_context,
        backend,
        cwd,
        [],
        truncated=False,
    )
