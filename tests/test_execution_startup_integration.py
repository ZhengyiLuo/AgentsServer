"""Production worker startup through a real pending execution transaction.

The application, lifespan, queue recovery, scheduler and HTTP controls are real.
Native service managers are not invoked. Provider executables are deliberately
disabled, with isolated state/history/workspaces; no model calls or login changes.
Run with the server dependency environment, like the other process tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx

import execution_install as install
from scripts.package_release import DIRECTORY_FILES, FILES


SOURCE = Path(__file__).resolve().parents[1]
SESSION_ID = "chat_startup_fixture"
QUEUED_ID = "queued_startup_fixture"
JOB_ID = "job_startup_fixture"
MAINTENANCE_DETAIL = "AgentsServer is preparing a managed update"


class ExecutionStartupProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # AF_UNIX has a small path bound on macOS. These paths never address a
        # real LaunchAgents folder, service, provider installation or credential.
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-start-", dir="/tmp")
        self.base = Path(self.temporary.name).resolve()
        self.base.chmod(0o700)
        self.processes = []
        self.logs = []
        self.clients = []
        self.addAsyncCleanup(self.shutdown)
        self.home = self.base / "service-home"
        self.root = self.base / "install"
        self.state = self.base / "state"
        self.config = self.base / "config"
        self.workspace = self.base / "workspace"
        for directory in (self.home, self.root, self.state, self.config,
                          self.workspace, self.base / "codex-history",
                          self.base / "claude-history", self.base / "tmp"):
            directory.mkdir(mode=0o700)
        version = (SOURCE / "VERSION").read_text().strip()
        self.release = self.root / "releases" / version
        self.release.mkdir(mode=0o700, parents=True)
        for name in FILES:
            shutil.copyfile(SOURCE / name, self.release / name)
            (self.release / name).chmod(0o600)
        for directory, names in DIRECTORY_FILES.items():
            for name in names:
                destination = self.release / directory / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / directory / name, destination)
                destination.chmod(0o600)
        # Preserve the active venv interpreter path. Resolving its symlink can
        # silently start the underlying Python without the server dependencies.
        interpreter = self.release / ".venv/bin/python"
        interpreter.parent.mkdir(mode=0o700, parents=True)
        interpreter.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        interpreter.chmod(0o700)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.layout = install.ExecutionLayout(
            self.root, self.config, self.state, self.home, platform.system(),
            self.release, self.release, "127.0.0.1", self.port, {})
        install.prepare_runtime(self.layout)
        transaction = install.stage(self.layout, scope="migration", prior_services={
            role: {"state": "absent", "enabled": False}
            for role in ("worker", "gateway")})
        install.publish(self.root)
        self.operation = install.maintenance_operation_id(transaction)
        self.token = uuid.uuid4().hex
        # Explicit missing executables prevent any native provider invocation,
        # including a regression that incorrectly opens admission during boot.
        # Do not override HOME/CODEX_HOME or borrow provider credentials.
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("AGENTSDOCK_", "AGENTS_SERVER_",
                                "ZENITHDOCK_", "ZENITHBOT_", "ANTHROPIC_", "OPENAI_"))}
        self.environment.update({
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "AGENTS_SERVER_INSTALL_DIR": str(self.root),
            "AGENTS_SERVER_CONFIG_DIR": str(self.config),
            "AGENTSDOCK_STATE_DIR": str(self.state),
            "AGENTSDOCK_AGENT_CWD": str(self.workspace),
            "AGENTSDOCK_AGENT_TOKEN": self.token,
            "AGENTSDOCK_TEAM_HUB_MODE": "disabled",
            "AGENTSDOCK_TEAM_HUB_TRANSPORT": "loopback",
            "AGENTSDOCK_AUTO_TITLES": "0",
            "AGENTSDOCK_JOB_SCHEDULER_INTERVAL_SECONDS": "1",
            "CODEX_SESSIONS_ROOT": str(self.base / "codex-history"),
            "CLAUDE_PROJECTS_ROOT": str(self.base / "claude-history"),
            "CODEX_BIN": str(self.base / "no-codex"),
            "CLAUDE_BIN": str(self.base / "no-claude"),
            "CURSOR_BIN": str(self.base / "no-cursor"),
            "TMPDIR": str(self.base / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def seed_waiting_work(self):
        """Use the persisted schemas consumed by SessionStore/JobStore/recovery."""
        timestamp = "2026-09-21T00:00:00Z"
        session = {"id": SESSION_ID, "title": "Owned startup fixture", "backend": "codex",
                   "cwd": str(self.workspace), "created_at": timestamp, "updated_at": timestamp,
                   "archived": False, "auto_title_enabled": False, "provider_jobs_access": "blocked"}
        (self.state / "sessions.json").write_text(json.dumps({SESSION_ID: session}))
        events = self.state / "sessions" / SESSION_ID / "events.jsonl"
        events.parent.mkdir(mode=0o700, parents=True)
        events.write_text(json.dumps({"id": "event_startup_fixture", "seq": 1,
            "type": "turn_queued", "session_id": SESSION_ID, "ts": timestamp,
            "queued_id": QUEUED_ID, "prompt": "This test must remain queued.",
            "request_prompt": "This test must remain queued.", "file_ids": [], "backend": "codex"}) + "\n")
        due = time.time() - 30
        job = {"id": JOB_ID, "session_id": SESSION_ID, "title": "Owned due job",
               "prompt": "This test must remain scheduled.", "backend": "codex", "context_mode": "chat",
               "schedule_kind": "interval", "interval_seconds": 60, "timezone": "UTC",
               "schedule_start_at": due, "scheduled_run_at": due, "next_run_at": due,
               "loop": False, "max_runs": 1, "enabled": True, "run_count": 0,
               "manual_run_pending": False, "created_at": timestamp, "updated_at": timestamp,
               "last_run_at": None, "chat_references": [], "team_references": [],
               "_revision": "job_rev_startup_fixture"}
        (self.state / "jobs.json").write_text(json.dumps({JOB_ID: job}))

    def spawn(self, role):
        log = (self.base / f"{role}-{len(self.processes)}.log").open("wb")
        self.logs.append(log)
        process = subprocess.Popen([
            str(self.release / ".venv/bin/python"), "-B", str(self.release / "execution_service.py"),
            role, "--runtime-dir", str(self.layout.runtime_dir), "--bind", "127.0.0.1",
            "--port", str(self.port), "--callback-port", "0"],
            cwd=self.release, env=self.environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    async def eventually(self, predicate, timeout=25):
        async def wait():
            while True:
                for process in self.processes:
                    if process.poll() is not None:
                        raise AssertionError(f"Owned worker/gateway exited: {process.returncode}")
                value = predicate()
                if asyncio.iscoroutine(value):
                    value = await value
                if value:
                    return value
                await asyncio.sleep(0.05)
        try:
            return await asyncio.wait_for(wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError, AssertionError) as error:
            logs = "\n".join(path.read_text(errors="replace")[-10000:] for path in self.base.glob("*.log"))
            self.fail(f"{error}\n{logs}")

    async def start(self):
        self.worker = self.spawn("worker")
        receipt_path = self.layout.runtime_dir / "worker.json"
        await self.eventually(receipt_path.is_file)
        self.receipt = json.loads(receipt_path.read_text())
        self.control = httpx.AsyncClient(base_url=self.receipt["callback_origin"],
            headers={"Authorization": "Bearer " + (self.layout.runtime_dir / "control.token").read_text()},
            timeout=10, trust_env=False)
        self.clients.append(self.control)
        self.gateway = self.spawn("gateway")
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}",
            headers={"X-AgentsDock-Token": self.token}, timeout=10, trust_env=False)
        self.clients.append(self.client)
        async def ready():
            try:
                response = await self.client.get("/api/health")
                return response.json() if response.status_code == 200 else None
            except httpx.TransportError:
                return None
        health = await self.eventually(ready)
        self.assertEqual(health["execution_service"]["pid"], self.worker.pid)
        self.assertEqual(health["gateway"]["pid"], self.gateway.pid)
        response = await self.control.get("/api/admin/execution/status")
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertEqual(status["worker_instance_id"], self.receipt["instance_id"])
        self.assertEqual(status["lease"]["operation_id"], self.operation)
        self.assertTrue(status["lease"]["sealed"])
        self.assertIsNone(status["lease"]["expires_at"])
        return status

    def session_body(self):
        return {"backend": "codex", "cwd": str(self.workspace), "title": "Owned zero-turn fixture",
                "auto_title_enabled": False, "import_history": False, "provider_jobs_access": "blocked"}

    async def test_pending_journal_closes_http_until_exact_hold_is_released(self):
        status = await self.start()
        response = await self.client.post("/api/sessions", json=self.session_body())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], MAINTENANCE_DETAIL)
        body = {"action": "release", "expected_worker_instance_id": self.receipt["instance_id"],
                "operation_id": str(uuid.uuid4()), "lease_id": status["lease"]["lease_id"]}
        denied = await self.control.post("/api/admin/execution/maintenance", json=body)
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual((await self.control.get("/api/admin/execution/status")).json()["lease"], status["lease"])
        released = await self.control.post("/api/admin/execution/maintenance",
            json={**body, "operation_id": self.operation})
        self.assertEqual(released.status_code, 200, released.text)
        self.assertIsNone(released.json()["lease"])
        created = await self.client.post("/api/sessions", json=self.session_body())
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["session"]["title"], "Owned zero-turn fixture")
        # Only zero-turn mutation was submitted. Never release seeded work to a provider.
        self.assertIsNone(self.worker.poll())
        self.assertFalse((self.state / "admin/provider-children.json").exists())

    async def test_restored_queue_and_due_job_reach_admission_but_do_not_start(self):
        self.seed_waiting_work()
        await self.start()
        events_path = self.state / "sessions" / SESSION_ID / "events.jsonl"
        def deferred():
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            jobs = json.loads((self.state / "jobs.json").read_text())
            queue_wait = any(event.get("type") == "turn_deferred"
                and event.get("queued_id") == QUEUED_ID
                and "managed update" in event.get("message", "") for event in events)
            job_wait = "managed update" in jobs[JOB_ID].get("last_defer_reason", "")
            return (events, jobs) if queue_wait and job_wait else None
        events, jobs = await self.eventually(deferred)
        self.assertEqual(jobs[JOB_ID]["run_count"], 0)
        self.assertIsNone(jobs[JOB_ID]["last_run_at"])
        self.assertFalse(any(event.get("type") in {"turn_started", "turn_unqueued"} for event in events))
        response = await self.client.get(f"/api/sessions/{SESSION_ID}")
        self.assertEqual(response.status_code, 200, response.text)
        queued = response.json()["queued_turns"]
        self.assertEqual([item["queued_id"] for item in queued], [QUEUED_ID])
        self.assertFalse((self.state / "admin/provider-children.json").exists())
        status = (await self.control.get("/api/admin/execution/status")).json()
        self.assertTrue(status["lease"]["sealed"])
        self.assertEqual(status["lease"]["operation_id"], self.operation)
        # This case deliberately stays sealed through owned-process shutdown.
        # Release/reopening is tested separately with an empty queue/job registry.

    async def shutdown(self):
        for client in self.clients:
            await client.aclose()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 25)
                except (TimeoutError, asyncio.TimeoutError):
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for log in self.logs:
            log.close()
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
