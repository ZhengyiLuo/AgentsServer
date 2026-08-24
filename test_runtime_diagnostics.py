import json
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


def completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class RuntimeDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        with agent_server.RUNTIME_DIAGNOSTICS_LOCK:
            agent_server.RUNTIME_DIAGNOSTICS.clear()

    def test_missing_runtime_is_explicit(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value=None):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["available"])
        self.assertIn("Install Claude Code", diagnostic["action"])

    def test_broken_tmux_is_reported_unavailable_but_optional(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/tmux"), patch.object(
            agent_server.subprocess,
            "run",
            return_value=completed(["/usr/local/bin/tmux", "-V"], returncode=127),
        ) as run:
            capability = agent_server.tmux_capability()

        self.assertFalse(capability["available"])
        self.assertFalse(capability["required"])
        self.assertIn("failed its version check", capability["message"])
        run.assert_called_once()

    def test_failed_tmux_probe_is_cached_for_health_polling(self) -> None:
        agent_server.TMUX_PROBE_CACHE.update({
            "path": None,
            "available": False,
            "checked_at": 0.0,
        })
        with patch.object(
            agent_server.shutil,
            "which",
            return_value="/usr/local/bin/tmux",
        ), patch.object(
            agent_server.subprocess,
            "run",
            side_effect=OSError("cannot execute tmux"),
        ) as run:
            self.assertIsNone(agent_server.working_tmux_bin(use_cache=True))
            self.assertIsNone(agent_server.working_tmux_bin(use_cache=True))

        run.assert_called_once()

    def test_missing_tmux_is_reported_unavailable_but_optional(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value=None), patch.object(
            agent_server.subprocess,
            "run",
        ) as run:
            capability = agent_server.tmux_capability()

        self.assertFalse(capability["available"])
        self.assertFalse(capability["required"])
        self.assertIn("rest of AgentsServer works without it", capability["message"])
        run.assert_not_called()

    def test_working_tmux_is_reported_available(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/tmux"), patch.object(
            agent_server.subprocess,
            "run",
            return_value=completed(["/usr/local/bin/tmux", "-V"]),
        ):
            capability = agent_server.tmux_capability()

        self.assertTrue(capability["available"])
        self.assertFalse(capability["required"])

    def test_claude_ready_probe_does_not_expose_identity(self) -> None:
        responses = [
            completed(["claude", "--version"], stdout="2.3.4 (Claude Code)\n"),
            completed(
                ["claude", "auth", "status", "--json"],
                stdout=json.dumps({"loggedIn": True, "email": "private@example.com", "organizationName": "Secret"}),
            ),
        ]
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/claude"), patch.object(
            agent_server, "runtime_command", side_effect=responses
        ):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        self.assertEqual(diagnostic["status"], "ready")
        self.assertEqual(diagnostic["version"], "2.3.4 (Claude Code)")
        self.assertNotIn("private@example.com", json.dumps(diagnostic))
        self.assertNotIn("Secret", json.dumps(diagnostic))

    def test_codex_auth_failure_is_actionable(self) -> None:
        responses = [
            completed(["codex", "--version"], stdout="codex-cli 1.2.3\n"),
            completed(["codex", "login", "status"], returncode=1, stderr="Not logged in"),
        ]
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/codex"), patch.object(
            agent_server, "runtime_command", side_effect=responses
        ):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CODEX)
        self.assertEqual(diagnostic["status"], "unauthenticated")
        self.assertIn("codex login", diagnostic["action"])

    def test_codex_model_effort_validation_accepts_supported_ultra(self) -> None:
        self.assertEqual(
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "gpt-5.6-sol",
                "ultra",
                strict=True,
            ),
            "ultra",
        )

    def test_codex_model_effort_validation_rejects_known_bad_pair(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "gpt-5.6-luna",
                "ultra",
                strict=True,
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("gpt-5.6-luna", str(raised.exception.detail))
        self.assertIn("does not support effort ultra", str(raised.exception.detail))

    def test_codex_model_effort_validation_leaves_custom_models_provider_owned(self) -> None:
        self.assertEqual(
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "custom-provider-model",
                "ultra",
                strict=True,
            ),
            "ultra",
        )

    def test_codex_catalog_preserves_model_scoped_efforts(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "ultra"},
                    ],
                },
                {
                    "slug": "gpt-5.6-luna",
                    "display_name": "GPT-5.6-Luna",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "max"},
                    ],
                },
            ]
        }
        with patch.object(
            agent_server,
            "run_catalog_command",
            return_value=json.dumps(payload),
        ), patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("gpt-5.6-sol", "medium", "priority"),
        ):
            catalog = agent_server.discover_codex_catalog()

        self.assertEqual(
            [option["value"] for option in catalog["model_efforts"]["gpt-5.6-sol"]],
            ["medium", "ultra"],
        )
        self.assertEqual(
            [option["value"] for option in catalog["model_efforts"]["gpt-5.6-luna"]],
            ["medium", "max"],
        )

    def test_codex_catalog_default_matches_runtime_fallback(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                },
                {
                    "slug": agent_server.CODEX_DEFAULT_MODEL,
                    "display_name": "Runtime fallback",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "xhigh",
                    "supported_reasoning_levels": [{"effort": "xhigh"}],
                },
            ]
        }
        with patch.object(
            agent_server,
            "run_catalog_command",
            return_value=json.dumps(payload),
        ), patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("", "", ""),
        ):
            catalog = agent_server.discover_codex_catalog()

        self.assertEqual(catalog["default_model"], agent_server.CODEX_DEFAULT_MODEL)

    def test_transient_provider_failure_keeps_cli_ready(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CLAUDE,
            "ready",
            installed=True,
            authenticated=True,
            version="2.3.4",
        ))
        agent_server.record_runtime_failure(agent_server.BACKEND_CLAUDE, "529 overloaded")
        snapshot = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CLAUDE]
        self.assertEqual(snapshot["status"], "ready")
        self.assertIsNotNone(snapshot["last_error"])
        self.assertNotIn("checked_at_epoch", snapshot)

    def test_provider_thread_not_found_does_not_mark_cli_missing(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "ready",
            installed=True,
            authenticated=True,
            version="1.2.3",
        ))
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            "No conversation found with session ID: external-thread",
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "ready")
        self.assertTrue(diagnostic["installed"])

    def test_spawn_failure_marks_cli_missing(self) -> None:
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            FileNotFoundError(2, "No such file or directory", "codex"),
            spawn_failure=True,
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["installed"])

    def test_claude_catalog_parses_wrapped_effort_levels(self) -> None:
        help_text = """\
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --exclude-dynamic-system-prompt-sections
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=False
        ):
            catalog = agent_server.parse_claude_help_catalog()
        self.assertEqual(
            [option["value"] for option in catalog["efforts"]],
            ["", "low", "medium", "high", "xhigh", "max"],
        )

    def test_claude_catalog_advertises_supported_ultracode_effort(self) -> None:
        help_text = """\
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=True
        ):
            catalog = agent_server.parse_claude_help_catalog()
        self.assertEqual(
            [option["value"] for option in catalog["efforts"]],
            ["", "low", "medium", "high", "xhigh", "max", "ultracode"],
        )

    def test_claude_effort_probe_rejects_unknown_values(self) -> None:
        warning = "Warning: Unknown --effort value 'ultracode' - ignoring it and using the default effort."
        with patch.object(agent_server.subprocess, "run", return_value=completed([], stderr=warning)):
            self.assertFalse(agent_server.claude_supports_effort("ultracode"))

    def test_claude_effort_probe_accepts_silent_values(self) -> None:
        with patch.object(agent_server.subprocess, "run", return_value=completed([], stdout="2.1.207 (Claude Code)\n")):
            self.assertTrue(agent_server.claude_supports_effort("ultracode"))

    def test_cursor_catalog_locks_named_models_on_free_plan(self) -> None:
        list_models_output = (
            "Available models\n\n"
            "auto - Auto (default)\n"
            "gpt-5.2 - GPT-5.2\n"
        )
        about_output = (
            "About Cursor CLI\n\n"
            "CLI Version         2026.08.11-e8db854\n"
            "Subscription Tier   Free\n"
            "User Email          user@example.com\n"
        )
        with patch.object(
            agent_server, "run_catalog_command", side_effect=[list_models_output, about_output]
        ):
            catalog = agent_server.discover_cursor_catalog()

        by_value = {option["value"]: option for option in catalog["models"]}
        self.assertNotIn("locked", by_value["auto"])
        self.assertTrue(by_value["gpt-5.2"]["locked"])
        self.assertIn("free plan", by_value["gpt-5.2"]["locked_reason"])
        # The synthetic "Server default" entry mirrors "auto" (the default),
        # so it must stay selectable too - only named models lock.
        self.assertNotIn("locked", by_value[""])

    def test_cursor_catalog_does_not_lock_models_on_paid_plan(self) -> None:
        list_models_output = (
            "Available models\n\n"
            "auto - Auto (default)\n"
            "gpt-5.2 - GPT-5.2\n"
        )
        about_output = (
            "About Cursor CLI\n\n"
            "Subscription Tier   Pro\n"
            "User Email          user@example.com\n"
        )
        with patch.object(
            agent_server, "run_catalog_command", side_effect=[list_models_output, about_output]
        ):
            catalog = agent_server.discover_cursor_catalog()

        for option in catalog["models"]:
            self.assertNotIn("locked", option)

    def test_cursor_catalog_fails_toward_locked_when_tier_detection_errors(self) -> None:
        list_models_output = "Available models\n\nauto - Auto (default)\ngpt-5.2 - GPT-5.2\n"
        with patch.object(
            agent_server,
            "run_catalog_command",
            side_effect=[list_models_output, RuntimeError("agent about exited 1")],
        ):
            catalog = agent_server.discover_cursor_catalog()

        by_value = {option["value"]: option for option in catalog["models"]}
        self.assertTrue(by_value["gpt-5.2"]["locked"])


class RuntimePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_runtime_fails_before_launch(self) -> None:
        diagnostic = agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "unauthenticated",
            installed=True,
            authenticated=False,
        )
        with patch.object(agent_server, "runtime_diagnostic", return_value=diagnostic):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.ensure_runtime_available(agent_server.BACKEND_CODEX)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "runtime_unavailable")
        self.assertEqual(raised.exception.detail["backend"], "codex")


class SessionRuntimeValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_rejects_incompatible_effort_without_mutating_session(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            with self.assertRaises(HTTPException):
                await store.update("chat", {"effort": "ultra"})

        self.assertEqual(store.sessions["chat"]["effort"], "medium")

    async def test_model_only_change_clears_existing_incompatible_effort(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "ultra",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            updated = await store.update("chat", {"model": "gpt-5.6-luna"})

        self.assertEqual(updated["model"], "gpt-5.6-luna")
        self.assertIsNone(updated["effort"])

    async def test_explicit_incompatible_model_effort_pair_is_rejected(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "ultra",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            with self.assertRaises(HTTPException):
                await store.update(
                    "chat",
                    {"model": "gpt-5.6-luna", "effort": "ultra"},
                )

        self.assertEqual(store.sessions["chat"]["model"], "gpt-5.6-sol")
        self.assertEqual(store.sessions["chat"]["effort"], "ultra")

    async def test_supported_ultra_pair_is_persisted(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "folder": "General",
            }
        }
        save = AsyncMock()
        with patch.object(store, "save", save):
            updated = await store.update("chat", {"effort": "ultra"})

        self.assertEqual(updated["effort"], "ultra")
        save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
