"""Codex side-question adapter tests: no server import, provider or subprocess."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import codex_side_question as adapter
import codex_provider
from claude_sdk_client import CLAUDE_PROVIDER_MCP_SERVER_NAME
from side_questions import SideQuestionError


def protocol_schema():
    return {"definitions": {
        "ThreadForkParams": {"properties": {
            "ephemeral": {"type": "boolean"},
            **{name: {} for name in ("excludeTurns", "runtimeWorkspaceRoots", "cwd",
                                    "developerInstructions", "config", "sandbox", "approvalPolicy",
                                    "permissions", "approvalsReviewer")},
        }},
        "TurnStartParams": {"properties": {"environments": {
            "type": ["array", "null"], "description": "Empty disables environment access for this turn.",
        }}},
    }}


def message(text="Answer", *, phase="final_answer", identifier="answer"):
    return {"method": "item/completed", "params": {"item": {
        "id": identifier, "type": "agentMessage", "phase": phase, "text": text,
    }}}


def completed(status="completed", error=None):
    return {"method": "turn/completed", "params": {"turn": {
        "status": status, "error": error,
    }}}


class ConfigurationTests(unittest.TestCase):
    def test_requires_native_fork_permissions_not_unused_environment_isolation(self):
        self.assertTrue(adapter.supports_native_side_chat(protocol_schema()))
        schema = protocol_schema()
        del schema["definitions"]["TurnStartParams"]
        self.assertTrue(adapter.supports_native_side_chat(schema))
        self.assertFalse(adapter._supports_empty_turn_environments(schema))
        self.assertTrue(adapter._supports_empty_turn_environments(protocol_schema()))
        for field in protocol_schema()["definitions"]["ThreadForkParams"]["properties"]:
            schema = protocol_schema()
            del schema["definitions"]["ThreadForkParams"]["properties"][field]
            self.assertFalse(adapter.supports_native_side_chat(schema), field)
        for malformed in ({}, None, {"definitions": []}):
            self.assertFalse(adapter.supports_native_side_chat(malformed))

    def test_disables_model_selected_subagents_and_legacy_notification_commands(self):
        # Title generation and endpoint probes still use this separate helper.
        config = adapter.isolated_config()
        self.assertIs(config["agents.enabled"], False)
        self.assertEqual(config["notify"], [])
        self.assertEqual(config["web_search"], "disabled")
        self.assertIs(config["tools.update_plan.enabled"], False)
        self.assertIs(config["tools.experimental_request_user_input.enabled"], False)
        self.assertEqual(config["project_doc_max_bytes"], 0)
        for key in ("orchestrator.skills.enabled", "orchestrator.mcp.enabled",
                    "skills.include_instructions", "skills.bundled.enabled"):
            self.assertIs(config[key], False)
        self.assertIs(config["features.skip_host_skill_discovery"], True)
        for name in ("apps", "plugins", "hooks", "multi_agent", "multi_agent_v2", "goals",
                     "memories", "image_generation", "shell_tool", "browser_use", "computer_use"):
            self.assertIs(config[f"features.{name}"], False)

    def test_side_chat_keeps_native_tools_and_excludes_only_parent_run_authority(self):
        name = CLAUDE_PROVIDER_MCP_SERVER_NAME
        parent = {"features.shell_tool": True, "web_search": "live",
                  "mcp_servers": {"ordinary": {"enabled": True}, name: {"http_headers": {"secret": "parent"}}},
                  f"mcp_servers.{name}.http_headers": {"secret": "parent"},
                  "shell_environment_policy.exclude": ["PRIVATE_KEY"]}
        before = json.dumps(parent, sort_keys=True)
        config = adapter.side_chat_config(parent)
        self.assertTrue(config["features.shell_tool"])
        self.assertEqual(config["web_search"], "live")
        self.assertEqual(config["mcp_servers"], {"ordinary": {"enabled": True}})
        self.assertFalse(config[f"mcp_servers.{name}.enabled"])
        self.assertFalse(config[f"mcp_servers.{name}.required"])
        self.assertEqual(config[f"mcp_servers.{name}.url"], "http://127.0.0.1:0")
        self.assertNotIn(f"mcp_servers.{name}.http_headers", config)
        self.assertEqual(config["shell_environment_policy.exclude"], ["PRIVATE_KEY"])
        self.assertFalse(config["agents.enabled"])
        self.assertEqual(json.dumps(parent, sort_keys=True), before)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn = SimpleNamespace(next_notification=AsyncMock(side_effect=[message(), completed()]), close=AsyncMock())
        self.client = SimpleNamespace(
            start=AsyncMock(), close=AsyncMock(),
            request=AsyncMock(return_value={"config": {"mcp_servers": {"ordinary": {}, "dotted.name": {}}}}),
            start_thread=AsyncMock(return_value="temporary-thread"),
            fork_thread=AsyncMock(return_value="temporary-thread"),
            read_thread=AsyncMock(return_value={"id": "temporary-thread", "ephemeral": True, "path": None}),
            start_turn=AsyncMock(return_value=self.turn),
        )
        self.factory = Mock(return_value=self.client)
        self.verify = AsyncMock()
        self.prepare_catalog = AsyncMock(side_effect=lambda executable, env, path: str(path))
        self.enterContext(patch.object(codex_provider, "prepare_native_catalog", self.prepare_catalog))
        async def start():
            callback = self.factory.call_args.kwargs.get("before_start")
            if callback:
                await callback()
            started = self.factory.call_args.kwargs.get("on_process_started")
            if started:
                started(12345, 12345)
        self.client.start.side_effect = start
        self.enterContext(patch.object(adapter, "CodexAppServerClient", self.factory))
        self.enterContext(patch.object(adapter, "_verify_protocol", self.verify))

    async def answer(self):
        return await adapter.answer_side_question("side question", parent_thread_id="parent-thread", executable="synthetic-codex",
            model="synthetic-model", env={"HOME": "/synthetic/auth", "PATH": "/bin",
                                         "AGENTSDOCK_CHAT_ID": "parent", "CODEX_THREAD_ID": "parent"},
            cwd="/synthetic/workspace", fork_overrides={"runtimeWorkspaceRoots": ["/synthetic/workspace"],
                "approvalPolicy": "on-request", "sandbox": "workspace-write"},
            turn_overrides={"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite"}})

    async def test_synced_chat_saves_native_fork_then_resumes_exact_child_after_restart(self):
        self.client.read_thread.return_value = {"id": "temporary-thread", "ephemeral": False, "path": "/private/native-side.jsonl"}
        self.client.clear_thread_goal = AsyncMock(return_value=False)
        self.client.resume_thread = AsyncMock(return_value="temporary-thread")
        saved = AsyncMock()
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=None,
            env={}, durable=True, persist_state=saved)
        self.assertEqual(await chat.ask("question"), "Answer")
        fork = self.client.fork_thread.await_args.args[1]
        self.assertFalse(fork["ephemeral"])
        self.assertTrue(fork["deferGoalContinuation"])
        self.client.clear_thread_goal.assert_awaited_once_with("temporary-thread")
        saved.assert_awaited_once_with({"thread_id": "temporary-thread", "path": "/private/native-side.jsonl"})
        await chat.close()
        self.turn.next_notification.side_effect = [message(), completed()]
        resumed = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=None,
            env={}, durable=True, resume_state=saved.await_args.args[0], persist_state=saved)
        self.assertEqual(await resumed.ask("follow up"), "Answer")
        self.client.fork_thread.assert_awaited_once()
        thread, params = self.client.resume_thread.await_args.args
        self.assertEqual(thread, "temporary-thread")
        self.assertEqual(params["path"], "/private/native-side.jsonl")
        self.assertTrue(params["deferGoalContinuation"])
        self.assertNotIn("ephemeral", params)
        self.assertEqual(self.client.start_turn.await_args.args[0], "temporary-thread")
        await resumed.close()

    async def test_synced_chat_never_starts_answer_before_native_binding_is_saved(self):
        self.client.read_thread.return_value = {"id": "temporary-thread", "ephemeral": False, "path": "/private/native-side.jsonl"}
        self.client.clear_thread_goal = AsyncMock(return_value=False)
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=None,
            env={}, durable=True, persist_state=AsyncMock(side_effect=OSError("disk full")))
        with self.assertRaises(OSError):
            await chat.ask("question")
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_synced_chat_rejects_resuming_parent_as_side_context(self):
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=None,
            env={}, durable=True, resume_state={"thread_id": "parent-thread"})
        with self.assertRaises(SideQuestionError):
            await chat.ask("question")
        self.client.start_turn.assert_not_awaited()
        self.client.fork_thread.assert_not_awaited()

    async def test_forks_native_history_with_parent_workspace_and_permissions(self):
        self.assertEqual(await self.answer(), "Answer")
        args, options = self.factory.call_args
        self.prepare_catalog.assert_not_awaited()
        self.assertNotIn("before_start", options)
        self.assertEqual(args, ("synthetic-codex",))
        self.assertEqual(options["env_factory"](), {"HOME": "/synthetic/auth", "PATH": "/bin"})
        self.assertEqual(options["cwd"], "/synthetic/workspace")
        self.assertTrue(Path(self.verify.await_args.args[1]).name.startswith("agentsdock-side-chat-"))
        self.client.request.assert_not_awaited()
        source, params = self.client.fork_thread.await_args.args
        self.assertEqual(source, "parent-thread")
        self.client.start_thread.assert_not_awaited()
        self.assertTrue(params["ephemeral"])
        self.assertTrue(params["excludeTurns"])
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/synthetic/workspace"])
        self.assertNotIn("environments", params)  # Unsupported on native forks.
        self.assertNotIn("dynamicTools", params)
        self.assertNotIn("deferGoalContinuation", params)
        self.assertEqual(params["cwd"], options["cwd"])
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertNotIn("baseInstructions", params)
        self.assertEqual(params["model"], "synthetic-model")
        self.assertNotIn("sqlite_home", params["config"])
        self.assertEqual(params["config"]["log_dir"], str(Path(self.verify.await_args.args[1]) / "log"))
        self.assertEqual(params["config"]["history.persistence"], "none")
        # These must be process startup overrides, not only thread overrides:
        # app-server can initialize databases before thread/start.
        cli = options["app_server_args"]
        cli_config = {cli[index + 1].split("=", 1)[0]: json.loads(cli[index + 1].split("=", 1)[1])
                      for index in range(0, len(cli), 2)}
        self.assertNotIn("sqlite_home", cli_config)
        for key in ("log_dir", "history.persistence"):
            self.assertEqual(cli_config[key], params["config"][key])
        self.assertNotIn("threadId", params)
        self.assertNotIn("parentThreadId", params)
        self.assertNotIn("mcp_servers", params["config"])
        self.assertIs(params["config"][f"mcp_servers.{CLAUDE_PROVIDER_MCP_SERVER_NAME}.enabled"], False)
        self.client.start_turn.assert_awaited_once_with("temporary-thread",
            [{"type": "text", "text": "side question"}],
            overrides={"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite"}})
        self.client.read_thread.assert_awaited_once_with("temporary-thread", include_turns=False)
        self.turn.close.assert_awaited_once()
        self.client.close.assert_awaited_once()

    async def test_preserves_provider_owned_runtime_location_without_reindexing_override(self):
        supplied = {"HOME": "/synthetic/auth", "CODEX_HOME": "/synthetic/provider",
                    "CODEX_SQLITE_HOME": "/synthetic/provider-state"}
        self.assertEqual(await adapter.answer_side_question("question", parent_thread_id="parent-thread", executable="synthetic-codex",
            model=None, env=supplied), "Answer")
        options = self.factory.call_args.kwargs
        self.assertEqual(options["env_factory"](), supplied)
        self.assertFalse(any(value.startswith("sqlite_home=") for value in options["app_server_args"]))
        self.assertTrue(self.client.fork_thread.await_args.args[1]["ephemeral"])
        self.assertNotIn("runtimeWorkspaceRoots", self.client.fork_thread.await_args.args[1])
        self.assertNotIn("environments", self.client.start_turn.await_args.kwargs["overrides"])

    async def test_side_chat_uses_normal_codex_transport_deadlines(self):
        from codex_app_server import CodexAppServerClient

        await self.answer()
        # Instantiate the shared transport from the actual adapter arguments;
        # side chat must not impose a shorter startup/fork/turn-ack deadline.
        transport = CodexAppServerClient(*self.factory.call_args.args, **self.factory.call_args.kwargs)
        ordinary = CodexAppServerClient("synthetic-codex", cwd="/synthetic", env_factory=dict)
        self.assertEqual(transport.request_timeout, ordinary.request_timeout)
        self.assertEqual(transport.lifecycle_timeout, ordinary.lifecycle_timeout)

    async def test_unsupported_protocol_never_starts_provider(self):
        self.verify.side_effect = SideQuestionError(503, "Update Codex")
        with self.assertRaises(SideQuestionError):
            await self.answer()
        self.factory.assert_not_called()

    async def test_custom_provider_key_survives_isolation_without_normal_login_credentials(self):
        from codex_provider import ENV_KEY, PROVIDER_ID
        selected = {"base_url": "https://example.invalid/v1", "model": "custom/model", "api_key": "synthetic-provider-key"}
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=selected["model"],
            env={"OPENAI_API_KEY": "unrelated-key", "AGENTSDOCK_TOKEN": "server-token"}, provider_selection=selected)
        self.assertEqual(await chat.ask("question"), "Answer")
        options = self.factory.call_args.kwargs
        self.assertEqual(options["env_factory"]()[ENV_KEY], selected["api_key"])
        self.assertNotIn("OPENAI_API_KEY", options["env_factory"]())
        self.assertNotIn("AGENTSDOCK_TOKEN", options["env_factory"]())
        self.assertNotIn(selected["api_key"], str(options["app_server_args"]))
        self.assertEqual(options["protected_env_keys"], (ENV_KEY,))
        params = self.client.fork_thread.await_args.args[1]
        self.prepare_catalog.assert_awaited_once()
        self.assertEqual(params["config"]["model_catalog_json"], str(Path(options["cwd"]) / "models.json"))
        self.assertIn('model_catalog_json=' + json.dumps(params["config"]["model_catalog_json"]), options["app_server_args"])
        self.assertEqual(params["modelProvider"], PROVIDER_ID)
        self.assertFalse(params["config"]["model_providers"][PROVIDER_ID]["requires_openai_auth"])
        self.assertEqual(params["config"]["model_reasoning_summary"], "none")
        overrides = self.client.start_turn.await_args.kwargs["overrides"]
        self.assertNotIn("environments", overrides)
        self.assertNotIn("effort", overrides)
        self.assertIsNone(overrides["collaborationMode"]["settings"]["reasoning_effort"])
        await chat.close()

    async def test_followups_reuse_provider_history_until_explicit_close(self):
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex",
            model=None, env={"HOME": "/synthetic/auth"})
        self.turn.next_notification.side_effect = [message("First answer"), completed(),
                                                  message("Followup answer"), completed()]
        self.assertEqual(await chat.ask("First question"), "First answer")
        self.assertEqual(await chat.ask("What did you just mean?"), "Followup answer")
        self.factory.assert_called_once()
        self.client.fork_thread.assert_awaited_once()
        self.assertEqual(self.client.start_turn.await_args_list[1].args,
                         ("temporary-thread", [{"type": "text", "text": "What did you just mean?"}]))
        self.assertEqual(self.turn.close.await_count, 2)
        self.client.close.assert_not_awaited()
        await chat.close()
        await chat.close()
        self.client.close.assert_awaited_once()
        with self.assertRaises(SideQuestionError) as caught:
            await chat.ask("Too late")
        self.assertEqual(caught.exception.status_code, 409)

    async def test_fork_must_confirm_ephemeral_before_any_model_turn(self):
        for metadata in ({"ephemeral": False, "path": None}, {"ephemeral": True, "path": "/saved"}, {}):
            with self.subTest(metadata=metadata):
                self.client.read_thread.return_value = metadata
                with self.assertRaises(SideQuestionError):
                    await self.answer()
        self.client.start_turn.assert_not_awaited()

    async def test_fork_rejects_source_identity_without_touching_parent(self):
        self.client.fork_thread.return_value = "parent-thread"
        with self.assertRaises(SideQuestionError):
            await self.answer()
        self.client.read_thread.assert_not_awaited()
        self.client.start_turn.assert_not_awaited()

    async def test_native_rollout_path_is_only_passed_to_provider_fork(self):
        await adapter.answer_side_question("question", parent_thread_id="parent-thread",
            parent_rollout_path="/synthetic/parent.jsonl", executable="synthetic-codex", model=None, env={})
        self.assertEqual(self.client.fork_thread.await_args.args[1]["path"], "/synthetic/parent.jsonl")
        self.assertEqual(self.client.start_turn.await_args.args[1], [{"type": "text", "text": "question"}])

    async def test_cancel_during_stalled_fork_closes_owned_process_without_waiting_for_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def fork(*args):
            entered.set()
            await release.wait()
            raise adapter.CodexAppServerError("fork connection closed")
        async def close():
            order.append("closed")
            release.set()
        self.client.fork_thread.side_effect = fork
        self.client.close.side_effect = close
        task = asyncio.create_task(self.answer())
        await entered.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        release.set()  # Always release the fixture if the regression fails.
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(task, done, "Stop waited for a fork response instead of closing its private process")
        self.assertEqual(order, ["closed"])
        self.client.start_turn.assert_not_awaited()

    async def test_cancel_during_stalled_initialize_closes_spawned_process(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def start():
            started = self.factory.call_args.kwargs.get("on_process_started")
            if started:
                started(12345, 12345)
            entered.set()
            await release.wait()
            raise adapter.CodexAppServerError("initialization connection closed")
        self.client.start.side_effect = start
        self.client.close.side_effect = lambda: release.set()
        task = asyncio.create_task(self.answer())
        await entered.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(task, done)
        self.client.close.assert_awaited_once()
        self.client.fork_thread.assert_not_awaited()

    async def test_fork_reply_racing_stop_cannot_restart_transport_for_metadata(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fork(*args):
            entered.set()
            await release.wait()
            return "temporary-thread"
        self.client.fork_thread.side_effect = fork
        self.client.close.side_effect = lambda: release.set()
        task = asyncio.create_task(self.answer())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.read_thread.assert_not_awaited()
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_explicit_close_cancels_current_ask_and_reaps_once(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        self.turn.next_notification.side_effect = wait
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex", model=None, env={})
        task = asyncio.create_task(chat.ask("pending"))
        await entered.wait()
        with self.assertRaises(SideQuestionError) as caught:
            await chat.ask("concurrent")
        self.assertEqual(caught.exception.status_code, 409)
        await chat.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.close.assert_awaited_once()

    async def test_approval_callback_only_accepts_exact_active_side_thread(self):
        callback = AsyncMock(return_value={"decision": "accept"})
        chat = adapter.NativeCodexSideChat("parent-thread", executable="synthetic-codex",
            model=None, env={}, server_request_handler=callback)
        async def notifications():
            handler = self.factory.call_args.kwargs["server_request_handler"]
            method = "item/commandExecution/requestApproval"
            self.assertEqual(await handler(1, method, {"threadId": "parent-thread"}), {"decision": "decline"})
            self.assertEqual(await handler(2, method, {"threadId": "temporary-thread"}), {"decision": "accept"})
            self.turn.next_notification.side_effect = [completed()]
            return message()
        self.turn.next_notification.side_effect = notifications
        self.assertEqual(await chat.ask("Read a file"), "Answer")
        callback.assert_awaited_once_with(2, "item/commandExecution/requestApproval", {"threadId": "temporary-thread"})
        self.assertEqual(await chat._handle_server_request(3, "item/commandExecution/requestApproval",
            {"threadId": "temporary-thread"}), {"decision": "decline"})
        await chat.close()

    async def test_final_answer_ignores_commentary_and_duplicate_completion(self):
        self.turn.next_notification.side_effect = [
            message("Working", phase="commentary", identifier="progress"),
            message("Private reasoning", phase="analysis", identifier="private"),
            message("First"), message("First"),
            message("Second", identifier="answer2"), completed(),
        ]
        self.assertEqual(await self.answer(), "First\n\nSecond")

    async def test_failure_and_empty_completion_never_return_an_answer(self):
        for packets in ([completed("failed", {"message": "private provider error"})], [completed()]):
            with self.subTest(packets=packets):
                self.turn.next_notification.side_effect = packets
                with self.assertRaises(SideQuestionError) as caught:
                    await self.answer()
                self.assertNotIn("private", str(caught.exception))

    async def test_transport_error_is_sanitized_and_process_closed(self):
        self.client.start_turn.side_effect = adapter.CodexAppServerError("private credentials/path")
        with self.assertRaises(SideQuestionError) as caught:
            await self.answer()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private", str(caught.exception))
        self.client.close.assert_awaited_once()

    async def test_output_is_bounded_in_utf8_bytes(self):
        self.turn.next_notification.side_effect = [message("é" * 6), completed()]
        with patch.object(adapter, "MAX_OUTPUT_BYTES", 10):
            with self.assertRaises(SideQuestionError) as caught:
                await self.answer()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("output limit", str(caught.exception))
        self.client.close.assert_awaited_once()

    async def test_cancel_during_pending_answer_closes_only_owned_client(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        self.turn.next_notification.side_effect = wait
        task = asyncio.create_task(self.answer())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.close.assert_awaited_once()

    async def test_cancel_during_start_joins_spawn_before_closing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def start():
            entered.set()
            await release.wait()
            order.append("started")
            started = self.factory.call_args.kwargs.get("on_process_started")
            if started:
                started(12345, 12345)
        async def close():
            order.append("closed")
        self.client.start.side_effect = start
        self.client.close.side_effect = close
        task = asyncio.create_task(self.answer())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(order, ["started", "closed"])
        self.client.fork_thread.assert_not_awaited()

    async def test_repeated_cancellation_does_not_interrupt_process_cleanup(self):
        answering, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reaped = []
        async def wait():
            answering.set()
            await asyncio.Event().wait()
        async def close():
            closing.set()
            await release.wait()
            reaped.append(True)
        self.turn.next_notification.side_effect = wait
        self.client.close.side_effect = close
        task = asyncio.create_task(self.answer())
        await answering.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(reaped, [True])
        self.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
