"""Actual gateway/worker/provider-fixture processes and network connections.

No live provider credentials, model calls, real user services, or provider homes
are used. Provider interoperability needs separate Codex and Claude acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


SERVICE = Path(__file__).resolve().parents[1] / "execution_service.py"
HEADERS = {"X-AgentsDock-Token": "owned-execution-fixture-token"}


class ExecutionGatewayProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep AF_UNIX paths below macOS's small path bound. No HOME overrides.
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-exec-", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.processes = []
        self.logs = []
        self.addAsyncCleanup(self.shutdown)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.worker = self.spawn("worker")
        await self.eventually(lambda: (self.root / "worker.json").is_file())
        self.client = httpx.AsyncClient(base_url=self.base, headers=HEADERS, timeout=3, trust_env=False)
        await self.start_gateway()
        self.initial = await self.state()

    def spawn(self, mode, *args):
        log = (self.root / f"{mode}-{len(self.processes)}.log").open("wb")
        self.logs.append(log)
        command = [sys.executable, "-B", str(SERVICE.resolve()), mode,
                   "--runtime-dir", str(self.root), "--bind", "127.0.0.1",
                   "--port", str(self.port), "--callback-port", "0"]
        if mode == "worker":
            command.extend(["--application", "test_support.execution_fixture:app"])
        command.extend(args)
        env = dict(os.environ, AGENTSDOCK_EXECUTION_FIXTURE_ROOT=str(self.root))
        process = subprocess.Popen(command, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    async def eventually(self, predicate, timeout=8):
        async def wait():
            while True:
                value = predicate()
                if asyncio.iscoroutine(value):
                    value = await value
                if value:
                    return value
                await asyncio.sleep(0.02)
        try:
            return await asyncio.wait_for(wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            logs = "\n".join(path.read_text(errors="replace")[-6000:] for path in self.root.glob("*.log"))
            self.fail(f"Fixture deadline exceeded.\n{logs}")

    async def start_gateway(self):
        self.gateway = self.spawn("gateway")
        async def ready():
            try:
                response = await self.client.get("/api/health")
                return response.status_code == 200 and response.json().get("gateway", {}).get("pid") == self.gateway.pid
            except httpx.TransportError:
                return False
        await self.eventually(ready)

    async def stop_gateway(self, number):
        self.gateway.send_signal(number)
        await asyncio.wait_for(asyncio.to_thread(self.gateway.wait), 5)
        self.assertIsNone(self.worker.poll(), "gateway termination killed worker")

    async def state(self):
        try:
            response = await self.client.get("/state")
        except httpx.TransportError as error:
            logs = "\n".join(path.read_text(errors="replace")[-6000:] for path in self.root.glob("*.log"))
            self.fail(f"State request failed ({type(error).__name__}).\n{logs}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def wait_state(self, predicate):
        async def check():
            value = await self.state()
            return value if predicate(value) else None
        return await self.eventually(check)

    def websocket(self, after=0, **kwargs):
        return connect(f"ws://127.0.0.1:{self.port}/events?after={after}",
                       additional_headers=kwargs.pop("additional_headers", HEADERS),
                       subprotocols=["agentsdock-test"], proxy=None, **kwargs)

    async def next_event(self, ws):
        event = json.loads(await asyncio.wait_for(ws.recv(), 3))
        self.assertIn("seq", event)
        return event

    async def shutdown(self):
        if hasattr(self, "client"):
            await self.client.aclose()
        # Only Popen-owned fixtures are signaled. Provider exits if its owner dies.
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 5)
                except (TimeoutError, asyncio.TimeoutError):
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for log in self.logs:
            log.close()
        self.temporary.cleanup()

    async def test_stream_and_approval_survive_graceful_and_abrupt_gateway_replacement(self):
        after = 0
        health = (await self.client.get("/api/health")).json()
        worker_identity = health["execution_service"]["instance_id"]
        gateway_identity = health["gateway"]["instance_id"]
        for number in (signal.SIGTERM, signal.SIGKILL):
            ws = await self.websocket(after)
            self.assertEqual(json.loads(await ws.recv())["type"], "connection")
            self.assertEqual(ws.subprotocol, "agentsdock-test")
            for _ in range(3):
                event = await self.next_event(ws)
                self.assertEqual(event["seq"], after + 1)
                after = event["seq"]
            before = await self.state()
            self.assertEqual(before["pending_approval"], "approval-1")
            journal = self.root / "provider-events.jsonl"
            lines_before = len(journal.read_text().splitlines())
            await self.stop_gateway(number)
            await self.eventually(lambda: len(journal.read_text().splitlines()) >= lines_before + 3)
            self.assertIsNone(self.worker.poll())
            callback = json.loads((self.root / "worker.json").read_text())["callback_origin"]
            self.assertTrue(callback.startswith("http://127.0.0.1:"))
            async with httpx.AsyncClient(base_url=callback, headers=HEADERS, trust_env=False) as direct:
                live = (await direct.get("/api/health")).json()
                self.assertEqual(live["execution_service"]["instance_id"], worker_identity)
                self.assertGreater(live["seq"], before["seq"])
                self.assertNotIn("gateway", live)
            await ws.close()
            await self.start_gateway()
            state = await self.state()
            self.assertEqual(state["worker_pid"], self.initial["worker_pid"])
            self.assertEqual(state["provider_pid"], self.initial["provider_pid"])
            self.assertIsNone(state["provider_returncode"])
            self.assertEqual(state["run_id"], before["run_id"])
            self.assertEqual(state["pending_approval"], "approval-1")
            health = (await self.client.get("/api/health")).json()
            self.assertEqual(health["execution_service"]["instance_id"], worker_identity)
            self.assertNotEqual(health["gateway"]["instance_id"], gateway_identity)
            self.assertEqual(health["server_version"], "fixture-engine-v1")
            self.assertEqual(health["server_instance_id"], "fixture-worker-1")
            gateway_identity = health["gateway"]["instance_id"]
        async with self.websocket(after) as ws:
            await ws.recv()
            live_boundary = (await self.state())["seq"] + 2
            while after < live_boundary:
                event = await self.next_event(ws)
                self.assertEqual(event["seq"], after + 1)
                after = event["seq"]
            approval = {"approval_id": "approval-1", "choice": "allow"}
            for _ in range(2):
                response = await self.client.post("/approval", json=approval)
                self.assertEqual(response.status_code, 200)
            await self.eventually(lambda: (self.root / "tool-effects.jsonl").is_file())
            self.assertEqual(len((self.root / "tool-effects.jsonl").read_text().splitlines()), 1)
            state = await self.state()
            self.assertEqual(state["approval_requests"], 2)
            self.assertEqual(state["approval_effects"], 1)
            self.assertIsNone(state["pending_approval"])
            async def approval_result():
                nonlocal after
                while True:
                    event = await self.next_event(ws)
                    self.assertEqual(event["seq"], after + 1)
                    after = event["seq"]
                    if event["type"] == "approval_result":
                        self.assertEqual(event["approval_id"], "approval-1")
                        return
            await asyncio.wait_for(approval_result(), 5)
        await self.wait_state(lambda state: state["active_streams"] == 0)

    async def test_accepted_mutation_is_not_replayed_after_gateway_loses_response(self):
        body = {"request_id": "mutation-1", "value": "one side effect", "withhold_response": True}
        pending = asyncio.create_task(self.client.post("/mutation", json=body))
        try:
            await self.wait_state(lambda state: state["mutation_effects"] == 1)
            await self.stop_gateway(signal.SIGKILL)
            with self.assertRaises(httpx.TransportError):
                await pending
            await self.start_gateway()
            state = await self.state()
            self.assertEqual(state["mutation_requests"], 1)
            self.assertEqual(state["mutation_effects"], 1)
            self.assertEqual(state["completed_mutations"], 0)
            self.assertEqual((await self.client.post("/mutation/release", json={})).status_code, 200)
            await self.wait_state(lambda state: state["completed_mutations"] == 1)
            retry = await self.client.post("/mutation", json=body)
            self.assertEqual(retry.status_code, 200)
            changed = await self.client.post("/mutation", json={**body, "value": "different"})
            self.assertEqual(changed.status_code, 409)
            state = await self.state()
            self.assertEqual(state["mutation_requests"], 3)
            self.assertEqual(state["mutation_effects"], 1)
            self.assertEqual(len((self.root / "mutation-effects.jsonl").read_text().splitlines()), 1)
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError, httpx.TransportError):
                await pending

    async def test_http_and_websocket_preserve_guard_headers_and_scope(self):
        duplicate = [("X-Fixture-Order", "first"), ("X-Fixture-Order", "second")]
        forwarded = [("Forwarded", "for=192.0.2.12;proto=https"),
                     ("X-Forwarded-For", "192.0.2.12"), ("X-Forwarded-Proto", "https")]
        response = await self.client.get("/headers?original=%2Fkeep%3F", headers=[*duplicate, *forwarded])
        self.assertEqual(response.status_code, 200)
        headers = response.json()["headers"]
        self.assertEqual([v for n, v in headers if n == "x-fixture-order"], ["first", "second"])
        self.assertEqual(response.headers.get_list("x-worker-duplicate"), ["one", "two"])
        self.assertEqual(response.json()["query_string"], "original=%2Fkeep%3F")
        self.assertEqual(response.json()["scheme"], "http")
        self.assertEqual(response.json()["client"][0], "127.0.0.1")
        for name, value in forwarded:
            self.assertIn([name.lower(), value], headers)
        cases = [([], 401), ([("Authorization", "Bearer owned-execution-fixture-token")], 401),
                 ([*HEADERS.items(), *HEADERS.items()], 401),
                 ([*HEADERS.items(), ("Origin", "https://untrusted.invalid")], 403),
                 ([*HEADERS.items(), ("Sec-Fetch-Site", "cross-site")], 403)]
        async with httpx.AsyncClient(base_url=self.base, trust_env=False) as untrusted:
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    rejected = await untrusted.get("/state?token=owned-execution-fixture-token", headers=headers)
                    self.assertEqual(rejected.status_code, expected)
        stale = await self.client.post("/approval", json={"expected_server_instance_id": "stale", "approval_id": "approval-1", "choice": "allow"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((await self.state())["approval_effects"], 0)
        async with self.websocket(additional_headers=[*HEADERS.items(), *duplicate]) as ws:
            hello = json.loads(await ws.recv())
            self.assertEqual([v for n, v in hello["headers"] if n == "x-fixture-order"], ["first", "second"])
            self.assertEqual(hello["scheme"], "ws")
            self.assertEqual(hello["client"][1], ws.local_address[1])
        for headers in ([*HEADERS.items(), *HEADERS.items()], [*HEADERS.items(), ("Origin", "https://untrusted.invalid")]):
            with self.assertRaises(InvalidStatus):
                async with self.websocket(additional_headers=headers):
                    self.fail("guard headers were lost")

    async def test_fast_responses_finish_when_worker_does_not_read_request(self):
        # The first real-process run exposed truncated replies here: the worker
        # closed its Unix socket with unread request bytes, or the gateway's
        # request pump closed a response reader on a concurrent write failure.
        responses = await asyncio.gather(*(self.client.get("/state") for _ in range(24)))
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["worker_pid"], self.initial["worker_pid"])
        # Early policy rejection must complete even with an unread upload.
        rejected = await self.client.post("/mutation", content=b"x" * (512 * 1024),
                                          headers={"Origin": "https://untrusted.invalid"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json(), {"guard": 403})
        self.assertEqual((await self.state())["mutation_effects"], 0)

    async def test_gateway_configuration_rollback_keeps_worker_and_prior_commands(self):
        # This exercises actual process/config replacement, not an old binary's compatibility.
        body = {"request_id": "before-rollback", "value": "preserved"}
        self.assertEqual((await self.client.post("/mutation", json=body)).status_code, 200)
        previous_seq = (await self.state())["seq"]
        original_port = self.port
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            candidate_port = probe.getsockname()[1]
        for port in (candidate_port, original_port):
            await self.stop_gateway(signal.SIGTERM)
            self.port = port
            self.base = f"http://127.0.0.1:{self.port}"
            await self.client.aclose()
            self.client = httpx.AsyncClient(base_url=self.base, headers=HEADERS, timeout=3, trust_env=False)
            await self.start_gateway()
            state = await self.wait_state(lambda value: value["seq"] > previous_seq)
            self.assertEqual(state["worker_pid"], self.initial["worker_pid"])
            self.assertEqual(state["provider_pid"], self.initial["provider_pid"])
            self.assertEqual(state["mutation_effects"], 1)
            self.assertEqual(state["pending_approval"], "approval-1")
            self.assertEqual((await self.client.post("/mutation", json=body)).status_code, 200)
            previous_seq = state["seq"]
        self.assertEqual(len((self.root / "mutation-effects.jsonl").read_text().splitlines()), 1)


class ExecutionProductionControlProcessTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real application admission without starting a provider turn."""

    async def test_private_maintenance_fences_public_mutations_until_exact_release(self):
        temporary = tempfile.TemporaryDirectory(prefix="ad-ctrl-", dir="/tmp")
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        processes, logs = [], []
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHDOCK_", "ZENITHBOT_"))}
        for name in ("state", "config", "codex-history", "claude-history", "workspace", "tmp"):
            (root / name).mkdir(mode=0o700)
        runtime = root / "state" / "execution"
        runtime.mkdir(mode=0o700)
        env.update({
            "AGENTSDOCK_STATE_DIR": str(root / "state"),
            "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
            "CODEX_SESSIONS_ROOT": str(root / "codex-history"),
            "CLAUDE_PROJECTS_ROOT": str(root / "claude-history"),
            "AGENTSDOCK_AGENT_CWD": str(root / "workspace"),
            "AGENTSDOCK_AGENT_TOKEN": HEADERS["X-AgentsDock-Token"],
            "AGENTSDOCK_AUTO_TITLES": "0",
            "AGENTSDOCK_TEAM_HUB_MODE": "disabled",
            "AGENTSDOCK_TEAM_HUB_TRANSPORT": "loopback",
            "TMPDIR": str(root / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        async def wait_until(predicate):
            async def wait():
                while True:
                    self.assertTrue(all(process.poll() is None for process in processes),
                                    "Owned execution process exited before readiness")
                    value = predicate()
                    if asyncio.iscoroutine(value):
                        value = await value
                    if value:
                        return value
                    await asyncio.sleep(0.05)
            return await asyncio.wait_for(wait(), 30)

        def launch(role):
            log = (root / f"{role}.log").open("wb")
            logs.append(log)
            processes.append(subprocess.Popen([
                sys.executable, "-B", str(SERVICE.resolve()), role,
                "--runtime-dir", str(runtime), "--bind", "127.0.0.1",
                "--port", str(port), "--callback-port", "0",
            ], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT))

        try:
            launch("worker")
            worker_path = runtime / "worker.json"
            await wait_until(worker_path.is_file)
            worker = json.loads(worker_path.read_text())
            # This is the disposable worker's own capability, never a provider credential.
            control_token = (runtime / "control.token").read_text().strip()
            launch("gateway")
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS,
                                         timeout=10, trust_env=False) as public:
                async def ready():
                    try:
                        response = await public.get("/api/health")
                        return response.status_code == 200
                    except httpx.TransportError:
                        return False
                await wait_until(ready)
                status_path = "/api/admin/execution/status"
                maintenance_path = "/api/admin/execution/maintenance"
                async with httpx.AsyncClient(base_url=worker["callback_origin"],
                        headers={"Authorization": "Bearer " + control_token},
                        timeout=10, trust_env=False) as private:
                    status = await private.get(status_path)
                    self.assertEqual(status.status_code, 200)
                    initial = status.json()
                    self.assertTrue(initial["idle"])
                    self.assertIsNone(initial["lease"])
                    operation = {"expected_worker_instance_id": initial["worker_instance_id"],
                                 "operation_id": str(uuid.uuid4())}
                    acquired = await private.post(maintenance_path, json={"action": "acquire", **operation})
                    self.assertEqual(acquired.status_code, 200)
                    lease = acquired.json()["lease"]
                    sealed = await private.post(maintenance_path, json={
                        "action": "seal", "lease_id": lease["lease_id"], **operation})
                    self.assertEqual(sealed.status_code, 200)
                    self.assertTrue(sealed.json()["lease"]["sealed"])
                    body = {"backend": "claude", "cwd": str(root / "workspace"),
                            "title": "Zero-turn admission test", "auto_title_enabled": False,
                            "import_history": False, "provider_jobs_access": "blocked"}
                    blocked = await public.post("/api/sessions", json=body)
                    self.assertEqual(blocked.status_code, 409)
                    self.assertIn("managed update", blocked.json()["detail"])
                    self.assertEqual((await public.get("/api/sessions")).json()["sessions"], [])
                    # Knowing the local control capability does not expose private routes publicly.
                    hidden = await public.get(status_path, headers={"Authorization": "Bearer " + control_token})
                    self.assertEqual(hidden.status_code, 404)
                    unauthenticated = await public.get("/api/sessions", headers={"X-AgentsDock-Token": "wrong"})
                    self.assertIn(unauthenticated.status_code, (401, 403))
                    released = await private.post(maintenance_path, json={
                        "action": "release", "lease_id": lease["lease_id"], **operation})
                    self.assertEqual(released.status_code, 200)
                    self.assertIsNone(released.json()["lease"])
                    created = await public.post("/api/sessions", json=body)
                    self.assertEqual(created.status_code, 200)
                    session_id = created.json()["session"]["id"]
                    events = (root / "state" / "sessions" / session_id / "events.jsonl").read_text().splitlines()
                    self.assertEqual([json.loads(event)["type"] for event in events], ["session_created"])
                    self.assertFalse((root / "state" / "admin" / "provider-children.json").exists())
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(asyncio.to_thread(process.wait), 25)
                    except (TimeoutError, asyncio.TimeoutError):
                        process.kill()
                        await asyncio.to_thread(process.wait)
            for log in logs:
                log.close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
