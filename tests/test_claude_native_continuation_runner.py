import asyncio
import json
import time
import unittest
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server as server
from claude_sdk_client import ClaudeSDKNativeContinuation

if __package__:
    from . import test_claude_sdk_runner as fixtures
else:
    import test_claude_sdk_runner as fixtures


class NativeHandle(fixtures.FakeClaudeRun):
    def set_pending_mail_hint(self, callback):
        self.pending_mail_hint = callback


class NativeContinuationRunnerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ClaudeSDKRunnerTests.asyncSetUp
    asyncTearDown = fixtures.ClaudeSDKRunnerTests.asyncTearDown

    async def prepare(self):
        self.session["claude_session_id"] = "provider"
        self.manager = fixtures.FakeClaudeManager()
        server.CLAUDE_SDK_MANAGER = self.manager
        path = await server.issue_cross_chat_capability(
            "chat-claude", "run_claude", [],
            source_user_instruction="Publish the completed report and read my mail",
            source_is_user_turn=True, actions={"publish", "jobs"},
        )
        await server.provider_authority_runtime_env("run_claude", path, "chat-claude", [])
        self.authority = next(iter(server.CROSS_CHAT_CAPABILITIES.values()))
        self.authority["team_mail_send_count"] = 2
        self.authority["team_authority_generation"] = "original-generation"
        self.old_token = json.loads(path.read_text())["provider_capability"]
        await server.revoke_cross_chat_capability("run_claude")
        server.BUSY_SESSIONS.clear()
        server.CURRENT_TURNS.clear()
        done = asyncio.Event()
        done.set()
        manifest = Path(self.authority_temporary.name) / "manifest.json"
        key = server.claude_sdk_configuration_key(
            self.session, self.cwd, "/usr/bin/claude",
            server.session_system_prompt("chat-claude", self.session, manifest),
        )
        self.context = {
            "authority": self.authority, "manager": self.manager,
            "options": (object(), key, "/usr/bin/claude"),
            "manifest_path": manifest, "prompt": "Original explicit instruction",
            "turn": {"prompt": "Original explicit instruction", "file_ids": ["attachment"],
                     "backend": "claude", "client_capabilities": ["claude-interactive-v1"]},
            "completed": {"run_claude": done},
        }

    def receipt(self, run_id="run_wake1", parent="run_claude"):
        self.manager.active_run_id = run_id
        return ClaudeSDKNativeContinuation(
            chat_id="chat-claude", run_id=run_id, parent_run_id=parent, parent_run_ids=(parent,),
            provider_session_id="provider", ownership_token=self.manager.owner_token,
            control_generation="generation-1", task_id="task-1", task_ids=("task-1",),
            task_type="local_bash", tool_use_id="tool-1", handle=NativeHandle(),
        )

    def runner_patches(self):
        stack = ExitStack()
        self.events = AsyncMock(return_value={})

        async def finish(_session_id, payload):
            await server.revoke_cross_chat_capability(payload["run_id"])
            return payload

        self.finished = AsyncMock(side_effect=finish)
        for name, value in {
            "capture_git_baseline": AsyncMock(return_value={}),
            "watch_manifest_artifacts": fixtures.wait_forever,
            "append_event": self.events,
            "append_turn_finished_event": self.finished,
            "persist_run_provider_session": AsyncMock(),
            "sample_claude_context_usage": AsyncMock(),
            "refresh_claude_goal": AsyncMock(),
            "cancel_claude_interactions": AsyncMock(),
            "collect_manifest": AsyncMock(),
            "collect_recent_leftover_manifests": AsyncMock(),
            "publish_turn_code_diff": AsyncMock(),
            "record_runtime_success": Mock(),
            "record_runtime_failure": Mock(),
            "should_schedule_queue_after_finish": Mock(return_value=False),
            "discard_owned_git_baselines": AsyncMock(),
        }.items():
            stack.enter_context(patch.object(server, name, value))
        return stack

    async def complete(self, receipt):
        receipt.handle.messages.put_nowait({
            "type": "AssistantMessage", "uuid": receipt.run_id + "-native-message",
            "content": [{"type": "text", "text": "Native child completed"}],
        })
        receipt.handle.messages.put_nowait({
            "type": "ResultMessage", "result": "Native child completed", "session_id": "provider",
        })
        await asyncio.wait_for(self.context["completed"][receipt.run_id].wait(), 2)
        tasks = list(server.SESSION_TURN_TASKS.get("chat-claude") or ())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    async def test_two_native_wakes_stream_without_query_and_preserve_parent_authority(self):
        await self.prepare()
        with self.runner_patches():
            for index in (1, 2):
                receipt = self.receipt(f"run_wake{index}", "run_claude" if index == 1 else "run_wake1")
                self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
                self.assertEqual(self.manager.start_calls, [])
                current = server.CURRENT_TURNS["chat-claude"]
                self.assertEqual(current["prompt"], "Original explicit instruction")
                self.assertEqual(current["file_ids"], ["attachment"])
                self.assertEqual(current["purpose"], "provider_background_wake")
                path, runtime = await server.provider_tool_capability_snapshot(
                    "chat-claude", receipt.run_id, backend="claude",
                    claude_owner_token=self.manager.owner_token,
                )
                self.assertEqual(runtime["AGENTSDOCK_PROVIDER_AUTHORITY_FILE"], str(path))
                self.assertNotEqual(json.loads(path.read_text())["provider_capability"], self.old_token)
                self.assertIs(next(iter(server.CROSS_CHAT_CAPABILITIES.values())), self.authority)
                self.assertEqual(self.authority["team_mail_send_count"], index + 1)
                self.assertEqual(self.authority["source_user_instruction"],
                                 "Publish the completed report and read my mail")
                self.assertEqual(self.authority["team_authority_generation"], "original-generation")
                self.authority["team_mail_send_count"] += 1
                with patch.object(server, "take_chat_mailbox_hint", return_value="mail") as hint:
                    self.assertEqual(receipt.handle.pending_mail_hint(), "mail")
                    hint.assert_called_once_with("chat-claude", receipt.run_id)
                await self.complete(receipt)
                self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
                self.assertNotIn("chat-claude", server.ACTIVE)
                self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
            starts = [call.args[2] for call in self.append_durable_event.await_args_list
                      if call.args[1] == "turn_started"]
            self.assertEqual([row["parent_run_id"] for row in starts], ["run_claude", "run_wake1"])
            self.assertTrue(all(row["prompt"] == "" and row["metadata_only"] for row in starts))
            texts = [call.args[2] for call in self.events.await_args_list if call.args[1] == "assistant_text"]
            self.assertEqual([row["run_id"] for row in texts], ["run_wake1", "run_wake2"])
            self.assertEqual([row["provider_message_id"] for row in texts],
                             ["run_wake1-native-message", "run_wake2-native-message"])

    async def test_human_stop_delete_provider_and_configuration_winners_refuse_admission(self):
        for conflict in ("human", "stop", "delete", "provider", "configuration", "generation", "lifecycle"):
            with self.subTest(conflict=conflict):
                await self.prepare()
                receipt = self.receipt()
                lock = server.session_lifecycle_lock("chat-claude")
                if conflict == "human":
                    server.BUSY_SESSIONS.add("chat-claude")
                    server.CURRENT_TURNS["chat-claude"] = {"run_id": "human"}
                elif conflict == "stop":
                    server.STOP_REQUESTS.add("chat-claude")
                elif conflict == "delete":
                    server.DELETING_SESSIONS.add("chat-claude")
                elif conflict == "provider":
                    self.session["claude_session_id"] = "replacement"
                elif conflict == "configuration":
                    self.session["model"] = "replacement-model"
                elif conflict == "generation":
                    self.manager.active_run_id = "replacement-run"
                else:
                    await lock.acquire()
                try:
                    self.assertFalse(await server.admit_claude_native_continuation(receipt, self.context))
                    self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
                    self.assertEqual(self.manager.start_calls, [])
                    if conflict == "human":
                        self.assertEqual(server.CURRENT_TURNS["chat-claude"]["run_id"], "human")
                finally:
                    if conflict == "lifecycle":
                        lock.release()
                    server.STOP_REQUESTS.clear()
                    server.DELETING_SESSIONS.clear()
                    self.session["model"] = "claude-opus"

    async def test_expired_authority_does_not_stop_output_or_get_renewed(self):
        await self.prepare()
        expired = time.time() - 60
        self.authority["expires_at"] = expired
        with self.runner_patches():
            receipt = self.receipt()
            self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
            path = Path(self.authority["authority_path"])
            self.assertEqual(json.loads(path.read_text())["expires_at"], expired)
            self.assertEqual(self.authority["expires_at"], expired)
            token = json.loads(path.read_text())["provider_capability"]
            authorized = await server.authorize_provider_action(
                fixtures.ClaudeSDKRunnerTests.provider_request(token),
                action="publish", session_id="chat-claude",
            )
            self.assertEqual(authorized["source_run_id"], receipt.run_id)
            with self.assertRaises(server.HTTPException):
                await server.authorize_provider_action(
                    fixtures.ClaudeSDKRunnerTests.provider_request(self.old_token),
                    action="publish", session_id="chat-claude",
                )
            await self.complete(receipt)

    async def test_mixed_task_provenance_keeps_only_selected_parent_authority(self):
        await self.prepare()
        with self.runner_patches():
            receipt = replace(self.receipt(), parent_run_ids=("run_older", "run_claude"),
                              task_ids=("older-task", "task-1"))
            self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
            row = self.append_durable_event.await_args.args[2]
            self.assertEqual(row["parent_run_ids"], ["run_older", "run_claude"])
            self.assertEqual(row["provider_task_ids"], ["older-task", "task-1"])
            self.assertEqual(row["parent_run_id"], "run_claude")
            self.assertEqual(set(self.authority["actions"]), {"publish", "jobs"})
            self.assertEqual(self.authority["team_mail_send_count"], 2)
            await self.complete(receipt)

    async def test_fast_wake_waits_for_exact_parent_finalization(self):
        await self.prepare()
        done = self.context["completed"]["run_claude"]
        done.clear()
        with self.runner_patches():
            receipt = self.receipt()
            admission = asyncio.create_task(server.admit_claude_native_continuation(receipt, self.context))
            await asyncio.sleep(0)
            self.assertFalse(admission.done())
            self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
            done.set()
            self.assertTrue(await asyncio.wait_for(admission, 2))
            await self.complete(receipt)

    async def test_permission_and_provider_tools_wait_for_new_owner_admission(self):
        captured = {}
        permission = AsyncMock(return_value={"behavior": "allow"})
        execute = AsyncMock(return_value=("read result", False))
        with patch.object(server, "claude_sdk_cli_path", return_value="/usr/bin/claude"), \
                patch.object(server, "create_claude_agent_options", side_effect=lambda **kw: kw), \
                patch.object(server, "create_claude_sdk_mcp_server", side_effect=lambda **kw: captured.update(kw) or {}), \
                patch.object(server, "handle_claude_tool_permission", permission), \
                patch.object(server, "execute_provider_tool", execute):
            options, _, _ = server.build_claude_sdk_options(
                "chat-claude", self.session, self.cwd, Path(self.cwd) / "manifest.json")
            gate = asyncio.Event()
            options["_agentsdock_bind_native_admission_gate"](gate)
            permission_call = asyncio.create_task(options["can_use_tool"]("Bash", {}, {}))
            tool_call = asyncio.create_task(captured["handler"]({"helper": "mail", "arguments": ["list"]}))
            await asyncio.sleep(0)
            self.assertFalse(permission_call.done())
            self.assertFalse(tool_call.done())
            permission.assert_not_called()
            execute.assert_not_called()
            options["can_use_tool"]._agentsdock_bind_owner("new-owner")
            options["_agentsdock_bind_provider_tool_owner"]("new-owner", "run_new")
            gate.set()
            await asyncio.gather(permission_call, tool_call)
            self.assertEqual(permission.await_args.kwargs["owner_token"], "new-owner")
            self.assertEqual(execute.await_args.args[:2], ("chat-claude", "run_new"))
            self.assertEqual(execute.await_args.kwargs["claude_owner_token"], "new-owner")

    async def test_early_runner_failure_refuses_without_stranding_busy_or_authority(self):
        await self.prepare()
        with self.runner_patches(), patch.object(server, "capture_git_baseline", AsyncMock(side_effect=OSError("disk"))):
            self.assertFalse(await server.admit_claude_native_continuation(self.receipt(), self.context))
            await asyncio.sleep(0)
            self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
            self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})

    async def test_idle_stop_retires_native_bash_wake_without_durable_subagents(self):
        await self.prepare()
        self.manager.has_pending_native_continuations = lambda chat: chat == "chat-claude"
        with patch.object(server, "build_claude_subagent_snapshot", return_value={"active_count": 0, "subagents": []}), \
                patch.object(server, "cancel_codex_interactions", AsyncMock()), \
                patch.object(server, "cancel_claude_interactions", AsyncMock()):
            result = await server.stop_turn(
                "chat-claude", emit_event=False, schedule_queue=False,
                pause_queued_turns_on_stop=False,
            )
        self.assertEqual(self.manager.evict_calls, [("chat-claude", True)])
        self.assertFalse(self.manager.loaded)
        self.assertTrue(result["subagents"]["fence_committed"])
        self.assertTrue(any(call.args[1] == "claude_subagents_stopped"
                            for call in self.append_durable_event.await_args_list))


if __name__ == "__main__":
    unittest.main()
