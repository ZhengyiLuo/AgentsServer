"""Authentication headroom and bounded, non-poisoning catalog refreshes."""

import asyncio
import json
import subprocess
import sys
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import agent_server


def completed(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class RuntimeProbeTimeoutTests(unittest.TestCase):
    def setUp(self):
        with agent_server.RUNTIME_DIAGNOSTICS_LOCK:
            agent_server.RUNTIME_DIAGNOSTICS.clear()
            agent_server.RUNTIME_DIAGNOSTIC_GENERATIONS.clear()
        token = agent_server.RUNTIME_CATALOG_DEADLINE.set(None)
        self.addCleanup(agent_server.RUNTIME_CATALOG_DEADLINE.reset, token)
        self.enterContext(patch.object(agent_server, "RUNTIME_CATALOG_TIMEOUT_SECONDS", 6.0))
        self.enterContext(patch.object(agent_server, "CLAUDE_AUTH_PROBE_TIMEOUT_SECONDS", 15.0))
        self.enterContext(patch.object(agent_server, "runner_env", return_value={}))

    def test_slow_successful_claude_auth_gets_15_seconds_not_six(self):
        def run(cmd, **kwargs):
            if cmd[1:] == ["--version"]:
                self.assertEqual(kwargs["timeout"], 6.0)
                return completed(cmd, "2.1.277 (Claude Code)")
            self.assertEqual(cmd[1:], ["auth", "status", "--json"])
            self.assertEqual(kwargs["timeout"], 15.0)
            # The real regression: a successful 6.6s auth check must survive.
            if kwargs["timeout"] < 6.6:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return completed(cmd, json.dumps({"loggedIn": True, "email": "private@example.com"}))

        with patch.object(agent_server.shutil, "which", return_value="/test/claude"), patch.object(
            agent_server.subprocess, "run", side_effect=run,
        ):
            result = agent_server.probe_runtime("claude")
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("private@example.com", json.dumps(result))

    def test_auth_timeout_is_explicit_unknown_auth_and_does_not_leak_output(self):
        error = subprocess.TimeoutExpired(
            ["claude", "auth", "status", "--json"], 15,
            output=b"private-account@example.com", stderr=b"secret-token",
        )
        with patch.object(agent_server.shutil, "which", return_value="/test/claude"), patch.object(
            agent_server, "runtime_command", side_effect=[completed([], "2.1.277"), error],
        ), self.assertLogs(agent_server.logger, level="WARNING") as logs:
            result = agent_server.probe_runtime("claude")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["authenticated"])
        self.assertTrue(result["installed"])
        self.assertIn("authentication check timed out", result["message"])
        self.assertNotIn("auth login", result["action"])
        exposed = json.dumps(result) + str(logs.output)
        self.assertNotIn("private-account", exposed)
        self.assertNotIn("secret-token", exposed)

    def test_explicit_signed_out_still_blocks(self):
        with patch.object(agent_server.shutil, "which", return_value="/test/claude"), patch.object(
            agent_server, "runtime_command", side_effect=[
                completed([], "2.1.277"), completed([], '{"loggedIn":false}', 1),
            ],
        ):
            result = agent_server.probe_runtime("claude")
        self.assertEqual(result["status"], "unauthenticated")
        self.assertFalse(result["available"])
        self.assertFalse(result["authenticated"])
        self.assertIn("claude auth login", result["action"])

    def test_version_help_and_other_auth_keep_six_second_limit(self):
        with patch.object(agent_server.subprocess, "run", return_value=completed([])) as run:
            for cmd in (["claude", "--version"], ["codex", "login", "status"], ["agent", "status"]):
                agent_server.runtime_command(cmd)
                self.assertEqual(run.call_args.kwargs["timeout"], 6.0)
            agent_server.run_catalog_command(["claude", "--help"])
            self.assertEqual(run.call_args.kwargs["timeout"], 6.0)
            agent_server.claude_supports_effort("ultracode")
            self.assertEqual(run.call_args.kwargs["timeout"], 6.0)

    def test_auth_deadline_is_configurable(self):
        with patch.object(agent_server, "CLAUDE_AUTH_PROBE_TIMEOUT_SECONDS", 19.0), patch.object(
            agent_server.shutil, "which", return_value="/test/claude",
        ), patch.object(agent_server.subprocess, "run", side_effect=[
            completed([], "2.1.277"), completed([], '{"loggedIn":true}'),
        ]) as run:
            agent_server.probe_runtime("claude")
        self.assertEqual(run.call_args.kwargs["timeout"], 19.0)

    def test_remaining_catalog_budget_clamps_each_command(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(102.0)
        with patch.object(agent_server.time, "monotonic", return_value=100.0), patch.object(
            agent_server.subprocess, "run", return_value=completed([]),
        ) as run:
            agent_server.runtime_command(["claude", "auth", "status"], timeout_seconds=15)
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
            agent_server.run_catalog_command(["claude", "--help"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
            agent_server.claude_supports_effort("ultracode")
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_exhausted_budget_starts_no_subprocess(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(100.0)
        with patch.object(agent_server.time, "monotonic", return_value=100.0), patch.object(
            agent_server.subprocess, "run",
        ) as run, self.assertRaises(agent_server.RuntimeCatalogBudgetExpired):
            agent_server.runtime_command(["claude", "--version"])
        run.assert_not_called()

    def test_budget_expiry_does_not_poison_or_freshen_cached_diagnostic(self):
        previous = agent_server.runtime_diagnostic_payload(
            "claude", "ready", installed=True, authenticated=True, version="2.1.277",
        )
        previous.update(checked_at="2026-09-18T01:00:00Z", checked_at_epoch=0.0)
        agent_server.store_runtime_diagnostic(previous)
        with patch.object(agent_server, "probe_runtime", side_effect=agent_server.RuntimeCatalogBudgetExpired):
            result = agent_server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result, previous)
        self.assertEqual(agent_server.RUNTIME_DIAGNOSTICS["claude"], previous)

    def test_unchecked_provider_remains_unknown_and_uncached(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(0.0)
        with patch.object(agent_server, "probe_runtime") as probe, patch.object(
            agent_server, "runtime_executable",
        ) as resolve:
            result = agent_server.runtime_diagnostic("cursor", force=True)
        probe.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["installed"])
        self.assertIsNone(result["authenticated"])
        self.assertIn("deadline", result["message"])
        self.assertNotIn("cursor", agent_server.RUNTIME_DIAGNOSTICS)

    def test_completed_signed_out_result_is_not_discarded_at_budget_boundary(self):
        clock = [100.0]
        agent_server.RUNTIME_CATALOG_DEADLINE.set(101.0)
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            "claude", "ready", installed=True, authenticated=True,
        ))

        def probe(backend):
            clock[0] = 101.0
            return agent_server.runtime_diagnostic_payload(
                backend, "unauthenticated", installed=True, authenticated=False,
            )

        with patch.object(agent_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            agent_server, "probe_runtime", side_effect=probe,
        ):
            result = agent_server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result["status"], "unauthenticated")
        self.assertEqual(agent_server.RUNTIME_DIAGNOSTICS["claude"]["status"], "unauthenticated")

    def test_cursor_catalog_resolution_budget_expiry_returns_safe_auto_fallback(self):
        with patch.object(agent_server, "resolve_cursor_executable", side_effect=agent_server.RuntimeCatalogBudgetExpired), patch.object(
            agent_server, "run_catalog_command",
        ) as run:
            result = agent_server.discover_cursor_catalog()
        run.assert_not_called()
        self.assertEqual(result["models"], [{"value": "auto", "label": "Auto"}])

    def test_budget_exhaustion_is_not_a_normal_auth_timeout(self):
        clock = [100.0]
        agent_server.RUNTIME_CATALOG_DEADLINE.set(102.0)

        def run(cmd, **kwargs):
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch.object(agent_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            agent_server.subprocess, "run", side_effect=run,
        ), self.assertRaises(agent_server.RuntimeCatalogBudgetExpired):
            agent_server.runtime_command(["claude", "auth", "status"], timeout_seconds=15)

    def test_models_api_cannot_outlive_remaining_budget(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(100.25)
        with patch.dict(agent_server.os.environ, {"ANTHROPIC_API_KEY": "test-not-a-real-key"}), patch.object(
            agent_server.shutil, "which", return_value="/test/curl",
        ), patch.object(agent_server.time, "monotonic", return_value=100.0), patch.object(
            agent_server.subprocess, "run", return_value=completed([], b'{"data":[]}'),
        ) as run:
            _, status = agent_server.discover_claude_provider_models()
        self.assertEqual(status, "success")
        self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--max-time") + 1], "0.25")

    def test_native_models_cannot_outlive_remaining_catalog_budget(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(102.0)
        with patch.object(agent_server.time, "monotonic", return_value=100.0), patch(
            "claude_model_catalog.probe_native_models", return_value=[],
        ) as probe:
            _, status = agent_server.discover_claude_native_models()
        self.assertEqual(status, "success")
        self.assertEqual(probe.call_args.kwargs["timeout"], 2.0)

    def test_native_models_expired_budget_starts_no_process(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(100.0)
        with patch.object(agent_server.time, "monotonic", return_value=100.0), patch(
            "claude_model_catalog.probe_native_models",
        ) as probe:
            models, status = agent_server.discover_claude_native_models()
        probe.assert_not_called()
        self.assertEqual((models, status), ([], "unavailable"))

    def test_models_api_respects_callers_smaller_candidate_budget(self):
        with patch.dict(agent_server.os.environ, {"ANTHROPIC_API_KEY": "synthetic-key"}), patch.object(
            agent_server.shutil, "which", return_value="/test/curl",
        ), patch.object(agent_server.subprocess, "run", return_value=completed([], b'{"data":[]}')) as run:
            _, status = agent_server.discover_claude_provider_models(timeout_seconds=2.0)
        self.assertEqual(status, "success")
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)  # One second to reap curl.
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--max-time") + 1], "2")

    def test_catalog_deadline_restored_on_exception_and_not_shared_between_threads(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(123.0)
        seen = []
        thread = threading.Thread(target=lambda: seen.append(agent_server.RUNTIME_CATALOG_DEADLINE.get()))
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(seen, [None])
        with patch.object(agent_server, "discover_runtime_catalog_within_budget", side_effect=ValueError("test")):
            with self.assertRaises(ValueError):
                agent_server.discover_runtime_catalog()
        self.assertEqual(agent_server.RUNTIME_CATALOG_DEADLINE.get(), 123.0)

    def test_full_catalog_slow_claude_and_stalled_peers_share_25_second_budget(self):
        clock = [100.0]
        started = []

        class InlinePool:
            """Deterministic worst-case budget without a racing virtual clock."""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def submit(self, fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        def run(cmd, **kwargs):
            self.assertLess(clock[0], 125.0)
            started.append((cmd, kwargs["timeout"]))
            if cmd[0] == "/test/claude" and cmd[1:] == ["--version"]:
                clock[0] += 0.1
                return completed(cmd, "2.1.277")
            if cmd[0] == "/test/claude" and cmd[1:] == ["auth", "status", "--json"]:
                self.assertGreaterEqual(kwargs["timeout"], 6.6)
                clock[0] += 6.6
                return completed(cmd, '{"loggedIn":true}')
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch.object(agent_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            agent_server.shutil, "which", side_effect=lambda cmd, **kw: "/test/" + cmd,
        ), patch.object(agent_server, "CLAUDE_BIN", "claude"), patch.object(
            agent_server.subprocess, "run", side_effect=run,
        ), patch.object(agent_server, "codex_user_config_defaults", return_value=("", "", "")), patch.dict(
            agent_server.os.environ, {"ANTHROPIC_API_KEY": ""},
        ), patch.object(
            agent_server, "ThreadPoolExecutor", return_value=InlinePool(),
        ):
            result = agent_server.discover_runtime_catalog(force_runtime_probe=True)
        self.assertAlmostEqual(clock[0] - 100, 25.0)
        self.assertTrue(started)
        self.assertEqual(set(result["backends"]), agent_server.VALID_BACKENDS)
        self.assertEqual(result["backends"]["claude"]["diagnostic"]["status"], "ready")
        self.assertEqual(result["backends"]["opencode"]["diagnostic"]["status"], "unknown")
        self.assertNotIn("opencode", agent_server.RUNTIME_DIAGNOSTICS)
        self.assertTrue(result["backends"]["claude"]["models"])
        self.assertIsNone(agent_server.RUNTIME_CATALOG_DEADLINE.get())

    def test_provider_checks_start_concurrently_and_inherit_refresh_deadline(self):
        agent_server.RUNTIME_CATALOG_DEADLINE.set(123.0)
        barrier = threading.Barrier(len(agent_server.VALID_BACKENDS))
        seen = {}
        lock = threading.Lock()

        def probe(backend, *, force_runtime_probe):
            with lock:
                seen[backend] = (force_runtime_probe, agent_server.RUNTIME_CATALOG_DEADLINE.get())
            # A sequential implementation would time out before reaching the
            # other providers, rather than letting the whole group proceed.
            barrier.wait(timeout=5)
            return {"backend": backend, "status": "ready"}

        with patch.object(agent_server, "discover_runtime_backend_catalog", side_effect=probe):
            results = agent_server.discover_runtime_catalog_within_budget(force_runtime_probe=True)
        self.assertEqual(set(results["backends"]), agent_server.VALID_BACKENDS)
        self.assertEqual(seen, {backend: (True, 123.0) for backend in agent_server.VALID_BACKENDS})
        self.assertEqual(agent_server.RUNTIME_CATALOG_DEADLINE.get(), 123.0)

    def test_slow_peer_diagnostic_cannot_starve_healthy_opencode_models(self):
        slow_started = threading.Event()
        models_finished = threading.Event()
        static = {"models": [], "efforts": []}

        def diagnostic(backend, *, force):
            if backend == "cursor":
                slow_started.set()
                self.assertTrue(models_finished.wait(timeout=5))
                return {"backend": backend, "status": "error"}
            return {"backend": backend, "status": "ready", "_executable": "/test/" + backend}

        def opencode_models(**_kwargs):
            self.assertTrue(slow_started.wait(timeout=5))
            models_finished.set()
            return {"models": [{"value": "opencode/test", "label": "Test"}], "efforts": []}

        with patch.object(agent_server, "runtime_diagnostic", side_effect=diagnostic), patch.object(
            agent_server, "parse_claude_help_catalog", return_value=dict(static),
        ), patch.object(agent_server, "discover_codex_catalog", return_value=dict(static)), patch.object(
            agent_server, "discover_opencode_catalog", side_effect=opencode_models,
        ):
            result = agent_server.discover_runtime_catalog(force_runtime_probe=True)
        self.assertEqual(result["backends"]["opencode"]["models"][0]["value"], "opencode/test")
        self.assertTrue(result["backends"]["opencode"]["available"])

    def test_http_catalog_route_propagates_budget_to_worker_and_returns_json(self):
        seen = []

        def discover(**kwargs):
            seen.append((kwargs, agent_server.RUNTIME_CATALOG_DEADLINE.get()))
            return {"backends": {}, "generated_at": "test"}

        with patch.object(agent_server, "discover_runtime_catalog_within_budget", side_effect=discover):
            result = asyncio.run(agent_server.runtime_catalog(refresh=True))
        self.assertEqual(json.loads(json.dumps(result))["backends"], {})
        self.assertEqual(seen[0][0], {"force_runtime_probe": True})
        self.assertIsInstance(seen[0][1], float)
        self.assertIsNone(agent_server.RUNTIME_CATALOG_DEADLINE.get())

    def test_real_subprocess_timeout_kills_and_reaps_child(self):
        # Exercise real subprocess cleanup, without a provider or credentials.
        with self.assertRaises(subprocess.TimeoutExpired):
            agent_server.runtime_command(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_seconds=0.1,
            )


if __name__ == "__main__":
    unittest.main()
