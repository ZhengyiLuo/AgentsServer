import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from gemini_acp_client import GeminiProtocolError
from gemini_history import fingerprint, replay_snapshot

SESSION = "12345678-1234-1234-1234-123456789abc"


class ReplaySnapshot(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = str(self.root / "workspace")
        self.env = {"HOME": str(self.root)}
        self.chats = self.root / ".gemini/tmp/project/chats"
        self.chats.mkdir(parents=True)
        self.path = self.chats / "session-fixture-12345678.jsonl"
        self.meta = {"sessionId": SESSION, "projectHash": hashlib.sha256(self.cwd.encode()).hexdigest()}
        self.records = [self.meta, {"id":"u", "type":"user", "content":[{"text":"Hello"}]},
            {"id":"a", "type":"gemini", "content":"Answer", "thoughts":[{"subject":"Plan", "description":"Exposed thought"}],
                "toolCalls":[{"id":"tool-1"}]}]

    def write(self):
        self.path.write_text("\n".join(json.dumps(row) for row in self.records) + "\n")

    def test_snapshot_counts_native_text_thoughts_tools_without_importing(self):
        self.write(); before = self.path.read_bytes()
        result = replay_snapshot(self.env, self.cwd, SESSION)
        self.assertEqual([row[0] for row in result], ["user_message_chunk", "agent_thought_chunk", "agent_message_chunk", "tool_call"])
        self.assertEqual(result[2], fingerprint({"sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"Answer"}}))
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_workspace_or_ambiguous_session_refused(self):
        self.write()
        with self.assertRaises(GeminiProtocolError): replay_snapshot(self.env, self.cwd + "other", SESSION)
        (self.chats / "session-copy-12345678.jsonl").write_bytes(self.path.read_bytes())
        with self.assertRaises(GeminiProtocolError): replay_snapshot(self.env, self.cwd, SESSION)

    def test_symlink_is_not_followed(self):
        self.write(); target = self.root / "original.jsonl"; self.path.rename(target); self.path.symlink_to(target)
        with self.assertRaises(GeminiProtocolError): replay_snapshot(self.env, self.cwd, SESSION)

    def test_unknown_patch_and_nontext_fail_closed(self):
        for row in [{"$patch":{"unknownFormat":True}}, {"id":"new", "type":"user", "content":[{"inlineData":{"data":"unread"}}]}]:
            self.records.append(row); self.write()
            with self.assertRaises(GeminiProtocolError): replay_snapshot(self.env, self.cwd, SESSION)
            self.records.pop()

    def test_native_patches_and_rewind_preserve_replay_order(self):
        self.records.extend([{"$patch":{"updates":[{"id":"a", "content":"Replacement"}]}}, {"$rewindTo":"a"}])
        self.write()
        self.assertEqual(len(replay_snapshot(self.env, self.cwd, SESSION)), 1)
        self.records.pop(); self.write()
        result = replay_snapshot(self.env, self.cwd, SESSION)
        self.assertEqual(result[2], fingerprint({"sessionUpdate":"agent_message_chunk", "content":{"type":"text", "text":"Replacement"}}))

    def test_metadata_message_reset_matches_native_order(self):
        self.records.append({"$set":{"messages":[{"id":"new", "type":"user", "content":"Restart"}]}})
        self.write()
        self.assertEqual(len(replay_snapshot(self.env, self.cwd, SESSION)), 1)
