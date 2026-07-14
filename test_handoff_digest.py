import unittest
from unittest.mock import AsyncMock, patch

import agent_server
from agent_server import (
    HandoffDigestSendRequest,
    deliver_handoff_digest,
    finalize_handoff_digest_turn,
    finish_handoff_digest_queue_item,
    queued_turn_from_event,
    reconcile_handoff_digest_jobs,
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
                "digest_detail": "detailed",
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
        self.assertEqual(item["digest_detail"], "detailed")
        self.assertEqual(item["source_session_id"], "source-1")
        self.assertEqual(item["target_session_id"], "target-1")
        self.assertEqual(item["model"], "gpt-5.6-sol")
        self.assertEqual(item["effort"], "xhigh")


class DigestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_jobs = agent_server.HANDOFF_DIGEST_JOBS
        self.previous_finalizing = agent_server.HANDOFF_DIGEST_FINALIZING
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
        agent_server.HANDOFF_DIGEST_JOBS = {}
        agent_server.HANDOFF_DIGEST_FINALIZING = set()

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.HANDOFF_DIGEST_JOBS = self.previous_jobs
        agent_server.HANDOFF_DIGEST_FINALIZING = self.previous_finalizing

    def request(self) -> HandoffDigestSendRequest:
        return HandoffDigestSendRequest(
            target_session_id="target-1",
            detail="normal",
            user_prompt="Keep the exact launch command.",
        )

    def job(self) -> dict[str, object]:
        return {
            "id": "digest-1",
            "source_session_id": "source-1",
            "target_session_id": "target-1",
            "detail": "normal",
            "user_prompt": "Keep the exact launch command.",
            "status": "created",
        }

    async def test_submission_only_starts_or_queues_the_source_turn(self) -> None:
        agent_server.HANDOFF_DIGEST_JOBS["digest-1"] = self.job()
        with patch.object(agent_server, "start_turn_durably", new_callable=AsyncMock) as start_turn, \
                patch.object(agent_server, "update_handoff_digest_job", new_callable=AsyncMock) as update_job:
            start_turn.return_value = {"queued_id": "source-queued", "queued": True}

            result = await run_handoff_digest_send("digest-1", "source-1", self.request())

        self.assertTrue(result["queued"])
        start_turn.assert_awaited_once()
        source_call = start_turn.await_args
        self.assertEqual(source_call.args[0], "source-1")
        source_request = source_call.args[1]
        self.assertEqual(source_request.purpose, "handoff_digest")
        self.assertEqual(source_request.digest_job_id, "digest-1")
        self.assertEqual(source_request.digest_detail, "normal")
        self.assertEqual(source_request.target_session_id, "target-1")
        self.assertIn("Keep the exact launch command.", source_request.prompt)
        update_job.assert_awaited_once()
        self.assertEqual(update_job.await_args.args[1]["status"], "source_queued")

    async def test_source_completion_delivers_once_even_if_reconciled_twice(self) -> None:
        digest = "# ZenithDock Context Digest\n\n## Executive Summary\nReady."
        agent_server.HANDOFF_DIGEST_JOBS["digest-1"] = self.job()
        emitted: list[tuple[str, str, dict[str, object]]] = []
        delivery_state: list[str | None] = [None]

        async def fake_append(session_id: str, event_type: str, payload: dict[str, object]) -> dict[str, object]:
            emitted.append((session_id, event_type, payload))
            return {"session_id": session_id, "type": event_type, **payload}

        def fake_exists(session_id: str, digest_job_id: str, event_type: str) -> bool:
            return any(
                emitted_session == session_id
                and emitted_type == event_type
                and payload.get("digest_job_id") == digest_job_id
                for emitted_session, emitted_type, payload in emitted
            )

        async def fake_update(digest_job_id: str, values: dict[str, object]) -> dict[str, object]:
            current = dict(agent_server.HANDOFF_DIGEST_JOBS[digest_job_id])
            current.update(values)
            agent_server.HANDOFF_DIGEST_JOBS[digest_job_id] = current
            return current

        async def fake_start(session_id: str, request: object) -> dict[str, object]:
            self.assertEqual(session_id, "target-1")
            self.assertEqual(request.purpose, "handoff_digest_delivery")
            self.assertEqual(request.prompt, digest)
            delivery_state[0] = "queued"
            return {"queued_id": "target-queued", "queued": True}

        event = {
            "type": "turn_finished",
            "run_id": "source-run",
            "purpose": "handoff_digest",
            "digest_job_id": "digest-1",
            "digest_detail": "normal",
            "source_session_id": "source-1",
            "target_session_id": "target-1",
            "result_text": digest,
        }
        with patch.object(agent_server, "append_event", side_effect=fake_append), \
                patch.object(agent_server, "digest_event_exists", side_effect=fake_exists), \
                patch.object(agent_server, "update_handoff_digest_job", side_effect=fake_update), \
                patch.object(agent_server, "start_turn_durably", side_effect=fake_start) as start_turn, \
                patch.object(agent_server, "digest_delivery_event_state", side_effect=lambda *_: delivery_state[0]), \
                patch.object(agent_server, "digest_job_is_active", new_callable=AsyncMock, return_value=False), \
                patch.object(agent_server, "digest_job_is_queued", new_callable=AsyncMock, return_value=False):
            await finalize_handoff_digest_turn("source-1", event)
            await finalize_handoff_digest_turn("source-1", event)

        start_turn.assert_awaited_once()
        self.assertEqual(sum(event_type == "handoff_digest_ready" for _, event_type, _ in emitted), 1)
        self.assertEqual(sum(event_type == "handoff_digest_received" for _, event_type, _ in emitted), 1)
        self.assertEqual(sum(event_type == "handoff_digest_sent" for _, event_type, _ in emitted), 1)
        received = next(payload for session, event_type, payload in emitted if session == "target-1" and event_type == "handoff_digest_received")
        self.assertEqual(received["digest"], digest)

    async def test_restart_replays_an_interrupted_target_delivery(self) -> None:
        digest = "# ZenithDock Context Digest\n\nRecovered after restart."
        job = self.job()
        job.update({"status": "target_running", "digest": digest})
        agent_server.HANDOFF_DIGEST_JOBS["digest-1"] = job

        with patch.object(agent_server, "digest_event_exists", return_value=True), \
                patch.object(agent_server, "digest_delivery_event_state", return_value="running"), \
                patch.object(agent_server, "digest_job_is_active", new_callable=AsyncMock, return_value=False), \
                patch.object(agent_server, "digest_job_is_queued", new_callable=AsyncMock, return_value=False), \
                patch.object(agent_server, "start_turn_durably", new_callable=AsyncMock) as start_turn, \
                patch.object(agent_server, "append_handoff_digest_sent_once", new_callable=AsyncMock), \
                patch.object(agent_server, "update_handoff_digest_job", new_callable=AsyncMock):
            start_turn.return_value = {"run_id": "recovered-target-run", "queued": False}
            await deliver_handoff_digest(job, digest, replay_interrupted=True)

        start_turn.assert_awaited_once()
        request = start_turn.await_args.args[1]
        self.assertEqual(request.purpose, "handoff_digest_delivery")
        self.assertEqual(request.digest_job_id, "digest-1")
        self.assertEqual(request.prompt, digest)

    async def test_reconcile_marks_source_completion_as_restart_replay(self) -> None:
        job = self.job()
        job["status"] = "source_complete"
        agent_server.HANDOFF_DIGEST_JOBS["digest-1"] = job
        finished = {
            "type": "turn_finished",
            "purpose": "handoff_digest",
            "digest_job_id": "digest-1",
            "result_text": "Recovered digest",
        }
        with patch.object(agent_server, "digest_job_events", return_value=[finished]), \
                patch.object(agent_server, "finalize_handoff_digest_turn", new_callable=AsyncMock) as finalize:
            recovered = await reconcile_handoff_digest_jobs()

        self.assertEqual(recovered, 1)
        finalize.assert_awaited_once_with("source-1", finished, replay_interrupted=True)

    async def test_unqueue_marks_the_durable_digest_cancelled(self) -> None:
        item = {
            "purpose": "handoff_digest",
            "digest_job_id": "digest-1",
            "digest_detail": "normal",
            "source_session_id": "source-1",
            "target_session_id": "target-1",
        }
        with patch.object(agent_server, "update_handoff_digest_job", new_callable=AsyncMock) as update_job, \
                patch.object(agent_server, "digest_event_exists", return_value=False), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock) as append_event:
            await finish_handoff_digest_queue_item(
                "source-1",
                item,
                "Context digest request was canceled before it ran.",
                cancelled=True,
            )

        self.assertEqual(update_job.await_args.args[1]["status"], "cancelled")
        append_event.assert_awaited_once()
        self.assertEqual(append_event.await_args.args[1], "handoff_digest_error")
        self.assertTrue(append_event.await_args.args[2]["cancelled"])


if __name__ == "__main__":
    unittest.main()
