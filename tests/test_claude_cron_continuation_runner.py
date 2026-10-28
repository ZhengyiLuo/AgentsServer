import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import agent_server as server
from claude_sdk_client import ClaudeSDKNativeWakeDeferred

if __package__:
    from . import test_claude_native_continuation_runner as native
else:
    import test_claude_native_continuation_runner as native


class CronContinuationRunnerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeContinuationRunnerTests.asyncSetUp
    prepare = native.NativeContinuationRunnerTests.prepare
    runner_patches = native.NativeContinuationRunnerTests.runner_patches
    complete = native.NativeContinuationRunnerTests.complete

    async def asyncTearDown(self):
        server.CLAUDE_TURN_ADMISSIONS.clear()
        server.CLAUDE_NATIVE_PAUSED_STARTS.clear()
        await native.NativeContinuationRunnerTests.asyncTearDown(self)

    async def prepare_human(self):
        await self.prepare()
        self.human_turn = {
            "run_id": "run_human", "backend": "claude", "prompt": "Exact human follow-up",
            "purpose": None, "provider_context_mode": "chat", "file_ids": ["human-file"],
            "_claude_native_pause_allowed": True,
            "provider_cross_chat_route_snapshot": [{"preserved_effect": "effect-1"}],
        }
        server.CURRENT_TURNS["chat-claude"] = self.human_turn
        server.BUSY_SESSIONS.add("chat-claude")
        path = await server.issue_cross_chat_capability(
            "chat-claude", "run_human", [], source_user_instruction="Exact human follow-up",
            source_is_user_turn=True, actions={"publish"},
        )
        await server.provider_authority_runtime_env("run_human", path, "chat-claude", [])
        self.human_authority = next(record for record in server.CROSS_CHAT_CAPABILITIES.values()
                                    if record["source_run_id"] == "run_human")
        self.human_authority["team_mail_send_count"] = 7
        server.RUN_METADATA["run_human"] = {"cross_chat_direct_message_ids": ["already-admitted-effect"]}

    def human_runner(self):
        return server.run_claude_sdk(
            "chat-claude", "run_human", "Exact human follow-up", self.session,
            self.context["manifest_path"],
        )

    def human_patches(self):
        stack = self.runner_patches()
        stack.enter_context(patch.object(server, "build_claude_sdk_options", return_value=self.context["options"]))
        stack.enter_context(patch.object(server, "claude_sdk_manager", AsyncMock(return_value=self.manager)))
        stack.enter_context(patch.object(server, "issue_cross_chat_capability", AsyncMock(side_effect=AssertionError("must preserve accepted authority"))))
        return stack

    def receipt(self, index=1):
        return replace(
            native.NativeContinuationRunnerTests.receipt(
                self, f"run_cron{index}", "run_claude",
            ),
            wake_kind="schedule", schedule_id="native-schedule-1",
            schedule_ids=("native-schedule-1",),
            native_wake_id=f"native-scheduled-user-{index}",
            task_id="", task_ids=(), task_type="", tool_use_id="cron-create-tool",
        )

    async def test_recurring_cron_keeps_creation_authority_across_human_turns(self):
        await self.prepare()
        with self.runner_patches():
            for index in (1, 2):
                receipt = self.receipt(index)
                self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
                self.assertEqual(self.manager.start_calls, [])
                self.assertEqual(server.ACTIVE["chat-claude"]["run_id"], receipt.run_id)
                self.assertIn("chat-claude", server.BUSY_SESSIONS)
                current = server.CURRENT_TURNS["chat-claude"]
                self.assertEqual(current["prompt"], "Original explicit instruction")
                self.assertEqual(current["parent_run_id"], "run_claude")
                path, runtime = await server.provider_tool_capability_snapshot(
                    "chat-claude", receipt.run_id, backend="claude",
                    claude_owner_token=self.manager.owner_token,
                )
                self.assertEqual(runtime["AGENTSDOCK_PROVIDER_AUTHORITY_FILE"], str(path))
                self.assertNotEqual(json.loads(path.read_text())["provider_capability"], self.old_token)
                self.assertIs(next(iter(server.CROSS_CHAT_CAPABILITIES.values())), self.authority)
                self.assertEqual(self.authority["source_user_instruction"],
                                 "Publish the completed report and read my mail")
                self.assertEqual(self.authority["team_mail_send_count"], index + 1)
                self.authority["team_mail_send_count"] += 1
                with patch.object(server, "take_chat_mailbox_hint", return_value="cron mail") as hint:
                    self.assertEqual(receipt.handle.pending_mail_hint(), "cron mail")
                    hint.assert_called_once_with("chat-claude", receipt.run_id)
                await self.complete(receipt)
                self.assertNotIn("chat-claude", server.ACTIVE)
                self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
                self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
                if index == 1:
                    # A later human's distinct authority must neither replace
                    # the recurring schedule's origin nor add grants to it.
                    await server.issue_cross_chat_capability(
                        "chat-claude", "run_human", [],
                        source_user_instruction="A separate human follow-up",
                        source_is_user_turn=True, actions={"publish"},
                    )
                    self.assertIsNot(next(iter(server.CROSS_CHAT_CAPABILITIES.values())), self.authority)
                    await server.revoke_cross_chat_capability("run_human")
            starts = [call.args[2] for call in self.append_durable_event.await_args_list
                      if call.args[1] == "turn_started"]
            self.assertEqual(len(starts), 2)
            for index, row in enumerate(starts, 1):
                self.assertEqual(row["purpose"], "provider_background_wake")
                self.assertEqual(row["provider_wake_kind"], "schedule")
                self.assertEqual(row["provider_schedule_id"], "native-schedule-1")
                self.assertEqual(row["provider_schedule_ids"], ["native-schedule-1"])
                self.assertEqual(row["provider_native_wake_id"], f"native-scheduled-user-{index}")
                self.assertEqual(row["provider_tool_use_id"], "cron-create-tool")
                self.assertEqual(row["parent_run_ids"], ["run_claude"])
                self.assertEqual(row["prompt"], "")
                self.assertTrue(row["metadata_only"])
                self.assertTrue(row["provider_generated"])
                for field in ("provider_task_id", "provider_task_ids", "provider_task_type"):
                    self.assertNotIn(field, row)
            texts = [call.args[2] for call in self.events.await_args_list
                     if call.args[1] == "assistant_text"]
            self.assertEqual([row["provider_message_id"] for row in texts],
                             ["run_cron1-native-message", "run_cron2-native-message"])

    async def test_identical_registered_prompts_preserve_all_origins_without_claiming_one_schedule(self):
        await self.prepare()
        with self.runner_patches():
            receipt = replace(
                self.receipt(), schedule_id=None,
                schedule_ids=("older-native-schedule", "native-schedule-1"),
                parent_run_ids=("run_older", "run_claude"),
            )
            self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
            row = self.append_durable_event.await_args.args[2]
            self.assertEqual(row["provider_schedule_ids"], ["older-native-schedule", "native-schedule-1"])
            self.assertNotIn("provider_schedule_id", row)
            self.assertEqual(row["parent_run_ids"], ["run_older", "run_claude"])
            self.assertEqual(row["parent_run_id"], "run_claude")
            self.assertIs(next(iter(server.CROSS_CHAT_CAPABILITIES.values())), self.authority)
            self.assertEqual(set(self.authority["actions"]), {"publish", "jobs"})
            self.assertEqual(self.authority["team_mail_send_count"], 2)
            await self.complete(receipt)

    async def test_idle_stop_preserves_future_schedule_without_running_native_work(self):
        await self.prepare()
        self.manager.has_pending_native_continuations = lambda _chat: False
        with patch.object(server, "build_claude_subagent_snapshot", return_value={"active_count": 0, "subagents": []}), \
                patch.object(server, "cancel_codex_interactions", AsyncMock()), \
                patch.object(server, "cancel_claude_interactions", AsyncMock()):
            result = await server.stop_turn(
                "chat-claude", emit_event=False, schedule_queue=False,
                pause_queued_turns_on_stop=False,
            )
        self.assertEqual(self.manager.evict_calls, [])
        self.assertTrue(self.manager.loaded)
        self.assertEqual(result["subagents"]["descendants"], 0)
        self.assertFalse(any(call.args[1] == "claude_subagents_stopped"
                             for call in self.append_durable_event.await_args_list))

    async def test_stopped_creator_preserves_registered_cron_but_not_background_task_authority(self):
        await self.prepare()
        server.STOPPED_RUNS.add("run_claude")
        try:
            with self.runner_patches():
                task = native.NativeContinuationRunnerTests.receipt(self, "run_task", "run_claude")
                self.assertFalse(await server.admit_claude_native_continuation(task, self.context))
                self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
                cron = self.receipt()
                self.assertTrue(await server.admit_claude_native_continuation(cron, self.context))
                self.assertEqual(self.manager.start_calls, [])
                self.assertIs(next(iter(server.CROSS_CHAT_CAPABILITIES.values())), self.authority)
                await self.complete(cron)
        finally:
            server.STOPPED_RUNS.discard("run_claude")

    async def test_native_wake_pauses_original_human_start_and_sends_it_once_after_completion(self):
        await self.prepare_human()
        entered, release = asyncio.Event(), asyncio.Event()
        real_start = self.manager.start_run
        attempts = 0
        self.manager.handle.messages.put_nowait({"type": "AssistantMessage", "uuid": "human-answer",
                                               "content": [{"type": "text", "text": "Human completed"}]})
        self.manager.handle.messages.put_nowait({"type": "ResultMessage", "result": "Human completed", "session_id": "provider"})

        async def start(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                raise ClaudeSDKNativeWakeDeferred(blocking_run_id="run_cron1", native_wake_id="native-scheduled-user-1")
            self.assertIs(server.CURRENT_TURNS["chat-claude"], self.human_turn)
            self.assertEqual(server.RUN_METADATA["run_human"]["cross_chat_direct_message_ids"], ["already-admitted-effect"])
            self.assertEqual(self.human_authority["team_mail_send_count"], 7)
            self.assertIs(next(record for record in server.CROSS_CHAT_CAPABILITIES.values()
                               if record["source_run_id"] == "run_human"), self.human_authority)
            return await real_start(*args, **kwargs)

        self.manager.start_run = start
        with self.human_patches():
            human = asyncio.create_task(self.human_runner())
            await asyncio.wait_for(entered.wait(), 2)
            original_active = server.ACTIVE["chat-claude"]
            receipt = self.receipt()
            self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
            self.assertEqual(server.ACTIVE["chat-claude"]["run_id"], receipt.run_id)
            self.assertIs(server.CLAUDE_NATIVE_PAUSED_STARTS["chat-claude"]["active"], original_active)
            release.set()
            await asyncio.sleep(0)
            self.assertFalse(human.done())
            self.assertEqual(self.manager.start_calls, [])
            await self.complete(receipt)
            await asyncio.wait_for(human, 2)
            self.assertEqual(len(self.manager.start_calls), 1)
            self.assertEqual(self.manager.start_calls[0][:3], ("chat-claude", "Exact human follow-up", "run_human"))
            self.assertEqual(self.manager.evict_calls, [])
            self.assertNotIn("chat-claude", server.CLAUDE_NATIVE_PAUSED_STARTS)
            self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
            human_text = [call.args[2] for call in self.events.await_args_list
                          if call.args[1] == "assistant_text" and call.args[2].get("run_id") == "run_human"]
            self.assertEqual(human_text[0]["provider_admission_after_run_id"], "run_cron1")
            human_final = [call.args[1] for call in self.finished.await_args_list
                           if call.args[1].get("run_id") == "run_human"]
            self.assertEqual(human_final[0]["provider_admission_after_run_id"], "run_cron1")

    async def test_resumed_execution_marker_is_inherited_by_tool_and_reasoning_events(self):
        server.RUN_METADATA["run_human"] = {"provider_admission_after_run_id": "run_cron1"}
        try:
            for event in ({"run_id": "run_human", "tool": {"id": "tool"}},
                          {"run_id": "run_human", "text": "reasoning"},
                          {"run_id": "run_human", "output": "tool output"}):
                projected = server.inherit_internal_status_run_metadata(event)
                self.assertEqual(projected["provider_admission_after_run_id"], "run_cron1")
                self.assertNotIn("provider_admission_after_run_id", event)
        finally:
            server.RUN_METADATA.pop("run_human", None)

    async def test_stop_and_delete_settle_paused_human_without_submitting_it(self):
        for conflict in ("stop", "delete"):
            with self.subTest(conflict=conflict):
                await self.prepare_human()
                with self.human_patches():
                    receipt = self.receipt()
                    self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
                    human = asyncio.create_task(self.human_runner())
                    await asyncio.sleep(0)
                    self.assertFalse(human.done())
                    if conflict == "stop":
                        server.STOPPED_RUNS.add(receipt.run_id)
                    else:
                        server.DELETING_SESSIONS.add("chat-claude")
                    try:
                        await self.complete(receipt)
                        await asyncio.wait_for(human, 2)
                    finally:
                        server.STOPPED_RUNS.discard(receipt.run_id)
                        server.DELETING_SESSIONS.discard("chat-claude")
                    self.assertEqual(self.manager.start_calls, [])
                    self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
                    self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
                    self.assertNotIn("chat-claude", server.CLAUDE_NATIVE_PAUSED_STARTS)
                    self.assertNotIn(("chat-claude", "run_human"), server.CLAUDE_START_TASKS)
                    stopped = [call.args[2] for call in self.events.await_args_list
                               if call.args[1] == "turn_stopped" and call.args[2].get("run_id") == "run_human"]
                    self.assertEqual(len(stopped), 1)
                    self.assertFalse(stopped[0]["provider_query_submitted"])

    async def test_command_validation_during_admission_uses_exact_cached_native_inventory(self):
        await self.prepare()
        server.CLAUDE_TURN_ADMISSIONS["chat-claude"] = asyncio.Event()
        self.manager.get_cached_server_info = Mock(return_value=({"commands": []}, "native-generation"))
        self.manager.get_server_info = AsyncMock(side_effect=AssertionError("would wait on native admission actor"))
        with patch.object(server, "claude_sdk_manager", AsyncMock(return_value=self.manager)), \
                patch.object(server, "build_claude_sdk_options", return_value=self.context["options"]), \
                patch.object(server, "provider_command_selector_secret", return_value="a" * 64):
            snapshot, _inventory = await asyncio.wait_for(
                server.discover_session_provider_commands("chat-claude", self.session), 2,
            )
        self.assertTrue(snapshot["support"]["available"])
        self.manager.get_cached_server_info.assert_called_once_with(
            "chat-claude", configuration_key=self.context["options"][1],
        )
        self.manager.get_server_info.assert_not_called()

    async def test_explicit_stop_finishes_native_and_paused_human_without_retiring_connection(self):
        await self.prepare_human()
        with self.human_patches():
            receipt = self.receipt()
            self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
            human = asyncio.create_task(self.human_runner())

            async def interrupt():
                receipt.handle.messages.put_nowait({
                    "type": "ResultMessage", "result": "", "session_id": "provider", "aborted": True,
                })
                return True

            receipt.handle.interrupt = AsyncMock(side_effect=interrupt)
            result = await asyncio.wait_for(server.stop_turn(
                "chat-claude", emit_event=False, schedule_queue=False,
                pause_queued_turns_on_stop=False, cascade_claude_subagents=False,
            ), 2)
            await asyncio.wait_for(human, 2)
            self.assertTrue(result["stopped"])
            self.assertFalse(result["pending"])
            receipt.handle.interrupt.assert_awaited_once()
            self.assertEqual(self.manager.start_calls, [])
            self.assertEqual(self.manager.evict_calls, [])
            self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
            self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
            self.assertNotIn("chat-claude", server.CLAUDE_NATIVE_PAUSED_STARTS)
            self.assertNotIn(("chat-claude", "run_human"), server.CLAUDE_START_TASKS)

    async def test_cancelled_paused_human_is_cleaned_before_and_after_slot_restoration(self):
        for phase in ("before_restore", "after_restore", "after_ready"):
            with self.subTest(phase=phase):
                await self.prepare_human()
                with self.human_patches():
                    receipt = self.receipt()
                    self.assertTrue(await server.admit_claude_native_continuation(receipt, self.context))
                    human = asyncio.create_task(self.human_runner())
                    await asyncio.sleep(0)
                    paused = server.CLAUDE_NATIVE_PAUSED_STARTS["chat-claude"]
                    complete = self.complete
                    if phase == "before_restore":
                        human.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await human
                    elif phase == "after_restore":
                        async def finish_after_restore(_session_id, payload):
                            self.assertTrue(paused["restored"])
                            self.assertFalse(paused["ready"].is_set())
                            human.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await human
                            await server.revoke_cross_chat_capability(payload["run_id"])
                            return payload
                        self.finished.side_effect = finish_after_restore
                    else:
                        original_finish = server.finish_claude_paused_start
                        async def finish_then_cancel(session_id, native_run_id):
                            await original_finish(session_id, native_run_id)
                            if paused.get("restored"):
                                self.assertTrue(paused["ready"].is_set())
                                human.cancel()
                        with patch.object(server, "finish_claude_paused_start", side_effect=finish_then_cancel):
                            await complete(receipt)
                    if phase != "after_ready":
                        await complete(receipt)
                    if not human.done():
                        with self.assertRaises(asyncio.CancelledError):
                            await human
                    self.assertTrue(human.cancelled())
                    await asyncio.sleep(0)
                    if paused.get("cleanup_task") is not None:
                        await asyncio.wait_for(paused["cleanup_task"], 2)
                    self.assertNotIn("chat-claude", server.CLAUDE_NATIVE_PAUSED_STARTS)
                    self.assertNotIn(("chat-claude", "run_human"), server.CLAUDE_START_TASKS)
                    self.assertNotIn("chat-claude", server.BUSY_SESSIONS)
                    self.assertNotIn("chat-claude", server.CURRENT_TURNS)
                    self.assertNotIn("chat-claude", server.ACTIVE)
                    self.assertEqual(server.CROSS_CHAT_CAPABILITIES, {})
                    self.assertEqual(self.manager.start_calls, [])
                    self.assertEqual(self.manager.evict_calls, [])
                    stopped = [call.args[2] for call in self.events.await_args_list
                               if call.args[1] == "turn_stopped" and call.args[2].get("run_id") == "run_human"]
                    self.assertEqual(len(stopped), 1)

    async def test_missing_live_command_cache_does_not_wait_on_admitting_actor(self):
        await self.prepare()
        server.CLAUDE_TURN_ADMISSIONS["chat-claude"] = asyncio.Event()
        self.manager.get_cached_server_info = Mock(return_value=None)
        self.manager.get_server_info = AsyncMock(side_effect=AssertionError("would wait on native admission actor"))
        with patch.object(server, "claude_sdk_manager", AsyncMock(return_value=self.manager)), \
                patch.object(server, "build_claude_sdk_options", return_value=self.context["options"]), \
                patch.object(server, "provider_command_selector_secret", return_value="a" * 64):
            snapshot, _inventory = await asyncio.wait_for(
                server.discover_session_provider_commands("chat-claude", self.session), 2,
            )
        self.assertFalse(snapshot["support"]["available"])
        self.manager.get_server_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
