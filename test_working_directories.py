import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import agent_server


class WorkingDirectoryCompletionTests(unittest.TestCase):
    def test_exact_directory_lists_children_with_trailing_sep(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "alpha").mkdir()
            (root / "alps").mkdir()
            (root / "a-file.txt").write_text("x")
            result = agent_server.complete_working_directory_sync(str(root))
            self.assertTrue(result["exists"])
            self.assertEqual(result["base_path"], str(root))
            names = [s["name"] for s in result["suggestions"]]
            self.assertIn("alpha", names)
            self.assertIn("alps", names)
            self.assertNotIn("a-file.txt", names)  # directories only
            for suggestion in result["suggestions"]:
                self.assertTrue(suggestion["path"].endswith(("\\", "/")))

    def test_prefix_completes_case_insensitively(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Beta").mkdir()
            (root / "BOW").mkdir()
            (root / "bowl").mkdir()
            result = agent_server.complete_working_directory_sync(str(root / "bO"))
            self.assertFalse(result["exists"])
            self.assertEqual(result["base_path"], str(root))
            self.assertEqual([s["name"] for s in result["suggestions"]], ["BOW", "bowl"])

    def test_hidden_entries_only_with_dot_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".hidden").mkdir()
            (root / "visible").mkdir()
            result = agent_server.complete_working_directory_sync(str(root))
            self.assertEqual([s["name"] for s in result["suggestions"]], ["visible"])
            dotted = agent_server.complete_working_directory_sync(str(root / ".h"))
            self.assertIn(".hidden", [s["name"] for s in dotted["suggestions"]])

    def test_missing_parent_reports_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = str(Path(temporary) / "gone" / "child")
            result = agent_server.complete_working_directory_sync(missing)
            self.assertEqual(result["suggestions"], [])
            self.assertEqual(result["message"], "Parent directory not found.")

    def test_nul_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            agent_server.complete_working_directory_sync("bad" + chr(0) + "path")
        self.assertEqual(raised.exception.status_code, 400)

    def test_limit_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for i in range(30):
                (root / f"dir{i:02d}").mkdir()
            result = agent_server.complete_working_directory_sync(str(root), limit=5)
            self.assertLessEqual(len(result["suggestions"]), 5)
            self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
