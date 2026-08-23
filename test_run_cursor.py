"""Real, non-mocked exercise of agent_server.run_cursor() against a fake
`agent` CLI subprocess that emits real captured stream-json event shapes.

run_cursor is NOT wired into VALID_BACKENDS yet - nothing in agent_server.py
calls it. This test calls it directly, the same way the future activation
step eventually will, to prove the runner shape actually works now (catches
NameErrors / wrong helper signatures that py_compile cannot see, since they
only surface when the function body actually executes) rather than after
activation.

The fake CLI is a real subprocess (not a Python-level mock of
run_cursor's internals) so the test exercises the real subprocess spawn,
stdout line reading, idle loop, and shutdown path exactly as production
would.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import agent_server

FAKE_AGENT_CLI = """#!/usr/bin/env python3
import json, os, sys

cwd = os.getcwd()
session_id = "cursor-sess-abc123"
hello_path = os.path.join(cwd, "hello.py")
events = [
    {"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": cwd,
     "session_id": session_id, "model": "Auto", "permissionMode": "default"},
    {"type": "tool_call", "subtype": "started", "call_id": "call-edit-1",
     "tool_call": {"editToolCall": {"args": {"path": hello_path, "streamContent": "print(1)"}}},
     "session_id": session_id},
    {"type": "tool_call", "subtype": "completed", "call_id": "call-edit-1",
     "tool_call": {"editToolCall": {"args": {"path": hello_path},
                                     "result": {"success": {"path": hello_path,
                                                             "linesAdded": 1,
                                                             "linesRemoved": 0}}}},
     "session_id": session_id},
    {"type": "assistant",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
     "session_id": session_id},
    {"type": "result", "subtype": "success", "duration_ms": 123, "is_error": False,
     "result": "Done.", "session_id": session_id,
     "usage": {"inputTokens": 10, "outputTokens": 2}},
]
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(0)
"""

FAKE_AGENT_CLI_REJECTED_SHELL = """#!/usr/bin/env python3
import json, sys

session_id = "cursor-sess-rejected"
events = [
    {"type": "system", "subtype": "init", "cwd": ".", "session_id": session_id, "model": "Auto"},
    {"type": "tool_call", "subtype": "completed", "call_id": "call-shell-1",
     "tool_call": {"shellToolCall": {"result": {"rejected": {"command": "rm -rf /",
                                                              "reason": "not trusted"}}}},
     "session_id": session_id},
    {"type": "result", "subtype": "success", "duration_ms": 5, "is_error": False,
     "result": "I can't run that without permission.", "session_id": session_id},
]
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(0)
"""


def _write_fake_cli(directory: Path, body: str) -> Path:
    script = directory / "fake_agent_cli.py"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class RunCursorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_stop_requests = agent_server.STOP_REQUESTS
        self.previous_stopped_runs = agent_server.STOPPED_RUNS
        self.previous_queued = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_run_metadata = agent_server.RUN_METADATA
        self.previous_state_dir = agent_server.STATE_DIR
        self.previous_cursor_bin = agent_server.CURSOR_BIN

        self.tempdir = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.tempdir.name) / "workspace")
        os.makedirs(self.cwd, exist_ok=True)
        agent_server.STATE_DIR = Path(self.tempdir.name) / "state"

        self.session_id = "chat-cursor-test"
        self.session = {
            "id": self.session_id,
            "backend": agent_server.BACKEND_CURSOR,
            "cwd": self.cwd,
        }
        agent_server.STORE.sessions = {self.session_id: self.session}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = {self.session_id}
        agent_server.CURRENT_TURNS = {
            self.session_id: {
                "run_id": "run-cursor-1",
                "prompt": "hello",
                "file_ids": [],
                "backend": agent_server.BACKEND_CURSOR,
            }
        }
        agent_server.STOP_REQUESTS = set()
        agent_server.STOPPED_RUNS = set()
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.RUN_METADATA = {}

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.STOP_REQUESTS = self.previous_stop_requests
        agent_server.STOPPED_RUNS = self.previous_stopped_runs
        agent_server.QUEUED_TURNS = self.previous_queued
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.RUN_METADATA = self.previous_run_metadata
        agent_server.STATE_DIR = self.previous_state_dir
        agent_server.CURSOR_BIN = self.previous_cursor_bin
        self.tempdir.cleanup()

    def _read_events(self) -> list[dict]:
        path = agent_server.events_path(self.session_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    async def test_full_turn_emits_tool_and_assistant_and_terminal_events(self) -> None:
        script = _write_fake_cli(Path(self.tempdir.name), FAKE_AGENT_CLI)
        agent_server.CURSOR_BIN = str(script)

        await agent_server.run_cursor(
            self.session_id,
            "run-cursor-1",
            "hello",
            dict(self.session),
            Path(self.tempdir.name) / "manifest.json",
        )

        events = self._read_events()
        types = [e["type"] for e in events]

        self.assertIn("process_started", types)
        self.assertIn("tool_started", types)
        self.assertIn("tool_finished", types)
        self.assertIn("assistant_text", types)
        self.assertIn("turn_finished", types)

        assistant_events = [e for e in events if e["type"] == "assistant_text"]
        self.assertEqual(assistant_events[0]["text"], "Done.")

        tool_started = next(e for e in events if e["type"] == "tool_started")
        self.assertEqual(tool_started["tool"]["name"], "edit")

        tool_finished = next(e for e in events if e["type"] == "tool_finished")
        self.assertEqual(tool_finished["tool"]["name"], "edit")
        self.assertEqual(tool_finished["output"]["linesAdded"], 1)

        turn_finished = next(e for e in events if e["type"] == "turn_finished")
        self.assertEqual(turn_finished["backend"], agent_server.BACKEND_CURSOR)
        self.assertEqual(turn_finished["exit_code"], 0)

        # persist_run_provider_session should have bound the resumed id.
        self.assertEqual(
            agent_server.STORE.sessions[self.session_id].get("cursor_session_id"),
            "cursor-sess-abc123",
        )

        # A successful turn must release the slot it was holding.
        self.assertNotIn(self.session_id, agent_server.BUSY_SESSIONS)
        self.assertNotIn(self.session_id, agent_server.CURRENT_TURNS)

    async def test_rejected_shell_call_is_reported_as_tool_finished_not_dropped(
        self,
    ) -> None:
        script = _write_fake_cli(Path(self.tempdir.name), FAKE_AGENT_CLI_REJECTED_SHELL)
        agent_server.CURSOR_BIN = str(script)

        await agent_server.run_cursor(
            self.session_id,
            "run-cursor-1",
            "run rm -rf /",
            dict(self.session),
            Path(self.tempdir.name) / "manifest.json",
        )

        events = self._read_events()
        tool_finished = next(e for e in events if e["type"] == "tool_finished")
        self.assertEqual(tool_finished["tool"]["name"], "shell")
        self.assertEqual(tool_finished["exit_code"], 1)
        self.assertIn("not trusted", tool_finished["output"])


if __name__ == "__main__":
    unittest.main()
