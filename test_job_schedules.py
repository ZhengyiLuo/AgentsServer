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

        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1370.0):
            await store.mark_ran("job_1")
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1420.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1420.0)

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


if __name__ == "__main__":
    unittest.main()
