import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class CompactTimelinePagingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = agent_server.STATE_DIR
        agent_server.STATE_DIR = Path(self.temporary.name)
        agent_server.FORK_INTERNAL_RUN_CACHE.clear()
        agent_server.FORK_INTERNAL_RUN_LOCKS.clear()
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()
        self.session_id = "compact-history-chat"
        path = agent_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(self.event(seq, event_type)) + "\n" for seq, event_type in enumerate([
                "turn_started",
                "raw_event",
                "reasoning_summary",
                "assistant_text",
                "tool_started",
                "artifact_created",
                "tool_finished",
                "job_started",
                "process_started",
                "error",
                "provider_session",
                "turn_finished",
                "cwd_fallback",
                "file_uploaded",
                "history_imported",
                "handoff_digest_received",
                "backend_changed",
                "turn_stopped",
                "session_created",
                "job_finished",
                "code_diff",
                "queue_snapshot",
            ], start=1)),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        agent_server.FORK_INTERNAL_RUN_CACHE.clear()
        agent_server.FORK_INTERNAL_RUN_LOCKS.clear()
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()
        agent_server.EVENT_SEQ_CACHE.pop(self.session_id, None)
        agent_server.HISTORY_SEARCH_DIRTY.discard(self.session_id)
        agent_server.STATE_DIR = self.previous_state_dir
        self.temporary.cleanup()

    def test_client_safe_job_projection_is_immutable(self) -> None:
        for prompt in (None, 42):
            with self.subTest(prompt=prompt):
                job = {"id": "job-1", "title": "Data Repeater"}
                if prompt is not None:
                    job["prompt"] = prompt
                event = self.event(23, "job_created", job=job)

                projected = agent_server.client_safe_event(event)

                self.assertIsNot(projected, event)
                self.assertIsNot(projected["job"], job)
                self.assertEqual(projected["job"]["prompt"], "")
                if prompt is None:
                    self.assertNotIn("prompt", job)
                else:
                    self.assertEqual(job["prompt"], prompt)

    def test_history_projects_promptless_job_for_older_clients(self) -> None:
        path = agent_server.events_path(self.session_id)
        stored = self.event(1, "job_created", job={"id": "job-1", "title": "Data Repeater"})
        path.write_text(json.dumps(stored) + "\n", encoding="utf-8")

        events = agent_server.read_visible_events_page(self.session_id, limit=100, tail=False)[0]

        self.assertEqual(events[0]["job"]["prompt"], "")
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("prompt", persisted["job"])

    async def test_append_event_broadcasts_compatible_job_without_persisting_prompt(self) -> None:
        broadcast = AsyncMock()
        with patch.object(agent_server.HUB, "broadcast", new=broadcast), patch.object(
            agent_server,
            "ensure_dirs",
        ):
            event = await agent_server.append_event(self.session_id, "job_created", {
                "job": {"id": "job-1", "title": "Data Repeater"},
                "job_id": "job-1",
            })

        live_event = broadcast.await_args.args[1]
        self.assertEqual(live_event["job"]["prompt"], "")
        self.assertNotIn("prompt", event["job"])
        persisted = json.loads(agent_server.events_path(self.session_id).read_text(encoding="utf-8").splitlines()[-1])
        self.assertNotIn("prompt", persisted["job"])

    def test_compact_filter_preserves_conversation_system_job_and_file_events(self) -> None:
        default_page = agent_server.read_visible_events_page(
            self.session_id,
            limit=100,
            tail=False,
        )
        compact_page = agent_server.read_visible_events_page(
            self.session_id,
            limit=100,
            tail=False,
            compact=True,
        )

        default_types = [event["type"] for event in default_page[0]]
        self.assertNotIn("raw_event", default_types)
        self.assertIn("reasoning_summary", default_types)
        self.assertIn("tool_started", default_types)
        self.assertIn("code_diff", default_types)

        compact_types = [event["type"] for event in compact_page[0]]
        self.assertEqual(compact_types, [
            "turn_started",
            "assistant_text",
            "artifact_created",
            "job_started",
            "error",
            "turn_finished",
            "file_uploaded",
            "handoff_digest_received",
            "turn_stopped",
            "job_finished",
            "queue_snapshot",
        ])
        self.assertEqual(compact_page[1:], (22, 11, 0, 0))

    def test_compact_before_and_after_pages_count_only_compact_events(self) -> None:
        before_page = agent_server.read_visible_events_page(
            self.session_id,
            before=18,
            limit=3,
            tail=True,
            compact=True,
        )
        self.assertEqual([event["seq"] for event in before_page[0]], [12, 14, 16])
        self.assertEqual(before_page[1:], (22, 8, 5, 0))

        after_page = agent_server.read_visible_events_after_page(
            self.session_id,
            after=10,
            limit=3,
            compact=True,
        )
        self.assertEqual([event["seq"] for event in after_page[0]], [12, 14, 16])
        self.assertEqual(after_page[1:], (22, 6, 0, 3))

    def test_legacy_fork_digest_runs_do_not_count_as_visible_page_events(self) -> None:
        path = agent_server.events_path(self.session_id)
        events = [
            self.event(1, "turn_started", run_id="digest-run", purpose="handoff_digest", forked=True, prompt="Generate digest"),
            self.event(2, "reasoning_summary", run_id="digest-run", forked=True, text="Private digest reasoning"),
            self.event(3, "assistant_text", run_id="digest-run", forked=True, text="Private digest body"),
            self.event(4, "turn_started", run_id="normal-run", forked=True, prompt="Retained question"),
            self.event(5, "assistant_text", run_id="normal-run", forked=True, text="Retained answer"),
        ]
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        agent_server.FORK_INTERNAL_RUN_CACHE.clear()

        page = agent_server.read_visible_events_page(self.session_id, limit=100, tail=False)
        after_page = agent_server.read_visible_events_after_page(self.session_id, after=0, limit=100)
        generic = agent_server.read_events(self.session_id, limit=100, visible=True)

        self.assertEqual([event["seq"] for event in page[0]], [4, 5])
        self.assertEqual(page[1:], (5, 2, 0, 0))
        self.assertEqual([event["seq"] for event in after_page[0]], [4, 5])
        self.assertEqual(after_page[1:], (5, 2, 0, 0))
        self.assertEqual([event["seq"] for event in generic], [4, 5])

    def test_semantic_page_counts_a_recurring_job_once_and_bounds_its_history(self) -> None:
        events = [
            self.event(1, "turn_started", run_id="human-old", prompt="Old question"),
            self.event(2, "turn_finished", run_id="human-old", result_text="Old answer"),
            self.event(3, "job_created", job_id="job-1", job={
                "id": "job-1", "title": "Capacity monitor", "run_count": 0,
            }),
        ]
        seq = 3
        expected_job_events = 1
        for run_number in range(1, 13):
            run_id = f"job-run-{run_number}"
            common = {
                "run_id": run_id,
                "purpose": "scheduled_job",
                "job_id": "job-1",
                "job_title": "Capacity monitor",
            }
            seq += 1
            events.append(self.event(seq, "turn_started", prompt="Check capacity", **common))
            seq += 1
            events.append(self.event(seq, "reasoning_summary", text="Checking capacity", **common))
            seq += 1
            events.append(self.event(seq, "assistant_text", text=f"Status {run_number}", **common))
            if run_number == 12:
                seq += 1
                events.append(self.event(seq, "artifact_created", artifact={
                    "id": "latest-report", "filename": "latest.txt",
                }, **common))
                seq += 1
                events.append(self.event(seq, "code_diff", diff_files=[{
                    "path": "status.py", "additions": 1, "deletions": 0,
                }], **common))
                expected_job_events += 2
            seq += 1
            events.append(self.event(seq, "turn_finished", result_text=f"Status {run_number}", **common))
            expected_job_events += 4
        seq += 1
        events.append(self.event(seq, "turn_started", run_id="human-new", prompt="Latest question"))
        seq += 1
        events.append(self.event(seq, "turn_finished", run_id="human-new", result_text="Latest answer"))
        self.write_events(events)

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=2,
            tail=True,
        )

        self.assertEqual(page["semantic_total"], 3)
        self.assertEqual(page["semantic_item_count"], 2)
        self.assertEqual(page["semantic_omitted_before"], 1)
        summaries = [event for event in page["events"] if event["type"] == "job_summary"]
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["job_id"], "job-1")
        self.assertEqual(summary["job_run_count"], 12)
        self.assertEqual(summary["job_event_count"], expected_job_events)
        self.assertTrue(summary["job_history_truncated"])
        self.assertEqual(page["next_semantic_before"], summary["seq"])
        self.assertNotEqual(page["next_semantic_before"], min(event["seq"] for event in page["events"]))

        representative_runs = {
            event["run_id"]
            for event in page["events"]
            if event["type"] == "turn_finished" and str(event.get("run_id") or "").startswith("job-run-")
        }
        self.assertEqual(representative_runs, {f"job-run-{number}" for number in range(6, 13)})
        self.assertIn("artifact_created", [event["type"] for event in page["events"]])
        self.assertIn("code_diff", [event["type"] for event in page["events"]])
        self.assertIn("human-new", [event.get("run_id") for event in page["events"]])
        self.assertNotIn("human-old", [event.get("run_id") for event in page["events"]])

        older = agent_server.read_semantic_timeline_page(
            self.session_id,
            semantic_before=page["next_semantic_before"],
            limit=2,
            tail=True,
        )
        self.assertEqual(older["semantic_item_count"], 1)
        self.assertEqual(older["semantic_omitted_before"], 0)
        self.assertEqual({event.get("run_id") for event in older["events"]}, {"human-old"})
        self.assertFalse(any(event["type"] == "job_summary" for event in older["events"]))

    def test_semantic_page_preserves_all_turn_artifacts_before_optional_trace_details(self) -> None:
        run_id = "artifact-heavy-run"
        events = [
            self.event(1, "turn_started", run_id=run_id, prompt="Render every example"),
        ]
        for seq in range(2, 7):
            events.append(self.event(
                seq,
                "artifact_created",
                run_id=run_id,
                artifact={
                    "id": f"video-{seq}",
                    "filename": f"example-{seq}.mp4",
                    "content_type": "video/mp4",
                },
            ))
        events.extend([
            self.event(7, "file_uploaded", run_id=run_id, file={
                "id": "uploaded-source",
                "filename": "source.json",
                "content_type": "application/json",
            }),
            self.event(8, "code_diff", run_id=run_id, diff_files=[{
                "path": "render.py",
                "additions": 2,
                "deletions": 1,
            }]),
            self.event(9, "tool_started", run_id=run_id),
            self.event(10, "reasoning_summary", run_id=run_id, text="Finalizing media"),
            self.event(11, "assistant_text", run_id=run_id, text="All videos are ready."),
            self.event(12, "turn_finished", run_id=run_id, result_text="All videos are ready."),
        ])
        self.write_events(events)

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )

        artifact_events = [
            event for event in page["events"]
            if event["type"] == "artifact_created"
        ]
        self.assertEqual(
            [(event["artifact"]["id"], event["seq"]) for event in artifact_events],
            [(f"video-{seq}", seq) for seq in range(2, 7)],
        )
        self.assertIn("file_uploaded", [event["type"] for event in page["events"]])
        self.assertIn("code_diff", [event["type"] for event in page["events"]])
        self.assertIn("turn_started", [event["type"] for event in page["events"]])
        self.assertIn("turn_finished", [event["type"] for event in page["events"]])
        self.assertGreater(
            len(page["events"]),
            agent_server.SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM,
        )
        self.assertNotIn("tool_started", [event["type"] for event in page["events"]])
        self.assertNotIn("reasoning_summary", [event["type"] for event in page["events"]])

    def test_semantic_media_overflow_is_bounded_but_preserves_thirteen_videos(self) -> None:
        for artifact_count in (13, 80):
            with self.subTest(artifact_count=artifact_count):
                run_id = f"media-run-{artifact_count}"
                events = [
                    self.event(
                        1,
                        "turn_started",
                        run_id=run_id,
                        prompt="Render examples",
                    ),
                ]
                for seq in range(2, artifact_count + 2):
                    events.append(self.event(
                        seq,
                        "artifact_created",
                        run_id=run_id,
                        artifact={
                            "id": f"video-{seq}",
                            "filename": f"example-{seq}.mp4",
                            "content_type": "video/mp4",
                        },
                    ))
                events.append(self.event(
                    artifact_count + 2,
                    "turn_finished",
                    run_id=run_id,
                    result_text="Done",
                ))
                self.write_events(events)

                page = agent_server.read_semantic_timeline_page(
                    self.session_id,
                    limit=1,
                    tail=True,
                )
                artifacts = [
                    event
                    for event in page["events"]
                    if event["type"] == "artifact_created"
                ]

                self.assertEqual(
                    len(artifacts),
                    min(
                        artifact_count,
                        agent_server.SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM,
                    ),
                )
                if artifact_count == 13:
                    self.assertEqual(
                        {event["artifact"]["id"] for event in artifacts},
                        {f"video-{seq}" for seq in range(2, 15)},
                    )
                self.assertLessEqual(
                    len(page["events"]),
                    agent_server.SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM
                    + agent_server.SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM,
                )

    def test_semantic_media_page_cap_is_fair_across_multiple_turns(self) -> None:
        events: list[dict[str, object]] = []
        seq = 0
        run_ids = [f"media-run-{index}" for index in range(5)]
        for run_id in run_ids:
            seq += 1
            events.append(self.event(
                seq,
                "turn_started",
                run_id=run_id,
                prompt="Render examples",
            ))
            for artifact_index in range(40):
                seq += 1
                events.append(self.event(
                    seq,
                    "artifact_created",
                    run_id=run_id,
                    artifact={
                        "id": f"{run_id}-video-{artifact_index}",
                        "filename": f"{artifact_index}.mp4",
                        "content_type": "video/mp4",
                    },
                ))
            seq += 1
            events.append(self.event(
                seq,
                "turn_finished",
                run_id=run_id,
                result_text="Done",
            ))
        self.write_events(events)

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=5,
            tail=True,
        )
        counts = {
            run_id: sum(
                event["type"] == "artifact_created"
                and event.get("run_id") == run_id
                for event in page["events"]
            )
            for run_id in run_ids
        }
        base_budget = (
            len(run_ids)
            * agent_server.SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM
        )

        self.assertTrue(all(count > 0 for count in counts.values()))
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertTrue(all(
            count <= agent_server.SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM
            for count in counts.values()
        ))
        self.assertLessEqual(
            len(page["events"]),
            base_budget
            + agent_server.SEMANTIC_TIMELINE_ESSENTIAL_PAGE_OVERFLOW_LIMIT,
        )

    def test_semantic_cursor_keeps_each_job_global_and_does_not_repeat_old_runs(self) -> None:
        events: list[dict[str, object]] = []
        seq = 0
        for job_id, run_id in (
            ("job-1", "job-1-run-1"),
            ("job-2", "job-2-run-1"),
            ("job-1", "job-1-run-2"),
        ):
            common = {
                "run_id": run_id,
                "purpose": "scheduled_job",
                "job_id": job_id,
                "job_title": job_id,
            }
            seq += 1
            events.append(self.event(seq, "turn_started", prompt="Poll", **common))
            seq += 1
            events.append(self.event(seq, "turn_finished", result_text=run_id, **common))
        self.write_events(events)

        latest = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        latest_summary = next(event for event in latest["events"] if event["type"] == "job_summary")
        self.assertEqual(latest["semantic_total"], 2)
        self.assertEqual(latest_summary["job_id"], "job-1")
        self.assertEqual(latest_summary["job_run_count"], 2)

        older = agent_server.read_semantic_timeline_page(
            self.session_id,
            semantic_before=latest["next_semantic_before"],
            limit=1,
            tail=True,
        )
        older_summary = next(event for event in older["events"] if event["type"] == "job_summary")
        self.assertEqual(older_summary["job_id"], "job-2")
        self.assertFalse(any(event.get("job_id") == "job-1" for event in older["events"]))

    def test_semantic_job_summary_uses_a_newer_runless_error_as_latest_status(self) -> None:
        common = {
            "run_id": "job-run-1",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="Check capacity", **common),
            self.event(2, "turn_finished", result_text="All good", **common),
            self.event(3, "job_error", job_id="job-1", message="Capacity monitor failed"),
        ])

        page = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        summary = next(event for event in page["events"] if event["type"] == "job_summary")

        self.assertEqual(page["semantic_total"], 1)
        self.assertEqual(summary["message"], "Capacity monitor failed")
        self.assertEqual(summary["error"], "Capacity monitor failed")
        self.assertTrue(summary["is_error"])
        self.assertNotIn("result_text", summary)
        self.assertIn("job_error", [event["type"] for event in page["events"]])

    def test_semantic_job_summary_preserves_partial_output_and_explicit_status(self) -> None:
        for terminal_type, expected_status in (
            ("turn_stopped", "stopped"),
            ("job_error", "failed"),
        ):
            with self.subTest(terminal_type=terminal_type):
                common = {
                    "run_id": f"{expected_status}-run",
                    "purpose": "scheduled_job",
                    "job_id": "job-1",
                    "job_title": "Capacity monitor",
                }
                terminal_fields = (
                    {"message": "Provider failed"}
                    if terminal_type == "job_error"
                    else {}
                )
                self.write_events([
                    self.event(1, "turn_started", prompt="Check", **common),
                    self.event(
                        2,
                        "assistant_text",
                        text="Useful partial output",
                        **common,
                    ),
                    self.event(3, terminal_type, **terminal_fields, **common),
                ])

                page = agent_server.read_semantic_timeline_page(
                    self.session_id,
                    limit=1,
                    tail=True,
                )
                summary = next(
                    event
                    for event in page["events"]
                    if event["type"] == "job_summary"
                )

                self.assertEqual(summary["text"], "Useful partial output")
                self.assertEqual(summary["job_latest_run_id"], common["run_id"])
                self.assertEqual(summary["job_latest_status_run_id"], common["run_id"])
                self.assertEqual(summary["job_latest_status"], expected_status)
                self.assertEqual(summary["job_latest_status_type"], terminal_type)
                self.assertEqual(summary["job_latest_status_seq"], 3)
                self.assertEqual(summary["job_status"], expected_status)
                self.assertEqual(summary["job_status_run_id"], common["run_id"])
                self.assertEqual(summary["job_status_type"], terminal_type)
                self.assertEqual(summary["job_status_seq"], 3)
                if expected_status == "stopped":
                    self.assertTrue(summary["stopped"])
                else:
                    self.assertTrue(summary["is_error"])
                    self.assertEqual(summary["message"], "Provider failed")

    def test_semantic_job_summary_uses_partial_output_from_the_latest_run(self) -> None:
        first = {
            "run_id": "completed-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        latest = {
            "run_id": "stopped-run",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="Check", **first),
            self.event(2, "turn_finished", result_text="Old completed output", **first),
            self.event(3, "turn_started", prompt="Check", **latest),
            self.event(4, "assistant_text", text="Latest partial output", **latest),
            self.event(5, "turn_stopped", **latest),
        ])

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )
        summary = next(
            event for event in page["events"] if event["type"] == "job_summary"
        )

        self.assertEqual(summary["text"], "Latest partial output")
        self.assertNotIn("result_text", summary)
        self.assertEqual(summary["job_latest_run_id"], "stopped-run")
        self.assertEqual(summary["job_latest_status"], "stopped")

    def test_semantic_job_summary_uses_a_newer_runless_defer_as_latest_status(self) -> None:
        common = {
            "run_id": "job-run-1",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        self.write_events([
            self.event(1, "turn_started", prompt="Check capacity", **common),
            self.event(2, "turn_finished", result_text="All good", **common),
            self.event(3, "job_deferred", job_id="job-1", message="Capacity monitor deferred"),
        ])

        page = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        summary = next(event for event in page["events"] if event["type"] == "job_summary")

        self.assertEqual(page["semantic_total"], 1)
        self.assertEqual(summary["message"], "Capacity monitor deferred")
        self.assertNotIn("result_text", summary)
        self.assertIn("job_deferred", [event["type"] for event in page["events"]])

    def test_semantic_job_summary_retains_latest_result_after_marker_only_stopped_run(self) -> None:
        first_run = {
            "run_id": "job-run-170",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        latest_run = {
            "run_id": "job-run-171",
            "purpose": "scheduled_job",
            "job_id": "job-1",
            "job_title": "Capacity monitor",
        }
        events = [
            self.event(1, "turn_started", prompt="Check capacity", **first_run),
            self.event(
                2,
                "turn_finished",
                result_text="Latest completed capacity result",
                **first_run,
            ),
        ]
        # Reproduce a busy recurring job whose bounded optional history is
        # dominated by runless deferrals before the next run starts.
        for seq in range(3, 70):
            events.append(self.event(
                seq,
                "job_deferred",
                job_id="job-1",
                job_title="Capacity monitor",
                message="Capacity monitor deferred",
            ))
        events.extend([
            self.event(70, "turn_started", prompt="Check capacity", **latest_run),
            self.event(
                71,
                "job_ran",
                message="Scheduled job ran: Capacity monitor",
                **latest_run,
            ),
            self.event(72, "turn_stopped", **latest_run),
            self.event(73, "turn_finished", result_text="", **latest_run),
            self.event(
                74,
                "job_finished",
                job_id="job-1",
                job_title="Capacity monitor",
            ),
        ])
        self.write_events(events)

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=1,
            tail=True,
        )
        summary = next(event for event in page["events"] if event["type"] == "job_summary")

        self.assertEqual(summary["job_run_count"], 2)
        self.assertEqual(summary["result_text"], "Latest completed capacity result")
        self.assertNotEqual(summary.get("message"), "Scheduled job ran: Capacity monitor")

    def test_semantic_page_retroactively_folds_a_legacy_late_job_link(self) -> None:
        initial_turn = [
            self.event(1, "turn_started", run_id="legacy-job-run", prompt="Legacy poll"),
            self.event(2, "reasoning_summary", run_id="legacy-job-run", text="Checking"),
            self.event(3, "assistant_text", run_id="legacy-job-run", text="Healthy"),
            self.event(4, "turn_finished", run_id="legacy-job-run", result_text="Healthy"),
        ]
        self.write_events(initial_turn)
        before_link = agent_server.build_timeline_index(self.session_id)
        self.assertEqual(len(before_link["landmarks"]), 1)

        late_link = self.event(5, "job_ran", run_id="legacy-job-run", job_id="job-legacy", job={
                "id": "job-legacy", "title": "Legacy monitor", "run_count": 1,
            })
        with agent_server.events_path(self.session_id).open("a", encoding="utf-8") as destination:
            destination.write(json.dumps(late_link) + "\n")

        page = agent_server.read_semantic_timeline_page(self.session_id, limit=10, tail=True)

        self.assertEqual(page["semantic_total"], 1)
        self.assertEqual(page["semantic_item_count"], 1)
        summary = next(event for event in page["events"] if event["type"] == "job_summary")
        self.assertEqual(summary["job_id"], "job-legacy")
        self.assertEqual(summary["job_run_count"], 1)
        self.assertEqual(summary["job_event_count"], 5)
        self.assertEqual(summary["job_start_seq"], 1)
        self.assertEqual(summary["job_end_seq"], 5)
        returned_run_events = [
            event
            for event in page["events"]
            if event.get("run_id") == "legacy-job-run"
        ]
        self.assertTrue(returned_run_events)
        self.assertTrue(all(
            event.get("job_id") == "job-legacy"
            and event.get("purpose") == "scheduled_job"
            for event in returned_run_events
        ))
        self.assertFalse(any(
            event["type"] == "turn_started" and event.get("prompt") == "Legacy poll"
            for event in page["events"]
        ))

    def test_semantic_cursor_uses_the_same_start_anchor_as_rendered_item_order(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="long-run", prompt="Long turn"),
            self.event(2, "error", message="Intervening error"),
            self.event(3, "assistant_text", run_id="long-run", text="Recovered"),
            self.event(4, "turn_finished", run_id="long-run", result_text="Recovered"),
            self.event(5, "turn_started", run_id="latest-run", prompt="Latest"),
            self.event(6, "turn_finished", run_id="latest-run", result_text="Done"),
        ])

        latest = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        self.assertEqual(latest["next_semantic_before"], 5)
        middle = agent_server.read_semantic_timeline_page(
            self.session_id,
            semantic_before=latest["next_semantic_before"],
            limit=1,
            tail=True,
        )
        self.assertEqual(middle["next_semantic_before"], 2)
        self.assertEqual([event["type"] for event in middle["events"]], ["error"])
        oldest = agent_server.read_semantic_timeline_page(
            self.session_id,
            semantic_before=middle["next_semantic_before"],
            limit=1,
            tail=True,
        )
        self.assertEqual({event.get("run_id") for event in oldest["events"]}, {"long-run"})
        self.assertIsNone(oldest["next_semantic_before"])

    def test_semantic_response_is_bounded_and_keeps_every_selected_item_represented(self) -> None:
        events = [
            self.event(1, "turn_started", run_id="large-run", prompt="Large turn"),
        ]
        final_large_seq = agent_server.MAX_EVENT_RESPONSE_LIMIT + 152
        for seq in range(2, final_large_seq):
            events.append(self.event(
                seq,
                "reasoning_summary",
                run_id="large-run",
                text=f"Trace {seq}",
            ))
        events.extend([
            self.event(final_large_seq, "turn_finished", run_id="large-run", result_text="Large done"),
            self.event(final_large_seq + 1, "turn_started", run_id="latest-run", prompt="Latest turn"),
            self.event(final_large_seq + 2, "turn_finished", run_id="latest-run", result_text="Latest done"),
        ])
        self.write_events(events)

        page = agent_server.read_semantic_timeline_page(self.session_id, limit=2, tail=True)

        self.assertEqual(page["semantic_item_count"], 2)
        self.assertLessEqual(len(page["events"]), agent_server.MAX_EVENT_RESPONSE_LIMIT)
        self.assertLessEqual(
            len(page["events"]),
            page["semantic_item_count"]
            * agent_server.SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM,
        )
        represented_runs = {
            str(event.get("run_id") or "")
            for event in page["events"]
        }
        self.assertIn("large-run", represented_runs)
        self.assertIn("latest-run", represented_runs)
        self.assertIn(1, [event["seq"] for event in page["events"]])
        self.assertIn(final_large_seq, [event["seq"] for event in page["events"]])

    def test_semantic_scan_projects_only_events_in_selected_units(self) -> None:
        events = []
        for index in range(100):
            seq = index * 2 + 1
            events.extend([
                self.event(seq, "turn_started", run_id=f"run-{index}", prompt=f"Question {index}"),
                self.event(seq + 1, "turn_finished", run_id=f"run-{index}", result_text=f"Answer {index}"),
            ])
        self.write_events(events)

        original_projection = agent_server.client_safe_event
        with patch.object(
            agent_server,
            "client_safe_event",
            wraps=original_projection,
        ) as projection:
            page = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)

        self.assertEqual(page["semantic_item_count"], 1)
        self.assertEqual(projection.call_count, 2)

    def test_timeline_index_does_not_retain_duplicate_search_payloads(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="run-1", prompt="Question"),
            self.event(2, "reasoning_summary", run_id="run-1", text="Private trace"),
            self.event(3, "turn_finished", run_id="run-1", result_text="Answer"),
        ])

        agent_server.build_timeline_index(self.session_id)
        records = agent_server.TIMELINE_INDEX_CACHE[self.session_id]["records"]

        self.assertTrue(records)
        self.assertTrue(all("search_entries" not in record for record in records))
        self.assertTrue(all("_search_values" not in record for record in records))

    def test_semantic_job_summary_id_stays_stable_across_incremental_refresh(self) -> None:
        first_run = [
            self.event(
                1,
                "turn_started",
                run_id="job-run-1",
                purpose="scheduled_job",
                job_id="job-1",
                job_title="Capacity monitor",
                prompt="Check capacity",
            ),
            self.event(
                2,
                "turn_finished",
                run_id="job-run-1",
                purpose="scheduled_job",
                job_id="job-1",
                job_title="Capacity monitor",
                result_text="First result",
            ),
        ]
        self.write_events(first_run)
        first = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        first_summary = next(event for event in first["events"] if event["type"] == "job_summary")

        path = agent_server.events_path(self.session_id)
        second_run = [
            self.event(
                3,
                "turn_started",
                run_id="job-run-2",
                purpose="scheduled_job",
                job_id="job-1",
                job_title="Capacity monitor",
                prompt="Check capacity",
            ),
            self.event(
                4,
                "turn_finished",
                run_id="job-run-2",
                purpose="scheduled_job",
                job_id="job-1",
                job_title="Capacity monitor",
                result_text="Second result",
            ),
        ]
        with path.open("a", encoding="utf-8") as destination:
            destination.write("".join(json.dumps(event) + "\n" for event in second_run))

        refreshed = agent_server.read_semantic_timeline_page(self.session_id, limit=1, tail=True)
        summaries = [event for event in refreshed["events"] if event["type"] == "job_summary"]

        self.assertEqual(len(summaries), 1)
        self.assertEqual(first_summary["id"], "job_summary:job-1")
        self.assertEqual(summaries[0]["id"], first_summary["id"])
        self.assertGreater(summaries[0]["seq"], first_summary["seq"])
        self.assertEqual(summaries[0]["job_run_count"], 2)
        self.assertEqual(summaries[0]["job_event_count"], 4)

    def test_semantic_paging_ignores_renderer_hidden_lifecycle_events(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="human-run", prompt="Question"),
            self.event(2, "turn_finished", run_id="human-run", result_text="Answer"),
            self.event(3, "job_started", run_id="job-run", job_id="job-1"),
            self.event(4, "job_finished", run_id="job-run", job_id="job-1"),
            self.event(5, "turn_queue_updated"),
            self.event(6, "turn_queue_reordered"),
            self.event(7, "turn_queue_run_now"),
            self.event(8, "subagent_state"),
            self.event(9, "job_updated", job_id="job-1"),
            self.event(10, "job_deleted", job_id="job-1"),
        ])

        page = agent_server.read_semantic_timeline_page(self.session_id, limit=10, tail=True)
        summary = next(event for event in page["events"] if event["type"] == "job_summary")

        self.assertEqual(page["semantic_total"], 2)
        self.assertEqual(page["semantic_item_count"], 2)
        self.assertEqual(summary["seq"], 4)
        self.assertEqual(summary["job_event_count"], 2)
        self.assertFalse({
            "turn_queue_updated",
            "turn_queue_reordered",
            "turn_queue_run_now",
            "subagent_state",
            "job_updated",
            "job_deleted",
        }.intersection(event["type"] for event in page["events"]))

    def test_semantic_paging_projects_codex_lifecycle_without_polluting_pages(
        self,
    ) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="human-run", prompt="Question"),
            self.event(2, "turn_finished", run_id="human-run", result_text="Answer"),
            self.event(
                3,
                "codex_thread_status",
                status={"type": "active"},
                message="Codex is working.",
            ),
            self.event(
                4,
                "codex_goal_updated",
                goal={"status": "inProgress"},
            ),
            self.event(5, "codex_goal_cleared"),
            self.event(
                6,
                "codex_goal_budget_limited",
                message="The persistent goal reached its time limit.",
            ),
            self.event(
                7,
                "codex_compaction_started",
                operation_id="compact-explicit",
                message="Codex started compacting this thread's context.",
            ),
            self.event(
                8,
                "codex_compaction_completed",
                operation_id="compact-explicit",
                status="completed",
                message="Context compaction completed.",
            ),
            self.event(
                9,
                "turn_stopped",
                run_id="superseded-run",
                native_steer=True,
                superseded_by_run_id="steered-run",
            ),
            self.event(
                10,
                "codex_compaction_completed",
                turn_id="automatic-turn",
                item_id="automatic-item",
                status="completed",
                message="Codex completed automatic context compaction.",
            ),
            self.event(
                11,
                "codex_thread_status",
                status={"type": "idle"},
                message="Codex is idle.",
            ),
        ])

        index = agent_server.build_timeline_index(self.session_id)
        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=10,
            tail=True,
        )
        raw_page = agent_server.read_visible_events_page(
            self.session_id,
            limit=100,
            tail=False,
        )[0]

        self.assertEqual(
            [landmark["key"] for landmark in index["landmarks"]],
            [
                "turn:human-run",
                "codex:goal-budget",
                "codex:compaction:compact-explicit",
                "codex:compaction:automatic-turn",
            ],
        )
        self.assertEqual(page["semantic_total"], 4)
        self.assertEqual(page["semantic_item_count"], 4)
        lifecycle_events = [
            event
            for event in page["events"]
            if event["type"].startswith("codex_")
        ]
        self.assertEqual(
            [(event["seq"], event["type"]) for event in lifecycle_events],
            [
                (6, "codex_goal_budget_limited"),
                (8, "codex_compaction_completed"),
                (10, "codex_compaction_completed"),
            ],
        )
        self.assertFalse({
            "codex_thread_status",
            "codex_goal_updated",
            "codex_goal_cleared",
            "codex_compaction_started",
            "turn_stopped",
        }.intersection(event["type"] for event in page["events"]))
        # Semantic projection is additive: durable/raw history remains
        # backward-compatible for clients that do not request semantic pages.
        self.assertTrue({
            "codex_thread_status",
            "codex_goal_updated",
            "codex_goal_cleared",
            "codex_compaction_started",
            "turn_stopped",
        }.issubset(event["type"] for event in raw_page))

    def test_codex_lifecycle_landmarks_update_incrementally(self) -> None:
        self.write_events([
            self.event(
                1,
                "codex_goal_updated",
                goal={"status": "inProgress"},
            ),
            self.event(
                2,
                "codex_compaction_started",
                operation_id="compact-incremental",
                message="Compaction started.",
            ),
        ])
        first = agent_server.build_timeline_index(self.session_id)

        with agent_server.events_path(self.session_id).open(
            "a",
            encoding="utf-8",
        ) as destination:
            destination.write(json.dumps(self.event(
                3,
                "codex_goal_cleared",
            )) + "\n")
            destination.write(json.dumps(self.event(
                4,
                "codex_goal_budget_limited",
                message="The persistent goal reached its time limit.",
            )) + "\n")
            destination.write(json.dumps(self.event(
                5,
                "codex_compaction_completed",
                operation_id="compact-incremental",
                status="completed",
                message="Compaction completed.",
            )) + "\n")

        refreshed = agent_server.build_timeline_index(self.session_id)
        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=10,
            tail=True,
        )

        self.assertEqual(
            [(item["key"], item["start_seq"]) for item in first["landmarks"]],
            [],
        )
        self.assertEqual(
            [
                (item["key"], item["start_seq"])
                for item in refreshed["landmarks"]
            ],
            [
                ("codex:goal-budget", 4),
                ("codex:compaction:compact-incremental", 5),
            ],
        )
        self.assertEqual(page["semantic_total"], 2)
        self.assertEqual(
            [(event["seq"], event["type"]) for event in page["events"]],
            [
                (4, "codex_goal_budget_limited"),
                (5, "codex_compaction_completed"),
            ],
        )

    def test_native_steer_retires_incremental_turn_routing(self) -> None:
        self.write_events([
            self.event(
                1,
                "turn_started",
                run_id="run-0",
                prompt="Initial request",
            ),
        ])
        agent_server.build_timeline_index(self.session_id)

        path = agent_server.events_path(self.session_id)
        for index in range(1, 11):
            previous_run = f"run-{index - 1}"
            current_run = f"run-{index}"
            with path.open("a", encoding="utf-8") as destination:
                destination.write(json.dumps(self.event(
                    index * 2,
                    "turn_stopped",
                    run_id=previous_run,
                    native_steer=True,
                    superseded_by_run_id=current_run,
                )) + "\n")
                destination.write(json.dumps(self.event(
                    index * 2 + 1,
                    "turn_started",
                    run_id=current_run,
                    prompt=f"Steer {index}",
                )) + "\n")

            index_payload = agent_server.build_timeline_index(self.session_id)
            cache = agent_server.TIMELINE_INDEX_CACHE[self.session_id]
            self.assertEqual(
                cache["current_turn_by_run"],
                {current_run: f"turn:{current_run}"},
            )
            self.assertEqual(cache["active_turn_key"], f"turn:{current_run}")
            self.assertEqual(len(index_payload["landmarks"]), index + 1)

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=20,
            tail=True,
        )
        self.assertFalse(
            any(event["type"] == "turn_stopped" for event in page["events"])
        )

    def test_native_steer_preserves_completed_commentary_in_semantic_page(self) -> None:
        self.write_events([
            self.event(1, "turn_started", run_id="run-a", prompt="Initial request"),
            self.event(2, "reasoning_summary", run_id="run-a", text="Private reasoning"),
            self.event(
                3,
                "reasoning_summary",
                run_id="run-a",
                item_id="commentary-1",
                phase="commentary",
                text="First completed commentary.",
            ),
            self.event(4, "tool_started", run_id="run-a", tool={"name": "exec"}),
            self.event(
                5,
                "reasoning_summary",
                run_id="run-a",
                item_id="commentary-2",
                phase="commentary",
                text="Second completed commentary.",
            ),
            self.event(6, "tool_finished", run_id="run-a", tool={"name": "exec"}),
            self.event(
                7,
                "reasoning_summary",
                run_id="run-a",
                item_id="commentary-3",
                phase="commentary",
                text="Third completed commentary.",
            ),
            self.event(
                8,
                "reasoning_summary",
                run_id="run-a",
                item_id="commentary-4",
                phase="commentary",
                text="Fourth completed commentary.",
            ),
            self.event(
                9,
                "turn_stopped",
                run_id="run-a",
                native_steer=True,
                superseded_by_run_id="run-b",
            ),
            self.event(
                10,
                "turn_started",
                run_id="run-b",
                native_steer=True,
                steer_interrupted_run_id="run-a",
                prompt="Steered request",
            ),
        ])

        page = agent_server.read_semantic_timeline_page(
            self.session_id,
            limit=2,
            tail=True,
        )

        commentary = [
            event
            for event in page["events"]
            if event["type"] == "reasoning_summary"
            and event.get("phase") == "commentary"
        ]
        self.assertEqual(
            [event["item_id"] for event in commentary],
            ["commentary-1", "commentary-2", "commentary-3", "commentary-4"],
        )
        self.assertFalse(
            any(event["type"] == "turn_stopped" for event in page["events"])
        )
        self.assertEqual(
            next(
                event
                for event in page["events"]
                if event["type"] == "turn_started"
                and event.get("run_id") == "run-b"
            )["steer_interrupted_run_id"],
            "run-a",
        )

    async def test_endpoint_keeps_default_payload_and_offloads_visible_scans(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Compact history",
            "backend": "codex",
            "created_at": "2026-07-19T00:00:00Z",
            "updated_at": "2026-07-19T00:00:00Z",
        }
        original_to_thread = asyncio.to_thread
        offload = AsyncMock(side_effect=original_to_thread)
        with patch.dict(agent_server.STORE.sessions, {self.session_id: session}, clear=True), patch.object(
            agent_server.asyncio,
            "to_thread",
            new=offload,
        ):
            default_response = await agent_server.get_session(
                self.session_id,
                limit=100,
                tail=False,
            )
            visible_response = await agent_server.get_session(
                self.session_id,
                limit=100,
                tail=False,
                visible=True,
            )
            compact_response = await agent_server.get_session(
                self.session_id,
                after=10,
                limit=3,
                tail=False,
                compact=True,
            )

        self.assertIn("raw_event", [event["type"] for event in default_response["events"]])
        self.assertIn("reasoning_summary", [event["type"] for event in visible_response["events"]])
        self.assertNotIn("raw_event", [event["type"] for event in visible_response["events"]])
        self.assertEqual([event["seq"] for event in compact_response["events"]], [12, 14, 16])
        self.assertEqual(offload.await_count, 2)
        self.assertIs(offload.await_args_list[0].args[0], agent_server.read_visible_events_page)
        self.assertIs(offload.await_args_list[1].args[0], agent_server.read_visible_events_after_page)
        self.assertTrue(offload.await_args_list[1].kwargs["compact"])

    async def test_endpoint_exposes_additive_semantic_paging_fields(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Semantic history",
            "backend": "codex",
            "created_at": "2026-07-19T00:00:00Z",
            "updated_at": "2026-07-19T00:00:00Z",
        }
        self.write_events([
            self.event(1, "turn_started", run_id="run-1", prompt="Question"),
            self.event(2, "turn_finished", run_id="run-1", result_text="Answer"),
        ])
        with patch.dict(agent_server.STORE.sessions, {self.session_id: session}, clear=True):
            response = await agent_server.get_session(
                self.session_id,
                limit=1,
                tail=True,
                page_mode="semantic",
            )

        self.assertEqual(response["semantic_item_count"], 1)
        self.assertEqual(response["semantic_total"], 1)
        self.assertEqual(response["semantic_omitted_before"], 0)
        self.assertEqual(response["semantic_omitted_after"], 0)
        self.assertIsNone(response["next_semantic_before"])
        self.assertEqual(response["event_count"], 1)
        self.assertEqual([event["seq"] for event in response["events"]], [1, 2])

    def write_events(self, events: list[dict[str, object]]) -> None:
        agent_server.events_path(self.session_id).write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        agent_server.TIMELINE_INDEX_CACHE.clear()

    def event(self, seq: int, event_type: str, **fields: object) -> dict[str, object]:
        event: dict[str, object] = {
            "id": f"event-{seq}",
            "session_id": self.session_id,
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-19T00:00:{seq:02d}Z",
        }
        if event_type == "turn_started":
            event["prompt"] = "Start the conversation"
        elif event_type in {"assistant_text", "reasoning_summary"}:
            event["text"] = event_type
        elif event_type == "turn_finished":
            event["result_text"] = "Done"
        elif event_type in {"artifact_created", "file_uploaded"}:
            event["file"] = {"id": f"file-{seq}", "filename": f"file-{seq}.txt"}
        elif event_type.startswith("job_"):
            event["job_id"] = "job-1"
        elif event_type == "raw_event":
            event["raw"] = "provider packet"
        event.update(fields)
        return event


if __name__ == "__main__":
    unittest.main()
