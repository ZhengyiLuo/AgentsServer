import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class StandaloneProviderContextTests(unittest.IsolatedAsyncioTestCase):
    def test_standalone_session_preserves_runtime_but_clears_continuity(self) -> None:
        parent = {
            "id": "sess_parent",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": "/tmp/work",
            "model": "gpt-test",
            "effort": "high",
            "session_id": "thread_parent",
            "codex_thread_id": "thread_parent",
            "claude_session_id": "claude_parent",
            "system_prompt": "Keep this policy",
            "memory_seed": "old memory",
            "memory_seed_used": True,
            "fork_from": "sess_origin",
            "codex_goal": {"objective": "Parent goal"},
            "codex_goal_time_budget_seconds": 60,
            "codex_goal_time_budget_exhausted": True,
        }

        isolated = agent_server.standalone_provider_session(parent)

        self.assertEqual(isolated["id"], "sess_parent")
        self.assertEqual(isolated["cwd"], "/tmp/work")
        self.assertEqual(isolated["model"], "gpt-test")
        self.assertEqual(isolated["effort"], "high")
        self.assertEqual(isolated["system_prompt"], "Keep this policy")
        self.assertIsNone(agent_server.session_provider_id(isolated))
        self.assertIsNone(isolated["memory_seed"])
        self.assertIsNone(isolated["fork_from"])
        self.assertIsNone(isolated["codex_goal"])
        self.assertIsNone(isolated["codex_goal_time_budget_seconds"])
        self.assertEqual(parent["codex_thread_id"], "thread_parent")

    def test_standalone_ignores_exhausted_parent_goal_budget(self) -> None:
        session = {
            "backend": agent_server.BACKEND_CODEX,
            "codex_goal": {
                "objective": "Parent goal",
                "status": "budgetLimited",
                "timeUsedSeconds": 60,
            },
            "codex_goal_time_budget_seconds": 60,
            "codex_goal_time_budget_exhausted": True,
        }

        self.assertTrue(
            agent_server.provider_context_goal_is_exhausted(session, "chat")
        )
        self.assertFalse(
            agent_server.provider_context_goal_is_exhausted(
                session,
                "standalone",
            )
        )

    async def test_standalone_codex_thread_is_ephemeral_with_parent_policy(self) -> None:
        manager = AsyncMock()
        manager.start_thread.return_value = "thread_standalone"
        session = {
            "id": "sess_parent",
            "backend": agent_server.BACKEND_CODEX,
            "model": "gpt-test",
            "effort": "high",
        }
        with (
            patch.object(
                agent_server,
                "codex_thread_instructions",
                return_value="parent policy",
            ),
            patch.object(
                agent_server,
                "codex_thread_params",
                return_value={"cwd": "/tmp/work", "model": "gpt-test"},
            ) as thread_params,
        ):
            thread_id = (
                await agent_server.start_standalone_codex_app_server_thread(
                    manager,
                    "sess_parent",
                    session,
                    "/tmp/work",
                )
            )

        self.assertEqual(thread_id, "thread_standalone")
        thread_params.assert_called_once_with(
            session,
            "/tmp/work",
            developer_instructions="parent policy",
        )
        params = manager.start_thread.await_args.args[0]
        self.assertTrue(params["ephemeral"])
        self.assertEqual(params["serviceName"], "AgentsDock Scheduled Job")

    async def test_run_codex_propagates_standalone_mode_to_app_server(self) -> None:
        runner = AsyncMock()
        with (
            patch.object(
                agent_server,
                "CODEX_TRANSPORT",
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            patch.object(agent_server, "run_codex_app_server", runner),
        ):
            await agent_server.run_codex(
                "sess_parent",
                "run_job",
                "Do the scheduled work",
                {"id": "sess_parent", "backend": agent_server.BACKEND_CODEX},
                Path("/tmp/manifest.json"),
                standalone_provider_context=True,
            )

        self.assertTrue(
            runner.await_args.kwargs["standalone_provider_context"]
        )

    async def test_claude_provider_binding_skips_standalone_and_keeps_chat(self) -> None:
        save_provider = AsyncMock()
        events = AsyncMock()
        with (
            patch.object(
                agent_server,
                "ACTIVE",
                {"sess_parent": {"run_id": "run_chat"}},
            ),
            patch.object(
                agent_server,
                "BUSY_SESSIONS",
                {"sess_parent"},
            ),
            patch.object(
                agent_server,
                "CURRENT_TURNS",
                {"sess_parent": {"run_id": "run_chat"}},
            ),
            patch.object(
                agent_server.STORE,
                "save_provider_session",
                save_provider,
            ),
            patch.object(agent_server, "append_event", events),
        ):
            standalone_saved = await agent_server.persist_run_provider_session(
                "sess_parent",
                "run_standalone",
                agent_server.BACKEND_CLAUDE,
                "claude_standalone",
                cwd="/tmp/work",
                standalone_provider_context=True,
            )
            chat_saved = await agent_server.persist_run_provider_session(
                "sess_parent",
                "run_chat",
                agent_server.BACKEND_CLAUDE,
                "claude_parent",
                cwd="/tmp/work",
                standalone_provider_context=False,
            )

        self.assertFalse(standalone_saved)
        self.assertTrue(chat_saved)
        save_provider.assert_awaited_once_with(
            "sess_parent",
            "claude_parent",
            agent_server.BACKEND_CLAUDE,
            cwd="/tmp/work",
            defer_runtime_broadcast=True,
        )
        events.assert_awaited_once()
        self.assertEqual(
            events.await_args.args[2]["provider_session_id"],
            "claude_parent",
        )

    async def test_codex_app_server_acquisition_isolated_from_parent_binding(self) -> None:
        manager = AsyncMock()
        session = {
            "id": "sess_parent",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": "thread_parent",
        }
        start_standalone = AsyncMock(return_value="thread_standalone")
        ensure_chat = AsyncMock(return_value=("thread_parent", "policy_hash"))
        pin = AsyncMock()
        with (
            patch.object(
                agent_server,
                "start_standalone_codex_app_server_thread",
                start_standalone,
            ),
            patch.object(
                agent_server,
                "ensure_codex_app_server_thread",
                ensure_chat,
            ),
            patch.object(agent_server, "pin_codex_app_server_thread", pin),
        ):
            standalone_id = await agent_server.acquire_codex_run_thread(
                manager,
                "sess_parent",
                agent_server.standalone_provider_session(session),
                "/tmp/work",
                standalone_provider_context=True,
            )
            chat_id = await agent_server.acquire_codex_run_thread(
                manager,
                "sess_parent",
                session,
                "/tmp/work",
                standalone_provider_context=False,
            )

        self.assertEqual(standalone_id, "thread_standalone")
        self.assertEqual(chat_id, "thread_parent")
        start_standalone.assert_awaited_once()
        pin.assert_awaited_once_with("thread_standalone", manager)
        ensure_chat.assert_awaited_once_with(
            manager,
            "sess_parent",
            session,
            "/tmp/work",
        )
        # The acquisition helper never rewrites parent state for standalone;
        # persistent binding remains solely in the ordinary ensure path.
        self.assertEqual(session["codex_thread_id"], "thread_parent")

    async def test_standalone_turn_can_override_locked_parent_without_mutating_it(
        self,
    ) -> None:
        parent = {
            "id": "sess_parent",
            "title": "Parent chat",
            "folder": "General",
            "cwd": "/tmp/work",
            "backend": agent_server.BACKEND_CODEX,
            "model": "gpt-5.6-sol",
            "effort": "ultra",
            "session_id": "thread_parent",
            "codex_thread_id": "thread_parent",
            "claude_session_id": "claude_parent",
        }
        runtime_before = {
            key: parent.get(key)
            for key in (
                "backend",
                "model",
                "effort",
                "session_id",
                "codex_thread_id",
                "claude_session_id",
            )
        }
        run_claude = AsyncMock()
        busy: set[str] = set()
        current: dict[str, object] = {}
        tasks: dict[str, set[object]] = {}
        run_metadata: dict[str, dict[str, object]] = {}
        append_event = AsyncMock(return_value={"seq": 1})

        with (
            patch.object(agent_server.STORE, "sessions", {"sess_parent": parent}),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "BUSY_SESSIONS", busy),
            patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()),
            patch.object(agent_server, "CURRENT_TURNS", current),
            patch.object(agent_server, "QUEUED_TURNS", {}),
            patch.object(agent_server, "SESSION_TURN_TASKS", tasks),
            patch.object(agent_server, "RUN_METADATA", run_metadata),
            patch.object(
                agent_server,
                "managed_server_update_blocker",
                return_value=None,
            ),
            patch.object(
                agent_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                agent_server,
                "ensure_runtime_available",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "build_turn_provider_prompt",
                return_value="scheduled prompt",
            ),
            patch.object(
                agent_server,
                "codex_manifest_path",
                return_value=Path("/tmp/agentsdock-standalone-test-manifest.json"),
            ),
            patch.object(agent_server, "append_event", append_event),
            patch.object(agent_server, "run_claude", run_claude),
        ):
            result = await agent_server._start_turn_locked(
                "sess_parent",
                agent_server.TurnRequest(
                    prompt="Run independently",
                    backend=agent_server.BACKEND_CLAUDE,
                    model="sonnet",
                    effort="high",
                    purpose="scheduled_job",
                ),
                queue_if_busy=False,
                provider_context_mode="standalone",
            )
            await asyncio.sleep(0)

        self.assertFalse(result["queued"])
        self.assertFalse(parent.get("backend_locked", False))
        launched = run_claude.await_args.args[3]
        self.assertEqual(launched["backend"], agent_server.BACKEND_CLAUDE)
        self.assertEqual(launched["model"], "sonnet")
        self.assertEqual(launched["effort"], "high")
        self.assertIsNone(launched["session_id"])
        self.assertIsNone(launched["codex_thread_id"])
        self.assertIsNone(launched["claude_session_id"])
        self.assertTrue(
            run_claude.await_args.kwargs["standalone_provider_context"]
        )
        self.assertEqual(
            {
                key: parent.get(key)
                for key in runtime_before
            },
            runtime_before,
        )
        started = append_event.await_args_list[0].args[2]
        self.assertEqual(started["backend"], agent_server.BACKEND_CLAUDE)

    def test_standalone_backend_change_clears_inherited_runtime_defaults(self) -> None:
        parent = {
            "id": "sess_parent",
            "backend": agent_server.BACKEND_CODEX,
            "model": "gpt-5.6-sol",
            "effort": "ultra",
            "session_id": "thread_parent",
            "codex_thread_id": "thread_parent",
        }
        isolated = agent_server.standalone_provider_session(parent)

        preview = agent_server.preview_session_runtime_update(
            isolated,
            {"backend": agent_server.BACKEND_CLAUDE},
        )

        self.assertEqual(preview["backend"], agent_server.BACKEND_CLAUDE)
        self.assertIsNone(preview["model"])
        self.assertIsNone(preview["effort"])
        self.assertEqual(parent["model"], "gpt-5.6-sol")
        self.assertEqual(parent["effort"], "ultra")


if __name__ == "__main__":
    unittest.main()
