import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import agent_server
import session_unread as unread


def event(seq, kind, run="", **fields):
    return {"seq": seq, "type": kind, "run_id": run, "session_id": "chat", **fields}


class UnreadProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.session = {"id": "chat", "backend": "codex", "manual_unread": True, "last_read_agent_event_seq": 0}
        self.store = SimpleNamespace(sessions={"chat": self.session}, save=AsyncMock(), ensure_sort_orders=AsyncMock())
        self.patches = [patch.object(agent_server, "STORE", self.store),
                        patch.object(agent_server, "events_path", return_value=self.path),
                        patch.object(agent_server, "RUN_METADATA", {}),
                        patch.object(agent_server, "NON_JOB_AGENT_RECOVERY_PENDING", set())]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def write(self, events, mode="w"):
        with self.path.open(mode) as target:
            target.writelines(json.dumps(row) + "\n" for row in events)

    async def append(self, row):
        self.write([row], "a")
        await agent_server.update_session_event_metadata("chat", row)

    def recover(self, snapshot=None, **overrides):
        options = {"visible": agent_server.is_agent_visible_event,
                   "resume_offset": agent_server.event_index_resume_offset,
                   "prior_events": lambda: agent_server.reversed_jsonl_events(self.path, retained_fields=frozenset({"seq", "type", "run_id", "purpose", "job_id", "job"})),
                   "cancelled": lambda: False, **overrides}
        return unread.recover_projection(self.path, snapshot or {}, **options)

    async def test_live_human_before_and_after_jobs_preserves_read_state(self):
        for row in [event(1, "session_created"), event(2, "turn_started", "human"),
                    event(3, "assistant_text", "human", text="Human answer"),
                    event(4, "turn_finished", "human", result_text="Done"),
                    event(5, "turn_started", "job", purpose="scheduled_job", job_id="schedule"),
                    event(6, "assistant_text", "job", text="Raw provider output with metadata stripped"),
                    event(7, "turn_finished", "job", result_text="Job done")]:
            await self.append(row)
        self.assertEqual(self.session[unread.COUNTER], 4)
        self.assertEqual(self.session["latest_agent_event_seq"], 7)
        self.assertTrue(self.session["manual_unread"])
        self.assertEqual(self.session["last_read_agent_event_seq"], 0)
        await self.append(event(8, "turn_started", "human-new"))
        await self.append(event(9, "assistant_text", "human-new", text="New human answer"))
        self.assertEqual(self.session[unread.COUNTER], 9)
        for summary in (True, False):
            self.assertEqual(agent_server.public_session(self.session, summary=summary)[unread.COUNTER], 9)

    async def test_job_types_and_runtime_provenance_do_not_promote(self):
        await self.append(event(1, "session_created"))
        agent_server.RUN_METADATA["job"] = {"purpose": "scheduled_job", "job_id": "schedule"}
        await self.append(event(2, "assistant_text", "job", text="Raw output"))
        for seq, kind in enumerate(["job_ran", "job_finished", "job_error"], 3):
            await self.append(event(seq, kind))
        await self.append(event(6, "artifact_created", "job", job_id="schedule"))
        self.assertEqual(self.session[unread.COUNTER], 0)
        self.assertEqual(self.session["latest_agent_event_seq"], 6)

    async def test_later_job_marker_is_retained_as_live_and_recovered_run_provenance(self):
        for row in [event(1, "session_created"), event(2, "turn_started", "job"),
                    event(3, "job_ran", "job", job_id="schedule"), event(4, "assistant_text", "job", text="Untagged output")]:
            await self.append(row)
        self.assertEqual(self.session[unread.COUNTER], 0)
        result = self.recover()
        self.assertTrue(result[unread.LAST_RUN]["scheduled_job"])
        self.session.update(result)
        await self.append(event(5, "turn_finished", "job", result_text="Untagged finish"))
        self.assertEqual(self.session[unread.COUNTER], 0)

    def test_legacy_scan_resolves_stripped_outputs_late_job_markers_and_reused_ids(self):
        self.write([event(1, "turn_started", "human"), event(2, "assistant_text", "human", text="Read me"),
                    event(3, "turn_finished", "human"), event(4, "turn_started", "reused"),
                    event(5, "assistant_text", "reused", text="Job output"), event(6, "turn_finished", "reused", result_text="Done"),
                    event(7, "job_ran", "reused", job_id="schedule")])
        self.assertEqual(self.recover()[unread.COUNTER], 2)
        self.write([event(8, "turn_started", "reused"), event(9, "assistant_text", "reused", text="Human now")], "a")
        self.assertEqual(self.recover()[unread.COUNTER], 9)

    async def test_imported_history_never_promotes_after_a_later_job(self):
        for row in [event(1, "session_created"), event(2, "assistant_text", "import_native", imported=True, text="Imported answer"),
                    event(3, "turn_started", "job", job_id="schedule"), event(4, "assistant_text", "job", text="Job output")]:
            await self.append(row)
        self.assertEqual(self.session[unread.COUNTER], 0)
        self.assertEqual(self.recover()[unread.COUNTER], 0)

    def test_restart_only_reads_tail_and_resolves_job_start_before_checkpoint(self):
        self.write([event(1, "turn_started", "human"), event(2, "assistant_text", "human", text="Answer"),
                    event(3, "turn_started", "job", purpose="scheduled_job")])
        persisted = json.loads(json.dumps(self.recover()))
        self.write([event(4, "assistant_text", "job", text="Raw tail"), event(5, "turn_finished", "job", result_text="Done")], "a")
        previous = Mock(wraps=lambda: agent_server.reversed_jsonl_events(self.path))
        offset = Mock(side_effect=AssertionError("persisted byte checkpoint avoids index rebuild"))
        recovered = self.recover(persisted, prior_events=previous, resume_offset=offset)
        self.assertEqual(recovered[unread.COUNTER], 2)
        self.assertEqual(recovered[unread.CHECKPOINT], 5)
        previous.assert_called_once()
        previous.reset_mock()
        self.assertEqual(self.recover(recovered, prior_events=previous, resume_offset=offset)[unread.COUNTER], 2)
        previous.assert_not_called()

    def test_late_untagged_job_output_is_not_promoted_after_terminal_retirement(self):
        self.write([event(1, "turn_started", "human"), event(2, "assistant_text", "human", text="Answer"),
                    event(3, "turn_started", "job", job_id="schedule"), event(4, "turn_finished", "job", result_text="Done"),
                    event(5, "assistant_text", "job", text="Late raw job output")])
        self.assertEqual(self.recover()[unread.COUNTER], 2)

    def test_fixed_extent_does_not_claim_concurrent_append(self):
        self.write([event(1, "assistant_text", "", text="Before")])
        def visible(kind, row):
            self.write([event(2, "assistant_text", "", text="After")], "a")
            return agent_server.is_agent_visible_event(kind, row)
        result = self.recover(visible=visible)
        self.assertEqual(result[unread.COUNTER], 1)
        self.assertEqual(result[unread.CHECKPOINT], 1)
        self.assertEqual(self.recover(result)[unread.COUNTER], 2)

    def test_partial_tail_keeps_complete_prefix_and_recovers_when_finished(self):
        self.write([event(1, "assistant_text", text="Complete")])
        complete_size = self.path.stat().st_size
        tail = json.dumps(event(2, "assistant_text", text="Later")) + "\n"
        with self.path.open("a") as source:
            source.write(tail[:20])
        result = self.recover()
        self.assertEqual(result[unread.COUNTER], 1)
        self.assertEqual(result[unread.JOURNAL]["offset"], complete_size)
        with self.path.open("a") as source:
            source.write(tail[20:])
        self.assertEqual(self.recover(result)[unread.COUNTER], 2)

    def test_replaced_journal_rebuilds_instead_of_trusting_old_checkpoint(self):
        self.write([event(1, "assistant_text", text="Old")])
        previous = self.recover()
        replacement = self.path.with_suffix(".replacement")
        replacement.write_text(json.dumps(event(1, "job_finished", job_id="job")) + "\n")
        replacement.replace(self.path)
        self.assertEqual(self.recover(previous)[unread.COUNTER], 0)

    async def test_recovery_failure_keeps_unknown_and_does_not_mutate_read_metadata(self):
        self.session.update({unread.COUNTER: 2, unread.CHECKPOINT: 3, "latest_event_seq": 3, "latest_agent_event_seq": 3})
        before = copy.deepcopy(self.session)
        items = agent_server.prepare_non_job_agent_recovery()
        self.assertIsNone(agent_server.public_session(self.session, summary=True)[unread.COUNTER])
        with patch.object(unread, "recover_projection", side_effect=OSError("fixture read failure")):
            await agent_server.recover_non_job_agent_metadata(items)
        self.assertIsNone(agent_server.public_session(self.session, summary=False)[unread.COUNTER])
        self.assertEqual(self.session, before)
        self.store.save.assert_not_awaited()

    async def test_background_recovery_merges_only_live_events_beyond_its_extent(self):
        self.session.update({"latest_event_seq": 7, "latest_agent_event_seq": 7, unread.PENDING_COUNTER: 6})
        items = agent_server.prepare_non_job_agent_recovery()
        entered, release = threading.Event(), threading.Event()
        def recover(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return {unread.COUNTER: 2, unread.CHECKPOINT: 7, unread.LAST_RUN: {}}
        with patch.object(unread, "recover_projection", side_effect=recover):
            task = asyncio.create_task(agent_server.recover_non_job_agent_metadata(items))
            await asyncio.to_thread(entered.wait, 5)
            listed = await agent_server.list_sessions(summary=True)
            self.assertIsNone(listed["sessions"][0][unread.COUNTER])
            await self.append(event(8, "turn_started", "human"))
            await self.append(event(9, "assistant_text", "human", text="New answer"))
            release.set()
            await task
        self.assertEqual(self.session[unread.COUNTER], 9)
        self.assertEqual(self.session["last_read_agent_event_seq"], 0)
        self.assertTrue(self.session["manual_unread"])
        self.assertNotIn(unread.PENDING_COUNTER, self.session)

    async def test_authoritative_scan_retires_covered_pending_candidate(self):
        self.write([event(1, "turn_started", "job", job_id="schedule"), event(2, "assistant_text", "job", text="Job output")])
        self.session.update({"latest_event_seq": 2, "latest_agent_event_seq": 2, unread.PENDING_COUNTER: 2})
        await agent_server.recover_non_job_agent_metadata(agent_server.prepare_non_job_agent_recovery())
        self.assertEqual(self.session[unread.COUNTER], 0)
        self.assertEqual(self.session["latest_agent_event_seq"], 2)
        self.assertEqual(self.session["last_read_agent_event_seq"], 0)
        self.store.save.assert_awaited_once_with(flush=False)

    async def test_deletion_during_recovery_cannot_update_replacement_session(self):
        items = agent_server.prepare_non_job_agent_recovery()
        replacement = {"id": "chat", "backend": "codex"}
        def recover(*_args, **_kwargs):
            self.store.sessions["chat"] = replacement
            return {unread.COUNTER: 10, unread.CHECKPOINT: 10, unread.LAST_RUN: {}}
        with patch.object(unread, "recover_projection", side_effect=recover):
            await agent_server.recover_non_job_agent_metadata(items)
        self.assertNotIn(unread.COUNTER, replacement)
        self.store.save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
