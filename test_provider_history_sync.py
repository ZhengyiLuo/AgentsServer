"""Catching a chat up with provider messages added outside AgentsDock.

Importing a remote conversation used to be a one-time snapshot: continuing it
in the provider's own CLI grew the transcript, AgentsDock never noticed, and
the next AgentsDock turn resumed the thread so the model answered with full
context the timeline had never shown. Re-importing was no fix either - it had
no dedup and appended the whole conversation a second time.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


def user(text: str) -> dict[str, str]:
    return {"kind": "user", "text": text}


def assistant(text: str) -> dict[str, str]:
    return {"kind": "assistant", "text": text}


class UnsyncedHistoryItemsTests(unittest.TestCase):
    """Pure selection logic: what does the timeline not have yet?"""

    def select(self, events, items):
        with patch.object(agent_server, "read_events", return_value=events):
            return agent_server.unsynced_history_items("chat-x", items)

    def test_nothing_new_when_the_timeline_already_shows_it_all(self) -> None:
        events = [
            {"type": "turn_started", "prompt": "hello"},
            {"type": "assistant_text", "text": "hi there"},
        ]
        self.assertEqual(
            self.select(events, [user("hello"), assistant("hi there")]), []
        )

    def test_returns_only_the_tail_added_elsewhere(self) -> None:
        events = [
            {"type": "turn_started", "prompt": "hello"},
            {"type": "assistant_text", "text": "hi there"},
        ]
        fresh = self.select(
            events,
            [
                user("hello"),
                assistant("hi there"),
                user("continued in the CLI"),
                assistant("answered in the CLI"),
            ],
        )
        self.assertEqual(
            fresh, [user("continued in the CLI"), assistant("answered in the CLI")]
        )

    def test_everything_is_new_for_an_empty_timeline(self) -> None:
        self.assertEqual(
            self.select([], [user("hello"), assistant("hi")]),
            [user("hello"), assistant("hi")],
        )

    def test_whitespace_differences_still_count_as_the_same_message(self) -> None:
        # The two sides reach the timeline through different cleaning paths.
        events = [{"type": "assistant_text", "text": "hi   there\n\n"}]
        self.assertEqual(self.select(events, [assistant("hi there")]), [])

    def test_a_repeated_message_is_matched_once_per_occurrence(self) -> None:
        # "continue" sent twice must not make the second one look already
        # imported.
        events = [
            {"type": "turn_started", "prompt": "continue"},
            {"type": "assistant_text", "text": "ok"},
        ]
        fresh = self.select(
            events, [user("continue"), assistant("ok"), user("continue")]
        )
        self.assertEqual(fresh, [user("continue")])

    def test_a_mid_history_mismatch_never_splices_a_duplicate(self) -> None:
        # If an older message fails to match, the safe outcome is importing
        # nothing extra - not re-inserting it in the middle of the chat.
        events = [
            {"type": "turn_started", "prompt": "hello"},
            {"type": "assistant_text", "text": "TIMELINE VERSION"},
            {"type": "turn_started", "prompt": "second"},
            {"type": "assistant_text", "text": "second reply"},
        ]
        fresh = self.select(
            events,
            [
                user("hello"),
                assistant("TRANSCRIPT VERSION"),
                user("second"),
                assistant("second reply"),
            ],
        )
        self.assertEqual(fresh, [])

    def test_turns_run_by_agentsdock_are_not_re_imported(self) -> None:
        # The transcript also contains everything this server ran itself.
        events = [
            {"type": "turn_started", "prompt": "asked from AgentsDock"},
            {"type": "assistant_text", "text": "answered to AgentsDock"},
        ]
        self.assertEqual(
            self.select(
                events,
                [user("asked from AgentsDock"), assistant("answered to AgentsDock")],
            ),
            [],
        )


class SyncProviderHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.transcript = Path(self.tempdir.name) / "rollout.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.sess = {
            "id": "chat-sync",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-1",
        }

    async def test_appends_only_the_externally_added_tail(self) -> None:
        items = [
            user("hello"),
            assistant("hi there"),
            user("continued in the CLI"),
            assistant("answered in the CLI"),
        ]
        events = [
            {"type": "turn_started", "prompt": "hello"},
            {"type": "assistant_text", "text": "hi there"},
        ]
        appended: list[tuple[str, dict]] = []

        async def fake_append(session_id, event_type, payload=None):
            appended.append((event_type, payload or {}))
            return {}

        with patch.object(
            agent_server, "provider_history", return_value=(self.transcript, items)
        ), patch.object(
            agent_server, "read_events", return_value=events
        ), patch.object(
            agent_server, "append_event", fake_append
        ):
            result = await agent_server.sync_provider_history(self.sess)

        self.assertEqual(result["imported"], 2)
        prompts = [p.get("prompt") for t, p in appended if t == "turn_started"]
        texts = [p.get("text") for t, p in appended if t == "assistant_text"]
        self.assertEqual(prompts, ["continued in the CLI"])
        self.assertEqual(texts, ["answered in the CLI"])
        # The already-shown opening must not be replayed.
        self.assertNotIn("hello", prompts)
        self.assertNotIn("hi there", texts)

    async def test_up_to_date_transcript_appends_nothing_at_all(self) -> None:
        # Opening a chat repeatedly must not keep adding landmark events.
        items = [user("hello"), assistant("hi there")]
        events = [
            {"type": "turn_started", "prompt": "hello"},
            {"type": "assistant_text", "text": "hi there"},
        ]
        appended: list[str] = []

        async def fake_append(session_id, event_type, payload=None):
            appended.append(event_type)
            return {}

        with patch.object(
            agent_server, "provider_history", return_value=(self.transcript, items)
        ), patch.object(
            agent_server, "read_events", return_value=events
        ), patch.object(
            agent_server, "append_event", fake_append
        ):
            result = await agent_server.sync_provider_history(self.sess)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(appended, [])

    async def test_session_without_a_provider_id_is_skipped(self) -> None:
        result = await agent_server.sync_provider_history(
            {"id": "chat-none", "backend": agent_server.BACKEND_CODEX}
        )
        self.assertEqual(result["imported"], 0)


class ScheduleProviderHistorySyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_scheduled = set(agent_server.HISTORY_SYNC_SCHEDULED)
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_active = agent_server.ACTIVE
        agent_server.HISTORY_SYNC_SCHEDULED.clear()
        agent_server.BUSY_SESSIONS = set()
        agent_server.ACTIVE = {}

    async def asyncTearDown(self) -> None:
        agent_server.HISTORY_SYNC_SCHEDULED.clear()
        agent_server.HISTORY_SYNC_SCHEDULED.update(self.previous_scheduled)
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.ACTIVE = self.previous_active

    def sess(self) -> dict:
        return {
            "id": "chat-open",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-1",
        }

    async def test_repeated_opens_do_not_start_overlapping_syncs(self) -> None:
        started = 0

        async def fake_run(session_id):
            nonlocal started
            started += 1
            await asyncio.sleep(0)

        with patch.object(agent_server, "run_provider_history_sync", fake_run):
            for _ in range(5):
                agent_server.schedule_provider_history_sync(self.sess())
            await asyncio.sleep(0)

        self.assertEqual(started, 1)

    async def test_busy_session_is_left_alone(self) -> None:
        # A live turn is already streaming provider output and still writing
        # its transcript.
        agent_server.BUSY_SESSIONS = {"chat-open"}
        started = False

        async def fake_run(session_id):
            nonlocal started
            started = True

        with patch.object(agent_server, "run_provider_history_sync", fake_run):
            agent_server.schedule_provider_history_sync(self.sess())
            await asyncio.sleep(0)

        self.assertFalse(started)

    async def test_session_without_provider_id_is_not_scheduled(self) -> None:
        started = False

        async def fake_run(session_id):
            nonlocal started
            started = True

        with patch.object(agent_server, "run_provider_history_sync", fake_run):
            agent_server.schedule_provider_history_sync(
                {"id": "chat-open", "backend": agent_server.BACKEND_CODEX}
            )
            await asyncio.sleep(0)

        self.assertFalse(started)

    async def test_a_failing_sync_clears_its_slot_so_a_later_open_retries(
        self,
    ) -> None:
        async def boom(sess, **kwargs):
            raise RuntimeError("transcript unreadable")

        with patch.object(agent_server.STORE, "sessions", {"chat-open": self.sess()}), \
                patch.object(agent_server, "sync_provider_history", boom):
            agent_server.HISTORY_SYNC_SCHEDULED.add("chat-open")
            await agent_server.run_provider_history_sync("chat-open")

        self.assertNotIn("chat-open", agent_server.HISTORY_SYNC_SCHEDULED)


if __name__ == "__main__":
    unittest.main()
