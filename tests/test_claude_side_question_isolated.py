"""Native Claude control tests: synthetic SDK router only, no provider process."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import claude_side_question as side


class NativeClient:
    def __init__(self):
        self.frames = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.send_gate = None
        self.cancel_gate = None
        self._query = SimpleNamespace(
            is_streaming_mode=True,
            _initialized=True,
            _closed=False,
            pending_control_responses={},
            pending_control_results={},
            transport=SimpleNamespace(write=self.write),
        )
        self.query = AsyncMock(side_effect=AssertionError("main query forbidden"))
        self.interrupt = AsyncMock(side_effect=AssertionError("main interrupt forbidden"))
        self.disconnect = AsyncMock(side_effect=AssertionError("main disconnect forbidden"))

    async def write(self, value):
        frame = json.loads(value)
        if frame["type"] == "control_request":
            self.started.set()
            if self.send_gate is not None:
                await self.send_gate.wait()
            self.frames.append(frame)
        elif frame["type"] == "control_cancel_request":
            self.frames.append(frame)
            self.cancelled.set()
            if self.cancel_gate is not None:
                await self.cancel_gate.wait()
            self.respond(frame["request_id"], Exception("Side question cancelled"))
        else:
            raise AssertionError(frame)

    def respond(self, request_id, result):
        query = self._query
        if request_id in query.pending_control_responses:
            query.pending_control_results[request_id] = result
            query.pending_control_responses[request_id].set()

    async def next_request(self, index=0):
        for _ in range(100):
            requests = [frame for frame in self.frames if frame["type"] == "control_request"]
            if len(requests) > index:
                return requests[index]
            await asyncio.sleep(0)
        raise AssertionError("control request not delivered")


def answer(value="Native answer", **extra):
    return {"subtype": "success", "response": {"response": value, "synthetic": False, **extra}}


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owned_native_progress_is_filtered_including_late_cancel(self):
        owned = "agentsdock_side_" + "b" * 32
        packet = {"type": "system", "subtype": "control_request_progress",
                  "request_id": owned, "status": "started"}
        self.assertTrue(side.is_native_side_question_progress(packet))
        self.assertTrue(side.is_native_side_question_progress(SimpleNamespace(
            subtype="control_request_progress", data={**packet, "status": "api_retry"})))
        for value in ({**packet, "request_id": "req_main"},
                      {**packet, "request_id": owned + "suffix"},
                      {**packet, "subtype": "status"},
                      {**packet, "type": "assistant"},
                      SimpleNamespace(subtype="control_request_progress", data=None), None):
            self.assertFalse(side.is_native_side_question_progress(value))

    async def test_native_request_sends_no_snapshot_or_main_turn_and_preserves_history(self):
        client = NativeClient()
        history = [{"question": " earlier? ", "response": "🚀 reply\n"}]
        task = asyncio.create_task(side.ask_native_side_question(client, "Exact tool result?", history=history))
        request = await client.next_request()
        self.assertEqual(request["request"], {
            "subtype": "side_question", "question": "Exact tool result?",
            "history": [{"question": " earlier? ", "response": "🚀 reply\n"}],
        })
        history[0]["question"] = "mutated"
        client.respond(request["request_id"], answer("From completed tool context"))
        result = await task
        self.assertEqual(result["answer"], "From completed tool context")
        self.assertIn("tool results", result["context_note"])
        self.assertEqual(request["request"]["history"][0]["question"], " earlier? ")
        self.assertEqual(client._query.pending_control_responses, {})
        self.assertEqual(client._query.pending_control_results, {})
        client.query.assert_not_called()
        client.interrupt.assert_not_called()
        client.disconnect.assert_not_called()

    async def test_native_twenty_exchanges_are_not_limited_by_legacy_client_history(self):
        client = NativeClient()
        history = [{"question": f"Question {i}", "response": "a" * 4000} for i in range(20)]
        task = asyncio.create_task(side.ask_native_side_question(client, "Follow-up 21", history=history))
        request = await client.next_request()
        self.assertEqual(request["request"]["history"], history)
        client.respond(request["request_id"], answer())
        await task

    async def test_cancel_only_exact_request_and_leave_main_and_sibling_controls_running(self):
        client = NativeClient()
        main_event = asyncio.Event()
        client._query.pending_control_responses["main-control"] = main_event
        first = asyncio.create_task(side.ask_native_side_question(client, "Cancel this"))
        second = asyncio.create_task(side.ask_native_side_question(client, "Keep this"))
        request1 = await client.next_request(0)
        request2 = await client.next_request(1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        self.assertFalse(main_event.is_set())
        self.assertIn("main-control", client._query.pending_control_responses)
        self.assertEqual(client.frames[-1], {"type": "control_cancel_request", "request_id": request1["request_id"]})
        client.respond(request2["request_id"], answer("Sibling completed"))
        self.assertEqual((await second)["answer"], "Sibling completed")
        client.interrupt.assert_not_called()
        client.disconnect.assert_not_called()

    async def test_cancel_waits_for_request_write_before_sending_cancel(self):
        client = NativeClient()
        client.send_gate = asyncio.Event()
        task = asyncio.create_task(side.ask_native_side_question(client, "Cancel during write"))
        await client.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(client.frames, [])
        self.assertFalse(task.done())
        client.send_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([frame["type"] for frame in client.frames], ["control_request", "control_cancel_request"])
        self.assertEqual(client.frames[0]["request_id"], client.frames[1]["request_id"])

    async def test_repeat_cancellation_does_not_abandon_provider_cleanup(self):
        client = NativeClient()
        client.cancel_gate = asyncio.Event()
        task = asyncio.create_task(side.ask_native_side_question(client, "Cancel twice"))
        await client.next_request()
        task.cancel()
        await client.cancelled.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        client.cancel_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client._query.pending_control_responses, {})
        self.assertEqual(len(client.frames), 2)

    async def test_timeout_cancels_native_side_request_only(self):
        client = NativeClient()
        with self.assertRaises(side.SideQuestionError) as caught:
            await side.ask_native_side_question(client, "Timeout", timeout_seconds=0.01)
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual([frame["type"] for frame in client.frames], ["control_request", "control_cancel_request"])
        self.assertEqual(client._query.pending_control_responses, {})

    async def test_nonresponsive_cancel_is_bounded_and_does_not_disconnect_main(self):
        client = NativeClient()
        client.cancel_gate = asyncio.Event()
        task = asyncio.create_task(side.ask_native_side_question(client, "Cancel"))
        await client.next_request()
        with patch.object(side, "CANCEL_TIMEOUT_SECONDS", 0.01):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(client._query.pending_control_responses, {})
        client.disconnect.assert_not_called()

    async def test_bad_inputs_fail_before_provider_execution(self):
        for question, history in [("", None), ("q", [{"role": "user", "text": "unpaired"}]), ("\ud800", None)]:
            client = NativeClient()
            with self.assertRaises(side.SideQuestionError) as caught:
                await side.ask_native_side_question(client, question, history=history)
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(client.frames, [])

    async def test_unavailable_sdk_fails_without_normal_query_fallback(self):
        client = NativeClient()
        client._query._initialized = False
        with self.assertRaises(side.SideQuestionError) as caught:
            await side.ask_native_side_question(client, "q")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(client.frames, [])
        client.query.assert_not_called()

    async def test_invalid_empty_synthetic_and_oversized_answers_fail_closed(self):
        values = [answer(None), answer(""), answer("Tool failure", synthetic=True),
                  answer("a" * (side.MAX_OUTPUT_BYTES + 1)), answer("\ud800"), {},
                  Exception("secret raw provider error")]
        for value in values:
            client = NativeClient()
            task = asyncio.create_task(side.ask_native_side_question(client, "q"))
            request = await client.next_request()
            client.respond(request["request_id"], value)
            with self.assertRaises(side.SideQuestionError) as caught:
                await task
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(client._query.pending_control_results, {})
            self.assertEqual(len(client.frames), 1)

    async def test_write_failure_cleans_registration_without_parent_shutdown(self):
        client = NativeClient()
        client._query.transport.write = AsyncMock(side_effect=RuntimeError("secret transport detail"))
        with self.assertRaises(side.SideQuestionError) as caught:
            await side.ask_native_side_question(client, "q")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(client._query.pending_control_responses, {})
        client.disconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
