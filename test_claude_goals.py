"""Native goal attachment projection; no provider/server import or network."""
import ast
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from claude_goals import ClaudeGoalProjection, ClaudeGoalHistoryNormalizer, is_claude_synthetic_no_response, MAX_GOAL_RECORD_BYTES


SESSION = "8a865fcc-fc32-4373-9524-7cc8e62cfbc2"
FORK = "c148bdf8-ce59-46a8-aeb5-923a3607468e"


def record(*, met=False, sentinel=False, second=0, **fields):
    return {"type": "attachment", "sessionId": SESSION, "isSidechain": False,
            "uuid": str(uuid4()), "timestamp": f"2026-09-22T23:45:{second:02}.051Z",
            "attachment": {"type": "goal_status", "condition": "Reply STAGE2",
                           "met": met, "sentinel": sentinel, **fields}}


def encode(value):
    return json.dumps(value).encode() + b"\n"


class ClaudeHistoryRootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "CLAUDE_PROJECTS_ROOT" for target in node.targets))
        cls.expression = compile(ast.Expression(assignment.value), "<claude-projects-root>", "eval")

    def root(self, environment):
        return eval(self.expression, {"os": SimpleNamespace(environ=environment), "Path": Path})

    def test_isolated_claude_config_home_locates_native_goal_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_home = Path(temporary) / "isolated-claude"
            projects = private_home / "projects"
            transcript = projects / "project" / f"{SESSION}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_bytes(encode(record(sentinel=True)) + encode(record(met=True, second=1)))
            resolved = self.root({"CLAUDE_CONFIG_DIR": str(private_home)})
            self.assertEqual(resolved, projects)
            projection = ClaudeGoalProjection(SESSION)
            projection.refresh(next(resolved.rglob(f"{SESSION}.jsonl")))
            self.assertTrue(projection.caught_up)
            self.assertEqual(projection.goal["status"], "achieved")

    def test_explicit_projects_root_overrides_config_home(self):
        self.assertEqual(self.root({"CLAUDE_PROJECTS_ROOT": "/explicit/transcripts",
            "CLAUDE_CONFIG_DIR": "/isolated/claude"}), Path("/explicit/transcripts"))

    def test_default_home_and_empty_overrides_remain_compatible(self):
        with patch.object(Path, "home", return_value=Path("/synthetic/home")):
            self.assertEqual(self.root({}), Path("/synthetic/home/.claude/projects"))
            self.assertEqual(self.root({"CLAUDE_CONFIG_DIR": "", "CLAUDE_PROJECTS_ROOT": ""}),
                             Path("/synthetic/home/.claude/projects"))


