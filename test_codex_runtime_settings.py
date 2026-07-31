import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server


class CodexRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "runtime-settings-test"
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_manager = agent_server.CODEX_APP_SERVER_MANAGER
        self.previous_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
        agent_server.STORE.sessions = {
            self.session_id: {
                "id": self.session_id,
                "title": "Runtime settings",
                "folder": "General",
                "cwd": "/tmp",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "max",
                "codex_thread_id": "thread-runtime-settings",
                "codex_goal": {
                    "objective": "Keep working",
                    "status": "active",
                },
            }
        }

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.CODEX_APP_SERVER_MANAGER = self.previous_manager
        agent_server.SESSION_LIFECYCLE_LOCKS = self.previous_locks

    async def test_effort_change_updates_loaded_thread_without_touching_goal(self) -> None:
        manager = AsyncMock()
        manager.is_thread_loaded = Mock(return_value=True)
        agent_server.CODEX_APP_SERVER_MANAGER = manager
        goal_before = dict(
            agent_server.STORE.sessions[self.session_id]["codex_goal"]
        )

        with (
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(
                agent_server,
                "codex_user_config_defaults",
                return_value=("gpt-5.6-sol", "medium", "priority"),
            ),
            patch.object(
                agent_server,
                "pin_codex_app_server_thread",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "unpin_codex_app_server_thread",
                AsyncMock(),
            ),
        ):
            await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(effort="ultra"),
            )

        manager.update_thread_settings.assert_awaited_once_with(
            "thread-runtime-settings",
            model="gpt-5.6-sol",
            effort="ultra",
            service_tier="priority",
        )
        manager.interrupt_turn.assert_not_awaited()
        manager.clear_thread_goal.assert_not_awaited()
        self.assertEqual(
            agent_server.STORE.sessions[self.session_id]["codex_goal"],
            goal_before,
        )

    async def test_effort_change_on_unloaded_thread_is_saved_for_next_turn(self) -> None:
        manager = AsyncMock()
        manager.is_thread_loaded = Mock(return_value=False)
        agent_server.CODEX_APP_SERVER_MANAGER = manager

        with patch.object(agent_server.STORE, "save", AsyncMock()):
            await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(effort="ultra"),
            )

        self.assertEqual(
            agent_server.STORE.sessions[self.session_id]["effort"],
            "ultra",
        )
        manager.update_thread_settings.assert_not_awaited()

    async def test_missing_live_update_method_defers_settings_to_next_turn(self) -> None:
        manager = AsyncMock()
        manager.is_thread_loaded = Mock(return_value=True)
        manager.update_thread_settings.side_effect = (
            agent_server.CodexAppServerRequestError(
                "thread/settings/update",
                {"code": -32601, "message": "Method not found"},
            )
        )
        agent_server.CODEX_APP_SERVER_MANAGER = manager

        with (
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(
                agent_server,
                "pin_codex_app_server_thread",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "unpin_codex_app_server_thread",
                AsyncMock(),
            ),
        ):
            result = await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(effort="ultra"),
            )

        self.assertEqual(result["session"]["effort"], "ultra")
        self.assertEqual(
            agent_server.STORE.sessions[self.session_id]["effort"],
            "ultra",
        )

    async def test_live_update_error_does_not_persist_or_report_new_effort(self) -> None:
        manager = AsyncMock()
        manager.is_thread_loaded = Mock(return_value=True)
        manager.update_thread_settings.side_effect = (
            agent_server.CodexAppServerRequestError(
                "thread/settings/update",
                {"code": -32000, "message": "provider rejected update"},
            )
        )
        agent_server.CODEX_APP_SERVER_MANAGER = manager
        save = AsyncMock()

        with (
            patch.object(agent_server.STORE, "save", save),
            patch.object(
                agent_server,
                "pin_codex_app_server_thread",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "unpin_codex_app_server_thread",
                AsyncMock(),
            ),
        ):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(effort="ultra"),
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            agent_server.STORE.sessions[self.session_id]["effort"],
            "max",
        )
        save.assert_not_awaited()

    def test_runtime_clamps_incompatible_codex_config_default(self) -> None:
        with patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("gpt-5.5", "ultra", ""),
        ):
            model, effort, _service_tier = agent_server.codex_runtime_settings({})

        self.assertEqual(model, "gpt-5.5")
        self.assertEqual(effort, "xhigh")

    async def test_load_preserves_effort_not_known_by_static_model_map(self) -> None:
        store = agent_server.SessionStore()
        payload = {
            "chat": {
                "id": "chat",
                "title": "Future capability",
                "folder": "General",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-luna",
                "effort": "ultra",
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_file = Path(temp_dir) / "sessions.json"
            sessions_file.write_text(json.dumps(payload))
            with (
                patch.object(agent_server, "SESSIONS_FILE", sessions_file),
                patch.object(store, "save", AsyncMock()),
            ):
                await store.load()

        self.assertEqual(store.sessions["chat"]["effort"], "ultra")


if __name__ == "__main__":
    unittest.main()
