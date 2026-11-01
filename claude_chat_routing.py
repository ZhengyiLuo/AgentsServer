"""Keep native Claude child messages separate from AgentsDock chat routes.

Native SendMessage also accepts local/cloud sessions and display names. Only
identities observed in the provider's SubagentStart hooks may use that tool;
AgentsDock recipients must use the separately authorized provider helper.
The same policy runs as SDK callbacks and as print-mode command hooks.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
from typing import Any


NATIVE_CHAT_TOOL_MATCHER = "^(ListAgents|ListPeers|SendMessage)$"
CHAT_ROUTING_REASON = (
    "Not sent or listed. Native Claude messaging is not AgentsDock chat routing. "
    "For an AgentsDock @Chat, use the run-bound AgentsDock provider tool with "
    "helper=chats and the exact server-issued route/target index (or the authorized "
    "helper CLI in print mode). Never substitute a similarly named Claude session. "
    "Native SendMessage is only for a child of this Claude session: use its exact "
    "agent ID from Agent/Task, or main when replying from that child. A native "
    "success is not an AgentsDock delivery receipt."
)


def denied() -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": CHAT_ROUTING_REASON,
    }}


def identity(value: Any) -> str:
    # These are provider hook fields, never IDs asserted in the message body.
    if not isinstance(value, str) or not value or len(value) > 200:
        return ""
    if any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in value):
        return ""
    return value


class ClaudeChatRouting:
    def __init__(self, registry: Path | None = None) -> None:
        self.registry = registry
        self.children: set[tuple[str, str]] = set()

    def _marker(self, session: str, agent: str) -> Path:
        assert self.registry is not None
        digest = hashlib.sha256(f"{session}\0{agent}".encode()).hexdigest()
        return self.registry / digest

    def _private_registry(self) -> None:
        assert self.registry is not None
        self.registry.mkdir(mode=0o700, exist_ok=True)
        info = self.registry.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Invalid Claude child registry")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Claude child registry must be private")

    def record_child(self, event: dict[str, Any]) -> dict[str, Any]:
        session, agent = identity(event.get("session_id")), identity(event.get("agent_id"))
        if event.get("hook_event_name") != "SubagentStart" or not session or not agent:
            return {}
        if self.registry is None:
            self.children.add((session, agent))
        else:
            self._private_registry()
            # One immutable empty marker per provider-owned child. Concurrent
            # hooks do not overwrite one another or store conversation text.
            try:
                fd = os.open(self._marker(session, agent), os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        return {}

    def _owns(self, session: str, agent: str) -> bool:
        if not session or not agent:
            return False
        if self.registry is None:
            return (session, agent) in self.children
        self._private_registry()
        try:
            info = self._marker(session, agent).lstat()
        except FileNotFoundError:
            return False
        return (stat.S_ISREG(info.st_mode) and info.st_size == 0 and info.st_nlink == 1
                and (os.name == "nt" or (info.st_uid == os.getuid() and not info.st_mode & 0o077)))

    def before_tool(self, event: dict[str, Any]) -> dict[str, Any]:
        name = event.get("tool_name")
        if name not in {"SendMessage", "ListAgents", "ListPeers"}:
            return {}
        if name != "SendMessage":
            return denied()
        data = event.get("tool_input")
        if not isinstance(data, dict):
            return denied()
        # Claude changed recipient/content to to/message. Do not accept
        # disagreeing legacy fields, broadcasts, socket addresses or names.
        target = data.get("to", data.get("recipient"))
        if "to" in data and "recipient" in data and data["to"] != data["recipient"]:
            return denied()
        session = identity(event.get("session_id"))
        sender = identity(event.get("agent_id"))
        try:
            if target == "main" and self._owns(session, sender):
                return {}
            if self._owns(session, identity(target)):
                # Defer to native permissions; never auto-approve a child tool.
                return {}
        except (OSError, ValueError):
            pass
        return denied()

    async def subagent_start(self, event: dict[str, Any], *_: Any) -> dict[str, Any]:
        return self.record_child(event)

    async def pre_tool_use(self, event: dict[str, Any], *_: Any) -> dict[str, Any]:
        return self.before_tool(event)


def print_routing_settings(manifest: Path) -> dict[str, Any]:
    registry = manifest.with_name("claude-child-routing")
    # Claude command hooks run through the user's shell. No authority or
    # endpoint credentials appear in this command or in its registry.
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), str(registry)])
    hook = {"type": "command", "command": command, "timeout": 5}
    return {"hooks": {
        "SubagentStart": [{"hooks": [hook]}],
        "PreToolUse": [{"matcher": NATIVE_CHAT_TOOL_MATCHER, "hooks": [hook]}],
    }}


def main() -> None:
    try:
        event = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
        policy = ClaudeChatRouting(Path(sys.argv[1]))
        result = (policy.record_child(event) if event.get("hook_event_name") == "SubagentStart"
                  else policy.before_tool(event))
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        result = denied()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
