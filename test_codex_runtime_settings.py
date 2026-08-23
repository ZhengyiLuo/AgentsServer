import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class CodexRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_keeps_agentsdock_permission_defaults(self) -> None:
        store = agent_server.SessionStore()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(store, "save", AsyncMock()),
        ):
            session = await store.create(
                agent_server.CreateSessionRequest(backend=agent_server.BACKEND_CODEX)
            )

        self.assertEqual(session["codex_approval_policy"], "never")
        self.assertEqual(session["codex_sandbox_mode"], "danger-full-access")
        self.assertEqual(session["codex_approvals_reviewer"], "user")

    def test_runtime_clamps_incompatible_codex_config_default(self) -> None:
        with patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("gpt-5.5", "ultra", ""),
        ):
            model, effort, _service_tier = agent_server.codex_runtime_settings({})

        self.assertEqual(model, "gpt-5.5")
        self.assertEqual(effort, "xhigh")

    def test_app_server_service_tier_aliases_priority_to_fast(self) -> None:
        self.assertEqual(agent_server.codex_app_server_service_tier("priority"), "fast")
        self.assertEqual(agent_server.codex_app_server_service_tier("flex"), "flex")
        self.assertEqual(agent_server.codex_app_server_service_tier(""), "")

    def test_thread_params_use_app_server_alias_for_priority_models(self) -> None:
        # The Codex app-server's thread/start JSON-RPC only accepts "fast"/"flex"
        # for serviceTier, unlike the canonical "priority" value used for
        # `codex exec` CLI config overrides. gpt-5.6-sol/terra/luna fall back to
        # canonical "priority" (CODEX_FALLBACK_SERVICE_TIERS), so thread/start
        # must translate it rather than forward it unchanged.
        with patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("", "", ""),
        ):
            params = agent_server.codex_thread_params({"model": "gpt-5.6-sol"}, "/tmp")

        self.assertEqual(params["serviceTier"], "fast")

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
