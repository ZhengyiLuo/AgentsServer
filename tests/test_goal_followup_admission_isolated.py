"""Actual Force Send admission AST; no server import, provider, or live state.

Run through public_chat_share_safe_tests.py. These tests intentionally stop at
admission; the native consumer's delivery/authority lifecycle has separate tests.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from collections import deque
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
FUNCTIONS = {
    "codex_goal_followup_requires_native",
    "codex_goal_steer_selection_is_plain",
    "_run_queued_turn_now_once",
    "async_route_queue_fields",
    "active_snapshot_input",
    "stop_turn",
    "canonical_provider_cross_chat_route_alias",
    "canonical_provider_cross_chat_route_actions",
    "normalized_provider_cross_chat_routes",
    "normalized_provider_cross_chat_route_snapshot",
    "provider_route_snapshot_allows_native_steer",
}


def saved_route_snapshots():
    """Real durable route DTO shape: automatic metadata, no route_kind."""
    return [{
        "route_id": f"route_{index + 1:032x}", "revision": f"rev_{index + 11:032x}",
        "pair_id": f"pair_{index + 21:032x}", "paired_route_id": f"route_{index + 31:032x}",
        "target_session_id": f"sess_synthetic_{index}", "alias": f"synthetic_{index}",
        "actions": ["instruction", "request_reply"],
        "created_at": "2026-09-09T10:00:00Z", "updated_at": "2026-09-10T10:00:00Z",
    } for index in range(3)]


class AdmissionHTTPException(Exception):
    def __init__(self, *, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class ObservedLock:
    """Expose a real blocked acquisition without sleep-based race timing."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.attempted = asyncio.Event()

    async def __aenter__(self):
        self.attempted.set()
        await self.lock.acquire()

    async def __aexit__(self, *_args):
        self.lock.release()


