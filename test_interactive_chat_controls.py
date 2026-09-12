"""Actual native request models extracted as AST; no server import or execution."""
import ast
from pathlib import Path
from typing import Any, Literal
import unittest
from unittest.mock import AsyncMock

from pydantic import BaseModel, Field
from interactive_chat_controls import ACTIONS, ChatControlError, InteractiveChatControls


def native_models():
    names = {"UpdateQueuedTurnRequest", "MoveQueuedTurnRequest", "RunQueuedTurnNowRequest",
             "UpdateSessionRequest", "CodexGoalRequest", "JobCreateFields", "CreateScopedJobRequest",
             "UpdateJobRequest", "CodexInteractionResponseRequest", "ClaudeInteractionResponseRequest"}
    source = Path(__file__).with_name("agent_server.py")
    nodes = [node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef) and node.name in names]
    assert {node.name for node in nodes} == names
    namespace = dict(BaseModel=BaseModel, Field=Field, Any=Any, Literal=Literal,
        SkillSelection=Any, ChatReference=Any, TeamReference=Any,
        MAX_SESSION_SYSTEM_PROMPT_CHARS=24000, MAX_JOB_TITLE_CHARS=200,
        MAX_JOB_PROMPT_CHARS=24000, MAX_CODEX_GOAL_CHARS=4000)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), namespace)
    return {name: namespace[name] for name in names}


class InteractiveChatControlTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = native_models()

    def setUp(self):
        self.callbacks = {name: AsyncMock(return_value={"native": name}) for name in
                          ACTIONS | {"approval.codex", "approval.claude"}}
        self.adapter = InteractiveChatControls(self.callbacks, self.models)
        self.trusted = {"share_id": "interactive_" + "a" * 32, "request_id": "request_0000000001"}

    async def test_every_action_calls_only_its_fixed_native_function_with_exact_chat(self):
        cases = {
            "state": {}, "turn.stop": {}, "turn.steer": {"prompt": "Keep working with this clarification"},
            "queue.run_now": {"id": "queued-one"}, "queue.delete": {"id": "queued-one"},
            "queue.edit": {"id": "queued-one", "prompt": "Changed", "expected_message_revision": 2},
            "queue.move": {"id": "queued-one", "direction": "up", "expected_adjacent_queued_id": "queued-before"},
            "settings.update": {"model": "synthetic-model", "system_prompt": "Synthetic instruction", "codex_sandbox_mode": "read-only"},
            "goal.set": {"objective": "Synthetic goal", "status": "active", "token_budget": 10},
            "goal.resume": {}, "goal.pause": {}, "goal.delete": {},
            "job.create": {"title": "Synthetic cron", "prompt": "Summarize", "schedule_kind": "interval", "interval_seconds": 60},
            "job.update": {"id": "job-one", "prompt": "Updated"},
            "job.toggle": {"id": "job-one", "enabled": False}, "job.delete": {"id": "job-one"}, "job.run": {"id": "job-one"},
            "approval.respond": {"id": "approval-one", "backend": "codex", "response": {"decision": "accept"}},
        }
        self.assertEqual(set(cases), ACTIONS)
        for action, payload in cases.items():
            with self.subTest(action=action):
                key = "approval.codex" if action == "approval.respond" else action
                self.assertEqual(await self.adapter.dispatch("owner-chat", action, payload, **self.trusted), {"native": key})
                self.assertEqual(self.callbacks[key].await_args.args[0], "owner-chat")
        await self.adapter.dispatch("owner-chat", "approval.respond", {"id": "approval-two", "backend": "claude", "response": {"decision": "decline"}})
        self.callbacks["approval.claude"].assert_awaited_once()
        self.assertEqual(self.callbacks["turn.steer"].await_args.kwargs, self.trusted)
        self.assertTrue(self.callbacks["queue.run_now"].await_args.args[2].accept_deferred_queue_response)

    async def test_no_files_paths_references_foreign_identity_or_generic_proxy(self):
        for action in ACTIONS:
            for field in ("session_id", "cwd", "file_ids", "chat_references", "team_references", "path", "method", "shared_chat_id"):
                with self.subTest(action=action, field=field), self.assertRaises(ChatControlError):
                    await self.adapter.dispatch("owner-chat", action, {field: "forbidden"}, **self.trusted)
        for action in ("files.list", "workspace.read", "terminal.input", "server.restart", "sessions.list"):
            with self.assertRaises(ChatControlError): await self.adapter.dispatch("owner-chat", action, {})
        self.assertTrue(all(callback.await_count == 0 for callback in self.callbacks.values()))

    async def test_native_models_preserve_omitted_fields_and_force_created_job_into_chat(self):
        payload = {"id": "queued-one", "prompt": "Edited", "expected_message_revision": 4}
        await self.adapter.dispatch("owner-chat", "queue.edit", payload)
        request = self.callbacks["queue.edit"].await_args.args[2]
        self.assertEqual(request.model_dump(exclude_unset=True), {"prompt": "Edited", "expected_message_revision": 4})
        self.assertEqual(payload["id"], "queued-one")
        await self.adapter.dispatch("owner-chat", "job.create", {"title": "Cron", "prompt": "Report"})
        request = self.callbacks["job.create"].await_args.args[1]
        self.assertEqual(request.model_dump(exclude_unset=True), {"title": "Cron", "prompt": "Report", "context_mode": "chat"})
        await self.adapter.dispatch("owner-chat", "job.update", {"id": "job-one", "enabled": False})
        self.assertEqual(self.callbacks["job.update"].await_args.args[2].model_dump(exclude_unset=True), {"enabled": False})

    async def test_malformed_or_oversized_inputs_fail_before_native_action(self):
        cases = [
            ("queue.edit", {"id": "queued-one", "prompt": "Text", "expected_message_revision": True}),
            ("queue.move", {"id": "queued-one", "direction": "sideways"}),
            ("queue.move", {"id": "queued-one", "direction": ["up"]}),
            ("queue.run_now", {"id": "../other"}), ("goal.set", {"token_budget": 0}),
            ("job.toggle", {"id": "job-one", "enabled": "false"}),
            ("settings.update", {"codex_sandbox_mode": "unknown"}),
            ("job.create", {"title": "Cron", "prompt": "Text", "context_mode": "standalone"}),
            ("approval.respond", {"id": "approval", "backend": "unknown", "response": {}}),
            ("settings.update", {}), ("goal.set", {"objective": "x" * 65537}),
            ("goal.set", {"token_budget": float("nan")}),
        ]
        for action, payload in cases:
            with self.subTest(action=action), self.assertRaises(ChatControlError):
                await self.adapter.dispatch("owner-chat", action, payload)
        with self.assertRaises(ChatControlError):
            await self.adapter.dispatch("owner-chat", "turn.steer", {"prompt": "No trusted attribution"})
        self.assertTrue(all(callback.await_count == 0 for callback in self.callbacks.values()))

    async def test_native_uncertainty_is_not_misreported_as_known_denial(self):
        failure = RuntimeError("Acceptance may already be committed")
        self.callbacks["job.run"].side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            await self.adapter.dispatch("owner-chat", "job.run", {"id": "job-one"})
        self.assertIs(caught.exception, failure)
        self.callbacks["job.run"].assert_awaited_once_with("owner-chat", "job-one")


if __name__ == "__main__":
    unittest.main()
