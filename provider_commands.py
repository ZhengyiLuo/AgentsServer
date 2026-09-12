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
import os
import re
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MAX_PROVIDER_COMMANDS = 512
MAX_PROVIDER_COMMAND_NAME_CHARS = 128
MAX_PROVIDER_COMMAND_LABEL_CHARS = 160
MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS = 800
MAX_PROVIDER_COMMAND_PATH_CHARS = 4096

# OpenCode skills are discovered directly from its documented local defaults.
# These ceilings make opening the slash palette a bounded filesystem read even
# when a workspace contains a deliberately hostile directory tree.
MAX_OPENCODE_SKILL_ANCESTORS = 64
MAX_OPENCODE_SKILL_ROOTS = 3 * MAX_OPENCODE_SKILL_ANCESTORS + 3
MAX_OPENCODE_SKILL_SCAN_DEPTH = 8
MAX_OPENCODE_SKILL_SCAN_DIRS = 4096
MAX_OPENCODE_SKILL_SCAN_ENTRIES = 16384
MAX_OPENCODE_SKILL_FILE_BYTES = 1024 * 1024
MAX_OPENCODE_SKILL_TOTAL_BYTES = 16 * 1024 * 1024
MAX_OPENCODE_SKILL_FRONTMATTER_LINES = 256
OPENCODE_SKILL_SCAN_TIMEOUT_SECONDS = 2.0

_COMMAND_NAME_RE = re.compile(
    rf"[A-Za-z0-9_][A-Za-z0-9_.:-]{{0,{MAX_PROVIDER_COMMAND_NAME_CHARS - 1}}}"
)
_OPENCODE_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER_KEY_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?")
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


class ProviderCommandDiscoveryError(RuntimeError):
    """A bounded local provider inventory could not be read safely."""


@dataclass
class _OpenCodeSkillScanBudget:
    deadline: float
    directories: int = 0
    entries: int = 0
    files: int = 0
    total_bytes: int = 0

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise ProviderCommandDiscoveryError(
                "OpenCode skill discovery exceeded its time limit"
            )

    def count_directory(self) -> None:
        self.check()
        self.directories += 1
        if self.directories > MAX_OPENCODE_SKILL_SCAN_DIRS:
            raise ProviderCommandDiscoveryError(
                "OpenCode skill discovery exceeded its directory limit"
            )

    def count_file(self, size: int) -> None:
        self.check()
        self.files += 1
        if self.files > MAX_PROVIDER_COMMANDS:
            raise ProviderCommandDiscoveryError(
                "OpenCode skill discovery exceeded its file limit"
            )
        if size < 0 or size > MAX_OPENCODE_SKILL_FILE_BYTES:
            raise ProviderCommandDiscoveryError(
                "an OpenCode skill exceeds the per-file byte limit"
            )
        self.total_bytes += size
        if self.total_bytes > MAX_OPENCODE_SKILL_TOTAL_BYTES:
            raise ProviderCommandDiscoveryError(
                "OpenCode skill discovery exceeded its aggregate byte limit"
            )

    def count_entry(self) -> None:
        self.check()
        self.entries += 1
        if self.entries > MAX_OPENCODE_SKILL_SCAN_ENTRIES:
            raise ProviderCommandDiscoveryError(
                "OpenCode skill discovery exceeded its entry limit"
            )


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


def _plain_frontmatter_scalar(value: str) -> str | None:
    """Parse the conservative scalar subset used by the local skill index.

    OpenCode uses a complete YAML parser. AgentsServer intentionally accepts a
    smaller, deterministic subset for the two display fields it needs rather
    than adding executable tags, aliases, or another deployment dependency.
    Files using richer YAML remain usable in OpenCode itself but are omitted
    from this bounded palette.
    """

    candidate = value.strip()
    if not candidate:
        return ""
    if candidate.startswith('"'):
        try:
            decoded = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, str) else None
    if candidate.startswith("'"):
        if len(candidate) < 2 or not candidate.endswith("'"):
            return None
        return candidate[1:-1].replace("''", "'")
    if candidate[0] in "[{&*!|>":
        return None
    # A hash starts a YAML comment only when separated from the scalar.
    candidate = re.split(r"[ \t]+#", candidate, maxsplit=1)[0].rstrip()
    if re.search(r":[ \t]", candidate):
        return None
    if candidate.lower() in {"null", "true", "false", "~"}:
        return None
    return candidate


