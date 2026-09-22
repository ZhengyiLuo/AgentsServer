"""Demand-driven shared-chat notifications with synthetic readers only."""
import asyncio
import threading
import unittest
from unittest.mock import Mock, AsyncMock

from interactive_chat_runtime import InteractiveChatLiveState
from public_chat_transcript import PublicTranscriptError


class InteractiveChatRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_snapshot_is_demand_driven_and_all_chat_control_signals_wake(self):
        native = AsyncMock(return_value={"session": {"id": "chat"}, "events": [], "queue": [], "active": False})
        state = InteractiveChatLiveState(None, lambda _: True, native_snapshot=native)
        state.notify("chat", {"type": "tool_started"})
        native.assert_not_called()
        before = await state.load("chat")
        self.assertEqual(before["session"], {"id": "chat"})
        self.assertNotIn("messages", before)
        for kind in ("tool_started", "reasoning_summary", "goal_updated", "job_updated", "interaction_required", "session_updated", "turn_queued"):
            revision = state.entries["chat"].revision
            state.notify("chat", {"type": kind})
            self.assertTrue(await state.wait("chat", revision, 0.01))
        native.assert_awaited_once_with("chat")

    async def test_native_snapshot_captures_revision_before_callback_and_no_reader_reset(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def native(_):
            entered.set()
            await release.wait()
            return {"session": {"id": "chat"}, "events": []}
        state = InteractiveChatLiveState(None, lambda _: True, native_snapshot=native)
        loading = asyncio.create_task(state.load("chat"))
        await entered.wait()
        previous_identity = state.entries["chat"].identity
        state.notify("chat", {"type": "history_reconciled"})
        self.assertNotEqual(state.entries["chat"].identity, previous_identity)
        release.set()
        result = await loading
        self.assertTrue(await state.wait("chat", result["revision"], 0.01))
        self.assertEqual((await state.load("chat"))["session"], {"id": "chat"})

    def state(self, reader=None, **kwargs):
        reader = reader or Mock(load=Mock(return_value={"messages": [{"role": "user", "text": "Question"}]}))
        factory = Mock(return_value=reader)
        return InteractiveChatLiveState(factory, lambda _sid: True,
                                        lambda _sid: {"queued": [], "busy": False}, **kwargs), reader, factory

    async def test_unopened_unrelated_and_tool_only_notifications_do_not_read_or_wake(self):
        state, reader, factory = self.state()
        state.notify("chat", {"type": "assistant_text"})
        factory.assert_not_called()
        snapshot = await state.load("chat")
        for event in ({"type": "tool_started"}, {"type": "tool_finished"},
                      {"type": "reasoning_summary", "phase": "summary"}, {"type": "file_uploaded"}):
            state.notify("chat", event)
        state.notify("other", {"type": "assistant_text"})
        self.assertFalse(await state.wait("chat", snapshot["revision"], 0.01))
        reader.load.assert_called_once()
        factory.assert_called_once_with("chat")

    async def test_same_chat_message_queue_and_delete_notifications_wake_without_io(self):
        state, reader, _factory = self.state()
        snapshot = await state.load("chat")
        for event in ({"type": "assistant_text"}, {"type": "turn_queued"},
                      {"type": "turn_queue_removed"}, {"type": "turn_unqueued"},
                      {"type": "reasoning_summary", "phase": "commentary"},
                      {"type": "session_deleted"}):
            revision = state.entries["chat"].revision
            waiting = asyncio.create_task(state.wait("chat", revision, 1))
            await asyncio.sleep(0)
            state.notify("chat", event)
            self.assertTrue(await asyncio.wait_for(waiting, 1))
        self.assertNotEqual(state.entries["chat"].revision, snapshot["revision"])
        reader.load.assert_called_once()

    async def test_notify_during_initial_read_closes_snapshot_to_wait_gap(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def read():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(2): raise AssertionError("test reader was not released")
            return {"messages": []}
        state, _reader, _factory = self.state(Mock(load=read))
        loading = asyncio.create_task(state.load("chat"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            state.notify("chat", {"type": "assistant_text"})
        finally:
            release.set()
        snapshot = await asyncio.wait_for(loading, 1)
        self.assertTrue(await state.wait("chat", snapshot["revision"], 0.01))

    async def test_repeated_cancellation_keeps_reader_thread_serialized(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        calls, active, peak = 0, 0, 0
        def read():
            nonlocal calls, active, peak
            calls += 1; active += 1; peak = max(peak, active)
            try:
                if calls == 1:
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(2): raise AssertionError("test reader was not released")
                return {"messages": []}
            finally:
                active -= 1
        state, _reader, _factory = self.state(Mock(load=read))
        first = asyncio.create_task(state.load("chat"))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            first.cancel(); await asyncio.sleep(0)
            first.cancel(); await asyncio.sleep(0)
            second = asyncio.create_task(state.load("chat"))
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertTrue(state.entries["chat"].lock.locked())
            self.assertEqual(calls, 1)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, 1)
        self.assertIsNotNone(second)
        await asyncio.wait_for(second, 1)
        self.assertEqual((calls, peak), (2, 1))

    async def test_waiters_pin_cache_and_eviction_changes_revision(self):
        state, _reader, factory = self.state(max_cached=1)
        original = await state.load("chat")
        waiting = asyncio.create_task(state.wait("chat", original["revision"], 1))
        await asyncio.sleep(0)
        try:
            with self.assertRaises(PublicTranscriptError):
                await state.load("other")
            self.assertEqual(factory.call_count, 1)
        finally:
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError): await waiting
        await state.load("other")
        self.assertTrue(await state.wait("chat", original["revision"], 0.01))
        restored = await state.load("chat")
        self.assertNotEqual(restored["revision"], original["revision"])

    async def test_deleted_chat_during_read_is_not_returned_and_reader_is_evicted(self):
        exists = True
        def read():
            nonlocal exists
            exists = False
            return {"messages": [{"role": "assistant", "text": "Do not return"}]}
        state, _reader, _factory = self.state(Mock(load=read))
        state.session_exists = lambda _sid: exists
        state.public_state = Mock(return_value={})
        with self.assertRaises(PublicTranscriptError): await state.load("chat")
        self.assertNotIn("chat", state.entries)
        state.public_state.assert_not_called()

    async def test_failed_reader_is_recreated_without_reusing_projector_state(self):
        state, _reader, _factory = self.state()
        failed = Mock(load=Mock(side_effect=PublicTranscriptError("changed")))
        healthy = Mock(load=Mock(return_value={"messages": []}))
        state.reader_factory = Mock(side_effect=[failed, healthy])
        with self.assertRaises(PublicTranscriptError): await state.load("chat")
        self.assertNotIn("chat", state.entries)
        self.assertEqual((await state.load("chat"))["messages"], [])
        self.assertEqual(state.reader_factory.call_count, 2)

    async def test_explicit_history_reconcile_rebuilds_stale_privacy_projection_on_demand(self):
        state, _reader, _factory = self.state()
        stale = Mock(load=Mock(return_value={"messages": [{"role": "user", "text": "Old unclassified provider input"}]}))
        repaired = Mock(load=Mock(return_value={"messages": []}))
        state.reader_factory = Mock(side_effect=[stale, repaired])
        before = await state.load("chat")
        state.notify("chat", {"type": "history_reconciled"})
        # Notification is O(1), including privacy changes. Re-read only when
        # this already-open viewer next asks for the corrected transcript.
        self.assertEqual(state.reader_factory.call_count, 1)
        after = await state.load("chat")
        self.assertEqual(after["messages"], [])
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual(state.reader_factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
