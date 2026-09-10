"""Actual Force Send admission AST; no server import, provider, or live state.

Run through public_chat_share_safe_tests.py. These tests intentionally stop at
admission; the native consumer's delivery/authority lifecycle has separate tests.
"""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "codex_goal_followup_requires_native",
    "codex_goal_steer_selection_is_plain",
    "_run_queued_turn_now_once",
}


class AdmissionHTTPException(Exception):
    def __init__(self, *, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


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
        "asyncio": asyncio, "deque": deque,
        "HTTPException": AdmissionHTTPException,
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude",
        "DEFAULT_BACKEND": "claude",
        "CODEX_TRANSPORT_APP_SERVER": "app_server",
        "CLAUDE_TRANSPORT_AGENT_SDK": "agent_sdk",
        "CODEX_GOAL_STEER_CLIENT_CAPABILITY": "codex_goal_steer_v1",
        "CROSS_CHAT_DELIVERY_PURPOSES": {"local_delivery", "peer_delivery"},
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
                side_effect=lambda snapshot: not snapshot or snapshot == [{"intrinsic": True}],
            ),
            **self.forbidden,
        })

    def snapshot(self):
        return (
            deepcopy(self.session), dict(self.active), deepcopy(self.current),
            deepcopy(list(self.queue)), list(self.queue),
        )

    def assert_untouched(self, before):
        session, active, current, queue_values, queue_objects = before
        self.assertEqual(self.session, session)
        self.assertEqual(self.active, active)
        self.assertEqual(self.current, current)
        self.assertIs(self.ns["QUEUED_TURNS"]["chat"], self.queue)
        self.assertEqual(list(self.queue), queue_values)
        for actual, original in zip(self.queue, queue_objects, strict=True):
            self.assertIs(actual, original)
        self.assertEqual(self.ns["RUN_NOW_TURNS"], {})
        self.assertEqual(self.ns["STEERING_SESSIONS"], set())
        self.assertEqual(self.ns["BUSY_SESSIONS"], {"chat"})
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

    async def test_starting_provider_keeps_goal_and_exact_queue_position(self):
        self.active["provider_turn_ready"] = False
        await self.assert_rejected()

    async def test_runtime_or_backend_change_cannot_pause_goal_via_fallback(self):
        self.ns["queued_codex_runtime_matches_active"].return_value = False
        await self.assert_rejected()
        self.ns["queued_codex_runtime_matches_active"].return_value = True
        self.selected["backend"] = "claude"
        await self.assert_rejected()

    async def test_authority_bearing_followups_stay_queued_without_stop(self):
        for field in (
            "chat_references", "team_references", "cross_chat_obligation_ids",
            "cross_chat_exchange_ids", "provider_cross_chat_route_snapshot",
            "secure_peer_route_snapshots", "file_ids", "cross_chat_envelope_id",
            "cross_chat_exchange_id", "cross_chat_exchange_leg_id",
        ):
            with self.subTest(field=field):
                self.selected[field] = [{"id": "scoped-reference"}]
                await self.assert_rejected()
                self.selected.pop(field)

    async def test_current_authority_boundary_also_prevents_stop_fallback(self):
        for field in (
            "chat_references", "team_references", "cross_chat_obligation_ids",
            "cross_chat_exchange_ids", "cross_chat_envelope_id",
            "cross_chat_exchange_id", "cross_chat_exchange_leg_id",
            "provider_cross_chat_route_snapshot",
        ):
            with self.subTest(field=field):
                self.current[field] = [{"id": "scoped-reference"}]
                await self.assert_rejected()
                self.current.pop(field)
        self.current["purpose"] = "local_delivery"
        await self.assert_rejected()

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

    async def test_delivery_authorization_and_order_guards_remain_stronger(self):
        for purpose in ("local_delivery", "peer_delivery"):
            with self.subTest(purpose=purpose):
                self.selected["purpose"] = purpose
                await self.assert_rejected(guard="selected_cross_chat_delivery")
                self.selected.pop("purpose")
                self.queue[0]["purpose"] = purpose
                await self.assert_rejected(guard="prior_cross_chat_delivery")
                self.queue[0].pop("purpose")

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

    async def test_ordinary_authority_owner_with_active_goal_cannot_fallback_to_stop(self):
        self.active.pop("codex_native_operation_kind")
        self.current.pop("purpose")
        await self.assert_rejected()

    async def test_exhausted_goal_budget_does_not_deliver_or_resume(self):
        self.session["codex_goal_time_budget_exhausted"] = True
        await self.assert_rejected()

    async def test_old_client_or_mail_command_cannot_reuse_goal_authority(self):
        self.selected["client_capabilities"] = []
        await self.assert_rejected()
        self.selected["client_capabilities"] = ["codex_goal_steer_v1"]
        self.selected["prompt"] = "/mail server remote New message"
        await self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
