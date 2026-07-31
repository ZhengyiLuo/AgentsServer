import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class CodexRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
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