def _opencode_skill_frontmatter(data: bytes) -> tuple[str, str, str] | None:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    closing = None
    for index, line in enumerate(
        lines[1 : MAX_OPENCODE_SKILL_FRONTMATTER_LINES + 1],
        start=1,
    ):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        return None

    values: dict[str, str] = {}
    body_lines = lines[1:closing]
    index = 0
    while index < len(body_lines):
        line = body_lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Nested metadata and other unknown structured fields are irrelevant.
        # A recognized field must be a top-level, single-line scalar.
        if line[:1].isspace():
            continue
        match = _FRONTMATTER_KEY_RE.fullmatch(line)
        if match is None:
            continue
        key, raw_value = match.groups()
        if key not in {"name", "description"}:
            continue
        if key in values or raw_value is None:
            return None
        if key == "description" and raw_value.strip() in {
            "|", "|-", "|+", ">", ">-", ">+",
        }:
            block: list[str] = []
            while index < len(body_lines):
                block_line = body_lines[index]
                if block_line and not block_line[:1].isspace():
                    break
                index += 1
                block.append(block_line)
            nonblank = [value for value in block if value.strip()]
            if not nonblank or any(value.startswith("\t") for value in nonblank):
                return None
            indentation = min(len(value) - len(value.lstrip(" ")) for value in nonblank)
            if indentation <= 0:
                return None
            value = "\n".join(
                item[indentation:] if item.strip() else ""
                for item in block
            ).strip("\n")
            values[key] = value
            continue
        value = _plain_frontmatter_scalar(raw_value)
        if value is None:
            return None
        values[key] = value

    name = values.get("name", "")
    description = values.get("description", "")
    if (
        not 1 <= len(name) <= 64
        or _OPENCODE_SKILL_NAME_RE.fullmatch(name) is None
        or not 1 <= len(description) <= 1024
    ):
        return None
    # OpenCode's native Skill loader passes gray-matter's trimmed Markdown
    # content to the model, not the YAML frontmatter itself.
    content = "\n".join(lines[closing + 1:]).strip()
    return name, description, content


def _path_has_symlink(path: Path) -> bool:
    """Return true when any existing component is a symbolic link."""

    absolute = Path(os.path.abspath(os.path.normpath(str(path))))
    parts = absolute.parts
    if not parts:
        return True
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return True
    return False


