import json
import tempfile
import unittest
from pathlib import Path

import agent_server


class TimelineSearchForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = agent_server.STATE_DIR
        self.previous_search_db = agent_server.HISTORY_SEARCH_DB
        agent_server.STATE_DIR = Path(self.temporary.name)
        agent_server.HISTORY_SEARCH_DB = agent_server.STATE_DIR / "history_search.sqlite3"
        agent_server.FORK_INTERNAL_RUN_CACHE.clear()
        agent_server.FORK_INTERNAL_RUN_LOCKS.clear()
        self.session_id = "legacy-fork"
        path = agent_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            self.event(1, "turn_started", run_id="digest-run", purpose="handoff_digest", forked=True, prompt="Generate private digest"),
            self.event(2, "reasoning_summary", run_id="digest-run", forked=True, text="Private digest reasoning"),
            self.event(3, "assistant_text", run_id="digest-run", forked=True, text="Private digest body"),
            self.event(4, "turn_started", run_id="normal-run", forked=True, prompt="Retained searchable question"),
            self.event(5, "assistant_text", run_id="normal-run", forked=True, text="Retained searchable answer"),
        ]
        path.write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        agent_server.FORK_INTERNAL_RUN_CACHE.clear()
        agent_server.FORK_INTERNAL_RUN_LOCKS.clear()
        agent_server.STATE_DIR = self.previous_state_dir
        agent_server.HISTORY_SEARCH_DB = self.previous_search_db
        self.temporary.cleanup()

    def test_search_excludes_the_entire_copied_digest_run(self) -> None:
        connection = agent_server.history_search_connection()
        try:
            indexed_sessions, indexed_events = agent_server.sync_history_search_index(
                connection,
                {self.session_id},
                {self.session_id},
            )
        finally:
            connection.close()

        self.assertEqual((indexed_sessions, indexed_events), (1, 2))
        self.assertEqual(agent_server.search_timeline_index(self.session_id, "private"), {
            "session_id": self.session_id,
            "query": "private",
            "results": [],
        })
        retained = agent_server.search_timeline_index(self.session_id, "searchable")
        self.assertEqual([result["seq"] for result in retained["results"]], [5, 4])

    def test_search_index_version_change_removes_legacy_rows_before_rebuild(self) -> None:
        connection = agent_server.history_search_connection()
        connection.execute(
            "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) VALUES (?, ?, ?, ?, ?, ?)",
            ("stale private digest", self.session_id, "legacy-event", 1, None, "assistant"),
        )
        connection.execute(
            "INSERT INTO history_search_state(session_id, inode, offset, mtime_ns) VALUES (?, ?, ?, ?)",
            (self.session_id, 1, 1, 1),
        )
        connection.execute(
            "UPDATE history_search_meta SET value = '1' WHERE key = 'index_version'"
        )
        connection.commit()
        connection.close()

        migrated = agent_server.history_search_connection()
        try:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM history_search").fetchone()[0], 0)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM history_search_state").fetchone()[0], 0)
            self.assertEqual(
                migrated.execute(
                    "SELECT value FROM history_search_meta WHERE key = 'index_version'"
                ).fetchone()[0],
                agent_server.HISTORY_SEARCH_INDEX_VERSION,
            )
        finally:
            migrated.close()

    def event(self, seq: int, event_type: str, **fields: object) -> dict[str, object]:
        return {
            "id": f"event-{seq}",
            "session_id": self.session_id,
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-21T00:00:{seq:02d}Z",
            **fields,
        }


if __name__ == "__main__":
    unittest.main()
