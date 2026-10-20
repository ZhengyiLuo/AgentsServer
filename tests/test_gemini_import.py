import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from gemini_import import candidates, initial_history
from gemini_acp_client import GeminiProtocolError
from local_session_ownership import provider_session_keys

ID = "12345678-1234-1234-1234-123456789abc"


class Import(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.project = self.root / "项目 空格"; self.project.mkdir()
        self.folder = self.root / ".gemini/tmp/slug"; self.chats = self.folder / "chats"; self.chats.mkdir(parents=True)
        (self.folder / ".project_root").write_text(str(self.project))
        self.path = self.chats / "session-fixture-12345678.json"
        self.env = {"HOME": str(self.root)}
        self.api = SimpleNamespace(strip_agentsdock_provider_context=lambda text, **kw: text,
            strip_agentsdock_generated_user_text=lambda text, **kw: text, native_session_title=lambda value: value,
            local_session_label=lambda value, fallback: value or fallback, iso_from_timestamp=lambda value: str(value))
        self.snapshot = {"sessionId": ID, "projectHash": hashlib.sha256(str(self.project).encode()).hexdigest(), "kind": "main",
            "summary": "Original native title", "messages": [
                {"id": "context", "type": "user", "content": "<session_context>internal"},
                {"id": "u", "type": "user", "content": [{"text": "First actual request"}]},
                {"id": "r", "type": "user", "content": [{"functionResponse": {"name": "read_file", "response": {"output": "private"}}}]},
                {"id": "a", "type": "gemini", "content": [{"text": "thinking", "thought": True}, {"text": "Answer"}]},
            ]}
        self.write()

    def write(self): self.path.write_text(json.dumps(self.snapshot))

    def test_native_title_and_first_user_fallback_are_read_only(self):
        before = self.path.read_bytes()
        rows = candidates(self.api, self.env, set())
        self.assertEqual(rows[0]["label"], "Original native title")
        self.assertEqual(rows[0]["cwd"], str(self.project))
        source, items = initial_history(self.api, self.env, ID, str(self.project))
        self.assertEqual(source, self.path)
        self.assertEqual([(item["kind"], item["text"]) for item in items], [("user", "First actual request"), ("assistant", "Answer")])
        self.assertEqual(self.path.read_bytes(), before)
        self.snapshot.pop("summary"); self.write()
        self.assertEqual(candidates(self.api, self.env, set())[0]["label"], "First actual request")

    def test_ownership_children_archive_missing_workspace_and_duplicates(self):
        self.assertFalse(candidates(self.api, self.env, {ID}))
        self.assertIn(("gemini", ID), provider_session_keys({"backend": "gemini", "session_id": ID}))
        for key, value in (("kind", "subagent"), ("archived", True), ("deleted", True), ("projectHash", "wrong")):
            original = dict(self.snapshot); self.snapshot[key] = value; self.write()
            self.assertFalse(candidates(self.api, self.env, set()))
            self.snapshot = original
        self.write()
        duplicate = self.chats / "session-copy.json"; duplicate.write_bytes(self.path.read_bytes())
        self.assertFalse(candidates(self.api, self.env, set()))
        with self.assertRaises(GeminiProtocolError): initial_history(self.api, self.env, ID, str(self.project))
        duplicate.unlink()
        (self.folder / ".project_root").unlink()
        self.assertFalse(candidates(self.api, self.env, set()), "must not infer cwd from directory slug")

    def test_one_corrupt_or_linked_file_does_not_hide_good_chat(self):
        (self.chats / "session-bad.json").write_text("{corrupt")
        (self.chats / "session-link.json").symlink_to(self.path)
        self.assertEqual(len(candidates(self.api, self.env, set())), 1)
