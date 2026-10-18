"""Runner ownership/failure contracts without user profiles or network calls."""
import asyncio
from pathlib import Path
import tempfile
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gemini_acp_client import GeminiProtocolError
import gemini_runner

ID = "12345678-1234-1234-1234-123456789abc"


class Runner(unittest.IsolatedAsyncioTestCase):
    def test_mcp_error_points_to_exact_workspace_without_auth_or_trust_mutation(self):
        for cwd in ("/project", "/project with 中文", "/project's $(not-a-command)", "/project\nwith-newline"):
            message = gemini_runner.communication_unavailable_message(cwd)
            self.assertIn("Workspace: " + json.dumps(cwd, ensure_ascii=False), message)
            self.assertNotIn("\n", message)
            self.assertIn("not a sign-in error", message)
            self.assertIn("/permissions", message)
            self.assertIn("does not grant folder trust automatically", message)
            self.assertNotIn("--skip-trust", message)

    async def test_cleanup_fence_only_clears_exact_owner_after_durable_save(self):
        row = {"gemini_cleanup_pending_run_id": "owned-run"}
        store = SimpleNamespace(sessions={"s": row}, save=AsyncMock(side_effect=OSError("disk full")))
        api = SimpleNamespace(STORE=store)
        await gemini_runner.clear_cleanup_fence(api, "s", "another-run")
        store.save.assert_not_awaited()
        with self.assertRaises(OSError):
            await gemini_runner.clear_cleanup_fence(api, "s", "owned-run")
        self.assertEqual(row["gemini_cleanup_pending_run_id"], "owned-run")
        store.save = AsyncMock()
        await gemini_runner.clear_cleanup_fence(api, "s", "owned-run")
        self.assertNotIn("gemini_cleanup_pending_run_id", row)
        store.save.assert_awaited_once_with(durable=True)

    async def scenario(self, *, updates=(), history=None, failure=False, history_error=None, connection=None, model=None):
        with tempfile.TemporaryDirectory() as directory:
            api = SimpleNamespace(
                STORE=SimpleNamespace(sessions={"s": {"backend": "gemini"}}, save=AsyncMock()),
                ACTIVE={"s": {"run_id": "r", "backend": "gemini", "provider_tools_ready": asyncio.Event()}},
                DELETING_SESSIONS=set(), STOPPED_RUNS=set(), DEFAULT_CWD=directory,
                CROSS_CHAT_CAPABILITY_LOCK=asyncio.Lock(), CROSS_CHAT_CAPABILITIES={}, ACTIVE_LOCK=asyncio.Lock(),
                PROVIDER_CONNECTION_STORE=SimpleNamespace(for_session=lambda sess: ({**connection, "model": sess.get("model") or connection.get("model")} if connection else None), root=Path(directory)),
                validate_provider_runtime_env=lambda _: {}, opencode_helper_output_redactor=lambda *_: lambda x: x,
                runner_env=lambda: {"HOME": directory}, run_event_metadata=lambda _: {},
                turn_slot_is_owned=AsyncMock(return_value=True),
                append_event=AsyncMock(), clear_reasoning_summary_stream=AsyncMock(),
                update_reasoning_summary_stream=AsyncMock(), persist_reasoning_summary=AsyncMock(),
                persist_run_provider_session=AsyncMock(), mark_provider_turn_ready=AsyncMock(),
                bounded_codex_interaction_value=lambda x: x,
                session_prompt_addendum=lambda _: "", record_runtime_success=Mock(),
                clear_active_process=AsyncMock(), finalize_owned_turn_finished=AsyncMock(),
                join_task_despite_caller_cancellation=lambda task: task)
            class Client:
                session_id = ID
                def __init__(self, *, on_update, **_): self.update = on_update
                async def start(self, command, **__): api.started_command = command
                async def open_session(self, **_): return {"modes": {"availableModes": [{"id": "default"}]}}
                async def request(self, method, params):
                    api.mode_request = (method, params)
                    return {}
                async def prompt(self, *_):
                    for update in updates: await self.update(update)
                    if failure: raise GeminiProtocolError("Synthetic failure")
                    return {"stopReason": "end_turn"}
                async def close(self): pass
            with patch.object(gemini_runner, "GeminiACPClient", Client), patch.object(
                gemini_runner, "build_environment", return_value={"HOME": directory}
            ) as environment, patch("gemini_history.native_snapshot", return_value=history, side_effect=history_error), patch(
                "gemini_content.attachments", return_value=[]
            ):
                await gemini_runner.run(api, "s", "r", "test", {"cwd": directory, "_gemini_executable": "fixture", "model": model}, Path(directory))
                if not connection:
                    self.assertIsNone(environment.call_args.kwargs["api_key"], "CLI mode must not require an API connection")
                    self.assertIsNone(environment.call_args.kwargs["api_home"])
                else:
                    self.assertEqual(environment.call_args.kwargs["api_key"], connection["api_key"])
                    self.assertIsNotNone(environment.call_args.kwargs["api_home"])
            return api

    async def test_api_model_defaults_reach_cli_and_explicit_chat_model_wins(self):
        connection = {"base_url": "https://generativelanguage.googleapis.com", "api_key": "fixture-key", "credential_id": "a" * 32}
        for default, model, expected in ((None, None, "auto"), ("gemini-saved", None, "gemini-saved"),
                                         ("gemini-saved", "gemini-chosen", "gemini-chosen")):
            api = await self.scenario(connection={**connection, "model": default}, model=model)
            self.assertEqual(api.started_command, ["fixture", "--acp", "--approval-mode", "default", "--model", expected])
            self.assertNotIn(connection["api_key"], str(api.started_command))
        native = await self.scenario()
        self.assertEqual(native.started_command, ["fixture", "--acp", "--approval-mode", "default"])
        self.assertEqual(native.mode_request, ("session/set_mode", {"sessionId": ID, "modeId": "default"}))

    async def test_native_connection_runs_without_custom_api_object(self):
        api = await self.scenario(updates=[{"type": "text", "text": "answer"}], history={"messages": [{"type": "gemini"}]})
        api.record_runtime_success.assert_called_once_with("gemini")
        self.assertEqual(api.persist_run_provider_session.call_args.args[3], ID)
        self.assertFalse(api.finalize_owned_turn_finished.call_args.kwargs["payload"]["is_error"])

    async def test_failed_first_request_has_no_orphan_identity(self):
        api = await self.scenario(failure=True, history={"messages": [{"type": "user"}]})
        api.persist_run_provider_session.assert_not_called()
        self.assertTrue(api.finalize_owned_turn_finished.call_args.kwargs["payload"]["is_error"])
        unknown = await self.scenario(failure=True, history_error=GeminiProtocolError("Unreadable"))
        self.assertEqual(unknown.persist_run_provider_session.call_args.args[3], ID,
                         "Unreadable history must not be treated as a known-empty first attempt")

    async def test_missing_history_after_visible_activity_keeps_identity_and_fails(self):
        for updates in ([{"type": "text", "text": "partial"}],
                        [{"type": "tool", "call_id": "tool1", "status": "completed", "title": "Read", "content": []}]):
            with self.subTest(updates=updates):
                api = await self.scenario(updates=updates)
                self.assertEqual(api.persist_run_provider_session.call_args.args[3], ID)
                self.assertTrue(api.finalize_owned_turn_finished.call_args.kwargs["payload"]["is_error"])

    async def test_plan_updates_and_native_diffs_are_recorded(self):
        diff = {"type": "diff", "path": "test.txt", "oldText": "old", "newText": "new"}
        plan = {"type": "plan", "entries": [{"content": "Edit", "status": "in_progress", "priority": "medium"}]}
        api = await self.scenario(updates=[plan, plan,
            {"type": "tool", "call_id": "tool1", "status": "pending", "title": "Write", "content": [diff]},
            {"type": "tool", "call_id": "tool1", "status": "completed", "title": "Write", "content": [diff]}],
            history={"messages": [{"type": "gemini"}]})
        rows = [call.args for call in api.append_event.call_args_list]
        self.assertEqual(sum(row[1] == "reasoning_summary" for row in rows), 1)
        tool = next(row[2]["tool"] for row in rows if row[1] == "tool_finished")
        self.assertEqual(tool["input"]["acp_content"], [diff])
        self.assertIn('"newText": "new"', tool["output"])
