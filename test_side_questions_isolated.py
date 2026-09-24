"""Pure side-question tests: no server import, processes, network or live state."""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import re
import tempfile
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

import side_questions as side
import codex_provider


def event(kind, run="main", **kwargs):
    return {"type": kind, "run_id": run, **kwargs}


def side_history(question="Previous question", answer="Previous answer"):
    return [{"role": "user", "text": question}, {"role": "assistant", "text": answer}]


class ShutdownBudgetTests(unittest.TestCase):
    def test_side_question_cleanup_is_included_in_cooperative_restart_budget(self):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        lifespan = next(node for node in tree.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        phases = [node.value for node in ast.walk(lifespan)
                  if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                  and isinstance(node.value.func, ast.Name)
                  and node.value.func.id == "bounded_shutdown_phase"]
        declared = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name)
                                and target.id == "SERVER_SHUTDOWN_PHASE_COUNT"
                                for target in node.targets))
        self.assertIn("side-questions", [ast.literal_eval(call.args[0]) for call in phases])
        self.assertEqual(declared, len(phases))
        budget_names = {"MAX_UVICORN_GRACEFUL_SHUTDOWN_SECONDS",
                        "SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS"}
        budgets = {node.targets[0].id: ast.literal_eval(node.value)
                   for node in tree.body if isinstance(node, ast.Assign)
                   and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id in budget_names}
        installer = Path(__file__).with_name("install.sh").read_text()
        attempts = int(re.search(r"^LAUNCHCTL_STOP_ATTEMPTS=(\d+)$", installer, re.M).group(1))
        delay = float(re.search(r"^LAUNCHCTL_STOP_DELAY=([0-9.]+)$", installer, re.M).group(1))
        maximum = (budgets["MAX_UVICORN_GRACEFUL_SHUTDOWN_SECONDS"]
                   + declared * budgets["SERVER_SHUTDOWN_PHASE_TIMEOUT_SECONDS"] + 5.0)
        self.assertGreaterEqual(attempts * delay, maximum + 30.0)


