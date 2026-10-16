import hashlib
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from gemini_acp_client import GeminiProtocolError
from gemini_history import fingerprint, replay_snapshot, replay_text, verify_resume_snapshot, read_snapshot

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

    def test_native_tool_response_placeholder_is_fenced_not_imported(self):
        self.records.append({"id": "tool-result", "type": "user", "content": [
            {"functionResponse": {"name": "activate_skill", "response": {"output": "private skill body"}}}
        ]})
        self.write()
        result = replay_snapshot(self.env, self.cwd, SESSION)
        self.assertEqual(result[-1], fingerprint({"sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": "[Function Response: activate_skill]"}}))
        self.assertNotIn("private skill body", repr(result))
        self.assertEqual(replay_text(["prefix", {"text": " text"},
            {"functionCall": {"name": "write_file", "args": {}}, "thoughtSignature": "opaque"}]),
            "prefix text[Function Call: write_file]")

    def test_malformed_or_ambiguous_tool_parts_fail_closed(self):
        for value in [False, 0, {}, [[{"text": "nested"}]],
                      {"functionResponse": {"name": ""}},
                      {"functionCall": {"name": "call"}, "text": "ambiguous"}]:
            with self.subTest(value=value), self.assertRaises(GeminiProtocolError):
                replay_text(value)

    def test_resume_accepts_only_exact_native_tool_conversion(self):
        tool = {"id": "call-1", "name": "activate_skill", "args": {"name": "test"}, "result": "fixture output"}
        before = {**self.meta, "messages": [
            {"id": "context", "type": "user", "content": [{"text": "<session_context>context"}]},
            {"id": "u", "type": "user", "content": "use skill"},
            {"id": "a", "type": "gemini", "content": "", "toolCalls": [tool]},
            {"id": "final", "type": "gemini", "content": "done"},
        ]}
        after = deepcopy(before)
        after["messages"][1]["content"] = [{"text": "use skill"}]
        after["messages"][2]["content"] = [{"functionCall": {key: tool[key] for key in ("id", "name", "args")}}]
        after["messages"][3]["content"] = [{"text": "done"}]
        after["messages"].insert(3, {"id": "a_response", "type": "user", "timestamp": "native-generated",
            "content": [{"functionResponse": {"id": "call-1", "name": "activate_skill", "response": {"output": "fixture output"}}}]})
        after["messages"][2]["toolCalls"][0]["result"] = deepcopy(after["messages"][3]["content"])
        verify_resume_snapshot(before, after)
        verify_resume_snapshot(after, deepcopy(after))
        for change in ("extra", "user", "result", "tool", "workspace"):
            broken = deepcopy(after)
            if change == "extra": broken["messages"].append({"id": "other", "type": "user", "content": "external"})
            if change == "user": broken["messages"][1]["content"] = [{"text": "changed"}]
            if change == "result": broken["messages"][3]["content"][0]["functionResponse"]["response"]["output"] = "changed"
            if change == "tool": broken["messages"][2]["toolCalls"][0]["args"] = {"name": "other"}
            if change == "workspace": broken["projectHash"] = "other"
            with self.subTest(change=change), self.assertRaisesRegex(GeminiProtocolError, "changed during resume"):
                verify_resume_snapshot(before, broken)

    def test_resume_thought_conversion_keeps_exposed_thoughts_and_text(self):
        before = {**self.meta, "messages": [{"id": "a", "type": "gemini", "content": "answer",
                  "thoughts": [{"subject": "Plan", "description": "Exposed"}]}]}
        after = deepcopy(before)
        after["messages"][0]["content"] = [{"text": "**Plan** Exposed", "thought": True}, {"text": "answer"}]
        verify_resume_snapshot(before, after)
        self.assertEqual(replay_text(after["messages"][0]["content"]), "[Thought: true]answer")

    def test_stop_empty_model_record_is_removed_but_real_output_is_not(self):
        before = {**self.meta, "messages": [
            {"id": "u", "type": "user", "content": [{"text": "hello"}]},
            {"id": "a", "type": "gemini", "content": "", "thoughts": []},
        ]}
        after = deepcopy(before)
        after["messages"].pop()
        verify_resume_snapshot(before, after)
        before["messages"][1]["content"] = "Already exposed"
        with self.assertRaises(GeminiProtocolError):
            verify_resume_snapshot(before, after)

    def test_tool_result_patch_is_loaded_but_identity_patch_is_rejected(self):
        self.records.append({"$patch": {"updates": [{"id": "a", "toolCalls": [{"id": "tool-1", "result": "updated"}]}]}})
        self.write()
        self.assertEqual(read_snapshot(self.path)["messages"][1]["toolCalls"][0]["result"], "updated")
        self.records[-1]["$patch"]["updates"][0]["toolCalls"][0]["name"] = "different"
        self.write()
        with self.assertRaises(GeminiProtocolError):
            read_snapshot(self.path)

    def test_resume_accepts_only_leading_native_environment_header_refresh(self):
        before = {**self.meta, "messages": [
            {"id": "ctx", "type": "user", "content": [{"text": "<session_context>\nold tree\n</session_context>"}]},
            {"id": "u", "type": "user", "content": [{"text": "create file"}]},
        ]}
        after = deepcopy(before)
        after["messages"][0]["content"][0]["text"] = "<session_context>\nnew tree\n</session_context>"
        verify_resume_snapshot(before, after)
        for field, value in (("id", "replaced"), ("type", "gemini"), ("content", [{"text": "user changed"}])):
            changed = deepcopy(after); changed["messages"][0][field] = value
            with self.subTest(field=field), self.assertRaises(GeminiProtocolError): verify_resume_snapshot(before, changed)
        after["messages"][1]["content"] = [{"text": "another request"}]
        with self.assertRaises(GeminiProtocolError): verify_resume_snapshot(before, after)
