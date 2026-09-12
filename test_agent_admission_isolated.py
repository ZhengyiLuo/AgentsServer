"""Admission regressions using actual helpers/defaults, without server startup."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).with_name("agent_server.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
FUNCTIONS = {
    "env_setting", "agentsdock_setting", "turn_start_blocker", "scheduled_job_blocker",
}
SETTINGS = {
    "MAX_ACTIVE_AGENT_RUNS", "JOB_MAX_ACTIVE_RUNS",
    "MIN_START_AVAILABLE_MEM_MB", "JOB_MIN_AVAILABLE_MEM_MB",
}


def load_admission(env=None):
    selected = [node for node in TREE.body if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in SETTINGS for target in node.targets)
    )]
    assert len(selected) == len(FUNCTIONS) + len(SETTINGS)
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
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), ns)
    return ns


class AgentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_allows_more_than_ten_chat_and_cron_runs(self):
        ns = load_admission()
        self.assertEqual(ns["MAX_ACTIVE_AGENT_RUNS"], 0)
        self.assertEqual(ns["JOB_MAX_ACTIVE_RUNS"], 0)
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

    async def test_memory_protection_remains_without_a_count_cap(self):
        ns = load_admission()
        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 1024}
        self.assertEqual(await ns["turn_start_blocker"](), "low available memory (1024 MB)")
        self.assertEqual(await ns["scheduled_job_blocker"]("new-cron"), "low available memory (1024 MB)")
        ns["host_pressure_snapshot"] = lambda: {"available_mem_mb": 3072}
        self.assertIsNone(await ns["turn_start_blocker"]())
        self.assertEqual(await ns["scheduled_job_blocker"]("new-cron"), "low available memory (3072 MB)")

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
