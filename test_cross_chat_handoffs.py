import asyncio
import json
import os
import tempfile
import time
import unittest
from collections import OrderedDict, deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import agent_server


class CrossChatStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_cross_chat = agent_server.CROSS_CHAT
        self.original_authority_root = agent_server.CROSS_CHAT_AUTHORITY_ROOT
        self.original_sessions = agent_server.STORE.sessions
        self.original_current_turns = agent_server.CURRENT_TURNS
        self.original_queued_turns = agent_server.QUEUED_TURNS
        self.original_agent_token = agent_server.AGENT_TOKEN
        self.original_busy_sessions = set(agent_server.BUSY_SESSIONS)
        self.original_run_now_turns = agent_server.RUN_NOW_TURNS
        self.original_queue_start_tasks = agent_server.QUEUE_START_TASKS
        self.original_cross_chat_event_type_cache = agent_server.CROSS_CHAT_EVENT_TYPE_CACHE
        agent_server.AGENT_TOKEN = "test-admin-token"
        agent_server.CROSS_CHAT = agent_server.CrossChatStore(self.root / "cross-chat.sqlite3")
        await agent_server.CROSS_CHAT.initialize()
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.root / "authority"
        agent_server.STORE.sessions = {
            "source": {"id": "source", "title": "Source", "backend": "codex"},
            "target": {"id": "target", "title": "Target", "backend": "claude"},
        }
        agent_server.CURRENT_TURNS = {}
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.QUEUE_START_TASKS = {}
        agent_server.CROSS_CHAT_EVENT_TYPE_CACHE = OrderedDict()
        agent_server.BUSY_SESSIONS.clear()
        agent_server.CROSS_CHAT_CAPABILITIES.clear()

    async def asyncTearDown(self) -> None:
        agent_server.CROSS_CHAT_CAPABILITIES.clear()
        agent_server.CROSS_CHAT = self.original_cross_chat
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.original_authority_root
        agent_server.STORE.sessions = self.original_sessions
        agent_server.CURRENT_TURNS = self.original_current_turns
        agent_server.QUEUED_TURNS = self.original_queued_turns
        agent_server.RUN_NOW_TURNS = self.original_run_now_turns
        agent_server.QUEUE_START_TASKS = self.original_queue_start_tasks
        agent_server.CROSS_CHAT_EVENT_TYPE_CACHE = self.original_cross_chat_event_type_cache
        agent_server.BUSY_SESSIONS.clear()
        agent_server.BUSY_SESSIONS.update(self.original_busy_sessions)
        agent_server.AGENT_TOKEN = self.original_agent_token
        self.temporary.cleanup()

    async def test_instruction_idempotency_rejects_payload_change(self) -> None:
        first, created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_one",
            source_session_id="source",
            source_run_id="run_one",
            target_session_id="target",
            body="Do the check",
            idempotency_key="stable-key",
        )
        self.assertTrue(created)
        replay, replay_created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_two",
            source_session_id="source",
            source_run_id="run_one",
            target_session_id="target",
            body="Do the check",
            idempotency_key="stable-key",
        )
        self.assertFalse(replay_created)
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.create_instruction(
                envelope_id="handoff_three",
                source_session_id="source",
                source_run_id="run_one",
                target_session_id="target",
                body="Changed payload",
                idempotency_key="stable-key",
            )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_capability_is_bound_and_one_use_per_route(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_one", [reference]
        )
        self.assertIsNotNone(authority_path)
        token = json.loads(authority_path.read_text())["capability"]
        self.assertNotIn(token, repr(agent_server.CROSS_CHAT_CAPABILITIES))
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_one"}}
        request = agent_server.CrossChatHandoffRequest(
            target_session_id="target",
            body="Do the check",
            idempotency_key="stable-key",
        )
        first, created = await agent_server.create_authorized_cross_chat_instruction(token, request)
        replay, replay_created = await agent_server.create_authorized_cross_chat_instruction(token, request)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(HTTPException) as raised:
            await agent_server.create_authorized_cross_chat_instruction(
                token,
                agent_server.CrossChatHandoffRequest(
                    target_session_id="target",
                    body="Different",
                    idempotency_key="other-key",
                ),
            )
        self.assertEqual(raised.exception.status_code, 403)
        await agent_server.revoke_cross_chat_capability("run_one")
        self.assertFalse(authority_path.exists())

    async def test_cancelled_instruction_creation_burns_exact_route_key(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_cancel", [reference]
        )
        token = json.loads(authority_path.read_text())["capability"]
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_cancel"}}
        entered = asyncio.Event()
        never = asyncio.Event()
        original_create = agent_server.CROSS_CHAT.create_instruction

        async def blocked_create(**_kwargs):
            entered.set()
            await never.wait()

        first_request = agent_server.CrossChatHandoffRequest(
            target_session_id="target",
            body="Do it",
            idempotency_key="cancel-key",
        )
        with patch.object(
            agent_server.CROSS_CHAT,
            "create_instruction",
            side_effect=blocked_create,
        ):
            task = asyncio.create_task(
                agent_server.create_authorized_cross_chat_instruction(
                    token, first_request
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(HTTPException) as raised:
                await agent_server.create_authorized_cross_chat_instruction(
                    token,
                    agent_server.CrossChatHandoffRequest(
                        target_session_id="target",
                        body="Different",
                        idempotency_key="different-key",
                    ),
                )
            self.assertEqual(raised.exception.status_code, 403)
        with patch.object(
            agent_server.CROSS_CHAT,
            "create_instruction",
            wraps=original_create,
        ):
            record, created = await agent_server.create_authorized_cross_chat_instruction(
                token, first_request
            )
        self.assertTrue(created)
        self.assertEqual(record["body"], "Do it")

    async def test_final_result_obligation_becomes_ready_once(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="final_result",
        )
        envelope_ids = await agent_server.register_final_result_obligations(
            "source", "run_final", [reference]
        )
        submit = AsyncMock()
        with patch.object(agent_server, "submit_cross_chat_delivery", submit):
            await agent_server.finalize_cross_chat_source_obligations({
                "run_id": "run_final",
                "result_text": "Finished result",
                "exit_code": 0,
                "stopped": False,
            })
            await agent_server.finalize_cross_chat_source_obligations({
                "run_id": "run_final",
                "result_text": "Finished result",
                "exit_code": 0,
                "stopped": False,
            })
        self.assertEqual(submit.await_count, 1)
        record = await agent_server.CROSS_CHAT.get(envelope_ids[0])
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["body"], "Finished result")

    async def test_cancelled_final_obligation_registration_settles_sqlite_then_fails(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="final_result",
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_create = agent_server.CROSS_CHAT.create_final_obligation

        async def delayed_create(**kwargs):
            entered.set()
            await release.wait()
            return await original_create(**kwargs)

        with patch.object(
            agent_server.CROSS_CHAT,
            "create_final_obligation",
            side_effect=delayed_create,
        ), patch.object(
            agent_server,
            "append_cross_chat_terminal_lifecycle",
            new_callable=AsyncMock,
        ):
            task = asyncio.create_task(
                agent_server.register_final_result_obligations(
                    "source", "run_cancel_final", [reference]
                )
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        records = await agent_server.CROSS_CHAT.for_source_run("run_cancel_final")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")

    async def test_accepted_instruction_finishes_submission_after_request_cancel(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_accept_cancel", [reference]
        )
        token = json.loads(authority_path.read_text())["capability"]
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_accept_cancel"}}
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode("utf-8"))
            ],
            "client": ("127.0.0.1", 1234),
        })
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_registered(*_args, **_kwargs):
            entered.set()
            await release.wait()

        submit = AsyncMock(side_effect=lambda record: record)
        with patch.object(
            agent_server,
            "append_cross_chat_event_once",
            side_effect=delayed_registered,
        ), patch.object(
            agent_server,
            "submit_cross_chat_delivery",
            submit,
        ):
            task = asyncio.create_task(
                agent_server.submit_authorized_cross_chat_handoff(
                    agent_server.CrossChatHandoffRequest(
                        target_session_id="target",
                        body="do it",
                        idempotency_key="accept-cancel-key",
                    ),
                    request,
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(20):
                if submit.await_count:
                    break
                await asyncio.sleep(0)
        submit.assert_awaited_once()

    async def test_request_cancel_during_instruction_create_still_submits_once(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_create_cancel", [reference]
        )
        token = json.loads(authority_path.read_text())["capability"]
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_create_cancel"}}
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode("utf-8"))
            ],
            "client": ("127.0.0.1", 1234),
        })
        entered = asyncio.Event()
        release = asyncio.Event()
        original_create = agent_server.CROSS_CHAT.create_instruction

        async def delayed_create(**kwargs):
            entered.set()
            await release.wait()
            return await original_create(**kwargs)

        submit = AsyncMock(side_effect=lambda record: record)
        with patch.object(
            agent_server.CROSS_CHAT,
            "create_instruction",
            side_effect=delayed_create,
        ), patch.object(
            agent_server,
            "append_cross_chat_event_once",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "submit_cross_chat_delivery",
            submit,
        ):
            task = asyncio.create_task(
                agent_server.submit_authorized_cross_chat_handoff(
                    agent_server.CrossChatHandoffRequest(
                        target_session_id="target",
                        body="do it after cancel",
                        idempotency_key="create-cancel-key",
                    ),
                    request,
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(40):
                if submit.await_count:
                    break
                await asyncio.sleep(0)
        submit.assert_awaited_once()
        records = await agent_server.CROSS_CHAT.for_source_run("run_create_cancel")
        self.assertEqual(len(records), 1)

    async def test_chat_reference_validation_rejects_self_archive_and_overlap(self) -> None:
        self_ref = agent_server.ChatReference(
            session_id="source", display_title_snapshot="Source",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_chat_references("source", "@", [self_ref])
        agent_server.STORE.sessions["target"]["archived"] = True
        archived = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_chat_references("source", "@", [archived])

    def test_queue_event_round_trip_keeps_structured_references(self) -> None:
        event = {
            "type": "turn_queued",
            "queued_id": "queued_one",
            "prompt": "ask @Target",
            "request_prompt": "ask @Target",
            "chat_references": [{
                "session_id": "target",
                "display_title_snapshot": "Target",
                "source_text_start": 4,
                "source_text_end": 11,
                "action": "instruction",
            }],
            "cross_chat_obligation_ids": ["handoff_one"],
            "ts": "2026-08-10T00:00:00Z",
        }
        item = agent_server.queued_turn_from_event(event, {"backend": "codex"}, 1)
        self.assertEqual(item["chat_references"][0]["session_id"], "target")
        self.assertEqual(item["cross_chat_obligation_ids"], ["handoff_one"])

    async def test_provider_capability_is_scoped_and_legacy_admin_tokens_are_removed(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_scope", [reference]
        )
        payload = json.loads(authority_path.read_text())
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_scope"}}
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/agent/sessions/source/jobs",
            "headers": [(b"x-agentsdock-provider-capability", payload["provider_capability"].encode())],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 7850),
            "client": ("127.0.0.1", 43210),
        })
        allowed = await agent_server.authorize_provider_action(
            request, action="jobs", session_id="source"
        )
        self.assertEqual(allowed["source_run_id"], "run_scope")
        with self.assertRaises(HTTPException):
            await agent_server.authorize_provider_action(
                request, action="jobs", session_id="target"
            )

        with patch.dict(os.environ, {
            "AGENTSDOCK_AGENT_TOKEN": "admin-a",
            "ZENITHDOCK_AGENT_TOKEN": "admin-b",
            "ZENITHBOT_AGENT_TOKEN": "admin-c",
        }, clear=False):
            for environment in (
                agent_server.runner_env(),
                agent_server.agent_runner_env("source"),
                agent_server.codex_app_server_env(),
            ):
                self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", environment)
                self.assertNotIn("ZENITHDOCK_AGENT_TOKEN", environment)
                self.assertNotIn("ZENITHBOT_AGENT_TOKEN", environment)

    def test_chat_reference_span_is_exact_utf16_and_surrogate_safe(self) -> None:
        prompt = "😀 ask @Target now"
        start = agent_server.utf16_length("😀 ask ")
        end = start + agent_server.utf16_length("@Target")
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=start,
            source_text_end=end,
            action="instruction",
        )
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            self.assertEqual(
                agent_server.validate_chat_references("source", prompt, [reference]),
                [reference],
            )
            mismatched = reference.model_copy(
                update={"display_title_snapshot": "Wrong"}
            )
            with self.assertRaises(HTTPException):
                agent_server.validate_chat_references("source", prompt, [mismatched])
            split_surrogate = reference.model_copy(
                update={"source_text_start": 1, "source_text_end": end}
            )
            with self.assertRaises(HTTPException) as raised:
                agent_server.validate_chat_references(
                    "source", prompt, [split_surrogate]
                )
            self.assertIn("Unicode character", str(raised.exception.detail))

    def test_capability_ttl_supports_overnight_live_turns(self) -> None:
        self.assertGreaterEqual(agent_server.CROSS_CHAT_CAPABILITY_TTL_SECONDS, 7 * 24 * 60 * 60)

    def test_queue_update_recovery_replaces_cross_chat_grants(self) -> None:
        path = self.root / "events.jsonl"
        events = [
            {
                "type": "turn_queued", "queued_id": "queued_one",
                "prompt": "old", "chat_references": [{"session_id": "old"}],
                "cross_chat_obligation_ids": ["handoff_old"],
            },
            {
                "type": "turn_queue_updated", "queued_id": "queued_one",
                "prompt": "new", "chat_references": [{"session_id": "target"}],
                "cross_chat_obligation_ids": ["handoff_new"],
            },
        ]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        with patch.object(agent_server, "events_path", return_value=path):
            recovered = agent_server.scan_queued_turns_from_events([
                ("source", {"backend": "codex"}),
            ])
        self.assertEqual(recovered["source"][0]["chat_references"], [{"session_id": "target"}])
        self.assertEqual(recovered["source"][0]["cross_chat_obligation_ids"], ["handoff_new"])

    async def test_restart_binds_submitting_ledger_before_queue_schedule(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_restart_bind",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="recover me",
            idempotency_key="restart-bind-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="submitting"
        )
        recovered_item = {
            "queued_id": "queued_restart_bind",
            "prompt": "relay",
            "purpose": "cross_chat_handoff_delivery",
            "source_session_id": "source",
            "target_session_id": "target",
            "cross_chat_envelope_id": record["id"],
            "position": 1,
            "_durable": True,
            "_paused_after_stop": False,
        }
        with patch.object(
            agent_server,
            "scan_queued_turns_from_events",
            return_value={"target": [recovered_item]},
        ), patch.object(
            agent_server,
            "schedule_next_queued_turn",
        ) as schedule:
            rebuilt, scheduled = await agent_server.recover_queued_turns_after_start()
        self.assertEqual((rebuilt, scheduled), (1, 1))
        bound = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(bound["status"], "queued")
        self.assertEqual(bound["queued_id"], "queued_restart_bind")
        schedule.assert_called_once_with("target")

    async def test_stale_nonterminal_lifecycle_cannot_follow_terminal_ledger(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_stale_queued",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="race",
            idempotency_key="stale-queued-key",
        )
        queued = await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"},
            status="queued", queued_id="queued_stale",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"queued"}, status="cancelled"
        )
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
        ) as append:
            await agent_server.append_cross_chat_lifecycle(
                queued,
                "cross_chat_handoff_queued",
                "queued",
                "Queued",
            )
        append.assert_not_awaited()

    def test_cross_chat_lifecycle_is_visible_and_bumps_activity(self) -> None:
        self.assertTrue(agent_server.is_agent_visible_event("cross_chat_handoff_received", {}))
        self.assertTrue(agent_server.should_bump_session_updated_at("cross_chat_handoff_started", {}))

    async def test_live_capability_survives_stale_ttl_and_terminal_revoke_wins(self) -> None:
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_overnight", []
        )
        payload = json.loads(authority_path.read_text())
        token = payload["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode()).hexdigest()
        agent_server.CROSS_CHAT_CAPABILITIES[token_hash]["expires_at"] = time.time() - 3600
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_overnight"}}
        request = Request({
            "type": "http", "method": "GET",
            "path": "/api/agent/sessions/source/jobs",
            "headers": [(b"x-agentsdock-provider-capability", token.encode())],
            "query_string": b"", "scheme": "http",
            "server": ("127.0.0.1", 7850), "client": ("127.0.0.1", 43210),
        })
        authorized = await agent_server.authorize_provider_action(
            request, action="jobs", session_id="source"
        )
        self.assertEqual(authorized["source_run_id"], "run_overnight")
        await agent_server.revoke_cross_chat_capability("run_overnight")
        with self.assertRaises(HTTPException):
            await agent_server.authorize_provider_action(
                request, action="jobs", session_id="source"
            )

    async def test_concurrent_idempotent_instruction_create_is_serialized_off_loop(self) -> None:
        async def create(envelope_id: str):
            return await agent_server.CROSS_CHAT.create_instruction(
                envelope_id=envelope_id,
                source_session_id="source",
                source_run_id="run_concurrent",
                target_session_id="target",
                body="same body",
                idempotency_key="same-key",
            )

        with patch.object(
            agent_server.asyncio,
            "to_thread",
            wraps=agent_server.asyncio.to_thread,
        ) as to_thread:
            first, second = await agent_server.asyncio.gather(
                create("handoff_concurrent_a"),
                create("handoff_concurrent_b"),
            )
        self.assertGreaterEqual(to_thread.await_count, 2)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(sorted((first[1], second[1])), [False, True])

    async def test_late_success_cannot_override_failed_delivery(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_late",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="work",
            idempotency_key="late-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="failed", error="target deleted"
        )
        append = AsyncMock()
        with (
            patch.object(agent_server, "append_durable_event", append),
            patch.object(agent_server, "cross_chat_event_exists", return_value=False),
        ):
            await agent_server.finish_cross_chat_delivery({
                "cross_chat_envelope_id": record["id"],
                "run_id": "run_target",
                "result_text": "late success",
                "exit_code": 0,
            })
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "failed")
        self.assertNotIn(
            "cross_chat_handoff_delivered",
            [call.args[1] for call in append.await_args_list],
        )

    async def test_normal_queue_item_cannot_move_across_delivery(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([
            {"queued_id": "delivery", "purpose": "cross_chat_handoff_delivery"},
            {"queued_id": "normal", "purpose": None},
        ])
        with patch.object(agent_server, "managed_server_update_blocker", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.move_queued_turn(
                    "target",
                    "normal",
                    agent_server.MoveQueuedTurnRequest(direction="up"),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["target"]],
            ["delivery", "normal"],
        )

    async def test_force_send_cannot_overtake_delivery(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([
            {"queued_id": "delivery", "purpose": "cross_chat_handoff_delivery"},
            {"queued_id": "normal", "purpose": None},
        ])
        with patch.object(agent_server, "managed_server_update_blocker", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server._run_queued_turn_now_once("target", "normal")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["target"]],
            ["delivery", "normal"],
        )

    async def test_explicit_stop_never_hides_and_pauses_internal_delivery(self) -> None:
        delivery = {
            "queued_id": "queued_delivery_after_stop",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": "handoff_after_stop",
            "_durable": True,
            "_paused_after_stop": False,
        }
        agent_server.QUEUED_TURNS["target"] = deque([delivery])
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
        ) as append, patch.object(
            agent_server,
            "start_internal_delivery_after_explicit_stop",
            new_callable=AsyncMock,
        ) as wake:
            paused = await agent_server.pause_queued_turns_after_explicit_stop(
                "target"
            )
            await asyncio.sleep(0)
        self.assertEqual(paused, 0)
        self.assertFalse(delivery["_paused_after_stop"])
        append.assert_not_awaited()
        wake.assert_awaited_once_with("target")

    async def test_stopped_slot_wakes_hidden_delivery_after_busy_releases(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_delivery_wakeup",
            "purpose": "cross_chat_handoff_delivery",
            "_paused_after_stop": False,
        }])
        agent_server.BUSY_SESSIONS.add("target")
        with patch.object(
            agent_server,
            "start_next_queued_turn",
            new_callable=AsyncMock,
        ) as start:
            wake = asyncio.create_task(
                agent_server.start_internal_delivery_after_explicit_stop("target")
            )
            await asyncio.sleep(0.06)
            start.assert_not_awaited()
            agent_server.BUSY_SESSIONS.discard("target")
            await asyncio.wait_for(wake, 0.5)
        start.assert_awaited_once_with("target")

    async def test_terminal_lifecycle_outbox_is_mirrored_once_concurrently(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_outbox",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="audit me",
            idempotency_key="outbox-key",
        )
        terminal = await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="delivered"
        )
        emitted: set[tuple[str, str, str]] = set()

        async def exists(
            session_id: str,
            envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            return (session_id, envelope_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            await asyncio.sleep(0)
            emitted.add((session_id, payload["handoff_id"], event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            await asyncio.gather(
                agent_server.append_cross_chat_terminal_lifecycle(terminal, "done"),
                agent_server.append_cross_chat_terminal_lifecycle(terminal, "done"),
            )
            await agent_server.append_cross_chat_terminal_lifecycle(terminal, "done")
        self.assertEqual(len(emitted), 2)
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["lifecycle_status"], "delivered")

    async def test_live_lifecycle_uses_primed_cache_without_full_history_scan(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_live_cache",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cache me",
            idempotency_key="live-cache-key",
        )
        agent_server.prime_cross_chat_event_cache(record)
        scans: list[bool] = []

        def scan(
            _session_id: str,
            _envelope_id: str,
            *,
            full_scan: bool = False,
        ) -> list[dict]:
            scans.append(full_scan)
            return []

        with (
            patch.object(agent_server, "cross_chat_events", side_effect=scan),
            patch.object(
                agent_server,
                "append_durable_event",
                new_callable=AsyncMock,
            ) as append,
        ):
            await agent_server.append_cross_chat_event_once(
                "source",
                record,
                "cross_chat_handoff_registered",
                "ready",
                "registered",
            )
            await agent_server.append_cross_chat_event_once(
                "source",
                record,
                "cross_chat_handoff_registered",
                "ready",
                "registered",
            )

        self.assertEqual(scans, [])
        append.assert_awaited_once()

    async def test_restart_flushes_terminal_lifecycle_outbox_exactly_once(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_restart_outbox",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="restart",
            idempotency_key="restart-outbox-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="failed", error="crash gap"
        )
        emitted: set[tuple[str, str, str]] = set()
        scan_modes: list[bool] = []

        async def exists(
            session_id: str,
            envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            scan_modes.append(full_scan)
            return (session_id, envelope_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            emitted.add((session_id, payload["handoff_id"], event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            await agent_server.reconcile_cross_chat_handoffs()
            await agent_server.reconcile_cross_chat_handoffs()
        self.assertEqual(len(emitted), 2)
        self.assertTrue(scan_modes)
        self.assertTrue(all(scan_modes))
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["lifecycle_status"], "failed")

    async def test_busy_delivery_is_ledger_bound_before_queue_is_schedulable(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_queue_bind",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="queued",
            idempotency_key="queue-bind-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="submitting"
        )
        agent_server.BUSY_SESSIONS.add("target")
        request = agent_server.TurnRequest(
            prompt="relay",
            display_prompt="relay",
            purpose="cross_chat_handoff_delivery",
            source_session_id="source",
            target_session_id="target",
            cross_chat_envelope_id=record["id"],
        )
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
            return_value={"type": "turn_queued"},
        ):
            result = await agent_server.enqueue_turn(
                "target", request, agent_server.STORE.sessions["target"]
            )
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "queued")
        self.assertEqual(refreshed["queued_id"], result["queued_id"])
        self.assertEqual(refreshed["queue_position"], 1)

    async def test_queued_cancel_removes_target_and_mirrors_terminal_lifecycle(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cancel",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cancel",
            idempotency_key="cancel-target-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="queued", queued_id="queued_cancel"
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_cancel",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": record["id"],
        }])
        terminal = AsyncMock()
        with (
            patch.object(agent_server, "append_durable_event", new_callable=AsyncMock),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", terminal),
        ):
            cancelled = await agent_server.cancel_queued_cross_chat_handoff(record["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        terminal.assert_awaited_once()

    async def test_cancel_after_durable_unqueue_finishes_ledger_and_lifecycle(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cancel_join",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cancel safely",
            idempotency_key="cancel-join-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="queued",
            queued_id="queued_cancel_join",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_cancel_join",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": record["id"],
        }])
        entered = asyncio.Event()
        release = asyncio.Event()
        original_update = agent_server.CROSS_CHAT.update
        emitted: list[tuple[str, str]] = []

        async def delayed_update(envelope_id: str, **kwargs):
            if kwargs.get("status") == "cancelled":
                entered.set()
                await release.wait()
            return await original_update(envelope_id, **kwargs)

        async def exists(
            session_id: str,
            _envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            return (session_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            if payload.get("handoff_id"):
                emitted.append((session_id, event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server.CROSS_CHAT, "update", side_effect=delayed_update),
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            task = asyncio.create_task(
                agent_server.cancel_queued_cross_chat_handoff(record["id"])
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        self.assertCountEqual(
            emitted,
            [
                ("source", "cross_chat_handoff_cancelled"),
                ("target", "cross_chat_handoff_cancelled"),
            ],
        )

    async def test_source_deletion_does_not_reclassify_admitted_target_run(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_running_delete_source",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="already admitted",
            idempotency_key="running-delete-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"},
            status="running", target_run_id="run_target",
        )
        count = await agent_server.terminalize_cross_chat_session_deletion("source")
        self.assertEqual(count, 0)
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "running")

    async def test_archive_cancels_paused_target_delivery_and_source_obligation(self) -> None:
        target_record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_archive_target",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="queued target",
            idempotency_key="archive-target-key",
        )
        await agent_server.CROSS_CHAT.update(
            target_record["id"], expected={"ready"},
            status="queued", queued_id="queued_target",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_target",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": target_record["id"],
            "_paused_after_stop": True,
        }])
        source_obligation = await agent_server.CROSS_CHAT.create_final_obligation(
            envelope_id="handoff_archive_source",
            source_session_id="source",
            source_run_id="queued_source",
            target_session_id="target",
            idempotency_key="archive-source-key",
        )
        agent_server.QUEUED_TURNS["source"] = deque([{
            "queued_id": "queued_source",
            "prompt": "send @Target",
            "file_ids": [],
            "chat_references": [{
                "session_id": "target",
                "display_title_snapshot": "Target",
                "source_text_start": 5,
                "source_text_end": 12,
                "action": "final_result",
            }],
            "cross_chat_obligation_ids": [source_obligation["id"]],
            "_durable": True,
        }])
        with (
            patch.object(agent_server, "append_durable_event", new_callable=AsyncMock),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", new_callable=AsyncMock),
        ):
            await agent_server.terminalize_archived_cross_chat_session("target")
            await agent_server.terminalize_archived_cross_chat_session("source")
        target_after = await agent_server.CROSS_CHAT.get(target_record["id"])
        source_after = await agent_server.CROSS_CHAT.get(source_obligation["id"])
        self.assertEqual(target_after["status"], "cancelled")
        self.assertEqual(source_after["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        self.assertEqual(
            agent_server.QUEUED_TURNS["source"][0]["cross_chat_obligation_ids"],
            [],
        )

    async def test_fast_delivery_terminal_does_not_regress_to_started(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_fast",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="fast",
            idempotency_key="fast-key",
        )

        async def finish_before_return(*_args, **_kwargs):
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={"submitting"},
                status="running", target_run_id="run_target",
            )
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={"running"}, status="delivered"
            )
            return {"queued": False, "run_id": "run_target"}

        lifecycle = AsyncMock()
        with (
            patch.object(agent_server, "cross_chat_delivery_client_capabilities", return_value=[]),
            patch.object(agent_server, "append_cross_chat_event_once", new_callable=AsyncMock),
            patch.object(agent_server, "start_turn_durably", side_effect=finish_before_return),
            patch.object(agent_server, "append_cross_chat_lifecycle", lifecycle),
        ):
            result = await agent_server.submit_cross_chat_delivery(record)
        self.assertEqual(result["status"], "delivered")
        lifecycle.assert_not_awaited()

    async def test_provider_admission_cas_loses_to_revocation_for_direct_and_queued(self) -> None:
        for suffix, initial, queued_id in (
            ("direct", "submitting", None),
            ("queued", "queued", "queued_before"),
        ):
            record, _created = await agent_server.CROSS_CHAT.create_instruction(
                envelope_id=f"handoff_admit_{suffix}",
                source_session_id="source",
                source_run_id=f"run_source_{suffix}",
                target_session_id="target",
                body=suffix,
                idempotency_key=f"admit-key-{suffix}",
            )
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={"ready"},
                status=initial, queued_id=queued_id,
            )
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={initial}, status="cancelled"
            )
            admitted = await agent_server.admit_cross_chat_delivery_run(
                record["id"], queued_id=queued_id, run_id=f"run_target_{suffix}"
            )
            self.assertIsNone(admitted)
            refreshed = await agent_server.CROSS_CHAT.get(record["id"])
            self.assertEqual(refreshed["status"], "cancelled")

    async def test_unsupported_target_transport_fails_once_before_received(self) -> None:
        agent_server.STORE.sessions["target"]["backend"] = "codex"
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_legacy",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="legacy",
            idempotency_key="legacy-key",
        )
        terminal = AsyncMock()
        received = AsyncMock()
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", terminal),
            patch.object(agent_server, "append_cross_chat_event_once", received),
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.submit_cross_chat_delivery(record)
        self.assertEqual(raised.exception.status_code, 409)
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "failed")
        terminal.assert_awaited_once()
        received.assert_not_awaited()

    def test_target_delivery_capabilities_require_native_codex_and_claude(self) -> None:
        with patch.object(
            agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_APP_SERVER
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities({"backend": "codex"}),
                [agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY],
            )
        with patch.object(
            agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC
        ):
            with self.assertRaises(HTTPException):
                agent_server.cross_chat_delivery_client_capabilities({"backend": "codex"})
        with (
            patch.object(
                agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AGENT_SDK
            ),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=True),
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities({"backend": "claude"}),
                [agent_server.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY],
            )
        with patch.object(
            agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT
        ):
            with self.assertRaises(HTTPException):
                agent_server.cross_chat_delivery_client_capabilities({"backend": "claude"})

    def test_handoff_capability_advertises_only_native_target_backends(self) -> None:
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC),
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=False),
        ):
            unavailable = agent_server.cross_chat_handoffs_capability()
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["supported_target_backends"], [])

        with (
            patch.object(
                agent_server,
                "CODEX_TRANSPORT",
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            patch.object(
                agent_server,
                "CLAUDE_TRANSPORT",
                agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
            ),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=True),
        ):
            available = agent_server.cross_chat_handoffs_capability()
        self.assertTrue(available["available"])
        self.assertEqual(
            available["supported_target_backends"],
            [agent_server.BACKEND_CODEX, agent_server.BACKEND_CLAUDE],
        )

    def test_agent_helper_bypass_allowlist_covers_exact_registered_routes(self) -> None:
        registered = []
        for route in agent_server.app.routes:
            path = str(getattr(route, "path", ""))
            if not path.startswith("/api/agent/"):
                continue
            for method in set(getattr(route, "methods", set())) - {"HEAD", "OPTIONS"}:
                registered.append((method, path))
                sample = path.replace("{session_id}", "sess").replace("{job_id}", "job")
                self.assertTrue(agent_server.is_agent_helper_route(method, sample))
        self.assertTrue(registered)
        self.assertFalse(
            agent_server.is_agent_helper_route("POST", "/api/agent/future-route")
        )

    async def test_internal_target_admission_is_hidden_from_all_client_generations(self) -> None:
        internal = {
            "id": "internal-start",
            "seq": 2,
            "session_id": "target",
            "type": "turn_started",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": "handoff_hidden",
            "prompt": "Handoff from Source",
        }
        self.assertFalse(agent_server.is_client_visible_event(internal))
        self.assertTrue(agent_server.is_client_visible_event({
            **internal,
            "purpose": None,
        }))
        self.assertIsNone(agent_server.history_search_event_record(internal))
        self.assertTrue(agent_server.is_client_visible_event({
            **internal,
            "type": "assistant_text",
            "text": "Target answer",
        }))
        async with agent_server.QUEUE_LOCK:
            agent_server.QUEUED_TURNS["target"] = agent_server.deque([
                {
                    "queued_id": "queued_internal",
                    "prompt": "internal",
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_envelope_id": "handoff_hidden",
                },
                {
                    "queued_id": "queued_user",
                    "prompt": "visible",
                    "purpose": None,
                },
            ])
        snapshot = await agent_server.queued_turns_snapshot("target")
        self.assertEqual([item["queued_id"] for item in snapshot], ["queued_user"])

        event_file = self.root / "client-events.jsonl"
        events = [
            {
                "id": "normal-start",
                "seq": 1,
                "session_id": "target",
                "type": "turn_started",
                "prompt": "Visible user prompt",
            },
            internal,
            {
                "id": "internal-reasoning",
                "seq": 3,
                "session_id": "target",
                "type": "reasoning_summary",
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target reasoning",
            },
            {
                "id": "internal-answer",
                "seq": 4,
                "session_id": "target",
                "type": "assistant_text",
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target answer",
            },
            {
                **internal,
                "id": "hidden-newest-start",
                "seq": 5,
            },
        ]
        event_file.write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )
        with patch.object(agent_server, "events_path", return_value=event_file):
            page = agent_server.read_client_events_page("target", limit=2)
            self.assertEqual([event["seq"] for event in page[0]], [1, 3])
            self.assertEqual(page[1], 5)
            self.assertEqual(page[2:], (3, 0, 1))
            tail = agent_server.read_client_events_page("target", limit=2, tail=True)
            self.assertEqual([event["seq"] for event in tail[0]], [3, 4])
            self.assertEqual(tail[1], 5)
            self.assertEqual(tail[2:], (3, 1, 0))
            visible = agent_server.read_visible_events_page("target", limit=10)
            self.assertEqual([event["seq"] for event in visible[0]], [1, 3, 4])
            self.assertEqual(visible[2:], (3, 0, 0))
            catchup = agent_server.read_event_catchup_batch(
                "target", after=0, through=5, limit=10
            )
            self.assertEqual([event["seq"] for event in catchup[0]], [1, 3, 4])
            response = await agent_server.get_session(
                "target", limit=2, tail=False
            )
            self.assertEqual([event["seq"] for event in response["events"]], [1, 3])
            self.assertEqual(response["latest_seq"], 5)
            self.assertEqual(response["event_count"], 3)
            self.assertEqual(response["events_omitted_after"], 1)

        broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "events_path", return_value=self.root / "live.jsonl"),
            patch.object(agent_server, "next_event_seq", AsyncMock(side_effect=[1, 2])),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", broadcast),
        ):
            await agent_server.append_event("target", "turn_started", {
                key: value for key, value in internal.items()
                if key not in {"id", "seq", "session_id", "type"}
            })
            await agent_server.append_event("target", "assistant_text", {
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target answer",
            })
        broadcast.assert_awaited_once()
        self.assertEqual(broadcast.await_args.args[1]["type"], "assistant_text")

    async def test_ordinary_turn_rejects_reserved_cross_chat_envelope(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await agent_server._start_turn_locked(
                "target",
                agent_server.TurnRequest(
                    prompt="Ordinary prompt",
                    cross_chat_envelope_id="forged-envelope",
                ),
                queue_if_busy=False,
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_unauthenticated_server_rejects_cross_chat_references(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        agent_server.AGENT_TOKEN = ""
        with self.assertRaises(HTTPException) as raised:
            agent_server.validate_chat_references("source", "@", [reference])
        self.assertEqual(raised.exception.status_code, 503)

    def test_semantic_pagination_groups_handoff_lifecycle_and_hides_synthetic_prompt(self) -> None:
        event_file = self.root / "timeline.jsonl"
        events = [
            {"id": "e1", "seq": 1, "type": "cross_chat_handoff_received", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "received", "handoff_action": "instruction", "message": "Received"},
            {"id": "e2", "seq": 2, "type": "cross_chat_handoff_queued", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "queued", "handoff_action": "instruction", "message": "Queued"},
            {"id": "e3", "seq": 3, "type": "turn_started", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "cross_chat_envelope_id": "handoff_group", "prompt": "synthetic relay"},
            {"id": "e4", "seq": 4, "type": "turn_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "cross_chat_envelope_id": "handoff_group", "result_text": "done", "exit_code": 0},
            {"id": "e5", "seq": 5, "type": "cross_chat_handoff_delivered", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "delivered", "handoff_action": "instruction", "message": "Delivered"},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("target", limit=10, tail=True)
        self.assertEqual(page["semantic_item_count"], 1)
        self.assertNotIn("turn_started", [event["type"] for event in page["events"]])
        self.assertIn("cross_chat_handoff_delivered", [event["type"] for event in page["events"]])

        event_file.write_text(json.dumps(events[2]) + "\n")
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            orphan_page = agent_server.read_semantic_timeline_page(
                "target", limit=10, tail=True
            )
        self.assertEqual(orphan_page["semantic_item_count"], 0)
        self.assertEqual(orphan_page["events"], [])

    def test_source_handoff_lifecycle_does_not_hijack_source_run(self) -> None:
        event_file = self.root / "source-timeline.jsonl"
        events = [
            {"id": "s1", "seq": 1, "type": "turn_started", "run_id": "run_source", "prompt": "work"},
            {"id": "h1", "seq": 2, "type": "cross_chat_handoff_registered", "run_id": "run_source", "handoff_id": "handoff_a", "cross_chat_envelope_id": "handoff_a", "handoff_status": "registered", "handoff_action": "final_result", "message": "A"},
            {"id": "h2", "seq": 3, "type": "cross_chat_handoff_registered", "run_id": "run_source", "handoff_id": "handoff_b", "cross_chat_envelope_id": "handoff_b", "handoff_status": "registered", "handoff_action": "final_result", "message": "B"},
            {"id": "s2", "seq": 4, "type": "reasoning_summary", "run_id": "run_source", "text": "thinking"},
            {"id": "s3", "seq": 5, "type": "turn_finished", "run_id": "run_source", "result_text": "source answer", "exit_code": 0},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("source", limit=10, tail=True)
        self.assertEqual(page["semantic_item_count"], 3)
        source_finished = [
            event for event in page["events"]
            if event.get("type") == "turn_finished" and event.get("run_id") == "run_source"
        ]
        self.assertEqual(len(source_finished), 1)

    def test_cross_chat_reload_keeps_target_trace_tools_and_artifacts(self) -> None:
        event_file = self.root / "target-trace-timeline.jsonl"
        base = {
            "cross_chat_envelope_id": "handoff_trace",
            "handoff_id": "handoff_trace",
        }
        events = [
            {"id": "c1", "seq": 1, "type": "cross_chat_handoff_started", "handoff_status": "running", "handoff_action": "instruction", "message": "Started", **base},
            {"id": "c2", "seq": 2, "type": "reasoning_summary", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "text": "reason", **base},
            {"id": "c3", "seq": 3, "type": "tool_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "tool_name": "shell", "output": "ok", **base},
            {"id": "c4", "seq": 4, "type": "artifact_created", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "artifact": {"id": "artifact"}, **base},
            {"id": "c5", "seq": 5, "type": "turn_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "result_text": "done", "exit_code": 0, **base},
            {"id": "c6", "seq": 6, "type": "cross_chat_handoff_delivered", "handoff_status": "delivered", "handoff_action": "instruction", "message": "Delivered", **base},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("target", limit=20, tail=True)
        types = {event["type"] for event in page["events"]}
        self.assertIn("reasoning_summary", types)
        self.assertIn("tool_finished", types)
        self.assertIn("artifact_created", types)
        self.assertIn("turn_finished", types)


if __name__ == "__main__":
    unittest.main()
