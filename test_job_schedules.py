import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import agent_server


def timestamp(value: str, timezone_name: str = "UTC") -> float:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone_name)).timestamp()


def local_time(value: float, timezone_name: str) -> datetime:
    return datetime.fromtimestamp(value, tz=ZoneInfo(timezone_name))


class JobOccurrenceTests(unittest.TestCase):
    def test_interval_skips_missed_slots_without_drifting(self) -> None:
        job = {
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "schedule_start_at": 100.0,
        }
        self.assertEqual(agent_server.next_job_occurrence(job, 275.0), 280.0)
        self.assertEqual(agent_server.next_job_occurrence(job, 280.0), 340.0)
        with self.assertRaisesRegex(HTTPException, "at most"):
            agent_server.normalize_interval_seconds(10**20)

    def test_cron_supports_alias_seconds_year_and_stable_hash(self) -> None:
        for expression in ("@hourly", "*/10 * * * * *", "0 0 9 * * * 2027", "H 9 * * *"):
            normalized = agent_server.normalize_cron_expression(expression, "job_stable")
            self.assertEqual(normalized, expression)

        hashed = {
            "id": "job_stable",
            "schedule_kind": "cron",
            "cron_expression": "H 9 * * *",
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-01-01T00:00:00"),
        }
        first = agent_server.next_job_occurrence(hashed, timestamp("2026-01-01T00:00:00"))
        second_read = agent_server.next_job_occurrence(hashed, timestamp("2026-01-01T00:00:00"))
        self.assertEqual(first, second_read)

    def test_cron_rejects_random_and_invalid_expressions(self) -> None:
        with self.assertRaisesRegex(HTTPException, "random"):
            agent_server.normalize_cron_expression("R 9 * * *", "job_1")
        with self.assertRaisesRegex(HTTPException, "invalid cron"):
            agent_server.normalize_cron_expression("99 99 99", "job_1")

    def test_cron_keeps_wall_time_and_skips_dst_gap_and_second_fold(self) -> None:
        zone = "America/Los_Angeles"
        daily = {
            "id": "job_daily",
            "schedule_kind": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": zone,
            "schedule_start_at": timestamp("2026-03-07T00:00:00", zone),
        }
        next_daily = agent_server.next_job_occurrence(daily, timestamp("2026-03-07T10:00:00", zone))
        self.assertEqual(local_time(next_daily, zone).isoformat(), "2026-03-08T09:00:00-07:00")

        missing = {**daily, "cron_expression": "30 2 * * *"}
        next_missing = agent_server.next_job_occurrence(missing, timestamp("2026-03-07T03:00:00", zone))
        self.assertEqual(local_time(next_missing, zone).isoformat(), "2026-03-09T02:30:00-07:00")

        folded = {
            **daily,
            "cron_expression": "30 1 * * *",
            "schedule_start_at": timestamp("2026-10-31T00:00:00", zone),
        }
        first = agent_server.next_job_occurrence(folded, timestamp("2026-10-31T03:00:00", zone))
        second = agent_server.next_job_occurrence(folded, first)
        self.assertEqual(local_time(first, zone).isoformat(), "2026-11-01T01:30:00-07:00")
        self.assertEqual(local_time(second, zone).isoformat(), "2026-11-02T01:30:00-08:00")

    def test_rrule_accepts_prefix_count_and_all_by_fields(self) -> None:
        zone = "America/New_York"
        anchor = timestamp("2026-01-01T08:00:00", zone)
        expression = agent_server.normalize_rrule_expression(
            "RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;BYHOUR=9;BYMINUTE=15",
            zone,
            anchor,
        )
        self.assertTrue(expression.startswith("FREQ=MONTHLY"))
        job = {
            "id": "job_rule",
            "schedule_kind": "rrule",
            "rrule": expression,
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        first = agent_server.next_job_occurrence(job, anchor, inclusive=True)
        second = agent_server.next_job_occurrence(job, first)
        third = agent_server.next_job_occurrence(job, second)
        fourth = agent_server.next_job_occurrence(job, third)
        self.assertEqual(local_time(first, zone).strftime("%Y-%m-%d %H:%M"), "2026-01-01 09:15")
        self.assertEqual(local_time(second, zone).strftime("%Y-%m-%d %H:%M"), "2026-02-02 09:15")
        self.assertEqual(local_time(third, zone).strftime("%Y-%m-%d %H:%M"), "2026-03-02 09:15")
        self.assertIsNone(fourth)

    def test_rrule_skips_nonexistent_dst_occurrence(self) -> None:
        zone = "America/Los_Angeles"
        anchor = timestamp("2026-03-07T03:00:00", zone)
        job = {
            "id": "job_rule",
            "schedule_kind": "rrule",
            "rrule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=30",
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        next_run = agent_server.next_job_occurrence(job, anchor)
        self.assertEqual(local_time(next_run, zone).isoformat(), "2026-03-09T02:30:00-07:00")

    def test_rrule_rejects_calendar_documents_and_timezone_is_strict(self) -> None:
        with self.assertRaisesRegex(HTTPException, "one RFC 5545 RRULE"):
            agent_server.normalize_rrule_expression(
                "DTSTART:20260101T090000\nRRULE:FREQ=DAILY",
                "UTC",
                timestamp("2026-01-01T00:00:00"),
            )
        with self.assertRaisesRegex(HTTPException, "IANA timezone"):
            agent_server.normalize_job_timezone("Mars/Olympus_Mons")
        with self.assertRaisesRegex(HTTPException, "does not exist"):
            agent_server.parse_job_timestamp("2026-03-08T02:30:00", "America/Los_Angeles")
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaisesRegex(HTTPException, "finite"):
                agent_server.parse_job_timestamp(value)
        for value in ("1e300", "-1e300"):
            with self.assertRaisesRegex(HTTPException, "supported range"):
                agent_server.parse_job_timestamp(value)

    def test_rrule_rejects_nonprogressing_and_out_of_range_parts(self) -> None:
        anchor = timestamp("2026-01-01T00:00:00")
        for expression in (
            "FREQ=DAILY;INTERVAL=0",
            "FREQ=DAILY;INTERVAL=-1",
            "FREQ=DAILY;COUNT=0",
            "FREQ=YEARLY;BYMONTH=0",
            "FREQ=DAILY;BYHOUR=24",
            "FREQ=MONTHLY;BYDAY=53MO",
            f"FREQ=DAILY;COUNT={'9' * 5000}",
            f"FREQ=DAILY;BYSECOND={'9' * 5000}",
            "FREQ=MINUTELY;BYSECOND=60",
        ):
            with self.subTest(expression=expression), self.assertRaises(HTTPException):
                agent_server.normalize_rrule_expression(expression, "UTC", anchor)

    def test_rrule_count_ignores_nonexistent_dst_instances(self) -> None:
        zone = "America/Los_Angeles"
        anchor = timestamp("2026-03-07T02:30:00", zone)
        job = {
            "id": "job_count",
            "schedule_kind": "rrule",
            "rrule": "FREQ=DAILY;COUNT=2;BYHOUR=2;BYMINUTE=30;BYSECOND=0",
            "timezone": zone,
            "schedule_start_at": anchor,
        }
        second = agent_server.next_job_occurrence(job, anchor)
        self.assertEqual(local_time(second, zone).isoformat(), "2026-03-09T02:30:00-07:00")
        self.assertIsNone(agent_server.next_job_occurrence(job, second))

    def test_large_count_rule_exhaustion_is_bounded(self) -> None:
        anchor = timestamp("2026-01-01T00:00:00")
        job = {
            "id": "job_count",
            "schedule_kind": "rrule",
            "rrule": "FREQ=SECONDLY;COUNT=10000",
            "timezone": "UTC",
            "schedule_start_at": anchor,
        }
        self.assertIsNone(agent_server.next_job_occurrence(job, anchor + 20_000))

    def test_exhausted_year_limited_cron_has_no_next_occurrence(self) -> None:
        job = {
            "id": "job_year",
            "schedule_kind": "cron",
            "cron_expression": "0 0 9 1 1 * 2026",
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-01-01T00:00:00"),
        }
        self.assertIsNone(agent_server.next_job_occurrence(job, timestamp("2027-01-01T00:00:00")))


class JobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_for_session_disables_enabled_jobs_and_emits_updates(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs = {
            "job_interval": {
                "id": "job_interval",
                "session_id": "sess_archived",
                "title": "Interval target",
                "prompt": "Do not include this in events",
                "schedule_kind": "interval",
                "interval_seconds": 60,
                "enabled": True,
                "next_run_at": 120.0,
                "scheduled_run_at": 120.0,
                "run_count": 7,
            },
            "job_cron": {
                "id": "job_cron",
                "session_id": "sess_archived",
                "title": "Cron target",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "enabled": True,
                "next_run_at": 130.0,
                "scheduled_run_at": 130.0,
            },
            "job_rrule": {
                "id": "job_rrule",
                "session_id": "sess_archived",
                "title": "RRULE target",
                "schedule_kind": "rrule",
                "rrule": "FREQ=DAILY;COUNT=3",
                "enabled": True,
                "next_run_at": 140.0,
                "scheduled_run_at": 140.0,
            },
            "job_already_paused": {
                "id": "job_already_paused",
                "session_id": "sess_archived",
                "title": "Already paused",
                "enabled": False,
                "next_run_at": None,
                "scheduled_run_at": None,
            },
            "job_other_chat": {
                "id": "job_other_chat",
                "session_id": "sess_active",
                "title": "Other chat",
                "enabled": True,
                "next_run_at": 180.0,
                "scheduled_run_at": 180.0,
            },
        }
        events = AsyncMock()

        with (
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(agent_server, "append_event", events),
        ):
            paused = await store.pause_for_session("sess_archived")

        self.assertEqual(paused, 3)
        for job_id in ("job_interval", "job_cron", "job_rrule"):
            self.assertFalse(store.jobs[job_id]["enabled"])
            self.assertIsNone(store.jobs[job_id]["next_run_at"])
            self.assertIsNone(store.jobs[job_id]["scheduled_run_at"])
        self.assertEqual(store.jobs["job_interval"]["interval_seconds"], 60)
        self.assertEqual(store.jobs["job_interval"]["run_count"], 7)
        self.assertEqual(store.jobs["job_cron"]["cron_expression"], "0 9 * * *")
        self.assertEqual(store.jobs["job_rrule"]["rrule"], "FREQ=DAILY;COUNT=3")
        self.assertFalse(store.jobs["job_already_paused"]["enabled"])
        self.assertTrue(store.jobs["job_other_chat"]["enabled"])
        self.assertEqual(store.jobs["job_other_chat"]["next_run_at"], 180.0)
        save.assert_awaited_once()
        self.assertEqual(events.await_count, 3)
        self.assertEqual(
            {call.args[2]["job_id"] for call in events.await_args_list},
            {"job_interval", "job_cron", "job_rrule"},
        )
        for call in events.await_args_list:
            self.assertEqual(call.args[0], "sess_archived")
            self.assertEqual(call.args[1], "job_updated")
            self.assertFalse(call.args[2]["job"]["enabled"])
            self.assertNotIn("prompt", call.args[2]["job"])

        events.reset_mock()
        save.reset_mock()
        self.assertEqual(await store.pause_for_session("sess_archived"), 0)
        save.assert_not_awaited()
        events.assert_not_awaited()

    async def test_pause_for_session_save_failure_stays_safe_in_memory(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_target"] = {
            "id": "job_target",
            "session_id": "sess_archived",
            "title": "Target",
            "enabled": True,
            "next_run_at": 120.0,
            "scheduled_run_at": 120.0,
        }
        events = AsyncMock()

        with (
            patch.object(
                store,
                "save",
                AsyncMock(side_effect=OSError("disk full")),
            ),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                await store.pause_for_session("sess_archived")

        self.assertFalse(store.jobs["job_target"]["enabled"])
        self.assertIsNone(store.jobs["job_target"]["next_run_at"])
        self.assertIsNone(store.jobs["job_target"]["scheduled_run_at"])
        events.assert_not_awaited()

    async def test_load_pauses_enabled_jobs_for_archived_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_archived": {
                    "id": "job_archived",
                    "session_id": "sess_archived",
                    "title": "Archived job",
                    "prompt": "Do not run",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "schedule_start_at": 120.0,
                    "scheduled_run_at": 120.0,
                    "next_run_at": 120.0,
                    "enabled": True,
                    "context_mode": "chat",
                    "run_count": 0,
                },
                "job_active": {
                    "id": "job_active",
                    "session_id": "sess_active",
                    "title": "Active job",
                    "prompt": "Still run",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "schedule_start_at": 180.0,
                    "scheduled_run_at": 180.0,
                    "next_run_at": 180.0,
                    "enabled": True,
                    "context_mode": "chat",
                    "run_count": 0,
                },
            }))
            store = agent_server.JobStore()
            sessions = {
                "sess_archived": {"id": "sess_archived", "archived": True},
                "sess_active": {"id": "sess_active", "archived": False},
            }

            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
                patch.object(agent_server.STORE, "sessions", sessions),
            ):
                await store.load()

            self.assertFalse(store.jobs["job_archived"]["enabled"])
            self.assertIsNone(store.jobs["job_archived"]["next_run_at"])
            self.assertIsNone(store.jobs["job_archived"]["scheduled_run_at"])
            self.assertTrue(store.jobs["job_active"]["enabled"])
            persisted = json.loads(jobs_file.read_text())
            self.assertFalse(persisted["job_archived"]["enabled"])
            self.assertIsNone(persisted["job_archived"]["next_run_at"])
            self.assertIsNone(persisted["job_archived"]["scheduled_run_at"])

    async def test_create_rejects_an_archived_parent_session(self) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_create"
        request = agent_server.CreateJobRequest(
            session_id=session_id,
            title="Should not exist",
            prompt="Do not schedule",
        )

        with patch.object(agent_server.STORE, "sessions", {
            session_id: {"id": session_id, "archived": True},
        }):
            with self.assertRaises(HTTPException) as raised:
                await store.create(request)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("unarchive", str(raised.exception.detail).lower())
        self.assertEqual(store.jobs, {})

    async def test_archived_session_job_cannot_be_enabled_but_can_be_edited(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_update"
        store.jobs["job_paused"] = {
            "id": "job_paused",
            "session_id": session_id,
            "title": "Paused",
            "prompt": "Do not run",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": None,
            "next_run_at": None,
            "enabled": False,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        original = dict(store.jobs["job_paused"])
        save = AsyncMock()
        events = AsyncMock()

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(store, "save", save),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(HTTPException) as raised:
                await store.update("job_paused", {"enabled": True})
            self.assertEqual(store.jobs["job_paused"], original)
            save.assert_not_awaited()
            events.assert_not_awaited()

            updated = await store.update(
                "job_paused",
                {"title": "Renamed while paused", "enabled": False},
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("unarchive", str(raised.exception.detail).lower())
        self.assertEqual(updated["title"], "Renamed while paused")
        self.assertFalse(updated["enabled"])
        self.assertIsNone(updated["next_run_at"])
        save.assert_awaited_once()
        events.assert_awaited_once()

    async def test_scheduler_pauses_due_archived_jobs_without_running_or_deferring(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_due"
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Due but archived",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        blocker = AsyncMock(return_value=None)
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(store, "run_job", new_callable=AsyncMock) as run_job,
            patch.object(store, "defer", new_callable=AsyncMock) as defer,
            patch.object(agent_server, "scheduled_job_blocker", blocker),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        self.assertFalse(store.jobs["job_due"]["enabled"])
        self.assertIsNone(store.jobs["job_due"]["next_run_at"])
        self.assertIsNone(store.jobs["job_due"]["scheduled_run_at"])
        blocker.assert_not_awaited()
        run_job.assert_not_awaited()
        defer.assert_not_awaited()
        events.assert_awaited_once()
        self.assertEqual(events.await_args.args[1], "job_updated")

    async def test_scheduler_does_not_dispatch_a_job_paused_during_blocker_check(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_pause_race"
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Due before pause",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def pause_while_checking(_session_id: str) -> None:
            store.jobs["job_due"]["enabled"] = False
            store.jobs["job_due"]["next_run_at"] = None
            store.jobs["job_due"]["scheduled_run_at"] = None
            return None

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                side_effect=pause_while_checking,
            ),
            patch.object(store, "run_job", new_callable=AsyncMock) as run_job,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        run_job.assert_not_awaited()

    async def test_scheduler_treats_a_late_archive_rejection_as_a_pause(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archive_during_dispatch"
        session = {"id": session_id, "archived": False}
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Archive race",
            "prompt": "Do not retry",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "run_count": 0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def reject_after_archive(_job_id: str) -> None:
            session["archived"] = True
            raise HTTPException(
                status_code=409,
                detail="archived chats cannot start turns",
            )

        pause = AsyncMock(return_value=1)
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {session_id: session}),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "run_job", side_effect=reject_after_archive),
            patch.object(store, "pause_for_session", pause),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        pause.assert_awaited_once_with(session_id)
        events.assert_not_awaited()
        self.assertEqual(store.jobs["job_due"]["run_count"], 0)

    async def test_run_now_rejects_an_archived_session_and_pauses_its_jobs(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_run"
        store.jobs["job_run"] = {
            "id": "job_run",
            "session_id": session_id,
            "title": "Run now",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(store, "pause_for_session", new_callable=AsyncMock) as pause,
            patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start,
        ):
            with self.assertRaises(HTTPException) as raised:
                await store.run_job("job_run")

        self.assertEqual(raised.exception.status_code, 409)
        pause.assert_awaited_once_with(session_id)
        start.assert_not_awaited()

    async def test_delete_for_session_restores_jobs_when_persistence_fails(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_retry"] = {
            "id": "job_retry",
            "session_id": "missing-chat",
            "title": "Retry cleanup",
        }
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=[OSError("disk full"), None]),
        ) as save:
            with self.assertRaisesRegex(OSError, "disk full"):
                await store.delete_for_session("missing-chat")
            self.assertIn("job_retry", store.jobs)

            self.assertEqual(
                await store.delete_for_session("missing-chat"),
                1,
            )

        self.assertNotIn("job_retry", store.jobs)
        self.assertEqual(save.await_count, 2)

    async def test_scheduler_cleanup_failure_does_not_resurrect_missing_session(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_orphan"] = {
            "id": "job_orphan",
            "session_id": "missing-chat",
            "title": "Orphan",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", root / "jobs.json"),
                patch.object(agent_server.STORE, "sessions", {}),
                patch.object(agent_server.time, "time", return_value=2.0),
                patch.object(
                    agent_server.asyncio,
                    "sleep",
                    side_effect=one_scheduler_iteration,
                ),
                patch.object(
                    store,
                    "save",
                    AsyncMock(side_effect=OSError("disk full")),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await store.scheduler_loop()

            self.assertFalse((root / "sessions" / "missing-chat").exists())

        self.assertIn("job_orphan", store.jobs)

    async def test_load_migrates_legacy_interval_without_rescheduling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_old": {
                    "id": "job_old",
                    "session_id": "sess_1",
                    "interval_seconds": 60,
                    "loop": True,
                    "enabled": True,
                    "next_run_at": 12345.0,
                    "run_count": 7,
                }
            }))
            store = agent_server.JobStore()
            with patch.object(agent_server, "STATE_DIR", root), patch.object(agent_server, "JOBS_FILE", jobs_file):
                await store.load()

            migrated = store.jobs["job_old"]
            self.assertEqual(migrated["schedule_kind"], "interval")
            self.assertEqual(migrated["timezone"], "UTC")
            self.assertEqual(migrated["next_run_at"], 12345.0)
            self.assertEqual(migrated["scheduled_run_at"], 12345.0)
            self.assertEqual(migrated["run_count"], 7)
            self.assertTrue(migrated["enabled"])

    async def test_explicit_first_cron_run_is_exact_then_returns_to_rule(self) -> None:
        store = agent_server.JobStore()
        now = timestamp("2026-07-21T08:00:00", "America/Los_Angeles")
        first = timestamp("2026-07-21T10:17:00", "America/Los_Angeles")
        agent_server.STORE.sessions["sess_test"] = {"id": "sess_test"}
        request = agent_server.CreateJobRequest(
            session_id="sess_test",
            title="Daily",
            prompt="Check",
            schedule_kind="cron",
            cron_expression="0 9 * * *",
            timezone="America/Los_Angeles",
            first_run_at="2026-07-21T10:17:00",
        )
        try:
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=now):
                job = await store.create(request)
            self.assertEqual(job["next_run_at"], first)

            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=first + 60):
                await store.mark_ran(job["id"])
            expected = timestamp("2026-07-22T09:00:00", "America/Los_Angeles")
            self.assertEqual(store.jobs[job["id"]]["next_run_at"], expected)
        finally:
            agent_server.STORE.sessions.pop("sess_test", None)

    async def test_defer_preserves_canonical_interval_cadence(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_1"] = {
            "id": "job_1",
            "session_id": "sess_1",
            "title": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": 1060.0,
            "scheduled_run_at": 1060.0,
            "next_run_at": 1060.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1065.0):
            await store.defer("job_1", "busy", delay_seconds=300)
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1365.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1060.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 0)

        # Repeated busy checks replace the one retry deadline. They must not
        # enqueue multiple catch-up executions for the missed intervals.
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1100.0):
            await store.defer("job_1", "still busy", delay_seconds=300)
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1400.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1060.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 0)

        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1405.0):
            await store.mark_ran("job_1")
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1420.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1420.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 1)

    async def test_scoped_update_delete_enforce_ownership_and_emit_events(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_1"] = {
            "id": "job_1",
            "session_id": "sess_owner",
            "title": "Check",
            "prompt": "private prompt",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 1060.0,
            "next_run_at": 1060.0,
            "enabled": True,
            "loop": True,
            "run_count": 0,
        }
        with self.assertRaises(HTTPException):
            await store.update("job_1", {"title": "No"}, expected_session_id="sess_other")
        with self.assertRaises(HTTPException):
            await store.delete("job_1", expected_session_id="sess_other")

        events = AsyncMock()
        with patch.object(store, "save", new_callable=AsyncMock), patch.object(agent_server, "append_event", events):
            await store.update("job_1", {"title": "Updated"}, expected_session_id="sess_owner")
            await store.delete("job_1", expected_session_id="sess_owner")
        self.assertEqual([call.args[1] for call in events.await_args_list], ["job_updated", "job_deleted"])
        self.assertNotIn("prompt", events.await_args_list[0].args[2]["job"])
        self.assertNotIn("prompt", events.await_args_list[1].args[2]["job"])

    async def test_legacy_interval_edit_cannot_convert_a_calendar_job(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-07-21T08:00:00"),
            "scheduled_run_at": timestamp("2026-07-21T09:00:00"),
            "next_run_at": timestamp("2026-07-21T09:00:00"),
            "enabled": True,
            "loop": True,
            "max_runs": 3,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock):
            updated = await store.update("job_cron", {
                "title": "Renamed by a v7 client",
                "interval_seconds": 3600,
                "loop": True,
                "max_runs": None,
            })
        self.assertEqual(updated["title"], "Renamed by a v7 client")
        self.assertEqual(updated["schedule_kind"], "cron")
        self.assertEqual(updated["cron_expression"], "0 9 * * *")
        self.assertIsNone(updated["interval_seconds"])
        self.assertEqual(updated["max_runs"], 3)

    async def test_rejected_schedule_update_is_atomic(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Hourly",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        before = json.loads(json.dumps(store.jobs["job_interval"]))
        with self.assertRaises(HTTPException):
            await store.update("job_interval", {
                "schedule_kind": "cron",
                "interval_seconds": None,
                "cron_expression": "not a cron expression",
            })
        self.assertEqual(store.jobs["job_interval"], before)
        with self.assertRaises(HTTPException):
            await store.update("job_interval", {"interval_seconds": 10**20})
        self.assertEqual(store.jobs["job_interval"], before)

    async def test_metadata_only_interval_edit_does_not_reschedule(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Hourly",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=2000.25):
            updated = await store.update("job_interval", {"title": "Renamed", "timezone": None})
        self.assertEqual(updated["schedule_start_at"], 1000.0)
        self.assertEqual(updated["next_run_at"], 4600.0)

    async def test_schedule_kind_switch_preserves_finite_run_limit(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_finite"] = {
            "id": "job_finite",
            "session_id": "sess_owner",
            "title": "Finite",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": False,
            "max_runs": 3,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=2000.25):
            cron = await store.update("job_finite", {
                "schedule_kind": "cron",
                "interval_seconds": None,
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
            })
            rrule = await store.update("job_finite", {
                "schedule_kind": "rrule",
                "cron_expression": None,
                "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                "timezone": "UTC",
            })
        self.assertEqual(cron["max_runs"], 3)
        self.assertEqual(rrule["max_runs"], 3)

    async def test_count_one_rrule_schedules_exactly_one_run(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_count"] = {"id": "sess_count"}
        request = agent_server.CreateJobRequest(
            session_id="sess_count",
            title="Once",
            prompt="Check",
            schedule_kind="rrule",
            rrule="FREQ=DAILY;COUNT=1",
            timezone="UTC",
        )
        try:
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=2000.25):
                job = await store.create(request)
            self.assertEqual(job["next_run_at"], 2001.0)
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=2001.5):
                await store.mark_ran(job["id"])
            self.assertEqual(store.jobs[job["id"]]["run_count"], 1)
            self.assertFalse(store.jobs[job["id"]]["enabled"])
            self.assertIsNone(store.jobs[job["id"]]["next_run_at"])
        finally:
            agent_server.STORE.sessions.pop("sess_count", None)

    async def test_load_defaults_legacy_jobs_to_chat_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_old": {
                    "id": "job_old",
                    "session_id": "sess_1",
                    "title": "Legacy",
                    "prompt": "Check",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "enabled": False,
                    "run_count": 0,
                }
            }))
            store = agent_server.JobStore()
            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
            ):
                await store.load()

            self.assertEqual(store.jobs["job_old"]["context_mode"], "chat")
            persisted = json.loads(jobs_file.read_text())
            self.assertEqual(persisted["job_old"]["context_mode"], "chat")

    async def test_create_and_update_job_context_mode(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_context"] = {"id": "sess_context"}
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                default_job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_context",
                    title="Default",
                    prompt="Check",
                ))
                standalone_job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_context",
                    title="Standalone",
                    prompt="Check",
                    context_mode="standalone",
                ))
                updated = await store.update(
                    standalone_job["id"],
                    {"context_mode": "chat"},
                )

            self.assertEqual(default_job["context_mode"], "chat")
            self.assertEqual(standalone_job["context_mode"], "standalone")
            self.assertEqual(updated["context_mode"], "chat")
        finally:
            agent_server.STORE.sessions.pop("sess_context", None)

    async def test_legacy_create_infers_standalone_for_alternate_backend(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_backend_contract"] = {
            "id": "sess_backend_contract",
            "backend": agent_server.BACKEND_CODEX,
        }
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                inherited = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Inherited",
                    prompt="Check",
                ))
                matching = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Matching",
                    prompt="Check",
                    backend=agent_server.BACKEND_CODEX,
                ))
                explicit_standalone = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                    context_mode="standalone",
                ))
                legacy_standalone = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Legacy independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                ))
                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.create(agent_server.CreateJobRequest(
                        session_id="sess_backend_contract",
                        title="Invalid same-chat backend",
                        prompt="Check",
                        backend=agent_server.BACKEND_CLAUDE,
                        context_mode="chat",
                    ))

            self.assertIsNone(inherited["backend"])
            self.assertEqual(matching["backend"], agent_server.BACKEND_CODEX)
            self.assertEqual(
                explicit_standalone["backend"],
                agent_server.BACKEND_CLAUDE,
            )
            self.assertEqual(explicit_standalone["context_mode"], "standalone")
            self.assertEqual(
                legacy_standalone["backend"],
                agent_server.BACKEND_CLAUDE,
            )
            self.assertEqual(legacy_standalone["context_mode"], "standalone")
            self.assertEqual(len(store.jobs), 4)
        finally:
            agent_server.STORE.sessions.pop("sess_backend_contract", None)

    async def test_context_mode_update_validates_resulting_backend_atomically(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_backend_update"] = {
            "id": "sess_backend_update",
            "backend": agent_server.BACKEND_CODEX,
        }
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_update",
                    title="Independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                    context_mode="standalone",
                ))
                before = dict(store.jobs[job["id"]])
                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.update(job["id"], {"context_mode": "chat"})
                self.assertEqual(store.jobs[job["id"]], before)

                same_chat = await store.update(job["id"], {
                    "context_mode": "chat",
                    "backend": agent_server.BACKEND_CODEX,
                })
                self.assertEqual(same_chat["context_mode"], "chat")
                self.assertEqual(same_chat["backend"], agent_server.BACKEND_CODEX)

                legacy_update = await store.update(
                    job["id"],
                    {"backend": agent_server.BACKEND_CLAUDE},
                )
                self.assertEqual(legacy_update["context_mode"], "standalone")
                self.assertEqual(
                    store.jobs[job["id"]]["backend"],
                    agent_server.BACKEND_CLAUDE,
                )

                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.update(job["id"], {"context_mode": "chat"})
                self.assertEqual(
                    store.jobs[job["id"]]["context_mode"],
                    "standalone",
                )
        finally:
            agent_server.STORE.sessions.pop("sess_backend_update", None)

    async def test_run_job_forwards_context_mode_and_projects_run_event(self) -> None:
        for stored_mode, expected_mode in (
            (None, "chat"),
            ("standalone", "standalone"),
        ):
            with self.subTest(stored_mode=stored_mode):
                store = agent_server.JobStore()
                job = {
                    "id": "job_context",
                    "session_id": "sess_context",
                    "title": "Context check",
                    "prompt": "Check now",
                    "schedule_kind": "interval",
                    "timezone": "UTC",
                    "enabled": False,
                    "run_count": 0,
                }
                if stored_mode is not None:
                    job["context_mode"] = stored_mode
                store.jobs[job["id"]] = job
                start_turn = AsyncMock(return_value={"run_id": "run_context"})
                events = AsyncMock()
                with (
                    patch.object(agent_server, "start_turn", start_turn),
                    patch.object(store, "mark_ran", new_callable=AsyncMock),
                    patch.object(agent_server, "append_event", events),
                ):
                    result = await store.run_job(job["id"])

                self.assertEqual(result["run_id"], "run_context")
                self.assertEqual(
                    start_turn.await_args.kwargs["provider_context_mode"],
                    expected_mode,
                )
                self.assertFalse(start_turn.await_args.kwargs["queue_if_busy"])
                self.assertEqual(
                    events.await_args.args[2]["context_mode"],
                    expected_mode,
                )

    async def test_load_migrates_and_runs_legacy_alternate_backend_standalone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_legacy_claude": {
                    "id": "job_legacy_claude",
                    "session_id": "sess_legacy_codex",
                    "title": "Legacy Claude job",
                    "prompt": "Check independently",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "enabled": False,
                    "backend": agent_server.BACKEND_CLAUDE,
                    "run_count": 0,
                }
            }))
            store = agent_server.JobStore()
            parent = {
                "id": "sess_legacy_codex",
                "backend": agent_server.BACKEND_CODEX,
            }
            start_turn = AsyncMock(return_value={"run_id": "run_legacy"})
            events = AsyncMock()
            with (
                patch.object(agent_server.STORE, "sessions", {
                    "sess_legacy_codex": parent,
                }),
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
            ):
                await store.load()
                persisted = json.loads(jobs_file.read_text())
                self.assertEqual(
                    persisted["job_legacy_claude"]["context_mode"],
                    "standalone",
                )
                with (
                    patch.object(agent_server, "start_turn", start_turn),
                    patch.object(store, "mark_ran", new_callable=AsyncMock),
                    patch.object(agent_server, "append_event", events),
                ):
                    result = await store.run_job("job_legacy_claude")

            self.assertEqual(result["run_id"], "run_legacy")
            turn_request = start_turn.await_args.args[1]
            self.assertEqual(turn_request.backend, agent_server.BACKEND_CLAUDE)
            self.assertEqual(
                start_turn.await_args.kwargs["provider_context_mode"],
                "standalone",
            )


if __name__ == "__main__":
    unittest.main()
