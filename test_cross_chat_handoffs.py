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
        self.original_session_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
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
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
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
        agent_server.SESSION_LIFECYCLE_LOCKS = self.original_session_lifecycle_locks
        agent_server.BUSY_SESSIONS.clear()
        agent_server.BUSY_SESSIONS.update(self.original_busy_sessions)
        agent_server.AGENT_TOKEN = self.original_agent_token
        self.temporary.cleanup()

    async def create_exchange(
        self,
        exchange_id: str,
        *,
        source_run_id: str = "run_source",
    ) -> tuple[dict, dict]:
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id=exchange_id,
            requester_session_id="source",
            authorization_source_run_id=source_run_id,
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, created = await agent_server.CROSS_CHAT.create_initial_exchange_leg(
            exchange_id=exchange_id,
            source_session_id="source",
            source_run_id=source_run_id,
            target_session_id="target",
            body="Please investigate",
            idempotency_key=f"ask:{exchange_id}",
        )
        self.assertTrue(created)
        return exchange, leg

    @staticmethod
    def direct_grant_handle(
        authority_path: Path,
        *,
        target_session_id: str = "target",
        action: str = "instruction",
    ) -> str:
        token = json.loads(authority_path.read_text())["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode()).hexdigest()
        capability = agent_server.CROSS_CHAT_CAPABILITIES[token_hash]
        handles = [
            grant_id
            for grant_id, grant in capability["provider_direct_grants"].items()
            if grant == {
                "target_session_id": target_session_id,
                "action": action,
            }
        ]
        if len(handles) != 1:
            raise AssertionError("expected one exact provider direct grant")
        return handles[0]

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
        handle = self.direct_grant_handle(authority_path)
        authority_block = agent_server.cross_chat_provider_authority_block(
            [reference],
            authority_path,
            "source",
            {"cross_chat_instruction"},
        )
        self.assertIn(f"handle={handle}", authority_block)
        self.assertNotIn("target=target", authority_block)
        second_authority = await agent_server.issue_cross_chat_capability(
            "source", "run_two", [reference]
        )
        self.assertNotEqual(
            handle,
            self.direct_grant_handle(second_authority),
        )
        await agent_server.revoke_cross_chat_capability("run_two")
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_one"}}
        with self.assertRaises(HTTPException) as raw_target:
            await agent_server.create_authorized_cross_chat_instruction(
                token,
                agent_server.CrossChatHandoffRequest(
                    target_session_id="target",
                    body="Raw ids are not provider grants",
                    idempotency_key="raw-target-key",
                ),
            )
        self.assertEqual(raw_target.exception.status_code, 403)
        with self.assertRaises(HTTPException) as wrong_action:
            await agent_server.create_authorized_cross_chat_instruction(
                token,
                agent_server.CrossChatHandoffRequest(
                    target_session_id=handle,
                    action="request_reply",
                    body="Wrong action",
                    idempotency_key="wrong-action-key",
                ),
            )
        self.assertEqual(wrong_action.exception.status_code, 403)
        request = agent_server.CrossChatHandoffRequest(
            target_session_id=handle,
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
                    target_session_id=handle,
                    body="Different",
                    idempotency_key="other-key",
                ),
            )
        self.assertEqual(raised.exception.status_code, 403)
        await agent_server.revoke_cross_chat_capability("run_one")
        self.assertFalse(authority_path.exists())

    async def test_direct_provider_receipt_is_minimal_and_opaque(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_receipt", [reference]
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        handle = self.direct_grant_handle(authority_path)
        agent_server.CURRENT_TURNS = {
            "source": {"run_id": "run_receipt"},
        }
        provider_request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode("utf-8"))
            ],
            "client": ("127.0.0.1", 1234),
        })
        with (
            patch.object(
                agent_server,
                "append_cross_chat_event_once",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent_server,
                "submit_cross_chat_delivery",
                new_callable=AsyncMock,
                side_effect=lambda record: record,
            ),
        ):
            response = await agent_server.submit_authorized_cross_chat_handoff(
                agent_server.CrossChatHandoffRequest(
                    target_session_id=handle,
                    body="Do the check",
                    idempotency_key="minimal-receipt-key",
                ),
                provider_request,
            )

        self.assertEqual(response, {
            "ok": True,
            "action": "instruction",
            "accepted": True,
        })
        self.assertNotIn("source", json.dumps(response))
        self.assertNotIn("target", json.dumps(response))

    async def test_direct_response_receipt_is_minimal_and_keeps_exchange(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_receipt")
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_response",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "target",
            "run_response",
            [],
            actions={"cross_chat_response"},
            exchange_response_grants={(exchange["id"], inbound["id"])},
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        agent_server.CURRENT_TURNS = {
            "target": {"run_id": "run_response"},
        }
        provider_request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode("utf-8"))
            ],
            "client": ("127.0.0.1", 1234),
        })

        async def accept_leg(current_exchange, leg):
            return current_exchange, leg

        with (
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_lifecycle",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent_server,
                "submit_cross_chat_exchange_leg",
                new_callable=AsyncMock,
                side_effect=accept_leg,
            ),
        ):
            response = await agent_server.submit_authorized_cross_chat_exchange_response(
                exchange["id"],
                agent_server.CrossChatExchangeResponseRequest(
                    inbound_leg_id=inbound["id"],
                    body="The answer",
                    request_response=False,
                    idempotency_key="minimal-response-receipt-key",
                ),
                provider_request,
            )

        self.assertEqual(response, {
            "ok": True,
            "action": "response",
            "accepted": True,
        })
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["id"],
            exchange["id"],
        )
        self.assertNotIn("source", json.dumps(response))
        self.assertNotIn("target", json.dumps(response))

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
        handle = self.direct_grant_handle(authority_path)
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_cancel"}}
        entered = asyncio.Event()
        never = asyncio.Event()
        original_create = agent_server.CROSS_CHAT.create_instruction

        async def blocked_create(**_kwargs):
            entered.set()
            await never.wait()

        first_request = agent_server.CrossChatHandoffRequest(
            target_session_id=handle,
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
                        target_session_id=handle,
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
        handle = self.direct_grant_handle(authority_path)
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
                        target_session_id=handle,
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
        handle = self.direct_grant_handle(authority_path)
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

        submitted = asyncio.Event()

        async def record_submit(record):
            submitted.set()
            return record

        submit = AsyncMock(side_effect=record_submit)
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
                        target_session_id=handle,
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
            await asyncio.wait_for(submitted.wait(), timeout=2)
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

    async def test_public_turn_cannot_spoof_scheduled_job_reference_authority(
        self,
    ) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        with patch.object(
            agent_server,
            "initial_provider_cross_chat_route_snapshot",
            return_value=[],
        ):
            with self.assertRaisesRegex(HTTPException, "internal turns") as raised:
                await agent_server._start_turn_locked(
                    "source",
                    agent_server.TurnRequest(
                        prompt="@Target do work",
                        purpose="scheduled_job",
                        chat_references=[reference],
                    ),
                    queue_if_busy=False,
                )
        self.assertEqual(raised.exception.status_code, 400)

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

            for attached_prompt in (
                "😀 ask @Target2 now",
                "😀 askx@Target now",
            ):
                with self.assertRaises(HTTPException) as attached:
                    agent_server.validate_chat_references(
                        "source", attached_prompt, [reference]
                    )
                self.assertIn("not delimited", str(attached.exception.detail))

            ambiguous_title = reference.model_copy(
                update={
                    "display_title_snapshot": "@Target",
                    "source_text_end": end + 1,
                }
            )
            with self.assertRaises(HTTPException) as ambiguous:
                agent_server.validate_chat_references(
                    "source", "😀 ask @@Target now", [ambiguous_title]
                )
            self.assertIn("cannot begin with @", str(ambiguous.exception.detail))

        with self.assertRaisesRegex(ValueError, "cannot begin with @"):
            agent_server.ChatReference(
                session_id="target",
                display_title_snapshot="@Target",
                source_text_start=start,
                source_text_end=end + 1,
                action="direct_message",
            )

    def test_request_reply_requires_additive_v2_client_capability(self) -> None:
        prompt = "ask @Target"
        instruction = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=4,
            source_text_end=11,
            action="instruction",
        )
        request_reply = instruction.model_copy(update={"action": "request_reply"})
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
        ):
            self.assertEqual(
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [instruction],
                    [agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY],
                ),
                [instruction],
            )
            with self.assertRaises(HTTPException) as raised:
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [request_reply],
                    [agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY],
                )
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("cross_chat_handoffs_v2", str(raised.exception.detail))
            self.assertEqual(
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [request_reply],
                    [
                        agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
                    ],
                ),
                [request_reply],
            )

    async def test_queue_edit_requires_and_durably_preserves_v2_capability(self) -> None:
        prompt = "ask @Target"
        instruction = {
            "session_id": "target",
            "display_title_snapshot": "Target",
            "source_text_start": 4,
            "source_text_end": 11,
            "action": "instruction",
        }
        request_reply = agent_server.ChatReference(
            **{**instruction, "action": "request_reply"}
        )
        original_event = {
            "type": "turn_queued",
            "queued_id": "queued_v2_upgrade",
            "prompt": prompt,
            "request_prompt": prompt,
            "chat_references": [instruction],
            "client_capabilities": [
                agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY
            ],
            "ts": "2026-08-10T00:00:00Z",
        }
        item = agent_server.queued_turn_from_event(
            original_event,
            agent_server.STORE.sessions["source"],
            1,
        )
        agent_server.QUEUED_TURNS = {"source": deque([item])}
        with (
            patch.object(agent_server, "managed_server_update_blocker", return_value=None),
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
            patch.object(agent_server, "append_durable_event", AsyncMock()) as append,
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.update_queued_turn(
                    "source",
                    "queued_v2_upgrade",
                    agent_server.UpdateQueuedTurnRequest(
                        chat_references=[request_reply],
                    ),
                )
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(item["chat_references"][0]["action"], "instruction")

            response = await agent_server.update_queued_turn(
                "source",
                "queued_v2_upgrade",
                agent_server.UpdateQueuedTurnRequest(
                    client_capabilities=[
                        agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
                    ],
                    chat_references=[request_reply],
                ),
            )
            updated = response["item"]
            with self.assertRaises(HTTPException) as downgrade:
                await agent_server.update_queued_turn(
                    "source",
                    "queued_v2_upgrade",
                    agent_server.UpdateQueuedTurnRequest(
                        client_capabilities=[
                            agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        ],
                    ),
                )
            self.assertEqual(downgrade.exception.status_code, 400)
        self.assertEqual(updated["chat_references"][0]["action"], "request_reply")
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            updated["client_capabilities"],
        )
        update_payload = append.await_args.args[2]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            update_payload["client_capabilities"],
        )

        event_file = self.root / "queued-v2-recovery.jsonl"
        event_file.write_text(
            json.dumps(original_event) + "\n"
            + json.dumps({"type": "turn_queue_updated", **update_payload}) + "\n"
        )
        with patch.object(agent_server, "events_path", return_value=event_file):
            recovered = agent_server.scan_queued_turns_from_events([
                ("source", agent_server.STORE.sessions["source"]),
            ])["source"][0]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            recovered["client_capabilities"],
        )

        agent_server.QUEUED_TURNS = {"source": deque([recovered])}
        start = AsyncMock(return_value={"ok": True})
        with patch.object(agent_server, "_start_turn_locked", start):
            await agent_server.start_next_queued_turn("source")
        promoted_request = start.await_args.args[1]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            promoted_request.client_capabilities,
        )

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

    async def test_internal_delivery_jobs_authority_follows_destination_policy(
        self,
    ) -> None:
        class IssuanceReached(Exception):
            pass

        for index, (mode, expected) in enumerate((
            ("full", True),
            ("read_only", True),
            ("blocked", False),
        )):
            with self.subTest(mode=mode):
                agent_server.STORE.sessions["target"][
                    "provider_jobs_access"
                ] = mode
                record, _created = await agent_server.CROSS_CHAT.create_instruction(
                    envelope_id=f"handoff_jobs_policy_{index}",
                    source_session_id="source",
                    source_run_id=f"run_source_{index}",
                    target_session_id="target",
                    body="Create the requested local cron.",
                    idempotency_key=f"handoff-jobs-policy-{index}",
                    authorization_kind="configured_route",
                    authorization_route_id="route_" + "a" * 32,
                )
                await agent_server.CROSS_CHAT.update(
                    record["id"],
                    expected={"ready"},
                    status="submitting",
                )
                captured: dict[str, set[str]] = {}

                async def capture_issue(*_args, **kwargs):
                    captured["actions"] = set(kwargs.get("actions") or set())
                    raise IssuanceReached

                request = agent_server.TurnRequest(
                    prompt=agent_server.cross_chat_delivery_prompt(
                        record,
                        "Source",
                    ),
                    display_prompt="Agent-authored same-server handoff",
                    purpose="cross_chat_handoff_delivery",
                    source_session_id="source",
                    target_session_id="target",
                    cross_chat_envelope_id=record["id"],
                    client_capabilities=(
                        agent_server.cross_chat_delivery_client_capabilities(
                            agent_server.STORE.sessions["target"]
                        )
                    ),
                )
                with (
                    patch.object(
                        agent_server,
                        "managed_server_update_blocker",
                        return_value=None,
                    ),
                    patch.object(
                        agent_server,
                        "turn_start_blocker",
                        AsyncMock(return_value=None),
                    ),
                    patch.object(
                        agent_server,
                        "ensure_runtime_available",
                        AsyncMock(),
                    ),
                    patch.object(
                        agent_server.STORE,
                        "mark_backend_started",
                        AsyncMock(
                            return_value=agent_server.STORE.sessions["target"]
                        ),
                    ),
                    patch.object(
                        agent_server,
                        "build_turn_provider_prompt",
                        return_value="relay prompt",
                    ),
                    patch.object(
                        agent_server,
                        "issue_cross_chat_capability",
                        side_effect=capture_issue,
                    ),
                    patch.object(
                        agent_server,
                        "append_cross_chat_terminal_lifecycle",
                        new_callable=AsyncMock,
                    ),
                ):
                    with self.assertRaises(IssuanceReached):
                        await agent_server._start_turn_locked(
                            "target",
                            request,
                            queue_if_busy=False,
                        )

                self.assertEqual("jobs" in captured["actions"], expected)
                self.assertNotIn("target", agent_server.BUSY_SESSIONS)

    async def test_exchange_jobs_authority_accepts_requests_not_replies_or_status(
        self,
    ) -> None:
        class IssuanceReached(Exception):
            pass

        agent_server.STORE.sessions["target"]["provider_jobs_access"] = "full"
        cases = (
            ("request", False, True),
            ("reply", False, False),
            ("status", True, False),
        )
        for index, (kind, status_delivery, expected) in enumerate(cases):
            with self.subTest(kind=kind):
                exchange_id = f"exchange_jobs_policy_{index}"
                leg_id = f"leg_jobs_policy_{index}"
                exchange = {
                    "id": exchange_id,
                    "status": "active",
                    "used_legs": 1,
                    "max_legs": 6,
                }
                leg = {
                    "id": leg_id,
                    "exchange_id": exchange_id,
                    "source_session_id": "source",
                    "target_session_id": "target",
                    "status": "submitting",
                    "kind": kind,
                }
                captured: dict[str, set[str]] = {}

                async def capture_issue(*_args, **kwargs):
                    captured["actions"] = set(kwargs.get("actions") or set())
                    raise IssuanceReached

                request = agent_server.TurnRequest(
                    prompt="relay prompt",
                    display_prompt="Cross-chat exchange message",
                    purpose="cross_chat_handoff_delivery",
                    source_session_id="source",
                    target_session_id="target",
                    cross_chat_exchange_id=exchange_id,
                    cross_chat_exchange_leg_id=leg_id,
                    cross_chat_exchange_status=status_delivery,
                    client_capabilities=(
                        agent_server.cross_chat_delivery_client_capabilities(
                            agent_server.STORE.sessions["target"]
                        )
                    ),
                )
                with (
                    patch.object(
                        agent_server,
                        "get_cross_chat_delivery_record",
                        AsyncMock(return_value=leg),
                    ),
                    patch.object(
                        agent_server.CROSS_CHAT,
                        "get_exchange",
                        AsyncMock(return_value=exchange),
                    ),
                    patch.object(
                        agent_server,
                        "managed_server_update_blocker",
                        return_value=None,
                    ),
                    patch.object(
                        agent_server,
                        "turn_start_blocker",
                        AsyncMock(return_value=None),
                    ),
                    patch.object(
                        agent_server,
                        "ensure_runtime_available",
                        AsyncMock(),
                    ),
                    patch.object(
                        agent_server.STORE,
                        "mark_backend_started",
                        AsyncMock(
                            return_value=agent_server.STORE.sessions["target"]
                        ),
                    ),
                    patch.object(
                        agent_server,
                        "build_turn_provider_prompt",
                        return_value="relay prompt",
                    ),
                    patch.object(
                        agent_server,
                        "issue_cross_chat_capability",
                        side_effect=capture_issue,
                    ),
                ):
                    with self.assertRaises(IssuanceReached):
                        await agent_server._start_turn_locked(
                            "target",
                            request,
                            queue_if_busy=False,
                        )

                self.assertEqual("jobs" in captured["actions"], expected)
                self.assertNotIn("target", agent_server.BUSY_SESSIONS)

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

    async def test_ready_cursor_target_uses_headless_delivery_contract(self) -> None:
        agent_server.STORE.sessions["target"]["backend"] = "cursor"
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cursor_ready",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="Inspect the failure and report back.",
            idempotency_key="cursor-ready-key",
        )
        captured: dict[str, object] = {}

        async def start_cursor_delivery(session_id: str, request):
            captured["session_id"] = session_id
            captured["request"] = request
            await agent_server.CROSS_CHAT.update(
                record["id"],
                expected={"submitting"},
                status="running",
                target_run_id="run_cursor_target",
            )
            return {"queued": False, "run_id": "run_cursor_target"}

        with (
            patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": "ready",
                        "available": True,
                        "installed": True,
                        "authenticated": True,
                        "_executable": "/test/bin/agent",
                    }
                },
                clear=True,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_event_once",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_lifecycle",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent_server,
                "start_turn_durably",
                side_effect=start_cursor_delivery,
            ),
        ):
            result = await agent_server.submit_cross_chat_delivery(record)

        request = captured["request"]
        self.assertEqual(captured["session_id"], "target")
        self.assertEqual(request.purpose, "cross_chat_handoff_delivery")
        self.assertEqual(request.target_session_id, "target")
        self.assertEqual(request.cross_chat_envelope_id, record["id"])
        self.assertEqual(request.client_capabilities, [])
        self.assertEqual(result["status"], "running")

    async def test_cursor_delivery_rechecks_runtime_at_provider_admission(self) -> None:
        class AdmissionReached(Exception):
            pass

        agent_server.STORE.sessions["target"]["backend"] = "cursor"
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cursor_admission",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="Run the admitted Cursor turn.",
            idempotency_key="cursor-admission-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="submitting",
        )
        request = agent_server.TurnRequest(
            prompt=agent_server.cross_chat_delivery_prompt(record, "Source"),
            display_prompt="Agent-authored same-server handoff",
            purpose="cross_chat_handoff_delivery",
            source_session_id="source",
            target_session_id="target",
            cross_chat_envelope_id=record["id"],
            client_capabilities=[],
        )
        ensure_runtime = AsyncMock(
            return_value={
                "backend": agent_server.BACKEND_CURSOR,
                "status": "ready",
                "_executable": "/test/bin/agent",
            }
        )
        with (
            patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": "ready",
                        "_executable": "/test/bin/agent",
                    }
                },
                clear=True,
            ),
            patch.object(
                agent_server,
                "managed_server_update_admission_blocker",
                return_value=None,
            ),
            patch.object(
                agent_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                agent_server,
                "ensure_runtime_available",
                ensure_runtime,
            ),
            patch.object(
                agent_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=agent_server.STORE.sessions["target"]),
            ),
            patch.object(
                agent_server,
                "build_turn_provider_prompt",
                return_value="relay prompt",
            ),
            patch.object(
                agent_server,
                "issue_cross_chat_capability",
                AsyncMock(side_effect=AdmissionReached),
            ),
        ):
            with self.assertRaises(AdmissionReached):
                await agent_server._start_turn_locked(
                    "target",
                    request,
                    queue_if_busy=False,
                )

        ensure_runtime.assert_awaited_once_with(agent_server.BACKEND_CURSOR)
        self.assertNotIn("target", agent_server.BUSY_SESSIONS)

    def test_target_delivery_capabilities_require_native_or_ready_runtimes(self) -> None:
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

        with patch.dict(
            agent_server.RUNTIME_DIAGNOSTICS,
            {
                agent_server.BACKEND_CURSOR: {
                    "backend": agent_server.BACKEND_CURSOR,
                    "status": "ready",
                    "available": True,
                    "installed": True,
                    "authenticated": True,
                    "_executable": "/test/bin/agent",
                }
            },
            clear=True,
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities(
                    {"backend": "cursor"}
                ),
                [],
            )
        for status in ("unknown", "missing", "unauthenticated", "error"):
            with self.subTest(cursor_status=status), patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": status,
                    }
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    HTTPException,
                    "compatible authenticated headless Cursor CLI",
                ):
                    agent_server.cross_chat_delivery_client_capabilities(
                        {"backend": "cursor"}
                    )

    def test_handoff_capability_advertises_only_admitted_target_backends(self) -> None:
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC),
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=False),
            patch.dict(agent_server.RUNTIME_DIAGNOSTICS, {}, clear=True),
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
            patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": "ready",
                        "available": True,
                        "installed": True,
                        "authenticated": True,
                        "_executable": "/test/bin/agent",
                    }
                },
                clear=True,
            ),
        ):
            available = agent_server.cross_chat_handoffs_capability()
        self.assertTrue(available["available"])
        self.assertEqual(
            available["supported_target_backends"],
            [
                agent_server.BACKEND_CODEX,
                agent_server.BACKEND_CLAUDE,
                agent_server.BACKEND_CURSOR,
            ],
        )
        self.assertEqual(
            available["required_target_transports"][agent_server.BACKEND_CURSOR],
            "headless-stream-json",
        )

    def test_agent_helper_bypass_allowlist_covers_exact_registered_routes(self) -> None:
        registered = []
        for route in agent_server.app.routes:
            path = str(getattr(route, "path", ""))
            if not path.startswith("/api/agent/"):
                continue
            for method in set(getattr(route, "methods", set())) - {"HEAD", "OPTIONS"}:
                registered.append((method, path))
                sample = (
                    path.replace("{session_id}", "sess")
                    .replace("{job_id}", "job")
                    .replace(
                        "{route_id}",
                        (
                            "mail_0123456789abcdef0123456789abcdef"
                            if path.startswith("/api/agent/team-mail/")
                            else "route_0123456789abcdef0123456789abcdef"
                        ),
                    )
                )
                self.assertTrue(agent_server.is_agent_helper_route(method, sample))
        self.assertTrue(registered)
        self.assertFalse(
            agent_server.is_agent_helper_route("POST", "/api/agent/future-route")
        )

    async def test_internal_target_admission_has_safe_fifo_queue_projection(self) -> None:
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
                    "queued_id": "queued_secure_peer",
                    "prompt": "opaque remote envelope",
                    "purpose": "secure_peer_handoff_delivery",
                },
                {
                    "queued_id": "queued_internal",
                    "prompt": "private provider wrapper",
                    "display_prompt": "Agent-authored same-server handoff",
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_envelope_id": "handoff_hidden",
                },
                {
                    "queued_id": "queued_user",
                    "prompt": "visible",
                    "purpose": None,
                },
                {
                    "queued_id": "queued_exchange_internal",
                    "prompt": "internal exchange",
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_exchange_id": "exchange_hidden",
                    "cross_chat_exchange_leg_id": "leg_hidden",
                },
            ])
        snapshot = await agent_server.queued_turns_snapshot("target")
        self.assertEqual(
            [item["queued_id"] for item in snapshot],
            ["queued_internal", "queued_user", "queued_exchange_internal"],
        )
        self.assertEqual([item["position"] for item in snapshot], [2, 3, 4])
        self.assertEqual(
            snapshot[0]["prompt"],
            "Agent-authored same-server handoff",
        )
        self.assertEqual(snapshot[2]["prompt"], "Incoming cross-chat message")
        self.assertNotIn("private provider wrapper", repr(snapshot))
        self.assertNotIn("internal exchange", repr(snapshot))

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

        exchange_internal = {
            **internal,
            "cross_chat_envelope_id": None,
            "cross_chat_exchange_id": "exchange_hidden",
            "cross_chat_exchange_leg_id": "leg_hidden",
            "exchange_id": "exchange_hidden",
            "exchange_leg_id": "leg_hidden",
        }
        self.assertFalse(agent_server.is_client_visible_event(exchange_internal))
        self.assertIsNone(
            agent_server.history_search_event_record(exchange_internal)
        )
        self.assertTrue(agent_server.is_client_visible_event({
            **exchange_internal,
            "type": "reasoning_summary",
            "text": "Visible exchange reasoning",
        }))
        self.assertTrue(agent_server.is_client_visible_event({
            **exchange_internal,
            "type": "assistant_text",
            "text": "Visible exchange answer",
        }))

        exchange_file = self.root / "exchange-client-events.jsonl"
        exchange_events = [
            {
                "id": "exchange-normal",
                "seq": 1,
                "session_id": "target",
                "type": "assistant_text",
                "text": "ordinary",
            },
            {**exchange_internal, "id": "exchange-start", "seq": 2},
            {
                "id": "exchange-lifecycle",
                "seq": 3,
                "session_id": "target",
                "type": "cross_chat_exchange_leg_started",
                "exchange_id": "exchange_hidden",
                "exchange_leg_id": "leg_hidden",
                "exchange_status": "active",
                "exchange_leg_status": "running",
                "message": "Working in this chat.",
            },
            {
                **exchange_internal,
                "id": "exchange-reasoning",
                "seq": 4,
                "type": "reasoning_summary",
                "text": "Visible exchange reasoning",
            },
            {
                **exchange_internal,
                "id": "exchange-answer",
                "seq": 5,
                "type": "assistant_text",
                "text": "Visible exchange answer",
            },
            {
                **exchange_internal,
                "id": "exchange-queue",
                "seq": 6,
                "type": "turn_queued",
            },
        ]
        exchange_file.write_text(
            "".join(json.dumps(event) + "\n" for event in exchange_events),
            encoding="utf-8",
        )
        with (
            patch.object(agent_server, "events_path", return_value=exchange_file),
            patch.object(
                agent_server,
                "TIMELINE_INDEX_CACHE",
                agent_server.OrderedDict(),
            ),
        ):
            default_page = agent_server.read_client_events_page(
                "target", limit=10
            )
            visible_page = agent_server.read_visible_events_page(
                "target", limit=10
            )
            catchup_page = agent_server.read_event_catchup_batch(
                "target", after=0, through=6, limit=10
            )
            semantic_page = agent_server.read_semantic_timeline_page(
                "target", limit=10, tail=True
            )
        self.assertEqual(
            [event["seq"] for event in default_page[0]],
            [1, 3, 4, 5],
        )
        self.assertEqual(
            [event["seq"] for event in visible_page[0]],
            [1, 3, 4, 5],
        )
        self.assertEqual(
            [event["seq"] for event in catchup_page[0]],
            [1, 3, 4, 5],
        )
        semantic_types = [event["type"] for event in semantic_page["events"]]
        self.assertNotIn("turn_started", semantic_types)
        self.assertNotIn("turn_queued", semantic_types)
        self.assertIn("reasoning_summary", semantic_types)
        self.assertIn("assistant_text", semantic_types)

        exchange_broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "events_path",
                return_value=self.root / "exchange-live.jsonl",
            ),
            patch.object(agent_server, "next_event_seq", AsyncMock(side_effect=[1, 2])),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", exchange_broadcast),
        ):
            await agent_server.append_event("target", "turn_started", {
                key: value for key, value in exchange_internal.items()
                if key not in {"id", "seq", "session_id", "type"}
            })
            await agent_server.append_event("target", "assistant_text", {
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_exchange_id": "exchange_hidden",
                "cross_chat_exchange_leg_id": "leg_hidden",
                "text": "Visible exchange answer",
            })
        exchange_broadcast.assert_awaited_once()
        self.assertEqual(
            exchange_broadcast.await_args.args[1]["type"],
            "assistant_text",
        )

        batch_broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "events_path",
                return_value=self.root / "exchange-live-batch.jsonl",
            ),
            patch.object(agent_server, "next_event_seq", AsyncMock(return_value=1)),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", batch_broadcast),
        ):
            await agent_server.append_durable_event_batch("target", [
                (
                    "turn_queued",
                    {
                        "purpose": "cross_chat_handoff_delivery",
                        "cross_chat_exchange_id": "exchange_hidden",
                        "cross_chat_exchange_leg_id": "leg_hidden",
                        "prompt": "Synthetic exchange queue row",
                    },
                ),
                (
                    "assistant_text",
                    {
                        "purpose": "cross_chat_handoff_delivery",
                        "cross_chat_exchange_id": "exchange_hidden",
                        "cross_chat_exchange_leg_id": "leg_hidden",
                        "text": "Visible exchange batch answer",
                    },
                ),
            ])
        batch_broadcast.assert_awaited_once()
        self.assertEqual(
            batch_broadcast.await_args.args[1]["type"],
            "assistant_text",
        )

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

    async def test_exchange_recovery_preserves_explicit_child_after_parent_owner_loss(self) -> None:
        exchange, parent = await self.create_exchange("exchange_explicit_restart")
        parent = await agent_server.CROSS_CHAT.update_exchange_leg(
            parent["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target",
        )
        exchange, child, created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=parent["id"],
            source_session_id="target",
            source_run_id="run_target",
            body="Can you clarify?",
            request_response=True,
            idempotency_key="explicit-child",
            automatic=False,
        )
        self.assertTrue(created)
        submit = AsyncMock()
        with (
            patch.object(agent_server, "live_cross_chat_exchange_leg_state", AsyncMock(return_value=None)),
            patch.object(agent_server, "cross_chat_exchange_events", return_value=[]),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", submit),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        parent = await agent_server.CROSS_CHAT.get_exchange_leg(parent["id"])
        exchange = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(parent["status"], "delivered")
        self.assertEqual(parent["response_state"], "explicit_committed")
        self.assertEqual(exchange["status"], "active")
        self.assertEqual(exchange["active_leg_id"], child["id"])
        self.assertTrue(any(
            call.args[1]["id"] == child["id"] for call in submit.await_args_list
        ))

    async def test_exchange_recovery_replays_auto_response_after_delivered_commit(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_auto_restart")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target",
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"], status="delivered"
        )
        terminal = {
            "type": "turn_finished",
            "run_id": "run_target",
            "purpose": "cross_chat_handoff_delivery",
            "exchange_id": exchange["id"],
            "exchange_leg_id": inbound["id"],
            "result_text": "Recovered answer",
            "exit_code": 0,
            "stopped": False,
        }

        async def mark_submitting(current_exchange, leg):
            await agent_server.CROSS_CHAT.update_exchange_leg(
                leg["id"], expected={"registered"}, status="submitting"
            )
            return current_exchange, leg

        with (
            patch.object(agent_server, "cross_chat_exchange_events", return_value=[terminal]),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock(side_effect=mark_submitting)),
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0]["response_state"], "automatic_committed")
        self.assertEqual(legs[1]["parent_leg_id"], inbound["id"])
        self.assertEqual(legs[1]["body"], "Recovered answer")

    async def test_exchange_failure_status_wakes_current_waiting_sender_in_both_directions(self) -> None:
        async def capture_direction(exchange, failed_leg):
            with (
                patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
                patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
            ):
                await agent_server.maybe_deliver_cross_chat_exchange_failure_status(
                    exchange,
                    failed_session_id=failed_leg["target_session_id"],
                    failed_leg=failed_leg,
                )
            return next(
                leg for leg in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
                if leg["kind"] == "status"
            )

        first_exchange, first_leg = await self.create_exchange("exchange_status_forward")
        failed = await agent_server.CROSS_CHAT.finish_exchange_leg(
            first_leg["id"],
            status="failed",
            error_code="target_failed",
            error="B failed",
        )
        first_status = await capture_direction(*failed)
        self.assertEqual(
            (first_status["source_session_id"], first_status["target_session_id"]),
            ("target", "source"),
        )

        reverse_exchange, reverse_parent = await self.create_exchange("exchange_status_reverse")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reverse_parent["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target_reverse",
        )
        reverse_exchange, reverse_leg, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=reverse_exchange["id"],
            inbound_leg_id=reverse_parent["id"],
            source_session_id="target",
            source_run_id="run_target_reverse",
            body="A follow-up question",
            request_response=True,
            idempotency_key="reverse-follow-up",
            automatic=False,
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reverse_leg["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_source_reverse",
        )
        failed = await agent_server.CROSS_CHAT.finish_exchange_leg(
            reverse_leg["id"],
            status="failed",
            error_code="target_failed",
            error="A failed",
        )
        reverse_status = await capture_direction(*failed)
        self.assertEqual(
            (reverse_status["source_session_id"], reverse_status["target_session_id"]),
            ("source", "target"),
        )

    async def test_exchange_reconcile_creates_missing_failure_status_outbox(self) -> None:
        exchange, leg = await self.create_exchange("exchange_status_outbox")
        exchange, leg = await agent_server.CROSS_CHAT.finish_exchange_leg(
            leg["id"],
            status="failed",
            error_code="target_failed",
            error="crashed before status wake",
        )
        self.assertFalse(any(
            item["kind"] == "status"
            for item in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        ))
        order = []

        async def leg_lifecycle(*_args, **_kwargs):
            order.append("leg")

        async def exchange_lifecycle(*_args, **_kwargs):
            order.append("exchange")

        async def submit_status(*_args, **_kwargs):
            order.append("wake")

        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock(side_effect=leg_lifecycle)),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock(side_effect=exchange_lifecycle)),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock(side_effect=submit_status)),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
            await agent_server.reconcile_cross_chat_exchanges()
        status_legs = [
            item for item in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
            if item["kind"] == "status"
        ]
        self.assertEqual(len(status_legs), 1)
        self.assertEqual(status_legs[0]["target_session_id"], "source")
        self.assertLess(order.index("exchange"), order.index("wake"))

    async def test_exchange_cancel_route_returns_leg_bodies(self) -> None:
        exchange, _leg = await self.create_exchange("exchange_cancel_body")
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            response = await agent_server.post_cancel_cross_chat_exchange(exchange["id"])
        self.assertEqual(response["exchange"]["legs"][0]["body"], "Please investigate")

    async def test_exchange_budget_rejection_releases_cap_for_terminal_fallback(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_budget_fallback")
        for ordinal in range(2, 6):
            source_session = inbound["target_session_id"]
            source_run = f"run_budget_{ordinal}"
            await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"registered"},
                status="running",
                target_run_id=source_run,
            )
            exchange, inbound, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id=source_session,
                source_run_id=source_run,
                body=f"Follow-up {ordinal}",
                request_response=True,
                idempotency_key=f"budget-{ordinal}",
                automatic=False,
            )
        self.assertEqual(exchange["used_legs"], 5)
        response_run = "run_budget_final"
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id=response_run,
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            inbound["target_session_id"],
            response_run,
            [],
            actions={"cross_chat_response"},
            exchange_response_grants={(exchange["id"], inbound["id"])},
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        agent_server.CURRENT_TURNS = {
            inbound["target_session_id"]: {"run_id": response_run}
        }
        with self.assertRaises(HTTPException) as raised:
            await agent_server.create_authorized_cross_chat_exchange_response(
                token,
                exchange["id"],
                agent_server.CrossChatExchangeResponseRequest(
                    inbound_leg_id=inbound["id"],
                    body="One more question",
                    request_response=True,
                    idempotency_key="budget-too-large",
                ),
            )
        self.assertEqual(raised.exception.detail, "budget_exhausted")
        exchange, terminal, created = await agent_server.create_authorized_cross_chat_exchange_response(
            token,
            exchange["id"],
            agent_server.CrossChatExchangeResponseRequest(
                inbound_leg_id=inbound["id"],
                body="Final answer instead",
                request_response=False,
                idempotency_key="budget-terminal-fallback",
            ),
        )
        self.assertTrue(created)
        self.assertEqual(exchange["used_legs"], 6)
        self.assertFalse(bool(terminal["expects_reply"]))

    async def test_exchange_expiry_is_committed_before_initial_and_response_410(self) -> None:
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_expired_unsent",
            requester_session_id="source",
            authorization_source_run_id="run_expired_unsent",
            responder_session_id="target",
            max_legs=6,
            expires_at="2000-01-01T00:00:00Z",
        )
        with self.assertRaises(HTTPException) as initial_error:
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_expired_unsent",
                source_session_id="source",
                source_run_id="run_expired_unsent",
                target_session_id="target",
                body="too late",
                idempotency_key="expired-unsent",
            )
        self.assertEqual(initial_error.exception.status_code, 410)
        unsent = await agent_server.CROSS_CHAT.get_exchange("exchange_expired_unsent")
        self.assertEqual(unsent["status"], "expired")

        exchange, inbound = await self.create_exchange("exchange_expired_response")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_expired_response",
        )
        with patch.object(agent_server, "now_iso", return_value="2100-01-01T00:00:00Z"):
            with self.assertRaises(HTTPException) as response_error:
                await agent_server.CROSS_CHAT.commit_exchange_response(
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    source_session_id="target",
                    source_run_id="run_expired_response",
                    body="too late too",
                    request_response=False,
                    idempotency_key="expired-response",
                    automatic=False,
                )
        self.assertEqual(response_error.exception.status_code, 410)
        active = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(active["status"], "expired")
        pending_exchanges, _pending_legs = await agent_server.CROSS_CHAT.pending_exchange_lifecycle()
        self.assertEqual(
            {item["id"] for item in pending_exchanges},
            {"exchange_expired_unsent", "exchange_expired_response"},
        )

    async def test_exchange_late_terminal_after_user_cancel_never_wakes_or_replies(self) -> None:
        exchange, leg = await self.create_exchange("exchange_cancel_late_failure")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="running", target_run_id="run_late_failure"
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        wake = AsyncMock()
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "maybe_deliver_cross_chat_exchange_failure_status", wake),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_late_failure",
                "purpose": "cross_chat_handoff_delivery",
                "exchange_id": exchange["id"],
                "exchange_leg_id": leg["id"],
                "result_text": "",
                "exit_code": 1,
                "stopped": True,
            })
        wake.assert_not_awaited()
        cancelled = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)

        exchange, leg = await self.create_exchange("exchange_cancel_late_success")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="running", target_run_id="run_late_success"
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        terminal_lifecycle = AsyncMock()
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", terminal_lifecycle),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_late_success",
                "purpose": "cross_chat_handoff_delivery",
                "exchange_id": exchange["id"],
                "exchange_leg_id": leg["id"],
                "result_text": "Late answer",
                "exit_code": 0,
                "stopped": False,
            })
        self.assertIn("cancelled", terminal_lifecycle.await_args.args[1].lower())
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)

    async def test_participant_cleanup_cannot_override_completed_exchange(self) -> None:
        exchange, parent = await self.create_exchange("exchange_delete_loses")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            parent["id"], expected={"registered"}, status="running", target_run_id="run_delete_parent"
        )
        exchange, answer, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=parent["id"],
            source_session_id="target",
            source_run_id="run_delete_parent",
            body="Done",
            request_response=False,
            idempotency_key="delete-answer",
            automatic=False,
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            answer["id"], expected={"registered"}, status="running", target_run_id="run_delete_answer"
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(answer["id"], status="delivered")
        stale = {**exchange, "status": "active", "active_leg_id": answer["id"]}
        schedule = AsyncMock()
        with (
            patch.object(agent_server.CROSS_CHAT, "nonterminal_exchanges_for_session", AsyncMock(return_value=[stale])),
            patch.object(agent_server, "schedule_cross_chat_exchange_failure_status_after_unlock", schedule),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            count = await agent_server.terminalize_cross_chat_exchanges_for_session(
                "source", archived=False
            )
        self.assertEqual(count, 0)
        schedule.assert_not_called()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "completed")
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            expired = await agent_server.fail_cross_chat_exchange(
                exchange["id"],
                leg_id=answer["id"],
                leg_status="expired",
                error_code="expired",
                error="stale expiry snapshot",
            )
        self.assertIsNone(expired)
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"],
            "completed",
        )

    async def test_reconcile_removes_cancelled_hidden_queue_owner_only(self) -> None:
        exchange, leg = await self.create_exchange("exchange_cancel_queue_crash")
        leg = await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_hidden_exchange",
            queue_position=2,
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        paused = {"queued_id": "queued_user", "prompt": "later", "_paused_after_stop": True}
        hidden = {
            "queued_id": "queued_hidden_exchange",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": leg["id"],
            "source_session_id": "source",
            "target_session_id": "target",
        }
        agent_server.QUEUED_TURNS = {"target": deque([paused, hidden])}
        with (
            patch.object(agent_server, "append_durable_event", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [paused])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertIsNone(durable_leg["queued_id"])

    async def test_reconcile_preserves_user_cancel_for_durably_unqueued_source(self) -> None:
        exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_unqueue_crash",
            requester_session_id="source",
            authorization_source_run_id="queued_source_exchange",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        event_file = self.root / "source-unqueue.jsonl"
        event_file.write_text(json.dumps({
            "type": "turn_unqueued",
            "queued_id": "queued_source_exchange",
            "cross_chat_exchange_ids": [exchange["id"]],
        }) + "\n")
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "cancelled")
        self.assertEqual(durable["error_code"], "cancelled_by_user")

    async def test_reconcile_cancels_exchange_removed_by_durable_queue_edit(self) -> None:
        old_exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_edit_old",
            requester_session_id="source",
            authorization_source_run_id="queued_edit_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        new_exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_edit_new",
            requester_session_id="source",
            authorization_source_run_id="queued_edit_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        agent_server.QUEUED_TURNS = {"source": deque([{
            "queued_id": "queued_edit_source",
            "cross_chat_exchange_ids": [new_exchange["id"]],
            "_durable": True,
        }])}
        with patch.object(
            agent_server,
            "append_cross_chat_exchange_terminal_lifecycle",
            AsyncMock(),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        old_exchange = await agent_server.CROSS_CHAT.get_exchange(old_exchange["id"])
        new_exchange = await agent_server.CROSS_CHAT.get_exchange(new_exchange["id"])
        self.assertEqual(old_exchange["status"], "cancelled")
        self.assertEqual(old_exchange["error_code"], "cancelled_by_user")
        self.assertEqual(new_exchange["status"], "waiting_request")

    async def test_failure_status_wake_waits_for_target_lifecycle_unlock(self) -> None:
        exchange, leg = await self.create_exchange("exchange_deferred_status_lock")
        lock = agent_server.session_lifecycle_lock("target")
        await lock.acquire()
        deliver = AsyncMock()
        try:
            with patch.object(
                agent_server,
                "maybe_deliver_cross_chat_exchange_failure_status",
                deliver,
            ):
                agent_server.schedule_cross_chat_exchange_failure_status_after_unlock(
                    exchange,
                    failed_session_id="target",
                    failed_leg=leg,
                )
                await asyncio.sleep(0)
                deliver.assert_not_awaited()
                lock.release()
                for _ in range(20):
                    if deliver.await_count:
                        break
                    await asyncio.sleep(0)
                deliver.assert_awaited_once()
        finally:
            if lock.locked():
                lock.release()

    def test_exchange_capability_v7_default_deny_contract_is_exact(self) -> None:
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_APP_SERVER),
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AGENT_SDK),
        ):
            capability = agent_server.cross_chat_handoffs_capability()
        self.assertTrue(capability["available"])
        self.assertEqual(capability["version"], 7)
        self.assertEqual(
            capability["actions"],
            [
                "route",
                "request_reply",
                "instruction",
                "final_result",
            ],
        )
        self.assertEqual(capability["default_action"], "route")
        self.assertEqual(capability["max_exchange_legs"], 6)
        self.assertEqual(capability["default_exchange_ttl_seconds"], 72 * 60 * 60)
        self.assertFalse(capability["features"]["direct_message_mentions"])
        self.assertTrue(capability["features"]["route_mentions"])
        self.assertTrue(capability["features"]["route_hint_mentions"])
        self.assertTrue(capability["features"]["durable_route_grants"])
        self.assertTrue(capability["features"]["agent_cross_chat_routes"])
        self.assertFalse(
            capability["features"]["agent_ambient_local_handoffs"]
        )
        self.assertEqual(
            capability["ambient_local_handoffs"]["policy"],
            "default_deny",
        )
        self.assertEqual(
            capability["ambient_local_handoffs"]["scope"],
            "explicit_source_grants",
        )
        self.assertFalse(
            capability["ambient_local_handoffs"]["setup_required"]
        )
        self.assertFalse(capability["ambient_local_handoffs"]["enabled"])
        self.assertEqual(
            capability["agent_routes"]["client_capability"],
            agent_server.AGENT_CROSS_CHAT_ROUTES_CLIENT_CAPABILITY,
        )
        self.assertEqual(capability["agent_routes"]["max_routes_per_chat"], 16)
        self.assertFalse(capability["agent_routes"]["transcript_access"])
        self.assertEqual(capability["agent_routes"]["policy"], "default_deny")
        self.assertTrue(capability["agent_routes"]["durable"])
        self.assertTrue(capability["agent_routes"]["directional"])
        self.assertTrue(
            capability["agent_routes"]["revoke_requires_revision"]
        )

    async def test_request_reply_capability_uses_exact_exchange_generation(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="request_reply",
        )
        old_ids = await agent_server.register_request_reply_exchanges(
            "source", "queued_generation", [reference]
        )
        await agent_server.CROSS_CHAT.update_exchange(
            old_ids[0],
            expected={"waiting_request"},
            status="cancelled",
            error_code="cancelled_by_user",
            error="edited away",
        )
        new_ids = await agent_server.register_request_reply_exchanges(
            "source", "run_generation", [reference]
        )
        self.assertNotEqual(old_ids, new_ids)
        authority_path = await agent_server.issue_cross_chat_capability(
            "source",
            "run_generation",
            [reference],
            exchange_request_grants={"target": new_ids[0]},
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        handle = self.direct_grant_handle(
            authority_path,
            action="request_reply",
        )
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_generation"}}
        request = agent_server.CrossChatHandoffRequest(
            target_session_id=handle,
            action="request_reply",
            body="Please answer",
            idempotency_key="exact-generation-key",
        )
        first, created = await agent_server.create_authorized_cross_chat_instruction(
            token, request
        )
        replay, replay_created = await agent_server.create_authorized_cross_chat_instruction(
            token, request
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["exchange"]["id"], new_ids[0])
        self.assertEqual(replay["leg"]["id"], first["leg"]["id"])
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get_exchange(old_ids[0]))["status"],
            "cancelled",
        )

    async def test_exchange_explicit_and_automatic_response_cas_both_orderings(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_explicit_wins")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_explicit_wins"
        )
        _exchange, explicit, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_explicit_wins",
            body="Explicit answer",
            request_response=False,
            idempotency_key="explicit-wins-key",
            automatic=False,
        )
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_explicit_wins",
                "exchange_id": exchange["id"],
                "exchange_leg_id": inbound["id"],
                "result_text": "Automatic should lose",
                "exit_code": 0,
                "stopped": False,
            })
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual([leg["id"] for leg in legs], [inbound["id"], explicit["id"]])

        exchange, inbound = await self.create_exchange("exchange_auto_wins")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_auto_wins"
        )
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_auto_wins",
                "exchange_id": exchange["id"],
                "exchange_leg_id": inbound["id"],
                "result_text": "Automatic answer",
                "exit_code": 0,
                "stopped": False,
            })
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id="target",
                source_run_id="run_auto_wins",
                body="Late explicit",
                request_response=False,
                idempotency_key="late-explicit-key",
                automatic=False,
            )
        self.assertEqual(raised.exception.detail, "response_already_committed")

    async def test_request_reply_normal_pipeline_auto_returns_then_completes(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="request_reply",
        )
        exchange_ids = await agent_server.register_request_reply_exchanges(
            "source", "run_pipeline_source", [reference]
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source",
            "run_pipeline_source",
            [reference],
            exchange_request_grants={"target": exchange_ids[0]},
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        handle = self.direct_grant_handle(
            authority_path,
            action="request_reply",
        )
        agent_server.CURRENT_TURNS = {
            "source": {"run_id": "run_pipeline_source"},
        }
        created, was_created = await agent_server.create_authorized_cross_chat_instruction(
            token,
            agent_server.CrossChatHandoffRequest(
                target_session_id=handle,
                action="request_reply",
                body="Please investigate the failure",
                idempotency_key="pipeline-initial-request",
            ),
        )
        self.assertTrue(was_created)
        exchange = created["exchange"]
        inbound = created["leg"]
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_pipeline_target",
        )

        async def start_return(_exchange, outbound):
            return (
                _exchange,
                await agent_server.CROSS_CHAT.update_exchange_leg(
                    outbound["id"],
                    expected={"registered"},
                    status="running",
                    target_run_id="run_pipeline_return",
                ),
            )

        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
            patch.object(
                agent_server,
                "submit_cross_chat_exchange_leg",
                AsyncMock(side_effect=start_return),
            ),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_pipeline_target",
                "exchange_id": exchange["id"],
                "exchange_leg_id": inbound["id"],
                "result_text": "The target answer",
                "exit_code": 0,
                "stopped": False,
            })
            legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
            self.assertEqual(len(legs), 2)
            outbound = legs[1]
            self.assertEqual(outbound["status"], "running")
            self.assertFalse(bool(outbound["expects_reply"]))
            self.assertEqual(outbound["target_session_id"], "source")

            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_pipeline_return",
                "exchange_id": exchange["id"],
                "exchange_leg_id": outbound["id"],
                "result_text": "Received and understood",
                "exit_code": 0,
                "stopped": False,
            })

        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(durable["status"], "completed")
        self.assertEqual([leg["status"] for leg in legs], ["delivered", "delivered"])
        self.assertEqual(legs[0]["response_state"], "automatic_committed")
        self.assertEqual(legs[1]["response_state"], "closed")

    async def test_unsent_request_reply_placeholder_fails_visibly_on_source_terminal(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="request_reply",
        )
        exchange_ids = await agent_server.register_request_reply_exchanges(
            "source", "run_unsent_request", [reference]
        )
        lifecycle = AsyncMock()
        with patch.object(
            agent_server,
            "append_cross_chat_exchange_terminal_lifecycle",
            lifecycle,
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_unsent_request",
                "result_text": "The source turn finished without calling ask",
                "exit_code": 0,
                "stopped": False,
            })
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange_ids[0])
        self.assertEqual(durable["status"], "failed")
        self.assertEqual(durable["error_code"], "exchange_not_sent")
        lifecycle.assert_awaited_once()
        self.assertEqual(lifecycle.await_args.args[0]["id"], exchange_ids[0])
        self.assertIn("not sent", lifecycle.await_args.args[1].lower())

    async def test_exchange_explicit_response_vs_failed_terminal_both_orderings(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_explicit_before_failure")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_explicit_failure"
        )
        exchange, child, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_explicit_failure",
            body="Continue anyway",
            request_response=True,
            idempotency_key="explicit-before-failure",
            automatic=False,
        )
        exchange, inbound = await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"],
            status="failed",
            error_code="target_stopped",
            error="provider stopped",
            preserve_committed_response=True,
        )
        self.assertEqual(inbound["status"], "delivered")
        self.assertEqual(exchange["status"], "active")
        self.assertEqual(exchange["active_leg_id"], child["id"])

        exchange, inbound = await self.create_exchange("exchange_failure_before_explicit")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_failure_first"
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"],
            status="failed",
            error_code="target_stopped",
            error="provider stopped",
            preserve_committed_response=True,
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id="target",
                source_run_id="run_failure_first",
                body="Too late",
                request_response=True,
                idempotency_key="explicit-after-failure",
                automatic=False,
            )
        self.assertEqual(raised.exception.detail, "response_already_committed")

    async def test_terminal_reply_can_receive_explicit_follow_up(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_follow_terminal_reply")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_reply"
        )
        exchange, reply, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_reply",
            body="Initial answer",
            request_response=False,
            idempotency_key="initial-answer-key",
            automatic=False,
        )
        self.assertFalse(bool(reply["expects_reply"]))
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reply["id"], expected={"registered"}, status="running", target_run_id="run_follow"
        )
        exchange, follow_up, created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=reply["id"],
            source_session_id="source",
            source_run_id="run_follow",
            body="Please clarify one thing",
            request_response=True,
            idempotency_key="follow-up-key",
            automatic=False,
        )
        self.assertTrue(created)
        self.assertTrue(bool(follow_up["expects_reply"]))
        self.assertEqual(follow_up["target_session_id"], "target")

    async def test_status_leg_is_non_budget_and_has_no_response_semantics(self) -> None:
        exchange, leg = await self.create_exchange("exchange_status_budget")
        before = int(exchange["used_legs"])
        status, created = await agent_server.CROSS_CHAT.create_exchange_status_leg(
            exchange_id=exchange["id"],
            source_session_id="target",
            target_session_id="source",
            body="Target failed",
            error_code="target_failed",
        )
        self.assertTrue(created)
        current = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(current["used_legs"], before)
        self.assertEqual(status["ordinal"], 0)
        self.assertFalse(bool(status["expects_reply"]))
        self.assertEqual(status["response_state"], "closed")

    async def test_automatic_exchange_response_enforces_explicit_body_limit(self) -> None:
        async def finish_with_size(exchange_id: str, size: int):
            exchange, inbound = await self.create_exchange(exchange_id)
            run_id = f"run_{exchange_id}"
            await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"registered"},
                status="running",
                target_run_id=run_id,
            )
            wake = AsyncMock()
            with (
                patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
                patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
                patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
                patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
                patch.object(agent_server, "maybe_deliver_cross_chat_exchange_failure_status", wake),
            ):
                await agent_server.finalize_cross_chat_exchange_run({
                    "run_id": run_id,
                    "exchange_id": exchange["id"],
                    "exchange_leg_id": inbound["id"],
                    "result_text": "x" * size,
                    "exit_code": 0,
                    "stopped": False,
                })
            return (
                await agent_server.CROSS_CHAT.get_exchange(exchange["id"]),
                await agent_server.CROSS_CHAT.exchange_legs(exchange["id"]),
                wake,
            )

        accepted, accepted_legs, accepted_wake = await finish_with_size(
            "exchange_body_limit_ok",
            agent_server.CROSS_CHAT_EXCHANGE_BODY_MAX_CHARS,
        )
        self.assertEqual(accepted["status"], "active")
        self.assertEqual(len(accepted_legs), 2)
        accepted_wake.assert_not_awaited()
        rejected, rejected_legs, rejected_wake = await finish_with_size(
            "exchange_body_limit_reject",
            agent_server.CROSS_CHAT_EXCHANGE_BODY_MAX_CHARS + 1,
        )
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["error_code"], "response_too_large")
        self.assertEqual(len(rejected_legs), 1)
        rejected_wake.assert_awaited_once()

    async def test_archived_target_discards_queued_exchange_status_leg(self) -> None:
        agent_server.STORE.sessions["target"]["archived"] = True
        item = {
            "queued_id": "queued_status_archived",
            "prompt": "status",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_exchange_id": "exchange_status_archived",
            "cross_chat_exchange_leg_id": "leg_status_archived",
            "cross_chat_exchange_status": True,
            "source_session_id": "source",
            "target_session_id": "target",
            "_durable": True,
            "_paused_after_stop": False,
        }
        agent_server.QUEUED_TURNS = {"target": deque([item])}
        discard = AsyncMock()
        with (
            patch.object(
                agent_server,
                "_start_turn_locked",
                AsyncMock(side_effect=HTTPException(status_code=409, detail="chat is archived")),
            ),
            patch.object(agent_server, "terminally_discard_queued_turn", discard),
        ):
            await agent_server.start_next_queued_turn("target")
        discard.assert_awaited_once_with("target", item, "chat is archived")

    async def test_automatic_return_target_unavailable_wakes_waiting_sender(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_auto_target_gone")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_auto_sender"
        )
        exchange, child, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_auto_sender",
            body="Automatic return",
            request_response=False,
            idempotency_key="auto-target-gone",
            automatic=True,
        )
        agent_server.STORE.sessions.pop("source")
        wake = AsyncMock()
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "maybe_deliver_cross_chat_exchange_failure_status", wake),
        ):
            with self.assertRaises(HTTPException):
                await agent_server.submit_cross_chat_exchange_leg(exchange, child)
        wake.assert_awaited_once()
        self.assertEqual(wake.await_args.kwargs["failed_session_id"], "source")
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "failed")


if __name__ == "__main__":
    unittest.main()
