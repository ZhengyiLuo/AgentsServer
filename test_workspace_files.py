import asyncio
import errno
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request

import agent_server


class WorkspaceFilesTests(unittest.TestCase):
    def session(self, root: Path, *, archived: bool = False) -> dict[str, object]:
        return {"id": "session-1", "cwd": str(root), "archived": archived}

    def request(self, method: str = "GET", **headers: str) -> Request:
        return Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/workspace/preview",
            "raw_path": b"/workspace/preview",
            "query_string": b"",
            "headers": [
                (name.lower().replace("_", "-").encode("ascii"), value.encode("ascii"))
                for name, value in headers.items()
            ],
            "client": ("test", 123),
            "server": ("test", 80),
        })

    def response_body(self, response: object) -> bytes:
        async def collect() -> bytes:
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
            return b"".join(chunks)

        return asyncio.run(collect())

    def test_workspace_uses_the_exact_session_cwd_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(missing)}):
                with self.assertRaises(HTTPException) as raised:
                    agent_server.workspace_info_sync("session-1")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "workspace_unavailable")

    def test_lists_one_directory_with_pagination_and_symlink_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "b.py").write_text("print('b')\n")
            (root / "src" / "a.py").write_text("print('a')\n")
            (root / "src" / "nested").mkdir()
            (root / "src" / "alias").symlink_to(root / "src" / "a.py")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                first = agent_server.list_workspace_entries_sync("session-1", "src", 0, 2)
                second = agent_server.list_workspace_entries_sync("session-1", "src", 2, 2)

        self.assertEqual(first["total"], 4)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["entries"][0]["kind"], "directory")
        self.assertEqual([item["path"] for item in first["entries"] + second["entries"]], [
            "src/nested", "src/a.py", "src/alias", "src/b.py",
        ])
        self.assertEqual(second["entries"][0]["kind"], "symlink")

    def test_workspace_capability_v4_entries_have_opaque_revisions_and_preview_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "app.py"
            path.write_text("first\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                info = agent_server.workspace_info_sync("session-1")
                listed = agent_server.list_workspace_entries_sync("session-1", "", 0, 20)["entries"][0]
                searched = agent_server.search_workspace_files_sync("session-1", "app.py", 20)["entries"][0]
                path.write_text("second, longer\n")
                changed = agent_server.list_workspace_entries_sync("session-1", "", 0, 20)["entries"][0]

        self.assertEqual(info["capability_version"], 4)
        self.assertEqual(info["max_preview_file_bytes"], agent_server.MAX_WORKSPACE_PREVIEW_BYTES)
        self.assertIn("application/pdf", info["preview_media_types"])
        self.assertIn("image/png", info["preview_media_types"])
        self.assertIn("image/tiff", info["preview_media_types"])
        self.assertNotIn("image/svg+xml", info["preview_media_types"])
        self.assertRegex(listed["revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(searched["revision"], listed["revision"])
        self.assertNotEqual(changed["revision"], listed["revision"])

    def test_create_file_and_directory_returns_editor_and_tree_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                created_file = agent_server.create_workspace_entry_sync(
                    "session-1", "src/new_file.py", "file"
                )
                created_directory = agent_server.create_workspace_entry_sync(
                    "session-1", "notes", "directory"
                )
                listed = {
                    entry["path"]: entry
                    for entry in (
                        agent_server.list_workspace_entries_sync("session-1", "", 0, 20)["entries"]
                        + agent_server.list_workspace_entries_sync("session-1", "src", 0, 20)["entries"]
                    )
                }
            file_bytes = (root / "src" / "new_file.py").read_bytes()
            directory_is_empty = list((root / "notes").iterdir()) == []

        self.assertEqual(set(created_file), {"root", "entry", "file"})
        self.assertEqual(created_file["entry"]["path"], "src/new_file.py")
        self.assertEqual(created_file["entry"]["kind"], "file")
        self.assertEqual(created_file["file"]["path"], "src/new_file.py")
        self.assertEqual(created_file["file"]["content"], "")
        self.assertEqual(created_file["file"]["revision"], agent_server.workspace_revision(b""))
        self.assertEqual(created_file["file"]["size"], 0)
        self.assertEqual(set(created_directory), {"root", "entry"})
        self.assertEqual(created_directory["entry"]["path"], "notes")
        self.assertEqual(created_directory["entry"]["kind"], "directory")
        self.assertEqual(listed["src/new_file.py"]["revision"], created_file["entry"]["revision"])
        self.assertEqual(listed["notes"]["revision"], created_directory["entry"]["revision"])
        self.assertEqual(file_bytes, b"")
        self.assertTrue(directory_is_empty)

    def test_create_never_overwrites_files_directories_or_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            existing_file = root / "occupied.txt"
            existing_file.write_text("keep\n")
            (root / "occupied-directory").mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside\n")
            (root / "occupied-link").symlink_to(outside)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                errors: list[HTTPException] = []
                for path, kind in (
                    ("occupied.txt", "file"),
                    ("occupied.txt", "directory"),
                    ("occupied-directory", "file"),
                    ("occupied-directory", "directory"),
                    ("occupied-link", "file"),
                    ("occupied-link", "directory"),
                ):
                    with self.subTest(path=path, kind=kind), self.assertRaises(HTTPException) as raised:
                        agent_server.create_workspace_entry_sync("session-1", path, kind)
                    errors.append(raised.exception)
            file_content = existing_file.read_text()
            outside_content = outside.read_text()
            link_target = (root / "occupied-link").readlink()

        self.assertTrue(all(error.status_code == 409 for error in errors))
        self.assertTrue(all(error.detail["code"] == "workspace_entry_exists" for error in errors))
        self.assertEqual(file_content, "keep\n")
        self.assertEqual(outside_content, "outside\n")
        self.assertEqual(link_target, outside)

    def test_create_rejects_unsafe_missing_symlinked_archived_and_denied_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            session = self.session(root)
            real_open = agent_server.os.open

            def deny_one_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
                if path == "denied.txt" and kwargs.get("dir_fd") is not None:
                    raise OSError(errno.EACCES, "denied")
                return real_open(path, flags, *args, **kwargs)

            with patch.object(agent_server.STORE, "sessions", {"session-1": session}):
                for unsafe_path in ("", "/absolute.txt", "../outside.txt", "safe/../../outside.txt"):
                    with self.subTest(path=unsafe_path), self.assertRaises(HTTPException) as unsafe:
                        agent_server.create_workspace_entry_sync("session-1", unsafe_path, "file")
                    self.assertEqual(unsafe.exception.status_code, 400)
                with self.assertRaises(HTTPException) as missing:
                    agent_server.create_workspace_entry_sync("session-1", "missing/new.txt", "file")
                with self.assertRaises(HTTPException) as symlink:
                    agent_server.create_workspace_entry_sync("session-1", "escape/new.txt", "file")
                with patch.object(agent_server.os, "open", side_effect=deny_one_file):
                    with self.assertRaises(HTTPException) as denied:
                        agent_server.create_workspace_entry_sync("session-1", "denied.txt", "file")
                session["archived"] = True
                with self.assertRaises(HTTPException) as archived:
                    agent_server.create_workspace_entry_sync("session-1", "archived.txt", "file")

        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(missing.exception.detail["code"], "workspace_directory_not_found")
        self.assertIn(symlink.exception.status_code, {400, 403})
        self.assertIn(
            symlink.exception.detail["code"],
            {"invalid_workspace_path", "workspace_symlink_blocked"},
        )
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(denied.exception.detail["code"], "workspace_permission_denied")
        self.assertEqual(archived.exception.status_code, 409)
        self.assertEqual(archived.exception.detail["code"], "workspace_read_only")

    def test_concurrent_create_allows_exactly_one_creator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            barrier = threading.Barrier(2)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                def create() -> str:
                    barrier.wait()
                    try:
                        agent_server.create_workspace_entry_sync("session-1", "shared.txt", "file")
                        return "created"
                    except HTTPException as exc:
                        return str(exc.detail["code"])

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: create(), range(2)))
            entries = list(root.iterdir())
            content = entries[0].read_bytes()

        self.assertEqual(sorted(results), ["created", "workspace_entry_exists"])
        self.assertEqual([entry.name for entry in entries], ["shared.txt"])
        self.assertEqual(content, b"")

    def test_create_post_create_failures_report_partial_success_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                real_fstat = agent_server.os.fstat
                fstat_calls = 0

                def fail_first_fstat(file_fd: int) -> os.stat_result:
                    nonlocal fstat_calls
                    fstat_calls += 1
                    if fstat_calls == 1:
                        raise OSError(errno.EIO, "fstat failed")
                    return real_fstat(file_fd)

                with patch.object(agent_server.os, "fstat", side_effect=fail_first_fstat):
                    with self.assertRaises(HTTPException) as fstat_failure:
                        agent_server.create_workspace_entry_sync(
                            "session-1", "fstat-failure.txt", "file"
                        )

                real_fsync = agent_server.os.fsync

                def fail_file_fsync(file_fd: int) -> None:
                    if stat.S_ISREG(real_fstat(file_fd).st_mode):
                        raise OSError(errno.EIO, "fsync failed")
                    real_fsync(file_fd)

                with patch.object(agent_server.os, "fsync", side_effect=fail_file_fsync):
                    with self.assertRaises(HTTPException) as fsync_failure:
                        agent_server.create_workspace_entry_sync(
                            "session-1", "fsync-failure.txt", "file"
                        )
            remaining = sorted(entry.name for entry in root.iterdir())

        self.assertEqual(
            fstat_failure.exception.detail["code"],
            "workspace_create_partial_success",
        )
        self.assertEqual(
            fsync_failure.exception.detail["code"],
            "workspace_create_partial_success",
        )
        self.assertEqual(remaining, ["fstat-failure.txt", "fsync-failure.txt"])

    def test_create_directory_validation_failure_reports_partial_success_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_stat = agent_server.os.stat
            target_stat_calls = 0

            def fail_second_target_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
                nonlocal target_stat_calls
                if path == "drafts" and kwargs.get("dir_fd") is not None:
                    target_stat_calls += 1
                    if target_stat_calls == 2:
                        raise OSError(errno.EIO, "validation stat failed")
                return real_stat(path, *args, **kwargs)

            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                with patch.object(agent_server.os, "stat", side_effect=fail_second_target_stat):
                    with self.assertRaises(HTTPException) as failed:
                        agent_server.create_workspace_entry_sync(
                            "session-1", "drafts", "directory"
                        )
            remaining = [entry.name for entry in root.iterdir()]

        self.assertEqual(failed.exception.detail["code"], "workspace_create_partial_success")
        self.assertEqual(remaining, ["drafts"])

    def test_create_rollback_never_removes_a_raced_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "raced.txt"
            real_fstat = agent_server.os.fstat
            real_fsync = agent_server.os.fsync
            replaced = False

            def replace_then_fail_fsync(file_fd: int) -> None:
                nonlocal replaced
                if stat.S_ISREG(real_fstat(file_fd).st_mode) and not replaced:
                    replaced = True
                    path.unlink()
                    path.write_text("replacement\n")
                    raise OSError(errno.EIO, "fsync failed after replacement")
                real_fsync(file_fd)

            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                with patch.object(agent_server.os, "fsync", side_effect=replace_then_fail_fsync):
                    with self.assertRaises(HTTPException) as failed:
                        agent_server.create_workspace_entry_sync(
                            "session-1", "raced.txt", "file"
                        )
            content = path.read_text()

        self.assertEqual(failed.exception.detail["code"], "workspace_create_partial_success")
        self.assertEqual(content, "replacement\n")

    def test_create_failure_never_attempts_automatic_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "stuck.txt"
            real_fstat = agent_server.os.fstat
            real_fsync = agent_server.os.fsync

            def fail_file_fsync(file_fd: int) -> None:
                if stat.S_ISREG(real_fstat(file_fd).st_mode):
                    raise OSError(errno.EIO, "fsync failed")
                real_fsync(file_fd)

            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                with (
                    patch.object(agent_server.os, "fsync", side_effect=fail_file_fsync),
                    patch.object(agent_server.os, "unlink") as unlink_entry,
                    patch.object(agent_server.os, "rmdir") as remove_directory,
                    self.assertRaises(HTTPException) as failed,
                ):
                    agent_server.create_workspace_entry_sync(
                        "session-1", "stuck.txt", "file"
                    )
            exists = path.exists()

        self.assertEqual(failed.exception.status_code, 500)
        self.assertEqual(
            failed.exception.detail["code"],
            "workspace_create_partial_success",
        )
        self.assertTrue(exists)
        unlink_entry.assert_not_called()
        remove_directory.assert_not_called()

    def test_post_workspace_entry_route_returns_create_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                result = asyncio.run(agent_server.post_session_workspace_entry(
                    "session-1",
                    agent_server.WorkspaceCreateRequest(path="draft.md", kind="file"),
                ))

        self.assertEqual(set(result), {"root", "entry", "file"})
        self.assertEqual(result["entry"]["path"], "draft.md")
        self.assertEqual(result["entry"]["kind"], "file")
        self.assertEqual(result["file"]["content"], "")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "atomic no-replace rename is supported on macOS and Linux",
    )
    def test_rename_is_same_parent_atomic_and_never_overwrites(self) -> None:
        self.assertTrue(agent_server.WORKSPACE_MUTATIONS_AVAILABLE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            destination = root / "renamed.txt"
            collision = root / "occupied.txt"
            source.write_text("source\n")
            collision.write_text("occupied\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                entries = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"]
                revision = next(entry["revision"] for entry in entries if entry["path"] == "source.txt")
                renamed = agent_server.rename_workspace_entry_sync(
                    "session-1", "source.txt", "renamed.txt", revision
                )
                renamed_revision = renamed["entry"]["revision"]
                with self.assertRaises(HTTPException) as exists:
                    agent_server.rename_workspace_entry_sync(
                        "session-1", "renamed.txt", "occupied.txt", renamed_revision
                    )
                source_exists = source.exists()
                destination_content = destination.read_text()
                collision_content = collision.read_text()

        self.assertEqual(renamed["previous_path"], "source.txt")
        self.assertEqual(renamed["entry"]["path"], "renamed.txt")
        self.assertEqual(renamed["entry"]["kind"], "file")
        self.assertFalse(source_exists)
        self.assertEqual(destination_content, "source\n")
        self.assertEqual(collision_content, "occupied\n")
        self.assertEqual(exists.exception.status_code, 409)
        self.assertEqual(exists.exception.detail["code"], "workspace_entry_exists")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "atomic no-replace rename is supported on macOS and Linux",
    )
    def test_rename_rolls_back_when_source_is_replaced_after_revision_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            displaced = root / "displaced.txt"
            destination = root / "renamed.txt"
            source.write_text("expected\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                atomic_rename = agent_server.atomic_rename_workspace_entry
                first_call = True

                def replace_then_rename(
                    parent_fd: int,
                    old_name: str,
                    new_name: str,
                    path: str,
                ) -> None:
                    nonlocal first_call
                    if first_call:
                        first_call = False
                        os.rename(source, displaced)
                        source.write_text("replacement\n")
                    atomic_rename(parent_fd, old_name, new_name, path)

                with patch.object(
                    agent_server,
                    "atomic_rename_workspace_entry",
                    side_effect=replace_then_rename,
                ), self.assertRaises(HTTPException) as raced:
                    agent_server.rename_workspace_entry_sync(
                        "session-1", "source.txt", "renamed.txt", revision
                    )

            source_content = source.read_text()
            displaced_content = displaced.read_text()
            destination_exists = destination.exists()

        self.assertEqual(raced.exception.status_code, 409)
        self.assertEqual(raced.exception.detail["code"], "workspace_entry_conflict")
        self.assertEqual(source_content, "replacement\n")
        self.assertEqual(displaced_content, "expected\n")
        self.assertFalse(destination_exists)

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "atomic no-replace rename is supported on macOS and Linux",
    )
    def test_rename_rejects_stale_revisions_invalid_names_and_archived_chats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "source.txt"
            path.write_text("first\n")
            session = self.session(root)
            with patch.object(agent_server.STORE, "sessions", {"session-1": session}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                path.write_text("changed\n")
                with self.assertRaises(HTTPException) as stale:
                    agent_server.rename_workspace_entry_sync(
                        "session-1", "source.txt", "renamed.txt", revision
                    )
                current_revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                for name in ("", ".", "..", "nested/name.txt"):
                    with self.subTest(name=name), self.assertRaises(HTTPException) as invalid:
                        agent_server.rename_workspace_entry_sync(
                            "session-1", "source.txt", name, current_revision
                        )
                    self.assertEqual(invalid.exception.status_code, 400)
                session["archived"] = True
                with self.assertRaises(HTTPException) as archived:
                    agent_server.rename_workspace_entry_sync(
                        "session-1", "source.txt", "renamed.txt", current_revision
                    )
                source_exists = path.exists()

        self.assertTrue(source_exists)
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(stale.exception.detail["code"], "workspace_entry_conflict")
        self.assertEqual(archived.exception.detail["code"], "workspace_read_only")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "atomic no-replace rename is supported on macOS and Linux",
    )
    def test_rename_moves_directories_and_symlinks_without_following_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            directory = root / "folder"
            directory.mkdir()
            (directory / "child.txt").write_text("child\n")
            outside = base / "outside.txt"
            outside.write_text("outside\n")
            (root / "link.txt").symlink_to(outside)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                entries = {
                    entry["path"]: entry
                    for entry in agent_server.list_workspace_entries_sync(
                        "session-1", "", 0, 20
                    )["entries"]
                }
                renamed_directory = agent_server.rename_workspace_entry_sync(
                    "session-1", "folder", "renamed-folder", entries["folder"]["revision"]
                )
                renamed_link = agent_server.rename_workspace_entry_sync(
                    "session-1", "link.txt", "renamed-link.txt", entries["link.txt"]["revision"]
                )
            child_content = (root / "renamed-folder" / "child.txt").read_text()
            link_target = (root / "renamed-link.txt").readlink()
            outside_content = outside.read_text()

        self.assertEqual(renamed_directory["entry"]["kind"], "directory")
        self.assertEqual(renamed_link["entry"]["kind"], "symlink")
        self.assertEqual(child_content, "child\n")
        self.assertEqual(link_target, outside)
        self.assertEqual(outside_content, "outside\n")

    def test_rejects_absolute_parent_and_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("outside")
            outside_directory = root.parent / f"{root.name}-outside"
            outside_directory.mkdir()
            outside_child = outside_directory / "outside.txt"
            outside_child.write_text("outside")
            (root / "escape").symlink_to(outside)
            (root / "escape-dir").symlink_to(outside_directory, target_is_directory=True)
            try:
                with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                    for path in ("/etc/passwd", "../outside.txt", "safe/../../outside.txt"):
                        with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                            agent_server.read_workspace_file_sync("session-1", path)
                        self.assertEqual(raised.exception.status_code, 400)
                    with self.assertRaises(HTTPException) as symlink_error:
                        agent_server.read_workspace_file_sync("session-1", "escape")
                    self.assertEqual(symlink_error.exception.status_code, 403)
                    self.assertEqual(symlink_error.exception.detail["code"], "workspace_symlink_blocked")
                    outside_revision = agent_server.workspace_entry_revision(outside_child.stat())
                    with self.assertRaises(HTTPException) as rename_symlink_parent:
                        agent_server.rename_workspace_entry_sync(
                            "session-1",
                            "escape-dir/outside.txt",
                            "renamed.txt",
                            outside_revision,
                        )
                    with self.assertRaises(HTTPException) as delete_symlink_parent:
                        agent_server.remove_workspace_entry_sync(
                            "session-1",
                            "escape-dir/outside.txt",
                            outside_revision,
                            False,
                        )
                    self.assertIn(
                        rename_symlink_parent.exception.detail["code"],
                        {"workspace_symlink_blocked", "invalid_workspace_path"},
                    )
                    self.assertIn(
                        delete_symlink_parent.exception.detail["code"],
                        {"workspace_symlink_blocked", "invalid_workspace_path"},
                    )
                    self.assertEqual(outside_child.read_text(), "outside")
            finally:
                outside.unlink(missing_ok=True)
                outside_child.unlink(missing_ok=True)
                outside_directory.rmdir()

    def test_reads_utf8_without_changing_bom_or_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b"\xef\xbb\xbfline one\r\nline two\r\n"
            (root / "notes.md").write_bytes(data)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                result = agent_server.read_workspace_file_sync("session-1", "notes.md")

        self.assertEqual(result["content"].encode("utf-8"), data)
        self.assertEqual(result["revision"], agent_server.workspace_revision(data))
        self.assertEqual(result["size"], len(data))

    def test_rejects_binary_invalid_utf8_and_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "binary.dat").write_bytes(b"abc\x00def")
            (root / "latin.txt").write_bytes(b"\xff\xfe")
            (root / "large.txt").write_bytes(b"12345")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                for path, code in (("binary.dat", "workspace_binary_file"), ("latin.txt", "workspace_encoding_unsupported")):
                    with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                        agent_server.read_workspace_file_sync("session-1", path)
                    self.assertEqual(raised.exception.status_code, 415)
                    self.assertEqual(raised.exception.detail["code"], code)
                with patch.object(agent_server, "MAX_WORKSPACE_TEXT_BYTES", 4):
                    with self.assertRaises(HTTPException) as too_large:
                        agent_server.read_workspace_file_sync("session-1", "large.txt")
                    self.assertEqual(too_large.exception.status_code, 413)

    def test_preview_get_and_head_stream_safe_media_with_revision_and_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_data = b"\x89PNG\r\n\x1a\npreview-data"
            pdf_data = b"%PDF-1.7\n0123456789\n%%EOF\n"
            (root / "preview.png").write_bytes(image_data)
            (root / "report.pdf").write_bytes(pdf_data)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                image_revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                image_response = asyncio.run(agent_server.get_session_workspace_preview(
                    self.request(),
                    "session-1",
                    "preview.png",
                ))
                image_body = self.response_body(image_response)
                range_response = asyncio.run(agent_server.get_session_workspace_preview(
                    self.request(Range="bytes=5-9"),
                    "session-1",
                    "report.pdf",
                ))
                range_body = self.response_body(range_response)
                head_response = asyncio.run(agent_server.get_session_workspace_preview(
                    self.request("HEAD", Range="bytes=-4"),
                    "session-1",
                    "report.pdf",
                ))

        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.headers["content-type"], "image/png")
        self.assertEqual(image_response.headers["content-length"], str(len(image_data)))
        self.assertEqual(image_response.headers["etag"], f'"{image_revision}"')
        self.assertEqual(image_response.headers["x-agentsdock-revision"], image_revision)
        self.assertEqual(image_response.headers["accept-ranges"], "bytes")
        self.assertEqual(image_response.headers["cache-control"], "no-store")
        self.assertEqual(image_response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(image_body, image_data)
        self.assertEqual(range_response.status_code, 206)
        self.assertEqual(range_response.headers["content-type"], "application/pdf")
        self.assertEqual(range_response.headers["content-range"], f"bytes 5-9/{len(pdf_data)}")
        self.assertEqual(range_response.headers["content-length"], "5")
        self.assertEqual(range_body, pdf_data[5:10])
        self.assertEqual(head_response.status_code, 206)
        self.assertEqual(head_response.headers["content-range"], (
            f"bytes {len(pdf_data) - 4}-{len(pdf_data) - 1}/{len(pdf_data)}"
        ))
        self.assertEqual(head_response.headers["content-length"], "4")
        self.assertEqual(head_response.body, b"")

    def test_preview_rejects_unsupported_oversized_nonregular_and_escaping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            outside = Path(temporary) / "outside.png"
            outside.write_bytes(b"outside")
            (root / "vector.svg").write_text("<svg></svg>\n")
            (root / "large.png").write_bytes(b"12345")
            (root / "escape.png").symlink_to(outside)
            (root / "directory.png").mkdir()
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                for path, status, code in (
                    ("vector.svg", 415, "workspace_preview_unsupported"),
                    ("escape.png", 403, "workspace_symlink_blocked"),
                    ("directory.png", 400, "workspace_not_regular_file"),
                    ("../outside.png", 400, "invalid_workspace_path"),
                ):
                    with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                        agent_server.open_workspace_preview_sync("session-1", path)
                    self.assertEqual(raised.exception.status_code, status)
                    self.assertEqual(raised.exception.detail["code"], code)
                with patch.object(agent_server, "MAX_WORKSPACE_PREVIEW_BYTES", 4):
                    with self.assertRaises(HTTPException) as oversized:
                        agent_server.open_workspace_preview_sync("session-1", "large.png")
                outside_data = outside.read_bytes()

        self.assertEqual(oversized.exception.status_code, 413)
        self.assertEqual(oversized.exception.detail["code"], "workspace_preview_too_large")
        self.assertEqual(outside_data, b"outside")

    def test_download_streams_arbitrary_workspace_files_as_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b"\x00\xffbinary-workspace-data"
            (root / "模型.weights").write_bytes(data)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                response = asyncio.run(agent_server.get_session_workspace_download(
                    self.request(),
                    "session-1",
                    "模型.weights",
                ))
                body = self.response_body(response)
                head_response = asyncio.run(agent_server.get_session_workspace_download(
                    self.request("HEAD"),
                    "session-1",
                    "模型.weights",
                ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["content-length"], str(len(data)))
        self.assertEqual(
            response.headers["content-disposition"],
            "attachment; filename*=UTF-8''%E6%A8%A1%E5%9E%8B.weights",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(body, data)
        self.assertEqual(head_response.status_code, 200)
        self.assertEqual(head_response.headers["content-length"], str(len(data)))
        self.assertEqual(head_response.body, b"")

    def test_download_rejects_directories_symlinks_and_escaping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            outside = Path(temporary) / "outside.bin"
            outside.write_bytes(b"outside")
            (root / "escape.bin").symlink_to(outside)
            (root / "directory.bin").mkdir()
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                for path, status, code in (
                    ("escape.bin", 403, "workspace_symlink_blocked"),
                    ("directory.bin", 400, "workspace_not_regular_file"),
                    ("../outside.bin", 400, "invalid_workspace_path"),
                ):
                    with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                        agent_server.open_workspace_download_sync("session-1", path)
                    self.assertEqual(raised.exception.status_code, status)
                    self.assertEqual(raised.exception.detail["code"], code)
                outside_data = outside.read_bytes()

        self.assertEqual(outside_data, b"outside")

    def test_preview_parses_only_one_satisfiable_byte_range(self) -> None:
        self.assertIsNone(agent_server.parse_workspace_preview_range(None, 10))
        self.assertEqual(agent_server.parse_workspace_preview_range("bytes=2-", 10), (2, 9))
        self.assertEqual(agent_server.parse_workspace_preview_range("bytes=2-99", 10), (2, 9))
        self.assertEqual(agent_server.parse_workspace_preview_range("bytes=-3", 10), (7, 9))
        for value, size in (
            ("bytes=1-2,4-5", 10),
            ("items=1-2", 10),
            ("bytes=-0", 10),
            ("bytes=10-", 10),
            ("bytes=4-2", 10),
            ("bytes=0-0", 0),
            ("bytes=999999999999999999999999999999-", 10),
        ):
            with self.subTest(value=value, size=size), self.assertRaises(HTTPException) as raised:
                agent_server.parse_workspace_preview_range(value, size)
            self.assertEqual(raised.exception.status_code, 416)
            self.assertEqual(raised.exception.headers["Content-Range"], f"bytes */{size}")

    def test_atomic_save_preserves_mode_and_detects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "script.sh"
            path.write_text("#!/bin/sh\necho old\n")
            path.chmod(0o750)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                original = agent_server.read_workspace_file_sync("session-1", "script.sh")
                updated = agent_server.write_workspace_file_sync(
                    "session-1",
                    "script.sh",
                    "#!/bin/sh\necho new\n",
                    original["revision"],
                )
                with self.assertRaises(HTTPException) as conflict:
                    agent_server.write_workspace_file_sync(
                        "session-1",
                        "script.sh",
                        "#!/bin/sh\necho stale\n",
                        original["revision"],
                    )

            self.assertEqual(path.read_text(), "#!/bin/sh\necho new\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o750)
            self.assertEqual(updated["revision"], agent_server.workspace_revision(path.read_bytes()))
            self.assertEqual(conflict.exception.status_code, 409)
            self.assertEqual(conflict.exception.detail["code"], "workspace_file_conflict")
            self.assertEqual(list(root.glob(".*.agentsdock-*.tmp")), [])

    def test_save_keeps_the_root_used_to_select_its_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original_root = base / "original"
            replacement_root = base / "replacement"
            original_root.mkdir()
            replacement_root.mkdir()
            original_path = original_root / "shared.txt"
            replacement_path = replacement_root / "shared.txt"
            original_path.write_text("original\n")
            replacement_path.write_text("original\n")
            session = self.session(original_root)
            original_write_lock = agent_server.workspace_write_lock

            def change_cwd_after_lock_selection(root: Path, relative_path: str) -> threading.Lock:
                session["cwd"] = str(replacement_root)
                return original_write_lock(root, relative_path)

            with patch.object(agent_server.STORE, "sessions", {"session-1": session}):
                revision = agent_server.read_workspace_file_sync("session-1", "shared.txt")["revision"]
                with patch.object(
                    agent_server,
                    "workspace_write_lock",
                    side_effect=change_cwd_after_lock_selection,
                ):
                    updated = agent_server.write_workspace_file_sync(
                        "session-1",
                        "shared.txt",
                        "saved\n",
                        revision,
                    )

            self.assertEqual(original_path.read_text(), "saved\n")
            self.assertEqual(replacement_path.read_text(), "original\n")
            self.assertEqual(updated["root"], str(original_root.resolve()))

    def test_atomic_save_does_not_overwrite_a_concurrent_permission_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "permissions.txt"
            path.write_text("original\n")
            path.chmod(0o644)
            original_preserve = agent_server.preserve_workspace_metadata

            def chmod_during_save(source_fd: int, destination_fd: int, source_stat: os.stat_result) -> None:
                original_preserve(source_fd, destination_fd, source_stat)
                path.chmod(0o444)

            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.read_workspace_file_sync("session-1", "permissions.txt")["revision"]
                with patch.object(agent_server, "preserve_workspace_metadata", side_effect=chmod_during_save):
                    with self.assertRaises(HTTPException) as conflict:
                        agent_server.write_workspace_file_sync(
                            "session-1", "permissions.txt", "changed\n", revision
                        )

            self.assertEqual(conflict.exception.status_code, 409)
            self.assertEqual(conflict.exception.detail["code"], "workspace_file_conflict")
            self.assertEqual(path.read_text(), "original\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(list(root.glob(".*.agentsdock-*.tmp")), [])

    def test_concurrent_saves_with_one_revision_allow_exactly_one_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "shared.txt"
            path.write_text("original\n")
            barrier = threading.Barrier(2)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.read_workspace_file_sync("session-1", "shared.txt")["revision"]

                def save(content: str) -> str:
                    barrier.wait()
                    try:
                        agent_server.write_workspace_file_sync("session-1", "shared.txt", content, revision)
                        return "saved"
                    except HTTPException as exc:
                        return str(exc.detail["code"])

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(save, ("first\n", "second\n")))
                final_content = path.read_text()

        self.assertEqual(sorted(results), ["saved", "workspace_file_conflict"])
        self.assertIn(final_content, {"first\n", "second\n"})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_special_and_read_only_files_fail_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fifo = root / "events.pipe"
            os.mkfifo(fifo)
            read_only = root / "locked.txt"
            read_only.write_text("locked\n")
            read_only.chmod(0o444)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                with self.assertRaises(HTTPException) as special:
                    agent_server.read_workspace_file_sync("session-1", "events.pipe")
                current = agent_server.read_workspace_file_sync("session-1", "locked.txt")
                with self.assertRaises(HTTPException) as denied:
                    agent_server.write_workspace_file_sync(
                        "session-1", "locked.txt", "changed\n", current["revision"]
                    )

        self.assertEqual(special.exception.detail["code"], "workspace_not_regular_file")
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(denied.exception.detail["code"], "workspace_permission_denied")

    def test_rejects_nul_content_and_hard_link_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.txt"
            original.write_text("hello\n")
            linked = root / "linked.txt"
            linked.hardlink_to(original)
            normal = root / "normal.txt"
            normal.write_text("hello\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                hard_link_revision = agent_server.read_workspace_file_sync("session-1", "linked.txt")["revision"]
                with self.assertRaises(HTTPException) as hard_link:
                    agent_server.write_workspace_file_sync(
                        "session-1", "linked.txt", "changed\n", hard_link_revision
                    )
                normal_revision = agent_server.read_workspace_file_sync("session-1", "normal.txt")["revision"]
                with self.assertRaises(HTTPException) as binary:
                    agent_server.write_workspace_file_sync(
                        "session-1", "normal.txt", "invalid\x00text", normal_revision
                    )

        self.assertEqual(hard_link.exception.detail["code"], "workspace_hard_link_blocked")
        self.assertEqual(binary.exception.detail["code"], "workspace_binary_file")

    def test_archived_workspace_is_readable_but_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "README.md"
            path.write_text("hello\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root, archived=True)}):
                current = agent_server.read_workspace_file_sync("session-1", "README.md")
                listed = agent_server.list_workspace_entries_sync("session-1", "", 0, 20)
                searched = agent_server.search_workspace_files_sync("session-1", "readme", 20)
                self.assertFalse(current["writable"])
                self.assertFalse(listed["entries"][0]["writable"])
                self.assertFalse(searched["entries"][0]["writable"])
                with self.assertRaises(HTTPException) as raised:
                    agent_server.write_workspace_file_sync(
                        "session-1", "README.md", "changed\n", current["revision"]
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "workspace_read_only")

    def test_delete_removes_files_symlinks_and_empty_directories_without_following_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("keep\n")
            (root / "file.txt").write_text("remove\n")
            (root / "link.txt").symlink_to(outside)
            (root / "empty").mkdir()
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                entries = {
                    entry["path"]: entry
                    for entry in agent_server.list_workspace_entries_sync(
                        "session-1", "", 0, 20
                    )["entries"]
                }
                removed_file = agent_server.remove_workspace_entry_sync(
                    "session-1", "file.txt", entries["file.txt"]["revision"], False
                )
                removed_link = agent_server.remove_workspace_entry_sync(
                    "session-1", "link.txt", entries["link.txt"]["revision"], False
                )
                removed_directory = agent_server.remove_workspace_entry_sync(
                    "session-1", "empty", entries["empty"]["revision"], False
                )
            remaining = sorted(path.name for path in root.iterdir())
            outside_content = outside.read_text()

        self.assertEqual(removed_file["kind"], "file")
        self.assertEqual(removed_link["kind"], "symlink")
        self.assertEqual(removed_directory["kind"], "directory")
        self.assertTrue(removed_file["removed"])
        self.assertEqual(remaining, [])
        self.assertEqual(outside_content, "keep\n")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "atomic no-replace rename is supported on macOS and Linux",
    )
    def test_patch_and_delete_route_contracts_return_v2_mutation_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "before.txt").write_text("content\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                renamed = asyncio.run(agent_server.patch_session_workspace_entry(
                    "session-1",
                    agent_server.WorkspaceRenameRequest(
                        path="before.txt",
                        new_name="after.txt",
                        expected_revision=revision,
                    ),
                ))
                removed = asyncio.run(agent_server.delete_session_workspace_entry(
                    "session-1",
                    "after.txt",
                    renamed["entry"]["revision"],
                    False,
                ))

        self.assertEqual(set(renamed), {"root", "previous_path", "entry"})
        self.assertEqual(renamed["previous_path"], "before.txt")
        self.assertEqual(renamed["entry"]["path"], "after.txt")
        self.assertEqual(removed, {
            "root": str(root.resolve()),
            "path": "after.txt",
            "kind": "file",
            "removed": True,
        })

    def test_recursive_delete_requires_recursive_confirmation_and_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep\n")
            tree = root / "tree"
            (tree / "nested").mkdir(parents=True)
            (tree / "nested" / "remove.txt").write_text("remove\n")
            (tree / "outside-link").symlink_to(outside, target_is_directory=True)
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                with self.assertRaises(HTTPException) as non_recursive:
                    agent_server.remove_workspace_entry_sync(
                        "session-1", "tree", revision, False
                    )
                removed = agent_server.remove_workspace_entry_sync(
                    "session-1", "tree", revision, True
                )
            tree_exists = tree.exists()
            outside_content = (outside / "keep.txt").read_text()

        self.assertEqual(non_recursive.exception.status_code, 409)
        self.assertEqual(non_recursive.exception.detail["code"], "workspace_directory_not_empty")
        self.assertEqual(removed["kind"], "directory")
        self.assertFalse(tree_exists)
        self.assertEqual(outside_content, "keep\n")

    def test_delete_rejects_stale_revisions_archived_chats_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "notes.txt"
            path.write_text("first\n")
            session = self.session(root)
            with patch.object(agent_server.STORE, "sessions", {"session-1": session}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                path.write_text("changed and longer\n")
                with self.assertRaises(HTTPException) as stale:
                    agent_server.remove_workspace_entry_sync(
                        "session-1", "notes.txt", revision, False
                    )
                current_revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                for unsafe_path in ("", "/notes.txt", "../notes.txt"):
                    with self.subTest(path=unsafe_path), self.assertRaises(HTTPException) as unsafe:
                        agent_server.remove_workspace_entry_sync(
                            "session-1", unsafe_path, current_revision, False
                        )
                    self.assertEqual(unsafe.exception.status_code, 400)
                session["archived"] = True
                with self.assertRaises(HTTPException) as archived:
                    agent_server.remove_workspace_entry_sync(
                        "session-1", "notes.txt", current_revision, False
                    )
                path_exists = path.exists()

        self.assertTrue(path_exists)
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(stale.exception.detail["code"], "workspace_entry_conflict")
        self.assertEqual(archived.exception.detail["code"], "workspace_read_only")

    def test_recursive_delete_rejects_a_nested_mounted_filesystem_before_removing_entries(self) -> None:
        class CrossDeviceStat:
            def __init__(self, original: os.stat_result):
                self.original = original
                self.st_dev = original.st_dev + 1

            def __getattr__(self, name: str) -> object:
                return getattr(self.original, name)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "tree"
            mounted = tree / "mounted"
            mounted.mkdir(parents=True)
            (mounted / "keep.txt").write_text("keep\n")
            (tree / "ordinary.txt").write_text("keep\n")
            real_stat = agent_server.os.stat

            def report_nested_mount(path: object, *args: object, **kwargs: object) -> object:
                result = real_stat(path, *args, **kwargs)
                if path == "mounted" and kwargs.get("dir_fd") is not None:
                    return CrossDeviceStat(result)
                return result

            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                revision = agent_server.list_workspace_entries_sync(
                    "session-1", "", 0, 20
                )["entries"][0]["revision"]
                with (
                    patch.object(agent_server.os, "stat", side_effect=report_nested_mount),
                    self.assertRaises(HTTPException) as mounted_error,
                ):
                    agent_server.remove_workspace_entry_sync(
                        "session-1", "tree", revision, True
                    )
            preserved = sorted(str(path.relative_to(root)) for path in tree.rglob("*"))

        self.assertEqual(mounted_error.exception.status_code, 409)
        self.assertEqual(mounted_error.exception.detail["code"], "workspace_cross_device_delete")
        self.assertEqual(preserved, ["tree/mounted", "tree/mounted/keep.txt", "tree/ordinary.txt"])

    def test_search_is_bounded_and_skips_generated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "agent_server.py").write_text("pass\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "agent-package.js").write_text("ignored\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                result = agent_server.search_workspace_files_sync("session-1", "agent", 20)

        self.assertEqual([item["path"] for item in result["entries"]], ["src/agent_server.py"])
        self.assertLessEqual(result["scanned"], agent_server.MAX_WORKSPACE_SEARCH_SCAN)

    def test_search_uses_git_index_to_reach_deep_files_before_the_scan_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "groot" / "control" / "sonic_vla" / "policy_runner.py"
            target.parent.mkdir(parents=True)
            target.write_text("pass\n")
            def list_git_files(*_args: object, stdout: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                stdout.write(b"groot/control/sonic_vla/policy_runner.py\0")
                return subprocess.CompletedProcess(args=["git", "ls-files"], returncode=0)
            with (
                patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}),
                patch.object(agent_server.subprocess, "run", side_effect=list_git_files) as run,
            ):
                result = agent_server.search_workspace_files_sync("session-1", "policy_runner", 20)

        self.assertEqual([item["path"] for item in result["entries"]], [
            "groot/control/sonic_vla/policy_runner.py"
        ])
        self.assertEqual(result["scanned"], 1)
        self.assertFalse(result["truncated"])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn(":(icase,glob)**/*policy_runner*", command)

    def test_empty_search_reports_truncation_and_posix_backslashes_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(3):
                (root / f"file-{index}.txt").write_text(str(index))
            unusual = root / "literal\\name.txt"
            unusual.write_text("backslash\n")
            with patch.object(agent_server.STORE, "sessions", {"session-1": self.session(root)}):
                result = agent_server.search_workspace_files_sync("session-1", "", 2)
                if os.name != "nt":
                    opened = agent_server.read_workspace_file_sync("session-1", "literal\\name.txt")

        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(result["truncated"])
        if os.name != "nt":
            self.assertEqual(opened["content"], "backslash\n")


if __name__ == "__main__":
    unittest.main()
