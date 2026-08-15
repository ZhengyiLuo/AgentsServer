import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import agent_server


class JobRunHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = agent_server.STATE_DIR
        agent_server.STATE_DIR = Path(self.temporary.name)
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()
        self.session_id = "job-history-chat"
        path = agent_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write_events(self.history_events())

    def tearDown(self) -> None:
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()
        agent_server.STATE_DIR = self.previous_state_dir
        self.temporary.cleanup()

    def test_returns_best_run_results_newest_first_with_cursor_paging(self) -> None:
        latest = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=2,
        )

        self.assertEqual(latest["total"], 5)
        self.assertTrue(latest["has_more"])
        self.assertEqual(
            [event["run_id"] for event in latest["runs"]],
            ["run-5", "run-4"],
        )
        self.assertEqual(latest["runs"][0]["type"], "job_finished")
        self.assertEqual(latest["runs"][0]["result_text"], "Result 5")
        self.assertEqual(latest["runs"][0]["job_run_status"], "completed")
        self.assertEqual(latest["runs"][0]["job_status"], "completed")
        self.assertEqual(latest["runs"][0]["job_status_run_id"], "run-5")
        self.assertEqual(latest["runs"][1]["type"], "job_error")
        self.assertEqual(latest["runs"][1]["message"], "Run 4 failed")
        self.assertEqual(latest["runs"][1]["result_text"], "Result 4")
        self.assertEqual(latest["runs"][1]["job_run_status"], "failed")
        self.assertEqual(latest["runs"][1]["job_status"], "failed")
        self.assertEqual(
            latest["next_before"],
            min(event["seq"] for event in latest["runs"]),
        )

        middle = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            before_seq=latest["next_before"],
            limit=2,
        )
        self.assertEqual(
            [event["run_id"] for event in middle["runs"]],
            ["run-3", "run-2"],
        )
        self.assertTrue(middle["has_more"])

        oldest = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            before_seq=middle["next_before"],
            limit=2,
        )
        self.assertEqual(
            [event["run_id"] for event in oldest["runs"]],
            ["run-1"],
        )
        self.assertFalse(oldest["has_more"])
        self.assertIsNone(oldest["next_before"])

    def test_incrementally_adds_appended_runs_without_losing_cached_history(self) -> None:
        first = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        path = agent_server.events_path(self.session_id)
        appended = [
            self.event(
                30,
                "turn_started",
                run_id="run-6",
                purpose="scheduled_job",
                job_id="job-1",
                prompt="Poll",
            ),
            self.event(
                31,
                "turn_finished",
                run_id="run-6",
                purpose="scheduled_job",
                job_id="job-1",
                result_text="Result 6",
            ),
        ]
        with path.open("a", encoding="utf-8") as output:
            output.write("".join(json.dumps(event) + "\n" for event in appended))

        refreshed = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(first["total"], 5)
        self.assertEqual(refreshed["total"], 6)
        self.assertEqual(refreshed["runs"][0]["run_id"], "run-6")
        self.assertEqual(
            {event["run_id"] for event in refreshed["runs"][1:]},
            {f"run-{number}" for number in range(1, 6)},
        )

    def test_incremental_index_does_not_advance_past_an_incomplete_jsonl_tail(self) -> None:
        path = agent_server.events_path(self.session_id)
        pending = json.dumps(self.event(
            30,
            "turn_finished",
            run_id="run-6",
            purpose="scheduled_job",
            job_id="job-1",
            result_text="Result 6",
        ))
        split = len(pending) // 2
        with path.open("ab") as output:
            output.write(pending[:split].encode())

        partial = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        with path.open("ab") as output:
            output.write(pending[split:].encode() + b"\n")
        complete = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(partial["total"], 5)
        self.assertEqual(complete["total"], 6)
        self.assertEqual(complete["runs"][0]["run_id"], "run-6")

    def test_combines_partial_output_with_authoritative_stop_and_failure(self) -> None:
        self.write_events([
            self.event(
                1,
                "job_created",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
            self.event(
                2,
                "turn_started",
                run_id="stopped-run",
                purpose="scheduled_job",
                job_id="job-1",
                prompt="Poll",
            ),
            self.event(
                3,
                "assistant_text",
                run_id="stopped-run",
                purpose="scheduled_job",
                job_id="job-1",
                text="Partial stopped output",
            ),
            self.event(
                4,
                "turn_stopped",
                run_id="stopped-run",
                purpose="scheduled_job",
                job_id="job-1",
            ),
            self.event(
                5,
                "turn_started",
                run_id="failed-run",
                purpose="scheduled_job",
                job_id="job-1",
                prompt="Poll",
            ),
            self.event(
                6,
                "assistant_text",
                run_id="failed-run",
                purpose="scheduled_job",
                job_id="job-1",
                text="Partial failed output",
            ),
            self.event(
                7,
                "job_error",
                run_id="failed-run",
                purpose="scheduled_job",
                job_id="job-1",
                message="Provider failed",
            ),
        ])

        page = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        failed, stopped = page["runs"]
        self.assertEqual(failed["type"], "job_error")
        self.assertEqual(failed["text"], "Partial failed output")
        self.assertEqual(failed["message"], "Provider failed")
        self.assertEqual(failed["job_run_status"], "failed")
        self.assertTrue(failed["is_error"])
        self.assertEqual(stopped["type"], "turn_stopped")
        self.assertEqual(stopped["text"], "Partial stopped output")
        self.assertEqual(stopped["job_run_status"], "stopped")
        self.assertTrue(stopped["stopped"])

    def test_runner_finished_event_with_stopped_flag_remains_cancelled(self) -> None:
        common = {
            "run_id": "cancelled-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(
                1,
                "job_created",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
            self.event(2, "turn_started", prompt="Poll", **common),
            self.event(3, "assistant_text", text="Stopping the check.", **common),
            self.event(
                4,
                "turn_finished",
                stopped=True,
                exit_code=None,
                result_text='{"status":"PENDING"}',
                **common,
            ),
            self.event(5, "job_ran", **common),
        ])

        history = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        run = history["runs"][0]
        self.assertEqual(run["type"], "turn_finished")
        self.assertEqual(run["result_text"], '{"status":"PENDING"}')
        self.assertEqual(run["job_run_status"], "stopped")
        self.assertEqual(run["job_status"], "stopped")
        self.assertTrue(run["stopped"])

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )
        summary = next(
            event for event in page["events"] if event["type"] == "job_summary"
        )
        self.assertEqual(summary["job_latest_status"], "stopped")
        self.assertEqual(summary["job_latest_status_type"], "turn_finished")
        self.assertEqual(summary["job_status"], "stopped")
        self.assertEqual(summary["job_status_type"], "turn_finished")
        self.assertTrue(summary["stopped"])

    def test_includes_runless_scheduler_deferrals_and_failures(self) -> None:
        self.write_events([
            self.event(
                1,
                "job_created",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
            self.event(
                2,
                "job_deferred",
                job_id="job-1",
                job_title="Capacity monitor",
                message="Busy",
            ),
            self.event(
                3,
                "job_error",
                job_id="job-1",
                job_title="Capacity monitor",
                message="Scheduler failed",
            ),
        ])

        page = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(page["total"], 2)
        self.assertEqual(
            [event["job_run_status"] for event in page["runs"]],
            ["failed", "deferred"],
        )
        self.assertTrue(page["runs"][0]["is_error"])
        self.assertNotIn("run_id", page["runs"][0])

    def test_coalesces_retries_for_one_pending_occurrence(self) -> None:
        occurrence = {
            "id": "job-1",
            "title": "Capacity monitor",
            "scheduled_run_at": 1_775_000_000.0,
        }
        next_occurrence = {
            **occurrence,
            "scheduled_run_at": 1_775_003_600.0,
        }
        completed = {
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        events = [
            self.event(1, "job_created", job_id="job-1", job=occurrence),
            self.event(
                2,
                "job_deferred",
                job_id="job-1",
                job=occurrence,
                message="Busy",
            ),
            self.event(
                3,
                "job_deferred",
                job_id="job-1",
                job=occurrence,
                message="Still busy",
            ),
            self.event(
                4,
                "turn_started",
                run_id="completed-1",
                prompt="Poll",
                **completed,
            ),
            self.event(
                5,
                "turn_finished",
                run_id="completed-1",
                result_text="No changes",
                **completed,
            ),
            self.event(
                6,
                "turn_started",
                run_id="completed-2",
                prompt="Poll",
                **completed,
            ),
            self.event(
                7,
                "turn_finished",
                run_id="completed-2",
                result_text="No changes",
                **completed,
            ),
            self.event(
                8,
                "job_deferred",
                job_id="job-1",
                job=next_occurrence,
                message="Busy again",
            ),
        ]
        self.write_events(events[:2])
        first = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        with agent_server.events_path(self.session_id).open(
            "a",
            encoding="utf-8",
        ) as output:
            output.write(
                "".join(json.dumps(event) + "\n" for event in events[2:])
            )

        page = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(first["total"], 1)
        self.assertEqual(page["total"], 4)
        self.assertEqual(
            [event["job_run_status"] for event in page["runs"]],
            ["deferred", "completed", "completed", "deferred"],
        )
        self.assertEqual(page["runs"][-1]["seq"], 3)
        self.assertEqual(page["runs"][-1]["message"], "Still busy")
        self.assertEqual(
            [
                event.get("run_id")
                for event in page["runs"]
                if event["job_run_status"] == "completed"
            ],
            ["completed-2", "completed-1"],
        )

    def test_eventual_run_replaces_deferred_row_for_same_occurrence(self) -> None:
        scheduled_run_at = 1_775_000_000.0
        occurrence = {
            "id": "job-1",
            "title": "Capacity monitor",
            "scheduled_run_at": scheduled_run_at,
        }
        self.write_events([
            self.event(1, "job_created", job_id="job-1", job=occurrence),
            self.event(
                2,
                "job_deferred",
                job_id="job-1",
                job=occurrence,
                message="Busy",
            ),
        ])
        deferred = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        with agent_server.events_path(self.session_id).open(
            "a",
            encoding="utf-8",
        ) as output:
            output.write("".join(json.dumps(event) + "\n" for event in [
                self.event(
                    3,
                    "turn_started",
                    run_id="eventual-run",
                    purpose="scheduled_job",
                    job_id="job-1",
                    job_title="Capacity monitor",
                    job_scheduled_run_at=scheduled_run_at,
                    prompt="Poll",
                ),
                self.event(
                    4,
                    "turn_finished",
                    run_id="eventual-run",
                    purpose="scheduled_job",
                    job_id="job-1",
                    job_title="Capacity monitor",
                    job_scheduled_run_at=scheduled_run_at,
                    result_text="Finished",
                ),
            ]))

        completed = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(deferred["total"], 1)
        self.assertEqual(deferred["runs"][0]["job_run_status"], "deferred")
        self.assertEqual(completed["total"], 1)
        self.assertEqual(completed["runs"][0]["run_id"], "eventual-run")
        self.assertEqual(completed["runs"][0]["job_run_status"], "completed")
        self.assertEqual(completed["runs"][0]["result_text"], "Finished")

    def test_history_pages_by_latest_event_when_run_finishes_after_defer(self) -> None:
        common = {
            "run_id": "overlapping-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(
                1,
                "job_created",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
            self.event(2, "turn_started", prompt="Poll", **common),
            self.event(
                3,
                "job_deferred",
                job_id="job-1",
                job_title="Capacity monitor",
                message="Previous run is still active",
            ),
            self.event(
                4,
                "turn_finished",
                result_text="Finished after deferral",
                **common,
            ),
        ])

        first = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=1,
        )
        second = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            before_seq=first["next_before"],
            limit=1,
        )

        self.assertEqual(first["total"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["runs"][0]["run_id"], "overlapping-run")
        self.assertEqual(first["runs"][0]["job_status"], "completed")
        self.assertEqual(first["runs"][0]["seq"], 4)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["runs"][0]["job_status"], "deferred")
        self.assertEqual(second["runs"][0]["seq"], 3)
        self.assertIsNone(second["next_before"])

    def test_late_job_ran_marker_does_not_regress_a_completed_run_to_running(self) -> None:
        common = {
            "run_id": "fast-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="Poll", **common),
            self.event(2, "turn_finished", result_text="Fast result", **common),
            self.event(3, "job_ran", message="Scheduled job ran", **common),
        ])

        history = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        semantic = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )
        summary = next(
            event
            for event in semantic["events"]
            if event["type"] == "job_summary"
        )

        self.assertEqual(history["runs"][0]["job_run_status"], "completed")
        self.assertEqual(history["runs"][0]["result_text"], "Fast result")
        self.assertEqual(summary["job_latest_status"], "completed")
        self.assertEqual(summary["result_text"], "Fast result")

    def test_reused_run_id_with_a_new_turn_can_transition_back_to_running(self) -> None:
        common = {
            "run_id": "reused-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="First", **common),
            self.event(2, "turn_finished", result_text="First result", **common),
            self.event(3, "turn_started", prompt="Second", **common),
        ])

        history = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(history["total"], 2)
        self.assertEqual(history["runs"][0]["job_status"], "running")
        self.assertEqual(history["runs"][1]["job_status"], "completed")

    def test_legacy_late_job_link_promotes_only_the_bounded_timeline_preview(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="legacy-run", prompt="Poll"),
            self.event(
                2,
                "turn_finished",
                run_id="legacy-run",
                result_text="Legacy result",
            ),
            self.event(
                3,
                "job_ran",
                run_id="legacy-run",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
        ])

        history = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )

        self.assertEqual(history["total"], 1)
        self.assertEqual(history["runs"][0]["result_text"], "Legacy result")
        self.assertEqual(history["runs"][0]["job_status"], "completed")

    def test_failed_run_preserves_inline_error_output_in_history_and_summary(self) -> None:
        common = {
            "run_id": "failed-inline",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="Poll", **common),
            self.event(
                2,
                "job_error",
                message="Provider failed",
                output="Useful diagnostic output",
                **common,
            ),
        ])

        history = agent_server.read_scheduled_job_runs(
            self.session_id,
            "job-1",
            limit=10,
        )
        semantic = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )
        summary = next(
            event
            for event in semantic["events"]
            if event["type"] == "job_summary"
        )

        self.assertEqual(history["runs"][0]["output"], "Useful diagnostic output")
        self.assertEqual(history["runs"][0]["job_status"], "failed")
        self.assertEqual(summary["output"], "Useful diagnostic output")
        self.assertEqual(summary["job_status"], "failed")

    def test_history_reuses_the_primary_index_without_a_second_file_scan(self) -> None:
        agent_server.build_timeline_index(self.session_id)

        with patch.object(
            Path,
            "open",
            side_effect=AssertionError("history attempted a second JSONL scan"),
        ):
            page = agent_server.read_scheduled_job_runs(
                self.session_id,
                "job-1",
                limit=2,
            )

        self.assertEqual(len(page["runs"]), 2)

    def test_incremental_index_reuses_unchanged_landmarks_and_run_records(self) -> None:
        path = agent_server.events_path(self.session_id)
        existing = path.read_text(encoding="utf-8")
        human = [
            self.event(40, "turn_started", run_id="human-run", prompt="Question"),
            self.event(41, "turn_finished", run_id="human-run", result_text="Answer"),
        ]
        path.write_text(
            existing + "".join(json.dumps(event) + "\n" for event in human),
            encoding="utf-8",
        )
        agent_server.build_timeline_index(self.session_id)
        cached = agent_server.TIMELINE_INDEX_CACHE[self.session_id]
        human_landmark = cached["landmarks_by_key"]["turn:human-run"]
        first_job_run = cached["run_history_records"]["run:run-1"]
        appended = [
            self.event(
                42,
                "turn_started",
                run_id="run-6",
                purpose="scheduled_job",
                job_id="job-1",
                prompt="Poll",
            ),
            self.event(
                43,
                "turn_finished",
                run_id="run-6",
                purpose="scheduled_job",
                job_id="job-1",
                result_text="Result 6",
            ),
        ]
        with path.open("a", encoding="utf-8") as output:
            output.write("".join(json.dumps(event) + "\n" for event in appended))

        agent_server.build_timeline_index(self.session_id)
        refreshed = agent_server.TIMELINE_INDEX_CACHE[self.session_id]

        self.assertIs(
            refreshed["landmarks_by_key"]["turn:human-run"],
            human_landmark,
        )
        self.assertIs(
            refreshed["run_history_records"]["run:run-1"],
            first_job_run,
        )

    def test_trace_is_available_after_cache_reload_and_pages_within_run_bounds(self) -> None:
        self.write_events([
            self.event(
                1,
                "turn_started",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                prompt="Poll",
            ),
            self.event(
                2,
                "reasoning_summary",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                text="First thought",
            ),
            self.event(
                3,
                "tool_started",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                tool_name="check",
            ),
            self.event(
                4,
                "tool_finished",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                tool_name="check",
            ),
            self.event(
                5,
                "reasoning_summary",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                text="Second thought",
            ),
            self.event(
                6,
                "turn_finished",
                run_id="trace-run",
                purpose="scheduled_job",
                job_id="job-1",
                result_text="Done",
            ),
        ])
        agent_server.build_timeline_index(self.session_id)
        agent_server.TIMELINE_INDEX_CACHE.clear()

        first = agent_server.read_indexed_run_trace(
            self.session_id,
            "trace-run",
            anchor_seq=1,
            limit=2,
        )
        second = agent_server.read_indexed_run_trace(
            self.session_id,
            "trace-run",
            anchor_seq=1,
            after_seq=first["next_after"],
            limit=2,
        )

        self.assertEqual(
            [event["type"] for event in first["events"]],
            ["reasoning_summary", "tool_started"],
        )
        self.assertTrue(first["has_more"])
        self.assertEqual(
            [event["type"] for event in second["events"]],
            ["tool_finished", "reasoning_summary"],
        )
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_after"], 6)

    def test_trace_anchor_disambiguates_a_reused_run_id(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="reused", prompt="First"),
            self.event(
                2,
                "reasoning_summary",
                run_id="reused",
                text="First trace",
            ),
            self.event(3, "turn_finished", run_id="reused", result_text="One"),
            self.event(4, "turn_started", run_id="reused", prompt="Second"),
            self.event(
                5,
                "reasoning_summary",
                run_id="reused",
                text="Second trace",
            ),
            self.event(6, "turn_finished", run_id="reused", result_text="Two"),
        ])

        first = agent_server.read_indexed_run_trace(
            self.session_id,
            "reused",
            anchor_seq=1,
        )
        second = agent_server.read_indexed_run_trace(
            self.session_id,
            "reused",
            anchor_seq=4,
        )

        self.assertEqual([event["text"] for event in first["events"]], ["First trace"])
        self.assertEqual([event["text"] for event in second["events"]], ["Second trace"])
        with self.assertRaisesRegex(KeyError, "run trace not found"):
            agent_server.read_indexed_run_trace(
                self.session_id,
                "reused",
                anchor_seq=99,
            )

    def test_cache_eviction_is_safe_during_concurrent_builds_and_history_reads(self) -> None:
        second_session = "job-history-chat-2"
        second_path = agent_server.events_path(second_session)
        second_path.parent.mkdir(parents=True, exist_ok=True)
        second_events = [
            {
                **event,
                "id": f"second-{event['id']}",
                "session_id": second_session,
            }
            for event in self.history_events()
        ]
        second_path.write_text(
            "".join(json.dumps(event) + "\n" for event in second_events),
            encoding="utf-8",
        )
        previous_max = agent_server.TIMELINE_INDEX_CACHE_MAX
        agent_server.TIMELINE_INDEX_CACHE_MAX = 1
        try:
            def read(session_id: str) -> int:
                return agent_server.read_scheduled_job_runs(
                    session_id,
                    "job-1",
                    limit=3,
                )["total"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                totals = list(executor.map(
                    read,
                    [self.session_id, second_session] * 12,
                ))
        finally:
            agent_server.TIMELINE_INDEX_CACHE_MAX = previous_max

        self.assertEqual(totals, [5] * len(totals))
        self.assertLessEqual(len(agent_server.TIMELINE_INDEX_CACHE), 1)
        self.assertEqual(agent_server.TIMELINE_INDEX_LOCKS, {})

    async def test_scoped_endpoint_exposes_additive_history_page(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Job history",
            "backend": "codex",
        }
        with patch.dict(
            agent_server.STORE.sessions,
            {self.session_id: session},
            clear=True,
        ):
            response = await agent_server.get_session_job_runs(
                self.session_id,
                "job-1",
                before_seq=None,
                limit=3,
            )

        self.assertEqual(response["session_id"], self.session_id)
        self.assertEqual(response["job_id"], "job-1")
        self.assertEqual(response["total"], 5)
        self.assertEqual(len(response["runs"]), 3)
        self.assertTrue(response["has_more"])

    async def test_scoped_endpoint_rejects_foreign_or_unknown_jobs(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Job history",
            "backend": "codex",
        }
        with patch.dict(
            agent_server.STORE.sessions,
            {self.session_id: session},
            clear=True,
        ):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.get_session_job_runs(
                    self.session_id,
                    "other-job",
                    before_seq=None,
                    limit=20,
                )

        self.assertEqual(raised.exception.status_code, 404)

    async def test_scoped_run_trace_endpoint_uses_indexed_bounds(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Job history",
            "backend": "codex",
        }
        with patch.dict(
            agent_server.STORE.sessions,
            {self.session_id: session},
            clear=True,
        ):
            response = await agent_server.get_session_run_trace(
                self.session_id,
                "run-5",
                anchor_seq=None,
                after_seq=0,
                limit=20,
            )

        self.assertEqual(response["events"], [])
        self.assertFalse(response["has_more"])
        self.assertGreater(response["next_after"], 0)

    async def test_scoped_run_trace_endpoint_supports_ordinary_turns(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="ordinary", prompt="Question"),
            self.event(
                2,
                "reasoning_summary",
                run_id="ordinary",
                text="Ordinary trace",
            ),
            self.event(3, "turn_finished", run_id="ordinary", result_text="Answer"),
        ])
        session = {
            "id": self.session_id,
            "title": "Ordinary turn",
            "backend": "codex",
        }
        with patch.dict(
            agent_server.STORE.sessions,
            {self.session_id: session},
            clear=True,
        ):
            response = await agent_server.get_session_run_trace(
                self.session_id,
                "ordinary",
                anchor_seq=1,
                after_seq=0,
                limit=20,
            )

        self.assertEqual(
            [event["text"] for event in response["events"]],
            ["Ordinary trace"],
        )
        self.assertFalse(response["has_more"])
        self.assertEqual(response["next_after"], 3)

    async def test_forget_clears_cache_without_growing_a_lock_registry(self) -> None:
        agent_server.build_timeline_index(self.session_id)
        self.assertIn(self.session_id, agent_server.TIMELINE_INDEX_CACHE)

        await agent_server.forget_event_seq(self.session_id)

        self.assertNotIn(self.session_id, agent_server.TIMELINE_INDEX_CACHE)
        self.assertEqual(agent_server.TIMELINE_INDEX_LOCKS, {})

    def history_events(self) -> list[dict[str, object]]:
        events: list[dict[str, object]] = [
            self.event(
                1,
                "job_created",
                job_id="job-1",
                job={"id": "job-1", "title": "Capacity monitor"},
            ),
        ]
        seq = 1
        for run_number in range(1, 6):
            run_id = f"run-{run_number}"
            common = {
                "run_id": run_id,
                "purpose": "scheduled_job",
                "job_id": "job-1",
                "job_title": "Capacity monitor",
            }
            seq += 1
            events.append(self.event(seq, "turn_started", prompt="Poll", **common))
            seq += 1
            events.append(
                self.event(
                    seq,
                    "assistant_text",
                    text=f"Draft {run_number}",
                    **common,
                )
            )
            seq += 1
            events.append(
                self.event(
                    seq,
                    "turn_finished",
                    result_text=f"Result {run_number}",
                    **common,
                )
            )
            if run_number == 3:
                seq += 1
                events.append(self.event(seq, "turn_stopped", **common))
            elif run_number == 4:
                seq += 1
                events.append(
                    self.event(
                        seq,
                        "job_error",
                        message="Run 4 failed",
                        **common,
                    )
                )
            elif run_number == 5:
                seq += 1
                events.append(self.event(seq, "job_finished", **common))
        return events

    def write_events(self, events: list[dict[str, object]]) -> None:
        agent_server.events_path(self.session_id).write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

    def event(
        self,
        seq: int,
        event_type: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "id": f"event-{seq}",
            "session_id": self.session_id,
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-28T00:00:{seq:02d}Z",
            **fields,
        }


if __name__ == "__main__":
    unittest.main()
