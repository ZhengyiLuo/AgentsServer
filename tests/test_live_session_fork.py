import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server as server


class CompletedPrefixForkTests(unittest.IsolatedAsyncioTestCase):
    def events(self):
        return [
            {"seq": 1, "type": "turn_started", "run_id": "done", "ts": "2026-09-08T10:00:00Z", "prompt": "Earlier question"},
            {"seq": 2, "type": "reasoning_summary", "phase": "commentary", "run_id": "done", "text": "Completed answer"},
            {"seq": 3, "type": "turn_finished", "run_id": "done", "ts": "2026-09-08T10:01:00Z", "exit_code": 0, "result_text": "Completed answer", "provider_thread_id": "parent-thread", "provider_turn_id": "done-turn"},
            {"seq": 4, "type": "file_uploaded", "file": {"id": "in-progress-file"}},
            {"seq": 5, "type": "turn_started", "run_id": "live", "prompt": "Do not copy"},
            {"seq": 6, "type": "turn_finished", "run_id": "live", "exit_code": 0},
        ]

    def test_prefix_excludes_current_prompt_files_and_racing_completion(self):
        events = self.events()
        self.assertEqual(server.completed_fork_events(events, active_run_id="live", through_sequence=6), events[:3])
        self.assertEqual(server.completed_fork_events(events, active_run_id="", through_sequence=3), events[:3])
        self.assertEqual(server.completed_fork_events(events[3:], active_run_id="live", through_sequence=6), [])

    def test_imported_history_is_not_mistaken_for_an_empty_chat(self):
        events = [{"type": "assistant_text", "text": "Previous history"}, {"type": "turn_finished", "imported": True}]
        self.assertEqual(server.completed_fork_events(events, active_run_id="live", through_sequence=None), events)

    def test_claude_boundary_requires_this_completed_turn_not_older_matching_text(self):
        events = self.events()[:3]
        native = {
            "type": "assistant", "uuid": "completed-uuid", "timestamp": "2026-09-08T10:00:40Z",
            "message": {"content": [{"type": "text", "text": "Completed answer"}]},
        }
        old = {**native, "uuid": "older-uuid", "timestamp": "2026-09-08T09:00:00Z"}
        future = {**native, "uuid": "running-uuid", "timestamp": "2026-09-08T10:02:00Z"}
        with patch.object(server, "claude_resume_file_for_cwd", return_value=Mock(is_file=Mock(return_value=True))), patch.object(
            server, "bounded_jsonl_events", return_value=[old, native, future],
        ):
            self.assertEqual(server.claude_completed_fork_boundary({"cwd": "/tmp"}, "claude-parent", events), "completed-uuid")
        with patch.object(server, "claude_resume_file_for_cwd", return_value=Mock(is_file=Mock(return_value=True))), patch.object(
            server, "bounded_jsonl_events", return_value=[old, future],
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server.claude_completed_fork_boundary({"cwd": "/tmp"}, "claude-parent", events)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_running_endpoint_passes_frozen_prefix_without_touching_active_run(self):
        parent = {"id": "parent", "backend": "claude", "latest_event_seq": 6}
        active = {"parent": {"run_id": "live", "sentinel": object()}}
        with patch.object(server.STORE, "sessions", {"parent": parent}), patch.object(server, "ACTIVE", active), patch.object(
            server, "iter_session_events", return_value=iter(self.events()),
        ), patch.object(server, "_fork_session_locked", new_callable=AsyncMock, return_value={"ok": True}) as fork:
            self.assertEqual(await server.fork_session("parent", server.ForkSessionRequest()), {"ok": True})
        self.assertEqual(fork.await_args.kwargs["completed_snapshot"], self.events()[:3])
        self.assertEqual(active["parent"]["run_id"], "live")

    async def test_codex_live_native_failure_never_becomes_memory_fork(self):
        parent = {"id": "parent", "backend": "codex", "cwd": "/tmp", "codex_thread_id": "parent-thread"}
        with patch.object(server.STORE, "sessions", {"parent": parent}), patch.object(
            server, "fork_codex_thread", new_callable=AsyncMock, side_effect=RuntimeError("unsupported cutoff"),
        ) as fork, patch.object(server.STORE, "create", new_callable=AsyncMock) as create, patch.object(server, "build_fork_memory") as memory:
            with self.assertRaises(server.HTTPException) as raised:
                await server._fork_session_locked("parent", server.ForkSessionRequest(), completed_snapshot=self.events()[:3])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(fork.await_args.kwargs, {"last_turn_id": "done-turn"})
        create.assert_not_awaited()
        memory.assert_not_called()

    async def test_no_completed_turn_creates_empty_child_for_both_native_backends(self):
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                parent = {"id": "parent", "backend": backend, "cwd": "/tmp", "claude_session_id": "claude-parent", "codex_thread_id": "parent-thread"}
                child = {"id": "child", "backend": backend, "cwd": "/tmp", "_fork_initializing": True}
                sessions = {"parent": parent}

                async def create_child(*args, **kwargs):
                    sessions["child"] = child
                    return child

                with patch.object(server.STORE, "sessions", sessions), patch.object(server.STORE, "_lock", asyncio.Lock()), patch.object(
                    server.STORE, "create", new_callable=AsyncMock, side_effect=create_child,
                ), patch.object(server.STORE, "reorder", new_callable=AsyncMock), patch.object(
                    server.STORE, "update", new_callable=AsyncMock, return_value=child,
                ), patch.object(server.STORE, "save", new_callable=AsyncMock), patch.object(
                    server, "copy_fork_history", new_callable=AsyncMock, return_value=0,
                ) as copy, patch.object(server, "append_event", new_callable=AsyncMock), patch.object(
                    server, "fork_codex_thread", new_callable=AsyncMock,
                ) as fork, patch.object(server, "validated_claude_fork_provider_id") as claude, patch.object(server, "build_fork_memory") as memory:
                    result = await server._fork_session_locked("parent", server.ForkSessionRequest(), completed_snapshot=[])
                self.assertEqual(result["session"]["id"], "child")
                copy.assert_awaited_once_with("parent", "child", source_events=[])
                fork.assert_not_awaited()
                claude.assert_not_called()
                memory.assert_not_called()
                self.assertNotIn("fork_from", child)

    async def test_codex_native_cutoff_is_sent_and_verified_on_new_child_only(self):
        manager = Mock(fork_thread=AsyncMock(return_value="child-thread"), read_thread=AsyncMock(return_value={
            "forkedFromId": "parent-thread", "cwd": "/tmp",
        }), list_turns=AsyncMock(return_value=[{"id": "done-turn", "status": "completed"}]))
        with patch.object(server, "codex_app_server_manager", new_callable=AsyncMock, return_value=manager), patch.object(
            server, "persist_abandoned_fork_provider_thread", new_callable=AsyncMock, return_value=True,
        ), patch.object(server, "touch_codex_app_server_thread", new_callable=AsyncMock):
            self.assertEqual(await server.fork_codex_thread("parent-thread", {"cwd": "/tmp", "backend": "codex"}, last_turn_id="done-turn"), "child-thread")
        self.assertEqual(manager.fork_thread.await_args.kwargs, {"last_turn_id": "done-turn"})
        manager.list_turns.assert_awaited_once_with("child-thread", limit=1, items_view="summary", sort_direction="desc")

    def test_claude_missing_deferred_snapshot_never_launches_fresh(self):
        with self.assertRaisesRegex(ValueError, "refusing an empty resume"):
            server.build_claude_cmd("child", {"fork_from": "parent", "fork_resume_session_at": "uuid"}, Path("manifest"))

    def test_imported_checkpoint_proves_exact_completed_tail_despite_later_live_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "provider.jsonl"
            parent = {"id": "parent", "backend": "claude", "claude_session_id": "provider", "cwd": str(root)}
            native = {
                "type": "assistant", "uuid": "completed-uuid", "timestamp": "2026-09-08T10:00:40Z",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Completed answer"}]},
            }
            for invalid in (None, "partial", "later-user", "changed-prefix"):
                with self.subTest(invalid=invalid):
                    record = {**native, "message": {**native["message"], "stop_reason": None}} if invalid == "partial" else native
                    prefix = (json.dumps(record) + "\n").encode()
                    if invalid == "later-user":
                        prefix += (json.dumps({"type": "user", "message": {"content": "New incomplete turn"}}) + "\n").encode()
                    path.write_bytes(prefix + b'{"type":"user","message":{"content":"LIVE: must not copy"}}\n')
                    stat = path.stat()
                    cursor = {
                        "version": server.HISTORY_SYNC_CURSOR_VERSION, "backend": "claude", "provider_session_id": "provider",
                        "source_path": str(path), "source_dev": stat.st_dev, "source_ino": stat.st_ino,
                        "source_size": len(prefix), "source_offset": len(prefix), "source_mtime_ns": stat.st_mtime_ns,
                        "source_digest": "0" * 64 if invalid == "changed-prefix" else hashlib.sha256(prefix).hexdigest(),
                        "last_item_digest": "", "timeline_seq": 3,
                    }
                    events = [
                        {"seq": 1, "type": "history_imported", "run_id": "import", "_history_sync_checkpoint": {"caught_up": True, "cursor": cursor}},
                        {"seq": 2, "type": "assistant_text", "run_id": "import", "imported": True, "text": "Completed answer"},
                        {"seq": 3, "type": "turn_finished", "run_id": "import", "imported": True, "result_text": "", "ts": "2026-09-08T10:42:00Z"},
                    ]
                    with patch.object(server, "CLAUDE_PROJECTS_ROOT", root):
                        if invalid:
                            with self.assertRaises(ValueError):
                                server.claude_completed_fork_boundary(parent, "provider", events)
                        else:
                            self.assertEqual(server.claude_completed_fork_boundary(parent, "provider", events), "completed-uuid")
                    self.assertEqual(events[-1]["seq"], 3)
