import asyncio
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server


class ForkHistoryCloneTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def write_registered_file(
        root: Path,
        file_id: str,
        session_id: str,
        *,
        kind: str,
        filename: str,
        content: bytes,
    ) -> dict:
        file_root = root / file_id
        file_root.mkdir()
        path = file_root / filename
        path.write_bytes(content)
        record = {
            "id": file_id,
            "session_id": session_id,
            "kind": kind,
            "filename": filename,
            "path": str(path),
            "size": len(content),
            "content_type": "image/png" if filename.endswith(".png") else "text/plain",
            "created_at": "2026-07-01T10:00:00Z",
        }
        (file_root / "meta.json").write_text(json.dumps(record))
        return record

    async def test_clone_remaps_files_preserves_timestamps_and_stop_semantics(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        with tempfile.TemporaryDirectory() as temporary:
            files_root = Path(temporary)
            upload = self.write_registered_file(
                files_root,
                "file_parent_image",
                parent_id,
                kind="upload",
                filename="question.png",
                content=b"parent image",
            )
            artifact = self.write_registered_file(
                files_root,
                "art_parent_output",
                parent_id,
                kind="artifact",
                filename="answer.txt",
                content=b"parent artifact",
            )
            events = [
                {
                    "seq": 1,
                    "id": "upload-event",
                    "session_id": parent_id,
                    "type": "file_uploaded",
                    "ts": "2026-07-01T10:00:00Z",
                    "file": upload,
                },
                {
                    "seq": 2,
                    "id": "turn-event",
                    "session_id": parent_id,
                    "type": "turn_started",
                    "ts": "2026-07-01T10:01:00Z",
                    "run_id": "run-parent",
                    "prompt": "Inspect the image",
                    "file_ids": [upload["id"]],
                },
                {
                    "seq": 3,
                    "id": "commentary-event",
                    "session_id": parent_id,
                    "type": "reasoning_summary",
                    "ts": "2026-07-01T10:02:00Z",
                    "run_id": "run-parent",
                    "phase": "commentary",
                    "text": "I found the problem.",
                },
                {
                    "seq": 4,
                    "id": "stop-event",
                    "session_id": parent_id,
                    "type": "turn_stopped",
                    "ts": "2026-07-01T10:03:00Z",
                    "run_id": "run-parent",
                    "native_steer": True,
                    "superseded_by_run_id": "run-next",
                },
                {
                    "seq": 5,
                    "id": "artifact-event",
                    "session_id": parent_id,
                    "type": "artifact_created",
                    "ts": "2026-07-01T10:04:00Z",
                    "run_id": "run-parent",
                    "artifact": artifact,
                },
            ]

            with patch.object(agent_server, "FILES_ROOT", files_root), patch.object(
                agent_server,
                "iter_session_events",
                side_effect=lambda _session_id: iter(events),
            ), patch.object(
                agent_server,
                "append_imported_events",
                new_callable=AsyncMock,
                side_effect=lambda _session_id, imported: len(imported),
            ) as append_imported:
                copied = await agent_server.copy_fork_history(parent_id, child_id)

                imported = append_imported.await_args.args[1]
                self.assertEqual(copied, 5)
                self.assertEqual(
                    [event_type for event_type, _payload in imported],
                    [
                        "file_uploaded",
                        "turn_started",
                        "reasoning_summary",
                        "turn_stopped",
                        "artifact_created",
                    ],
                )

                cloned_upload = imported[0][1]["file"]
                cloned_artifact = imported[4][1]["artifact"]
                self.assertNotEqual(cloned_upload["id"], upload["id"])
                self.assertNotEqual(cloned_artifact["id"], artifact["id"])
                self.assertEqual(cloned_upload["session_id"], child_id)
                self.assertEqual(cloned_artifact["session_id"], child_id)
                self.assertEqual(
                    imported[1][1]["file_ids"],
                    [cloned_upload["id"]],
                )
                self.assertEqual(
                    imported[3][1]["superseded_by_run_id"],
                    "run-next",
                )
                self.assertEqual(
                    [payload["ts"] for _event_type, payload in imported],
                    [event["ts"] for event in events],
                )
                self.assertTrue(all(
                    payload["original_ts"] == payload["ts"]
                    for _event_type, payload in imported
                ))

                self.assertEqual(
                    Path(cloned_upload["path"]).read_bytes(),
                    b"parent image",
                )
                self.assertEqual(
                    Path(cloned_artifact["path"]).read_bytes(),
                    b"parent artifact",
                )
                self.assertEqual(
                    agent_server.validate_session_file_ids(
                        child_id,
                        [cloned_upload["id"], cloned_artifact["id"]],
                    ),
                    [cloned_upload["id"], cloned_artifact["id"]],
                )
                with self.assertRaises(agent_server.HTTPException):
                    agent_server.validate_session_file_ids(
                        child_id,
                        [upload["id"]],
                    )
                self.assertEqual(Path(upload["path"]).read_bytes(), b"parent image")
                self.assertEqual(json.loads(
                    (files_root / upload["id"] / "meta.json").read_text()
                )["session_id"], parent_id)

    def test_fork_source_rejects_registry_entry_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            files_root = Path(temporary) / "files"
            files_root.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            outside_file = outside / "secret.txt"
            outside_file.write_text("secret")
            (files_root / "file_parent").symlink_to(outside, target_is_directory=True)
            record = {
                "id": "file_parent",
                "session_id": "parent-chat",
                "path": str(outside_file),
            }

            with patch.object(agent_server, "FILES_ROOT", files_root):
                self.assertIsNone(
                    agent_server.fork_file_source_path("file_parent", record)
                )

    async def test_cancelled_file_clone_joins_worker_and_removes_child_copy(self) -> None:
        started = threading.Event()
        release = threading.Event()
        child_id = "child-chat"
        cloned_id = "file_child_clone"
        with tempfile.TemporaryDirectory() as temporary:
            files_root = Path(temporary)

            def slow_clone(*_args) -> dict:
                started.set()
                release.wait(timeout=5)
                cloned_root = files_root / cloned_id
                cloned_root.mkdir()
                cloned_path = cloned_root / "copy.txt"
                cloned_path.write_text("copy")
                return {
                    "id": cloned_id,
                    "session_id": child_id,
                    "path": str(cloned_path),
                }

            with patch.object(agent_server, "FILES_ROOT", files_root), patch.object(
                agent_server,
                "clone_fork_file_record",
                side_effect=slow_clone,
            ):
                task = asyncio.create_task(
                    agent_server.clone_fork_file_record_async(
                        "parent-chat",
                        child_id,
                        {"id": "file_parent"},
                    )
                )
                await asyncio.to_thread(started.wait, 2)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertFalse((files_root / cloned_id).exists())

    async def test_duplicate_final_is_deduped_and_open_run_is_retired(self) -> None:
        events = [
            {
                "seq": 1,
                "type": "turn_started",
                "ts": "2026-07-01T10:00:00Z",
                "run_id": "completed-run",
                "prompt": "First",
            },
            {
                "seq": 2,
                "type": "assistant_text",
                "ts": "2026-07-01T10:01:00Z",
                "run_id": "completed-run",
                "text": "Done.",
            },
            {
                "seq": 3,
                "type": "turn_finished",
                "ts": "2026-07-01T10:02:00Z",
                "run_id": "completed-run",
                "result_text": "Done.",
            },
            {
                "seq": 4,
                "type": "turn_started",
                "ts": "2026-07-01T10:03:00Z",
                "run_id": "open-run",
                "prompt": "Second",
            },
            {
                "seq": 5,
                "type": "reasoning_summary",
                "ts": "2026-07-01T10:04:00Z",
                "run_id": "open-run",
                "phase": "commentary",
                "text": "Still working.",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            agent_server,
            "FILES_ROOT",
            Path(temporary),
        ), patch.object(
            agent_server,
            "iter_session_events",
            side_effect=lambda _session_id: iter(events),
        ), patch.object(
            agent_server,
            "append_imported_events",
            new_callable=AsyncMock,
            side_effect=lambda _session_id, imported: len(imported),
        ) as append_imported:
            copied = await agent_server.copy_fork_history("parent", "child")

        imported = append_imported.await_args.args[1]
        self.assertEqual(copied, 6)
        self.assertEqual(
            [event_type for event_type, _payload in imported],
            [
                "turn_started",
                "assistant_text",
                "turn_finished",
                "turn_started",
                "reasoning_summary",
                "turn_stopped",
            ],
        )
        self.assertEqual(imported[2][1]["result_text"], "")
        self.assertTrue(imported[-1][1]["synthetic"])
        self.assertEqual(imported[-1][1]["reason"], "fork_snapshot")

    async def test_import_batch_preserves_history_without_live_metadata(self) -> None:
        session_id = "batch-child"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                json.dumps({
                    "seq": 1,
                    "id": "created",
                    "session_id": session_id,
                    "type": "session_created",
                    "ts": "2026-08-01T00:00:00Z",
                }) + "\n"
            )
            agent_server.EVENT_SEQ_CACHE.pop(session_id, None)
            with patch.object(agent_server, "ensure_dirs"), patch.object(
                agent_server,
                "events_path",
                return_value=path,
            ), patch.object(
                agent_server,
                "update_session_event_metadata",
                new_callable=AsyncMock,
            ) as update_metadata:
                copied = await agent_server.append_imported_events(
                    session_id,
                    [
                        (
                            "turn_started",
                            {
                                "run_id": "old-run",
                                "prompt": "Old prompt",
                                "ts": "2026-07-01T00:00:00Z",
                            },
                        ),
                        (
                            "turn_stopped",
                            {
                                "run_id": "old-run",
                                "ts": "2026-07-01T00:01:00Z",
                            },
                        ),
                    ],
                )

            stored = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(copied, 2)
            self.assertEqual([event["seq"] for event in stored], [1, 2, 3])
            self.assertEqual(
                [event["ts"] for event in stored[1:]],
                ["2026-07-01T00:00:00Z", "2026-07-01T00:01:00Z"],
            )
            update_metadata.assert_not_awaited()
            self.assertEqual(agent_server.EVENT_SEQ_CACHE[session_id], 3)
            agent_server.EVENT_SEQ_CACHE.pop(session_id, None)


class ForkSessionFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_validated_claude_fork_identity_drives_sdk_and_print_fork_flags(
        self,
    ) -> None:
        parent_id = "claude-parent-resumable"
        provider_id = "claude-provider-resumable"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "workspace"
            cwd.mkdir()
            projects_root = root / "claude-projects"
            parent = {
                "id": parent_id,
                "title": "Claude parent",
                "cwd": str(cwd),
                "backend": agent_server.BACKEND_CLAUDE,
                "session_id": provider_id,
                "claude_session_id": provider_id,
                "claude_session_cwd": str(cwd),
            }
            with patch.object(
                agent_server,
                "CLAUDE_PROJECTS_ROOT",
                projects_root,
            ):
                transcript = agent_server.claude_resume_file_for_cwd(
                    provider_id,
                    str(cwd),
                )
                transcript.parent.mkdir(parents=True)
                transcript.write_text("{}\n")
                resolved = agent_server.validated_claude_fork_provider_id(
                    parent,
                    parent_id,
                    str(cwd),
                )
                child = {
                    **parent,
                    "id": "claude-child",
                    "session_id": None,
                    "claude_session_id": None,
                    "fork_from": resolved,
                }
                print_cmd = agent_server.build_claude_cmd(
                    child["id"],
                    child,
                    root / "manifest.json",
                    provider_id=agent_server.resolve_claude_resume_provider(
                        child,
                        str(cwd),
                    )[0],
                )
                captured_options: dict[str, object] = {}

                def capture_options(**kwargs: object) -> dict[str, object]:
                    captured_options.update(kwargs)
                    return kwargs

                with patch.object(
                    agent_server,
                    "claude_sdk_cli_path",
                    return_value="/usr/bin/claude",
                ), patch.object(
                    agent_server,
                    "create_claude_agent_options",
                    side_effect=capture_options,
                ):
                    agent_server.build_claude_sdk_options(
                        child["id"],
                        child,
                        str(cwd),
                        root / "manifest.json",
                    )

        self.assertEqual(resolved, provider_id)
        self.assertEqual(
            print_cmd[print_cmd.index("--resume") + 1],
            provider_id,
        )
        self.assertIn("--fork-session", print_cmd)
        self.assertEqual(captured_options["resume"], provider_id)
        self.assertIs(captured_options["fork_session"], True)

    async def test_claude_fork_missing_transcript_is_rejected_before_child_creation(
        self,
    ) -> None:
        parent_id = "claude-parent-missing-transcript"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "workspace"
            cwd.mkdir()
            projects_root = root / "claude-projects"
            projects_root.mkdir()
            parent = {
                "id": parent_id,
                "title": "Claude parent",
                "cwd": str(cwd),
                "backend": agent_server.BACKEND_CLAUDE,
                "session_id": "claude-provider-missing",
                "claude_session_id": "claude-provider-missing",
                "claude_session_cwd": str(cwd),
            }
            create = AsyncMock()
            with patch.object(
                agent_server.STORE,
                "sessions",
                {parent_id: parent},
            ), patch.object(
                agent_server.STORE,
                "create",
                create,
            ), patch.object(
                agent_server,
                "CLAUDE_PROJECTS_ROOT",
                projects_root,
            ):
                with self.assertRaises(agent_server.HTTPException) as raised:
                    await agent_server._fork_session_locked(
                        parent_id,
                        agent_server.ForkSessionRequest(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("no local transcript", str(raised.exception.detail))
        create.assert_not_awaited()

    async def test_claude_fork_cwd_mismatch_is_rejected_before_child_creation(
        self,
    ) -> None:
        parent_id = "claude-parent-cwd-mismatch"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_cwd = root / "current-workspace"
            current_cwd.mkdir()
            parent = {
                "id": parent_id,
                "title": "Claude parent",
                "cwd": str(current_cwd),
                "backend": agent_server.BACKEND_CLAUDE,
                "session_id": "claude-provider-old-cwd",
                "claude_session_id": "claude-provider-old-cwd",
                "claude_session_cwd": str(root / "old-workspace"),
            }
            create = AsyncMock()
            with patch.object(
                agent_server.STORE,
                "sessions",
                {parent_id: parent},
            ), patch.object(
                agent_server.STORE,
                "create",
                create,
            ):
                with self.assertRaises(agent_server.HTTPException) as raised:
                    await agent_server._fork_session_locked(
                        parent_id,
                        agent_server.ForkSessionRequest(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("original working directory", str(raised.exception.detail))
        self.assertIn("old-workspace", str(raised.exception.detail))
        create.assert_not_awaited()

    async def test_providerless_claude_fork_with_history_is_rejected(self) -> None:
        parent_id = "claude-parent-without-provider"
        parent = {
            "id": parent_id,
            "title": "Claude parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
        }
        create = AsyncMock()
        events = [{"type": "turn_started", "prompt": "Existing question"}]
        with patch.object(
            agent_server.STORE,
            "sessions",
            {parent_id: parent},
        ), patch.object(
            agent_server.STORE,
            "create",
            create,
        ), patch.object(
            agent_server,
            "iter_session_events",
            return_value=iter(events),
        ):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server._fork_session_locked(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("no resumable provider session", str(raised.exception.detail))
        create.assert_not_awaited()

    async def test_cwd_update_waits_for_fork_lifecycle_snapshot(self) -> None:
        session_id = "fork-update-serialization"
        parent = {
            "id": session_id,
            "title": "Parent",
            "cwd": "/old-workspace",
            "backend": agent_server.BACKEND_CODEX,
        }
        fork_started = asyncio.Event()
        release_fork = asyncio.Event()

        async def blocked_fork(
            requested_session_id: str,
            _request: agent_server.ForkSessionRequest,
            **_kwargs,
        ) -> dict:
            self.assertEqual(requested_session_id, session_id)
            self.assertEqual(parent["cwd"], "/old-workspace")
            fork_started.set()
            await release_fork.wait()
            return {"session": dict(parent), "sessions": [dict(parent)]}

        async def apply_update(
            requested_session_id: str,
            patch: dict,
        ) -> dict:
            self.assertEqual(requested_session_id, session_id)
            self.assertTrue(release_fork.is_set())
            parent.update(patch)
            return parent

        update = AsyncMock(side_effect=apply_update)
        with patch.object(agent_server.STORE, "sessions", {session_id: parent}), patch.object(
            agent_server,
            "_fork_session_locked",
            new_callable=AsyncMock,
            side_effect=blocked_fork,
        ), patch.object(
            agent_server.STORE,
            "update",
            update,
        ):
            fork_task = asyncio.create_task(
                agent_server.fork_session(
                    session_id,
                    agent_server.ForkSessionRequest(),
                )
            )
            await asyncio.wait_for(fork_started.wait(), timeout=1)
            update_task = asyncio.create_task(
                agent_server.update_session(
                    session_id,
                    agent_server.UpdateSessionRequest(cwd="/new-workspace"),
                )
            )
            await asyncio.sleep(0)
            update.assert_not_awaited()
            self.assertFalse(update_task.done())

            release_fork.set()
            await asyncio.wait_for(fork_task, timeout=1)
            response = await asyncio.wait_for(update_task, timeout=1)

        update.assert_awaited_once_with(session_id, {"cwd": "/new-workspace"})
        self.assertEqual(response["session"]["cwd"], "/new-workspace")

    def test_all_provider_policy_updates_use_the_lifecycle_lock(self) -> None:
        self.assertEqual(
            agent_server.SESSION_LIFECYCLE_UPDATE_FIELDS,
            frozenset({
                "cwd",
                "backend",
                "model",
                "effort",
                "system_prompt",
                "codex_approval_policy",
                "codex_sandbox_mode",
                "codex_permission_profile",
                "codex_approvals_reviewer",
            }),
        )

    async def test_initializing_fork_is_hidden_and_rejects_turns(self) -> None:
        child_id = "staged-child"
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "_fork_initializing": True,
        }
        with patch.object(agent_server.STORE, "sessions", {child_id: child}), patch.object(
            agent_server.STORE,
            "ensure_sort_orders",
            new_callable=AsyncMock,
        ):
            listed = await agent_server.list_sessions()
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.start_turn(
                    child_id,
                    agent_server.TurnRequest(prompt="Do not run yet"),
                )

        self.assertEqual(listed["sessions"], [])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("initializing", str(raised.exception.detail))

    async def test_completed_fork_publishes_child_atomically(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}

        async def create_child(*_args, **kwargs) -> dict:
            self.assertTrue(kwargs["initializing_fork"])
            sessions[child_id] = child
            return child

        cleanup_state: dict[str, str | None] = {}
        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            side_effect=create_child,
        ), patch.object(
            agent_server.STORE,
            "reorder",
            new_callable=AsyncMock,
            return_value=[parent, child],
        ), patch.object(
            agent_server.STORE,
            "update",
            new_callable=AsyncMock,
            return_value=child,
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
        ) as save, patch.object(
            agent_server,
            "copy_fork_history",
            new_callable=AsyncMock,
            return_value=12,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
            return_value={},
        ):
            result = await agent_server._fork_session_locked(
                parent_id,
                agent_server.ForkSessionRequest(),
                cleanup_state=cleanup_state,
            )

        self.assertNotIn("_fork_initializing", child)
        save.assert_awaited_once()
        self.assertEqual(result["session"]["id"], child_id)
        self.assertEqual(
            {session["id"] for session in result["sessions"]},
            {parent_id, child_id},
        )
        self.assertIsNone(cleanup_state["child_session_id"])
        self.assertIsNone(cleanup_state["provider_thread_id"])

    async def test_parent_fork_audit_is_emitted_only_after_child_publish(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}
        order: list[str] = []

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        async def save_sessions() -> None:
            self.assertNotIn("_fork_initializing", child)
            order.append("publish")

        async def append_fork_event(target_id: str, event_type: str, *_args) -> dict:
            if target_id == parent_id and event_type == "session_forked":
                order.append("parent-audit")
            return {}

        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            side_effect=create_child,
        ), patch.object(
            agent_server.STORE,
            "reorder",
            new_callable=AsyncMock,
            return_value=[parent, child],
        ), patch.object(
            agent_server.STORE,
            "update",
            new_callable=AsyncMock,
            return_value=child,
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
            side_effect=save_sessions,
        ), patch.object(
            agent_server,
            "copy_fork_history",
            new_callable=AsyncMock,
            return_value=4,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
            side_effect=append_fork_event,
        ):
            await agent_server._fork_session_locked(
                parent_id,
                agent_server.ForkSessionRequest(),
            )

        self.assertEqual(order, ["publish", "parent-audit"])

    async def test_failed_child_publish_restores_hidden_marker_and_has_no_parent_audit(
        self,
    ) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CLAUDE,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}
        cleanup_state: dict[str, str | None] = {}

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        append_event = AsyncMock(return_value={})
        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            side_effect=create_child,
        ), patch.object(
            agent_server.STORE,
            "reorder",
            new_callable=AsyncMock,
            return_value=[parent, child],
        ), patch.object(
            agent_server.STORE,
            "update",
            new_callable=AsyncMock,
            return_value=child,
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
            side_effect=OSError("state unavailable"),
        ), patch.object(
            agent_server,
            "copy_fork_history",
            new_callable=AsyncMock,
            return_value=4,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ):
            with self.assertRaisesRegex(OSError, "state unavailable"):
                await agent_server._fork_session_locked(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                    cleanup_state=cleanup_state,
                )

        self.assertTrue(child["_fork_initializing"])
        self.assertEqual(cleanup_state["child_session_id"], child_id)
        self.assertFalse(any(
            call.args[:2] == (parent_id, "session_forked")
            for call in append_event.await_args_list
        ))

    async def test_native_fork_transfers_cleanup_ownership_before_bind(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        provider_id = "thread-child"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": None,
            "codex_thread_id": None,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}
        order: list[str] = []
        cleanup_state: dict[str, str | None] = {}

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        async def fork_provider(*_args, **_kwargs) -> str:
            self.assertTrue(
                await agent_server.persist_abandoned_fork_provider_thread(
                    provider_id
                )
            )
            order.append("journal")
            return provider_id

        original_forget = agent_server.forget_abandoned_fork_provider_thread

        async def forget_provider(thread_id: str) -> bool:
            self.assertEqual(thread_id, provider_id)
            self.assertEqual(child["codex_thread_id"], provider_id)
            self.assertTrue(child["_fork_initializing"])
            order.append("transfer")
            return await original_forget(thread_id)

        async def bind_provider(*_args, **_kwargs) -> tuple[str, str]:
            self.assertNotIn(
                provider_id,
                agent_server.ABANDONED_FORK_PROVIDER_THREADS,
            )
            self.assertEqual(child["codex_thread_id"], provider_id)
            order.append("bind")
            child["session_id"] = provider_id
            return provider_id, "policy-hash"

        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-forks.json"
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            with patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server.STORE,
                "sessions",
                sessions,
            ), patch.object(
                agent_server.STORE,
                "_lock",
                asyncio.Lock(),
            ), patch.object(
                agent_server.STORE,
                "create",
                new_callable=AsyncMock,
                side_effect=create_child,
            ), patch.object(
                agent_server.STORE,
                "reorder",
                new_callable=AsyncMock,
                return_value=[parent, child],
            ), patch.object(
                agent_server.STORE,
                "update",
                new_callable=AsyncMock,
                return_value=child,
            ), patch.object(
                agent_server.STORE,
                "save",
                new_callable=AsyncMock,
            ), patch.object(
                agent_server,
                "fork_codex_thread",
                new_callable=AsyncMock,
                side_effect=fork_provider,
            ), patch.object(
                agent_server,
                "forget_abandoned_fork_provider_thread",
                new_callable=AsyncMock,
                side_effect=forget_provider,
            ), patch.object(
                agent_server,
                "bind_forked_codex_thread",
                new_callable=AsyncMock,
                side_effect=bind_provider,
            ), patch.object(
                agent_server,
                "copy_fork_history",
                new_callable=AsyncMock,
                return_value=3,
            ), patch.object(
                agent_server,
                "append_event",
                new_callable=AsyncMock,
                return_value={},
            ):
                result = await agent_server._fork_session_locked(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                    cleanup_state=cleanup_state,
                )
            ledger_ids = json.loads(cleanup_ledger.read_text())["thread_ids"]

        self.assertEqual(order, ["journal", "transfer", "bind"])
        self.assertEqual(ledger_ids, [])
        self.assertNotIn("_fork_initializing", child)
        self.assertEqual(result["session"]["id"], child_id)
        self.assertIsNone(cleanup_state["provider_thread_id"])
        self.assertIsNone(cleanup_state["child_session_id"])

    async def test_failed_native_bind_is_journaled_before_memory_fallback(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        provider_id = "thread-child"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": None,
            "codex_thread_id": None,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        async def fork_provider(*_args, **_kwargs) -> str:
            self.assertTrue(
                await agent_server.persist_abandoned_fork_provider_thread(
                    provider_id
                )
            )
            return provider_id

        async def update_child(*_args, **_kwargs) -> dict:
            return child

        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-forks.json"
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            with patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server.STORE,
                "sessions",
                sessions,
            ), patch.object(
                agent_server.STORE,
                "_lock",
                asyncio.Lock(),
            ), patch.object(
                agent_server.STORE,
                "create",
                new_callable=AsyncMock,
                side_effect=create_child,
            ), patch.object(
                agent_server.STORE,
                "reorder",
                new_callable=AsyncMock,
                return_value=[parent, child],
            ), patch.object(
                agent_server.STORE,
                "update",
                new_callable=AsyncMock,
                side_effect=update_child,
            ), patch.object(
                agent_server.STORE,
                "save",
                new_callable=AsyncMock,
            ), patch.object(
                agent_server,
                "fork_codex_thread",
                new_callable=AsyncMock,
                side_effect=fork_provider,
            ), patch.object(
                agent_server,
                "bind_forked_codex_thread",
                new_callable=AsyncMock,
                side_effect=RuntimeError("policy bind failed"),
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                return_value=False,
            ), patch.object(
                agent_server,
                "build_fork_memory",
                return_value="bounded parent memory",
            ), patch.object(
                agent_server,
                "copy_fork_history",
                new_callable=AsyncMock,
                return_value=8,
            ), patch.object(
                agent_server,
                "append_event",
                new_callable=AsyncMock,
                return_value={},
            ):
                result = await agent_server._fork_session_locked(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )
            ledger_ids = json.loads(cleanup_ledger.read_text())["thread_ids"]

        self.assertEqual(ledger_ids, [provider_id])
        self.assertEqual(
            agent_server.ABANDONED_FORK_PROVIDER_THREADS,
            {provider_id},
        )
        self.assertIsNone(child["session_id"])
        self.assertIsNone(child["codex_thread_id"])
        self.assertTrue(child["memory_forked"])
        self.assertEqual(child["memory_seed"], "bounded parent memory")
        self.assertNotIn("_fork_initializing", child)
        self.assertEqual(result["session"]["id"], child_id)

    async def test_load_discards_abandoned_staged_fork(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions_file = root / "sessions.json"
            sessions_file.write_text(json.dumps({
                "normal": {
                    "id": "normal",
                    "title": "Normal",
                    "cwd": "/tmp",
                    "backend": agent_server.BACKEND_CODEX,
                },
                "staged": {
                    "id": "staged",
                    "title": "Incomplete fork",
                    "cwd": "/tmp",
                    "backend": agent_server.BACKEND_CODEX,
                    "codex_thread_id": "thread-staged",
                    "_fork_initializing": True,
                },
            }))
            cleanup_ledger = root / "abandoned-fork-threads.json"
            store = agent_server.SessionStore()
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            with patch.object(agent_server, "SESSIONS_FILE", sessions_file), patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server,
                "ensure_dirs",
            ), patch.object(
                agent_server,
                "session_dir",
                side_effect=lambda session_id: root / session_id,
            ), patch.object(
                agent_server,
                "delete_session_owned_file_records",
                return_value=0,
            ) as delete_files, patch.object(
                agent_server,
                "forget_event_seq",
                new_callable=AsyncMock,
            ), patch.object(
                agent_server,
                "rebuild_codex_subagent_indexes",
            ), patch.object(
                store,
                "save",
                new_callable=AsyncMock,
            ):
                await store.load()
            cleanup_thread_ids = json.loads(cleanup_ledger.read_text())[
                "thread_ids"
            ]

        self.assertEqual(set(store.sessions), {"normal"})
        delete_files.assert_called_once_with("staged")
        self.assertEqual(cleanup_thread_ids, ["thread-staged"])
        self.assertEqual(
            agent_server.ABANDONED_FORK_PROVIDER_THREADS,
            {"thread-staged"},
        )

    async def test_abandoned_provider_cleanup_is_durable_and_never_deletes_owned_thread(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-fork-threads.json"
            pending = {
                "thread-orphan",
                "thread-owned",
                "thread-parked",
                "thread-retry",
            }
            agent_server.write_abandoned_fork_thread_ids(pending, cleanup_ledger)
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.update(pending)
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            sessions = {
                "real-chat": {
                    "id": "real-chat",
                    "backend": agent_server.BACKEND_CODEX,
                    "codex_thread_id": "thread-owned",
                },
                "parked-chat": {
                    "id": "parked-chat",
                    "backend": agent_server.BACKEND_CLAUDE,
                    "claude_session_id": "claude-active",
                    "session_id": "claude-active",
                    "codex_thread_id": "thread-parked",
                }
            }

            async def retire(thread_id: str) -> bool:
                return thread_id == "thread-orphan"

            with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                side_effect=retire,
            ) as retire_fork:
                await agent_server.cleanup_abandoned_fork_provider_threads()

            self.assertEqual(
                {call.args[0] for call in retire_fork.await_args_list},
                {"thread-orphan", "thread-retry"},
            )
            self.assertEqual(
                json.loads(cleanup_ledger.read_text())["thread_ids"],
                ["thread-retry"],
            )
            self.assertEqual(
                agent_server.ABANDONED_FORK_PROVIDER_THREADS,
                {"thread-retry"},
            )

    async def test_startup_cleanup_does_not_consume_new_inflight_fork(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-fork-threads.json"
            pending = {"thread-startup", "thread-new-fork"}
            agent_server.write_abandoned_fork_thread_ids(pending, cleanup_ledger)
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.update(pending)
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            with patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server.STORE,
                "sessions",
                {},
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                return_value=True,
            ) as retire:
                await agent_server.cleanup_abandoned_fork_provider_threads(
                    {"thread-startup"}
                )

            retire.assert_awaited_once_with("thread-startup")
            self.assertEqual(
                agent_server.ABANDONED_FORK_PROVIDER_THREADS,
                {"thread-new-fork"},
            )
            self.assertEqual(
                json.loads(cleanup_ledger.read_text())["thread_ids"],
                ["thread-new-fork"],
            )

    async def test_pending_orphan_cleanup_fences_codex_thread_reattachment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-fork-threads.json"
            pending = {"thread-orphan"}
            agent_server.write_abandoned_fork_thread_ids(pending, cleanup_ledger)
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.update(pending)
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            cleanup_started = asyncio.Event()
            finish_cleanup = asyncio.Event()
            store = agent_server.SessionStore()

            async def retire(_thread_id: str) -> bool:
                cleanup_started.set()
                await finish_cleanup.wait()
                return True

            with patch.object(agent_server.STORE, "sessions", {}), patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                side_effect=retire,
            ), patch.object(
                agent_server,
                "ensure_dirs",
            ) as ensure_dirs:
                cleanup_task = asyncio.create_task(
                    agent_server.cleanup_abandoned_fork_provider_threads()
                )
                try:
                    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
                    with self.assertRaises(agent_server.HTTPException) as raised:
                        await store.create(
                            agent_server.CreateSessionRequest(
                                backend=agent_server.BACKEND_CODEX,
                                codex_thread_id="thread-orphan",
                            )
                        )
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertIn("interrupted fork", str(raised.exception.detail))
                    ensure_dirs.assert_not_called()

                    with self.assertRaises(agent_server.HTTPException) as parked_raised:
                        await store.create(
                            agent_server.CreateSessionRequest(
                                backend=agent_server.BACKEND_CLAUDE,
                                claude_session_id="claude-active",
                                codex_thread_id="thread-orphan",
                            )
                        )
                    self.assertEqual(parked_raised.exception.status_code, 409)
                    ensure_dirs.assert_not_called()

                    with self.assertRaises(agent_server.HTTPException) as save_raised:
                        await store.save_provider_session(
                            "some-chat",
                            "thread-orphan",
                            agent_server.BACKEND_CODEX,
                        )
                    self.assertEqual(save_raised.exception.status_code, 409)
                finally:
                    finish_cleanup.set()
                    await cleanup_task

            self.assertEqual(agent_server.ABANDONED_FORK_PROVIDER_THREADS, set())
            self.assertEqual(
                json.loads(cleanup_ledger.read_text())["thread_ids"],
                [],
            )

    async def test_staged_fork_is_retained_when_cleanup_ledger_cannot_persist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions_file = root / "sessions.json"
            sessions_file.write_text(json.dumps({
                "staged": {
                    "id": "staged",
                    "cwd": "/tmp",
                    "backend": agent_server.BACKEND_CODEX,
                    "codex_thread_id": "thread-staged",
                    "_fork_initializing": True,
                    "sort_order": 1000,
                }
            }))
            store = agent_server.SessionStore()
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
            with patch.object(agent_server, "SESSIONS_FILE", sessions_file), patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                root / "abandoned-fork-threads.json",
            ), patch.object(
                agent_server,
                "write_abandoned_fork_thread_ids",
                side_effect=OSError("read-only state"),
            ), patch.object(
                agent_server,
                "ensure_dirs",
            ), patch.object(
                agent_server,
                "delete_session_owned_file_records",
            ) as delete_files, patch.object(
                agent_server,
                "rebuild_codex_subagent_indexes",
            ), patch.object(
                store,
                "save",
                new_callable=AsyncMock,
            ):
                await store.load()

        self.assertIn("staged", store.sessions)
        delete_files.assert_not_called()
        self.assertEqual(agent_server.ABANDONED_FORK_PROVIDER_THREADS, set())

    async def test_cancelled_session_created_append_rolls_back_child_and_provider(
        self,
    ) -> None:
        parent_id = "parent-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        sessions = {parent_id: parent}

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            agent_server.STORE,
            "sessions",
            sessions,
        ), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "ensure_dirs",
        ), patch.object(
            agent_server,
            "session_dir",
            return_value=Path(temporary) / "child-session",
        ), patch.object(
            agent_server,
            "delete_session_owned_file_records",
        ), patch.object(
            agent_server,
            "forget_event_seq",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
            return_value="thread-child",
        ), patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=True,
        ) as retire:
            with self.assertRaises(asyncio.CancelledError):
                await agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )

        self.assertEqual(sessions, {parent_id: parent})
        retire.assert_awaited_once_with("thread-child")

    async def test_session_create_save_failure_rolls_back_in_memory_record(self) -> None:
        store = agent_server.SessionStore()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            store,
            "save",
            new_callable=AsyncMock,
            side_effect=[OSError("disk full"), None],
        ) as save, patch.object(
            agent_server,
            "ensure_dirs",
        ), patch.object(
            agent_server,
            "session_dir",
            return_value=Path(temporary) / "new-session",
        ), patch.object(
            agent_server,
            "delete_session_owned_file_records",
        ), patch.object(
            agent_server,
            "forget_event_seq",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
        ) as append_event:
            with self.assertRaisesRegex(OSError, "disk full"):
                await store.create(
                    agent_server.CreateSessionRequest(
                        title="Failed child",
                        cwd="/tmp",
                        backend=agent_server.BACKEND_CODEX,
                    )
                )

        self.assertEqual(store.sessions, {})
        self.assertEqual(save.await_count, 2)
        append_event.assert_not_awaited()

    async def test_history_copy_infrastructure_failure_aborts_entire_fork(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        self.addCleanup(
            agent_server.DELETED_SESSION_TOMBSTONES.discard,
            child_id,
        )
        self.addCleanup(agent_server.DELETING_SESSIONS.discard, child_id)
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        async def delete_child(session_id: str) -> bool:
            return sessions.pop(session_id, None) is not None

        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            side_effect=create_child,
        ), patch.object(
            agent_server.STORE,
            "delete",
            new_callable=AsyncMock,
            side_effect=delete_child,
        ) as delete_session, patch.object(
            agent_server.STORE,
            "reorder",
            new_callable=AsyncMock,
            return_value=[parent, child],
        ), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
            return_value="thread-child",
        ), patch.object(
            agent_server,
            "bind_forked_codex_thread",
            new_callable=AsyncMock,
            return_value=("thread-child", "policy-hash"),
        ), patch.object(
            agent_server,
            "copy_fork_history",
            new_callable=AsyncMock,
            side_effect=RuntimeError("history storage failed"),
        ), patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=True,
        ) as retire:
            with self.assertRaisesRegex(RuntimeError, "history storage failed"):
                await agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )

        self.assertEqual(sessions, {parent_id: parent})
        delete_session.assert_awaited_once_with(child_id)
        retire.assert_awaited_once_with("thread-child")

    async def test_aborted_fork_keeps_local_reference_until_provider_is_retired(
        self,
    ) -> None:
        child_delete_started = asyncio.Event()
        provider_delete_started = asyncio.Event()
        release_provider = asyncio.Event()
        deleting_sessions: set[str] = set()
        deleted_tombstones: set[str] = set()

        async def delete_child(_session_id: str) -> bool:
            child_delete_started.set()
            return True

        async def retire_provider(_thread_id: str) -> bool:
            provider_delete_started.set()
            await release_provider.wait()
            return True

        with patch.object(
            agent_server,
            "DELETING_SESSIONS",
            deleting_sessions,
        ), patch.object(
            agent_server,
            "DELETED_SESSION_TOMBSTONES",
            deleted_tombstones,
        ), patch.object(
            agent_server.STORE,
            "delete",
            new_callable=AsyncMock,
            side_effect=delete_child,
        ), patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            side_effect=retire_provider,
        ):
            cleanup = asyncio.create_task(
                agent_server.cleanup_aborted_session_fork({
                    "child_session_id": "child-chat",
                    "provider_thread_id": "thread-child",
                })
            )
            await asyncio.wait_for(provider_delete_started.wait(), timeout=1)
            self.assertFalse(child_delete_started.is_set())
            self.assertFalse(cleanup.done())
            release_provider.set()
            await cleanup
            self.assertTrue(child_delete_started.is_set())

    async def test_failed_aborted_fork_retirement_is_ledgered_before_local_delete(
        self,
    ) -> None:
        child_id = "child-chat"
        thread_id = "thread-child"
        self.addCleanup(agent_server.DELETING_SESSIONS.discard, child_id)
        self.addCleanup(agent_server.DELETED_SESSION_TOMBSTONES.discard, child_id)
        sessions = {
            child_id: {
                "id": child_id,
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": thread_id,
                "_fork_initializing": True,
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            cleanup_ledger = Path(temporary) / "abandoned-fork-threads.json"
            agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
            self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)

            async def delete_child(session_id: str) -> bool:
                return sessions.pop(session_id, None) is not None

            with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
                agent_server.STORE,
                "delete",
                new_callable=AsyncMock,
                side_effect=delete_child,
            ) as delete_session, patch.object(
                agent_server,
                "ABANDONED_FORK_THREADS_FILE",
                cleanup_ledger,
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                return_value=False,
            ):
                await agent_server.cleanup_aborted_session_fork({
                    "child_session_id": child_id,
                    "provider_thread_id": thread_id,
                })

            delete_session.assert_awaited_once_with(child_id)
            self.assertEqual(sessions, {})
            self.assertEqual(
                agent_server.ABANDONED_FORK_PROVIDER_THREADS,
                {thread_id},
            )
            self.assertEqual(
                json.loads(cleanup_ledger.read_text())["thread_ids"],
                [thread_id],
            )

    async def test_staged_child_survives_if_failed_retirement_cannot_be_ledgered(
        self,
    ) -> None:
        child_id = "child-chat"
        thread_id = "thread-child"
        self.addCleanup(agent_server.DELETING_SESSIONS.discard, child_id)
        self.addCleanup(agent_server.DELETED_SESSION_TOMBSTONES.discard, child_id)
        sessions = {
            child_id: {
                "id": child_id,
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": thread_id,
                "_fork_initializing": True,
            }
        }
        agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
        self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)
        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "delete",
            new_callable=AsyncMock,
        ) as delete_session, patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=False,
        ), patch.object(
            agent_server,
            "write_abandoned_fork_thread_ids",
            side_effect=OSError("read-only state"),
        ):
            await agent_server.cleanup_aborted_session_fork({
                "child_session_id": child_id,
                "provider_thread_id": thread_id,
            })

        delete_session.assert_not_awaited()
        self.assertIn(child_id, sessions)
        self.assertTrue(sessions[child_id]["_fork_initializing"])
        self.assertIn(thread_id, agent_server.ABANDONED_FORK_PROVIDER_THREADS)

    async def test_cancelled_bound_fork_fences_late_provider_notification(
        self,
    ) -> None:
        parent_id = "parent-chat"
        child_id = "staged-child"
        provider_id = "thread-child"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": None,
            "codex_thread_id": None,
            "_fork_initializing": True,
        }
        sessions = {parent_id: parent}
        history_started = asyncio.Event()
        late_append_results: list[dict] = []
        deleting_sessions: set[str] = set()
        deleted_tombstones: set[str] = set()
        thread_index: dict[str, str] = {}

        async def create_child(*_args, **_kwargs) -> dict:
            sessions[child_id] = child
            return child

        async def bind_child(*_args, **_kwargs) -> tuple[str, str]:
            child["session_id"] = provider_id
            child["codex_thread_id"] = provider_id
            thread_index[provider_id] = child_id
            return provider_id, "policy-hash"

        async def block_history(*_args, **_kwargs) -> int:
            history_started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_root = root / child_id
            child_root.mkdir()
            (child_root / "events.jsonl").write_text("staged\n")

            def ensure_test_dirs(session_id: str | None = None) -> None:
                if session_id:
                    (root / session_id).mkdir(parents=True, exist_ok=True)

            async def delete_child(session_id: str) -> bool:
                # This coroutine starts only after cleanup has yielded. The
                # routing/filesystem fence must already be fully installed.
                self.assertIn(child_id, deleting_sessions)
                self.assertIn(child_id, deleted_tombstones)
                self.assertNotIn(provider_id, thread_index)
                shutil.rmtree(child_root)

                await agent_server.project_codex_notification({
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": provider_id,
                        "status": {"type": "idle"},
                    },
                })
                late_append_results.append(await agent_server.append_event(
                    child_id,
                    "codex_thread_status",
                    {"status": {"type": "idle"}},
                ))
                sessions.pop(session_id, None)
                return True

            with patch.object(
                agent_server,
                "DELETING_SESSIONS",
                deleting_sessions,
            ), patch.object(
                agent_server,
                "DELETED_SESSION_TOMBSTONES",
                deleted_tombstones,
            ), patch.object(
                agent_server,
                "CODEX_THREAD_SESSION_INDEX",
                thread_index,
            ), patch.object(
                agent_server.STORE,
                "sessions",
                sessions,
            ), patch.object(
                agent_server.STORE,
                "_lock",
                asyncio.Lock(),
            ), patch.object(
                agent_server.STORE,
                "save",
                new_callable=AsyncMock,
            ), patch.object(
                agent_server.STORE,
                "create",
                new_callable=AsyncMock,
                side_effect=create_child,
            ), patch.object(
                agent_server.STORE,
                "delete",
                new_callable=AsyncMock,
                side_effect=delete_child,
            ) as delete_session, patch.object(
                agent_server.STORE,
                "reorder",
                new_callable=AsyncMock,
                return_value=[parent, child],
            ), patch.object(
                agent_server,
                "fork_codex_thread",
                new_callable=AsyncMock,
                return_value=provider_id,
            ), patch.object(
                agent_server,
                "bind_forked_codex_thread",
                new_callable=AsyncMock,
                side_effect=bind_child,
            ), patch.object(
                agent_server,
                "copy_fork_history",
                new_callable=AsyncMock,
                side_effect=block_history,
            ), patch.object(
                agent_server,
                "retire_failed_codex_fork",
                new_callable=AsyncMock,
                return_value=True,
            ) as retire, patch.object(
                agent_server,
                "ensure_dirs",
                side_effect=ensure_test_dirs,
            ), patch.object(
                agent_server,
                "events_path",
                side_effect=lambda session_id: root / session_id / "events.jsonl",
            ):
                task = asyncio.create_task(agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                ))
                await asyncio.wait_for(history_started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)

            self.assertEqual(sessions, {parent_id: parent})
            delete_session.assert_awaited_once_with(child_id)
            retire.assert_awaited_once_with(provider_id)
            self.assertEqual(len(late_append_results), 1)
            self.assertTrue(late_append_results[0]["discarded"])
            self.assertFalse(child_root.exists())
            self.assertNotIn("codex_thread_status", child)
            self.assertNotIn(child_id, deleting_sessions)
            self.assertIn(child_id, deleted_tombstones)
            self.assertNotIn(provider_id, thread_index)

    async def test_missing_parent_codex_thread_uses_bounded_memory_fallback(self) -> None:
        parent_id = "parent-chat"
        child_id = "child-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": None,
            "session_id": None,
            "pinned": False,
            "archived": False,
        }
        child = {
            "id": child_id,
            "title": "Fork of Parent",
            "folder": "General",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": None,
            "session_id": None,
            "parent_id": parent_id,
            "pinned": False,
            "archived": False,
        }
        sessions = {parent_id: parent, child_id: child}

        async def update_child(session_id: str, _patch: dict) -> dict:
            self.assertEqual(session_id, child_id)
            child["updated_at"] = "2026-08-03T01:00:00Z"
            return child

        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            return_value=child,
        ), patch.object(
            agent_server.STORE,
            "reorder",
            new_callable=AsyncMock,
            return_value=[parent, child],
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server.STORE,
            "update",
            new_callable=AsyncMock,
            side_effect=update_child,
        ), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
        ) as fork_codex_thread, patch.object(
            agent_server,
            "build_fork_memory",
            return_value="bounded parent memory",
        ) as build_fork_memory, patch.object(
            agent_server,
            "copy_fork_history",
            new_callable=AsyncMock,
            return_value=17,
        ), patch.object(
            agent_server,
            "append_event",
            new_callable=AsyncMock,
            return_value={},
        ) as append_event:
            result = await agent_server.fork_session(
                parent_id,
                agent_server.ForkSessionRequest(),
            )

        fork_codex_thread.assert_not_awaited()
        build_fork_memory.assert_called_once()
        self.assertEqual(child["memory_seed"], "bounded parent memory")
        self.assertFalse(child["memory_seed_used"])
        self.assertTrue(child["memory_forked"])
        imported = [
            call for call in append_event.await_args_list
            if call.args[0] == child_id and call.args[1] == "history_imported"
        ]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].args[2]["copied_events"], 17)
        self.assertIn("bounded rough history", imported[0].args[2]["message"])
        self.assertEqual(result["session"]["id"], child_id)

    async def test_active_parent_is_rejected_before_provider_fork(self) -> None:
        parent_id = "busy-parent"
        parent = {
            "id": parent_id,
            "title": "Busy",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        with patch.object(agent_server.STORE, "sessions", {parent_id: parent}), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
        ) as fork_codex_thread:
            agent_server.BUSY_SESSIONS.add(parent_id)
            try:
                with self.assertRaises(agent_server.HTTPException) as raised:
                    await agent_server.fork_session(
                        parent_id,
                        agent_server.ForkSessionRequest(),
                    )
            finally:
                agent_server.BUSY_SESSIONS.discard(parent_id)

        self.assertEqual(raised.exception.status_code, 409)
        fork_codex_thread.assert_not_awaited()

    async def test_invalid_parent_cwd_is_rejected_before_provider_fork(self) -> None:
        parent_id = "bad-cwd-parent"
        parent = {
            "id": parent_id,
            "title": "Bad cwd",
            "cwd": "relative/path",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        with patch.object(agent_server.STORE, "sessions", {parent_id: parent}), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
        ) as fork_codex_thread:
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        fork_codex_thread.assert_not_awaited()

    async def test_cancellation_after_provider_fork_retires_unexposed_child(self) -> None:
        parent_id = "parent-chat"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        create_started = asyncio.Event()

        async def blocked_create(*_args, **_kwargs) -> dict:
            create_started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        with patch.object(agent_server.STORE, "sessions", {parent_id: parent}), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
            side_effect=blocked_create,
        ), patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
            return_value="thread-child",
        ), patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=True,
        ) as retire:
            task = asyncio.create_task(
                agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )
            )
            await asyncio.wait_for(create_started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        retire.assert_awaited_once_with("thread-child")

    async def test_unrecoverable_provider_fork_fails_closed_and_retries_cleanup(
        self,
    ) -> None:
        parent_id = "parent-chat"
        provider_id = "thread-child"
        parent = {
            "id": parent_id,
            "title": "Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread-parent",
        }
        with patch.object(
            agent_server.STORE,
            "sessions",
            {parent_id: parent},
        ), patch.object(
            agent_server.STORE,
            "create",
            new_callable=AsyncMock,
        ) as create, patch.object(
            agent_server,
            "fork_codex_thread",
            new_callable=AsyncMock,
            side_effect=agent_server.CodexForkCleanupError(provider_id),
        ), patch.object(
            agent_server,
            "retire_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=True,
        ) as retire:
            with self.assertRaises(agent_server.CodexForkCleanupError):
                await agent_server.fork_session(
                    parent_id,
                    agent_server.ForkSessionRequest(),
                )

        create.assert_not_awaited()
        retire.assert_awaited_once_with(provider_id)


class NativeCodexForkSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._cleanup_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_directory.cleanup)
        self._cleanup_ledger_patch = patch.object(
            agent_server,
            "ABANDONED_FORK_THREADS_FILE",
            Path(self._cleanup_directory.name) / "abandoned-forks.json",
        )
        self._cleanup_ledger_patch.start()
        self.addCleanup(self._cleanup_ledger_patch.stop)
        agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear()
        self.addCleanup(agent_server.ABANDONED_FORK_PROVIDER_THREADS.clear)

    async def test_retire_failed_fork_clears_route_before_first_await(self) -> None:
        thread_id = "thread-child"
        thread_index = {thread_id: "staged-child"}
        manager = Mock()
        manager.delete_thread = AsyncMock()

        async def get_manager() -> Mock:
            self.assertNotIn(thread_id, thread_index)
            return manager

        with patch.object(
            agent_server,
            "CODEX_THREAD_SESSION_INDEX",
            thread_index,
        ), patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            side_effect=get_manager,
        ):
            await agent_server.retire_failed_codex_fork(thread_id)

        manager.delete_thread.assert_awaited_once_with(thread_id)

    async def test_provider_fork_defers_inherited_goal_continuation(self) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock(return_value={
            "id": "thread-child",
            "forkedFromId": "thread-parent",
            "cwd": "/tmp",
        })
        manager.delete_thread = AsyncMock()
        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "touch_codex_app_server_thread",
            new_callable=AsyncMock,
        ):
            result = await agent_server.fork_codex_thread(
                "thread-parent",
                {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
            )

        self.assertEqual(result, "thread-child")
        params = manager.fork_thread.await_args.args[1]
        self.assertTrue(params["deferGoalContinuation"])
        self.assertFalse(params["ephemeral"])
        self.assertTrue(params["excludeTurns"])
        manager.read_thread.assert_awaited_once_with(
            "thread-child",
            include_turns=False,
        )
        manager.delete_thread.assert_not_awaited()
        self.assertEqual(
            agent_server.ABANDONED_FORK_PROVIDER_THREADS,
            {"thread-child"},
        )

    async def test_provider_fork_is_journaled_before_verification(self) -> None:
        order: list[str] = []
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")

        async def read_thread(*_args, **_kwargs) -> dict:
            self.assertEqual(order, ["journal"])
            order.append("verify")
            return {
                "id": "thread-child",
                "forkedFromId": "thread-parent",
                "cwd": "/tmp",
            }

        manager.read_thread = AsyncMock(side_effect=read_thread)

        async def persist_thread(thread_id: str) -> bool:
            self.assertEqual(thread_id, "thread-child")
            order.append("journal")
            return True

        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            side_effect=persist_thread,
        ), patch.object(
            agent_server,
            "touch_codex_app_server_thread",
            new_callable=AsyncMock,
        ):
            result = await agent_server.fork_codex_thread(
                "thread-parent",
                {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
            )

        self.assertEqual(result, "thread-child")
        self.assertEqual(order, ["journal", "verify"])

    async def test_late_provider_id_on_timeout_is_journaled_before_propagation(
        self,
    ) -> None:
        manager = Mock()
        timeout = asyncio.TimeoutError("thread/fork timed out")
        timeout.unretired_fork_thread_ids = ("thread-late",)
        manager.fork_thread = AsyncMock(side_effect=timeout)
        persist = AsyncMock(return_value=True)

        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            persist,
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            new_callable=AsyncMock,
        ) as cleanup:
            with self.assertRaises(asyncio.TimeoutError) as raised:
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        self.assertIs(raised.exception, timeout)
        persist.assert_awaited_once_with("thread-late")
        cleanup.assert_not_awaited()

    async def test_late_provider_id_on_cancellation_is_journaled_before_propagation(
        self,
    ) -> None:
        manager = Mock()
        cancellation = asyncio.CancelledError()
        cancellation.unretired_fork_thread_ids = ("thread-late",)
        manager.fork_thread = AsyncMock(side_effect=cancellation)
        persist = AsyncMock(return_value=True)

        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            persist,
        ):
            with self.assertRaises(asyncio.CancelledError) as raised:
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        self.assertIs(raised.exception, cancellation)
        persist.assert_awaited_once_with("thread-late")

    async def test_unowned_late_provider_id_fails_closed_with_exact_id(self) -> None:
        manager = Mock()
        timeout = asyncio.TimeoutError("thread/fork timed out")
        timeout.unretired_fork_thread_ids = ("thread-late",)
        manager.fork_thread = AsyncMock(side_effect=timeout)

        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            return_value=False,
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with self.assertRaises(agent_server.CodexForkCleanupError) as raised:
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        self.assertEqual(raised.exception.thread_id, "thread-late")

    async def test_unjournaled_undeletable_provider_fork_reports_its_id(
        self,
    ) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock()
        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            return_value=False,
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with self.assertRaises(agent_server.CodexForkCleanupError) as raised:
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        self.assertEqual(raised.exception.thread_id, "thread-child")
        manager.read_thread.assert_not_awaited()

    async def test_journal_error_with_failed_cleanup_reports_provider_id(
        self,
    ) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock()
        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            side_effect=OSError("state unavailable"),
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with self.assertRaises(agent_server.CodexForkCleanupError) as raised:
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        self.assertEqual(raised.exception.thread_id, "thread-child")
        manager.read_thread.assert_not_awaited()

    async def test_cancelled_journal_is_joined_before_provider_cleanup(self) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock()
        journal_started = asyncio.Event()
        release_journal = asyncio.Event()

        async def persist_thread(_thread_id: str) -> bool:
            journal_started.set()
            await release_journal.wait()
            return True

        cleanup = AsyncMock(return_value=True)
        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            side_effect=persist_thread,
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            cleanup,
        ):
            task = asyncio.create_task(agent_server.fork_codex_thread(
                "thread-parent",
                {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
            ))
            await asyncio.wait_for(journal_started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_journal.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        cleanup.assert_awaited_once_with("thread-child")
        manager.read_thread.assert_not_awaited()

    async def test_cancelled_failed_journal_never_loses_provider_id(self) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock()
        journal_started = asyncio.Event()
        release_journal = asyncio.Event()

        async def persist_thread(_thread_id: str) -> bool:
            journal_started.set()
            await release_journal.wait()
            raise OSError("state unavailable")

        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "persist_abandoned_fork_provider_thread",
            new_callable=AsyncMock,
            side_effect=persist_thread,
        ), patch.object(
            agent_server,
            "retire_or_record_failed_codex_fork",
            new_callable=AsyncMock,
            return_value=False,
        ) as cleanup:
            task = asyncio.create_task(agent_server.fork_codex_thread(
                "thread-parent",
                {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
            ))
            await asyncio.wait_for(journal_started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_journal.set()
            with self.assertRaises(agent_server.CodexForkCleanupError) as raised:
                await asyncio.wait_for(task, timeout=1)

        self.assertEqual(raised.exception.thread_id, "thread-child")
        cleanup.assert_awaited_once_with("thread-child")
        manager.read_thread.assert_not_awaited()

    async def test_unverifiable_provider_fork_is_deleted(self) -> None:
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock(return_value={
            "id": "thread-child",
            "forkedFromId": None,
            "cwd": "/tmp",
        })
        manager.delete_thread = AsyncMock()
        manager.is_thread_loaded.return_value = True
        manager.unsubscribe_thread = AsyncMock()
        with patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ):
            with self.assertRaises(agent_server.CodexAppServerProtocolError):
                await agent_server.fork_codex_thread(
                    "thread-parent",
                    {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                )

        manager.delete_thread.assert_awaited_once_with("thread-child")
        manager.unsubscribe_thread.assert_not_awaited()

    async def test_missing_or_mismatched_provider_cwd_is_deleted(self) -> None:
        for returned_cwd in (None, "/different/workspace"):
            with self.subTest(returned_cwd=returned_cwd):
                manager = Mock()
                manager.fork_thread = AsyncMock(return_value="thread-child")
                manager.read_thread = AsyncMock(return_value={
                    "id": "thread-child",
                    "forkedFromId": "thread-parent",
                    "cwd": returned_cwd,
                })
                manager.delete_thread = AsyncMock()
                manager.is_thread_loaded.return_value = True
                manager.unsubscribe_thread = AsyncMock()
                with patch.object(
                    agent_server,
                    "codex_app_server_manager",
                    new_callable=AsyncMock,
                    return_value=manager,
                ):
                    with self.assertRaises(agent_server.CodexAppServerProtocolError):
                        await agent_server.fork_codex_thread(
                            "thread-parent",
                            {"cwd": "/tmp", "backend": agent_server.BACKEND_CODEX},
                        )

                manager.delete_thread.assert_awaited_once_with("thread-child")

    async def test_bind_pauses_inherited_active_goal_before_return(self) -> None:
        session_id = "child-chat"
        thread_id = "thread-child"
        child = {
            "id": session_id,
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": None,
            "codex_thread_id": None,
            "codex_goal": {
                "objective": "Finish work",
                "status": "paused",
                "tokenBudget": 5000,
            },
        }
        manager = Mock(generation=7)
        manager.is_thread_loaded.return_value = False
        manager.resume_thread = AsyncMock(return_value=thread_id)
        manager.inject_items = AsyncMock()
        manager.get_thread_goal = AsyncMock(
            return_value={
                "objective": "Finish work",
                "status": "active",
                "tokenBudget": 5000,
            }
        )
        manager.set_thread_goal = AsyncMock(
            return_value={
                "objective": "Finish work",
                "status": "paused",
                "tokenBudget": 5000,
            }
        )

        async def save_provider_session(
            requested_session_id: str,
            provider_id: str,
            _backend: str,
            **_kwargs,
        ) -> None:
            current = agent_server.STORE.sessions[requested_session_id]
            current["session_id"] = provider_id
            current["codex_thread_id"] = provider_id

        with patch.object(agent_server.STORE, "sessions", {session_id: child}), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server.STORE,
            "save_provider_session",
            new_callable=AsyncMock,
            side_effect=save_provider_session,
        ), patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "codex_thread_instructions",
            return_value="child policy",
        ), patch.object(
            agent_server,
            "codex_thread_instruction_hash",
            return_value="policy-hash",
        ), patch.object(
            agent_server,
            "pin_codex_app_server_thread",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "unpin_codex_app_server_thread",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "touch_codex_app_server_thread",
            new_callable=AsyncMock,
        ):
            bound_id, _ = await agent_server.bind_forked_codex_thread(
                session_id,
                thread_id,
                child,
                require_goal_support=True,
                expected_goal={
                    "objective": "Finish work",
                    "status": "active",
                    "tokenBudget": 5000,
                },
            )

        self.assertEqual(bound_id, thread_id)
        manager.set_thread_goal.assert_awaited_once_with(
            thread_id,
            status="paused",
        )
        self.assertEqual(child["codex_goal"]["status"], "paused")

    async def test_failed_goal_bind_detaches_and_unloads_provider_fork(self) -> None:
        session_id = "child-chat"
        thread_id = "thread-child"
        child = {
            "id": session_id,
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": None,
            "codex_thread_id": None,
            "codex_goal": {"objective": "Finish work", "status": "paused"},
        }
        manager = Mock(generation=8)
        manager.is_thread_loaded.side_effect = [False, True]
        manager.resume_thread = AsyncMock(return_value=thread_id)
        manager.inject_items = AsyncMock()
        manager.get_thread_goal = AsyncMock(side_effect=RuntimeError("unsupported"))
        manager.unsubscribe_thread = AsyncMock()

        async def save_provider_session(
            requested_session_id: str,
            provider_id: str,
            _backend: str,
            **_kwargs,
        ) -> None:
            current = agent_server.STORE.sessions[requested_session_id]
            current["session_id"] = provider_id
            current["codex_thread_id"] = provider_id

        with patch.object(agent_server.STORE, "sessions", {session_id: child}), patch.object(
            agent_server.STORE,
            "_lock",
            asyncio.Lock(),
        ), patch.object(
            agent_server.STORE,
            "save",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server.STORE,
            "save_provider_session",
            new_callable=AsyncMock,
            side_effect=save_provider_session,
        ), patch.object(
            agent_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            agent_server,
            "codex_thread_instructions",
            return_value="child policy",
        ), patch.object(
            agent_server,
            "codex_thread_instruction_hash",
            return_value="policy-hash",
        ), patch.object(
            agent_server,
            "pin_codex_app_server_thread",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "unpin_codex_app_server_thread",
            new_callable=AsyncMock,
        ), patch.object(
            agent_server,
            "codex_goal_api_is_unsupported",
            return_value=True,
        ):
            with self.assertRaises(RuntimeError):
                await agent_server.bind_forked_codex_thread(
                    session_id,
                    thread_id,
                    child,
                    require_goal_support=True,
                )

        self.assertIsNone(child["session_id"])
        self.assertIsNone(child["codex_thread_id"])
        manager.unsubscribe_thread.assert_awaited_once_with(thread_id)

    async def test_any_inherited_goal_requires_native_readback(self) -> None:
        session_id = "child-chat"
        thread_id = "thread-child"
        child = {
            "id": session_id,
            "backend": agent_server.BACKEND_CODEX,
            "session_id": thread_id,
            "codex_thread_id": thread_id,
            "codex_goal": {"objective": "Finish work", "status": "paused"},
        }
        manager = Mock(generation=9)
        manager.get_thread_goal = AsyncMock(return_value=None)
        agent_server.CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)

        with patch.object(agent_server.STORE, "sessions", {session_id: child}):
            with self.assertRaisesRegex(
                agent_server.CodexAppServerProtocolError,
                "did not preserve the inherited goal",
            ):
                await agent_server.reconcile_codex_thread_goal(
                    manager,
                    session_id,
                    thread_id,
                    force=True,
                    expected_goal={
                        "objective": "Finish work",
                        "status": "paused",
                    },
                )

        agent_server.CODEX_GOAL_SYNC_GENERATIONS.pop(session_id, None)

    async def test_inherited_goal_validation_rejects_field_mismatches(self) -> None:
        expected = {
            "objective": "Finish work",
            "status": "paused",
            "tokenBudget": 5000,
        }
        mismatches = {
            "missing": None,
            "objective": {
                "objective": "Different work",
                "status": "paused",
                "tokenBudget": 5000,
            },
            "token budget": {
                "objective": "Finish work",
                "status": "paused",
                "tokenBudget": 6000,
            },
            "status": {
                "objective": "Finish work",
                "status": "blocked",
                "tokenBudget": 5000,
            },
        }

        for field, native in mismatches.items():
            with self.subTest(field=field), self.assertRaises(
                agent_server.CodexAppServerProtocolError
            ):
                agent_server.validate_inherited_codex_goal(expected, native)

    async def test_active_inherited_goal_validates_as_paused(self) -> None:
        agent_server.validate_inherited_codex_goal(
            {
                "objective": "Finish work",
                "status": "active",
                "token_budget": 5000,
            },
            {
                "objective": "Finish work",
                "status": "paused",
                "tokenBudget": 5000,
            },
        )


