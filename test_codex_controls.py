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

    def test_command_approval_accepts_only_exact_proposed_amendments(self) -> None:
        exec_decision = {
            "acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": ["git", "status"],
            }
        }
        network_decision = {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {
                    "action": "allow",
                    "host": "example.com",
                }
            }
        }
        pending = {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "availableDecisions": [exec_decision, network_decision, "decline"],
                "proposedExecpolicyAmendment": ["git", "status"],
                "proposedNetworkPolicyAmendments": [
                    {"action": "allow", "host": "example.com"}
                ],
            },
        }
        self.assertEqual(
            agent_server.validate_codex_interaction_response(
                pending,
                {"decision": exec_decision},
            ),
            {"decision": exec_decision},
        )
        self.assertEqual(
            agent_server.validate_codex_interaction_response(
                pending,
                {"decision": network_decision},
            ),
            {"decision": network_decision},
        )

        invalid_decisions = [
            {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": ["rm", "-rf"],
                }
            },
            {
                "applyNetworkPolicyAmendment": {
                    "network_policy_amendment": {
                        "action": "allow",
                        "host": "other.example",
                    }
                }
            },
            {
                "applyNetworkPolicyAmendment": {
                    "network_policy_amendment": {
                        "action": "sometimes",
                        "host": "example.com",
                    }
                }
            },
        ]
        for decision in invalid_decisions:
            with self.subTest(decision=decision), self.assertRaises(HTTPException):
                agent_server.validate_codex_interaction_response(
                    pending,
                    {"decision": decision},
                )
        for available in (None, []):
            with self.subTest(available=available), self.assertRaises(
                HTTPException
            ):
                agent_server.validate_codex_interaction_response(
                    {
                        "method": pending["method"],
                        "params": {
                            **pending["params"],
                            "availableDecisions": available,
                        },
                    },
                    {"decision": exec_decision},
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

    async def test_interaction_registration_is_atomic_and_does_not_hold_lifecycle_lock(
        self,
    ) -> None:
        handler_tasks: dict[str, set[asyncio.Task[object]]] = {}
        lifecycle_locks: dict[str, asyncio.Lock] = {}
        with (
            patch.object(
                agent_server,
                "CODEX_INTERACTION_HANDLER_TASKS",
                handler_tasks,
            ),
            patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", lifecycle_locks),
            patch.object(agent_server, "DELETING_SESSIONS", set()),
            patch.object(agent_server, "DELETED_SESSION_TOMBSTONES", set()),
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
                    9,
                    "item/commandExecution/requestApproval",
                    {
                        "threadId": "thread",
                        "availableDecisions": ["accept", "decline"],
                    },
                )
            )
            for _attempt in range(20):
                if agent_server.CODEX_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)

            self.assertTrue(agent_server.CODEX_PENDING_INTERACTIONS)
            self.assertIn(request_task, handler_tasks["chat"])
            lifecycle_lock = agent_server.session_lifecycle_lock("chat")
            await asyncio.wait_for(lifecycle_lock.acquire(), timeout=0.1)
            lifecycle_lock.release()

            interaction_id = next(iter(agent_server.CODEX_PENDING_INTERACTIONS))
            await agent_server.resolve_codex_interaction(
                "chat",
                interaction_id,
                {"decision": "decline"},
            )
            self.assertEqual(await request_task, {"decision": "decline"})

    async def test_delete_marker_wins_before_interaction_registration(self) -> None:
        handler_tasks: dict[str, set[asyncio.Task[object]]] = {}
        lifecycle_locks: dict[str, asyncio.Lock] = {}
        deleting: set[str] = set()
        with (
            patch.object(
                agent_server,
                "CODEX_INTERACTION_HANDLER_TASKS",
                handler_tasks,
            ),
            patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", lifecycle_locks),
            patch.object(agent_server, "DELETING_SESSIONS", deleting),
            patch.object(agent_server, "DELETED_SESSION_TOMBSTONES", set()),
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
                AsyncMock(return_value={"decision": "decline"}),
            ) as decline,
        ):
            lifecycle_lock = agent_server.session_lifecycle_lock("chat")
            await lifecycle_lock.acquire()
            request_task = asyncio.create_task(
                agent_server.handle_codex_server_request(
                    10,
                    "item/commandExecution/requestApproval",
                    {
                        "threadId": "thread",
                        "availableDecisions": ["accept", "decline"],
                    },
                )
            )
            await asyncio.sleep(0)
            deleting.add("chat")
            lifecycle_lock.release()
            try:
                self.assertEqual(
                    await request_task,
                    {"decision": "decline"},
                )
            finally:
                deleting.discard("chat")

            decline.assert_awaited_once()
            self.assertFalse(agent_server.CODEX_PENDING_INTERACTIONS)
            self.assertNotIn("chat", handler_tasks)

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

    async def test_terminate_terminal_requires_confirmation(self) -> None:
        with patch.object(
            agent_server,
            "acquire_codex_control_thread",
            AsyncMock(),
        ) as acquire:
            with self.assertRaises(HTTPException) as raised:
                await agent_server.post_codex_background_terminal_terminate(
                    "chat",
                    agent_server.CodexBackgroundTerminalRequest(
                        process_id="process-1",
                        confirmed=False,
                    ),
                )
        self.assertEqual(raised.exception.status_code, 400)
        acquire.assert_not_awaited()

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

    def test_goal_time_budget_stays_exhausted_after_native_status_change(
        self,
    ) -> None:
        session = {
            "codex_goal": {
                "status": "budgetLimited",
                "timeUsedSeconds": 12,
            },
            "codex_goal_time_budget_seconds": 20,
            "codex_goal_time_budget_exhausted": True,
        }
        self.assertEqual(
            agent_server.codex_goal_time_budget_remaining(session),
            0,
        )

    async def test_native_elapsed_budget_is_exposed_as_exhausted(self) -> None:
        session = agent_server.STORE.sessions["chat"]
        session.update(
            {
                "codex_goal": {
                    "status": "budgetLimited",
                    "timeUsedSeconds": 20,
                },
                "codex_goal_time_budget_seconds": 20,
                "codex_goal_time_budget_exhausted": False,
            }
        )
        self.assertTrue(
            agent_server.public_session(session)[
                "codex_goal_time_budget_exhausted"
            ]
        )
        goal = await agent_server.get_codex_goal("chat")
        self.assertTrue(goal["time_budget_exhausted"])
        runtime = await agent_server.get_codex_runtime("chat")
        self.assertTrue(runtime["time_budget_exhausted"])

    async def test_exhausted_time_budget_blocks_a_new_turn(self) -> None:
        agent_server.STORE.sessions["chat"].update(
            {
                "codex_goal": {
                    "status": "budgetLimited",
                    "timeUsedSeconds": 12,
                },
                "codex_goal_time_budget_seconds": 20,
                "codex_goal_time_budget_exhausted": True,
            }
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.start_turn(
                "chat",
                agent_server.TurnRequest(prompt="continue"),
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_permission_profile_cache_is_ttl_and_generation_scoped(self) -> None:
        class Manager:
            ready = True
            generation = 4

        manager = Manager()
        agent_server.CODEX_PERMISSION_PROFILES_CACHE["/work"] = (
            4,
            agent_server.time.monotonic(),
            [{"id": "default", "allowed": True}],
        )
        self.assertEqual(
            agent_server.cached_codex_permission_profiles("/work", manager),
            [{"id": "default", "allowed": True}],
        )
        manager.generation = 5
        self.assertIsNone(
            agent_server.cached_codex_permission_profiles("/work", manager)
        )
        agent_server.CODEX_PERMISSION_PROFILES_CACHE["/work"] = (
            5,
            agent_server.time.monotonic()
            - agent_server.CODEX_PERMISSION_PROFILES_CACHE_SECONDS
            - 1,
            [{"id": "stale", "allowed": True}],
        )
        self.assertIsNone(
            agent_server.cached_codex_permission_profiles("/work", manager)
        )

    async def test_native_status_and_automatic_compaction_events_are_readable(
        self,
    ) -> None:
        self.assertEqual(
            agent_server.codex_thread_status_message(
                {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                }
            ),
            "Codex is waiting for approval.",
        )
        with (
            patch.object(agent_server, "ACTIVE", {}),
            patch.object(
                agent_server,
                "append_event",
                AsyncMock(),
            ) as append_event,
        ):
            await agent_server.project_codex_notification(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn-1",
                        "item": {
                            "id": "compact-1",
                            "type": "contextCompaction",
                        },
                    },
                }
            )
        payload = append_event.await_args.args[2]
        self.assertEqual(
            payload["message"],
            "Codex completed automatic context compaction.",
        )

    def test_goal_budget_and_native_completion_events_bump_timeline(self) -> None:
        for event_type in (
            "codex_goal_budget_limited",
            "codex_compaction_completed",
            "codex_review_finished",
            "codex_shell_finished",
        ):
            with self.subTest(event_type=event_type):
                self.assertTrue(
                    agent_server.should_bump_session_updated_at(
                        event_type,
                        {"type": event_type},
                    )
                )

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
        self.previous_pins = agent_server.CODEX_APP_SERVER_PINNED_THREADS
        self.previous_pin_counts = (
            agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS
        )
        self.previous_interactive_threads = (
            agent_server.CODEX_INTERACTIVE_CONTROL_THREADS
        )
        self.previous_interactive_counts = (
            agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS
        )
        self.previous_turn_tasks = agent_server.SESSION_TURN_TASKS
        self.previous_interaction_tasks = (
            agent_server.CODEX_INTERACTION_HANDLER_TASKS
        )
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
        agent_server.CODEX_APP_SERVER_PINNED_THREADS = set()
        agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS = {}
        agent_server.CODEX_INTERACTIVE_CONTROL_THREADS = set()
        agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS = {}
        agent_server.SESSION_TURN_TASKS = {}
        agent_server.CODEX_INTERACTION_HANDLER_TASKS = {}

    async def asyncTearDown(self) -> None:
        await agent_server.cancel_codex_native_actions()
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.CODEX_NATIVE_ACTION_TASKS = self.previous_tasks
        agent_server.CODEX_APP_SERVER_PINNED_THREADS = self.previous_pins
        agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS = (
            self.previous_pin_counts
        )
        agent_server.CODEX_INTERACTIVE_CONTROL_THREADS = (
            self.previous_interactive_threads
        )
        agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS = (
            self.previous_interactive_counts
        )
        agent_server.SESSION_TURN_TASKS = self.previous_turn_tasks
        agent_server.CODEX_INTERACTION_HANDLER_TASKS = (
            self.previous_interaction_tasks
        )

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
        self.assertEqual(
            events[-1][1]["message"],
            "Context compaction completed.",
        )
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

    async def test_native_actions_remain_owned_when_start_event_write_fails(
        self,
    ) -> None:
        cases = (
            (
                "compaction",
                "codex_compaction_started",
                lambda: agent_server.post_codex_compact("chat"),
            ),
            (
                "review",
                "codex_review_started",
                lambda: agent_server.post_codex_review(
                    "chat",
                    agent_server.CodexReviewRequest(target={"type": "uncommittedChanges"}),
                ),
            ),
            (
                "shell",
                "codex_shell_started",
                lambda: agent_server.post_codex_shell_command(
                    "chat",
                    agent_server.CodexShellCommandRequest(
                        command="git status --short",
                        confirmed=True,
                    ),
                ),
            ),
        )
        for operation, failing_event, launch in cases:
            with self.subTest(operation=operation):
                agent_server.ACTIVE["chat"].update(
                    {
                        "run_id": None,
                        "provider_turn_id": None,
                        "provider_turn_ready": False,
                        "codex_native_operation_kind": None,
                    }
                )
                turn_id = f"turn-{operation}"
                subscription = GatedNativeSubscription(
                    [
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread",
                                "turn": {
                                    "id": turn_id,
                                    "status": "completed",
                                },
                            },
                        }
                    ],
                    gate_at=0,
                )
                manager = AsyncMock()
                manager.subscribe_thread = lambda _thread_id: subscription
                manager.compact_thread = AsyncMock()
                manager.start_review = AsyncMock(
                    return_value={"turn": {"id": turn_id}}
                )
                manager.run_thread_shell_command = AsyncMock()

                async def acquire(
                    _session_id: str,
                    *,
                    reserve_session: bool = False,
                ) -> tuple[object, str, dict[str, object]]:
                    self.assertTrue(reserve_session)
                    return manager, "thread", agent_server.STORE.sessions["chat"]

                async def persist_event(
                    _session_id: str,
                    event_type: str,
                    _payload: dict[str, object],
                ) -> dict[str, object]:
                    if event_type == failing_event:
                        raise OSError("disk full")
                    return {}

                with (
                    patch.object(
                        agent_server,
                        "acquire_codex_control_thread",
                        side_effect=acquire,
                    ),
                    patch.object(
                        agent_server,
                        "append_event",
                        side_effect=persist_event,
                    ),
                    patch.object(
                        agent_server,
                        "release_codex_control_thread",
                        AsyncMock(),
                    ) as release_thread,
                ):
                    result = await launch()
                    operation_id = str(result["operation_id"])
                    task = agent_server.CODEX_NATIVE_ACTION_TASKS[
                        ("chat", operation_id)
                    ]
                    await subscription.waiting.wait()
                    self.assertFalse(task.done())
                    release_thread.assert_not_awaited()
                    subscription.release.set()
                    await task
                    release_thread.assert_awaited_once()

    async def test_budget_interrupt_survives_timeline_write_failure(self) -> None:
        agent_server.STORE.sessions["chat"].update(
            {
                "codex_goal": {
                    "status": "active",
                    "timeUsedSeconds": 10,
                },
                "codex_goal_time_budget_seconds": 10,
            }
        )
        agent_server.ACTIVE["chat"]["run_id"] = "run-budget"
        manager = AsyncMock()
        manager.set_thread_goal = AsyncMock(
            return_value={
                "status": "budgetLimited",
                "timeUsedSeconds": 10,
            }
        )
        turn = AsyncMock()
        turn.turn_id = "turn-budget"
        with (
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(
                agent_server,
                "append_event",
                AsyncMock(side_effect=OSError("disk full")),
            ),
        ):
            await agent_server.apply_codex_goal_time_budget_limit(
                "chat",
                "run-budget",
                manager,
                "thread",
                turn,
                10,
            )
        turn.interrupt.assert_awaited_once()
        self.assertTrue(agent_server.ACTIVE["chat"]["stop_requested"])
        self.assertTrue(
            agent_server.STORE.sessions["chat"][
                "codex_goal_time_budget_exhausted"
            ]
        )

    async def test_native_tasks_can_be_cancelled_by_session(self) -> None:
        agent_server.ACTIVE.pop("chat", None)
        first = asyncio.create_task(asyncio.Event().wait())
        second = asyncio.create_task(asyncio.Event().wait())
        agent_server.register_codex_native_action("chat", "first", first)
        agent_server.register_codex_native_action("other", "second", second)

        await agent_server.cancel_codex_native_actions("chat")

        self.assertTrue(first.cancelled())
        self.assertFalse(second.done())
        self.assertNotIn(("chat", "first"), agent_server.CODEX_NATIVE_ACTION_TASKS)
        self.assertIn(("other", "second"), agent_server.CODEX_NATIVE_ACTION_TASKS)

    async def test_native_task_without_turn_id_is_not_orphaned_by_cancel(
        self,
    ) -> None:
        task = asyncio.create_task(asyncio.Event().wait())
        agent_server.register_codex_native_action("chat", "operation", task)
        previous_manager = agent_server.CODEX_APP_SERVER_MANAGER
        agent_server.CODEX_APP_SERVER_MANAGER = AsyncMock()
        try:
            with (
                patch.object(
                    agent_server,
                    "CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS",
                    0.01,
                ),
                self.assertRaises(HTTPException) as raised,
            ):
                await agent_server.cancel_codex_native_actions("chat")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertFalse(task.done())
            self.assertTrue(
                agent_server.ACTIVE["chat"]["stop_requested"]
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            agent_server.CODEX_NATIVE_ACTION_TASKS.pop(
                ("chat", "operation"),
                None,
            )
            agent_server.CODEX_APP_SERVER_MANAGER = previous_manager

    async def test_thread_pins_and_interactive_controls_are_reference_counted(
        self,
    ) -> None:
        manager = AsyncMock()
        await agent_server.pin_codex_app_server_thread("thread")
        await agent_server.pin_codex_app_server_thread("thread")
        await agent_server.unpin_codex_app_server_thread(manager, "thread")
        self.assertIn(
            "thread",
            agent_server.CODEX_APP_SERVER_PINNED_THREADS,
        )
        self.assertEqual(
            agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS["thread"],
            1,
        )

        agent_server.acquire_codex_interactive_control_lease("thread")
        agent_server.acquire_codex_interactive_control_lease("thread")
        agent_server.release_codex_interactive_control_lease("thread")
        self.assertIn(
            "thread",
            agent_server.CODEX_INTERACTIVE_CONTROL_THREADS,
        )
        self.assertEqual(
            agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS["thread"],
            1,
        )

    async def test_manager_shutdown_clears_lru_touched_by_turn_finalizer(
        self,
    ) -> None:
        class Manager:
            generation = 1

            def __init__(self) -> None:
                self.closed = asyncio.Event()

            async def close(self) -> None:
                self.closed.set()

            def is_thread_loaded(self, _thread_id: str) -> bool:
                return True

            def active_turn(self, _thread_id: str) -> None:
                return None

        manager = Manager()

        async def finalize_after_close() -> None:
            await manager.closed.wait()
            await agent_server.unpin_codex_app_server_thread(
                manager,
                "thread",
            )

        task = asyncio.create_task(finalize_after_close())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat",
            task,
        )
        previous_manager = agent_server.CODEX_APP_SERVER_MANAGER
        agent_server.CODEX_APP_SERVER_MANAGER = manager
        agent_server.CODEX_APP_SERVER_PINNED_THREADS.add("thread")
        agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS["thread"] = 1
        try:
            with (
                patch.object(
                    agent_server,
                    "cancel_codex_interactions",
                    AsyncMock(),
                ),
                patch.object(
                    agent_server,
                    "cancel_codex_native_actions",
                    AsyncMock(),
                ),
                patch.object(
                    agent_server,
                    "reset_codex_ephemeral_runtime_metadata",
                    AsyncMock(),
                ),
            ):
                await agent_server.close_codex_app_server_manager()
            self.assertFalse(agent_server.CODEX_APP_SERVER_THREAD_LRU)
            self.assertFalse(agent_server.CODEX_APP_SERVER_PINNED_THREADS)
            self.assertFalse(agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS)
        finally:
            agent_server.CODEX_APP_SERVER_MANAGER = previous_manager

    async def test_deleting_session_discards_append_waiting_on_event_lock(
        self,
    ) -> None:
        lock = agent_server.event_delivery_lock("chat")
        await lock.acquire()
        append_task = asyncio.create_task(
            agent_server.append_event(
                "chat",
                "codex_thread_status",
                {"message": "late"},
            )
        )
        await asyncio.sleep(0)
        agent_server.DELETING_SESSIONS.add("chat")
        try:
            lock.release()
            event = await append_task
        finally:
            agent_server.DELETING_SESSIONS.discard("chat")
        self.assertTrue(event["discarded"])
        self.assertEqual(event["seq"], 0)

    async def test_session_turn_tasks_are_joinable_before_delete(self) -> None:
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat",
            task,
        )
        self.assertFalse(
            await agent_server.wait_for_session_tasks(
                agent_server.SESSION_TURN_TASKS,
                "chat",
                timeout=0.01,
            )
        )
        release.set()
        await task
        self.assertTrue(
            await agent_server.wait_for_session_tasks(
                agent_server.SESSION_TURN_TASKS,
                "chat",
                timeout=0.01,
            )
        )

    async def test_delete_drains_late_turn_before_removing_session(self) -> None:
        late_event: dict[str, object] = {}

        async def finish_during_delete() -> None:
            while "chat" not in agent_server.DELETING_SESSIONS:
                await asyncio.sleep(0)
            late_event.update(
                await agent_server.append_event(
                    "chat",
                    "assistant_text",
                    {"text": "too late"},
                )
            )

        task = asyncio.create_task(finish_during_delete())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat",
            task,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_path = state_dir / "sessions" / "chat"
            session_path.mkdir(parents=True)
            (session_path / "events.jsonl").write_text("{}\n")
            with (
                patch.object(agent_server, "STATE_DIR", state_dir),
                patch.object(
                    agent_server,
                    "SESSIONS_FILE",
                    state_dir / "sessions.json",
                ),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_MANAGER",
                    None,
                ),
                patch.object(
                    agent_server.JOBS,
                    "delete_for_session",
                    AsyncMock(return_value=0),
                ),
                patch.object(agent_server, "kill_terminal_session"),
            ):
                try:
                    result = await agent_server.delete_session("chat")
                    self.assertIn(
                        "chat",
                        agent_server.DELETED_SESSION_TOMBSTONES,
                    )
                finally:
                    agent_server.DELETED_SESSION_TOMBSTONES.discard("chat")

            self.assertTrue(result["deleted"])
            self.assertFalse(session_path.exists())
        self.assertNotIn("chat", agent_server.STORE.sessions)
        self.assertTrue(late_event["discarded"])

    async def test_delete_job_cleanup_failure_is_retryable_without_resurrection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_path = state_dir / "sessions" / "chat"
            session_path.mkdir(parents=True)
            (session_path / "events.jsonl").write_text("{}\n")
            delete_jobs = AsyncMock(
                side_effect=[OSError("job store unavailable"), 2]
            )
            with (
                patch.object(agent_server, "STATE_DIR", state_dir),
                patch.object(
                    agent_server,
                    "SESSIONS_FILE",
                    state_dir / "sessions.json",
                ),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_MANAGER",
                    None,
                ),
                patch.object(
                    agent_server.JOBS,
                    "delete_for_session",
                    delete_jobs,
                ),
                patch.object(agent_server, "kill_terminal_session"),
            ):
                try:
                    with self.assertRaisesRegex(
                        OSError,
                        "job store unavailable",
                    ):
                        await agent_server.delete_session("chat")

                    self.assertNotIn("chat", agent_server.STORE.sessions)
                    self.assertIn(
                        "chat",
                        agent_server.DELETED_SESSION_TOMBSTONES,
                    )
                    self.assertFalse(session_path.exists())
                    late_event = await agent_server.append_event(
                        "chat",
                        "job_error",
                        {"message": "must not resurrect"},
                    )
                    self.assertTrue(late_event["discarded"])
                    self.assertFalse(session_path.exists())

                    retry = await agent_server.delete_session("chat")
                    self.assertFalse(retry["deleted"])
                    self.assertEqual(retry["deleted_jobs"], 2)
                    self.assertEqual(delete_jobs.await_count, 2)
                    self.assertFalse(session_path.exists())
                finally:
                    agent_server.DELETED_SESSION_TOMBSTONES.discard("chat")

    async def test_delete_cleans_provider_thread_bound_while_turn_drains(
        self,
    ) -> None:
        agent_server.STORE.sessions["chat"]["codex_thread_id"] = None

        class LateBindingManager:
            def __init__(self) -> None:
                self.unsubscribe_thread = AsyncMock()

            def is_thread_loaded(self, _thread_id: str) -> bool:
                return True

        manager = LateBindingManager()
        thread_index: dict[str, str] = {}
        thread_lru = agent_server.OrderedDict()
        approval_cache = agent_server.OrderedDict()

        async def bind_before_turn_finishes() -> None:
            while "chat" not in agent_server.DELETING_SESSIONS:
                await asyncio.sleep(0)
            session = agent_server.STORE.sessions["chat"]
            session["session_id"] = "late-thread"
            session["codex_thread_id"] = "late-thread"
            thread_index["late-thread"] = "chat"
            thread_lru["late-thread"] = 1.0
            agent_server.CODEX_APP_SERVER_PINNED_THREADS.add("late-thread")
            agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS["late-thread"] = 1
            agent_server.CODEX_INTERACTIVE_CONTROL_THREADS.add("late-thread")
            agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS["late-thread"] = 1
            approval_cache[("late-thread", "approval")] = {"id": "approval"}

        turn_task = asyncio.create_task(bind_before_turn_finishes())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat",
            turn_task,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            session_path = state_dir / "sessions" / "chat"
            session_path.mkdir(parents=True)
            with (
                patch.object(agent_server, "STATE_DIR", state_dir),
                patch.object(
                    agent_server,
                    "SESSIONS_FILE",
                    state_dir / "sessions.json",
                ),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_MANAGER",
                    manager,
                ),
                patch.object(
                    agent_server,
                    "CODEX_THREAD_SESSION_INDEX",
                    thread_index,
                ),
                patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_THREAD_LRU",
                    thread_lru,
                ),
                patch.object(
                    agent_server,
                    "CODEX_APPROVAL_ITEM_CACHE",
                    approval_cache,
                ),
                patch.object(
                    agent_server.JOBS,
                    "delete_for_session",
                    AsyncMock(return_value=0),
                ),
                patch.object(agent_server, "kill_terminal_session"),
            ):
                try:
                    result = await agent_server.delete_session("chat")
                finally:
                    agent_server.DELETED_SESSION_TOMBSTONES.discard("chat")

        self.assertTrue(result["deleted"])
        manager.unsubscribe_thread.assert_awaited_once_with("late-thread")
        self.assertNotIn("late-thread", thread_index)
        self.assertNotIn("late-thread", thread_lru)
        self.assertNotIn(
            "late-thread",
            agent_server.CODEX_APP_SERVER_PINNED_THREADS,
        )
        self.assertNotIn(
            "late-thread",
            agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS,
        )
        self.assertNotIn(
            "late-thread",
            agent_server.CODEX_INTERACTIVE_CONTROL_THREADS,
        )
        self.assertNotIn(
            "late-thread",
            agent_server.CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS,
        )
        self.assertFalse(approval_cache)

    async def test_delete_timeout_preserves_session_for_retry(self) -> None:
        task = asyncio.create_task(asyncio.Event().wait())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat",
            task,
        )
        try:
            async def wait_for_tasks(
                registry: dict[str, set[asyncio.Task[object]]],
                _session_id: str,
                **_kwargs: object,
            ) -> bool:
                return registry is not agent_server.SESSION_TURN_TASKS

            with (
                patch.object(
                    agent_server,
                    "wait_for_session_tasks",
                    side_effect=wait_for_tasks,
                ),
                self.assertRaises(HTTPException) as raised,
            ):
                await agent_server.delete_session("chat")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("chat", agent_server.STORE.sessions)
            self.assertNotIn(
                "chat",
                agent_server.DELETED_SESSION_TOMBSTONES,
            )
            self.assertNotIn("chat", agent_server.DELETING_SESSIONS)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