class ClaudeGoalProjectionTests(unittest.TestCase):
    def test_only_native_goal_parent_proves_xml_command_and_clear(self):
        tracker = ClaudeGoalHistoryNormalizer()
        parent = record(sentinel=True)
        command = {"type": "user", "sessionId": SESSION, "isSidechain": False,
                   "uuid": str(uuid4()), "parentUuid": parent["uuid"], "promptId": str(uuid4()),
                   "message": {"role": "user", "content": "<command-name>/goal</command-name>\n <command-message>goal</command-message>\n <command-args>Reply STAGE2</command-args>"}}
        self.assertIs(tracker.consume(command), command)
        tracker.consume(parent)
        self.assertEqual(tracker.consume(command)["message"]["content"], "/goal Reply STAGE2")
        foreign = {**command, "sessionId": FORK}
        self.assertIs(tracker.consume(foreign), foreign)
        cleared = record(met=True, sentinel=True)
        caveat = {**command, "uuid": str(uuid4()), "parentUuid": cleared["uuid"], "isMeta": True,
                  "message": {"role": "user", "content": "<local-command-caveat>Native local command</local-command-caveat>"}}
        clear = {**command, "parentUuid": caveat["uuid"], "message": {"role": "user", "content": command["message"]["content"].replace("Reply STAGE2", "clear")}}
        tracker.consume(cleared); tracker.consume(caveat)
        self.assertEqual(tracker.consume(clear)["message"]["content"], "/goal clear")
        clear["promptId"] = str(uuid4())
        self.assertIs(tracker.consume(clear), clear)

    def test_synthetic_no_response_is_not_a_model_answer(self):
        message = {"role": "assistant", "model": "<synthetic>", "usage": {"output_tokens": 0},
                   "content": [{"type": "text", "text": "No response requested."}]}
        self.assertTrue(is_claude_synthetic_no_response({"type": "assistant", "message": message}))
        self.assertFalse(is_claude_synthetic_no_response({"type": "assistant", "message": {**message, "model": "claude"}}))

    def test_native_evaluator_completion_and_clear_are_distinct(self):
        projection = ClaudeGoalProjection(SESSION)
        projection.consume(record(sentinel=True))
        started = projection.goal["set_at"]
        self.assertNotIn("iterations", projection.goal)
        pending = record(second=1, reason="Only STAGE1 exists")
        projection.consume(pending)
        self.assertFalse(projection.consume(pending))
        self.assertNotIn("iterations", projection.goal)
        projection.consume(record(met=True, second=2, iterations=2, durationMs=6465,
                                  tokens=400, reason="Both replies exist"))
        self.assertEqual(projection.goal, {
            "condition": "Reply STAGE2", "status": "achieved", "iterations": 2,
            "set_at": started, "last_reason": "Both replies exist",
            "duration_ms": 6465, "tokens": 400,
        })
        projection.consume(record(sentinel=True, second=3))
        projection.consume(record(met=True, sentinel=True, second=4))
        self.assertEqual(projection.goal["status"], "cleared")
        self.assertNotIn("iterations", projection.goal)
        self.assertNotIn("last_reason", projection.goal)

    def test_resumed_native_evaluator_count_is_not_a_historical_total(self):
        projection = ClaudeGoalProjection(SESSION)
        projection.consume(record(sentinel=True))
        for second in range(1, 4):
            projection.consume(record(second=second, reason="Still pending"))
            self.assertNotIn("iterations", projection.goal)
        projection.consume(record(met=True, second=4, iterations=1, tokens=42))
        self.assertEqual(projection.goal["iterations"], 1)
        projection.consume(record(met=True, sentinel=True, second=5))
        self.assertNotIn("iterations", projection.goal)
        self.assertNotIn("tokens", projection.goal)

    def test_only_exact_native_session_evidence_establishes_or_finishes_goal(self):
        projection = ClaudeGoalProjection(SESSION)
        for extra in [{"sessionId": FORK}, {"isSidechain": True}, {"isSidechain": None},
                      {"type": "user"}, {"uuid": "not-a-uuid"}, {"timestamp": "yesterday"}]:
            self.assertFalse(projection.consume({**record(sentinel=True), **extra}))
        self.assertFalse(projection.consume(record(met=True)))
        self.assertIsNone(projection.goal)
        projection.consume(record(sentinel=True, second=3))
        self.assertFalse(projection.consume(record(met=True, second=2)))
        self.assertFalse(projection.consume(record(met=True, second=4, condition="Different")))
        self.assertFalse(projection.consume(record(met="true", second=4)))
        self.assertEqual(projection.goal["status"], "active")

    def test_incremental_reads_partial_rows_and_bounded_unknown_state(self):
        projection = ClaudeGoalProjection(SESSION)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "native.jsonl"
            start = encode(record(sentinel=True))
            path.write_bytes(b'{}\n' * 100 + start[:-5])
            projection.refresh(path, max_bytes=50)
            self.assertFalse(projection.caught_up)
            self.assertIsNone(projection.goal)
            projection.refresh(path)
            self.assertTrue(projection.caught_up)
            self.assertIsNone(projection.goal)
            with path.open("ab") as stream:
                stream.write(start[-5:])
            self.assertTrue(projection.refresh(path))
            self.assertEqual(projection.goal["status"], "active")
            self.assertFalse(projection.refresh(path))
            self.assertEqual(projection.offset, path.stat().st_size)

    def test_oversized_rows_rewrites_and_forks_do_not_leak_goal_state(self):
        projection = ClaudeGoalProjection(SESSION)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "native.jsonl"
            path.write_bytes(b"x" * (MAX_GOAL_RECORD_BYTES + 20) + b"\n" + encode(record(sentinel=True)))
            projection.refresh(path, max_bytes=MAX_GOAL_RECORD_BYTES + 10)
            projection.refresh(path)
            self.assertEqual(projection.goal["status"], "active")
            # Same inode, same size, rewritten history is not an append.
            path.write_bytes(b" " * (path.stat().st_size - 1) + b"\n")
            self.assertTrue(projection.refresh(path))
            self.assertIsNone(projection.goal)
            path.write_bytes(encode(record(sentinel=True)))
            projection.refresh(path)
            self.assertEqual(projection.goal["status"], "active")
            projection.refresh(path, provider_session_id=FORK)
            self.assertIsNone(projection.goal)
            child = {**record(sentinel=True), "sessionId": FORK}
            with path.open("ab") as stream:
                stream.write(encode(child))
            projection.refresh(path)
            self.assertEqual(projection.goal["status"], "active")


if __name__ == "__main__":
    unittest.main()