class ForkMemoryTests(unittest.TestCase):
    def test_tool_noise_does_not_displace_useful_conversation(self) -> None:
        parent_id = "parent-chat"
        events = [
            {
                "type": "turn_started",
                "run_id": "run-1",
                "prompt": "Please inspect this image",
            },
            *[
                {
                    "type": "tool_finished",
                    "run_id": "run-1",
                    "output": f"tool output {index}",
                }
                for index in range(500)
            ],
            {
                "type": "reasoning_summary",
                "run_id": "run-1",
                "phase": "commentary",
                "text": "I found the visual issue.",
            },
            {
                "type": "turn_stopped",
                "run_id": "run-1",
            },
            {
                "type": "turn_started",
                "run_id": "run-2",
                "prompt": "Continue with the fix",
            },
            {
                "type": "turn_finished",
                "run_id": "run-2",
                "result_text": "The fix is ready.",
            },
        ]
        parent = {
            "id": parent_id,
            "title": "Parent",
            "cwd": "/tmp",
            "backend": agent_server.BACKEND_CODEX,
        }

        with patch.object(
            agent_server,
            "iter_session_events",
            side_effect=lambda _session_id: iter(events),
        ) as iter_events:
            memory = agent_server.build_fork_memory(parent, parent_id)

        self.assertEqual(iter_events.call_count, 2)
        self.assertIn("Please inspect this image", memory)
        self.assertIn("I found the visual issue.", memory)
        self.assertIn("Continue with the fix", memory)
        self.assertIn("The fix is ready.", memory)
        self.assertNotIn("tool output 499", memory)


if __name__ == "__main__":
    unittest.main()
