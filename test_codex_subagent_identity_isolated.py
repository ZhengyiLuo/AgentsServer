"""Public provider identity projection; AST only, no server/provider startup."""
from __future__ import annotations

import ast
import asyncio
from collections import OrderedDict
from pathlib import Path
import re
import threading
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import AsyncMock, Mock
from codex_app_server import CodexAppServerClient


FUNCTIONS = {
    "compact_subagent_text", "useful_subagent_identity_text",
    "codex_subagent_thread_identity", "normalize_subagent_status",
    "emit_codex_subagent_state", "_emit_codex_subagent_state_once", "reconcile_codex_subagents",
    "durable_event_seq", "build_subagent_snapshot",
    "is_agent_visible_event", "should_bump_session_updated_at",
    "codex_child_status_from_thread", "codex_child_status_from_turn",
    "project_codex_notification",
}


class CodexSubagentIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in FUNCTIONS]
        self.assertEqual({node.name for node in nodes}, FUNCTIONS)
        self.session = {"id": "chat", "backend": "codex", "codex_thread_id": "parent"}
        self.store = SimpleNamespace(sessions={"chat": self.session}, _lock=asyncio.Lock(), save=AsyncMock())
        self.sequence = 0
        self.now = "2026-09-14T04:00:00Z"

        async def append(session_id, kind, payload):
            self.sequence += 1
            return {"seq": self.sequence, "id": f"event-{self.sequence}", "ts": self.now,
                    "session_id": session_id, "type": kind, **payload}

        self.ns = {
            "Any": object, "CodexAppServerManager": object, "re": re, "asyncio": asyncio,
            "OrderedDict": OrderedDict,
            "STORE": self.store, "BACKEND_CODEX": "codex",
            "SUBAGENT_SNAPSHOT_TEXT_LIMIT": 200, "SUBAGENT_SNAPSHOT_LOG_LIMIT": 8,
            "SUBAGENT_SNAPSHOT_STATE_LIMIT": 64, "CODEX_SUBAGENT_RECONCILE_LIMIT": 32,
            "CODEX_SUBAGENT_INDEX_LOCK": threading.RLock(),
            "CODEX_SUBAGENT_TRANSITION_LOCKS": weakref.WeakValueDictionary(),
            "CODEX_SUBAGENT_STATE": {}, "CODEX_SUBAGENT_SESSION_INDEX": {},
            "CODEX_SUBAGENT_LIVE_GENERATIONS": {}, "CODEX_SUBAGENT_LIVE_MANAGERS": {}, "CODEX_QUARANTINED_GOAL_THREADS": {},
            "CODEX_APP_SERVER_MANAGER": SimpleNamespace(ready=True, generation=9),
            "now_iso": lambda: self.now, "append_event": AsyncMock(side_effect=append),
            "session_codex_thread_id": lambda session: session.get("codex_thread_id", ""),
            "logger": Mock(), "concise_error_message": str,
            "codex_session_has_active_run": AsyncMock(side_effect=AssertionError("identity must not inspect/wake execution")),
        }
        self.ns["existing_codex_app_server_manager"] = lambda session=None: self.ns["CODEX_APP_SERVER_MANAGER"]
        self.ns["existing_codex_app_server_manager_for_thread"] = lambda thread: self.ns["CODEX_APP_SERVER_MANAGER"]
        self.ns["codex_session_id_for_thread"] = lambda thread: (
            "chat" if thread == "parent" else self.ns["CODEX_SUBAGENT_SESSION_INDEX"].get(thread))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "subagent-identity-isolated", "exec"), self.ns)

    async def seed(self, *, status="running", title="Old title", nickname="Kepler"):
        self.ns["CODEX_SUBAGENT_STATE"].pop("child", None)
        await self.ns["emit_codex_subagent_state"](
            "chat", "child", status, parent_thread_id="parent", run_id="original-run",
            nickname=nickname, agent_path="/root/resources", name="Legacy task",
            title=title, activity="Original activity", summary="Original summary")
        self.ns["append_event"].reset_mock()
        self.store.save.reset_mock()
        return dict(self.ns["CODEX_SUBAGENT_STATE"]["child"])

    async def rename(self, value, *, thread="child"):
        await self.ns["project_codex_notification"]({"method": "thread/name/updated",
            "params": {"threadId": thread, "threadName": value}})

    async def test_custom_session_reconciliation_and_generation_use_its_manager(self):
        self.session.update(codex_provider="custom", codex_provider_revision="synthetic-generation")
        custom = SimpleNamespace(ready=True, generation=23,
            list_descendant_threads=AsyncMock(return_value=[{
                "id": "child", "status": {"type": "active"}, "parentThreadId": "parent",
            }]))
        self.ns["CODEX_APP_SERVER_MANAGER"] = Mock(
            list_descendant_threads=AsyncMock(side_effect=AssertionError("normal provider was used")))
        select = Mock(return_value=custom)
        self.ns["existing_codex_app_server_manager"] = select
        result = await self.ns["reconcile_codex_subagents"]("chat")
        self.assertEqual(result["reconciled"], 1)
        custom.list_descendant_threads.assert_awaited_once_with("parent")
        self.assertEqual(self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"]["child"], 23)
        self.assertTrue(all(call.args == (self.session,) for call in select.call_args_list))

    async def test_missing_custom_manager_does_not_fall_back_to_normal_provider(self):
        self.session["codex_provider"] = "custom"
        self.ns["existing_codex_app_server_manager"] = Mock(return_value=None)
        normal = SimpleNamespace(list_descendant_threads=AsyncMock())
        self.ns["CODEX_APP_SERVER_MANAGER"] = normal
        self.assertEqual(await self.ns["reconcile_codex_subagents"]("chat"),
                         {"reconciled": 0, "descendants": 0})
        normal.list_descendant_threads.assert_not_awaited()

    async def test_thread_list_explicit_title_is_separate_from_nickname_path_and_preview(self):
        manager = SimpleNamespace(list_descendant_threads=AsyncMock(return_value=[{
            "id": "child", "name": "Public resources", "agentNickname": "Kepler",
            "agentPath": "/root/resources", "parentThreadId": "parent",
            "preview": "Long original task prompt must not be the title",
            "status": {"type": "active"},
        }]))
        result = await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertEqual(result["reconciled"], 1)
        state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assertEqual(state["subagent_title"], "Public resources")
        self.assertEqual(state["subagent_nickname"], "Kepler")
        self.assertEqual(state["subagent_name"], "Kepler")
        self.assertEqual(state["subagent_path"], "/root/resources")
        self.assertEqual(self.session["codex_subagents"]["child"]["subagent_title"], "Public resources")
        manager.list_descendant_threads.assert_awaited_once_with("parent")

    async def test_missing_explicit_name_never_turns_preview_into_a_title(self):
        manager = SimpleNamespace(list_descendant_threads=AsyncMock(return_value=[{
            "id": "child", "preview": "A long prompt is not a provider title",
            "status": {"type": "idle"},
        }]))
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assertNotIn("subagent_title", state)
        self.assertEqual(state["subagent_name"], "A long prompt is not a provider title")

    async def test_title_updates_preserve_nickname_and_explicit_clear_does_not_reappear(self):
        await self.seed()
        for value in ({"name": "Wrong object"}, ["Wrong list"], True,
                      "[AgentsDock context] generated wrapper", "Codex subagent"):
            await self.ns["emit_codex_subagent_state"]("chat", "child", "running", title=value)
            self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"], "Old title")
        await self.ns["emit_codex_subagent_state"]("chat", "child", "completed")
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"], "Old title")
        for value in (None, ""):
            await self.rename("Publications audit")
            await self.rename(value)
            await self.ns["emit_codex_subagent_state"]("chat", "child", "completed")
            state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
            self.assertIsNone(state["subagent_title"])
            self.assertEqual(state["subagent_name"], "Kepler")

    async def test_live_rename_changes_only_title_not_generation_activity_or_owner(self):
        before = await self.seed()
        self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"]["child"] = 3
        await self.rename("Review letters audit")
        after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        ignored = {"id", "seq", "ts", "subagent_title"}
        self.assertEqual({k: v for k, v in before.items() if k not in ignored},
                         {k: v for k, v in after.items() if k not in ignored})
        self.assertEqual(after["subagent_title"], "Review letters audit")
        self.assertEqual(self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"]["child"], 3)
        self.ns["append_event"].assert_awaited_once()
        self.ns["codex_session_has_active_run"].assert_not_awaited()
        await self.rename("Review letters audit")
        self.ns["append_event"].assert_awaited_once()

    async def test_terminal_rename_cannot_reactivate_or_reassign_finished_child(self):
        before = await self.seed(status="completed")
        self.now = "2026-09-14T08:00:00Z"
        await self.ns["emit_codex_subagent_state"](
            "chat", "child", "running", title="Public resources", identity_only=True,
            run_id="unrelated-run", activity="Wrong new work", nickname="Wrong nickname")
        after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        for key in ("ts", "run_id", "subagent_status", "subagent_activity", "subagent_log",
                    "subagent_nickname", "subagent_name", "subagent_parent_thread_id"):
            self.assertEqual(after[key], before[key])
        self.assertNotIn("child", self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"])
        self.assertEqual(after["subagent_title"], "Public resources")

    async def test_root_unknown_foreign_and_malformed_rename_notifications_do_nothing(self):
        await self.seed(status="completed")
        for thread in ("parent", "unknown"):
            await self.rename("Not a child", thread=thread)
        for value in ({"name": "Object"}, ["List"], 42, True):
            await self.rename(value)
        await self.ns["project_codex_notification"]({"method": "thread/name/updated", "params": {"threadId": "child"}})
        self.ns["CODEX_SUBAGENT_STATE"]["child"]["session_id"] = "other-chat"
        await self.rename("Foreign")
        self.ns["append_event"].assert_not_awaited()
        self.store.save.assert_not_awaited()

    async def test_title_uses_existing_compact_bound_and_null_reconciliation_clears(self):
        await self.seed()
        await self.rename("Public\nresources " + "x" * 1000)
        self.assertEqual(len(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"]), 200)
        self.assertNotIn("\n", self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"])
        manager = SimpleNamespace(list_descendant_threads=AsyncMock(return_value=[{
            "id": "child", "name": None, "status": {"type": "active"},
        }]))
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertIsNone(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"])

    async def test_actual_notification_dispatch_keeps_started_title_then_latest_rename(self):
        before = await self.seed(status="completed")
        # Construct only the transport router: no start(), process or provider.
        client = CodexAppServerClient(codex_bin="never-executed", cwd="/unused", env_factory=dict)
        handler = self.ns["project_codex_notification"]
        client.add_notification_handler(handler)
        client._route_notification({"method": "thread/started", "params": {"thread": {
            "id": "child", "name": "Public resources", "status": {"type": "active"}}}})
        await client.wait_for_notification_handler(handler, "child")
        state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assertEqual(state["subagent_title"], "Public resources")
        self.assertEqual(state["subagent_status"], "completed")
        self.assertEqual(state["run_id"], before["run_id"])
        client._route_notification({"method": "thread/name/updated", "params": {
            "threadId": "child", "threadName": "Review letters audit"}})
        await client.wait_for_notification_handler(handler, "child")
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"], "Review letters audit")
        self.assertIsNone(client._proc)
        self.ns["codex_session_has_active_run"].assert_not_awaited()

    async def test_started_title_cannot_relabel_another_thread_or_create_a_child(self):
        await self.seed(status="completed")
        for params in (
            {"threadId": "child", "thread": {"id": "foreign", "name": "Wrong"}},
            {"thread": {"id": "parent", "name": "Root"}},
            {"thread": {"id": "unknown", "name": "Unknown"}},
            {"thread": {"id": "child", "name": {"unexpected": "object"}}},
            {"thread": {"id": "child", "preview": "Not a title"}},
        ):
            await self.ns["project_codex_notification"]({"method": "thread/started", "params": params})
        self.ns["append_event"].assert_not_awaited()
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"]["subagent_title"], "Old title")

    async def test_rename_completion_races_commit_in_both_orders_without_stale_overwrite(self):
        append = self.ns["append_event"]
        for rename_first in (True, False):
            with self.subTest(rename_first=rename_first):
                self.ns["append_event"] = append
                await self.seed()
                self.now = "2026-09-14T08:00:00Z"
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def gated_append(session, kind, payload):
                    calls.append(dict(payload))
                    if len(calls) == 1:
                        entered.set()
                        await release.wait()
                    return await append(session, kind, payload)

                self.ns["append_event"] = AsyncMock(side_effect=gated_append)

                async def complete():
                    await self.ns["emit_codex_subagent_state"](
                        "chat", "child", "completed", activity="Actual completion")

                first = asyncio.create_task(self.rename("Publications audit") if rename_first else complete())
                second = None
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    second = asyncio.create_task(complete() if rename_first else self.rename("Publications audit"))
                    await asyncio.sleep(0)
                    self.assertEqual(len(calls), 1, "second same-child transition must wait through commit")
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 2)
                state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
                self.assertEqual(state["subagent_title"], "Publications audit")
                self.assertEqual(state["subagent_status"], "completed")
                self.assertEqual(state["subagent_activity"], "Actual completion")
                self.assertEqual(state["run_id"], "original-run")
                self.assertNotIn("child", self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"])
                self.assertEqual(len(self.ns["CODEX_SUBAGENT_TRANSITION_LOCKS"]), 0)

    async def test_independent_children_do_not_share_transition_lock(self):
        await self.seed()
        append = self.ns["append_event"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated_append(session, kind, payload):
            if payload["subagent_id"] == "child":
                entered.set()
                await release.wait()
            return await append(session, kind, payload)

        self.ns["append_event"] = AsyncMock(side_effect=gated_append)
        pending = asyncio.create_task(self.rename("Public resources"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.ns["emit_codex_subagent_state"](
                "chat", "other-child", "completed", title="Review letters audit"), 1)
            self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["other-child"]["subagent_title"], "Review letters audit")
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(len(self.ns["CODEX_SUBAGENT_TRANSITION_LOCKS"]), 0)

    def terminal_manager(self, **identity):
        return SimpleNamespace(list_descendant_threads=AsyncMock(return_value=[{
            "id": "child", "parentThreadId": "parent", "status": {"type": "notLoaded"},
            "preview": "A full provider prompt is not a corrected identity",
            **identity,
        }]))

    def assert_lifecycle_unchanged(self, before, after):
        identity = {"id", "seq", "subagent_title", "subagent_name", "subagent_nickname", "subagent_path"}
        self.assertEqual({k: v for k, v in before.items() if k not in identity},
                         {k: v for k, v in after.items() if k not in identity})

    async def test_known_terminal_identity_correction_is_durable_and_reopen_is_idempotent(self):
        for status in ("completed", "failed", "stopped"):
            for title in ("Public resources", None, ""):
                with self.subTest(status=status, title=title):
                    before = await self.seed(status=status, nickname=None)
                    self.now = "2026-09-14T10:00:00Z"
                    manager = self.terminal_manager(name=title, agentNickname="Hypatia", agentPath="/root/assets")
                    await self.ns["reconcile_codex_subagents"]("chat", manager)
                    after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
                    self.assert_lifecycle_unchanged(before, after)
                    self.assertEqual(after["subagent_title"], title or None)
                    self.assertEqual(after["subagent_nickname"], "Hypatia")
                    self.assertEqual(after["subagent_name"], "Hypatia")
                    self.assertEqual(after["subagent_path"], "/root/assets")
                    self.assertGreater(after["seq"], before["seq"])
                    self.assertNotEqual(after["id"], before["id"])
                    self.assertEqual(self.session["codex_subagents"]["child"], after)
                    self.assertEqual(self.ns["build_subagent_snapshot"]("chat")["subagents"], [after])
                    self.assertFalse(self.ns["is_agent_visible_event"]("subagent_state", after))
                    self.assertFalse(self.ns["should_bump_session_updated_at"]("subagent_state", after))
                    self.ns["append_event"].assert_awaited_once()
                    self.store.save.assert_awaited_once()
                    await self.ns["reconcile_codex_subagents"]("chat", manager)
                    self.ns["CODEX_SUBAGENT_STATE"].clear()  # Reopen after a process restart.
                    await self.ns["reconcile_codex_subagents"]("chat", manager)
                    self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"], after)
                    self.ns["append_event"].assert_awaited_once()
                    self.store.save.assert_awaited_once()
                    self.assertNotIn("child", self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"])
                    self.ns["codex_session_has_active_run"].assert_not_awaited()

    async def test_terminal_repair_compares_stale_durable_identity_not_corrected_idless_memory(self):
        before = await self.seed(status="completed", nickname=None)
        self.ns["CODEX_SUBAGENT_STATE"]["child"] = {
            **{k: v for k, v in before.items() if k not in {"id", "seq"}},
            "ts": "2026-09-14T09:00:00Z", "subagent_activity": "Subagent completed",
            "subagent_log": [{"ts": "2026-09-14T09:00:00Z", "text": "Subagent completed"}],
            "subagent_name": "Curie", "subagent_nickname": "Curie", "subagent_title": "Live status",
        }
        manager = self.terminal_manager(name="Live status", agentNickname="Curie")
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assert_lifecycle_unchanged(before, after)
        self.assertEqual(after["subagent_nickname"], "Curie")
        self.assertEqual(after["subagent_title"], "Live status")
        self.assertEqual(self.ns["build_subagent_snapshot"]("chat")["subagents"], [after])
        self.ns["append_event"].assert_awaited_once()
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.ns["append_event"].assert_awaited_once()

    async def test_unchanged_terminal_reopen_keeps_durable_identity_despite_different_activity(self):
        before = await self.seed(status="completed")
        manager = self.terminal_manager(agentNickname="Kepler")
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"], before)
        self.assertEqual(self.ns["build_subagent_snapshot"]("chat")["subagents"], [before])
        self.ns["append_event"].assert_not_awaited()
        self.store.save.assert_not_awaited()
        self.ns["CODEX_SUBAGENT_STATE"]["child"] = {
            k: v for k, v in before.items() if k not in {"id", "seq"}
        }
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"], before)
        self.ns["append_event"].assert_not_awaited()

    async def test_idless_recovered_identity_survives_omitted_or_malformed_provider_fields(self):
        for title in (..., {"malformed": "title"}):
            with self.subTest(title=title):
                before = await self.seed(status="completed", nickname=None)
                self.ns["CODEX_SUBAGENT_STATE"]["child"] = {
                    **{k: v for k, v in before.items() if k not in {"id", "seq"}},
                    "subagent_title": "Recovered title", "subagent_nickname": "Curie",
                    "subagent_name": "Curie", "subagent_path": "/root/recovered",
                }
                manager = self.terminal_manager(**({} if title is ... else {"name": title}))
                await self.ns["reconcile_codex_subagents"]("chat", manager)
                after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
                self.assert_lifecycle_unchanged(before, after)
                self.assertEqual(after["subagent_title"], "Recovered title")
                self.assertEqual(after["subagent_nickname"], "Curie")
                self.assertEqual(after["subagent_path"], "/root/recovered")
                self.ns["append_event"].assert_awaited_once()

    async def test_legacy_metadata_without_event_identity_rehydrates_without_advertising_an_event(self):
        self.session["codex_subagents"] = {"child": {
            "session_id": "chat", "subagent_id": "child",
            "subagent_status": "completed", "subagent_name": "Leibniz",
        }}
        await self.ns["reconcile_codex_subagents"]("chat", self.terminal_manager())
        state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assertEqual(state["subagent_name"], "Leibniz")
        self.assertEqual(self.ns["CODEX_SUBAGENT_SESSION_INDEX"]["child"], "chat")
        self.assertNotIn("id", state)
        self.assertNotIn("seq", state)
        self.assertEqual(self.ns["build_subagent_snapshot"]("chat")["subagents"], [])
        self.ns["append_event"].assert_not_awaited()
        self.store.save.assert_not_awaited()

    async def test_unknown_terminal_child_stays_silent_and_cannot_claim_root_or_foreign_child(self):
        manager = self.terminal_manager(agentNickname="Avicenna")
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        state = self.ns["CODEX_SUBAGENT_STATE"]["child"]
        self.assertEqual(state["subagent_nickname"], "Avicenna")
        self.assertNotIn("id", state)
        self.assertNotIn("seq", state)
        self.assertEqual(self.ns["build_subagent_snapshot"]("chat")["subagents"], [])
        manager.list_descendant_threads.return_value[0]["id"] = "parent"
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertNotIn("parent", self.ns["CODEX_SUBAGENT_STATE"])
        foreign = {**state, "session_id": "other", "subagent_id": "foreign"}
        self.ns["CODEX_SUBAGENT_STATE"]["foreign"] = foreign
        self.ns["CODEX_SUBAGENT_SESSION_INDEX"]["foreign"] = "other"
        manager.list_descendant_threads.return_value[0]["id"] = "foreign"
        await self.ns["reconcile_codex_subagents"]("chat", manager)
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["foreign"], foreign)
        self.assertEqual(self.ns["CODEX_SUBAGENT_SESSION_INDEX"]["foreign"], "other")
        self.ns["append_event"].assert_not_awaited()
        self.store.save.assert_not_awaited()

    async def test_new_running_transition_wins_over_terminal_snapshot_read_in_flight(self):
        await self.seed(status="completed", nickname=None)
        entered, release = asyncio.Event(), asyncio.Event()
        manager = self.terminal_manager(agentNickname="Hypatia")
        threads = manager.list_descendant_threads.return_value

        async def delayed_read(_root):
            entered.set()
            await release.wait()
            return threads

        manager.list_descendant_threads.side_effect = delayed_read
        pending = asyncio.create_task(self.ns["reconcile_codex_subagents"]("chat", manager))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.ns["emit_codex_subagent_state"](
                "chat", "child", "running", run_id="new-run", activity="New work", nickname="Latest nickname")
            running = dict(self.ns["CODEX_SUBAGENT_STATE"]["child"])
        finally:
            release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(self.ns["CODEX_SUBAGENT_STATE"]["child"], running)
        self.assertEqual(self.session["codex_subagents"]["child"], running)
        self.assertEqual(self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"]["child"], 9)
        self.ns["append_event"].assert_awaited_once()

    async def test_terminal_identity_correction_and_running_transition_serialize_in_both_orders(self):
        append = self.ns["append_event"]
        for correction_first in (True, False):
            with self.subTest(correction_first=correction_first):
                self.ns["append_event"] = append
                before = await self.seed(status="completed", nickname=None)
                manager = self.terminal_manager(agentNickname="Hypatia")
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def gated_append(session, kind, payload):
                    calls.append(dict(payload))
                    if len(calls) == 1:
                        entered.set()
                        await release.wait()
                    return await append(session, kind, payload)

                self.ns["append_event"] = AsyncMock(side_effect=gated_append)

                async def start():
                    await self.ns["emit_codex_subagent_state"](
                        "chat", "child", "running", run_id="new-run", activity="New work")

                first = asyncio.create_task(self.ns["reconcile_codex_subagents"]("chat", manager)
                                            if correction_first else start())
                second = None
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    second = asyncio.create_task(start() if correction_first else
                        self.ns["reconcile_codex_subagents"]("chat", manager))
                    await asyncio.sleep(0)
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 2)
                after = self.ns["CODEX_SUBAGENT_STATE"]["child"]
                self.assertEqual(after["subagent_status"], "running")
                self.assertEqual(after["run_id"], "new-run")
                self.assertEqual(after["subagent_activity"], "New work")
                self.assertEqual(after["subagent_started_at"], before["subagent_started_at"])
                self.assertEqual(self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"]["child"], 9)
                self.assertEqual(after["subagent_nickname"], "Hypatia" if correction_first else None)
                self.assertEqual(len(calls), 2 if correction_first else 1)
                self.assertEqual(len(self.ns["CODEX_SUBAGENT_TRANSITION_LOCKS"]), 0)


if __name__ == "__main__":
    unittest.main()
