import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import agent_server


class WorkspaceFilesTests(unittest.TestCase):
    def session(self, root: Path, *, archived: bool = False) -> dict[str, object]:
        return {"id": "session-1", "cwd": str(root), "archived": archived}

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

    def test_rejects_absolute_parent_and_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("outside")
            (root / "escape").symlink_to(outside)
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
            finally:
                outside.unlink(missing_ok=True)

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
