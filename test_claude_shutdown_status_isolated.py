"""Exercise real runner catch/finalizer code without importing the server."""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from claude_sdk_client import ClaudeSDKSupervisorClosed


SOURCE = Path(__file__).with_name("agent_server.py")


def load_probe(path, *, shutting_down, previous_error=None, result=None, projection_error=False):
    tree = ast.parse(SOURCE.read_text())
    runner = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_claude_sdk")
    classify = next(node for node in runner.body if isinstance(node, ast.FunctionDef) and node.name == "record_sdk_stream_exception")
    finalize = next(node for node in runner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "finalize_sdk_run")
    handlers = [node for node in ast.walk(runner) if isinstance(node, ast.ExceptHandler) and any(
        isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        and child.func.id == "record_sdk_stream_exception" for child in ast.walk(node))]
    assert len(handlers) == 2
    handler = next(node for node in handlers if (
        "message_task = None" in ast.unparse(node)) == (path == "iterator"))
    setup = ast.parse('''
async def probe(error, late_shutdown=False):
    global SERVER_SHUTTING_DOWN
    current_run_id = "exact-current-run"
    session_id = "isolated-chat"
    stream_error = previous_error
    result_details = initial_result
    projection_error_run_ids = {current_run_id} if initial_projection_error else set()
    shutdown_interrupted_run_ids = set()
    retire_supervisor = False
    cancelled_error = None
    delivery_unknown = False
    provider_id = ""
    sdk_ownership_token = ""
    current_handle = None
    current_reconciliation_batches = []
    receipts_persisted_run_ids = set()
    manager = None
    cwd = "unused"
    current_prompt = "user prompt"
    tool_activity_run_ids = set()
    current_diff_baseline = {}
    changed_paths = set()
    seen_artifacts = set()
    manifest_path = None
''').body[0]
    setup.body.append(classify)
    setup.body.append(ast.Try(body=[ast.Raise(exc=ast.Name(id="error", ctx=ast.Load()))], handlers=[handler], orelse=[], finalbody=[]))
    setup.body.extend(ast.parse('''
if late_shutdown:
    SERVER_SHUTTING_DOWN = True
''').body)
    setup.body.append(finalize)
    setup.body.extend(ast.parse('await finalize_sdk_run()').body)
    module = ast.fix_missing_locations(ast.Module(body=[setup], type_ignores=[]))
    namespace = {
        "ClaudeSDKSupervisorClosed": ClaudeSDKSupervisorClosed,
        "SERVER_SHUTTING_DOWN": shutting_down,
        "STOPPED_RUNS": {"unrelated-run"}, "RUN_METADATA": {},
        "previous_error": previous_error, "initial_result": result,
        "initial_projection_error": projection_error,
        "concise_error_message": str, "clean_assistant_text": str,
        "suppress": suppress, "asyncio": asyncio, "BACKEND_CLAUDE": "claude",
        "CLAUDE_TRANSPORT_AGENT_SDK": "agent_sdk",
        "claude_empty_turn_failure_message": lambda **kwargs: None,
        "run_event_metadata": lambda run_id: {},
        "should_schedule_queue_after_finish": lambda *args: False,
        "logger": SimpleNamespace(exception=Mock()),
        "record_runtime_failure": Mock(), "record_runtime_success": Mock(),
        "schedule_next_queued_turn": Mock(),
    }
    for name in (
        "interrupt_claude_sdk_run_bounded", "evict_claude_sdk_chat",
        "persist_run_provider_session", "finish_outputs",
        "collect_recent_leftover_manifests", "append_turn_finished_event", "append_event",
        "acknowledge_claude_background_reconciliation", "persist_claude_background_task_receipts",
    ):
        namespace[name] = AsyncMock()
    namespace["release_turn_slot"] = AsyncMock(return_value=True)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class ClaudeShutdownStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconciliation_write_failure_or_cancellation_still_releases_exact_slot(self):
        for error in (OSError("receipt write failed"), asyncio.CancelledError()):
            env = load_probe("iterator", shutting_down=True)
            env["persist_claude_background_task_receipts"].side_effect = error
            await env["probe"](ClaudeSDKSupervisorClosed("closed"))
            env["release_turn_slot"].assert_awaited_once_with("isolated-chat", expected_run_id="exact-current-run")
            self.assertEqual(env["STOPPED_RUNS"], {"unrelated-run"})
            env["append_turn_finished_event"].assert_awaited_once()

    async def check_case(self, path, error, *, stopped, late_shutdown=False, **options):
        env = load_probe(path, **options)
        await env["probe"](error, late_shutdown)
        event = env["append_turn_finished_event"].await_args.args[1]
        self.assertEqual(event["run_id"], "exact-current-run")
        self.assertEqual(event["stopped"], stopped)
        self.assertEqual(event["exit_code"], None if stopped else 1)
        self.assertEqual(event.get("reason"), "server_shutdown" if stopped else None)
        self.assertEqual(env["STOPPED_RUNS"], {"unrelated-run"})
        if stopped:
            env["append_event"].assert_not_awaited()
            env["record_runtime_failure"].assert_not_called()
            env["record_runtime_success"].assert_not_called()
        else:
            self.assertTrue(env["append_event"].await_count)
            self.assertEqual(env["append_event"].await_args.args[1], "error")
            env["record_runtime_failure"].assert_called_once()
        env["schedule_next_queued_turn"].assert_not_called()
        env["release_turn_slot"].assert_awaited_once_with("isolated-chat", expected_run_id="exact-current-run")
        return env

    async def test_both_actual_catch_paths_record_intentional_shutdown_as_stopped(self):
        for path in ("iterator", "outer"):
            with self.subTest(path=path):
                env = await self.check_case(path, ClaudeSDKSupervisorClosed("supervisor was closed"), shutting_down=True, stopped=True)
                env["logger"].exception.assert_not_called()

    async def test_unexpected_closed_supervisor_remains_an_error(self):
        for path in ("iterator", "outer"):
            with self.subTest(path=path):
                await self.check_case(path, ClaudeSDKSupervisorClosed("supervisor was closed"), shutting_down=False, stopped=False)

    async def test_network_failure_and_same_text_without_typed_close_remain_errors(self):
        for error in (ConnectionError("connection lost"), RuntimeError("supervisor was closed")):
            for path in ("iterator", "outer"):
                with self.subTest(path=path, error=type(error).__name__):
                    await self.check_case(path, error, shutting_down=True, stopped=False)

    async def test_shutdown_after_failure_does_not_reclassify_it(self):
        for path in ("iterator", "outer"):
            with self.subTest(path=path):
                await self.check_case(path, ClaudeSDKSupervisorClosed("supervisor was closed"), shutting_down=False, late_shutdown=True, stopped=False)

    async def test_prior_stream_provider_or_projection_failure_is_not_hidden(self):
        for options in (
            {"previous_error": "earlier failure"},
            {"result": {"error": "provider failed"}},
            {"result": {"is_error": True}},
            {"projection_error": True},
        ):
            for path in ("iterator", "outer"):
                with self.subTest(path=path, options=options):
                    await self.check_case(path, ClaudeSDKSupervisorClosed("supervisor was closed"), shutting_down=True, stopped=False, **options)

    async def test_existing_result_text_is_preserved_on_outer_shutdown(self):
        env = await self.check_case("outer", ClaudeSDKSupervisorClosed("supervisor was closed"), shutting_down=True, stopped=True, result={"result_text": "Already received output"})
        self.assertEqual(env["append_turn_finished_event"].await_args.args[1]["result_text"], "Already received output")


if __name__ == "__main__":
    unittest.main()
