"""Native side-question leases; no server import, process, or provider I/O."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from claude_sdk_client import (
    ClaudeSDKConfigurationConflict,
    ClaudeSDKGenerationChanged,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
)
from side_questions import SideQuestionError
from test_claude_sdk_client import FakeClaudeClient, FakeFactory


class ClaudeSDKSideQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.factory = FakeFactory()
        self.manager = ClaudeSDKSupervisorManager(
            client_factory=self.factory, idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.1,
        )
        self.options = {"resume": "existing-provider-session"}
        self.tasks: list[asyncio.Task] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.manager.close_all()

    def ask(self, **kwargs) -> asyncio.Task:
        task = asyncio.create_task(self.manager.ask_side_question(
            "chat", "Why?", options=self.options,
            configuration_key="profile", **kwargs,
        ))
        self.tasks.append(task)
        return task

    async def start_main(self):
        handle = await asyncio.wait_for(self.manager.start_run(
            "chat", "Main task", run_id="main", options=self.options,
            configuration_key="profile",
        ), 1)
        await asyncio.wait_for(handle.wait_acknowledged(), 1)
        return handle

    async def test_cold_resume_uses_exact_client_without_main_turn(self) -> None:
        history = [{"question": "Before?", "response": "Previous answer"}]
        answer = {"answer": "Native context", "context_note": "Native"}

        async def native(client, question, **kwargs):
            self.assertIs(client, self.factory.clients[0])
            self.assertTrue(client.connected)
            self.assertEqual(client.options["resume"], "existing-provider-session")
            self.assertEqual(question, "Why?")
            self.assertEqual(kwargs, {"history": history, "timeout_seconds": 4})
            self.assertEqual(self.manager._pins, {"chat": 1})
            return answer

        with patch("claude_side_question.ask_native_side_question", native):
            self.assertEqual(await self.ask(history=history, timeout_seconds=4,
                expected_provider_id="existing-provider-session"), answer)
        client = self.factory.clients[0]
        self.assertEqual([call[0] for call in client.calls], ["connect", "receive_messages"])
        self.assertFalse(self.manager._supervisors["chat"].is_active)
        self.assertEqual(self.manager._pins, {})

    async def test_pending_side_answer_does_not_block_main_start_messages_or_stop(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def native(*args, **kwargs):
            started.set()
            await release.wait()
            return {"answer": "Side answer"}

        with patch("claude_side_question.ask_native_side_question", native):
            side = self.ask()
            await asyncio.wait_for(started.wait(), 1)
            main = await self.start_main()
            client = self.factory.clients[0]
            message = {"type": "assistant", "content": [{"type": "text", "text": "Main progress"}]}
            await client.emit(message)
            self.assertEqual(await asyncio.wait_for(main.__anext__(), 1), message)
            self.assertTrue(await asyncio.wait_for(main.interrupt(), 1))
            self.assertFalse(side.done())
            self.assertFalse(client.disconnected)
            release.set()
            self.assertEqual(await side, {"answer": "Side answer"})
        self.assertEqual([call[0] for call in client.calls].count("query"), 1)
        self.assertEqual([call[0] for call in client.calls].count("interrupt"), 1)
        self.assertEqual(self.manager._pins, {})

    async def test_cancel_and_provider_error_leave_active_main_unchanged(self) -> None:
        main = await self.start_main()
        client = self.factory.clients[0]
        original_calls = list(client.calls)
        started = asyncio.Event()

        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch("claude_side_question.ask_native_side_question", pending):
            side = self.ask()
            await asyncio.wait_for(started.wait(), 1)
            side.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await side
        self.assertEqual(self.manager._pins, {})
        self.assertFalse(main.done)
        self.assertEqual(client.calls, original_calls)
        with patch("claude_side_question.ask_native_side_question", side_effect=SideQuestionError(503, "Unavailable")):
            with self.assertRaises(SideQuestionError):
                await self.ask()
        self.assertEqual(client.calls, original_calls)
        self.assertEqual(self.manager._supervisors["chat"].active_run_id, "main")
        self.assertEqual(self.manager._pins, {})

    async def test_saved_effort_change_uses_live_parent_without_reconfiguration(self) -> None:
        self.options["effort"] = "ultracode"
        main = await self.start_main()
        client = self.factory.clients[0]
        supervisor = self.manager._supervisors["chat"]
        original_calls = list(client.calls)
        saved_options = {**self.options, "effort": "max"}

        with patch("claude_side_question.ask_native_side_question",
                   return_value={"answer": "Live context"}) as native:
            answer = await self.manager.ask_side_question(
                "chat", "Status?", options=saved_options,
                configuration_key="saved-max-profile",
                expected_provider_id="existing-provider-session",
            )
            self.assertEqual(answer, {"answer": "Live context"})
            self.assertIs(native.call_args.args[0], client)

        self.assertFalse(main.done)
        self.assertEqual(client.calls, original_calls)
        self.assertEqual(len(self.factory.clients), 1)
        self.assertIs(self.manager._supervisors["chat"], supervisor)
        self.assertEqual(supervisor.configuration_key, "profile")
        self.assertEqual(client.options["effort"], "ultracode")
        self.assertEqual(self.manager._pins, {})
        with self.assertRaises(ClaudeSDKConfigurationConflict):
            await self.manager.get_mcp_status(
                "chat", options=saved_options,
                configuration_key="saved-max-profile",
            )

    async def test_lease_blocks_idle_eviction_and_configuration_replacement(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def native(*args, **kwargs):
            started.set()
            await release.wait()
            return {"answer": "Done"}

        with patch("claude_side_question.ask_native_side_question", native):
            side = self.ask()
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(await self.manager.evict("chat"))
            with self.assertRaises(ClaudeSDKConfigurationConflict):
                await self.manager.get("chat", options={}, configuration_key="different")
            self.assertEqual(len(self.factory.clients), 1)
            release.set()
            await side
        self.assertTrue(await self.manager.evict("chat"))

    async def test_retired_side_reply_is_rejected_and_cannot_unpin_replacement(self) -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        old_cancelled = asyncio.Event()
        allow_old_cleanup = asyncio.Event()
        finish_new = asyncio.Event()

        async def native(client, *args, **kwargs):
            if client is self.factory.clients[0]:
                first_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    old_cancelled.set()
                    await allow_old_cleanup.wait()
                    return {"answer": "Stale"}
            second_started.set()
            await finish_new.wait()
            return {"answer": "Replacement"}

        with patch("claude_side_question.ask_native_side_question", native):
            old = self.ask()
            await asyncio.wait_for(first_started.wait(), 1)
            self.assertTrue(await self.manager.evict("chat", force=True))
            await asyncio.wait_for(old_cancelled.wait(), 1)
            new = self.ask()
            await asyncio.wait_for(second_started.wait(), 1)
            allow_old_cleanup.set()
            with self.assertRaises(ClaudeSDKGenerationChanged):
                await old
            self.assertEqual(self.manager._pins, {"chat": 1})
            self.assertFalse(self.factory.clients[1].disconnected)
            finish_new.set()
            self.assertEqual(await new, {"answer": "Replacement"})
        self.assertEqual(self.manager._pins, {})

    async def test_cancel_during_cold_connect_leaves_connection_for_waiting_main(self) -> None:
        connecting = asyncio.Event()
        connected = asyncio.Event()
        factory = self.factory

        class SlowClient(FakeClaudeClient):
            async def connect(self):
                connecting.set()
                await connected.wait()
                await super().connect()

        def make_client(options):
            client = SlowClient(options)
            factory.clients.append(client)
            return client

        self.manager._client_factory = make_client
        with patch("claude_side_question.ask_native_side_question") as native:
            side = self.ask()
            await asyncio.wait_for(connecting.wait(), 1)
            side.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await side
            self.assertEqual(self.manager._pins, {})
            connected.set()
            main = await self.start_main()
            self.assertFalse(main.done)
            native.assert_not_called()
        self.assertEqual(len(factory.clients), 1)
        self.assertFalse(factory.clients[0].disconnected)

    async def test_cold_connection_failure_unpins_without_retiring_supervisor(self) -> None:
        self.factory.connect_error = RuntimeError("Synthetic connect failure")
        with patch("claude_side_question.ask_native_side_question") as native:
            with self.assertRaises(ClaudeSDKUnavailable):
                await self.ask()
            native.assert_not_called()
        self.assertEqual(self.manager._pins, {})
        self.assertFalse(self.manager._supervisors["chat"].closed)
        self.factory.connect_error = None
        await self.start_main()
        self.assertEqual(len(self.factory.clients), 2)

    async def test_cold_resume_rejects_missing_or_different_native_parent(self) -> None:
        for resume in (None, "", "different-provider"):
            with self.subTest(resume=resume):
                self.options = {"resume": resume}
                with patch("claude_side_question.ask_native_side_question") as native:
                    with self.assertRaises(ClaudeSDKUnavailable):
                        await self.ask(expected_provider_id="existing-provider-session")
                    native.assert_not_called()
                self.assertEqual(self.factory.clients, [])
                self.assertEqual(self.manager._pins, {})

    async def test_connected_main_without_resume_option_retains_its_native_context(self) -> None:
        self.options = {"resume": None}
        main = await self.start_main()
        original_calls = list(self.factory.clients[0].calls)
        with patch("claude_side_question.ask_native_side_question", return_value={"answer": "Live context"}) as native:
            self.assertEqual(await self.ask(expected_provider_id="now-bound-provider"),
                             {"answer": "Live context"})
            self.assertIs(native.call_args.args[0], self.factory.clients[0])
        self.assertFalse(main.done)
        self.assertEqual(self.factory.clients[0].calls, original_calls)

    async def test_side_control_progress_never_enters_main_run_projection(self) -> None:
        main = await self.start_main()
        client = self.factory.clients[0]
        side_id = "agentsdock_side_" + "a" * 32
        await client.emit({"type": "system", "subtype": "control_request_progress",
                           "request_id": side_id, "status": "started"})
        await client.emit(SimpleNamespace(subtype="control_request_progress", data={
            "request_id": side_id, "status": "api_retry"}))
        other_control = {"type": "system", "subtype": "control_request_progress",
                         "request_id": "main-control", "status": "started"}
        await client.emit(other_control)
        main_text = {"type": "assistant", "content": [{"type": "text", "text": "Main answer"}]}
        await client.emit(main_text)
        result = {"type": "result", "subtype": "success", "result": "Main answer"}
        await client.emit(result)
        projected = await asyncio.wait_for(self.collect(main), 1)
        self.assertEqual(projected, [other_control, main_text, result])
        self.assertEqual([call[0] for call in client.calls].count("query"), 1)

    @staticmethod
    async def collect(handle):
        return [message async for message in handle]


if __name__ == "__main__":
    unittest.main()
