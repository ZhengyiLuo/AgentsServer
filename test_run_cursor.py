"""Subprocess-level lifecycle regressions for the production Cursor runner.

The fake CLIs emit captured Cursor stream-json shapes through real OS pipes,
so these tests exercise spawn, concurrent stderr drain, protocol validation,
timeouts, stop/teardown, provider persistence, and terminal projection rather
than mocking the runner's internals.
"""

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server
import cursor_agent_client

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


def _event_script(
    events: list[dict],
    *,
    exit_code: int = 0,
    stderr_bytes: int = 0,
    hang_after: float = 0,
) -> str:
    payload = json.dumps(events)
    return f'''#!/usr/bin/env python3
import json, sys, time
events = json.loads({payload!r})
if {stderr_bytes}:
    sys.stderr.write("x" * {stderr_bytes})
    sys.stderr.flush()
for event in events:
    print(json.dumps(event), flush=True)
if {hang_after!r}:
    time.sleep({hang_after!r})
sys.exit({exit_code})
'''


def _init_event(session_id: str = "cursor-sess-test") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "cwd": ".",
        "session_id": session_id,
        "model": "Auto",
    }


def _result_event(
    text: str = "Done.",
    *,
    session_id: str = "cursor-sess-test",
    is_error: bool = False,
) -> dict:
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "duration_ms": 5,
        "is_error": is_error,
        "result": text,
        "session_id": session_id,
    }


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
        self.previous_runtime_diagnostics = dict(
            agent_server.RUNTIME_DIAGNOSTICS
        )
        self.previous_state_dir = agent_server.STATE_DIR
        self.previous_sessions_file = agent_server.SESSIONS_FILE
        self.previous_cursor_bin = agent_server.CURSOR_BIN
        self.previous_startup_timeout = agent_server.CURSOR_STARTUP_TIMEOUT_SECONDS
        self.previous_turn_timeout = agent_server.CURSOR_TURN_TIMEOUT_SECONDS
        self.previous_idle_timeout = agent_server.CURSOR_IDLE_TIMEOUT_SECONDS
        self.previous_idle_warn = agent_server.CURSOR_IDLE_WARN_SECONDS
        self.previous_post_terminal = agent_server.CURSOR_POST_TERMINAL_EXIT_SECONDS
        self.previous_accumulated_text = agent_server.CURSOR_ACCUMULATED_TEXT_MAX_CHARS
        self.previous_max_tool_calls = agent_server.CURSOR_MAX_TOOL_CALLS
        self.previous_max_stream_events = agent_server.CURSOR_MAX_STREAM_EVENTS
        self.previous_max_stream_bytes = agent_server.CURSOR_MAX_STREAM_BYTES

        self.tempdir = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.tempdir.name) / "workspace")
        os.makedirs(self.cwd, exist_ok=True)
        agent_server.STATE_DIR = Path(self.tempdir.name) / "state"
        agent_server.SESSIONS_FILE = agent_server.STATE_DIR / "sessions.json"

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
        agent_server.RUNTIME_DIAGNOSTICS.clear()
        agent_server.RUNTIME_DIAGNOSTICS.update(
            self.previous_runtime_diagnostics
        )
        agent_server.STATE_DIR = self.previous_state_dir
        agent_server.SESSIONS_FILE = self.previous_sessions_file
        agent_server.CURSOR_BIN = self.previous_cursor_bin
        agent_server.CURSOR_STARTUP_TIMEOUT_SECONDS = self.previous_startup_timeout
        agent_server.CURSOR_TURN_TIMEOUT_SECONDS = self.previous_turn_timeout
        agent_server.CURSOR_IDLE_TIMEOUT_SECONDS = self.previous_idle_timeout
        agent_server.CURSOR_IDLE_WARN_SECONDS = self.previous_idle_warn
        agent_server.CURSOR_POST_TERMINAL_EXIT_SECONDS = self.previous_post_terminal
        agent_server.CURSOR_ACCUMULATED_TEXT_MAX_CHARS = self.previous_accumulated_text
        agent_server.CURSOR_MAX_TOOL_CALLS = self.previous_max_tool_calls
        agent_server.CURSOR_MAX_STREAM_EVENTS = self.previous_max_stream_events
        agent_server.CURSOR_MAX_STREAM_BYTES = self.previous_max_stream_bytes
        self.tempdir.cleanup()

    def _read_events(self) -> list[dict]:
        path = agent_server.events_path(self.session_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _arm_run(self, run_id: str, prompt: str = "hello") -> None:
        agent_server.BUSY_SESSIONS.add(self.session_id)
        agent_server.CURRENT_TURNS[self.session_id] = {
            "run_id": run_id,
            "prompt": prompt,
            "file_ids": [],
            "backend": agent_server.BACKEND_CURSOR,
        }

    async def _run_script(
        self,
        body: str,
        *,
        run_id: str = "run-cursor-1",
        prompt: str = "hello",
        session_patch: dict | None = None,
        standalone: bool = False,
    ) -> list[dict]:
        script = _write_fake_cli(Path(self.tempdir.name), body)
        runner_session = {**self.session, **(session_patch or {})}
        runner_session["_cursor_executable"] = str(script)
        self._arm_run(run_id, prompt)
        await agent_server.run_cursor(
            self.session_id,
            run_id,
            prompt,
            runner_session,
            Path(self.tempdir.name) / "manifest.json",
            standalone_provider_context=standalone,
        )
        return [
            event
            for event in self._read_events()
            if event.get("run_id") == run_id
        ]

    async def test_full_turn_emits_tool_and_assistant_and_terminal_events(self) -> None:
        script = _write_fake_cli(Path(self.tempdir.name), FAKE_AGENT_CLI)
        self.session["_cursor_executable"] = str(script)

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
        self.assertIn("linesAdded", str(tool_finished["output"]))

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
        self.session["_cursor_executable"] = str(script)

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

    async def test_logical_error_exit_zero_is_failed_with_empty_result(self) -> None:
        agent_server.RUN_METADATA["run-cursor-1"] = {
            "purpose": "scheduled_job",
            "job_id": "job-cursor-failure",
        }
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial secret"}],
                },
                "session_id": "cursor-sess-test",
            },
            _result_event("provider rejected request", is_error=True),
        ]))

        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")
        self.assertEqual(
            agent_server.scheduled_job_run_status(terminal),
            "failed",
        )
        self.assertTrue(any(event["type"] == "error" for event in events))

    async def test_admitted_executable_is_spawned_without_reresolution(self) -> None:
        with patch.object(
            agent_server,
            "resolve_cursor_executable",
            side_effect=AssertionError("admitted executable must be fenced"),
        ):
            events = await self._run_script(
                _event_script([_init_event(), _result_event("fenced")])
            )

        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        started = next(event for event in events if event["type"] == "process_started")
        self.assertEqual(started["argv"][0], str(Path(self.tempdir.name) / "fake_agent_cli.py"))

    async def test_exit_zero_without_terminal_after_partial_text_fails(self) -> None:
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial"}],
                },
                "session_id": "cursor-sess-test",
            },
        ]))

        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")
        error = next(event for event in events if event["type"] == "error")
        self.assertIn("without a terminal result", error["message"])

    async def test_unknown_schema_after_partial_text_fails_closed(self) -> None:
        sensitive_unknown_payload = "AUTHORITY-SENTINEL-DO-NOT-PERSIST"
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial"}],
                },
                "session_id": "cursor-sess-test",
            },
            {
                "type": sensitive_unknown_payload,
                "subtype": sensitive_unknown_payload,
                "session_id": "cursor-sess-test",
                sensitive_unknown_payload: sensitive_unknown_payload,
            },
        ]))

        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")
        raw_event = next(event for event in events if event["type"] == "raw_event")
        self.assertNotIn(sensitive_unknown_payload, json.dumps(raw_event))
        self.assertNotIn("raw", raw_event)
        self.assertEqual(raw_event["diagnostic"]["json_type"], "dict")
        self.assertTrue(raw_event["diagnostic"]["has_type_field"])
        self.assertTrue(raw_event["diagnostic"]["has_subtype_field"])
        error = next(event for event in events if event["type"] == "error")
        self.assertIn("unsupported stream-json", error["message"])
        self.assertNotIn(sensitive_unknown_payload, json.dumps(error))

    async def test_malformed_stream_and_stderr_cannot_leak_prompt_material(
        self,
    ) -> None:
        secret = "AUTHORITY-SENTINEL-MALFORMED-CURSOR"
        malformed_script = f'''#!/usr/bin/env python3
import json
print(json.dumps({{"type":"system","subtype":"init","session_id":"cursor-sess-test","cwd":".","model":"Auto"}}), flush=True)
print('{{"type":"future_schema","payload":"{secret}"', flush=True)
'''
        with self.assertLogs(agent_server.logger, level="WARNING") as captured:
            events = await self._run_script(
                malformed_script,
                run_id="run-malformed-private",
            )

        raw_event = next(event for event in events if event["type"] == "raw_event")
        terminal = next(event for event in events if event["type"] == "turn_finished")
        error = next(event for event in events if event["type"] == "error")
        self.assertTrue(terminal["is_error"])
        self.assertNotIn(secret, json.dumps(events))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn(secret, json.dumps(raw_event))
        self.assertNotIn(secret, json.dumps(error))
        self.assertNotIn(
            secret,
            json.dumps(
                agent_server.RUNTIME_DIAGNOSTICS.get(
                    agent_server.BACKEND_CURSOR,
                    {},
                )
            ),
        )

        stderr_events = await self._run_script(
            f'''#!/usr/bin/env python3
import json, sys
print(json.dumps({{"type":"system","subtype":"init","session_id":"cursor-sess-test","cwd":".","model":"Auto"}}), flush=True)
sys.stderr.write("{secret}")
sys.stderr.flush()
sys.exit(7)
''',
            run_id="run-stderr-private",
        )
        self.assertNotIn(secret, json.dumps(stderr_events))
        stderr_error = next(
            event for event in stderr_events if event["type"] == "error"
        )
        self.assertIn("stderr was omitted", stderr_error["message"])

    async def test_session_id_is_pinned_across_every_projected_event(self) -> None:
        for event in (
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "wrong"}],
                },
                "session_id": "different-session",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "missing"}],
                },
            },
        ):
            run_id = "run-" + (event.get("session_id") or "missing")
            events = await self._run_script(
                _event_script([_init_event(), event, _result_event()]),
                run_id=run_id,
            )
            terminal = next(
                item for item in events if item["type"] == "turn_finished"
            )
            self.assertTrue(terminal["is_error"])
            self.assertEqual(terminal["result_text"], "")
            self.assertTrue(any(item["type"] == "error" for item in events))

    async def test_concurrent_stderr_drain_prevents_pipe_deadlock(self) -> None:
        events = await asyncio.wait_for(
            self._run_script(
                _event_script(
                    [_init_event(), _result_event("drained")],
                    stderr_bytes=2 * 1024 * 1024,
                )
            ),
            timeout=5,
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "drained")

    async def test_terminal_event_with_hung_process_gets_bounded_grace(self) -> None:
        agent_server.CURSOR_POST_TERMINAL_EXIT_SECONDS = 0.05
        started = time.monotonic()
        events = await self._run_script(
            _event_script(
                [_init_event(), _result_event("complete before hang")],
                hang_after=10,
            )
        )
        self.assertLess(time.monotonic() - started, 2)
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "complete before hang")

    async def test_active_cursor_turn_stops_and_releases_process(self) -> None:
        script = _write_fake_cli(
            Path(self.tempdir.name),
            """#!/usr/bin/env python3
import json, time
print(json.dumps({"type":"system","subtype":"init","session_id":"cursor-sess-test","cwd":".","model":"Auto"}), flush=True)
while True:
    print("heartbeat", flush=True)
    time.sleep(0.01)
""",
        )
        runner_session = {**self.session, "_cursor_executable": str(script)}
        self._arm_run("run-cursor-stop")
        task = asyncio.create_task(
            agent_server.run_cursor(
                self.session_id,
                "run-cursor-stop",
                "hello",
                runner_session,
                Path(self.tempdir.name) / "manifest.json",
            )
        )
        try:
            for _ in range(200):
                active = agent_server.ACTIVE.get(self.session_id)
                if active and active.get("provider_turn_ready"):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Cursor runner never reached its active ready state")

            result = await asyncio.wait_for(
                agent_server.stop_turn(
                    self.session_id,
                    schedule_queue=False,
                    cascade_codex_subagents=False,
                    cascade_claude_subagents=False,
                ),
                timeout=3,
            )
            await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertTrue(result["stopped"])
        self.assertNotIn(self.session_id, agent_server.ACTIVE)
        self.assertNotIn(self.session_id, agent_server.BUSY_SESSIONS)
        terminal = next(
            event
            for event in self._read_events()
            if event.get("run_id") == "run-cursor-stop"
            and event["type"] == "turn_finished"
        )
        self.assertTrue(terminal["stopped"])
        self.assertFalse(terminal["is_error"])

    async def test_process_endpoint_never_exposes_cursor_prompt_or_user_echo(
        self,
    ) -> None:
        secret = "AUTHORITY-SENTINEL-CURSOR-PROCESS-LEAK"
        script = _write_fake_cli(
            Path(self.tempdir.name),
            """#!/usr/bin/env python3
import json, sys, time
prompt = sys.argv[2]
session_id = "cursor-sess-test"
print(json.dumps({"type":"system","subtype":"init","session_id":session_id,"cwd":".","model":"Auto"}), flush=True)
print(json.dumps({"type":"user","message":{"role":"user","content":prompt},"session_id":session_id}), flush=True)
while True:
    time.sleep(0.01)
""",
        )
        runner_session = {
            **self.session,
            "system_prompt": f"System {secret}",
            "_cursor_executable": str(script),
        }
        self._arm_run("run-cursor-private")
        task = asyncio.create_task(
            agent_server.run_cursor(
                self.session_id,
                "run-cursor-private",
                f"User {secret}",
                runner_session,
                Path(self.tempdir.name) / "manifest.json",
            )
        )
        try:
            for _ in range(200):
                active = agent_server.ACTIVE.get(self.session_id)
                if active and active.get("provider_turn_ready"):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Cursor runner never reached its active ready state")

            pid = int(active["pid"])
            pgid = int(active["pgid"])
            leaked_args = (
                f"{script} -p '[AgentsDock provider instructions] "
                f"{secret}' --output-format stream-json"
            )
            process_row = {
                "pid": pid,
                "ppid": 1,
                "pgid": pgid,
                "sid": pgid,
                "stat": "S",
                "elapsed_seconds": 1,
                "cpu_percent": 0.0,
                "mem_percent": 0.0,
                "rss_kb": 1024,
                "command": str(script),
                "args": leaked_args,
            }
            with patch.object(
                agent_server,
                "ps_process_rows",
                return_value=[process_row],
            ), patch.object(
                agent_server,
                "proc_cwd",
                return_value=self.cwd,
            ), patch.object(
                agent_server,
                "fd_log_hints",
                return_value=[],
            ):
                snapshot = await agent_server.get_session_processes(
                    self.session_id
                )
        finally:
            await agent_server.stop_turn(
                self.session_id,
                emit_event=False,
                schedule_queue=False,
                cascade_codex_subagents=False,
                cascade_claude_subagents=False,
            )
            await asyncio.wait_for(task, timeout=3)

        serialized = json.dumps(snapshot)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("AgentsDock provider instructions", serialized)
        self.assertIn("<prompt>", serialized)
        self.assertTrue(snapshot["processes"][0]["args_redacted"])
        self.assertNotIn(secret, snapshot["stdout_tail"]["text"])
        self.assertNotIn("Current user prompt", snapshot["stdout_tail"]["text"])

    async def test_cursor_prebind_stop_hard_terminalizes_stale_runner(self) -> None:
        self._arm_run("run-cursor-prebind")

        async def never_binds() -> None:
            await asyncio.Future()

        stale_runner = asyncio.create_task(never_binds())
        turn_tasks = {self.session_id: {stale_runner}}
        try:
            with patch.object(agent_server, "SESSION_TURN_TASKS", turn_tasks), patch.object(
                agent_server,
                "STOP_CONFIRM_TIMEOUT_SECONDS",
                0.02,
            ):
                result = await agent_server.stop_turn(
                    self.session_id,
                    schedule_queue=False,
                    cascade_codex_subagents=False,
                    cascade_claude_subagents=False,
                )
        finally:
            if not stale_runner.done():
                stale_runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stale_runner

        self.assertTrue(result["stopped"])
        self.assertTrue(result["hard_stop"])
        self.assertNotIn(self.session_id, agent_server.BUSY_SESSIONS)
        stopped = next(
            event
            for event in self._read_events()
            if event.get("run_id") == "run-cursor-prebind"
            and event["type"] == "turn_stopped"
        )
        self.assertEqual(stopped["backend"], agent_server.BACKEND_CURSOR)

    async def test_startup_timeout_fails_and_releases_turn(self) -> None:
        agent_server.CURSOR_STARTUP_TIMEOUT_SECONDS = 0.05
        agent_server.CURSOR_TURN_TIMEOUT_SECONDS = 1
        events = await self._run_script(
            """#!/usr/bin/env python3
import time
time.sleep(10)
"""
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")
        self.assertNotIn(self.session_id, agent_server.BUSY_SESSIONS)

    async def test_absolute_timeout_applies_despite_continuous_output(self) -> None:
        agent_server.CURSOR_STARTUP_TIMEOUT_SECONDS = 0.2
        agent_server.CURSOR_TURN_TIMEOUT_SECONDS = 0.15
        events = await self._run_script(
            """#!/usr/bin/env python3
import json, time
print(json.dumps({"type":"system","subtype":"init","session_id":"cursor-sess-test","cwd":".","model":"Auto"}), flush=True)
while True:
    print("heartbeat", flush=True)
    time.sleep(0.01)
"""
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        error = next(event for event in events if event["type"] == "error")
        self.assertIn("absolute turn timeout", error["message"])

    async def test_missing_runtime_is_a_failed_terminal(self) -> None:
        self._arm_run("run-cursor-1")
        with patch.object(agent_server, "resolve_cursor_executable", return_value=None):
            await agent_server.run_cursor(
                self.session_id,
                "run-cursor-1",
                "hello",
                dict(self.session),
                Path(self.tempdir.name) / "manifest.json",
            )
        events = self._read_events()
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")

    async def test_spawn_failure_is_a_failed_terminal(self) -> None:
        runner = {
            **self.session,
            "_cursor_executable": str(Path(self.tempdir.name) / "missing-agent"),
        }
        self._arm_run("run-cursor-1")
        await agent_server.run_cursor(
            self.session_id,
            "run-cursor-1",
            "hello",
            runner,
            Path(self.tempdir.name) / "manifest.json",
        )
        terminal = next(
            event
            for event in self._read_events()
            if event["type"] == "turn_finished"
        )
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")

    async def test_tool_state_and_payloads_are_bounded(self) -> None:
        huge_id = "call-" + ("z" * 235)
        huge_args = {"nested": {"value": "a" * 100_000}}
        huge_reason = "r" * 100_000
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": huge_id,
                "tool_call": {"shellToolCall": {"args": huge_args}},
                "session_id": "cursor-sess-test",
            },
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": huge_id,
                "tool_call": {
                    "shellToolCall": {
                        "result": {"rejected": {"reason": huge_reason}}
                    }
                },
                "session_id": "cursor-sess-test",
            },
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "without-start",
                "tool_call": {
                    (("n" * 1000) + "ToolCall"): {
                        "result": {"success": {"output": "o" * 100_000}}
                    }
                },
                "session_id": "cursor-sess-test",
            },
            _result_event(),
        ]))
        started = next(event for event in events if event["type"] == "tool_started")
        self.assertLessEqual(len(started["tool"]["id"]), 240)
        self.assertLess(len(json.dumps(started["tool"]["input"])), 20_000)
        completed = [event for event in events if event["type"] == "tool_finished"]
        self.assertTrue(completed)
        self.assertTrue(all(len(event["tool_id"]) <= 240 for event in completed))
        self.assertTrue(all(len(event["tool"]["name"]) <= 240 for event in completed))
        self.assertTrue(all(len(str(event["output"])) < 150_000 for event in completed))

    async def test_invalid_tool_call_id_fails_closed_without_projection(self) -> None:
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "tool_call",
                "subtype": "started",
                "tool_call": {"readToolCall": {"args": {"path": "a"}}},
                "session_id": "cursor-sess-test",
            },
            _result_event(),
        ]))
        self.assertFalse(any(event["type"] == "tool_started" for event in events))
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")

    async def test_tool_cardinality_ceiling_fails_visibly(self) -> None:
        agent_server.CURSOR_MAX_TOOL_CALLS = 2
        tool_events = []
        for index in range(3):
            tool_events.append({
                "type": "tool_call",
                "subtype": "started",
                "call_id": f"call-{index}",
                "tool_call": {"readToolCall": {"args": {"path": str(index)}}},
                "session_id": "cursor-sess-test",
            })
        events = await self._run_script(
            _event_script([_init_event(), *tool_events, _result_event()])
        )
        self.assertEqual(
            len([event for event in events if event["type"] == "tool_started"]),
            2,
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(terminal["result_text"], "")

    async def test_stream_event_ceiling_bounds_durable_output(self) -> None:
        agent_server.CURSOR_MAX_STREAM_EVENTS = 3
        events = await self._run_script(_event_script([
            _init_event(),
            {
                "type": "thinking",
                "subtype": "delta",
                "text": "one",
                "session_id": "cursor-sess-test",
            },
            {
                "type": "thinking",
                "subtype": "delta",
                "text": "two",
                "session_id": "cursor-sess-test",
            },
            {
                "type": "thinking",
                "subtype": "delta",
                "text": "three",
                "session_id": "cursor-sess-test",
            },
            _result_event(),
        ]))
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertTrue(terminal["is_error"])
        self.assertEqual(
            len([event for event in events if event["type"] == "reasoning_summary"]),
            2,
        )

    async def test_accumulated_assistant_fallback_is_bounded(self) -> None:
        agent_server.CURSOR_ACCUMULATED_TEXT_MAX_CHARS = 10
        assistant = lambda text: {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "session_id": "cursor-sess-test",
        }
        events = await self._run_script(
            _event_script([
                _init_event(),
                assistant("abcdefgh"),
                assistant("ijklmnop"),
                _result_event(""),
            ])
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        self.assertLessEqual(len(terminal["result_text"]), 10)

    async def test_standalone_turn_does_not_mutate_parent_provider_state(self) -> None:
        parent = agent_server.STORE.sessions[self.session_id]
        parent.update({
            "cursor_session_id": "parent-cursor-id",
            "session_id": "parent-cursor-id",
            "cursor_instruction_hash": "parent-hash",
            "cursor_instruction_version": "old-version",
            "memory_seed": "parent memory",
            "memory_seed_used": False,
        })
        before = dict(parent)
        events = await self._run_script(
            _event_script([_init_event("standalone-id"), _result_event(
                "standalone",
                session_id="standalone-id",
            )]),
            session_patch=before,
            standalone=True,
        )
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        for key in (
            "session_id",
            "cursor_session_id",
            "cursor_instruction_hash",
            "cursor_instruction_version",
            "memory_seed_used",
        ):
            self.assertEqual(parent.get(key), before.get(key))

    async def test_start_turn_scheduled_standalone_cursor_override_isolated_from_parent(
        self,
    ) -> None:
        script = _write_fake_cli(
            Path(self.tempdir.name),
            _event_script([
                _init_event("standalone-cursor-job"),
                _result_event(
                    "scheduled standalone complete",
                    session_id="standalone-cursor-job",
                ),
            ]),
        )
        parent = agent_server.STORE.sessions[self.session_id]
        parent.update({
            "title": "Claude parent",
            "backend": agent_server.BACKEND_CLAUDE,
            "backend_locked": True,
            "session_id": "claude-parent-provider",
            "claude_session_id": "claude-parent-provider",
            "cursor_session_id": None,
            "cursor_instruction_hash": "parked-cursor-hash",
            "cursor_instruction_version": "parked-version",
            "memory_seed": "parent memory remains untouched",
            "memory_seed_used": False,
        })
        before = dict(parent)
        agent_server.BUSY_SESSIONS.clear()
        agent_server.CURRENT_TURNS.clear()

        with patch.object(
            agent_server,
            "ensure_runtime_available",
            return_value={
                "status": "ready",
                "_executable": str(script),
            },
        ), patch.object(
            agent_server,
            "scrub_tmux_global_secret_environment",
        ):
            admitted = await agent_server.start_turn(
                self.session_id,
                agent_server.TurnRequest(
                    prompt="Run scheduled Cursor work",
                    backend=agent_server.BACKEND_CURSOR,
                    purpose="scheduled_job",
                    job_id="job-cursor-standalone",
                ),
                provider_context_mode="standalone",
            )
            for _ in range(500):
                if self.session_id not in agent_server.BUSY_SESSIONS:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("standalone Cursor job did not reach terminal state")

        self.assertFalse(admitted["queued"])
        events = [
            event
            for event in self._read_events()
            if event.get("run_id") == admitted["run_id"]
        ]
        terminal = next(event for event in events if event["type"] == "turn_finished")
        self.assertFalse(terminal["is_error"])
        self.assertEqual(terminal["backend"], agent_server.BACKEND_CURSOR)
        self.assertEqual(terminal["job_context_mode"], "standalone")
        for key in (
            "backend",
            "backend_locked",
            "session_id",
            "claude_session_id",
            "cursor_session_id",
            "cursor_instruction_hash",
            "cursor_instruction_version",
            "memory_seed_used",
        ):
            self.assertEqual(parent.get(key), before.get(key))

    async def test_failed_policy_and_fork_memory_are_reinjected_until_success(
        self,
    ) -> None:
        session = agent_server.STORE.sessions[self.session_id]
        session.update({
            "system_prompt": "Always preserve the test contract.",
            "memory_seed": "bounded fork memory sentinel",
            "memory_seed_used": False,
        })
        captured_prompts: list[str] = []
        real_build_cursor_cmd = cursor_agent_client.build_cursor_cmd

        def capture_cmd(sess: dict, provider_prompt: str, **kwargs: object) -> list[str]:
            captured_prompts.append(provider_prompt)
            return real_build_cursor_cmd(sess, provider_prompt, **kwargs)

        with patch.object(
            cursor_agent_client,
            "build_cursor_cmd",
            side_effect=capture_cmd,
        ):
            failed = await self._run_script(
                _event_script([
                    _init_event(),
                    _result_event("provider failure", is_error=True),
                ]),
                run_id="run-policy-failed",
                session_patch=session,
            )
            self.assertTrue(next(
                event for event in failed if event["type"] == "turn_finished"
            )["is_error"])
            self.assertFalse(session["memory_seed_used"])
            self.assertNotIn("cursor_instruction_hash", session)

            succeeded = await self._run_script(
                _event_script([_init_event(), _result_event("success")]),
                run_id="run-policy-success",
                session_patch=session,
            )
            self.assertFalse(next(
                event for event in succeeded if event["type"] == "turn_finished"
            )["is_error"])
            self.assertTrue(session["memory_seed_used"])
            self.assertIn("cursor_instruction_hash", session)

            resumed = await self._run_script(
                _event_script([_init_event(), _result_event("resumed")]),
                run_id="run-policy-resumed",
                session_patch=session,
            )
            self.assertFalse(next(
                event for event in resumed if event["type"] == "turn_finished"
            )["is_error"])

        self.assertEqual(len(captured_prompts), 3)
        self.assertIn("Always preserve the test contract.", captured_prompts[0])
        self.assertIn("bounded fork memory sentinel", captured_prompts[0])
        self.assertIn("Always preserve the test contract.", captured_prompts[1])
        self.assertIn("bounded fork memory sentinel", captured_prompts[1])
        self.assertNotIn("[AgentsDock provider instructions]", captured_prompts[2])
        self.assertNotIn("bounded fork memory sentinel", captured_prompts[2])


if __name__ == "__main__":
    unittest.main()
