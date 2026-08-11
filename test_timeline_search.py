import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            self.event(
                6,
                "turn_started",
                run_id="handoff-run",
                purpose="cross_chat_handoff_delivery",
                cross_chat_envelope_id="handoff-hidden",
                prompt="Hidden relay needle",
            ),
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
        self.assertEqual(agent_server.search_timeline_index(self.session_id, "relay"), {
            "session_id": self.session_id,
            "query": "relay",
            "results": [],
        })

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

    def test_initializing_fork_is_not_eligible_for_history_indexing(self) -> None:
        staged_id = "staged-fork"
        staged_path = agent_server.events_path(staged_id)
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(
            json.dumps({
                "id": "staged-event",
                "session_id": staged_id,
                "seq": 1,
                "type": "assistant_text",
                "ts": "2026-07-21T01:00:00Z",
                "text": "staged-only needle",
            }, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        sessions = {
            self.session_id: {
                "id": self.session_id,
                "archived": False,
            },
            staged_id: {
                "id": staged_id,
                "archived": False,
                "_fork_initializing": True,
            },
        }

        with patch.object(agent_server.STORE, "sessions", sessions):
            active_ids = agent_server.active_history_search_session_ids()
            connection = agent_server.history_search_connection()
            try:
                agent_server.sync_history_search_index(
                    connection,
                    {self.session_id, staged_id},
                    active_ids,
                )
                indexed_session_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT session_id FROM history_search"
                    ).fetchall()
                }
            finally:
                connection.close()

        self.assertEqual(active_ids, {self.session_id})
        self.assertEqual(indexed_session_ids, {self.session_id})

    def test_queries_hide_stale_rows_from_initializing_fork(self) -> None:
        staged_id = "staged-fork"
        removed_id = "removed-staged-fork"
        connection = agent_server.history_search_connection()
        try:
            connection.executemany(
                "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "sharedneedle visible",
                        self.session_id,
                        "visible-event",
                        10,
                        "2026-07-21T01:00:00Z",
                        "assistant",
                    ),
                    (
                        "sharedneedle hidden",
                        staged_id,
                        "hidden-event",
                        20,
                        "2026-07-21T02:00:00Z",
                        "assistant",
                    ),
                    (
                        "sharedneedle removed",
                        removed_id,
                        "removed-event",
                        30,
                        "2026-07-21T03:00:00Z",
                        "assistant",
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO history_search_sessions(session_id, active) VALUES (?, 1)",
                [(self.session_id,), (staged_id,), (removed_id,)],
            )
            connection.commit()
        finally:
            connection.close()

        sessions = {
            self.session_id: {"id": self.session_id},
            staged_id: {"id": staged_id, "_fork_initializing": True},
        }
        with patch.object(agent_server.STORE, "sessions", sessions):
            global_results = agent_server.search_all_timelines("sharedneedle")
            staged_results = agent_server.search_timeline_index(
                staged_id,
                "sharedneedle",
            )

        self.assertEqual(
            [result["session_id"] for result in global_results["results"]],
            [self.session_id],
        )
        self.assertEqual(staged_results["results"], [])

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
