import json
import tempfile
import unittest
from pathlib import Path
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

    def test_cursor_source_pack_includes_provider_without_name_error(self) -> None:
        source = {
            "id": "cursor-source",
            "title": "Cursor source",
            "backend": agent_server.BACKEND_CURSOR,
            "cwd": "/tmp/cursor",
            "session_id": "cursor-provider-1",
            "cursor_session_id": "cursor-provider-1",
        }
        with patch.object(
            agent_server.STORE,
            "sessions",
            {"cursor-source": source},
        ), patch.object(
            agent_server,
            "read_events",
            return_value=[],
        ), patch.object(
            agent_server,
            "list_session_file_records",
            return_value=[],
        ):
            pack = agent_server.build_handoff_source_pack("cursor-source")

        self.assertIn("Backend: cursor", pack["source_pack"])
        self.assertIn(
            "Provider session/thread: cursor-provider-1",
            pack["source_pack"],
        )

    def test_source_pack_never_embeds_legacy_provider_only_prompts(self) -> None:
        session_id = "legacy-handoff-source"
        source = {
            "id": session_id,
            "title": "Legacy source",
            "backend": agent_server.BACKEND_CLAUDE,
            "cwd": "/tmp/source",
        }
        authority = agent_server.cross_chat_provider_authority_block(
            [],
            agent_server.cross_chat_authority_path(
                "run_handoff_projection",
                "abcdef0123456789abcdef0123456789",
            ),
            session_id,
            {"publish"},
            "blocked",
            compact=True,
        )
        notice = (
            "<task-notification>\n"
            "<task-id>task_pack1</task-id>\n"
            "<tool-use-id>toolu_pack_projection_123</tool-use-id>\n"
            "<status>completed</status>\n"
            "<summary>Private provider completion</summary>\n"
            "</task-notification>"
        )
        events = [
            {
                "seq": 1,
                "session_id": session_id,
                "type": "turn_started",
                "run_id": "import_handoff_projection",
                "backend": agent_server.BACKEND_CLAUDE,
                "imported": True,
                "prompt": "Keep this user request" + authority,
            },
            {
                "seq": 2,
                "session_id": session_id,
                "type": "turn_started",
                "run_id": "import_handoff_projection",
                "backend": agent_server.BACKEND_CLAUDE,
                "imported": True,
                "prompt": notice,
            },
            {
                "seq": 3,
                "session_id": session_id,
                "type": "assistant_text",
                "run_id": "import_handoff_projection",
                "text": "Retained assistant answer",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            event_file = Path(temporary) / "events.jsonl"
            event_file.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            with patch.object(
                agent_server.STORE,
                "sessions",
                {session_id: source},
            ), patch.object(
                agent_server,
                "events_path",
                return_value=event_file,
            ), patch.object(
                agent_server,
                "list_session_file_records",
                return_value=[],
            ):
                pack = agent_server.build_handoff_source_pack(session_id)

        source_pack = pack["source_pack"]
        self.assertIn("Keep this user request", source_pack)
        self.assertIn("Retained assistant answer", source_pack)
        self.assertNotIn("AgentsDock provider authority", source_pack)
        self.assertNotIn("Private provider completion", source_pack)


class DigestSummarizerBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_cannot_be_mislabeled_as_digest_summarizer(self) -> None:
        with patch.object(
            agent_server,
            "build_handoff_source_pack",
            return_value={
                "source_pack": "source",
                "source_session": {"id": "source"},
                "event_count": 0,
                "file_count": 0,
                "detail": "normal",
            },
        ), patch.object(
            agent_server,
            "run_claude_handoff_summarizer",
            new_callable=AsyncMock,
        ) as claude, patch.object(
            agent_server,
            "run_codex_handoff_summarizer",
            new_callable=AsyncMock,
        ) as codex:
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.build_handoff_digest(
                    "source",
                    summarizer_backend=agent_server.BACKEND_CURSOR,
                )

        self.assertEqual(raised.exception.status_code, 400)
        claude.assert_not_awaited()
        codex.assert_not_awaited()


class ForkHistoryDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_digest_runs_are_not_copied_into_a_fork(self) -> None:
        events = [
            {"seq": 1, "id": "normal-start", "type": "turn_started", "run_id": "normal-run", "prompt": "Keep this"},
            {"seq": 2, "id": "normal-answer", "type": "assistant_text", "run_id": "normal-run", "text": "Kept"},
            {"seq": 9, "id": "normal-finish", "type": "turn_finished", "run_id": "normal-run", "result_text": "Kept"},
            {"seq": 3, "id": "digest-start", "type": "turn_started", "run_id": "digest-run", "purpose": "handoff_digest", "digest_job_id": "digest-1", "prompt": "Generate a digest"},
            # Provider trace events do not always repeat the workflow metadata;
            # the run ID must still keep them out of forked conversation history.
            {"seq": 4, "id": "digest-thought", "type": "reasoning_summary", "run_id": "digest-run", "text": "Selecting context"},
            {"seq": 5, "id": "digest-tool", "type": "tool_started", "run_id": "digest-run", "tool": {"name": "Read"}},
            {"seq": 6, "id": "digest-answer", "type": "assistant_text", "run_id": "digest-run", "text": "# AgentsDock Context Digest"},
            {"seq": 7, "id": "delivery-start", "type": "turn_started", "run_id": "delivery-run", "purpose": "handoff_digest_delivery", "digest_job_id": "digest-1", "prompt": "Delivered context"},
            {"seq": 8, "id": "delivery-answer", "type": "assistant_text", "run_id": "delivery-run", "text": "I have the context."},
        ]

        with patch.object(agent_server, "iter_session_events", return_value=iter(events)), \
                patch.object(
                    agent_server,
                    "append_imported_events",
                    new_callable=AsyncMock,
                    side_effect=lambda _session_id, imported: len(imported),
                ) as append_imported:
            copied = await agent_server.copy_fork_history("parent-1", "child-1")

        imported = append_imported.await_args.args[1]
        self.assertEqual(copied, 3)
        self.assertEqual(
            [event_type for event_type, _payload in imported],
            ["turn_started", "assistant_text", "turn_finished"],
        )
        self.assertEqual(
            [payload["run_id"] for _event_type, payload in imported],
            ["normal-run", "normal-run", "normal-run"],
        )
        self.assertTrue(all(payload["forked"] is True for _event_type, payload in imported))
        self.assertEqual(imported[-1][1]["result_text"], "")

    async def test_fork_copy_projects_legacy_prompts_but_keeps_turn_boundaries(self) -> None:
        parent_id = "legacy-fork-parent"
        authority = agent_server.cross_chat_provider_authority_block(
            [],
            agent_server.cross_chat_authority_path(
                "run_fork_projection",
                "1234567890abcdef1234567890abcdef",
            ),
            parent_id,
            {"publish"},
            "blocked",
            compact=True,
        )
        notice = (
            "<task-notification>\n"
            "<task-id>task_fork1</task-id>\n"
            "<tool-use-id>toolu_fork_projection_123</tool-use-id>\n"
            "<status>completed</status>\n"
            "<summary>Private fork notification</summary>\n"
            "</task-notification>"
        )
        common = {
            "session_id": parent_id,
            "run_id": "import_fork_projection",
            "backend": agent_server.BACKEND_CLAUDE,
            "imported": True,
        }
        events = [
            {
                **common,
                "seq": 1,
                "type": "turn_started",
                "prompt": "Retained fork request" + authority,
            },
            {
                **common,
                "seq": 2,
                "type": "assistant_text",
                "text": "First fork answer",
            },
            {
                **common,
                "seq": 3,
                "type": "turn_started",
                "prompt": notice,
            },
            {
                **common,
                "seq": 4,
                "type": "assistant_text",
                "text": "Answer after empty fork boundary",
            },
            {
                **common,
                "seq": 5,
                "type": "turn_finished",
                "result_text": "Answer after empty fork boundary",
            },
        ]
        with patch.object(
            agent_server,
            "iter_session_events",
            return_value=iter(events),
        ), patch.object(
            agent_server,
            "append_imported_events",
            new_callable=AsyncMock,
            side_effect=lambda _session_id, imported: len(imported),
        ) as append_imported:
            copied = await agent_server.copy_fork_history(parent_id, "fork-child")

        imported = append_imported.await_args.args[1]
        self.assertEqual(copied, 5)
        self.assertEqual(imported[0][1]["prompt"], "Retained fork request")
        self.assertEqual(imported[2][0], "turn_started")
        self.assertEqual(imported[2][1]["prompt"], "")
        self.assertTrue(
            imported[2][1][agent_server.TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD]
        )
        self.assertEqual(imported[3][1]["text"], "Answer after empty fork boundary")
        self.assertNotIn("AgentsDock provider authority", json.dumps(imported))
        self.assertNotIn("Private fork notification", json.dumps(imported))


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
        digest = "# AgentsDock Context Digest\n\n## Executive Summary\nReady."
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

    async def test_logical_cursor_failure_never_delivers_digest(self) -> None:
        agent_server.HANDOFF_DIGEST_JOBS["digest-1"] = self.job()
        event = {
            "type": "turn_finished",
            "run_id": "cursor-source-run",
            "backend": agent_server.BACKEND_CURSOR,
            "purpose": "handoff_digest",
            "digest_job_id": "digest-1",
            "source_session_id": "source-1",
            "target_session_id": "target-1",
            "exit_code": 0,
            "is_error": True,
            "result_text": "# AgentsDock Context Digest\nshould not deliver",
        }
        updates: list[dict[str, object]] = []

        async def update_job(
            _job_id: str,
            values: dict[str, object],
        ) -> dict[str, object]:
            updates.append(values)
            return {**self.job(), **values}

        with patch.object(
            agent_server,
            "update_handoff_digest_job",
            side_effect=update_job,
        ), patch.object(
            agent_server,
            "digest_event_exists",
            return_value=False,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "deliver_handoff_digest",
            new_callable=AsyncMock,
        ) as deliver:
            await finalize_handoff_digest_turn("source-1", event)

        deliver.assert_not_awaited()
        self.assertEqual(updates[-1]["status"], "failed")

    async def test_restart_replays_an_interrupted_target_delivery(self) -> None:
        digest = "# AgentsDock Context Digest\n\nRecovered after restart."
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
