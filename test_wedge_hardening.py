"""Regression tests for the 2026-09-04 wedge: fenced queues, hung Stop, retry
storms, health-poll reconcile spam, and update-when-idle lockout."""

import asyncio
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

import agent_server
from test_server_restart_endpoints import (
    http_request,
    restart_body,
    restart_environment,
)


def utc(offset_seconds: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class QueueFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_skips_and_names_explicit_stop_fence(self):
        never = asyncio.get_running_loop().create_future()
        stop_operation = asyncio.ensure_future(never)
        try:
            with patch.object(
                agent_server,
                "EXPLICIT_STOP_OPERATIONS",
                {"chat": stop_operation},
            ), patch.object(
                agent_server,
                "QUEUED_TURNS",
                {"chat": agent_server.deque([{"queued_id": "q1", "_durable": True}])},
            ), patch.object(agent_server, "QUEUE_FENCE_LOGGED_AT", {}), \
                 patch.object(
                     agent_server,
                     "schedule_next_queued_turn",
                 ) as schedule, \
                 self.assertLogs(agent_server.logger, level="WARNING") as logs:
                repaired = await agent_server.reconcile_idle_queue_session(
                    "chat",
                    schedule=True,
                    reason="health_poll",
                )
                # A second reconcile within the log interval stays quiet.
                await agent_server.reconcile_idle_queue_session(
                    "chat",
                    schedule=True,
                    reason="health_poll",
                )
        finally:
            never.cancel()
        self.assertFalse(repaired)
        schedule.assert_not_called()
        fence_lines = [
            line for line in logs.output if "queue promotion fenced" in line
        ]
        self.assertEqual(len(fence_lines), 1)
        self.assertIn("fence=explicit_stop", fence_lines[0])
        self.assertIn("session=chat", fence_lines[0])

    async def test_reconcile_names_server_restart_fence(self):
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=agent_server.MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertEqual(
                agent_server.queue_promotion_fence("chat"),
                "server_restart",
            )
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=agent_server.MANAGED_SERVER_UPDATE_ACTIVE_DETAIL,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertEqual(
                agent_server.queue_promotion_fence("chat"),
                "server_update",
            )
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=None,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertIsNone(agent_server.queue_promotion_fence("chat"))

    async def test_routine_idle_reconcile_logs_at_debug_only(self):
        with patch.object(
            agent_server,
            "QUEUED_TURNS",
            {"chat": agent_server.deque([{"queued_id": "q1", "_durable": True}])},
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}), \
             patch.object(agent_server, "RUN_NOW_REQUESTS", {}), \
             patch.object(agent_server, "QUEUE_START_TASKS", {}), \
             patch.object(agent_server, "STEERING_WAIT_TASKS", {}), \
             patch.object(agent_server, "STEERING_SESSIONS", set()), \
             patch.object(agent_server, "RUN_NOW_TURNS", {}), \
             patch.object(agent_server, "ACTIVE", {}), \
             patch.object(agent_server, "BUSY_SESSIONS", set()), \
             patch.object(agent_server, "CURRENT_TURNS", {}), \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ), \
             patch.object(agent_server, "schedule_next_queued_turn") as schedule, \
             self.assertNoLogs(agent_server.logger, level="INFO"):
            repaired = await agent_server.reconcile_idle_queue_session(
                "chat",
                schedule=True,
                reason="health_poll",
            )
        self.assertTrue(repaired)
        schedule.assert_called_once_with("chat")


class RetryTimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_retry_timer_is_armed_per_chat(self):
        with patch.object(agent_server, "QUEUE_RETRY_TASKS", {}) as timers, \
             patch.object(
                 agent_server,
                 "start_next_queued_turn",
                 new=AsyncMock(),
             ) as start:
            self.assertTrue(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertFalse(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertFalse(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertEqual(len(timers), 1)
            timers["chat"].cancel()
            await asyncio.gather(timers["chat"], return_exceptions=True)
            await asyncio.sleep(0)
            self.assertNotIn("chat", timers)
            start.assert_not_called()
            # Once the previous timer settled a new one may be armed.
            self.assertTrue(agent_server.schedule_queued_turn_retry("chat", 5))
            timers["chat"].cancel()
            await asyncio.gather(timers["chat"], return_exceptions=True)


class HealthReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_poll_reconcile_is_rate_limited(self):
        with patch.object(
            agent_server,
            "HEALTH_QUEUE_RECONCILE_STATE",
            {"last_at": float("-inf")},
        ) as state, patch.object(
            agent_server,
            "reconcile_idle_queued_turns",
            new=AsyncMock(return_value=0),
        ) as reconcile, patch.object(
            agent_server,
            "HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS",
            3600.0,
        ):
            self.assertTrue(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertFalse(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertFalse(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            reconcile.assert_awaited_once_with(reason="health_poll")
            state["last_at"] = float("-inf")
            self.assertTrue(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertEqual(reconcile.await_count, 2)


class ExplicitStopDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_stop_releases_fence_and_reports_timeout(self):
        release = asyncio.Event()

        async def hung_stop(session_id, *, _admission_ready=None):
            if _admission_ready is not None:
                _admission_ready.set()
            await release.wait()
            return {"ok": True, "stopped": True, "late": True}

        events: list[tuple[str, dict]] = []

        async def record_event(session_id, event_type, payload):
            events.append((event_type, payload))

        with patch.object(agent_server, "stop_turn", new=hung_stop), \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}) as fences, \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS", 1.0), \
             patch.object(agent_server, "DETACHED_STOP_TASKS", set()) as detached, \
             patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", {}), \
             patch.object(agent_server, "append_event", new=record_event), \
             patch.object(agent_server, "schedule_next_queued_turn") as schedule, \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ), \
             self.assertLogs(agent_server.logger, level="ERROR") as logs:
            result = await asyncio.wait_for(
                agent_server.stop_turn_endpoint("chat"),
                timeout=5,
            )
            self.assertFalse(agent_server.explicit_stop_in_progress("chat"))
            self.assertNotIn("chat", fences)
            # Teardown keeps running detached until the provider settles.
            self.assertEqual(len(detached), 1)
            release.set()
            await asyncio.gather(*detached, return_exceptions=True)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])
        self.assertEqual([kind for kind, _ in events], ["error"])
        self.assertTrue(events[0][1]["stop_timeout"])
        schedule.assert_called_with("chat")
        self.assertTrue(any("did not finish" in line for line in logs.output))

    async def test_prompt_stop_returns_its_result_and_clears_fence(self):
        async def quick_stop(session_id, *, _admission_ready=None):
            if _admission_ready is not None:
                _admission_ready.set()
            return {"ok": True, "stopped": True}

        with patch.object(agent_server, "stop_turn", new=quick_stop), \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}) as fences, \
             patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", {}), \
             patch.object(agent_server, "schedule_next_queued_turn"), \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ):
            result = await agent_server.stop_turn_endpoint("chat")
        self.assertEqual(result, {"ok": True, "stopped": True})
        self.assertEqual(fences, {})


class PendingUpdateGraceTests(unittest.TestCase):
    def pending_status(self, root: Path, *, age_seconds: float) -> None:
        agent_server.write_fresh_server_update_status(
            phase="pending",
            schedule_id="e" * 32,
            target_version="1.1.0",
            track="stable",
            when_idle=True,
            pending_at=utc(age_seconds),
        )

    def test_recent_reservation_still_parks_interactive_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ), patch.object(
                agent_server,
                "PENDING_UPDATE_BYPASS_LOGGED_AT",
                {"at": float("-inf")},
            ):
                self.pending_status(root, age_seconds=5)
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(prompt="hi"),
                    )
                )

    def test_stale_reservation_admits_interactive_turns_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ), patch.object(
                agent_server,
                "PENDING_UPDATE_BYPASS_LOGGED_AT",
                {"at": float("-inf")},
            ), patch.object(
                agent_server,
                "SERVER_UPDATE_PENDING_INTERACTIVE_FENCE_SECONDS",
                120.0,
            ):
                self.pending_status(root, age_seconds=600)
                with self.assertLogs(agent_server.logger, level="WARNING") as logs:
                    self.assertTrue(
                        agent_server.interactive_turn_may_bypass_pending_update(
                            agent_server.TurnRequest(prompt="hi"),
                        )
                    )
                self.assertTrue(
                    any("parked chats" in line for line in logs.output)
                )
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(
                            prompt="job",
                            purpose="scheduled_job",
                        ),
                    )
                )
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(
                            prompt="delivery",
                            cross_chat_envelope_id="env-1",
                        ),
                    )
                )

    def test_no_reservation_means_no_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ):
                self.assertIsNone(agent_server.pending_server_update_age_seconds())
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(prompt="hi"),
                    )
                )


class RestartDenialLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_restart_logs_code_and_blockers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"chat"}), \
                 self.assertLogs(agent_server.logger, level="WARNING") as logs:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )
        self.assertEqual(raised.exception.detail["code"], "server_restart_busy")
        denial = [line for line in logs.output if "server restart denied" in line]
        self.assertEqual(len(denial), 1)
        self.assertIn("code=server_restart_busy", denial[0])
        self.assertIn("forced=False", denial[0])
        self.assertIn('"active_count": 1', denial[0])

    async def test_snapshot_advertises_force_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", {"chat"}):
                snapshot = agent_server.server_restart_blocker_snapshot_locked()
        self.assertTrue(snapshot["has_safety_blockers"])
        self.assertTrue(snapshot["force_restart_available"])


class HistoryImportAnchorTests(unittest.TestCase):
    def select(self, events, items):
        with patch.object(agent_server, "read_events", return_value=events) as read:
            fresh = agent_server.unsynced_history_items("chat-x", items)
        return fresh, read

    def test_dedup_scans_the_timeline_tail(self):
        events = [{"type": "turn_started", "prompt": "hello"}]
        _fresh, read = self.select(events, [{"kind": "user", "text": "hello"}])
        self.assertTrue(read.call_args.kwargs.get("tail"))

    def test_no_anchor_on_a_populated_timeline_imports_nothing(self):
        # Every transcript item failed to match an existing conversation:
        # importing them would duplicate the whole chat (12k duplicate events
        # on 2026-09-04). Nothing is the only safe answer.
        events = [
            {"type": "turn_started", "prompt": "timeline only"},
            {"type": "assistant_text", "text": "timeline reply"},
        ]
        with self.assertLogs(agent_server.logger, level="WARNING"):
            fresh, _read = self.select(
                events,
                [
                    {"kind": "user", "text": "transcript A"},
                    {"kind": "assistant", "text": "transcript B"},
                ],
            )
        self.assertEqual(fresh, [])

    def test_empty_timeline_still_imports_everything(self):
        items = [{"kind": "user", "text": "hello"}, {"kind": "assistant", "text": "hi"}]
        fresh, _read = self.select([], items)
        self.assertEqual(fresh, items)


class SubagentReconcileBurstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_index = agent_server.CODEX_SUBAGENT_SESSION_INDEX
        self.previous_states = agent_server.CODEX_SUBAGENT_STATE
        self.session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "parent-thread",
            "session_id": "parent-thread",
        }
        agent_server.STORE.sessions = {"chat": self.session}
        agent_server.CODEX_SUBAGENT_SESSION_INDEX = {}
        agent_server.CODEX_SUBAGENT_STATE = {}
        self.events: list[tuple[str, dict]] = []

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.CODEX_SUBAGENT_SESSION_INDEX = self.previous_index
        agent_server.CODEX_SUBAGENT_STATE = self.previous_states

    async def append(self, session_id, event_type, payload):
        self.events.append((event_type, payload))
        return {"seq": len(self.events), **payload}

    class Manager:
        def __init__(self, descendants):
            self.descendants = descendants

        async def list_descendant_threads(self, thread_id):
            return list(self.descendants)

    async def test_terminal_children_unknown_to_this_process_are_learned_silently(self):
        manager = self.Manager([
            {"id": f"child-{index}", "parentThreadId": "parent-thread",
             "preview": f"Audit {index}", "status": {"type": "notLoaded"},
             "updatedAt": f"2026-09-04T10:00:{index:02d}Z"}
            for index in range(5)
        ])
        with patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)), \
             patch.object(agent_server.STORE, "save", AsyncMock()):
            result = await agent_server.reconcile_codex_subagents("chat", manager)
        self.assertEqual(result["reconciled"], 5)
        self.assertEqual(result["silent"], 5)
        self.assertEqual(self.events, [])
        self.assertEqual(
            agent_server.CODEX_SUBAGENT_STATE["child-3"]["subagent_status"],
            "completed",
        )
        self.assertEqual(agent_server.CODEX_SUBAGENT_SESSION_INDEX["child-3"], "chat")

    async def test_running_children_and_known_transitions_still_emit(self):
        manager = self.Manager([
            {"id": "child-run", "parentThreadId": "parent-thread",
             "preview": "Live", "status": {"type": "active", "activeFlags": []}},
        ])
        with patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)), \
             patch.object(agent_server.STORE, "save", AsyncMock()):
            await agent_server.reconcile_codex_subagents("chat", manager)
            self.assertEqual([kind for kind, _ in self.events], ["subagent_state"])
            self.assertEqual(self.events[0][1]["subagent_status"], "running")
            manager.descendants[0]["status"] = {"type": "notLoaded"}
            await agent_server.reconcile_codex_subagents("chat", manager)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[1][1]["subagent_status"], "completed")

    async def test_durable_snapshot_rehydrates_known_children_after_restart(self):
        self.session["codex_subagents"] = {
            "child-a": {
                "session_id": "chat",
                "subagent_id": "child-a",
                "subagent_status": "completed",
                "subagent_name": "Leibniz",
            },
        }
        manager = self.Manager([
            {"id": "child-a", "parentThreadId": "parent-thread",
             "preview": "Audit A", "status": {"type": "notLoaded"}},
        ])
        with patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)), \
             patch.object(agent_server.STORE, "save", AsyncMock()):
            await agent_server.reconcile_codex_subagents("chat", manager)
        self.assertEqual(self.events, [])
        self.assertEqual(
            agent_server.CODEX_SUBAGENT_STATE["child-a"]["subagent_name"],
            "Leibniz",
        )

    async def test_descendant_reconciliation_is_capped_to_most_recent(self):
        manager = self.Manager([
            {"id": f"child-{index}", "parentThreadId": "parent-thread",
             "preview": f"Audit {index}", "status": {"type": "notLoaded"},
             "updatedAt": f"2026-09-04T10:{index // 60:02d}:{index % 60:02d}Z"}
            for index in range(10)
        ])
        with patch.object(agent_server, "CODEX_SUBAGENT_RECONCILE_LIMIT", 3), \
             patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)), \
             patch.object(agent_server.STORE, "save", AsyncMock()):
            result = await agent_server.reconcile_codex_subagents("chat", manager)
        self.assertEqual(result["reconciled"], 3)
        self.assertEqual(result["skipped"], 7)
        self.assertEqual(result["descendants"], 10)
        self.assertIn("child-9", agent_server.CODEX_SUBAGENT_STATE)
        self.assertNotIn("child-0", agent_server.CODEX_SUBAGENT_STATE)


class SessionsWriterFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_encode_failure_reaches_the_awaiter_and_does_not_strand_later_saves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(agent_server, "SESSIONS_FILE", root / "sessions.json"):
                store = agent_server.SessionStore()
                store.sessions = {"chat": {"id": "chat", "bad": {1, 2}}}
                with self.assertRaises(TypeError):
                    await asyncio.wait_for(store.save(), timeout=5)
                store.sessions = {"chat": {"id": "chat"}}
                await asyncio.wait_for(store.save(), timeout=5)
                await asyncio.wait_for(store.flush_pending_save(), timeout=5)
            self.assertEqual(
                json.loads((root / "sessions.json").read_text())["chat"]["id"],
                "chat",
            )

    def test_bounded_writer_lock_refuses_instead_of_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions.json"
            agent_server.SESSIONS_WRITE_LOCK.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    agent_server.write_sessions_json_text(
                        path,
                        "{}",
                        durable=False,
                        lock_timeout=0.05,
                    )
            finally:
                agent_server.SESSIONS_WRITE_LOCK.release()
            agent_server.write_sessions_json_text(path, "{}", durable=False, lock_timeout=0.05)
            self.assertEqual(path.read_text(), "{}")