def load_admission():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in FUNCTIONS | {"NonNativeForceSendRequiresLifecycleLock"}
    ]
    assert {node.name for node in selected} == FUNCTIONS | {
        "NonNativeForceSendRequiresLifecycleLock"
    }
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *selected], type_ignores=[]))
    namespace = {
        "asyncio": asyncio, "deque": deque, "suppress": suppress,
        "HTTPException": AdmissionHTTPException,
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude",
        "DEFAULT_BACKEND": "claude",
        "CODEX_TRANSPORT_APP_SERVER": "app_server",
        "CLAUDE_TRANSPORT_AGENT_SDK": "agent_sdk",
        "CODEX_GOAL_STEER_CLIENT_CAPABILITY": "codex_goal_steer_v1",
        "PROVIDER_CROSS_CHAT_ROUTE_KIND_REFERENCE": "prompt_reference",
        "PROVIDER_CROSS_CHAT_ROUTE_KIND_AMBIENT": "ambient_local",
        "AGENT_AMBIENT_LOCAL_HANDOFFS_ENABLED": True,
        "PROVIDER_CROSS_CHAT_ROUTE_ALIAS_RE": re.compile(r"^[a-z][a-z0-9_-]{0,31}$"),
        "PROVIDER_CROSS_CHAT_ROUTE_ID_RE": re.compile(r"^route_[0-9a-f]{32}$"),
        "PROVIDER_CROSS_CHAT_ROUTE_REVISION_RE": re.compile(r"^rev_[0-9a-f]{32}$"),
        "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE": re.compile(r"^pair_[0-9a-f]{32}$"),
        "PROVIDER_CROSS_CHAT_RECIPROCAL_EFFECT_ID_RE": re.compile(r"^exchange_[0-9a-f]{32}$"),
        "PROVIDER_CROSS_CHAT_ROUTE_ACTIONS": ("instruction", "request_reply"),
        "PROVIDER_CROSS_CHAT_ROUTE_ACTION_SET": {"instruction", "request_reply"},
        "CROSS_CHAT_DELIVERY_PURPOSES": {"local_delivery", "peer_delivery"},
        "LOCAL_CROSS_CHAT_DELIVERY_PURPOSE": "local_delivery",
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class GoalFollowupAdmissionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_namespace = load_admission()

    def setUp(self):
        self.ns = dict(self.source_namespace)
        # Extracted functions retain their defining globals. Re-execution into
        # this test's namespace keeps concurrent/previous cases fully isolated.
        for name in FUNCTIONS:
            function = self.source_namespace[name]
            self.ns[name] = type(function)(
                function.__code__, self.ns, function.__name__, function.__defaults__,
                function.__closure__,
            )
            self.ns[name].__kwdefaults__ = function.__kwdefaults__
        self.session = {
            "id": "chat", "backend": "codex", "codex_thread_id": "thread-1",
            "codex_goal": {
                "objective": "Finish the existing work", "status": "active",
                "tokenBudget": 32000, "tokensUsed": 4100,
            },
        }
        self.active = {
            "backend": "codex", "transport": "app_server", "run_id": "run-1",
            "provider_thread_id": "thread-1", "provider_turn_id": "turn-1",
            "provider_turn_ready": True, "native_steer_queue": asyncio.Queue(),
            "codex_native_operation_kind": "goal_resume",
        }
        self.current = {"run_id": "run-1", "prompt": "Original request",
                        "purpose": "codex_goal_resume"}
        self.selected = {
            "queued_id": "q-followup", "backend": "codex",
            "prompt": "Please also verify the result.", "file_ids": [],
            "client_capabilities": ["codex_goal_steer_v1"],
            "_paused_after_stop": True,
        }
        self.queue = deque([
            {"queued_id": "q-before", "prompt": "Earlier work"},
            self.selected,
            {"queued_id": "q-after", "prompt": "Later work"},
        ])
        self.forbidden = {
            name: AsyncMock(side_effect=AssertionError(f"unexpected {name}"))
            for name in (
                "stop_turn", "append_event", "append_durable_event",
                "await_native_steer_result", "pause_active_codex_goal_for_stop",
                "fence_native_steer_delivery", "requeue_native_steer_after_safe_rejection",
            )
        }
        self.forbidden.update({
            name: Mock(side_effect=AssertionError(f"unexpected {name}"))
            for name in (
                "prepare_steered_turn", "schedule_next_queued_turn",
                "schedule_steered_turn_slot_waiter",
            )
        })
        self.ns.update({
            "STORE": SimpleNamespace(sessions={"chat": self.session}),
            "ACTIVE": {"chat": self.active}, "CURRENT_TURNS": {"chat": self.current},
            "ACTIVE_LOCK": asyncio.Lock(), "QUEUE_LOCK": asyncio.Lock(),
            "QUEUED_TURNS": {"chat": self.queue}, "RUN_NOW_TURNS": {},
            "STEERING_SESSIONS": set(), "STEERING_WAIT_TASKS": {},
            "BUSY_SESSIONS": {"chat"},
            "stop_cleanup_in_progress": Mock(return_value=False),
            "managed_server_update_admission_blocker": Mock(return_value=None),
            "force_send_conflict_detail": Mock(side_effect=lambda *_args, **kwargs: kwargs),
            "queued_codex_runtime_matches_active": Mock(return_value=True),
            "queued_claude_runtime_matches_active": Mock(return_value=False),
            "provider_route_snapshot_allows_native_steer": Mock(
                side_effect=self.ns["provider_route_snapshot_allows_native_steer"],
            ),
            **self.forbidden,
        })

    def snapshot(self):
        return (
            deepcopy(self.session), dict(self.active), deepcopy(self.current),
            deepcopy(list(self.queue)), list(self.queue),
            dict(self.ns["ACTIVE"]), dict(self.ns["CURRENT_TURNS"]),
            set(self.ns["BUSY_SESSIONS"]),
        )

    def assert_untouched(self, before):
        session, active, current, queue_values, queue_objects, active_map, current_map, busy = before
        self.assertEqual(self.session, session)
        self.assertEqual(self.active, active)
        self.assertEqual(self.current, current)
        self.assertIs(self.ns["QUEUED_TURNS"]["chat"], self.queue)
        self.assertEqual(list(self.queue), queue_values)
        for actual, original in zip(self.queue, queue_objects, strict=True):
            self.assertIs(actual, original)
        self.assertEqual(self.ns["RUN_NOW_TURNS"], {})
        self.assertEqual(self.ns["STEERING_SESSIONS"], set())
        self.assertEqual(self.ns["ACTIVE"], active_map)
        self.assertEqual(self.ns["CURRENT_TURNS"], current_map)
        self.assertEqual(self.ns["BUSY_SESSIONS"], busy)
        for call in self.forbidden.values():
            call.assert_not_called()

    async def assert_rejected(self, *, guard="active_goal_requires_native_steer", status=409):
        for require_native in (True, False):
            before = self.snapshot()
            with self.subTest(require_native=require_native):
                with self.assertRaises(AdmissionHTTPException) as caught:
                    await self.ns["_run_queued_turn_now_once"](
                        "chat", "q-followup", require_native=require_native,
                    )
                self.assertEqual(caught.exception.status_code, status)
                if guard is not None:
                    self.assertEqual(caught.exception.detail["guard"], guard)
                self.assert_untouched(before)

    async def test_missing_native_lane_does_not_stop_or_release_a_held_followup(self):
        self.active.pop("native_steer_queue")
        await self.assert_rejected()

    async def test_starting_ordinary_provider_keeps_goal_and_exact_queue_position(self):
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["codex_goal_steer_queue"] = asyncio.Queue()
        self.active["provider_turn_ready"] = False
        await self.assert_rejected()

    async def test_between_goal_turns_queues_native_followup_without_stale_turn_id(self):
        self.active["provider_turn_ready"] = False
        self.active["provider_turn_id"] = "previous-completed-turn"
        await self.assert_admitted_to_goal_lane(self.active["native_steer_queue"], expected_turn_id="")

    async def test_idle_cached_active_goal_rejects_before_stop_or_queue_mutation(self):
        self.ns["ACTIVE"].clear()
        self.ns["CURRENT_TURNS"].clear()
        self.ns["BUSY_SESSIONS"].clear()
        for backend in ("codex", "claude"):
            for capabilities in (["codex_goal_steer_v1"], []):
                with self.subTest(backend=backend, capabilities=capabilities):
                    self.selected["backend"] = backend
                    self.selected["client_capabilities"] = capabilities
                    await self.assert_rejected()

    async def test_prebind_goal_owner_without_active_record_cannot_fallback(self):
        self.ns["ACTIVE"].clear()
        self.current["run_id"] = None
        self.current.pop("purpose")
        await self.assert_rejected()
        self.current["purpose"] = "codex_goal_resume"
        for goal in (None, {"status": "paused"}, {"status": "complete"}):
            with self.subTest(goal=goal):
                self.session["codex_goal"] = goal
                await self.assert_rejected()

    async def test_active_goal_rejection_does_not_require_native_transport(self):
        self.active.pop("native_steer_queue")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        for transport in (None, "exec", "agent_sdk"):
            with self.subTest(transport=transport):
                self.active["transport"] = transport
                await self.assert_rejected()

    async def test_backend_change_cannot_pause_goal_via_fallback(self):
        self.selected["backend"] = "claude"
        await self.assert_rejected()

    async def test_goal_steer_uses_running_settings_not_next_turn_selection(self):
        for ordinary in (False, True):
            with self.subTest(ordinary=ordinary):
                self.setUp()
                lane = self.active["native_steer_queue"]
                if ordinary:
                    self.active.pop("codex_native_operation_kind")
                    self.current.pop("purpose")
                    self.active["native_steer_queue"] = None
                    lane = self.active["codex_goal_steer_queue"] = asyncio.Queue()
                self.active.update(provider_model="gpt-6-astra", provider_effort="xhigh", provider_service_tier="priority")
                self.session.update(model="gpt-6-sol", effort="low")
                self.selected.update(model="gpt-6-sol", effort="low")
                self.ns["queued_codex_runtime_matches_active"].return_value = False
                await self.assert_admitted_to_goal_lane(lane)
                self.ns["queued_codex_runtime_matches_active"].assert_not_called()

    async def test_non_goal_settings_change_still_requires_lifecycle_transition(self):
        self.session.pop("codex_goal")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.ns["queued_codex_runtime_matches_active"].return_value = False
        before = self.snapshot()
        with self.assertRaises(self.ns["NonNativeForceSendRequiresLifecycleLock"]):
            await self.ns["_run_queued_turn_now_once"]("chat", "q-followup", require_native=True)
        self.assert_untouched(before)
        self.ns["queued_codex_runtime_matches_active"].assert_called_once()

    async def test_authority_bearing_followups_stay_queued_without_stop(self):
        for field in (
            "chat_references", "team_references", "cross_chat_obligation_ids",
            "cross_chat_exchange_ids",
            "secure_peer_route_snapshots", "cross_chat_envelope_id",
            "cross_chat_exchange_id", "cross_chat_exchange_leg_id",
        ):
            with self.subTest(field=field):
                self.selected[field] = [{"id": "scoped-reference"}]
                await self.assert_rejected()
                self.selected.pop(field)

    async def test_goal_steer_keeps_original_references_commands_and_delivery_owner(self):
        for ordinary in (False, True):
            with self.subTest(ordinary=ordinary):
                self.setUp()
                lane = self.active["native_steer_queue"]
                if ordinary:
                    self.active.pop("codex_native_operation_kind")
                    self.active["native_steer_queue"] = None
                    lane = self.active["codex_goal_steer_queue"] = asyncio.Queue()
                self.current.update(
                    purpose="local_delivery", skill_selection={"name": "original-command"},
                    chat_references=[{"session_id": "original-target"}],
                    team_references=[{"id": "original-team"}],
                    secure_peer_route_snapshots=[{"route_id": "original-route"}],
                    cross_chat_obligation_ids=["original-obligation"],
                    cross_chat_exchange_ids=["original-exchange"],
                    cross_chat_envelope_id="original-envelope",
                    cross_chat_exchange_id="original-exchange",
                    cross_chat_exchange_leg_id="original-leg",
                )
                await self.assert_admitted_to_goal_lane(lane)

    async def test_resume_owner_protects_even_missing_or_stale_goal_cache(self):
        self.active.pop("native_steer_queue")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        for goal in (None, {"status": "paused"}, {"status": "complete"}):
            self.session["codex_goal"] = goal
            for owner in ("active", "current"):
                with self.subTest(goal=goal, owner=owner):
                    if owner == "active":
                        self.active["codex_native_operation_kind"] = "goal_resume"
                    else:
                        self.current["purpose"] = "codex_goal_resume"
                    await self.assert_rejected()
                    self.active.pop("codex_native_operation_kind", None)
                    self.current.pop("purpose", None)

    async def test_explicit_stop_cleanup_rejects_without_releasing_pause(self):
        self.ns["stop_cleanup_in_progress"].return_value = True
        self.active["stop_requested"] = True
        self.session["codex_goal"]["status"] = "paused"
        await self.assert_rejected(guard=None)
        self.ns["force_send_conflict_detail"].assert_not_called()

    async def test_unknown_delivery_row_cannot_be_replayed_or_unpaused(self):
        self.selected["_native_delivery_fenced"] = True
        await self.assert_rejected(guard="delivery_uncertain")

    async def test_selecting_immutable_delivery_still_rejects(self):
        for purpose in ("local_delivery", "peer_delivery"):
            with self.subTest(purpose=purpose):
                self.selected["purpose"] = purpose
                await self.assert_rejected(guard="selected_cross_chat_delivery")
                self.selected.pop("purpose")

    async def test_special_purpose_input_cannot_enter_plain_goal_steering(self):
        # Scheduler-specific queue precedence belongs to its separate change.
        # Goal admission itself must reject all special-purpose selected input,
        # without relying on that change or changing ownership/queue contents.
        for purpose in ("scheduled_job", "standalone_task"):
            with self.subTest(purpose=purpose):
                self.selected["purpose"] = purpose
                await self.assert_rejected(guard=None)
                self.selected.pop("purpose")

    async def test_non_goal_and_paused_goal_keep_legacy_lifecycle_probe(self):
        self.active.pop("native_steer_queue")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        for goal in (None, {"status": "paused"}, {"status": "complete"}):
            with self.subTest(goal=goal):
                self.session["codex_goal"] = goal
                before = self.snapshot()
                with self.assertRaises(self.ns["NonNativeForceSendRequiresLifecycleLock"]):
                    await self.ns["_run_queued_turn_now_once"](
                        "chat", "q-followup", require_native=True,
                    )
                self.assert_untouched(before)

    async def test_ordinary_owner_without_goal_lane_cannot_fallback_to_stop(self):
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        await self.assert_rejected()

    async def assert_admitted_to_goal_lane(self, lane, *, expected_turn_id="turn-1"):
        goal_before = deepcopy(self.session["codex_goal"])
        active_before = dict(self.active)
        current_before = deepcopy(self.current)

        async def accept(chat, selected, **kwargs):
            self.assertIs(kwargs["native_steer_queue"], lane)
            command = lane.get_nowait()
            self.assertIs(command["selected"], self.selected)
            self.assertEqual(command["expected_provider_turn_id"], expected_turn_id)
            self.assertEqual(command["goal_identity"], ("", "Finish the existing work"))
            return {"ok": True, "queued_id": selected["queued_id"], "interrupted": False}

        self.ns["await_native_steer_result"] = AsyncMock(side_effect=accept)
        self.ns["suppress"] = __import__("contextlib").suppress
        result = await self.ns["_run_queued_turn_now_once"]("chat", "q-followup")
        self.assertTrue(result["ok"])
        self.assertFalse(result["interrupted"])
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(self.session["codex_goal"], goal_before)
        self.assertEqual(self.active, active_before)
        self.assertEqual(self.current, current_before)
        self.assertEqual([row["queued_id"] for row in self.ns["QUEUED_TURNS"]["chat"]], ["q-before", "q-after"])
        self.assertFalse(self.ns["STEERING_SESSIONS"])
        self.forbidden["stop_turn"].assert_not_called()
        self.forbidden["pause_active_codex_goal_for_stop"].assert_not_called()
        self.forbidden["prepare_steered_turn"].assert_not_called()

    async def test_original_turn_that_creates_goal_uses_goal_only_lane(self):
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["native_steer_queue"] = None
        self.active["codex_goal_steer_queue"] = asyncio.Queue()
        await self.assert_admitted_to_goal_lane(self.active["codex_goal_steer_queue"])

    async def test_saved_route_metadata_allows_plain_goal_steering_without_replacing_authority(self):
        for ordinary in (False, True):
            for current_routes in ([], saved_route_snapshots()):
                for files in ([], ["file_synthetic_attachment"]):
                    with self.subTest(ordinary=ordinary, current_routes=bool(current_routes), files=files):
                        self.setUp()
                        routes = saved_route_snapshots()
                        self.assertEqual(self.ns["normalized_provider_cross_chat_route_snapshot"](routes), routes)
                        self.assertFalse(self.ns["provider_route_snapshot_allows_native_steer"](routes))
                        self.selected.update(
                            prompt="Status report!", model="gpt-6-astra", effort="xhigh",
                            file_ids=files, provider_cross_chat_route_snapshot=routes,
                            provider_team_mail_route_snapshot=[{"route_id": "mail_" + "1" * 32, "revision": "rev_" + "2" * 32}],
                        )
                        self.current["provider_cross_chat_route_snapshot"] = current_routes
                        authority = self.active["provider_authority"] = {"proof": "existing-owner-only"}
                        lane = self.active["native_steer_queue"]
                        if ordinary:
                            self.active.pop("codex_native_operation_kind")
                            self.current["purpose"] = "scheduled_job"
                            self.active["native_steer_queue"] = None
                            lane = self.active["codex_goal_steer_queue"] = asyncio.Queue()
                        await self.assert_admitted_to_goal_lane(lane)
                        self.assertIs(self.active["provider_authority"], authority)
                        self.assertEqual(self.selected["provider_cross_chat_route_snapshot"], routes)

    async def test_saved_routes_do_not_relax_explicit_reference_or_command_rejections(self):
        for field, value in (
            ("chat_references", [{"session_id": "new-target"}]),
            ("team_references", [{"id": "new-team"}]),
            ("skill_selection", {"name": "provider-command"}),
            ("prompt", "/mail server remote New message"),
            ("provider_cross_chat_route_snapshot", [
                *saved_route_snapshots(), {**saved_route_snapshots()[0], "route_kind": "prompt_reference"},
            ]),
        ):
            with self.subTest(field=field):
                self.setUp()
                self.selected["provider_cross_chat_route_snapshot"] = saved_route_snapshots()
                self.selected[field] = value
                await self.assert_rejected()

    async def test_saved_routes_still_require_ordinary_non_goal_authority_replacement(self):
        self.session.pop("codex_goal")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.selected["provider_cross_chat_route_snapshot"] = saved_route_snapshots()
        before = self.snapshot()
        with self.assertRaises(self.ns["NonNativeForceSendRequiresLifecycleLock"]):
            await self.ns["_run_queued_turn_now_once"]("chat", "q-followup", require_native=True)
        self.assert_untouched(before)

    async def test_goal_created_while_waiting_for_queue_lock_uses_goal_lane(self):
        goal = self.session.pop("codex_goal")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["native_steer_queue"] = None
        lane = self.active["codex_goal_steer_queue"] = asyncio.Queue()
        lock = self.ns["QUEUE_LOCK"] = ObservedLock()
        await lock.lock.acquire()

        async def accept(_chat, _selected, **kwargs):
            self.assertIs(kwargs["native_steer_queue"], lane)
            request = lane.get_nowait()
            self.assertEqual(request["goal_identity"], ("", goal["objective"]))
            self.assertEqual(request["expected_provider_turn_id"], "turn-1")
            return {"ok": True, "interrupted": False}

        self.ns["await_native_steer_result"] = AsyncMock(side_effect=accept)
        task = asyncio.create_task(self.ns["_run_queued_turn_now_once"](
            "chat", "q-followup", require_native=False,
        ))
        try:
            await asyncio.wait_for(lock.attempted.wait(), 5)
            self.session["codex_goal"] = goal
            lock.lock.release()
            result = await asyncio.wait_for(task, 5)
            self.assertTrue(result["ok"])
            self.assertFalse(result["interrupted"])
            self.assertIs(self.session["codex_goal"], goal)
            self.assertEqual(goal["status"], "active")
            self.assertEqual([row["queued_id"] for row in self.ns["QUEUED_TURNS"]["chat"]], ["q-before", "q-after"])
            self.forbidden["stop_turn"].assert_not_called()
            self.forbidden["pause_active_codex_goal_for_stop"].assert_not_called()
            self.forbidden["prepare_steered_turn"].assert_not_called()
        finally:
            if not task.done():
                task.cancel()
                if lock.lock.locked():
                    lock.lock.release()
            with suppress(BaseException):
                await task

    async def test_original_goal_guard_survives_goal_clearing_during_queue_wait(self):
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["native_steer_queue"] = None
        self.active["codex_goal_steer_queue"] = asyncio.Queue()
        lock = self.ns["QUEUE_LOCK"] = ObservedLock()
        await lock.lock.acquire()
        task = asyncio.create_task(self.ns["_run_queued_turn_now_once"](
            "chat", "q-followup", require_native=False,
        ))
        try:
            await asyncio.wait_for(lock.attempted.wait(), 5)
            self.session["codex_goal"] = None
            before = self.snapshot()
            lock.lock.release()
            with self.assertRaises(AdmissionHTTPException) as caught:
                await asyncio.wait_for(task, 5)
            self.assertEqual(caught.exception.detail["guard"], "active_goal_requires_native_steer")
            self.assert_untouched(before)
        finally:
            if not task.done():
                task.cancel()
                if lock.lock.locked():
                    lock.lock.release()
            with suppress(BaseException):
                await task

    def test_active_snapshot_excludes_both_private_steer_queues(self):
        self.active["codex_goal_steer_queue"] = asyncio.Queue()
        snapshot = self.ns["active_snapshot_input"](self.active)
        self.assertNotIn("native_steer_queue", snapshot)
        self.assertNotIn("codex_goal_steer_queue", snapshot)
        self.assertEqual(json.loads(json.dumps(snapshot))["run_id"], "run-1")

    def actual_stop_turn(self):
        source = self.source_namespace["stop_turn"]
        actual = type(source)(source.__code__, self.ns, source.__name__)
        actual.__kwdefaults__ = source.__kwdefaults__
        return actual

    async def test_goal_created_during_final_stop_lock_wait_restores_exact_queue(self):
        goal = self.session.pop("codex_goal")
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["native_steer_queue"] = None
        active_before, current_before = dict(self.active), deepcopy(self.current)
        selected_before = deepcopy(self.selected)
        original_rows = list(self.queue)
        actual_stop = self.actual_stop_turn()

        async def enter_stop(*args, **kwargs):
            self.assertIs(kwargs["preserve_active_goal"], True)
            lock = self.ns["ACTIVE_LOCK"] = ObservedLock()
            await lock.lock.acquire()
            task = asyncio.create_task(actual_stop(*args, **kwargs))
            try:
                await asyncio.wait_for(lock.attempted.wait(), 5)
                self.session["codex_goal"] = goal
                lock.lock.release()
                return await asyncio.wait_for(task, 5)
            finally:
                if not task.done():
                    task.cancel()
                    if lock.lock.locked():
                        lock.lock.release()
                with suppress(BaseException):
                    await task

        self.ns["stop_turn"] = AsyncMock(side_effect=enter_stop)
        # Rollback may offer queue scheduling; it must not prepare a new turn,
        # interrupt the current provider, or actually execute any queued work.
        self.ns["schedule_next_queued_turn"] = Mock()
        with self.assertRaises(AdmissionHTTPException) as caught:
            await self.ns["_run_queued_turn_now_once"]("chat", "q-followup")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["guard"], "active_goal_requires_native_steer")
        self.assertEqual(self.active, active_before)
        self.assertEqual(self.current, current_before)
        self.assertEqual(self.selected, selected_before)
        self.assertEqual(list(self.ns["QUEUED_TURNS"]["chat"]), original_rows)
        for actual, original in zip(self.ns["QUEUED_TURNS"]["chat"], original_rows, strict=True):
            self.assertIs(actual, original)
        self.assertFalse(self.ns["STEERING_SESSIONS"])
        self.assertFalse(self.ns["RUN_NOW_TURNS"])
        self.assertEqual(goal["status"], "active")
        self.forbidden["pause_active_codex_goal_for_stop"].assert_not_called()
        self.forbidden["prepare_steered_turn"].assert_not_called()

    async def test_protected_stop_checks_cached_goal_and_both_owner_markers(self):
        actual_stop = self.actual_stop_turn()
        for mode in ("active-goal", "active-owner", "current-owner", "idle-goal"):
            with self.subTest(mode=mode):
                self.session["codex_goal"] = {"status": "active"} if mode.endswith("goal") else None
                self.active.pop("codex_native_operation_kind", None)
                self.current.pop("purpose", None)
                if mode == "active-owner":
                    self.active["codex_native_operation_kind"] = "goal_resume"
                elif mode == "current-owner":
                    self.current["purpose"] = "codex_goal_resume"
                elif mode == "idle-goal":
                    self.ns["ACTIVE"].clear()
                    self.ns["CURRENT_TURNS"].clear()
                    self.ns["BUSY_SESSIONS"].clear()
                before = self.snapshot()
                ready = asyncio.Event()
                with self.assertRaises(AdmissionHTTPException) as caught:
                    await actual_stop("chat", preserve_active_goal=True, _admission_ready=ready)
                self.assertEqual(caught.exception.detail["guard"], "active_goal_requires_native_steer")
                self.assertTrue(ready.is_set())
                self.assert_untouched(before)

    async def test_explicit_stop_default_still_fences_active_goal(self):
        # Execute the real synchronous Stop admission, then stop at its first
        # cleanup dependency so no provider task or service is touched.
        self.ns.update({
            "STOPPED_RUNS": set(), "RUN_METADATA": {},
            "SESSION_TURN_TASKS": {}, "CODEX_NATIVE_ACTION_TASKS": {},
            "empty_subagent_stop_result": Mock(side_effect=RuntimeError("cleanup boundary")),
        })
        with self.assertRaisesRegex(RuntimeError, "cleanup boundary"):
            await self.actual_stop_turn()("chat")
        self.assertTrue(self.active["stop_requested"])
        self.assertEqual(self.ns["STOPPED_RUNS"], {"run-1"})

    async def test_goal_continuation_accepts_attached_followup_without_pausing(self):
        self.selected["file_ids"] = ["uploaded-image"]
        await self.assert_admitted_to_goal_lane(self.active["native_steer_queue"])

    async def test_non_goal_cannot_use_goal_only_lane_to_replace_authority(self):
        self.session["codex_goal"] = None
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        self.active["native_steer_queue"] = None
        self.active["codex_goal_steer_queue"] = asyncio.Queue()
        with self.assertRaises(self.ns["NonNativeForceSendRequiresLifecycleLock"]):
            await self.ns["_run_queued_turn_now_once"]("chat", "q-followup", require_native=True)
        self.assertTrue(self.active["codex_goal_steer_queue"].empty())

    async def test_exhausted_goal_budget_does_not_deliver_or_resume(self):
        self.session["codex_goal_time_budget_exhausted"] = True
        await self.assert_rejected()

    async def test_goal_steer_accepts_plain_input_without_client_capability(self):
        for ordinary in (False, True):
            for capabilities in (None, []):
                with self.subTest(ordinary=ordinary, capabilities=capabilities):
                    self.setUp()
                    lane = self.active["native_steer_queue"]
                    if ordinary:
                        self.active.pop("codex_native_operation_kind")
                        self.current.pop("purpose")
                        self.active["native_steer_queue"] = None
                        lane = self.active["codex_goal_steer_queue"] = asyncio.Queue()
                    if capabilities is None:
                        self.selected.pop("client_capabilities")
                    else:
                        self.selected["client_capabilities"] = capabilities
                    await self.assert_admitted_to_goal_lane(lane)

    async def test_mail_command_cannot_reuse_goal_authority(self):
        self.selected["prompt"] = "/mail server remote New message"
        await self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
