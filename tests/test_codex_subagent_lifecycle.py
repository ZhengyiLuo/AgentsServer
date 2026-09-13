import unittest
from unittest.mock import AsyncMock, patch

import agent_server


class FakeSubagentManager:
    def __init__(self) -> None:
        self.descendants = [
            {
                "id": "child-a",
                "parentThreadId": "parent-thread",
                "preview": "Audit A",
                "status": {"type": "active", "activeFlags": []},
            },
            {
                "id": "child-b",
                "parentThreadId": "child-a",
                "preview": "Audit B",
                "status": {"type": "idle"},
            },
        ]
        self.turn_reads: dict[str, int] = {}
        self.interrupts: list[tuple[str, str]] = []

    def is_thread_loaded(self, _thread_id: str) -> bool:
        return False

    async def list_descendant_threads(self, thread_id: str) -> list[dict[str, object]]:
        assert thread_id == "parent-thread"
        return list(self.descendants)

    async def list_turns(self, thread_id: str, **_kwargs: object) -> list[dict[str, object]]:
        reads = self.turn_reads.get(thread_id, 0)
        self.turn_reads[thread_id] = reads + 1
        if thread_id == "child-a":
            return [{
                "id": "turn-a",
                "status": "inProgress" if reads == 0 else "interrupted",
            }]
        return [{"id": "turn-b", "status": "completed"}]

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))


class CodexSubagentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_index = agent_server.CODEX_SUBAGENT_SESSION_INDEX
        self.previous_states = agent_server.CODEX_SUBAGENT_STATE
        self.session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "parent-thread",
            "session_id": "parent-thread",
        }
        agent_server.STORE.sessions = {"chat": self.session}
        agent_server.CODEX_SUBAGENT_SESSION_INDEX = {}
        agent_server.CODEX_SUBAGENT_STATE = {}
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.CODEX_SUBAGENT_SESSION_INDEX = self.previous_index
        agent_server.CODEX_SUBAGENT_STATE = self.previous_states

    async def append(self, session_id: str, event_type: str, payload: dict[str, object]) -> dict[str, object]:
        self.sequence += 1
        return {
            "seq": self.sequence,
            "id": f"event-{self.sequence}",
            "session_id": session_id,
            "type": event_type,
            "ts": f"2026-08-02T00:00:00.{self.sequence:06d}Z",
            **payload,
        }

    async def test_current_and_legacy_collaboration_shapes_project_targeted_state(self) -> None:
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            await agent_server.project_codex_subagent_item(
                "chat",
                "parent-thread",
                {
                    "type": "collabAgentToolCall",
                    "id": "spawn-tool",
                    "tool": "spawnAgent",
                    "senderThreadId": "parent-thread",
                    "receiverThreadIds": ["child-a"],
                    "status": "completed",
                    "agentsStates": {
                        "child-a": {"status": "running", "message": "Working"},
                    },
                },
                run_id="run-1",
                completed=True,
            )
            await agent_server.project_codex_subagent_item(
                "chat",
                "parent-thread",
                {
                    "type": "collabToolCall",
                    "id": "interrupt-tool",
                    "tool": "interrupt_agent",
                    "senderThreadId": "parent-thread",
                    "receiverThreadId": "child-a",
                    "status": "completed",
                    "agentStatus": "interrupted",
                },
                run_id="run-2",
                completed=True,
            )

        state = agent_server.CODEX_SUBAGENT_STATE["child-a"]
        self.assertEqual(state["subagent_status"], "stopped")
        self.assertEqual(state["subagent_tool_id"], "interrupt-tool")
        self.assertEqual(state["subagent_provider_ref"], "child-a")
        self.assertEqual(self.session["codex_subagents"]["child-a"]["subagent_status"], "stopped")

    async def test_child_usage_and_compaction_never_project_as_parent_lifecycle(self) -> None:
        self.session["codex_token_usage_snapshot"] = {
            "thread_id": "parent-thread",
            "turn_id": "parent-turn",
            "context_tokens": 42_000,
            "context_window": 100_000,
        }
        agent_server.CODEX_SUBAGENT_SESSION_INDEX["child-a"] = "chat"
        append_event = AsyncMock()
        record_usage = AsyncMock()

        with (
            patch.object(agent_server, "append_event", append_event),
            patch.object(agent_server, "record_codex_token_usage", record_usage),
        ):
            await agent_server.project_codex_notification({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "child-a",
                    "turnId": "child-turn",
                    "tokenUsage": {
                        "last": {"totalTokens": 91_000},
                        "total": {"totalTokens": 500_000},
                        "modelContextWindow": 100_000,
                    },
                },
            })
            for method in ("item/started", "item/completed"):
                await agent_server.project_codex_notification({
                    "method": method,
                    "params": {
                        "threadId": "child-a",
                        "turnId": "child-turn",
                        "item": {
                            "id": "child-compaction",
                            "type": "contextCompaction",
                        },
                    },
                })

        append_event.assert_not_awaited()
        record_usage.assert_not_awaited()
        self.assertEqual(
            self.session["codex_token_usage_snapshot"]["thread_id"],
            "parent-thread",
        )

    async def test_root_thread_is_never_classified_as_its_own_subagent(self) -> None:
        agent_server.CODEX_SUBAGENT_SESSION_INDEX["parent-thread"] = "chat"
        agent_server.CODEX_SUBAGENT_STATE["parent-thread"] = {
            "session_id": "chat",
            "subagent_id": "parent-thread",
            "backend": agent_server.BACKEND_CODEX,
        }

        self.assertEqual(
            agent_server.codex_session_id_for_thread("parent-thread"),
            "chat",
        )
        self.assertNotIn("parent-thread", agent_server.CODEX_SUBAGENT_SESSION_INDEX)
        self.assertNotIn("parent-thread", agent_server.CODEX_SUBAGENT_STATE)

    async def test_parked_codex_root_is_not_a_child_after_backend_switch(self) -> None:
        self.session.update({
            "backend": agent_server.BACKEND_CLAUDE,
            "session_id": "claude-thread",
            "claude_session_id": "claude-thread",
        })
        agent_server.CODEX_SUBAGENT_SESSION_INDEX["parent-thread"] = "chat"
        agent_server.CODEX_SUBAGENT_STATE["parent-thread"] = {
            "session_id": "chat",
            "subagent_id": "parent-thread",
            "backend": agent_server.BACKEND_CODEX,
        }
        append_event = AsyncMock()

        with patch.object(agent_server, "append_event", append_event):
            await agent_server.project_codex_notification({
                "method": "item/started",
                "params": {
                    "threadId": "parent-thread",
                    "turnId": "codex-turn",
                    "item": {
                        "id": "compact-item",
                        "type": "contextCompaction",
                    },
                },
            })

        append_event.assert_awaited_once()
        self.assertEqual(
            append_event.await_args.args[:2],
            ("chat", "codex_compaction_started"),
        )
        self.assertNotIn("parent-thread", agent_server.CODEX_SUBAGENT_SESSION_INDEX)

    def test_subagent_ownership_snapshot_is_detached_from_live_index(self) -> None:
        agent_server.CODEX_SUBAGENT_SESSION_INDEX["child-a"] = "chat"

        snapshot = agent_server.codex_subagent_ownership_snapshot()
        agent_server.CODEX_SUBAGENT_SESSION_INDEX["child-b"] = "chat"

        self.assertEqual(snapshot, {"child-a": "chat"})
        self.assertEqual(
            agent_server.codex_subagent_ownership_snapshot(),
            {"child-a": "chat", "child-b": "chat"},
        )

    async def test_parent_stop_interrupts_every_active_descendant_and_confirms_state(self) -> None:
        manager = FakeSubagentManager()
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            result = await agent_server.stop_codex_descendant_subagents(
                "chat",
                "parent-thread",
                manager=manager,  # type: ignore[arg-type]
            )

        self.assertEqual(manager.interrupts, [("child-a", "turn-a")])
        self.assertEqual(result["requested"], ["child-a"])
        self.assertEqual(result["interrupted"], ["child-a"])
        self.assertEqual(result["pending"], [])
        self.assertEqual(agent_server.CODEX_SUBAGENT_STATE["child-a"]["subagent_status"], "stopped")

    async def test_explicit_parent_stop_cascades_even_when_parent_is_idle(self) -> None:
        manager = FakeSubagentManager()
        cascade = AsyncMock(return_value={
            "descendants": 2,
            "requested": ["child-a"],
            "interrupted": ["child-a"],
            "pending": [],
            "errors": [],
        })
        with (
            patch.object(agent_server, "ACTIVE", {}),
            patch.object(agent_server, "BUSY_SESSIONS", set()),
            patch.object(agent_server, "CODEX_APP_SERVER_MANAGER", manager),
            patch.object(agent_server, "stop_codex_descendant_subagents", cascade),
            patch.object(agent_server, "cancel_codex_interactions", AsyncMock()),
            patch.object(agent_server, "schedule_next_queued_turn"),
        ):
            result = await agent_server.stop_turn("chat")

        cascade.assert_awaited_once_with(
            "chat",
            "parent-thread",
            manager=manager,
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(result["subagents"]["interrupted"], ["child-a"])

    async def test_targeted_stop_does_not_touch_sibling(self) -> None:
        manager = FakeSubagentManager()
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            result = await agent_server.stop_codex_descendant_subagents(
                "chat",
                "parent-thread",
                manager=manager,  # type: ignore[arg-type]
                target_thread_ids={"child-a"},
            )

        self.assertEqual(manager.interrupts, [("child-a", "turn-a")])
        self.assertNotIn("child-b", manager.turn_reads)
        self.assertEqual(result["interrupted"], ["child-a"])

    async def test_reconciliation_retires_not_loaded_running_child(self) -> None:
        await self._seed_running_child()
        manager = FakeSubagentManager()
        manager.descendants = [{
            "id": "child-a",
            "parentThreadId": "parent-thread",
            "preview": "Audit A",
            "status": {"type": "notLoaded"},
        }]

        async def completed_turns(_thread_id: str, **_kwargs: object) -> list[dict[str, object]]:
            return [{"id": "turn-a", "status": "completed"}]

        manager.list_turns = completed_turns  # type: ignore[method-assign]
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            result = await agent_server.reconcile_codex_subagents(
                "chat",
                manager,  # type: ignore[arg-type]
            )

        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(agent_server.CODEX_SUBAGENT_STATE["child-a"]["subagent_status"], "completed")

    async def test_emit_preserves_authoritative_identity_over_generated_preview(self) -> None:
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            await agent_server.emit_codex_subagent_state(
                "chat",
                "child-a",
                "running",
                parent_thread_id="parent-thread",
                name="Audit readonly files",
                nickname="Leibniz the 2nd",
                agent_path="/root/readonly_round2",
                activity="Working",
            )
            await agent_server.emit_codex_subagent_state(
                "chat",
                "child-a",
                "completed",
                name="[AgentsDock context] generated wrapper",
                activity="Subagent completed",
            )

        state = agent_server.CODEX_SUBAGENT_STATE["child-a"]
        self.assertEqual(state["subagent_name"], "Leibniz the 2nd")
        self.assertEqual(state["subagent_nickname"], "Leibniz the 2nd")
        self.assertEqual(state["subagent_path"], "/root/readonly_round2")
        self.assertEqual(state["subagent_status"], "completed")
        self.assertEqual(
            self.session["codex_subagents"]["child-a"]["subagent_name"],
            "Leibniz the 2nd",
        )

    async def test_reconciliation_reads_thread_and_spawn_source_identity(self) -> None:
        manager = FakeSubagentManager()
        manager.descendants = [
            {
                "id": "child-a",
                "parentThreadId": "parent-thread",
                "preview": "[AgentsDock context] generated wrapper",
                "agentNickname": "Leibniz the 2nd",
                "agentPath": "/root/readonly_round2",
                "status": {"type": "active", "activeFlags": []},
            },
            {
                "id": "child-b",
                "preview": "Full path",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "child-a",
                            "agent_nickname": "Carson the 2nd",
                            "agent_path": "/root/readonly_round2/source_trace",
                        },
                    },
                },
                "status": {"type": "idle"},
            },
        ]

        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            result = await agent_server.reconcile_codex_subagents(
                "chat",
                manager,  # type: ignore[arg-type]
            )

        self.assertEqual(result["reconciled"], 2)
        child_a = agent_server.CODEX_SUBAGENT_STATE["child-a"]
        self.assertEqual(child_a["subagent_name"], "Leibniz the 2nd")
        self.assertEqual(child_a["subagent_nickname"], "Leibniz the 2nd")
        self.assertEqual(child_a["subagent_path"], "/root/readonly_round2")
        child_b = agent_server.CODEX_SUBAGENT_STATE["child-b"]
        self.assertEqual(child_b["subagent_name"], "Carson the 2nd")
        self.assertEqual(child_b["subagent_nickname"], "Carson the 2nd")
        self.assertEqual(child_b["subagent_path"], "/root/readonly_round2/source_trace")
        self.assertEqual(child_b["subagent_parent_thread_id"], "child-a")

    async def _seed_running_child(self) -> None:
        with (
            patch.object(agent_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            await agent_server.emit_codex_subagent_state(
                "chat",
                "child-a",
                "running",
                parent_thread_id="parent-thread",
                activity="Working",
            )


if __name__ == "__main__":
    unittest.main()