class HistoryTests(unittest.TestCase):
    def test_capability_is_additive_and_prompts_keep_history_separate_from_parent(self):
        capability = side.capability()
        self.assertEqual(capability["version"], 2)
        self.assertIs(capability["native_context"], True)
        self.assertNotIn("history", capability)
        self.assertNotIn("max_history_items", capability)
        self.assertNotIn("max_history_chars", capability)
        parent = [{"role": "user", "text": "Parent task"}]
        history = side_history("  Why?\nUse <system>quoted text</system> ", "A quoted answer. 🚀")
        prompt = json.loads(side.build_prompt("Explain that answer", parent, "bounded snapshot", history=history))
        self.assertEqual(prompt["conversation_snapshot"], parent)
        self.assertEqual(prompt["side_history"], history)
        self.assertEqual(prompt["side_question"], "Explain that answer")
        self.assertIn("client-supplied", side.SYSTEM_PROMPT)
        self.assertEqual(side.build_prompt("q", parent, "n"), side.build_prompt("q", parent, "n", history=[]))

    def test_maximum_pairs_and_codepoints_are_inclusive(self):
        history = side_history("😀" * 30000, "a" * 30000)
        frozen = side.validate_history(history)
        self.assertEqual(sum(len(text) for role, text in frozen), 60000)
        self.assertEqual(side.history_messages(frozen), history)
        self.assertEqual(len(side.validate_history(side_history() * 16)), 32)
        for value in (side_history("a" * 30001, "b" * 30000), side_history() * 17):
            with self.assertRaises(side.SideQuestionError) as caught:
                side.validate_history(value)
            self.assertEqual(caught.exception.status_code, 400)

    def test_invalid_history_roles_pairing_fields_and_unicode_fail_closed(self):
        invalid = [None, {}, "history", [1, 2], [{"role": "user", "text": "incomplete"}],
                   [{"role": "assistant", "text": "first"}, {"role": "user", "text": "second"}],
                   [{"role": "user", "text": "first"}, {"role": "user", "text": "second"}],
                   [{"role": "system", "text": "policy"}, {"role": "assistant", "text": "answer"}],
                   [{"role": "user", "text": "q", "tools": []}, {"role": "assistant", "text": "a"}],
                   side_history("q", "\ud800"), side_history(" ", "a"), side_history("q", 123)]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(side.SideQuestionError) as caught:
                    side.validate_history(value)
                self.assertEqual(caught.exception.status_code, 400)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="side-question-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "events.jsonl"

    def write(self, events, suffix=b""):
        self.path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in events) + suffix)

    def snapshot(self, project=lambda value: value):
        return side.read_context_snapshot(self.path, project)

    def test_preserves_user_and_completed_assistant_deduplicates_final_and_excludes_execution(self):
        self.write([
            event("turn_started", prompt="Why blue?"),
            event("assistant_text", phase="final_answer", text="It scatters."),
            event("assistant_text", phase="analysis", text="hidden raw reasoning"),
            event("tool_started", tool={"name": "danger", "input": "private"}),
            event("reasoning_summary", text="private reasoning"),
            event("turn_finished", result_text="It scatters."),
            event("turn_queued", prompt="queued instructions"),
            event("turn_started", run="job", purpose="scheduled_job", job_id="cron", prompt="auto"),
            event("assistant_text", run="job", text="job output"),
            event("turn_started", run="next", prompt="And red?"),
            event("reasoning_summary", run="next", phase="commentary", text="Checking the comparison."),
        ], b'{"type":"assistant_text","text":"partial')
        messages, note = self.snapshot()
        self.assertEqual(messages, [
            {"role": "user", "text": "Why blue?"}, {"role": "assistant", "text": "It scatters."},
            {"role": "user", "text": "And red?"},
            {"role": "assistant", "text": "Checking the comparison."},
        ])
        self.assertIn("excludes tool results", note)
        self.assertNotIn("Older text was omitted", note)

    def test_projector_removes_authority_and_hidden_import_segments(self):
        self.write([event("turn_started", prompt="user text AUTHORITY"),
                    event("assistant_text", text="answer"),
                    event("turn_started", prompt="hidden provider wake"),
                    event("assistant_text", text="hidden wake answer"),
                    event("turn_started", prompt="human question"),
                    event("assistant_text", text="human answer")])
        def project(row):
            if row.get("prompt") == "hidden provider wake":
                return {**row, "prompt": ""}
            return {**row, "prompt": str(row.get("prompt", "")).replace(" AUTHORITY", "")}
        messages, _ = self.snapshot(project)
        self.assertEqual([message["text"] for message in messages],
                         ["user text", "answer", "human question", "human answer"])

    def test_tail_requires_new_boundary_and_discloses_truncation(self):
        self.write([event("turn_started", prompt="old"), event("assistant_text", text="x" * 500),
                    event("assistant_text", text="unproven mid-run"),
                    event("turn_started", run="latest", prompt="latest question"),
                    event("assistant_text", run="latest", text="latest answer")])
        with patch.object(side, "MAX_LOG_BYTES", 390):
            messages, note = self.snapshot()
        self.assertEqual([message["text"] for message in messages], ["latest question", "latest answer"])
        self.assertIn("Older text was omitted", note)

    def test_context_budget_keeps_latest_message_and_labels_trim(self):
        self.write([event("turn_started", prompt="question"), event("assistant_text", text="abcdef" * 10)])
        with patch.object(side, "MAX_CONTEXT_CHARS", 20):
            messages, note = self.snapshot()
        self.assertEqual(messages[0]["text"], "[Beginning omitted]\n" + ("abcdef" * 10)[-20:])
        self.assertIn("Older text was omitted", note)

    def test_empty_context_fails_without_fake_prompt(self):
        self.write([event("turn_queued", prompt="not accepted"), event("assistant_text", text="orphan")])
        with self.assertRaises(side.SideQuestionError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.status_code, 409)

    def test_environment_strips_authority_without_replacing_auth_home(self):
        env = {"HOME": "/synthetic/auth-home", "PATH": "/bin", "ANTHROPIC_API_KEY": "synthetic",
               "AGENTSDOCK_CHAT_ID": "parent", "AGENTSDOCK_TEAM_AUTHORITY": "secret",
               "ZENITHBOT_AGENT_TOKEN": "secret", "CODEX_THREAD_ID": "parent",
               "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1", "TMUX": "parent"}
        self.assertEqual(side.isolated_environment(env),
                         {"HOME": "/synthetic/auth-home", "PATH": "/bin", "ANTHROPIC_API_KEY": "synthetic"})


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_frozen_for_receipt_identity_and_provider_input(self):
        answer = AsyncMock(return_value={"answer": "follow-up"})
        runtime = side.SideQuestions(answer)
        self.addAsyncCleanup(runtime.close)
        history = side_history()
        original = side_history()
        receipt = runtime.submit("owner", "chat", "id", "Why?", history=history)
        history[0]["text"] = "mutated by caller"
        history.append({"role": "user", "text": "new unpaired message"})
        self.assertEqual(await receipt.task, {"answer": "follow-up"})
        answer.assert_awaited_once_with("chat", "Why?", history=original)
        answer.await_args.kwargs["history"][1]["text"] = "mutated by provider callback"
        self.assertIs(runtime.submit("owner", "chat", "id", "Why?", history=original), receipt)
        for changed in (side_history(answer="Different answer"), []):
            with self.assertRaises(side.SideQuestionError) as caught:
                runtime.submit("owner", "chat", "id", "Why?", history=changed)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_empty_and_omitted_history_replay_same_legacy_callback(self):
        answer = AsyncMock(return_value={"answer": "legacy"})
        runtime = side.SideQuestions(answer)
        self.addAsyncCleanup(runtime.close)
        receipt = runtime.submit("o", "s", "id", "q")
        self.assertIs(runtime.submit("o", "s", "id", "q", history=[]), receipt)
        await receipt.task
        answer.assert_awaited_once_with("s", "q")

    async def test_duplicate_identity_coalesces_and_changed_question_conflicts(self):
        ready = asyncio.Event()
        calls = []
        async def answer(sid, question):
            calls.append((sid, question))
            await ready.wait()
            return {"answer": "one"}
        runtime = side.SideQuestions(answer)
        first = runtime.submit("owner", "chat", "request", "question")
        self.assertIs(runtime.submit("owner", "chat", "request", "question"), first)
        with self.assertRaises(side.SideQuestionError):
            runtime.submit("owner", "chat", "request", "changed")
        ready.set()
        self.assertEqual(await first.task, {"answer": "one"})
        self.assertEqual(calls, [("chat", "question")])
        self.assertIs(runtime.submit("owner", "chat", "request", "question"), first)
        await runtime.close()

    async def test_cancel_only_exact_owner_session_request_and_reaps_before_return(self):
        entered = asyncio.Event()
        reaped = asyncio.Event()
        async def answer(sid, question):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                reaped.set()
        runtime = side.SideQuestions(answer)
        receipt = runtime.submit("owner", "chat", "request", "q")
        await entered.wait()
        self.assertEqual(await runtime.cancel("other", "chat", "request"), "not_found")
        self.assertEqual(await runtime.cancel("owner", "other", "request"), "not_found")
        self.assertFalse(receipt.task.done())
        self.assertEqual(await runtime.cancel("owner", "chat", "request"), "cancelled")
        self.assertTrue(reaped.is_set())
        self.assertTrue(receipt.task.cancelled())
        await runtime.close()

    async def test_delete_overtaking_post_fences_delayed_request(self):
        answer = AsyncMock()
        runtime = side.SideQuestions(answer)
        self.assertEqual(await runtime.cancel("owner", "chat", "request"), "not_found")
        with self.assertRaises(side.SideQuestionError) as caught:
            runtime.submit("owner", "chat", "request", "delayed")
        self.assertEqual(caught.exception.status_code, 409)
        answer.assert_not_awaited()

    async def test_failure_replays_without_second_provider_call_and_shutdown_cancels(self):
        answer = AsyncMock(side_effect=side.SideQuestionError(503, "unavailable"))
        runtime = side.SideQuestions(answer)
        receipt = runtime.submit("o", "s", "r", "q")
        with self.assertRaises(side.SideQuestionError):
            await receipt.task
        self.assertIs(runtime.submit("o", "s", "r", "q"), receipt)
        answer.assert_awaited_once()
        await runtime.close()
        self.assertEqual(runtime.receipts, {})


