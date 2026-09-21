"""Retirement races through real admission helpers, without production state."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from execution_control import ExecutionControlError
from execution_maintenance import ExecutionMaintenance


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "execution_maintenance_control", "managed_server_update_blocks_work",
    "server_update_blocker_counts", "server_update_active_session_ids_locked",
    "unsafe_http_mutation_count_locked", "live_unsafe_http_mutation_ids_locked",
    "official_server_release_tree",
}


def compile_helpers():
    selected = [node for node in ast.parse(SOURCE.read_text()).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in FUNCTIONS]
    assert len(selected) == len(FUNCTIONS)
    return compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec")


CODE = compile_helpers()


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.operation = str(uuid.uuid4())
        self.manager = ExecutionMaintenance(self.root / "hold.json", "epoch")
        self.ns = {
            "Any": Any, "Path": Path,
            "os": SimpleNamespace(environ={"AGENTS_SERVER_INSTALL_DIR": str(self.root)}),
            "EXECUTION_MAINTENANCE": self.manager,
            "ACTIVE_LOCK": asyncio.Lock(), "QUEUE_LOCK": asyncio.Lock(),
            "UNSAFE_HTTP_MUTATION_ADMISSION_LOCK": asyncio.Lock(),
            "BUSY_SESSIONS": set(), "SERVER_MAINTENANCE_SESSIONS": set(),
            "DELETING_SESSIONS": set(), "CODEX_GOALS_RECONFIGURING": False,
            "UNSAFE_HTTP_MUTATION_TASKS": {},
            "explicit_stop_session_ids": lambda: set(),
            "update_blocking_queued_turn_count_locked": lambda: 0,
            "prepare_provider_background_work_snapshot": AsyncMock(return_value={}),
            "provider_background_work_labels_from_snapshot": lambda _: [],
            "managed_server_restart_blocks_work": lambda: False,
            "managed_update_provider_quiesce_in_progress": lambda: False,
            "read_server_update_status": lambda: {},
            "SERVER_UPDATE_ACTIVE_PHASES": {"installing", "restarting"},
        }
        exec(CODE, self.ns)

    async def acquire(self):
        return await self.ns["execution_maintenance_control"]("acquire", self.operation)

    async def test_admitted_turn_wins_race_and_prevents_retirement(self):
        # Retirement must wait for the same lock that reserves a provider turn.
        async with self.ns["ACTIVE_LOCK"]:
            retirement = asyncio.create_task(self.acquire())
            await asyncio.sleep(0)
            self.assertFalse(retirement.done())
            self.ns["BUSY_SESSIONS"].add("accepted-turn")
        with self.assertRaises(ExecutionControlError):
            await retirement
        self.assertFalse(self.manager.is_held())

    async def test_accepted_mutation_and_background_child_block_idle_handover(self):
        self.ns["UNSAFE_HTTP_MUTATION_TASKS"]["accepted"] = asyncio.current_task()
        with self.assertRaises(ExecutionControlError):
            await self.acquire()
        self.ns["UNSAFE_HTTP_MUTATION_TASKS"].clear()
        result = await self.acquire()
        self.assertTrue(self.ns["managed_server_update_blocks_work"]())
        self.ns["provider_background_work_labels_from_snapshot"] = lambda _: ["active subagent"]
        with self.assertRaises(ExecutionControlError):
            await self.ns["execution_maintenance_control"](
                "seal", self.operation, result["lease"]["lease_id"])
        self.assertFalse(self.manager.lease["sealed"])

    async def test_sealed_hold_closes_admission_until_exact_release(self):
        result = await self.acquire()
        await self.ns["execution_maintenance_control"](
            "seal", self.operation, result["lease"]["lease_id"])
        self.assertTrue(self.ns["managed_server_update_blocks_work"]())
        status = await self.ns["execution_maintenance_control"]()
        self.assertTrue(status["idle"])
        self.assertTrue(status["lease"]["sealed"])
        await self.ns["execution_maintenance_control"](
            "release", self.operation, result["lease"]["lease_id"])
        self.assertFalse(self.ns["managed_server_update_blocks_work"]())

    async def test_existing_managed_update_cannot_race_worker_retirement(self):
        self.ns["read_server_update_status"] = lambda: {"phase": "installing"}
        with self.assertRaises(ExecutionControlError):
            await self.acquire()
        self.assertFalse(self.manager.is_held())

    def test_pinned_execution_proof_survives_gateway_advance_but_not_tampering(self):
        new = self.root / "new"
        old = self.root / "old"
        new.mkdir()
        old.mkdir()
        (self.root / "current").symlink_to(new)
        self.ns["SERVER_ROOT"] = old
        with patch("execution_install.active_worker_release", return_value=old):
            self.assertTrue(self.ns["official_server_release_tree"]())
        with patch("execution_install.active_worker_release", side_effect=ValueError("invalid")):
            self.assertFalse(self.ns["official_server_release_tree"]())
        self.ns["EXECUTION_MAINTENANCE"] = None
        self.assertFalse(self.ns["official_server_release_tree"]())
        self.ns["SERVER_ROOT"] = new
        self.assertTrue(self.ns["official_server_release_tree"]())


if __name__ == "__main__":
    unittest.main()
