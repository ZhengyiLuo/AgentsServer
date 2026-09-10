"""Real transport framing with in-memory provider I/O; no server import."""

import asyncio
import unittest

from codex_app_server import CodexAppServerManager, CodexAppServerProtocolError
from test_codex_app_server import FakeProcessFactory, wait_until


class GoalSteerTransportTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self):
        factory = FakeProcessFactory()
        manager = CodexAppServerManager(
            "not-a-real-provider", cwd="/tmp",
            env_factory=lambda: {}, process_factory=factory, request_timeout=1,
        )
        self.addAsyncCleanup(manager.close)
        return manager, factory.process

    async def test_goal_thread_subscription_preserves_ack_boundary_across_turns(self):
        manager, process = self.make_manager()
        subscription = manager.subscribe_thread("goal-thread")
        self.addCleanup(subscription.close)
        await manager.start()
        # Goals are resumed with goal/set, so there need not be a registered
        # CodexAppServerTurn. The exact thread subscription owns the watermark.
        self.assertIsNone(manager.active_turn("goal-thread"))

        def steer(message):
            turn_id = message["params"]["expectedTurnId"]
            process.feed({"method": "item/completed", "params": {
                "threadId": "goal-thread", "turnId": turn_id,
                "item": {"id": "before-" + turn_id, "type": "agentMessage",
                         "phase": "commentary", "text": "Before follow-up"},
            }})
            return {"turnId": turn_id}

        process.responders["turn/steer"] = steer
        last_watermark = 0
        for turn_id in ("goal-turn-1", "goal-turn-2"):
            user_input = [{"type": "text", "text": "Please keep working."}]
            result, watermark = await manager.steer_turn_with_notification_watermark(
                "goal-thread", turn_id, user_input,
                client_user_message_id="follow-up-" + turn_id,
                notification_subscription=subscription,
            )
            self.assertEqual(result, turn_id)
            self.assertGreater(watermark, last_watermark)
            sequence, event = await subscription.next_notification_with_sequence(timeout=1)
            self.assertEqual(sequence, watermark)
            self.assertEqual(event["params"]["item"]["id"], "before-" + turn_id)
            last_watermark = watermark
            self.assertEqual(process.messages[-1]["params"], {
                "threadId": "goal-thread", "expectedTurnId": turn_id,
                "input": user_input, "clientUserMessageId": "follow-up-" + turn_id,
            })
        methods = [item.get("method") for item in process.messages]
        self.assertEqual(methods.count("turn/steer"), 2)
        self.assertFalse(set(methods) & {"turn/start", "turn/interrupt", "thread/goal/set"})

    async def test_ack_for_other_turn_is_uncertain_and_is_not_replayed(self):
        manager, process = self.make_manager()
        subscription = manager.subscribe_thread("goal-thread")
        self.addCleanup(subscription.close)
        process.responders["turn/steer"] = lambda _: {"turnId": "different-turn"}
        with self.assertRaises(CodexAppServerProtocolError) as caught:
            await manager.steer_turn_with_notification_watermark(
                "goal-thread", "expected-turn", [{"type": "text", "text": "Follow-up"}],
                notification_subscription=subscription,
            )
        self.assertTrue(caught.exception.request_sent)
        self.assertFalse(caught.exception.safe_to_retry)
        self.assertEqual([item.get("method") for item in process.messages].count("turn/steer"), 1)

    async def test_missing_subscription_rejects_before_any_provider_request(self):
        manager, process = self.make_manager()
        with self.assertRaises(CodexAppServerProtocolError) as caught:
            await manager.steer_turn_with_notification_watermark(
                "goal-thread", "expected-turn", [{"type": "text", "text": "Follow-up"}],
            )
        self.assertFalse(caught.exception.request_sent)
        self.assertTrue(caught.exception.safe_to_retry)
        self.assertEqual(process.messages, [])

    async def test_pause_while_waiting_for_writer_prevents_any_steer_bytes(self):
        manager, process = self.make_manager()
        subscription = manager.subscribe_thread("goal-thread")
        self.addCleanup(subscription.close)
        await manager.start()
        live = {"status": "active"}
        checks = []

        def before_send():
            checks.append(live["status"])
            return live["status"] == "active"

        await manager.client._write_lock.acquire()
        task = asyncio.create_task(manager.steer_turn_with_notification_watermark(
            "goal-thread", "turn", [{"type": "text", "text": "Follow-up"}],
            notification_subscription=subscription, before_send=before_send,
        ))
        try:
            await wait_until(lambda: bool(manager.client._pending))
            self.assertEqual(checks, [])
            live["status"] = "paused"
        finally:
            manager.client._write_lock.release()
        with self.assertRaises(CodexAppServerProtocolError) as caught:
            await task
        self.assertFalse(caught.exception.request_sent)
        self.assertTrue(caught.exception.safe_to_retry)
        self.assertEqual(checks, ["paused"])
        self.assertNotIn("turn/steer", [message.get("method") for message in process.messages])
        self.assertEqual(manager.client._pending, {})
        self.assertTrue(manager.ready)

    async def test_allowed_guard_runs_once_at_the_wire_boundary(self):
        manager, process = self.make_manager()
        subscription = manager.subscribe_thread("goal-thread")
        self.addCleanup(subscription.close)
        seen = []

        def guard():
            seen.append("guard")
            return True

        def accepted(message):
            seen.append("write")
            return {"turnId": "turn"}

        process.responders["turn/steer"] = accepted
        self.assertEqual(await manager.steer_turn_with_notification_watermark(
            "goal-thread", "turn", [{"type": "text", "text": "Follow-up"}],
            notification_subscription=subscription, before_send=guard,
        ), ("turn", 0))
        self.assertEqual(seen, ["guard", "write"])