class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handles = []
        def create(_session_id):
            handle = SimpleNamespace(ask=AsyncMock(return_value={
                "backend": "claude", "answer": "Native answer", "context_note": "Native context"}),
                close=AsyncMock())
            self.handles.append(handle)
            return handle
        self.factory = AsyncMock(side_effect=create)
        self.runtime = side.SideQuestions(native_factory=self.factory)
        self.addAsyncCleanup(self.runtime.close)

    def submit(self, request_id="first", question="Question?", *, owner="owner", session_id="chat",
               side_chat_id="panel", after_request_id=None, **kwargs):
        return self.runtime.submit(owner, session_id, request_id, question,
            side_chat_id=side_chat_id, after_request_id=after_request_id, **kwargs)

    async def test_long_native_answer_keeps_context_for_followup(self):
        await self.submit().task
        started, release = asyncio.Event(), asyncio.Event()

        async def waiting(*args, **kwargs):
            started.set()
            await release.wait()
            return {"answer": "Long answer completed"}

        handle = self.handles[0]
        handle.ask.side_effect = waiting
        loop = asyncio.get_running_loop()
        clock = loop.time
        elapsed = 0
        with patch.object(loop, "slow_callback_duration", 1000), \
                patch.object(loop, "time", side_effect=lambda: clock() + elapsed):
            active = self.submit("long", "Think carefully", after_request_id="first")
            await started.wait()
            # Advance the actual loop deadlines beyond the former 150-second
            # cap. The provider remains active until it chooses to answer.
            elapsed = 151
            await asyncio.sleep(0.01)
            self.assertFalse(active.task.done())
            handle.close.assert_not_awaited()
            self.assertIs(self.submit("long", "Think carefully", after_request_id="first"), active)
            release.set()
            self.assertEqual((await active.task)["answer"], "Long answer completed")
            handle.ask.side_effect = None
            await self.submit("followup", "Explain that", after_request_id="long").task
            self.factory.assert_awaited_once_with("chat")
            self.assertEqual(handle.ask.await_args.kwargs["history"][-1], {
                "question": "Think carefully", "response": "Long answer completed"})
            handle.close.assert_not_awaited()

    async def test_native_dedup_cursor_and_server_owned_history(self):
        first = self.submit()
        self.assertIs(self.submit(), first)
        await first.task
        second = self.submit("second", "Follow-up?", after_request_id="first")
        await second.task
        self.factory.assert_awaited_once_with("chat")
        self.handles[0].ask.assert_awaited_with("Follow-up?", history=[{
            "question": "Question?", "response": "Native answer"}])
        stale = self.submit("stale", "Stale question", after_request_id="first")
        with self.assertRaises(side.SideQuestionError) as caught:
            await stale.task
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.handles[0].ask.await_count, 2)
        self.handles[0].close.assert_not_awaited()
        with self.assertRaises(side.SideQuestionError):
            self.submit("second", "Follow-up?", after_request_id="different")

    async def test_native_rejects_client_history_and_missing_panel_before_factory(self):
        for kwargs in ({"history": side_history()}, {"side_chat_id": None}):
            with self.assertRaises(side.SideQuestionError) as caught:
                self.submit(**kwargs)
            self.assertEqual(caught.exception.status_code, 409)
        self.factory.assert_not_awaited()

    async def test_native_retains_newest_twenty_complete_exchanges(self):
        previous = None
        for index in range(23):
            request_id = f"r{index}"
            await self.submit(request_id, f"q{index}", after_request_id=previous).task
            previous = request_id
        history = self.handles[0].ask.await_args.kwargs["history"]
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]["question"], "q2")
        self.assertEqual(history[-1]["question"], "q21")

    async def test_native_clear_overtakes_post_and_never_resurrects_closed_panel(self):
        await self.runtime.close_conversation("owner", "chat", "panel")
        delayed = self.submit()
        with self.assertRaises(side.SideQuestionError) as caught:
            await delayed.task
        self.assertEqual(caught.exception.status_code, 410)
        self.factory.assert_not_awaited()
        await self.submit("fresh", side_chat_id="new-panel").task
        self.factory.assert_awaited_once()

    async def test_native_owner_and_parent_separation(self):
        requests = [self.submit(), self.submit(owner="other"), self.submit(session_id="other-chat")]
        await asyncio.gather(*(request.task for request in requests))
        self.assertEqual(len(self.handles), 3)
        await self.runtime.close_conversation("owner", "chat", "panel")
        self.handles[0].close.assert_awaited_once()
        self.handles[1].close.assert_not_awaited()
        self.handles[2].close.assert_not_awaited()
        await self.submit("followup", owner="other", after_request_id="first").task

    async def test_native_cancel_closes_only_exact_panel_and_rejects_stale_followup(self):
        await self.submit().task
        started = asyncio.Event()
        async def waiting(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.handles[0].ask.side_effect = waiting
        loop = asyncio.get_running_loop()
        clock = loop.time
        elapsed = 0
        with patch.object(loop, "slow_callback_duration", 1000), \
                patch.object(loop, "time", side_effect=lambda: clock() + elapsed):
            active = self.submit("active", after_request_id="first")
            await started.wait()
            elapsed = 151
            await asyncio.sleep(0.01)
            self.assertFalse(active.task.done())
            await self.runtime.cancel("owner", "chat", "active")
        self.assertTrue(active.task.cancelled())
        self.handles[0].close.assert_awaited_once()
        late = self.submit("late", after_request_id="first")
        with self.assertRaises(side.SideQuestionError) as caught:
            await late.task
        self.assertEqual(caught.exception.status_code, 410)

    async def test_native_busy_rejection_does_not_cancel_original(self):
        await self.submit().task
        started, release = asyncio.Event(), asyncio.Event()
        async def waiting(*args, **kwargs):
            started.set()
            await release.wait()
            return {"answer": "Finished"}
        self.handles[0].ask.side_effect = waiting
        active = self.submit("active", after_request_id="first")
        await started.wait()
        competing = self.submit("competing", after_request_id="first")
        with self.assertRaises(side.SideQuestionError) as caught:
            await competing.task
        self.assertEqual(caught.exception.status_code, 409)
        self.handles[0].close.assert_not_awaited()
        self.assertFalse(active.task.done())
        release.set()
        self.assertEqual((await active.task)["answer"], "Finished")

    async def test_native_idle_expiry_closes_handle_and_followup_cannot_create_new_context(self):
        await self.submit().task
        key = ("owner", "chat", "panel")
        conversation = self.runtime.conversations[key]
        self.runtime._expire_conversation(key, conversation)
        await asyncio.gather(*tuple(self.runtime.cleanup_tasks))
        self.handles[0].close.assert_awaited_once()
        self.assertNotIn(key, self.runtime.conversations)
        stale = self.submit("followup", after_request_id="first")
        with self.assertRaises(side.SideQuestionError) as caught:
            await stale.task
        self.assertEqual(caught.exception.status_code, 410)
        self.factory.assert_awaited_once()

    async def test_native_concurrent_clear_joins_cleanup_even_after_first_waiter_disconnects(self):
        await self.submit().task
        started, release = asyncio.Event(), asyncio.Event()
        async def closing():
            started.set()
            await release.wait()
        self.handles[0].close.side_effect = closing
        first = asyncio.create_task(self.runtime.close_conversation("owner", "chat", "panel"))
        await started.wait()
        second = asyncio.create_task(self.runtime.close_conversation("owner", "chat", "panel"))
        await asyncio.sleep(0)
        self.assertFalse(second.done(), "Concurrent clear returned before native child cleanup")
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        release.set()
        await second
        self.handles[0].close.assert_awaited_once()

    async def test_native_clear_fences_factory_that_finishes_after_cancellation(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        handle = SimpleNamespace(ask=AsyncMock(return_value={"answer": "Too late"}), close=AsyncMock())
        async def slow_factory(_session_id):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return handle
        self.factory.side_effect = slow_factory
        receipt = self.submit()
        await started.wait()
        clearing = asyncio.create_task(self.runtime.close_conversation("owner", "chat", "panel"))
        await cancelled.wait()
        release.set()
        await clearing
        self.assertTrue(receipt.task.done())
        handle.ask.assert_not_awaited()
        handle.close.assert_awaited_once()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0
        self.inputs = []
        async def answer(sid, question, *, history=None):
            self.calls += 1
            self.inputs.append({"session_id": sid, "question": question, "history": history})
            self.started.set()
            try:
                await self.finish.wait()
                return {"backend": "claude", "answer": "Contextual answer", "context_note": "Recent text"}
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        self.runtime = side.SideQuestions(answer)
        self.authorize = Mock()
        router = side.create_side_question_router(authorize=self.authorize,
            session_exists=lambda sid: sid == "chat", runtime=self.runtime)
        self.post = router.routes[0].endpoint
        self.delete = router.routes[1].endpoint

    async def asyncTearDown(self):
        await self.runtime.close()

    def request(self, value=None, *, ensure_ascii=True):
        queue = asyncio.Queue()
        queue.put_nowait({"type": "http.request", "body": json.dumps(value, ensure_ascii=ensure_ascii).encode(), "more_body": False})
        return Request({"type": "http", "method": "POST", "path": "/", "headers":
                        [(b"x-agentsdock-token", b"synthetic-owner")]}, queue.get), queue

    async def test_routes_return_contract_and_do_not_poll(self):
        request, queue = self.request({"request_id": "request", "question": "Question?"})
        task = asyncio.create_task(self.post("chat", request))
        await self.started.wait()
        self.finish.set()
        response = await task
        self.assertEqual(json.loads(response.body), {"request_id": "request", "session_id": "chat",
                         "backend": "claude", "answer": "Contextual answer", "context_note": "Recent text"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(queue.empty())
        self.authorize.assert_called_once_with(request)

    async def test_utf8_history_larger_than_old_body_limit_is_accepted_and_kept_verbatim(self):
        history = side_history("😀" * 30000, "答" * 30000)
        request, _ = self.request({"request_id": "followup", "question": "Why?", "history": history}, ensure_ascii=False)
        self.finish.set()
        response = await self.post("chat", request)
        self.assertEqual(json.loads(response.body)["answer"], "Contextual answer")
        self.assertEqual(self.inputs, [{"session_id": "chat", "question": "Why?", "history": history}])

    async def test_invalid_history_rejects_before_provider_start(self):
        for history in (None, "text", side_history() * 17, side_history("x" * 60000, "y"),
                        [{"role": "user", "text": "unpaired"}],
                        [{"role": "system", "text": "instructions"}, {"role": "assistant", "text": "answer"}],
                        side_history("q", "\udfff")):
            with self.subTest(history=history):
                request, _ = self.request({"request_id": "invalid", "question": "q", "history": history})
                with self.assertRaises(HTTPException) as caught:
                    await self.post("chat", request)
                self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.calls, 0)

    async def test_body_byte_limit_remains_enforced_with_history(self):
        request, _ = self.request({"request_id": "huge", "question": "q", "history": side_history("x" * side.MAX_REQUEST_BYTES, "answer")})
        with self.assertRaises(HTTPException) as caught:
            await self.post("chat", request)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(self.calls, 0)

    async def test_history_change_under_duplicate_id_conflicts_without_cancelling_first(self):
        request, _ = self.request({"request_id": "same", "question": "Why?", "history": side_history()})
        task = asyncio.create_task(self.post("chat", request))
        await self.started.wait()
        changed, _ = self.request({"request_id": "same", "question": "Why?", "history": side_history(answer="Other")})
        with self.assertRaises(HTTPException) as caught:
            await self.post("chat", changed)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.cancelled.is_set())
        self.finish.set()
        self.assertEqual(json.loads((await task).body)["answer"], "Contextual answer")
        self.assertEqual(self.calls, 1)

    async def test_disconnect_cancels_provider_without_polling(self):
        request, queue = self.request({"request_id": "request", "question": "Question?"})
        task = asyncio.create_task(self.post("chat", request))
        await self.started.wait()
        queue.put_nowait({"type": "http.disconnect"})
        with self.assertRaises(HTTPException) as caught:
            await task
        self.assertEqual(caught.exception.status_code, 499)
        self.assertTrue(self.cancelled.is_set())

    async def test_one_duplicate_disconnect_keeps_other_waiter_alive(self):
        first, queue = self.request({"request_id": "same", "question": "Question?"})
        second, _ = self.request({"request_id": "same", "question": "Question?"})
        task1 = asyncio.create_task(self.post("chat", first))
        await self.started.wait()
        task2 = asyncio.create_task(self.post("chat", second))
        while next(iter(self.runtime.receipts.values())).waiters != 2:
            await asyncio.sleep(0)
        queue.put_nowait({"type": "http.disconnect"})
        with self.assertRaises(HTTPException):
            await task1
        self.assertFalse(self.cancelled.is_set())
        self.finish.set()
        self.assertEqual(json.loads((await task2).body)["answer"], "Contextual answer")
        self.assertEqual(self.calls, 1)

    async def test_delete_cancels_post_and_is_scoped(self):
        request, _ = self.request({"request_id": "request", "question": "Question?"})
        task = asyncio.create_task(self.post("chat", request))
        await self.started.wait()
        receipt = await self.delete("chat", "request", request)
        self.assertEqual(json.loads(receipt.body), {"request_id": "request", "status": "cancelled"})
        with self.assertRaises(HTTPException) as caught:
            await task
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.cancelled.is_set())

    async def test_auth_and_shape_fail_before_provider_start(self):
        for payload in ({"request_id": "x", "question": ""},
                        {"request_id": "x", "question": "a" * 8001},
                        {"request_id": "x", "question": "\ud800"},
                        {"request_id": "../parent", "question": "q"},
                        {"request_id": "x", "question": "q", "tools": ["Bash"]}):
            request, _ = self.request(payload)
            with self.assertRaises(HTTPException) as caught:
                await self.post("chat", request)
            self.assertEqual(caught.exception.status_code, 400)
        self.authorize.side_effect = HTTPException(401, "unauthorized")
        request, _ = self.request({"request_id": "x", "question": "q"})
        with self.assertRaises(HTTPException) as caught:
            await self.post("chat", request)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(self.calls, 0)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        signals = patch.object(side.os, "killpg")
        self.killpg = signals.start()
        self.addCleanup(signals.stop)

    async def test_claude_has_empty_tools_safe_mode_and_no_persistence(self):
        help_text = "--safe-mode --tools --no-session-persistence --strict-mcp-config --name"
        runner = AsyncMock(side_effect=[help_text, '{"result":"A contextual answer","is_error":false}'])
        with patch.object(side, "run_isolated_command", runner), patch.object(side, "_CLAUDE_SUPPORTED", set()):
            answer = await side.answer_claude("snapshot", executable="synthetic-claude", model="sonnet",
                                              env={"HOME": "/synthetic", "AGENTSDOCK_CHAT_ID": "main"})
        self.assertEqual(answer, "A contextual answer")
        command = runner.await_args.args[0]
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--name") + 1], "Side question")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertNotIn("--resume", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("--fork-session", command)
        self.assertNotIn("AGENTSDOCK_CHAT_ID", runner.await_args.kwargs["env"])
        self.assertFalse(Path(runner.await_args.kwargs["cwd"]).exists())

    async def test_old_claude_fails_closed(self):
        runner = AsyncMock(return_value="--tools")
        with patch.object(side, "run_isolated_command", runner), patch.object(side, "_CLAUDE_SUPPORTED", set()):
            with self.assertRaises(side.SideQuestionError) as caught:
                await side.answer_claude("q", executable="old-claude", model=None, env={})
        self.assertEqual(caught.exception.status_code, 503)
        runner.assert_awaited_once()

    async def test_owned_process_reads_chunked_output_to_eof(self):
        proc = fake_process([b'{"res', b'ult":"ok"}', b''], [b'warning', b''])
        with patch.object(side.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            output = await side.run_isolated_command(["fake"], prompt="q", cwd="/synthetic", env={})
        self.assertEqual(output, '{"result":"ok"}')
        self.assertEqual(proc.stdout.read.await_count, 3)
        self.assertEqual(proc.stderr.read.await_count, 2)

    async def test_legacy_answer_process_has_no_default_elapsed_time_cutoff(self):
        started, release = asyncio.Event(), asyncio.Event()
        proc = fake_process([], [b""])

        async def read(_):
            started.set()
            await release.wait()
            proc.stdout.read = AsyncMock(return_value=b"")
            return b"Long answer"

        proc.stdout.read = read
        loop = asyncio.get_running_loop()
        clock = loop.time
        elapsed = 0
        with patch.object(side.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(loop, "slow_callback_duration", 1000), \
                patch.object(loop, "time", side_effect=lambda: clock() + elapsed):
            task = asyncio.create_task(side.run_isolated_command(
                ["fake"], prompt="q", cwd="/synthetic", env={}))
            self.addAsyncCleanup(self._cancel_task, task)
            await started.wait()
            elapsed = 151
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.killpg.assert_not_called()
            release.set()
            self.assertEqual(await task, "Long answer")

    async def _cancel_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_output_limit_is_enforced_across_chunks(self):
        proc = fake_process([b'1234', b'5678', b''], [b''])
        with patch.object(side.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch.object(side, "MAX_OUTPUT_BYTES", 6):
            with self.assertRaises(side.SideQuestionError) as caught:
                await side.run_isolated_command(["fake"], prompt="q", cwd="/synthetic", env={})
        self.assertEqual(caught.exception.status_code, 502)

    async def test_cancellation_during_spawn_joins_and_kills_only_owned_process(self):
        started, release = asyncio.Event(), asyncio.Event()
        proc = fake_process([b''], [b''])
        async def spawn(*args, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            started.set()
            await release.wait()
            return proc
        with patch.object(side.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(side.run_isolated_command(["fake"], prompt="q", cwd="/synthetic", env={}))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({call.args[0] for call in self.killpg.call_args_list}, {proc.pid})
        self.assertIsNotNone(proc.returncode)

    async def test_exited_leader_with_child_pipe_is_killed_on_cancellation(self):
        reading = asyncio.Event()
        proc = fake_process([], [b''])
        proc.returncode = 0
        async def read(_):
            reading.set()
            await asyncio.Event().wait()
        proc.stdout.read = read
        with patch.object(side.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(side.run_isolated_command(["fake"], prompt="q", cwd="/synthetic", env={}))
            await reading.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({call.args[0] for call in self.killpg.call_args_list}, {proc.pid})
        self.assertEqual(self.killpg.call_count, 2)


def fake_process(stdout, stderr):
    proc = SimpleNamespace(pid=123456, returncode=None,
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
        stdout=SimpleNamespace(read=AsyncMock(side_effect=stdout)),
        stderr=SimpleNamespace(read=AsyncMock(side_effect=stderr)))
    async def wait():
        proc.returncode = 0
        return 0
    proc.wait = AsyncMock(side_effect=wait)
    return proc


class ServerGlueTests(unittest.TestCase):
    def test_native_glue_has_no_snapshot_main_turn_or_state_writes(self):
        source = Path(__file__).with_name("agent_server.py")
        tree = ast.parse(source.read_text(), filename=str(source))
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "create_native_side_chat"]
        self.assertEqual(len(selected), 1)
        text = "\n".join(ast.unparse(node) for node in selected)
        for forbidden in ("ACTIVE", "QUEUED_TURNS", "BUSY_SESSIONS", "post_turn", "emit(", "save(",
                          "provider_authority", "goal/", "resume_thread", "fork_thread",
                          "read_context_snapshot", "conversation_snapshot", "build_prompt",
                          "side_questions.answer_claude", "answer_codex_side_question"):
            self.assertNotIn(forbidden, text)
        self.assertIn("manager.ask_side_question", text)
        self.assertIn("NativeCodexSideChat", text)
        health = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "health")
        self.assertIn("'side_questions': side_questions.capability()", ast.unparse(health))


class SideApprovalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_side_approval_uses_existing_validator_without_registering_parent(self):
        source = Path(__file__).with_name("agent_server.py")
        names = {"handle_codex_server_request", "public_codex_interaction", "validate_codex_interaction_response",
                 "resolve_codex_interaction", "finish_codex_interaction_locked", "cancel_codex_interactions"}
        selected = [node for node in ast.parse(source.read_text()).body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        requested = asyncio.Event()
        async def append(*args):
            if args[1] == "codex_interaction_requested":
                requested.set()
        registry = {}
        no_parent = Mock(side_effect=AssertionError("Side fork must not use the parent registry"))
        scope = dict(asyncio=asyncio, uuid=uuid, suppress=suppress, HTTPException=HTTPException,
            CODEX_INTERACTION_METHODS={"item/commandExecution/requestApproval"},
            CODEX_PENDING_INTERACTIONS=registry, CODEX_PENDING_INTERACTIONS_LOCK=asyncio.Lock(),
            CODEX_INTERACTION_HANDLER_TASKS={}, CODEX_APPROVAL_ITEM_CACHE={}, MAX_CODEX_PENDING_INTERACTIONS=8,
            DELETING_SESSIONS=set(), DELETED_SESSION_TOMBSTONES=set(),
            session_lifecycle_lock=lambda sid: asyncio.Lock(),
            codex_session_id_for_thread=no_parent, codex_request_is_interactive=no_parent,
            existing_codex_app_server_manager_for_thread=no_parent,
            bounded_codex_interaction_value=lambda value: value, now_iso=lambda: "now",
            register_session_task=Mock(), update_codex_pending_session_metadata=AsyncMock(),
            append_event=append, decline_server_request=AsyncMock(return_value={"decision": "decline"}),
            _CODEX_CANCEL_INTERACTION_FUTURE=object())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])),
            str(source), "exec"), scope)
        params = {"threadId": "side-fork", "availableDecisions": ["accept", "decline"]}
        task = asyncio.create_task(scope["handle_codex_server_request"](1, "item/commandExecution/requestApproval",
            params, side_session_id="chat", side_owner_is_current=lambda: True))
        await asyncio.wait_for(requested.wait(), 1)
        interaction_id = next(iter(registry))
        self.assertEqual(registry[interaction_id]["thread_id"], "side-fork")
        await scope["cancel_codex_interactions"]("chat", resolution="turn_stopped")
        self.assertFalse(registry[interaction_id]["future"].done())
        with self.assertRaises(HTTPException):
            await scope["resolve_codex_interaction"]("chat", interaction_id, {"decision": "acceptForSession"})
        await scope["resolve_codex_interaction"]("chat", interaction_id, {"decision": "accept"})
        self.assertEqual(await task, {"decision": "accept"})
        self.assertEqual(registry, {})
        owner = Mock(side_effect=[True, False])
        self.assertEqual(await scope["handle_codex_server_request"](2, "item/commandExecution/requestApproval",
            params, side_session_id="chat", side_owner_is_current=owner), {"decision": "decline"})
        no_parent.assert_not_called()


class ServerCallbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("agent_server.py")
        tree = ast.parse(source.read_text(), filename=str(source))
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "create_native_side_chat"]
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])),
            str(source), "exec")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="side-question-glue-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "sessions" / "chat").mkdir(parents=True)
        self.path = root / "sessions" / "chat" / "events.jsonl"
        self.path.write_text(json.dumps(event("turn_started", prompt="Original user context")) + "\n")
        self.session = {"id": "chat", "backend": "claude", "model": "sonnet",
                        "claude_session_id": "claude-parent", "codex_thread_id": "codex-parent",
                        "codex_goal": {"status": "active"}}
        self.parent_active = {"chat": {"run_id": "parent", "provider_turn_ready": True}}
        self.parent_queue = {"chat": [{"prompt": "queued work"}]}
        self.manager = SimpleNamespace(ask_side_question=AsyncMock(return_value={
            "answer": "Native Claude answer", "context_note": "Native context"}))
        self.codex = SimpleNamespace(ask=AsyncMock(return_value="Native Codex answer"), close=AsyncMock(),
                                     closed=False, thread_id="side-fork")
        self.codex_factory = Mock(return_value=self.codex)
        self.options = SimpleNamespace(resume="claude-parent")
        self.namespace = dict(asyncio=asyncio, side_questions=side, STATE_DIR=root,
            SERVER_SHUTTING_DOWN=False, STORE=SimpleNamespace(sessions={"chat": self.session}),
            CODEX_GOALS_RECONFIGURING=False,
            codex_provider=codex_provider,
            CODEX_PROVIDER_STORE=SimpleNamespace(revision=lambda: None, for_session=lambda *args, **kwargs: None,
                require_thread=lambda *args: None),
            DEFAULT_BACKEND="claude", BACKEND_CLAUDE="claude", BACKEND_CODEX="codex",
            CLAUDE_BIN="synthetic-claude", CODEX_BIN="synthetic-codex", DEFAULT_CWD=str(root),
            ACTIVE=self.parent_active, QUEUED_TURNS=self.parent_queue,
            session_codex_thread_id=lambda session: session.get("codex_thread_id"),
            existing_cwd=lambda value: value, codex_manifest_path=lambda sid: str(root / "unused-manifest"),
            codex_runtime_settings=lambda session: (session.get("model", ""), session.get("effort", ""), ""),
            codex_effective_thread_config=lambda session: {}, flatten_codex_config_overrides=lambda value: value,
            codex_app_server_service_tier=lambda value: value,
            CODEX_APPROVAL_POLICIES={"never", "on-request", "untrusted"}, CODEX_DEFAULT_APPROVAL_POLICY="never",
            CODEX_APPROVAL_REVIEWERS={"user", "auto_review", "guardian_subagent"}, CODEX_DEFAULT_APPROVALS_REVIEWER="user",
            CODEX_SANDBOX_MODES={"read-only", "workspace-write", "danger-full-access"}, CODEX_DEFAULT_SANDBOX_MODE="danger-full-access",
            CODEX_SANDBOX_POLICY_TYPES={"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"},
            CODEX_DEFAULT_PERMISSION_PROFILE=None, CODEX_PROVIDER_MCP_NAME="_agentsdock_internal_provider_9f3a2c71",
            handle_codex_server_request=AsyncMock(),
            build_claude_sdk_options=Mock(return_value=(self.options, "configuration", "synthetic-claude")),
            claude_sdk_manager=AsyncMock(return_value=self.manager),
            runner_env=lambda: {"AGENTSDOCK_CHAT_ID": "parent", "HOME": "/synthetic"})
        self.namespace["public_chat_share_session_exists"] = lambda sid: sid in self.namespace["STORE"].sessions
        exec(self.code, self.namespace)

    async def test_codex_side_fork_carries_parent_permissions_and_scopes_approval_owner(self):
        self.session.update(backend="codex", cwd="/project", codex_approval_policy="on-request",
                            codex_permission_profile=":workspace-write", codex_approvals_reviewer="user")
        self.namespace["codex_effective_thread_config"] = lambda session: {
            "model_reasoning_summary": "detailed",
            "mcp_servers._agentsdock_internal_provider_9f3a2c71.url": "parent-only-secret",
        }
        with patch.dict(sys.modules, {"codex_side_question": SimpleNamespace(NativeCodexSideChat=self.codex_factory)}):
            chat = await self.namespace["create_native_side_chat"]("chat")
            await chat.ask("Read a new file", history=[])
        options = self.codex_factory.call_args.kwargs
        self.assertEqual(options["cwd"], "/project")
        for key in ("fork_overrides", "turn_overrides"):
            self.assertEqual(options[key]["permissions"], ":workspace-write")
            self.assertEqual(options[key]["approvalPolicy"], "on-request")
            self.assertEqual(options[key]["runtimeWorkspaceRoots"], ["/project"])
            self.assertNotIn("sandbox", options[key])
            self.assertNotIn("sandboxPolicy", options[key])
        self.assertEqual(options["fork_overrides"]["config"], {"model_reasoning_summary": "detailed"})
        bridge = self.namespace["handle_codex_server_request"]
        async def owns(*args, **kwargs):
            self.assertEqual(kwargs["side_session_id"], "chat")
            return kwargs["side_owner_is_current"]()
        bridge.side_effect = owns
        callback = options["server_request_handler"]
        self.assertTrue(await callback(1, "item/commandExecution/requestApproval", {"threadId": "side-fork"}))
        self.assertFalse(await callback(2, "item/commandExecution/requestApproval", {"threadId": "codex-parent"}))
        self.session["codex_thread_id"] = "replacement"
        self.assertFalse(await callback(3, "item/commandExecution/requestApproval", {"threadId": "side-fork"}))

    async def test_codex_auth_reservation_rejects_side_chat_before_provider_creation(self):
        self.session["backend"] = "codex"
        chat = await self.namespace["create_native_side_chat"]("chat")
        self.namespace["CODEX_GOALS_RECONFIGURING"] = True
        with patch.dict(sys.modules, {"codex_side_question": SimpleNamespace(NativeCodexSideChat=self.codex_factory)}):
            with self.assertRaises(side.SideQuestionError) as raised:
                await chat.ask("Why?", history=[])
            self.assertEqual(raised.exception.status_code, 409)
            self.codex_factory.assert_not_called()
            self.codex.ask.assert_not_awaited()
            self.namespace["CODEX_GOALS_RECONFIGURING"] = False
            self.assertEqual((await chat.ask("Why?", history=[]))["answer"], "Native Codex answer")

    async def test_side_chat_inherits_custom_selection_but_default_ignores_custom_revision(self):
        self.session["backend"] = "codex"
        self.session["effort"] = "high"
        selected = {"base_url": "https://synthetic.invalid/v1", "model": "synthetic-model", "api_key": "synthetic-key", "credential_id": "retained"}
        catalog = {"model_capabilities": {"synthetic-model": {
            "reasoning_efforts": ["high"], "reasoning_summary_supported": True}}}
        revision = Mock(return_value="first")
        self.namespace["CODEX_PROVIDER_STORE"] = SimpleNamespace(revision=revision,
            for_session=lambda session, **kwargs: dict(selected) if session.get("codex_provider") == "custom" else None,
            cached_catalog=lambda selection: catalog,
            require_thread=Mock())
        with patch.dict(sys.modules, {"codex_side_question": SimpleNamespace(NativeCodexSideChat=self.codex_factory)}):
            normal = await self.namespace["create_native_side_chat"]("chat")
            revision.return_value = "second"
            await normal.ask("Normal?", history=[])
            self.assertIsNone(self.codex_factory.call_args.kwargs["provider_selection"])
            revision.assert_not_called()
            self.session["codex_provider"] = "custom"
            custom = await self.namespace["create_native_side_chat"]("chat")
            await custom.ask("Custom?", history=[])
            self.assertEqual(self.codex_factory.call_args.kwargs["provider_selection"], {
                **selected, "effort": "high", "reasoning_summary": "auto"})
            revision.return_value = "third"
            await custom.ask("Still retained after another endpoint is saved?", history=[])
            revision.return_value = None
            await custom.ask("Still retained after settings reset?", history=[])
            revision.assert_not_called()
            catalog.clear()
            unknown = await self.namespace["create_native_side_chat"]("chat")
            await unknown.ask("Unknown reasoning support?", history=[])
            self.assertEqual(self.codex_factory.call_args.kwargs["provider_selection"], {
                **selected, "effort": "", "reasoning_summary": "none"})
            self.assertNotIn("effort", selected)
            selected["credential_id"] = "changed-chat-binding"
            with self.assertRaises(side.SideQuestionError) as caught:
                await custom.ask("Stale?", history=[])
            self.assertEqual(caught.exception.status_code, 410)

    async def test_both_native_backends_leave_busy_parent_and_event_log_untouched(self):
        initial_session = json.dumps(self.session, sort_keys=True)
        initial_active = json.dumps(self.parent_active, sort_keys=True)
        initial_queue = json.dumps(self.parent_queue, sort_keys=True)
        initial_bytes = self.path.read_bytes()
        with patch.object(side, "read_context_snapshot", side_effect=AssertionError("snapshot forbidden")), \
                patch.dict(sys.modules, {"codex_side_question": SimpleNamespace(NativeCodexSideChat=self.codex_factory)}):
            claude = await self.namespace["create_native_side_chat"]("chat")
            first = await claude.ask("Why?", history=[])
            self.assertEqual(first["answer"], "Native Claude answer")
            self.manager.ask_side_question.assert_awaited_once_with(
                "chat", "Why?", history=[], options=self.options, configuration_key="configuration",
                expected_provider_id="claude-parent")
            self.session["backend"] = "codex"
            self.session["model"] = "synthetic-model"
            codex = await self.namespace["create_native_side_chat"]("chat")
            second = await codex.ask("Why?", history=[])
            self.assertEqual(second["answer"], "Native Codex answer")
            self.assertEqual(self.codex_factory.call_args.args, ("codex-parent",))
            self.assertEqual(self.codex_factory.call_args.kwargs["model"], "synthetic-model")
            self.assertNotIn("AGENTSDOCK_CHAT_ID", self.codex_factory.call_args.kwargs["env"])
            await codex.close()
            self.codex.close.assert_awaited_once()
            await claude.close()
            self.session.update(backend="claude", model="sonnet")
        self.assertEqual(json.dumps(self.session, sort_keys=True), initial_session)
        self.assertEqual(json.dumps(self.parent_active, sort_keys=True), initial_active)
        self.assertEqual(json.dumps(self.parent_queue, sort_keys=True), initial_queue)
        self.assertEqual(self.path.read_bytes(), initial_bytes)

    async def test_missing_native_parent_and_unsupported_backend_never_launch_provider(self):
        self.session.pop("claude_session_id")
        with self.assertRaises(side.SideQuestionError) as caught:
            await self.namespace["create_native_side_chat"]("chat")
        self.assertEqual(caught.exception.status_code, 409)
        self.session["backend"] = "cursor"
        with self.assertRaises(side.SideQuestionError) as caught:
            await self.namespace["create_native_side_chat"]("chat")
        self.assertEqual(caught.exception.status_code, 503)
        self.manager.ask_side_question.assert_not_awaited()

    async def test_followup_history_uses_native_claude_pairs_and_retained_codex_handle(self):
        history = [{"question": "Initial side question", "response": "Initial side answer"}]
        initial_bytes = self.path.read_bytes()
        initial_active = json.dumps(self.parent_active, sort_keys=True)
        initial_queue = json.dumps(self.parent_queue, sort_keys=True)
        initial_session = json.dumps(self.session, sort_keys=True)
        with patch.dict(sys.modules, {"codex_side_question": SimpleNamespace(NativeCodexSideChat=self.codex_factory)}):
            claude = await self.namespace["create_native_side_chat"]("chat")
            await claude.ask("Explain that", history=history)
            self.assertEqual(self.manager.ask_side_question.await_args.kwargs["history"], history)
            self.session["backend"] = "codex"
            codex = await self.namespace["create_native_side_chat"]("chat")
            await codex.ask("Explain that", history=history)
            await codex.ask("And that?", history=history)
            self.codex_factory.assert_called_once()
            self.assertEqual(self.codex.ask.await_args.args, ("And that?",))
            self.session["backend"] = "claude"
        self.assertEqual(self.path.read_bytes(), initial_bytes)
        self.assertEqual(json.dumps(self.parent_active, sort_keys=True), initial_active)
        self.assertEqual(json.dumps(self.parent_queue, sort_keys=True), initial_queue)
        self.assertEqual(json.dumps(self.session, sort_keys=True), initial_session)

    async def test_changed_backend_or_native_parent_cannot_reuse_existing_side_chat(self):
        for field, value in (("backend", "codex"), ("claude_session_id", "replacement-parent")):
            original = self.session[field]
            chat = await self.namespace["create_native_side_chat"]("chat")
            self.session[field] = value
            with self.assertRaises(side.SideQuestionError) as caught:
                await chat.ask("Why?", history=[])
            self.assertEqual(caught.exception.status_code, 410)
            self.session[field] = original
        self.manager.ask_side_question.assert_not_awaited()

    async def test_deleted_parent_rejects_before_ask_and_discards_inflight_result(self):
        chat = await self.namespace["create_native_side_chat"]("chat")
        sessions = self.namespace["STORE"].sessions
        sessions.clear()
        with self.assertRaises(side.SideQuestionError) as caught:
            await chat.ask("Why?", history=[])
        self.assertEqual(caught.exception.status_code, 404)
        self.manager.ask_side_question.assert_not_awaited()
        sessions["chat"] = self.session
        async def remove_parent(*args, **kwargs):
            sessions.clear()
            return {"answer": "Stale", "context_note": "Native context"}
        self.manager.ask_side_question.side_effect = remove_parent
        with self.assertRaises(side.SideQuestionError) as caught:
            await chat.ask("Why?", history=[])
        self.assertEqual(caught.exception.status_code, 404)

    async def test_native_sdk_lifecycle_errors_have_safe_http_statuses(self):
        from claude_sdk_client import (
            ClaudeSDKConfigurationConflict, ClaudeSDKGenerationChanged,
            ClaudeSDKRunActive, ClaudeSDKUnavailable,
        )
        for error_type, status in ((ClaudeSDKGenerationChanged, 410),
                                   (ClaudeSDKConfigurationConflict, 409),
                                   (ClaudeSDKRunActive, 409), (ClaudeSDKUnavailable, 503)):
            chat = await self.namespace["create_native_side_chat"]("chat")
            self.manager.ask_side_question.side_effect = error_type("private provider detail")
            with self.assertRaises(side.SideQuestionError) as caught:
                await chat.ask("Why?", history=[])
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
