"""Windows-branch tests for the ConPTY terminal facade in agent_server.

All tests here exercise the real winterminal backend (real ConPTY shells);
they are skipped on non-Windows platforms and when pywinpty is unavailable.

Run:  .venv/Scripts/python.exe -m unittest test_terminal_windows -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_server
from fastapi import HTTPException

WINDOWS_TERMINAL = bool(getattr(agent_server, "WINDOWS_TERMINAL_BACKEND", False))

SNAPSHOT_KEYS = {
    "session_id", "name", "exists", "created", "cwd", "command", "pane_pid",
    "attached", "columns", "rows", "lines", "text", "updated_at",
}


def wait_for_snapshot_text(session_id: str, marker: str, timeout: float = 15.0) -> dict:
    """Poll terminal_snapshot until the ring-buffer history contains marker."""
    deadline = time.monotonic() + timeout
    snapshot = agent_server.terminal_snapshot(session_id)
    while time.monotonic() < deadline:
        if marker in snapshot["text"]:
            return snapshot
        time.sleep(0.25)
        snapshot = agent_server.terminal_snapshot(session_id)
    raise AssertionError(
        f"marker {marker!r} never appeared; tail={snapshot['text'][-400:]!r}"
    )


class ScriptWebSocket:
    """Minimal starlette-WebSocket stand-in with a scripted receive queue.

    Script items: ("bytes", data), ("text", str), ("sleep", seconds).
    An empty script yields websocket.disconnect.
    """

    def __init__(self, script: list[tuple[str, object]]) -> None:
        self._script = list(script)
        self.sent_json: list[dict] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code

    async def receive(self) -> dict:
        while self._script:
            kind, value = self._script.pop(0)
            if kind == "sleep":
                await asyncio.sleep(float(value))
                continue
            if kind == "bytes":
                return {"type": "websocket.receive", "bytes": bytes(value)}
            return {"type": "websocket.receive", "text": str(value)}
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(bytes(data))


@unittest.skipUnless(WINDOWS_TERMINAL, "Windows ConPTY terminal backend only")
class WindowsTerminalFacadeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wt-facade-")
        self.sid = "wt-facade-chat"
        self.patchers = [
            patch.dict(
                agent_server.STORE.sessions,
                {self.sid: {"id": self.sid, "cwd": self.tmp}},
            )
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        manager = agent_server.TERMINAL_MANAGER
        if manager is not None:
            manager.close(self.sid)
        agent_server.STORE.sessions.pop(self.sid, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capability_reports_conpty_terminal(self) -> None:
        capability = agent_server.tmux_capability()
        self.assertTrue(capability["available"])
        self.assertFalse(capability["required"])
        self.assertIn("ConPTY", capability["message"])
        self.assertIsNone(capability["action"])

    def test_ensure_returns_snapshot_contract(self) -> None:
        snapshot = agent_server.ensure_terminal_session(
            self.sid, self.tmp, columns=80, rows=24
        )
        self.assertLessEqual(SNAPSHOT_KEYS, set(snapshot.keys()))
        self.assertTrue(snapshot["exists"])
        self.assertTrue(snapshot["created"])
        self.assertEqual(snapshot["session_id"], self.sid)
        self.assertEqual(snapshot["name"], agent_server.terminal_session_name(self.sid))
        self.assertEqual(snapshot["command"], "cmd.exe")
        self.assertIsNotNone(snapshot["pane_pid"])
        self.assertEqual(snapshot["attached"], 1)
        self.assertEqual((snapshot["columns"], snapshot["rows"]), (80, 24))
        # Re-ensure returns the same session without recreating it.
        again = agent_server.ensure_terminal_session(self.sid)
        self.assertFalse(again["created"])
        self.assertTrue(again["exists"])

    def test_input_reaches_snapshot_history(self) -> None:
        agent_server.ensure_terminal_session(self.sid, self.tmp)
        agent_server.send_terminal_input(self.sid, "echo WT_MARKER_FACADE7")
        snapshot = wait_for_snapshot_text(self.sid, "WT_MARKER_FACADE7")
        self.assertTrue(snapshot["exists"])

    def test_key_input_and_unknown_key_rejection(self) -> None:
        agent_server.ensure_terminal_session(self.sid, self.tmp)
        agent_server.send_terminal_input(self.sid, "echo WT_MARKER_KEY9", enter=False)
        agent_server.send_terminal_input(self.sid, None, key="Enter")
        wait_for_snapshot_text(self.sid, "WT_MARKER_KEY9")
        with self.assertRaises(HTTPException) as raised:
            agent_server.send_terminal_input(self.sid, None, key="F13")
        self.assertEqual(raised.exception.status_code, 400)

    def test_resize_updates_snapshot_geometry(self) -> None:
        agent_server.ensure_terminal_session(self.sid, self.tmp, columns=80, rows=24)
        snapshot = agent_server.resize_terminal_pane(self.sid, 132, 43)
        self.assertEqual((snapshot["columns"], snapshot["rows"]), (132, 43))

    def test_windows_scroll_and_actions_are_safe(self) -> None:
        agent_server.ensure_terminal_session(self.sid, self.tmp)
        self.assertFalse(agent_server.scroll_terminal_history(self.sid, -5))
        self.assertFalse(agent_server.scroll_terminal_history(self.sid, 5))
        agent_server.exit_terminal_auto_scroll(self.sid)  # must not raise
        windows = agent_server.terminal_windows_snapshot(self.sid)
        self.assertTrue(windows["exists"])
        self.assertFalse(windows["mouse_enabled"])
        self.assertEqual(
            windows["windows"],
            [{"id": "@0", "index": 0, "name": "shell", "active": True, "panes": 1}],
        )
        with self.assertRaises(HTTPException) as raised:
            agent_server.terminal_action(self.sid, "new-window")
        self.assertEqual(raised.exception.status_code, 409)

    def test_kill_terminal_session_closes_and_forgets(self) -> None:
        agent_server.ensure_terminal_session(self.sid, self.tmp)
        killed = agent_server.kill_terminal_session(self.sid)
        self.assertTrue(killed["killed"])
        self.assertFalse(killed["exists"])
        self.assertIsNone(agent_server.TERMINAL_MANAGER.get(self.sid))
        snapshot = agent_server.terminal_snapshot(self.sid)
        self.assertFalse(snapshot["exists"])
        self.assertEqual(snapshot["text"], "")
        killed_again = agent_server.kill_terminal_session(self.sid)
        self.assertFalse(killed_again["killed"])

    def test_archived_chat_is_rejected_before_any_backend_call(self) -> None:
        agent_server.STORE.sessions[self.sid]["archived"] = True
        with self.assertRaises(HTTPException) as raised:
            agent_server.ensure_terminal_session(self.sid, self.tmp)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIsNone(agent_server.TERMINAL_MANAGER.get(self.sid))

    def test_missing_chat_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            agent_server.ensure_terminal_session("wt-no-such-chat", self.tmp)
        self.assertEqual(raised.exception.status_code, 404)

    def test_manifest_path_in_agent_env_uses_posix_separators(self) -> None:
        env = agent_server.agent_runner_env(self.sid)
        self.assertTrue(env["AGENTSDOCK_MANIFEST_PATH"].endswith("/manifests/current.json"))
        self.assertNotIn("\\", env["AGENTSDOCK_MANIFEST_PATH"])

    async def test_ws_streams_input_output_and_survives_disconnect(self) -> None:
        script = [
            ("bytes", b"echo WT_WS_MARKER\r"),
            ("sleep", 6.0),  # let cmd boot, execute, and stream output back
            ("text", json.dumps({"type": "resize", "columns": 100, "rows": 30})),
            ("sleep", 1.0),
        ]
        ws = ScriptWebSocket(script)
        with patch.object(agent_server, "websocket_authorized", return_value=True):
            await agent_server.session_terminal(
                self.sid, ws, columns=80, rows=24, cwd=self.tmp  # type: ignore[arg-type]
            )

        ready = next((m for m in ws.sent_json if m.get("type") == "ready"), None)
        self.assertIsNotNone(ready, f"no ready frame in {ws.sent_json!r}")
        self.assertEqual(ready["session_id"], self.sid)
        self.assertEqual(ready["name"], agent_server.terminal_session_name(self.sid))
        self.assertEqual((ready["columns"], ready["rows"]), (80, 24))

        output = b"".join(ws.sent_bytes).decode("utf-8", "replace")
        self.assertIn("WT_WS_MARKER", output)

        # The disconnect must NOT kill the shell: same session, still alive,
        # marked detached, resize applied.
        session = agent_server.TERMINAL_MANAGER.get(self.sid)
        self.assertIsNotNone(session)
        self.assertTrue(session.alive)
        self.assertTrue(session.detached)
        self.assertEqual((session.columns, session.rows), (100, 30))

        # Still responsive through the HTTP facade after the WS went away.
        agent_server.send_terminal_input(self.sid, "echo WT_AFTER_DISCONNECT")
        wait_for_snapshot_text(self.sid, "WT_AFTER_DISCONNECT")


if __name__ == "__main__":
    unittest.main()
