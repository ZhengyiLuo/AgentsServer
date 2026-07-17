import json
import tempfile
import unittest
from pathlib import Path

import agent_server


class TimelineIndexJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = agent_server.STATE_DIR
        agent_server.STATE_DIR = Path(self.temporary.name)
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()

    def tearDown(self) -> None:
        agent_server.TIMELINE_INDEX_CACHE.clear()
        agent_server.TIMELINE_INDEX_LOCKS.clear()
        agent_server.STATE_DIR = self.previous_state_dir
        self.temporary.cleanup()

    def test_scheduled_run_is_one_landmark_at_its_latest_update(self) -> None:
        session_id = "chat-1"
        path = agent_server.events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            self.event(1, "job_created", job_id="job-1", job={"id": "job-1", "title": "Status check"}),
            # Legacy scheduled runs reveal their job only in the following
            # job_ran event. The index must fold this provisional turn back
            # into the job instead of leaving a fake user landmark behind.
            self.event(2, "turn_started", run_id="job-run", prompt="Check status"),
            self.event(3, "job_ran", run_id="job-run", job_id="job-1"),
            self.event(4, "turn_started", run_id="user-run", prompt="What changed?"),
            self.event(5, "turn_finished", run_id="user-run", result_text="Nothing yet."),
            self.event(6, "process_started", run_id="job-run"),
            self.event(7, "reasoning_summary", run_id="job-run", text="Checking the live run"),
            self.event(8, "assistant_text", run_id="job-run", job_id="job-1", text="Everything is healthy."),
            self.event(9, "turn_finished", run_id="job-run", job_id="job-1", result_text="Everything is healthy."),
        ]
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

        index = agent_server.build_timeline_index(session_id)

        self.assertEqual([item["key"] for item in index["landmarks"]], ["turn:user-run", "job:job-1"])
        job = index["landmarks"][1]
        self.assertEqual(job["start_seq"], 9)
        self.assertEqual(job["end_seq"], 9)
        self.assertEqual(job["timestamp"], "2026-07-16T10:00:09Z")
        self.assertEqual(job["preview"], "Everything is healthy.")

    @staticmethod
    def event(seq: int, event_type: str, **fields: object) -> dict[str, object]:
        return {
            "id": f"event-{seq}",
            "session_id": "chat-1",
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-16T10:00:{seq:02d}Z",
            **fields,
        }


if __name__ == "__main__":
    unittest.main()