def _read_regular_file_without_symlinks(
    path: Path,
    budget: _OpenCodeSkillScanBudget,
) -> bytes:
    if _path_has_symlink(path):
        raise ProviderCommandDiscoveryError(
            "an OpenCode skill path contains a symbolic link"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProviderCommandDiscoveryError(
            "an OpenCode skill could not be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProviderCommandDiscoveryError(
                "an OpenCode skill is not a regular file"
            )
        budget.count_file(before.st_size)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            budget.check()
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ProviderCommandDiscoveryError(
                    "an OpenCode skill changed while being read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProviderCommandDiscoveryError(
                "an OpenCode skill changed while being read"
            )
        return data
    finally:
        os.close(descriptor)


def _bounded_skill_files(
    root: Path,
    budget: _OpenCodeSkillScanBudget,
) -> list[Path]:
    if _path_has_symlink(root):
        return []
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ProviderCommandDiscoveryError(
            "an OpenCode skill root could not be inspected"
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        return []

    matches: list[Path] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        budget.count_directory()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    budget.count_entry()
                    entries.append(entry)
                entries.sort(key=lambda item: item.name)
        except OSError as exc:
            raise ProviderCommandDiscoveryError(
                "an OpenCode skill directory could not be read"
            ) from exc
        child_dirs: list[Path] = []
        for entry in entries:
            budget.check()
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ProviderCommandDiscoveryError(
                    "an OpenCode skill entry could not be inspected"
                ) from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                continue
            candidate = directory / entry.name
            if stat.S_ISDIR(entry_stat.st_mode):
                if depth < MAX_OPENCODE_SKILL_SCAN_DEPTH:
                    child_dirs.append(candidate)
                continue
            if stat.S_ISREG(entry_stat.st_mode) and entry.name == "SKILL.md":
                matches.append(candidate)
        # Stack reversal preserves lexical traversal while remaining iterative.
        pending.extend((child, depth + 1) for child in reversed(child_dirs))
    return matches


def _opencode_project_ancestors(cwd: Path) -> list[Path]:
    """Walk nearest-first through the git worktree, or only cwd outside git."""

    ancestors: list[Path] = []
    current = cwd
    worktree_index: int | None = None
    for _ in range(MAX_OPENCODE_SKILL_ANCESTORS):
        ancestors.append(current)
        git_marker = current / ".git"
        try:
            marker_mode = git_marker.lstat().st_mode
        except FileNotFoundError:
            marker_mode = 0
        except OSError as exc:
            raise ProviderCommandDiscoveryError(
                "the OpenCode worktree boundary could not be inspected"
            ) from exc
        if marker_mode and not stat.S_ISLNK(marker_mode):
            worktree_index = len(ancestors) - 1
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    else:
        raise ProviderCommandDiscoveryError(
            "the OpenCode worktree exceeds the ancestor limit"
        )
    return ancestors[: worktree_index + 1] if worktree_index is not None else [cwd]


def opencode_provider_skill_inventory(
    *,
    cwd: str,
    home: str,
    xdg_config_home: str | None,
    selector_secret: str,
    binding_context: str,
    limit: int = MAX_PROVIDER_COMMANDS,
    timeout_seconds: float = OPENCODE_SKILL_SCAN_TIMEOUT_SECONDS,
) -> ProviderCommandInventory:
    """Index the documented default local OpenCode skill roots only.

    This deliberately does not evaluate OpenCode configuration or start its
    CLI. Configured paths/URLs, plugins, commands, agents, MCP prompts, the
    undocumented ``~/.opencode`` compatibility root, and built-in skills are
    outside this side-effect-free v1 contract.
    """

    try:
        canonical_cwd = Path(
            os.path.abspath(os.path.normpath(os.path.expanduser(cwd)))
        ).resolve(strict=True)
        canonical_home = Path(
            os.path.abspath(os.path.normpath(os.path.expanduser(home)))
        ).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProviderCommandDiscoveryError(
            "OpenCode skill discovery boundaries are unavailable"
        ) from exc
    if not canonical_cwd.is_dir() or not canonical_home.is_dir():
        raise ProviderCommandDiscoveryError(
            "OpenCode skill discovery boundaries are not directories"
        )
    if xdg_config_home:
        expanded_xdg = Path(os.path.expanduser(xdg_config_home))
        config_home = (
            expanded_xdg
            if expanded_xdg.is_absolute()
            else canonical_home / ".config"
        )
    else:
        config_home = canonical_home / ".config"
    try:
        if config_home.exists():
            config_home = config_home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProviderCommandDiscoveryError(
            "the OpenCode configuration boundary could not be resolved"
        ) from exc

    project_roots: list[tuple[Path, str]] = []
    for ancestor in _opencode_project_ancestors(canonical_cwd):
        project_roots.extend((
            (ancestor / ".opencode" / "skills", "project"),
            (ancestor / ".claude" / "skills", "project"),
            (ancestor / ".agents" / "skills", "project"),
        ))
    roots = [
        *project_roots,
        (config_home / "opencode" / "skills", "user"),
        (canonical_home / ".claude" / "skills", "user"),
        (canonical_home / ".agents" / "skills", "user"),
    ]
    if len(roots) > MAX_OPENCODE_SKILL_ROOTS:
        raise ProviderCommandDiscoveryError(
            "OpenCode skill discovery exceeded its root limit"
        )

    budget = _OpenCodeSkillScanBudget(
        deadline=time.monotonic() + max(0.01, float(timeout_seconds))
    )
    discovered: dict[str, list[tuple[Path, bytes, str, str]]] = {}
    seen_paths: set[str] = set()
    for root, scope in roots:
        budget.check()
        for path in _bounded_skill_files(root, budget):
            canonical_path = os.path.abspath(os.path.normpath(str(path)))
            if len(canonical_path) > MAX_PROVIDER_COMMAND_PATH_CHARS:
                raise ProviderCommandDiscoveryError(
                    "an OpenCode skill path exceeds the path limit"
                )
            if canonical_path in seen_paths:
                continue
            seen_paths.add(canonical_path)
            data = _read_regular_file_without_symlinks(path, budget)
            parsed = _opencode_skill_frontmatter(data)
            if parsed is None:
                continue
            name, description, _content = parsed
            if path.parent.name != name:
                continue
            discovered.setdefault(name, []).append((path, data, scope, description))

    bounded_limit = max(1, min(int(limit), MAX_PROVIDER_COMMANDS))
    unique = [
        (name, entries[0])
        for name, entries in sorted(discovered.items())
        if len(entries) == 1
    ]
    if len(unique) > bounded_limit:
        raise ProviderCommandDiscoveryError(
            "OpenCode skill discovery exceeded its inventory limit"
        )

    records: list[ProviderCommandRecord] = []
    for name, (path, data, scope, description) in unique:
        canonical_path = os.path.abspath(os.path.normpath(str(path)))
        digest = hashlib.sha256(data).hexdigest()
        native_identity = json.dumps(
            [canonical_path, digest],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        command_id = _opaque_command_id(
            selector_secret,
            binding_context,
            "opencode",
            str(canonical_cwd),
            name,
            native_identity,
        )
        safe_description = sanitize_provider_command_text(
            description,
            MAX_PROVIDER_COMMAND_DESCRIPTION_CHARS,
        )
        records.append(ProviderCommandRecord(
            public={
                "id": command_id,
                "name": name,
                "label": name,
                "description": safe_description,
                "scope": scope,
                "source": "opencode",
                "kind": "skill",
                "invocation": f"/{name}",
            },
            native={
                "name": name,
                "path": canonical_path,
                "directory": os.path.dirname(canonical_path),
                "content_sha256": digest,
            },
        ))

    return _inventory(
        selector_secret,
        binding_context,
        "opencode",
        str(canonical_cwd),
        records,
        truncated=False,
    )


def validate_opencode_provider_skill_record(
    record: ProviderCommandRecord,
) -> tuple[str, str, str]:
    """Revalidate one private skill identity immediately before launch."""

    name = str(record.native.get("name") or "")
    raw_path = str(record.native.get("path") or "")
    expected_directory = str(record.native.get("directory") or "")
    expected_digest = str(record.native.get("content_sha256") or "")
    if (
        record.public.get("source") != "opencode"
        or record.public.get("kind") != "skill"
        or record.public.get("name") != name
        or record.public.get("invocation") != f"/{name}"
        or _OPENCODE_SKILL_NAME_RE.fullmatch(name) is None
        or not raw_path
        or not os.path.isabs(raw_path)
        or len(raw_path) > MAX_PROVIDER_COMMAND_PATH_CHARS
        or not expected_directory
        or not hmac.compare_digest(
            expected_digest,
            expected_digest.lower(),
        )
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise ProviderCommandDiscoveryError(
            "the selected OpenCode skill identity is invalid"
        )
    path = Path(os.path.abspath(os.path.normpath(raw_path)))
    if _path_has_symlink(path):
        raise ProviderCommandDiscoveryError(
            "the selected OpenCode skill path is no longer safe"
        )
    budget = _OpenCodeSkillScanBudget(
        deadline=time.monotonic() + OPENCODE_SKILL_SCAN_TIMEOUT_SECONDS
    )
    data = _read_regular_file_without_symlinks(path, budget)
    parsed = _opencode_skill_frontmatter(data)
    canonical_directory = os.path.dirname(str(path))
    if (
        parsed is None
        or parsed[0] != name
        or path.parent.name != name
        or canonical_directory != expected_directory
        or not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(),
            expected_digest,
        )
    ):
        raise ProviderCommandDiscoveryError(
            "the selected OpenCode skill changed before launch"
        )
    return name, canonical_directory, parsed[2]


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
