import unittest
from unittest.mock import AsyncMock, patch

import agent_server
from agent_server import (
    HandoffDigestSendRequest,
    queued_turn_from_event,
    run_handoff_digest_send,
)


class DigestQueuePersistenceTests(unittest.TestCase):
    def test_digest_queue_event_round_trips_hidden_request_metadata(self) -> None:
        item = queued_turn_from_event(
            {
                "queued_id": "queued-digest",
                "request_prompt": "Internal full digest instruction",
                "prompt": "Generate a handoff digest for Target.",
                "display_prompt": "Generate a handoff digest for Target.",
                "backend": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "purpose": "handoff_digest",
                "digest_job_id": "digest-1",
                "source_session_id": "source-1",
                "target_session_id": "target-1",
                "position": 3,
                "ts": "2026-07-13T10:00:00Z",
            },
            {"backend": "claude", "model": "sonnet", "effort": "high"},
            1,
        )

        self.assertEqual(item["prompt"], "Internal full digest instruction")
        self.assertEqual(item["display_prompt"], "Generate a handoff digest for Target.")
        self.assertEqual(item["purpose"], "handoff_digest")
        self.assertEqual(item["digest_job_id"], "digest-1")
        self.assertEqual(item["source_session_id"], "source-1")
        self.assertEqual(item["target_session_id"], "target-1")
        self.assertEqual(item["model"], "gpt-5.6-sol")
        self.assertEqual(item["effort"], "xhigh")


class DigestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_generation_and_target_delivery_use_distinct_typed_turns(self) -> None:
        previous_sessions = agent_server.STORE.sessions
        agent_server.STORE.sessions = {
            "source-1": {
                "id": "source-1", "title": "Source Chat", "backend": "codex",
                "cwd": "/tmp/source", "latest_event_seq": 11,
            },
            "target-1": {
                "id": "target-1", "title": "Target Chat", "backend": "claude",
                "cwd": "/tmp/target",
            },
        }
        request = HandoffDigestSendRequest(
            target_session_id="target-1",
            detail="normal",
            user_prompt="Keep the exact launch command.",
        )
        digest = "# ZenithDock Context Digest\n\n## Executive Summary\nReady."

        try:
            with patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start_turn, \
                    patch.object(agent_server, "wait_for_digest_turn_result", new_callable=AsyncMock) as wait_for_result, \
                    patch.object(agent_server, "append_event", new_callable=AsyncMock) as append_event:
                start_turn.side_effect = [
                    {"run_id": "source-run", "queued": False},
                    {"queued_id": "target-queued", "queued": True},
                ]
                wait_for_result.return_value = digest

                await run_handoff_digest_send("digest-1", "source-1", request)

            self.assertEqual(start_turn.await_count, 2)
            source_call = start_turn.await_args_list[0]
            self.assertEqual(source_call.args[0], "source-1")
            self.assertEqual(source_call.args[1].purpose, "handoff_digest")
            self.assertEqual(source_call.args[1].digest_job_id, "digest-1")
            self.assertEqual(source_call.args[1].target_session_id, "target-1")

            target_call = start_turn.await_args_list[1]
            target_request = target_call.args[1]
            self.assertEqual(target_call.args[0], "target-1")
            self.assertEqual(target_request.prompt, digest)
            self.assertEqual(target_request.display_prompt, "Context digest from Source Chat.")
            self.assertEqual(target_request.purpose, "handoff_digest_delivery")
            self.assertEqual(target_request.digest_job_id, "digest-1")
            self.assertEqual(target_request.source_session_id, "source-1")
            self.assertEqual(target_request.target_session_id, "target-1")

            received = next(
                call for call in append_event.await_args_list
                if call.args[0] == "target-1" and call.args[1] == "handoff_digest_received"
            )
            self.assertEqual(received.args[2]["digest"], digest)
            self.assertEqual(received.args[2]["source_session_id"], "source-1")
            self.assertTrue(any(
                call.args[0] == "source-1" and call.args[1] == "handoff_digest_sent"
                for call in append_event.await_args_list
            ))
        finally:
            agent_server.STORE.sessions = previous_sessions


if __name__ == "__main__":
    unittest.main()