class ManagedServiceProofTests(unittest.TestCase):
    def test_probe_timeout_keeps_prior_positive_proof(self):
        with patch.object(agent_server.sys, "platform", "darwin"), \
             patch.object(agent_server, "official_server_release_tree", return_value=True), \
             patch.object(agent_server, "MANAGED_SERVER_SERVICE_KIND_CACHE", "launch-agent"), \
             patch.object(
                 agent_server,
                 "macos_launchd_owns_current_process",
                 return_value=None,
             ):
            self.assertEqual(
                agent_server.detect_managed_server_service_kind(),
                "launch-agent",
            )
            self.assertEqual(agent_server.managed_server_service_kind(), "launch-agent")

    def test_probe_timeout_without_prior_proof_fails_closed(self):
        with patch.object(agent_server.sys, "platform", "darwin"), \
             patch.object(agent_server, "official_server_release_tree", return_value=True), \
             patch.object(agent_server, "MANAGED_SERVER_SERVICE_KIND_CACHE", None), \
             patch.object(
                 agent_server,
                 "macos_launchd_owns_current_process",
                 return_value=None,
             ):
            self.assertIsNone(agent_server.detect_managed_server_service_kind())

    def test_launchctl_timeout_reports_unknown(self):
        with patch.object(agent_server.sys, "platform", "darwin"), \
             patch.object(agent_server.Path, "is_file", return_value=True), \
             patch.object(
                 agent_server.subprocess,
                 "run",
                 side_effect=agent_server.subprocess.TimeoutExpired(cmd="launchctl", timeout=3),
             ):
            self.assertIsNone(agent_server.macos_launchd_owns_current_process())

    def test_cooperative_kill_delay_covers_every_shutdown_phase(self):
        self.assertGreaterEqual(
            agent_server.SERVER_RESTART_GRACEFUL_KILL_DELAY_SECONDS,
            agent_server.configured_uvicorn_graceful_shutdown_seconds()
            + agent_server.SERVER_SHUTDOWN_PHASE_COUNT
            * agent_server.SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS,
        )
        source = Path(agent_server.__file__).read_text()
        self.assertEqual(
            source.count("await bounded_shutdown_phase("),
            agent_server.SERVER_SHUTDOWN_PHASE_COUNT,
        )


class DarwinMetricsTests(unittest.TestCase):
    def test_elapsed_time_parsing(self):
        self.assertEqual(agent_server.parse_elapsed_seconds("05:33"), 333)
        self.assertEqual(agent_server.parse_elapsed_seconds("01:02:03"), 3723)
        self.assertEqual(agent_server.parse_elapsed_seconds("10-17:57:40"), 928660)
        with self.assertRaises(ValueError):
            agent_server.parse_elapsed_seconds("")

    def test_darwin_ps_rows_parse_bsd_output(self):
        stdout = (
            "52965 52964 52924 S    03:58 14.6  4.4 1641904 codex codex app-server --listen stdio://\n"
            "  791     1   791 Ss 10-18:28:56 40.6  0.1 23056 backupd /System/Library/CoreServices/TimeMachine/backupd\n"
            "garbage line\n"
        )
        rows = agent_server.parse_darwin_ps_rows(stdout)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pid"], 52965)
        self.assertEqual(rows[0]["sid"], 52924)
        self.assertEqual(rows[0]["elapsed_seconds"], 238)
        self.assertEqual(rows[0]["rss_kb"], 1641904)
        self.assertEqual(rows[0]["command"], "codex")
        self.assertEqual(rows[0]["args"], "codex app-server --listen stdio://")
        self.assertEqual(rows[1]["elapsed_seconds"], 10 * 86400 + 18 * 3600 + 28 * 60 + 56)

    def test_vm_stat_available_memory(self):
        stdout = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               100000.\n"
            "Pages active:                            2000000.\n"
            "Pages inactive:                           200000.\n"
            "Pages speculative:                         50000.\n"
            "Pages purgeable:                           10000.\n"
            "Pages wired down:                         900000.\n"
        )
        expected = int((100000 + 200000 + 50000 + 10000) * 16384 / (1024 * 1024))
        self.assertEqual(agent_server.parse_vm_stat_available_mb(stdout), expected)
        self.assertIsNone(agent_server.parse_vm_stat_available_mb("no pages here"))

    def test_darwin_memory_probe_is_cached(self):
        completed = MagicMock(returncode=0, stdout=(
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free: 1024.\n"
        ))
        with patch.object(
            agent_server,
            "DARWIN_MEMORY_CACHE",
            {"at": float("-inf"), "mb": None},
        ), patch.object(agent_server.subprocess, "run", return_value=completed) as run:
            first = agent_server.darwin_available_memory_mb()
            second = agent_server.darwin_available_memory_mb()
        self.assertEqual(first, 4)
        self.assertEqual(second, 4)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
