import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


class CodexControlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_pending = agent_server.CODEX_PENDING_INTERACTIONS
        self.previous_approval_items = agent_server.CODEX_APPROVAL_ITEM_CACHE
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_thread_index = agent_server.CODEX_THREAD_SESSION_INDEX
        agent_server.CODEX_PENDING_INTERACTIONS = {}
        agent_server.CODEX_APPROVAL_ITEM_CACHE = agent_server.OrderedDict()
        agent_server.CODEX_THREAD_SESSION_INDEX = {}
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": "thread",
            }
        }

    async def asyncTearDown(self) -> None:
        agent_server.CODEX_PENDING_INTERACTIONS = self.previous_pending
        agent_server.CODEX_APPROVAL_ITEM_CACHE = self.previous_approval_items
        agent_server.CODEX_THREAD_SESSION_INDEX = self.previous_thread_index
        agent_server.STORE.sessions = self.previous_sessions

    def test_command_approval_is_fail_closed(self) -> None:
        pending = {
            "method": "item/commandExecution/requestApproval",
            "params": {"availableDecisions": ["accept", "decline"]},
        }
        self.assertEqual(
            agent_server.validate_codex_interaction_response(
                pending,
                {"decision": "decline"},
            ),
            {"decision": "decline"},
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_codex_interaction_response(
                pending,
                {"decision": "acceptForSession"},
            )
        with self.assertRaises(HTTPException):
            agent_server.validate_codex_interaction_response(
                pending,
                {
                    "decision": {
                        "applyNetworkPolicyAmendment": {
                            "network_policy_amendment": {"host": "example.com"}
                        }
                    }
                },
            )

    async def test_file_approval_includes_cached_proposed_changes(self) -> None:
        agent_server.cache_codex_approval_item({
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "item": {
                    "id": "change-1",
                    "type": "fileChange",
                    "changes": [{
                        "path": "/work/app.py",
                        "kind": "update",
                        "diff": "+print('safe')",
                    }],
                },
            },
        })
        with (
            patch.object(
                agent_server,
                "codex_session_id_for_thread",
                return_value="chat",
            ),
            patch.object(
                agent_server,
                "codex_request_is_interactive",
                return_value=True,
            ),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(
                agent_server,
                "update_codex_pending_session_metadata",
                AsyncMock(),
            ),
        ):
            request_task = asyncio.create_task(
                agent_server.handle_codex_server_request(
                    7,
                    "item/fileChange/requestApproval",
                    {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "change-1",
                    },
                )
            )
            await asyncio.sleep(0)
            interaction_id, pending = next(
                iter(agent_server.CODEX_PENDING_INTERACTIONS.items())
            )
            self.assertEqual(
                pending["params"]["changes"][0]["path"],
                "/work/app.py",
            )
            await agent_server.resolve_codex_interaction(
                "chat",
                interaction_id,
                {"decision": "decline"},
            )
            self.assertEqual(
                await request_task,
                {"decision": "decline"},
            )

    async def test_stop_all_terminals_requires_confirmation(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await agent_server.post_codex_background_terminals_clean(
                "chat",
                agent_server.CodexBackgroundTerminalsCleanRequest(
                    confirmed=False
                ),
            )
        self.assertEqual(raised.exception.status_code, 400)

        manager = AsyncMock()
        with (
            patch.object(
                agent_server,
                "acquire_codex_control_thread",
                AsyncMock(return_value=(manager, "thread", {})),
            ),
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ) as release,
        ):
            result = await agent_server.post_codex_background_terminals_clean(
                "chat",
                agent_server.CodexBackgroundTerminalsCleanRequest(
                    confirmed=True
                ),
            )
        self.assertEqual(result, {"cleaned": True})
        manager.clean_background_terminals.assert_awaited_once_with("thread")
        release.assert_awaited_once_with("chat", manager, "thread")

    def test_permission_grant_cannot_exceed_requested_subset(self) -> None:
        pending = {
            "method": "item/permissions/requestApproval",
            "params": {
                "permissions": {
                    "network": False,
                    "filesystem": {"read": True, "write": False},
                }
            },
        }
        accepted = agent_server.validate_codex_interaction_response(
            pending,
            {
                "permissions": {
                    "filesystem": {"read": True},
                },
                "scope": "turn",
            },
        )
        self.assertEqual(accepted["scope"], "turn")
        with self.assertRaises(HTTPException):
            agent_server.validate_codex_interaction_response(
                pending,
                {
                    "permissions": {
                        "filesystem": {"write": True},
                    },
                    "scope": "session",
                },
            )

    def test_question_answers_are_bounded_and_keyed_by_known_ids(self) -> None:
        pending = {
            "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "choice"}]},
        }
        self.assertEqual(
            agent_server.validate_codex_interaction_response(
                pending,
                {"answers": {"choice": {"answers": ["A"]}}},
            ),
            {"answers": {"choice": {"answers": ["A"]}}},
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_codex_interaction_response(
                pending,
                {"answers": {"unknown": {"answers": ["A"]}}},
            )

    async def test_cancel_pending_interaction_returns_least_privilege(self) -> None:
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        agent_server.CODEX_PENDING_INTERACTIONS["pending"] = {
            "id": "pending",
            "native_request_id": 1,
            "session_id": "chat",
            "thread_id": "thread",
            "method": "item/fileChange/requestApproval",
            "params": {},
            "future": future,
            "responded": False,
        }
        with patch.object(agent_server.STORE, "save", AsyncMock()):
            await agent_server.cancel_codex_interactions(
                "chat",
                resolution="turn_stopped",
            )
        self.assertEqual(await future, {"decision": "decline"})
        self.assertEqual(
            agent_server.CODEX_PENDING_INTERACTIONS["pending"]["resolution"],
            "turn_stopped",
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.resolve_codex_interaction(
                "chat",
                "pending",
                {"decision": "accept"},
            )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_user_response_wins_race_with_interaction_cancellation(self) -> None:
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = {
            "id": "pending",
            "native_request_id": 1,
            "session_id": "chat",
            "thread_id": "thread",
            "method": "item/fileChange/requestApproval",
            "params": {"availableDecisions": ["accept", "decline"]},
            "created_at": agent_server.now_iso(),
            "future": future,
            "responded": False,
        }
        agent_server.CODEX_PENDING_INTERACTIONS["pending"] = pending
        decline_started = asyncio.Event()
        release_decline = asyncio.Event()

        async def delayed_decline(*_args: object, **_kwargs: object) -> dict[str, str]:
            decline_started.set()
            await release_decline.wait()
            return {"decision": "decline"}

        with (
            patch.object(
                agent_server,
                "decline_server_request",
                side_effect=delayed_decline,
            ),
            patch.object(agent_server.STORE, "save", AsyncMock()),
        ):
            cancel_task = asyncio.create_task(
                agent_server.cancel_codex_interactions(
                    "chat",
                    resolution="turn_stopped",
                )
            )
            await decline_started.wait()
            interaction = await agent_server.resolve_codex_interaction(
                "chat",
                "pending",
                {"decision": "accept"},
            )
            release_decline.set()
            await cancel_task

        self.assertEqual(interaction["id"], "pending")
        self.assertEqual(await future, {"decision": "accept"})
        self.assertEqual(pending["resolution"], "answered")

    async def test_user_response_wins_race_with_auto_resolution(self) -> None:
        decline_started = asyncio.Event()
        release_decline = asyncio.Event()

        async def delayed_decline(*_args: object, **_kwargs: object) -> dict[str, object]:
            decline_started.set()
            await release_decline.wait()
            return {"answers": {}}

        with (
            patch.object(
                agent_server,
                "codex_session_id_for_thread",
                return_value="chat",
            ),
            patch.object(
                agent_server,
                "codex_request_is_interactive",
                return_value=True,
            ),
            patch.object(
                agent_server,
                "decline_server_request",
                side_effect=delayed_decline,
            ),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(
                agent_server,
                "update_codex_pending_session_metadata",
                AsyncMock(),
            ),
        ):
            request_task = asyncio.create_task(
                agent_server.handle_codex_server_request(
                    1,
                    "item/tool/requestUserInput",
                    {
                        "threadId": "thread",
                        "autoResolutionMs": 1,
                        "questions": [{"id": "choice"}],
                    },
                )
            )
            await decline_started.wait()
            interaction_id, pending = next(
                iter(agent_server.CODEX_PENDING_INTERACTIONS.items())
            )
            await agent_server.resolve_codex_interaction(
                "chat",
                interaction_id,
                {"answers": {"choice": {"answers": ["A"]}}},
            )
            release_decline.set()
            result = await request_task

        self.assertEqual(
            result,
            {"answers": {"choice": {"answers": ["A"]}}},
        )
        self.assertEqual(pending["resolution"], "answered")

    async def test_secret_response_is_not_copied_into_pending_metadata(self) -> None:
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = {
            "id": "secret",
            "native_request_id": 1,
            "session_id": "chat",
            "thread_id": "thread",
            "method": "mcpServer/elicitation/request",
            "params": {},
            "created_at": agent_server.now_iso(),
            "future": future,
            "responded": False,
        }
        agent_server.CODEX_PENDING_INTERACTIONS["secret"] = pending

        interaction = await agent_server.resolve_codex_interaction(
            "chat",
            "secret",
            {"action": "accept", "content": {"password": "top-secret"}},
        )

        self.assertEqual(
            await future,
            {"action": "accept", "content": {"password": "top-secret"}},
        )
        self.assertNotIn("response", pending)
        self.assertNotIn("top-secret", repr(interaction))

    def test_goal_time_budget_uses_native_elapsed_time(self) -> None:
        session = {
            "codex_goal": {
                "status": "active",
                "timeUsedSeconds": 12,
            },
            "codex_goal_time_budget_seconds": 20,
        }
        self.assertEqual(
            agent_server.codex_goal_time_budget_remaining(session),
            8,
        )
        session["codex_goal"]["status"] = "paused"
        self.assertIsNone(agent_server.codex_goal_time_budget_remaining(session))

    async def test_session_load_clears_process_owned_codex_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_file = Path(temp_dir) / "sessions.json"
            sessions_file.write_text(json.dumps({
                "stale": {
                    "id": "stale",
                    "backend": agent_server.BACKEND_CODEX,
                    "codex_thread_id": "thread-stale",
                    "codex_thread_status": {
                        "type": "active",
                        "activeFlags": ["waitingOnApproval"],
                    },
                    "codex_pending_interaction_count": 2,
                    "codex_needs_user_action": True,
                    "codex_token_usage": {"totalTokens": 100},
                }
            }))
            store = agent_server.SessionStore()
            with patch.object(agent_server, "SESSIONS_FILE", sessions_file):
                await store.load()

        session = store.sessions["stale"]
        self.assertEqual(session["codex_thread_status"], {"type": "notLoaded"})
        self.assertEqual(session["codex_pending_interaction_count"], 0)
        self.assertFalse(session["codex_needs_user_action"])
        self.assertNotIn("codex_token_usage", session)
        self.assertEqual(
            agent_server.CODEX_THREAD_SESSION_INDEX["thread-stale"],
            "stale",
        )

    async def test_runtime_never_trusts_stale_active_status_when_thread_is_unloaded(
        self,
    ) -> None:
        agent_server.STORE.sessions["chat"]["codex_thread_status"] = {
            "type": "active",
            "activeFlags": ["waitingOnApproval"],
        }
        previous_manager = agent_server.CODEX_APP_SERVER_MANAGER
        agent_server.CODEX_APP_SERVER_MANAGER = None
        try:
            runtime = await agent_server.get_codex_runtime("chat")
        finally:
            agent_server.CODEX_APP_SERVER_MANAGER = previous_manager
        self.assertEqual(runtime["status"], {"type": "notLoaded"})


class GatedNativeSubscription:
    def __init__(self, notifications: list[dict[str, object]], gate_at: int) -> None:
        self.notifications = notifications
        self.gate_at = gate_at
        self.index = 0
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def next_notification(self, *, timeout: float) -> dict[str, object]:
        del timeout
        if self.index == self.gate_at:
            self.waiting.set()
            await self.release.wait()
        notification = self.notifications[self.index]
        self.index += 1
        return notification

    def close(self) -> None:
        self.closed = True


class CodexNativeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_tasks = agent_server.CODEX_NATIVE_ACTION_TASKS
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": "thread",
            }
        }
        agent_server.ACTIVE = {
            "chat": {
                "run_id": "operation",
                "provider_thread_id": "thread",
                "provider_turn_id": None,
                "provider_turn_ready": False,
                "codex_native_operation": True,
            }
        }
        agent_server.BUSY_SESSIONS = {"chat"}
        agent_server.CURRENT_TURNS = {}
        agent_server.CODEX_NATIVE_ACTION_TASKS = {}

    async def asyncTearDown(self) -> None:
        await agent_server.cancel_codex_native_actions()
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.CODEX_NATIVE_ACTION_TASKS = self.previous_tasks

    async def test_compaction_captures_turn_and_waits_for_turn_completed(self) -> None:
        subscription = GatedNativeSubscription(
            [
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn-compact"},
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn-compact",
                        "item": {
                            "id": "compact-item",
                            "type": "contextCompaction",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {
                            "id": "turn-compact",
                            "status": "completed",
                        },
                    },
                },
            ],
            gate_at=2,
        )
        events: list[tuple[str, dict[str, object]]] = []

        async def record_event(
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            events.append((event_type, payload))
            return {}

        with (
            patch.object(agent_server, "append_event", side_effect=record_event),
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ) as release_thread,
        ):
            task = asyncio.create_task(
                agent_server.consume_codex_native_turn(
                    "chat",
                    "operation",
                    "compaction",
                    AsyncMock(),
                    "thread",
                    subscription,
                )
            )
            await subscription.waiting.wait()
            self.assertFalse(task.done())
            self.assertEqual(
                agent_server.ACTIVE["chat"]["provider_turn_id"],
                "turn-compact",
            )
            self.assertTrue(agent_server.ACTIVE["chat"]["provider_turn_ready"])
            release_thread.assert_not_awaited()

            subscription.release.set()
            await task

        self.assertTrue(subscription.closed)
        self.assertEqual(
            [event_type for event_type, _payload in events].count(
                "codex_compaction_completed"
            ),
            1,
        )
        self.assertEqual(events[-1][1]["turn_id"], "turn-compact")
        release_thread.assert_awaited_once()

    async def test_pending_stop_interrupts_when_compaction_turn_id_arrives(
        self,
    ) -> None:
        agent_server.ACTIVE["chat"]["stop_requested"] = True
        subscription = GatedNativeSubscription(
            [
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn-compact"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {
                            "id": "turn-compact",
                            "status": "interrupted",
                        },
                    },
                },
            ],
            gate_at=1,
        )
        manager = AsyncMock()
        with (
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ),
        ):
            task = asyncio.create_task(
                agent_server.consume_codex_native_turn(
                    "chat",
                    "operation",
                    "compaction",
                    manager,
                    "thread",
                    subscription,
                )
            )
            await subscription.waiting.wait()
            manager.request.assert_awaited_once_with(
                "turn/interrupt",
                {
                    "threadId": "thread",
                    "turnId": "turn-compact",
                },
            )
            subscription.release.set()
            await task

    async def test_shell_retains_native_slot_until_turn_completed(self) -> None:
        subscription = GatedNativeSubscription(
            [
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn-shell"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {
                            "id": "turn-shell",
                            "status": "completed",
                        },
                    },
                },
            ],
            gate_at=1,
        )
        manager = AsyncMock()
        manager.subscribe_thread = lambda _thread_id: subscription
        manager.run_thread_shell_command = AsyncMock()

        async def acquire(
            _session_id: str,
            *,
            reserve_session: bool = False,
        ) -> tuple[object, str, dict[str, object]]:
            self.assertTrue(reserve_session)
            return manager, "thread", agent_server.STORE.sessions["chat"]

        with (
            patch.object(
                agent_server,
                "acquire_codex_control_thread",
                side_effect=acquire,
            ),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ) as release_thread,
        ):
            result = await agent_server.post_codex_shell_command(
                "chat",
                agent_server.CodexShellCommandRequest(
                    command="git status --short",
                    confirmed=True,
                ),
            )
            operation_id = str(result["operation_id"])
            task = agent_server.CODEX_NATIVE_ACTION_TASKS[
                ("chat", operation_id)
            ]
            await subscription.waiting.wait()
            self.assertFalse(task.done())
            release_thread.assert_not_awaited()
            self.assertEqual(
                agent_server.ACTIVE["chat"]["provider_turn_id"],
                "turn-shell",
            )

            subscription.release.set()
            await task

        manager.run_thread_shell_command.assert_awaited_once_with(
            "thread",
            "git status --short",
        )
        release_thread.assert_awaited_once()

    async def test_native_tasks_can_be_cancelled_by_session(self) -> None:
        first = asyncio.create_task(asyncio.Event().wait())
        second = asyncio.create_task(asyncio.Event().wait())
        agent_server.register_codex_native_action("chat", "first", first)
        agent_server.register_codex_native_action("other", "second", second)

        await agent_server.cancel_codex_native_actions("chat")

        self.assertTrue(first.cancelled())
        self.assertFalse(second.done())
        self.assertNotIn(("chat", "first"), agent_server.CODEX_NATIVE_ACTION_TASKS)
        self.assertIn(("other", "second"), agent_server.CODEX_NATIVE_ACTION_TASKS)

    async def test_deleted_session_does_not_receive_native_final_events(self) -> None:
        agent_server.STORE.sessions = {}
        subscription = GatedNativeSubscription(
            [
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn", "status": "completed"},
                    },
                }
            ],
            gate_at=1,
        )
        subscription.release.set()
        with (
            patch.object(agent_server, "append_event", AsyncMock()) as append,
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ),
        ):
            await agent_server.consume_codex_native_turn(
                "chat",
                "operation",
                "shell",
                AsyncMock(),
                "thread",
                subscription,
            )
        append.assert_not_awaited()

    async def test_rollback_requires_server_side_confirmation(self) -> None:
        with patch.object(
            agent_server,
            "acquire_codex_control_thread",
            AsyncMock(),
        ) as acquire:
            with self.assertRaises(HTTPException) as raised:
                await agent_server.post_codex_rollback(
                    "chat",
                    agent_server.CodexRollbackRequest(
                        num_turns=1,
                        confirmed=False,
                    ),
                )
        self.assertEqual(raised.exception.status_code, 400)
        acquire.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
