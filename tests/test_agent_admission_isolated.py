"""Admission regressions using actual helpers/defaults, without server startup."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
FUNCTIONS = {
    "env_setting", "agentsdock_setting", "turn_start_blocker", "scheduled_job_blocker",
    "low_available_memory_message",
}
SETTINGS = {
    "MAX_ACTIVE_AGENT_RUNS", "JOB_MAX_ACTIVE_RUNS",
    "MIN_START_AVAILABLE_MEM_MB", "JOB_MIN_AVAILABLE_MEM_MB",
}


def compile_admission():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in SETTINGS for target in node.targets)
    )]
    assert len(selected) == len(FUNCTIONS) + len(SETTINGS)
    # Retain only the compiled allowlist, not the entire server AST throughout
    # the suite: a large live syntax tree adds unrelated GC work to async tests.
    return compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec")


ADMISSION_CODE = compile_admission()


def load_admission(env=None):
    ns = {
        "os": SimpleNamespace(environ=dict(env or {})),
        "ACTIVE_LOCK": asyncio.Lock(),
        "BUSY_SESSIONS": {f"busy-{index}" for index in range(100)},
        "managed_server_update_admission_blocker": lambda: None,
        "managed_server_update_scheduled_job_blocker": lambda **_: None,
        "host_pressure_snapshot": lambda: {"available_mem_mb": 16384},
        "STORE": SimpleNamespace(sessions={}),
        "CODEX_GOALS_RECONFIGURING": False,
        "DEFAULT_BACKEND": "claude", "BACKEND_CODEX": "codex",
        "SERVER_MAINTENANCE_SESSIONS": set(), "CLAUDE_STOP_FENCE_SESSIONS": set(),
        "stop_cleanup_in_progress": lambda _: False,
    }
    exec(ADMISSION_CODE, ns)
    return ns


class AgentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_allows_more_than_ten_chat_and_cron_runs(self):
        ns = load_admission()
        self.assertEqual(ns["MAX_ACTIVE_AGENT_RUNS"], 0)
        self.assertEqual(ns["JOB_MAX_ACTIVE_RUNS"], 0)
        self.assertEqual(ns["MIN_START_AVAILABLE_MEM_MB"], 512)
        self.assertEqual(ns["JOB_MIN_AVAILABLE_MEM_MB"], 4096)
        self.assertIsNone(await ns["turn_start_blocker"]())
        self.assertIsNone(await ns["turn_start_blocker"](ignore_session_id="busy-0"))
        self.assertIsNone(await ns["scheduled_job_blocker"]("new-cron"))

    async def test_explicit_operator_cap_is_respected_including_legacy_config(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            ns = load_admission({f"{prefix}_MAX_ACTIVE_AGENT_RUNS": "100"})
            self.assertIn("100 active", await ns["turn_start_blocker"]())
            self.assertIsNone(await ns["turn_start_blocker"](ignore_session_id="busy-0"))
            self.assertIn("100 active", await ns["scheduled_job_blocker"]("new-cron"))
        ns = load_admission({
            "AGENTSDOCK_MAX_ACTIVE_AGENT_RUNS": "0",
            "ZENITHBOT_MAX_ACTIVE_AGENT_RUNS": "10",
        })
        self.assertIsNone(await ns["turn_start_blocker"]())

    async def test_interactive_memory_boundaries_and_reported_567_mib(self):
        ns = load_admission()
        for available in (0, 480, 511, 512, 513, 567, 1024, 2048):
            with self.subTest(available=available):
                ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": available}
                blocker = await ns["turn_start_blocker"]()
                if available < 512:
                    self.assertEqual(blocker,
                        f"low available memory on the server: {available} MiB available; "
                        "at least 512 MiB required to start an agent turn. "
                        "Close unused applications or stop other agent runs on the server, then retry.")
                else:
                    self.assertIsNone(blocker)

    async def test_scheduled_memory_floor_and_action_are_independent(self):
        ns = load_admission()
        for available in (512, 567, 1024, 3072, 4095, 4096, 4097):
            with self.subTest(available=available):
                ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": available}
                self.assertIsNone(await ns["turn_start_blocker"]())
                for manual in (False, True):
                    blocker = await ns["scheduled_job_blocker"]("new-cron", manual=manual)
                    if available < 4096:
                        self.assertIn(f"{available} MiB available", blocker)
                        self.assertIn("at least 4096 MiB required to start a scheduled job", blocker)
                        self.assertIn("then retry", blocker)
                    else:
                        self.assertIsNone(blocker)

    async def test_memory_overrides_use_effective_floor_including_legacy(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            for minimum in (256, 1024, 2048):
                ns = load_admission({f"{prefix}_MIN_START_AVAILABLE_MEM_MB": str(minimum)})
                self.assertEqual(ns["MIN_START_AVAILABLE_MEM_MB"], minimum)
                for available in (minimum - 1, minimum, minimum + 1, 567):
                    with self.subTest(prefix=prefix, minimum=minimum, available=available):
                        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": available}
                        blocker = await ns["turn_start_blocker"]()
                        if available < minimum:
                            self.assertIn(f"{available} MiB available", blocker)
                            self.assertIn(f"at least {minimum} MiB required", blocker)
                        else:
                            self.assertIsNone(blocker)

    async def test_canonical_memory_override_wins_over_legacy(self):
        for minimum in (0, 512):
            ns = load_admission({
                "AGENTSDOCK_MIN_START_AVAILABLE_MEM_MB": str(minimum),
                "ZENITHBOT_MIN_START_AVAILABLE_MEM_MB": "2048",
                "AGENTSDOCK_JOB_MIN_AVAILABLE_MEM_MB": "512",
                "ZENITHBOT_JOB_MIN_AVAILABLE_MEM_MB": "8192",
            })
            self.assertEqual(ns["MIN_START_AVAILABLE_MEM_MB"], minimum)
            self.assertEqual(ns["JOB_MIN_AVAILABLE_MEM_MB"], 512)
            ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 567}
            self.assertIsNone(await ns["turn_start_blocker"]())
            self.assertIsNone(await ns["scheduled_job_blocker"]("new-cron"))

    async def test_scheduled_memory_override_and_global_guard_are_both_respected(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            ns = load_admission({f"{prefix}_JOB_MIN_AVAILABLE_MEM_MB": "1024"})
            for available in (567, 1023, 1024):
                ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": available}
                blocker = await ns["scheduled_job_blocker"]("new-cron")
                if available < 1024:
                    self.assertIn("at least 1024 MiB required to start a scheduled job", blocker)
                else:
                    self.assertIsNone(blocker)
        ns = load_admission({"AGENTSDOCK_JOB_MIN_AVAILABLE_MEM_MB": "0"})
        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 511}
        self.assertIn("at least 512 MiB required", await ns["scheduled_job_blocker"]("new-cron"))
        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 567}
        self.assertIsNone(await ns["scheduled_job_blocker"]("new-cron"))

    async def test_explicit_opt_out_and_unknown_memory_preserve_existing_behavior(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            ns = load_admission({f"{prefix}_MIN_START_AVAILABLE_MEM_MB": "0",
                                 f"{prefix}_JOB_MIN_AVAILABLE_MEM_MB": "0"})
            ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 0}
            self.assertIsNone(await ns["turn_start_blocker"]())
            self.assertIsNone(await ns["scheduled_job_blocker"]("new-cron"))
        ns = load_admission()
        for pressure in ({}, {"available_mem_mb": None}):
            ns["host_pressure_snapshot"] = lambda: pressure
            self.assertIsNone(await ns["turn_start_blocker"]())
            self.assertIsNone(await ns["scheduled_job_blocker"]("new-cron"))

    async def test_count_and_update_guards_still_block_at_567_mib(self):
        ns = load_admission({"AGENTSDOCK_MAX_ACTIVE_AGENT_RUNS": "100"})
        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 567}
        self.assertEqual(await ns["turn_start_blocker"](), "server already has 100 active agent run(s)")
        self.assertIsNone(await ns["turn_start_blocker"](ignore_session_id="busy-0"))
        ns["managed_server_update_admission_blocker"] = lambda: "update activating"
        self.assertEqual(await ns["turn_start_blocker"](), "update activating")
        ns["managed_server_update_scheduled_job_blocker"] = lambda **_: "scheduled update pending"
        self.assertEqual(await ns["scheduled_job_blocker"]("new-cron"), "scheduled update pending")

    async def test_provider_maintenance_stop_and_codex_goals_guards_remain(self):
        ns = load_admission()
        ns["SERVER_MAINTENANCE_SESSIONS"].add("chat")
        self.assertEqual(await ns["scheduled_job_blocker"]("chat"), "wait for provider session maintenance to finish")
        ns["CLAUDE_STOP_FENCE_SESSIONS"].add("chat")
        self.assertEqual(await ns["scheduled_job_blocker"]("chat"), "wait for Claude Stop recovery to finish")
        ns["SERVER_MAINTENANCE_SESSIONS"].clear()
        ns["stop_cleanup_in_progress"] = lambda _: True
        self.assertEqual(await ns["scheduled_job_blocker"]("chat"), "chat is finishing an explicit Stop")
        ns["CODEX_GOALS_RECONFIGURING"] = True
        ns["STORE"].sessions["chat"] = {"backend": "codex"}
        self.assertEqual(await ns["scheduled_job_blocker"]("chat"), "wait for Codex goals configuration to finish")

    async def test_update_drain_and_existing_chat_ownership_remain_protected(self):
        ns = load_admission()
        self.assertEqual(await ns["scheduled_job_blocker"]("busy-0"), "chat already has a running turn")
        ns["managed_server_update_admission_blocker"] = lambda: "update activating"
        self.assertEqual(await ns["turn_start_blocker"](), "update activating")
        self.assertEqual(await ns["scheduled_job_blocker"]("new-cron"), "update activating")

    async def test_independent_explicit_cron_cap_is_preserved(self):
        ns = load_admission({"AGENTSDOCK_JOB_MAX_ACTIVE_RUNS": "100"})
        self.assertIsNone(await ns["turn_start_blocker"]())
        self.assertIn("100 active", await ns["scheduled_job_blocker"]("new-cron"))


if __name__ == "__main__":
    unittest.main()
