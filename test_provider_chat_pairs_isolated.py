"""Permanent chat permission tests without importing the server or live storage."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime
import logging
import hashlib
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid
import unicodedata
import team_mail_grants


TREE = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
FUNCTIONS = {
    "canonical_provider_cross_chat_route_alias", "canonical_provider_cross_chat_route_actions",
    "normalized_provider_cross_chat_routes", "stored_provider_cross_chat_routes",
    "normalized_pending_provider_cross_chat_grant", "provider_cross_chat_routes",
    "normalized_provider_cross_chat_route_audit", "provider_cross_chat_route_audit_entry",
    "durable_provider_cross_chat_route_actions", "next_durable_provider_cross_chat_route_alias",
    "local_route_hint_target_ids", "persist_provider_cross_chat_pair_grants",
    "persist_durable_provider_cross_chat_reference_grants", "rollback_durable_provider_cross_chat_reference_grants",
    "commit_durable_provider_cross_chat_reference_grants", "provider_cross_chat_grant_admission_event",
    "reconcile_pending_provider_cross_chat_grant", "initial_provider_cross_chat_route_snapshot",
    "provider_cross_chat_route_snapshot_for_hints", "normalized_provider_cross_chat_route_snapshot",
    "provider_cross_chat_pair_is_live", "live_provider_cross_chat_route",
    "provider_cross_chat_route_availability", "pending_admission_provider_cross_chat_route",
    "list_agent_handoff_routes", "create_agent_handoff_route", "delete_agent_handoff_route", "reject_unavailable_route_target",
    "provider_cross_chat_delivery_pair_is_live", "admit_cross_chat_delivery_run",
    "provider_cross_chat_route_id_is_revoked", "retire_deleted_provider_cross_chat_pairs",
    "deliver_cross_chat_live_response_locked", "reconcile_cross_chat_handoffs", "reconcile_cross_chat_exchange_leg",
    "async_route_delivery_snapshot", "is_async_route_message", "async_route_conversation_fields",
    "async_route_queue_fields", "public_queued_turn", "queued_turn_from_event", "queued_turn_run_metadata",
    "async_message_target_fields",
    "sanitized_provider_route_label", "enqueue_turn",
    "provider_cross_chat_reciprocal_admission_fields",
    "join_task_despite_caller_cancellation",
    "stage_provider_team_mail_grants", "settle_provider_team_mail_grants",
}
CONSTANTS = {
    "CROSS_CHAT_HANDOFF_BODY_MAX_CHARS",
    "PROVIDER_CROSS_CHAT_ROUTE_ID_RE", "PROVIDER_CROSS_CHAT_ROUTE_REVISION_RE",
    "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE", "PROVIDER_CROSS_CHAT_ROUTE_ALIAS_RE",
    "PROVIDER_CROSS_CHAT_ROUTE_AUDIT_ID_RE", "PROVIDER_CROSS_CHAT_RECIPROCAL_EFFECT_ID_RE",
    "PROVIDER_CROSS_CHAT_GRANT_ADMISSION_ID_RE", "PENDING_PROVIDER_CROSS_CHAT_GRANT_KEY",
    "PROVIDER_CROSS_CHAT_ROUTE_LEGACY_CLIENT_HINT", "PROVIDER_CROSS_CHAT_ROUTE_AUDIT_LIMIT",
    "PROVIDER_CROSS_CHAT_ROUTE_ACTIONS", "PROVIDER_CROSS_CHAT_ROUTE_ACTION_SET", "PROVIDER_CROSS_CHAT_ROUTE_KIND_AMBIENT",
    "PROVIDER_CROSS_CHAT_ROUTE_KIND_REFERENCE",
}
NODES = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
        node = deepcopy(node)
        node.decorator_list = []
        NODES.append(node)
    elif isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets
    ):
        NODES.append(node)
CODE = compile(ast.fix_missing_locations(ast.Module(body=NODES, type_ignores=[])), "<isolated-chat-pairs>", "exec")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class MemoryStore:
    def __init__(self, sessions):
        self.sessions = sessions
        self._lock = asyncio.Lock()
        self.saved = []
        self.failure = None

    async def save(self, *, durable):
        self.saved.append(deepcopy(self.sessions))
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure

    async def persist_restored_state(self, *, durable):
        await self.save(durable=durable)


class ChatPairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore({sid: {
            "id": sid, "backend": "codex", "archived": False,
            "provider_cross_chat_routes": [], "provider_cross_chat_route_audit": [],
            "updated_at": "2026-09-09T00:00:00Z",
        } for sid in ("a", "b", "c", "fork")})
        self.events = {}
        self.retired = AsyncMock()
        self.namespace = {
            "team_mail_grants": team_mail_grants,
            "asyncio": asyncio, "datetime": datetime, "re": re, "uuid": uuid,
            "unicodedata": unicodedata, "deque": deque, "suppress": suppress,
            "HTTPException": HTTPException, "STORE": self.store,
            "now_iso": lambda: "2026-09-10T00:00:00Z", "logger": logging.getLogger(__name__),
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "DEFAULT_BACKEND": "codex", "VALID_BACKENDS": {"codex"},
            "BACKEND_OPENCODE": "opencode",
            "cross_chat_target_backend_supported": lambda backend: backend == "codex",
            "cross_chat_delivery_client_capabilities": lambda target: ["supported"],
            "AGENT_TOKEN": "isolated-only", "AGENT_AMBIENT_LOCAL_HANDOFFS_ENABLED": False,
            "events_path": lambda sid: sid,
            "reversed_jsonl_events": lambda sid: iter(reversed(self.events.get(sid, []))),
            "chat_reference_dict": lambda reference: vars(reference),
            "session_lifecycle_lock": self.lifecycle,
            "ensure_session_not_deleting": lambda sid: None,
            "admin_provider_cross_chat_route": lambda sid, route: route,
            "append_agent_handoff_route_audit": AsyncMock(),
            "retire_revoked_provider_route_deliveries": self.retired,
            "LOCAL_CROSS_CHAT_DELIVERY_PURPOSE": "cross_chat_handoff_delivery",
            "SECURE_PEER_DELIVERY_PURPOSE": "secure_peer_handoff_delivery",
            "CROSS_CHAT_DELIVERY_PURPOSES": {"cross_chat_handoff_delivery", "secure_peer_handoff_delivery"},
            "ASYNC_ROUTE_V1_CLIENT_CAPABILITY": "chat_conversation_async_route_v1",
            "normalized_secure_peer_route_snapshots": lambda value: [],
            "normalize_steering_lineage": lambda value: [],
        }
        exec(CODE, self.namespace)
        self.pending_key = self.namespace["PENDING_PROVIDER_CROSS_CHAT_GRANT_KEY"]

    @asynccontextmanager
    async def lifecycle(self, _session_id):
        yield

    def call(self, name, *args, **kwargs):
        return self.namespace[name](*args, **kwargs)

    def routes(self, sid):
        return self.call("provider_cross_chat_routes", self.store.sessions[sid])

    @staticmethod
    def hint(sid):
        return SimpleNamespace(session_id=sid, target_kind=None, action="route", grant_intent=True)

    async def stage(self, source="a", targets=("b",), event_type="turn_started"):
        return await self.call("persist_durable_provider_cross_chat_reference_grants", source,
                               [self.hint(sid) for sid in targets], admission_id="grant_admission_" + uuid.uuid4().hex,
                               event_type=event_type)

    def accept(self, source, mutation):
        self.events.setdefault(source, []).append({
            "type": mutation["event_type"],
            "provider_cross_chat_grant_admission_id": mutation["admission_id"],
            "provider_cross_chat_route_snapshot": deepcopy(mutation["routes"]),
        })

    async def grant(self, source="a", targets=("b",)):
        mutation = await self.stage(source, targets)
        self.accept(source, mutation)
        await self.call("commit_durable_provider_cross_chat_reference_grants", source, mutation)
        return mutation

    def add_targets(self, count):
        targets = [f"target_{index}" for index in range(count)]
        for sid in targets:
            self.store.sessions[sid] = {**deepcopy(self.store.sessions["b"]), "id": sid}
        return targets

    async def test_saved_pairs_exceed_old_count_and_audit_limits_without_truncation(self):
        targets = self.add_targets(81)
        for start in range(0, 80, 16):
            await self.grant(targets=targets[start:start + 16])
        self.store.sessions = deepcopy(self.store.sessions)  # Rebuild from persisted-shaped records.
        routes = self.routes("a")
        self.assertEqual(len(routes), 80)
        self.assertEqual(len({route["alias"] for route in routes}), 80)
        self.assertEqual(len(self.store.sessions["a"]["provider_cross_chat_route_audit"]), 64)
        self.assertEqual(self.call("initial_provider_cross_chat_route_snapshot", "a",
            SimpleNamespace(purpose=None, chat_references=[]), "chat"), routes)
        current = await self.call("list_agent_handoff_routes", "a", unlimited_routes=True)
        legacy = await self.call("list_agent_handoff_routes", "a")
        self.assertIsNone(current["max_routes"])
        self.assertEqual(legacy["max_routes"], 16)  # Compatibility metadata only.
        self.assertEqual(current["routes"], legacy["routes"])
        self.assertEqual(len(current["routes"]), 80)
        added = await self.call("create_agent_handoff_route", "a",
            SimpleNamespace(alias="manual81", target_session_id=targets[80], actions=["instruction"]))
        self.assertEqual(added["route"]["target_session_id"], targets[80])
        last = routes[-1]
        await self.call("delete_agent_handoff_route", "a", last["route_id"], last["revision"])
        self.assertEqual(len(self.routes("a")), 80)
        self.assertEqual(self.routes(targets[79]), [])
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", last))
        self.assertTrue(self.call("provider_cross_chat_pair_is_live", "a", routes[0]))

    async def test_seventeen_change_journal_recovers_acceptance_and_rollback_exactly(self):
        targets = self.add_targets(17)
        before = deepcopy(self.store.sessions)
        await self.stage(targets=targets)
        for sid in [*targets, "a"]:
            self.call("reconcile_pending_provider_cross_chat_grant", sid, self.store.sessions[sid])
        self.assertEqual(self.store.sessions, before)

        mutation = await self.stage(targets=targets, event_type="turn_queued")
        self.accept("a", mutation)
        for sid in [*targets, "a"]:
            self.assertTrue(self.call("reconcile_pending_provider_cross_chat_grant", sid, self.store.sessions[sid]))
        self.assertEqual(len(self.routes("a")), 17)
        self.assertTrue(all(self.call("provider_cross_chat_pair_is_live", "a", route) for route in self.routes("a")))

    async def test_both_directions_exist_before_any_send_but_stay_hidden_until_acceptance(self):
        mutation = await self.stage()
        self.assertEqual(len(self.store.saved), 1)
        self.assertEqual(self.routes("a"), [])
        self.assertEqual(self.routes("b"), [])
        forward = mutation["routes"][0]
        reverse = self.store.sessions["b"]["provider_cross_chat_routes"][0]
        self.assertEqual(forward["pair_id"], reverse["pair_id"])
        self.assertEqual(forward["paired_route_id"], reverse["route_id"])
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", forward))
        self.assertIsNotNone(self.call("pending_admission_provider_cross_chat_route", "a", mutation["admission_id"], forward))
        self.accept("a", mutation)
        await self.call("commit_durable_provider_cross_chat_reference_grants", "a", mutation)
        self.assertEqual(len(self.routes("a")), 1)
        self.assertEqual(len(self.routes("b")), 1)
        self.assertNotIn(self.pending_key, self.store.sessions["b"])

    async def test_future_no_mention_turn_gets_fresh_pair_snapshot_without_extending_old_snapshot(self):
        request = SimpleNamespace(purpose=None, chat_references=[])
        old_snapshot = self.call("initial_provider_cross_chat_route_snapshot", "a", request, "chat")
        await self.grant()
        new_snapshot = self.call("initial_provider_cross_chat_route_snapshot", "a", request, "chat")
        self.assertEqual(old_snapshot, [])
        self.assertEqual(new_snapshot, self.routes("a"))
        self.assertEqual(self.call("provider_cross_chat_route_snapshot_for_hints", new_snapshot, []), new_snapshot)
        self.assertEqual(self.call("initial_provider_cross_chat_route_snapshot", "a", request, "job"), [])

    async def test_pair_does_not_propagate_and_fork_has_no_authority(self):
        await self.grant("a", ("b",))
        await self.grant("b", ("c",))
        self.assertEqual({route["target_session_id"] for route in self.routes("a")}, {"b"})
        self.assertEqual({route["target_session_id"] for route in self.routes("c")}, {"b"})
        self.assertEqual(self.routes("fork"), [])

    async def test_repeated_mention_reuses_exact_pair(self):
        await self.grant()
        before = deepcopy(self.store.sessions)
        mutation = await self.stage()
        self.assertFalse(mutation["committed"])
        self.assertEqual(self.store.sessions, before)

    async def test_multi_target_stage_is_atomic_when_one_target_is_unavailable(self):
        before = deepcopy(self.store.sessions)
        self.namespace["DELETING_SESSIONS"].add("c")
        with self.assertRaises(HTTPException):
            await self.stage(targets=("b", "c"))
        self.assertEqual(self.store.sessions, before)
        self.assertEqual(self.store.saved, [])

    async def test_save_failure_restores_all_participants(self):
        before = deepcopy(self.store.sessions)
        self.store.failure = OSError("isolated disk failure")
        with self.assertRaises(OSError):
            await self.stage(targets=("b", "c"))
        self.assertEqual(self.store.sessions, before)
        self.assertEqual(self.store.saved[-1], before)

    async def test_runtime_rollback_restores_every_participant_in_one_save(self):
        before = deepcopy(self.store.sessions)
        mutation = await self.stage(targets=("b", "c"))
        await self.call("rollback_durable_provider_cross_chat_reference_grants", "a", mutation)
        self.assertEqual(self.store.sessions, before)
        self.assertEqual(len(self.store.saved), 2)

    async def test_staged_pair_journal_is_detached_from_later_route_revision(self):
        mutation = await self.stage()
        source = self.store.sessions["a"]
        route = source["provider_cross_chat_routes"][0]
        pending_after = source[self.pending_key]["rollback_changes"][0]["after"]
        original_after = deepcopy(pending_after)
        self.assertIsNot(pending_after, route)
        self.assertIsNot(pending_after["actions"], route["actions"])
        route["revision"] = "rev_" + "f" * 32
        route["actions"].remove("request_reply")
        self.assertEqual(pending_after, original_after)
        await self.call("rollback_durable_provider_cross_chat_reference_grants", "a", mutation)
        retained = source["provider_cross_chat_routes"][0]
        self.assertEqual(retained["revision"], "rev_" + "f" * 32)
        self.assertEqual(retained["actions"], ["instruction"])
        self.assertEqual(len(source["provider_cross_chat_route_audit"]), 1)
        self.assertEqual(self.routes("b"), [])
        self.assertEqual(self.store.sessions["b"]["provider_cross_chat_route_audit"], [])
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", retained))

    async def test_pair_rollback_retries_transient_persistence_without_restoring_authority(self):
        before = deepcopy(self.store.sessions)
        mutation = await self.stage(targets=("b", "c"))
        self.store.failure = OSError("transient rollback failure")
        await self.call("rollback_durable_provider_cross_chat_reference_grants", "a", mutation)
        self.assertEqual(self.store.sessions, before)
        self.assertEqual(len(self.store.saved), 3)
        self.assertEqual(self.store.saved[1:], [before, before])

    async def test_pair_rollback_persistent_failure_stays_narrowed_and_raises_after_three_attempts(self):
        before = deepcopy(self.store.sessions)
        mutation = await self.stage(targets=("b", "c"))
        failure = OSError("persistent rollback failure")
        self.store.persist_restored_state = AsyncMock(side_effect=failure)
        with self.assertRaises(OSError) as raised:
            await self.call("rollback_durable_provider_cross_chat_reference_grants", "a", mutation)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.store.persist_restored_state.await_count, 3)
        self.assertTrue(all(call.kwargs == {"durable": True}
                            for call in self.store.persist_restored_state.await_args_list))
        self.assertEqual(self.store.sessions, before)

    async def test_pair_rollback_joins_durable_writer_despite_repeated_cancellation(self):
        before = deepcopy(self.store.sessions)
        mutation = await self.stage(targets=("b", "c"))
        store_node = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "SessionStore")
        persist_node = deepcopy(next(node for node in store_node.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "persist_restored_state"))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[persist_node], type_ignores=[])), "<isolated-rollback-persistence>", "exec"), self.namespace)
        self.store.persist_restored_state = self.namespace["persist_restored_state"].__get__(self.store)
        writer_started = asyncio.Event()
        writer_release = asyncio.Event()
        committed = []

        async def blocked_save(*, durable):
            self.assertTrue(durable)
            writer_started.set()
            await writer_release.wait()
            committed.append(deepcopy(self.store.sessions))

        self.store.save = AsyncMock(side_effect=blocked_save)
        rollback = asyncio.create_task(self.call("rollback_durable_provider_cross_chat_reference_grants", "a", mutation))
        try:
            await asyncio.wait_for(writer_started.wait(), 1)
            for _ in range(2):
                rollback.cancel()
                await asyncio.sleep(0)
                self.assertFalse(rollback.done())
                self.assertTrue(self.store._lock.locked())
                self.assertEqual(self.store.sessions, before)
        finally:
            writer_release.set()
            await asyncio.wait_for(rollback, 1)
        self.assertEqual(committed, [before])
        self.assertEqual(self.store.save.await_count, 1)
        self.assertFalse(self.store._lock.locked())

    async def test_restart_accepts_both_directions_from_exact_source_event(self):
        mutation = await self.stage(event_type="turn_queued")
        self.accept("a", mutation)
        for sid in ("b", "a"):
            self.assertTrue(self.call("reconcile_pending_provider_cross_chat_grant", sid, self.store.sessions[sid]))
        self.assertTrue(self.call("provider_cross_chat_pair_is_live", "a", self.routes("a")[0]))

    async def test_restart_rolls_back_all_unaccepted_grants(self):
        before = deepcopy(self.store.sessions)
        await self.stage(targets=("b", "c"))
        for sid in ("c", "a", "b"):
            self.call("reconcile_pending_provider_cross_chat_grant", sid, self.store.sessions[sid])
        self.assertEqual(self.store.sessions, before)

    async def test_wrong_source_or_pair_in_event_cannot_accept_reverse_grant(self):
        mutation = await self.stage()
        self.accept("fork", mutation)
        self.assertIsNone(self.call("provider_cross_chat_grant_admission_event", "b", self.store.sessions["b"][self.pending_key]))
        self.accept("a", mutation)
        self.events["a"][0]["provider_cross_chat_route_snapshot"][0]["pair_id"] = "pair_" + "f" * 32
        self.assertIsNone(self.call("provider_cross_chat_grant_admission_event", "b", self.store.sessions["b"][self.pending_key]))

    async def test_either_side_revoke_deletes_exact_pair_and_preserves_other_permission(self):
        await self.grant("a", ("b", "c"))
        forward = deepcopy(self.routes("a")[0])
        reverse = self.routes("b")[0]
        result = await self.call("delete_agent_handoff_route", "b", reverse["route_id"], reverse["revision"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.routes("b"), [])
        self.assertEqual({route["target_session_id"] for route in self.routes("a")}, {"c"})
        self.assertEqual(len(self.routes("c")), 1)
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", forward))
        self.assertEqual(self.retired.await_count, 2)

    async def test_stale_revoke_cannot_delete_regranted_pair(self):
        await self.grant()
        stale = deepcopy(self.routes("a")[0])
        await self.call("delete_agent_handoff_route", "a", stale["route_id"], stale["revision"])
        await self.grant()
        with self.assertRaises(HTTPException) as error:
            await self.call("delete_agent_handoff_route", "a", stale["route_id"], stale["revision"])
        self.assertEqual(error.exception.status_code, 409)
        self.assertNotEqual(self.routes("a")[0]["route_id"], stale["route_id"])

    async def test_revoke_save_failure_restores_exact_pair(self):
        await self.grant()
        before = deepcopy(self.store.sessions)
        route = self.routes("a")[0]
        self.store.failure = OSError("isolated revoke save failure")
        with self.assertRaises(OSError):
            await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        self.assertEqual(self.store.sessions, before)
        self.retired.assert_not_awaited()

    async def test_torn_pair_or_deleted_target_is_not_live(self):
        await self.grant()
        route = self.routes("a")[0]
        self.store.sessions["b"]["provider_cross_chat_routes"][0]["paired_route_id"] = "route_" + "f" * 32
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", route))
        self.namespace["DELETED_SESSION_TOMBSTONES"].add("b")
        self.assertIsNone(self.call("live_provider_cross_chat_route", "a", route))

    def delivery(self, route, **extra):
        return {
            "id": "message", "source_session_id": "a", "target_session_id": "b",
            "authorization_kind": "configured_route", "authorization_route_id": route["route_id"],
            "authorization_pair_id": route["pair_id"], "action": "instruction",
            "status": "queued", "kind": "instruction", "queued_id": "queued-message", **extra,
        }

    async def test_immutable_message_pair_fences_revocation_crash_and_regrant(self):
        await self.grant()
        route = self.routes("a")[0]
        message = self.delivery(route)
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", message))
        # Model restart immediately after the durable policy save, before any
        # ledger/queue cleanup. The old message still cannot launch.
        self.store.sessions["a"]["provider_cross_chat_routes"] = []
        self.store.sessions["b"]["provider_cross_chat_routes"] = []
        self.assertFalse(self.call("provider_cross_chat_delivery_pair_is_live", message))
        await self.grant()
        self.assertFalse(self.call("provider_cross_chat_delivery_pair_is_live", message))
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", self.delivery(self.routes("a")[0])))

    async def test_legacy_independent_job_message_keeps_existing_contract(self):
        message = {"authorization_kind": "configured_route", "authorization_route_id": "route_" + "e" * 32}
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", message))

    async def test_reverse_exchange_delivery_keeps_original_pair_identity(self):
        await self.grant()
        route = self.routes("a")[0]
        exchange = {"authorization_kind": "configured_route", "authorization_route_id": route["route_id"],
                    "authorization_pair_id": route["pair_id"], "requester_session_id": "a",
                    "responder_session_id": "b", "initial_action": "instruction"}
        reply = {"source_session_id": "b", "target_session_id": "a", "kind": "reply"}
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", reply, exchange))
        reverse = self.routes("b")[0]
        await self.call("delete_agent_handoff_route", "b", reverse["route_id"], reverse["revision"])
        self.assertFalse(self.call("provider_cross_chat_delivery_pair_is_live", reply, exchange))

    async def test_final_provider_admission_checks_pair_before_running_cas(self):
        await self.grant()
        route = self.routes("a")[0]
        self.namespace["get_cross_chat_delivery_record"] = AsyncMock(return_value=self.delivery(route))
        cas = AsyncMock(return_value={"status": "running"})
        self.namespace["update_cross_chat_delivery_record"] = cas
        result = await self.call("admit_cross_chat_delivery_run", "message", queued_id="queued-message", run_id="new-run")
        self.assertEqual(result, {"status": "running"})
        await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        cas.reset_mock()
        self.assertIsNone(await self.call("admit_cross_chat_delivery_run", "message", queued_id="queued-message", run_id="other-run"))
        cas.assert_not_awaited()

    async def test_running_cas_and_revoke_share_same_policy_fence(self):
        await self.grant()
        route = self.routes("a")[0]
        self.namespace["get_cross_chat_delivery_record"] = AsyncMock(return_value=self.delivery(route))
        entered, release = asyncio.Event(), asyncio.Event()

        async def running_cas(*args, **kwargs):
            self.assertTrue(self.store._lock.locked())
            entered.set()
            await release.wait()
            return {"status": "running"}

        self.namespace["update_cross_chat_delivery_record"] = running_cas
        launch = asyncio.create_task(self.call("admit_cross_chat_delivery_run", "message", queued_id="queued-message", run_id="new-run"))
        await entered.wait()
        revoke = asyncio.create_task(self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"]))
        await asyncio.sleep(0)
        self.assertFalse(revoke.done())
        release.set()
        self.assertEqual(await launch, {"status": "running"})
        await revoke
        self.assertEqual(self.routes("a"), [])

    async def test_restart_recovery_checks_permission_before_replaying_unsent_work(self):
        await self.grant()
        route = self.routes("a")[0]
        message = self.delivery(route)
        await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        self.retired.reset_mock()
        self.namespace["CHAT_MAILBOX_PENDING"] = set()
        self.namespace["CROSS_CHAT"] = SimpleNamespace(
            mailbox_call=AsyncMock(return_value=[]), mailbox_envelopes=AsyncMock(return_value=[]),
            pending_terminal_lifecycle=AsyncMock(return_value=[]), recoverable=AsyncMock(return_value=[message]),
            get_exchange_leg=AsyncMock(return_value={"id": "leg", "exchange_id": "exchange", "kind": "request", "status": "queued"}),
            get_exchange=AsyncMock(return_value={"id": "exchange", "authorization_kind": "configured_route",
                                                "authorization_route_id": route["route_id"], "authorization_pair_id": route["pair_id"],
                                                "requester_session_id": "a", "responder_session_id": "b", "initial_action": "instruction"}),
        )
        self.namespace["cross_chat_exchange_leg_admission"] = self.lifecycle
        self.assertEqual(await self.call("reconcile_cross_chat_handoffs"), 1)
        self.assertEqual(await self.call("reconcile_cross_chat_exchange_leg", {"id": "leg"}), 1)
        self.assertEqual(self.retired.await_count, 2)

    async def test_legacy_route_revocation_survives_reload_and_only_blocks_exact_route(self):
        await self.grant()
        route = self.routes("a")[0]
        legacy = self.delivery(route, authorization_pair_id="")
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", legacy))
        await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        self.store.sessions = deepcopy(self.store.saved[-1])
        self.assertFalse(self.call("provider_cross_chat_delivery_pair_is_live", legacy))
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", {
            **legacy, "authorization_route_id": "route_" + "e" * 32,
        }))
        await self.grant()
        self.assertFalse(self.call("provider_cross_chat_delivery_pair_is_live", legacy))
        self.assertTrue(self.call("provider_cross_chat_delivery_pair_is_live", self.delivery(self.routes("a")[0])))

    async def test_target_deletion_retires_exact_surviving_pairs_without_touching_unrelated_chats(self):
        await self.grant("a", ("b", "c"))
        old_route = self.routes("a")[0]
        deleted = self.store.sessions.pop("b")
        self.call("retire_deleted_provider_cross_chat_pairs", self.store.sessions, "b", deleted)
        self.assertEqual({route["target_session_id"] for route in self.routes("a")}, {"c"})
        self.assertEqual(len(self.routes("c")), 1)
        self.assertIn(old_route["route_id"], self.store.sessions["a"]["_revoked_provider_cross_chat_route_ids"])

    async def test_target_deletion_during_pending_admission_cannot_be_undone_by_recovery(self):
        mutation = await self.stage()
        route = mutation["routes"][0]
        deleted = self.store.sessions.pop("b")
        self.call("retire_deleted_provider_cross_chat_pairs", self.store.sessions, "b", deleted)
        self.call("reconcile_pending_provider_cross_chat_grant", "a", self.store.sessions["a"])
        self.assertEqual(self.routes("a"), [])
        self.assertIn(route["route_id"], self.store.sessions["a"]["_revoked_provider_cross_chat_route_ids"])

    async def test_live_http_reply_is_fenced_after_pair_revocation(self):
        await self.grant()
        route = self.routes("a")[0]
        exchange = {"id": "exchange", "authorization_kind": "configured_route", "authorization_pair_id": route["pair_id"],
                    "authorization_route_id": route["route_id"], "requester_session_id": "a", "responder_session_id": "b",
                    "initial_action": "instruction"}
        outbound = {"id": "reply", "kind": "reply"}
        delivered = AsyncMock(return_value=(exchange, outbound, None))
        self.namespace["_deliver_cross_chat_live_response_with_policy_locked"] = delivered
        self.namespace["CROSS_CHAT"] = SimpleNamespace(cancel_exchange=AsyncMock())
        future = asyncio.get_running_loop().create_future()
        self.namespace["CROSS_CHAT_LIVE_RESPONSE_WAITERS"] = {("exchange", "parent"): {"future": future}}
        await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        with self.assertRaises(HTTPException) as error:
            await self.call("deliver_cross_chat_live_response_locked", exchange, outbound)
        self.assertEqual(error.exception.status_code, 410)
        delivered.assert_not_awaited()
        self.assertFalse(future.result()["ok"])

    async def test_direct_acceptance_payload_contains_exact_recovery_snapshot(self):
        mutation = await self.stage()
        started_function = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_turn_locked")
        payload = next(node.value for node in ast.walk(started_function) if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "started_payload" for target in node.targets)
                       and isinstance(node.value, ast.Dict))
        expression = next(value for key, value in zip(payload.keys, payload.values)
                          if isinstance(key, ast.Constant) and key.value == "provider_cross_chat_route_snapshot")
        self.namespace["provider_route_snapshot"] = mutation["routes"]
        recovered_snapshot = eval(compile(ast.Expression(expression), "<actual-started-snapshot>", "eval"), self.namespace)
        self.accept("a", mutation)
        self.events["a"][0]["provider_cross_chat_route_snapshot"] = recovered_snapshot
        self.call("reconcile_pending_provider_cross_chat_grant", "a", self.store.sessions["a"])
        self.call("reconcile_pending_provider_cross_chat_grant", "b", self.store.sessions["b"])
        self.assertTrue(self.call("provider_cross_chat_pair_is_live", "a", self.routes("a")[0]))

    async def test_async_delivery_issues_only_the_exact_reverse_route(self):
        await self.grant("a", ("b",))
        await self.grant("b", ("c",))
        message = self.delivery(self.routes("a")[0])
        snapshot = self.call("async_route_delivery_snapshot", "b", message)
        self.assertEqual([route["target_session_id"] for route in snapshot], ["a"])
        self.assertEqual(self.call("async_route_delivery_snapshot", "c", message), [])
        self.assertEqual(self.call("async_route_delivery_snapshot", "b", {**message, "authorization_pair_id": ""}), [])
        route = self.routes("a")[0]
        await self.call("delete_agent_handoff_route", "a", route["route_id"], route["revision"])
        self.assertEqual(self.call("async_route_delivery_snapshot", "b", message), [])

    async def test_queued_async_delivery_persists_and_recovers_display_metadata_and_reverse_snapshot(self):
        await self.grant("a", ("b",))
        await self.grant("b", ("c",))
        self.store.sessions["a"]["title"] = "Sender"
        self.store.sessions["b"]["title"] = "Recipient"
        message = self.delivery(self.routes("a")[0], status="submitting")
        message["body"] = "The actual message from Sender"
        self.namespace.update({
            "CROSS_CHAT": SimpleNamespace(get=AsyncMock(return_value=message)),
            "wait_for_queue_recovery_admission": AsyncMock(),
            "routed_references_match_visible_prompt": lambda *args: True,
            "validate_session_file_ids": lambda sid, files: files,
            "secure_peer_route_snapshots_for_references": lambda *args: [],
            "QUEUE_LOCK": asyncio.Lock(), "QUEUED_TURNS": {}, "RUN_NOW_TURNS": {},
            "ACTIVE_LOCK": asyncio.Lock(), "BUSY_SESSIONS": {"b"},
            "managed_server_update_blocker": lambda: None,
            "register_final_result_obligations": AsyncMock(return_value=[]),
            "register_request_reply_exchanges": AsyncMock(return_value=[]),
            "update_cross_chat_delivery_record": AsyncMock(return_value={**message, "status": "queued"}),
            "settle_provider_cross_chat_reciprocal_effect": AsyncMock(),
            "append_durable_provider_cross_chat_grant_audits": AsyncMock(),
            "chat_reference_dicts": lambda references: [], "team_reference_dicts": lambda references: [],
            "public_session": lambda session: session,
        })

        async def append(sid, event_type, payload):
            return {"type": event_type, **payload}

        self.namespace["append_durable_event"] = append
        request = SimpleNamespace(
            purpose="cross_chat_handoff_delivery", job_id=None, prompt="private relay wrapper", display_prompt="Message from Sender",
            file_ids=[], chat_references=[], team_references=[], source_session_id="a", target_session_id="b",
            cross_chat_envelope_id="message", cross_chat_exchange_id=None, cross_chat_exchange_leg_id=None,
            cross_chat_exchange_status=False, secure_peer_envelope_id=None, backend=None, model=None, effort=None,
            digest_job_id=None, digest_detail=None, client_capabilities=["backend-exact"], skill_selection=None,
        )
        result = await self.call("enqueue_turn", "b", request, self.store.sessions["b"], provider_route_snapshot=self.routes("b"))
        self.assertTrue(result["queued"])
        queued = self.namespace["QUEUED_TURNS"]["b"][0]
        self.assertEqual([route["target_session_id"] for route in queued["provider_cross_chat_route_snapshot"]], ["a"])
        recovered = self.call("queued_turn_from_event", result["event"], self.store.sessions["b"], 1)
        public = self.call("public_queued_turn", "b", recovered, 1)
        self.assertEqual((public["conversation_mode"], public["source_title"], public["message_id"]), ("async_route_v1", "Sender", "message"))
        self.assertEqual(public["prompt"], message["body"])
        self.assertEqual(public["message_body"], message["body"])
        self.assertEqual(public["message_revision"], 0)
        self.assertEqual(recovered["client_capabilities"], ["backend-exact"])
        self.assertIsNone(recovered["backend"])
        self.assertEqual(self.call("queued_turn_run_metadata", recovered)["conversation_id"], message["authorization_pair_id"])

    async def test_async_mode_issuance_is_negotiated_for_users_and_derived_for_deliveries(self):
        function = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_turn_locked")
        issuance = next(node for node in ast.walk(function) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "issue_cross_chat_capability")
        expression = next(keyword.value for keyword in issuance.keywords if keyword.arg == "async_route_v1")
        code = compile(ast.Expression(expression), "<actual-async-mode-issuance>", "eval")
        self.namespace.update(delivery_record=None, req=SimpleNamespace(purpose=None, client_capabilities=[]))
        self.assertFalse(eval(code, self.namespace))
        self.namespace["req"].client_capabilities = ["chat_conversation_async_route_v1"]
        self.assertTrue(eval(code, self.namespace))
        self.namespace["req"].purpose = "scheduled_job"
        self.assertFalse(eval(code, self.namespace))
        await self.grant()
        self.namespace.update(delivery_record=self.delivery(self.routes("a")[0]), req=SimpleNamespace(purpose="cross_chat_handoff_delivery", client_capabilities=["backend-exact"]))
        self.assertTrue(eval(code, self.namespace))


class PairLedgerMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ledger = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "CrossChatStore")
        schema = next(node.args[0].value for node in ast.walk(ledger)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "executescript" and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and "CREATE TABLE IF NOT EXISTS cross_chat_envelopes" in str(node.args[0].value))
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(schema)
        selected = ast.ClassDef(name="Ledger", bases=[], keywords=[], decorator_list=[], body=[
            deepcopy(node) for node in ledger.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"create_instruction", "create_route_exchange_request"}
        ])
        namespace = {
            "HTTPException": HTTPException, "hashlib": hashlib, "time": time,
            "now_iso": lambda: "2026-09-10T00:00:00Z",
            "validated_cross_chat_source_user_instruction": lambda value: value,
            "SERVER_INSTANCE_ID": "isolated", "PROVIDER_CROSS_CHAT_ROUTE_ID_RE": re.compile(r"^route_[0-9a-f]{32}$"),
            "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE": re.compile(r"^pair_[0-9a-f]{32}$"),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected,
        ], type_ignores=[])), "<isolated-pair-ledger>", "exec"), namespace)
        self.ledger = namespace["Ledger"]()
        self.ledger._transaction = lambda: self.connection

        async def call(operation):
            return operation()

        self.ledger._call = call
        self.route_id = "route_" + "a" * 32
        self.pair_id = "pair_" + "b" * 32

    async def test_envelope_pair_identity_is_durable_and_idempotency_bound(self):
        args = dict(envelope_id="message", source_session_id="a", source_run_id="run", target_session_id="b",
                    body="hello", idempotency_key="one", authorization_kind="configured_route",
                    authorization_route_id=self.route_id, authorization_pair_id=self.pair_id)
        message, created = await self.ledger.create_instruction(**args)
        self.assertTrue(created)
        self.assertEqual(message["authorization_pair_id"], self.pair_id)
        self.assertFalse((await self.ledger.create_instruction(**args))[1])
        with self.assertRaises(HTTPException):
            await self.ledger.create_instruction(**{**args, "authorization_pair_id": "pair_" + "c" * 32})

    async def test_exchange_pair_identity_preserves_legacy_limits_and_is_idempotency_bound(self):
        args = dict(exchange_id="exchange", leg_id="leg", requester_session_id="a", authorization_source_run_id="run",
                    responder_session_id="b", body="hello", idempotency_key="one", max_legs=2,
                    expires_at="2026-09-11T00:00:00Z", authorization_route_id=self.route_id,
                    authorization_pair_id=self.pair_id, initial_action="instruction")
        exchange, leg, created = await self.ledger.create_route_exchange_request(**args)
        self.assertTrue(created)
        self.assertEqual(exchange["authorization_pair_id"], self.pair_id)
        self.assertEqual((exchange["max_legs"], exchange["used_legs"], exchange["expires_at"]), (2, 1, args["expires_at"]))
        self.assertEqual(leg["status"], "registered")
        self.assertFalse((await self.ledger.create_route_exchange_request(**args))[2])
        with self.assertRaises(HTTPException):
            await self.ledger.create_route_exchange_request(**{**args, "authorization_pair_id": "pair_" + "c" * 32})

    async def test_legacy_envelope_has_empty_pair_identity(self):
        record, _created = await self.ledger.create_instruction(envelope_id="legacy", source_session_id="a", source_run_id="run",
                                                              target_session_id="b", body="hello", idempotency_key="one")
        self.assertEqual(record["authorization_pair_id"], "")

    async def test_additive_migration_preserves_existing_ledger_rows(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("CREATE TABLE cross_chat_envelopes (id TEXT); INSERT INTO cross_chat_envelopes VALUES ('old-envelope');"
                                 "CREATE TABLE cross_chat_exchanges (id TEXT, max_legs INTEGER, expires_at TEXT);"
                                 "INSERT INTO cross_chat_exchanges VALUES ('old-exchange', 2, 'legacy-expiry');")
        migrations = [deepcopy(node) for node in ast.walk(TREE)
                      if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                      and isinstance(node.test.left, ast.Constant)
                      and node.test.left.value == "authorization_pair_id"]
        self.assertEqual(len(migrations), 2)
        code = compile(ast.fix_missing_locations(ast.Module(body=migrations, type_ignores=[])), "<isolated-pair-migration>", "exec")
        for _ in range(2):
            namespace = {"connection": connection,
                         "columns": {row[1] for row in connection.execute("PRAGMA table_info(cross_chat_envelopes)")},
                         "exchange_columns": {row[1] for row in connection.execute("PRAGMA table_info(cross_chat_exchanges)")}}
            exec(code, namespace)
        self.assertEqual(connection.execute("SELECT * FROM cross_chat_envelopes").fetchone(), ("old-envelope", ""))
        self.assertEqual(connection.execute("SELECT * FROM cross_chat_exchanges").fetchone(), ("old-exchange", 2, "legacy-expiry", ""))


if __name__ == "__main__":
    unittest.main()
