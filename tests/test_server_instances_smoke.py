"""Opt-in real HTTP smoke; no installer/service manager or existing user state.

AGENTSDOCK_RUN_INSTANCE_SMOKE=1 python -m unittest test_server_instances_smoke
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from server_instances import Instance, Registry

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("AGENTSDOCK_RUN_INSTANCE_SMOKE") == "1", "opt-in isolated HTTP smoke")
class InstanceHTTPSmokeTests(unittest.TestCase):
    def test_two_real_servers_have_distinct_auth_identity_state_and_lifetimes(self):
        with tempfile.TemporaryDirectory(prefix="agents-instances-http-") as temporary:
            home = Path(temporary).resolve()
            processes = []
            files = []
            bindings = []
            registry = Registry(home)
            try:
                for name in ("smoke-one", "smoke-two"):
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    self.assertNotEqual(port, 7850)
                    instance = Instance(name, home)
                    with registry.locked():
                        registry.save(instance, "installed")
                    token = name.replace("-", "_") + "_" + "a" * 40
                    # Allowlist only: never inherit provider credentials, server
                    # selectors, real HOME/XDG config, agents or a tmux socket.
                    env = {
                        "HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
                        "XDG_CONFIG_HOME": str(home / ".config"),
                        "XDG_DATA_HOME": str(home / ".local/share"),
                        "XDG_CACHE_HOME": str(home / ".cache"),
                        "PYTHONDONTWRITEBYTECODE": "1", **instance.environment(),
                        "AGENTSDOCK_AGENT_CWD": str(home), "AGENTSDOCK_AGENT_TOKEN": token,
                        "AGENTSDOCK_AGENT_BIND": "127.0.0.1", "AGENTSDOCK_AGENT_PORT": str(port),
                    }
                    log = (home / f"{name}.log").open("w+")
                    files.append(log)
                    command = [sys.executable, str(ROOT / "agent_server.py"), "serve", "--bind", "127.0.0.1", "--port", str(port)]
                    process = subprocess.Popen(command, cwd=home, env=env, stdout=log, stderr=subprocess.STDOUT)
                    processes.append(process)
                    bindings.append((instance, port, token, env, command))

                def health(port, token):
                    request = Request(f"http://127.0.0.1:{port}/api/health", headers={"Authorization": f"Bearer {token}"})
                    with urlopen(request, timeout=3) as response:
                        return json.load(response)

                results = []
                for index, (_, port, token, _, _) in enumerate(bindings):
                    deadline = time.monotonic() + 120
                    while True:
                        if processes[index].poll() is not None:
                            files[index].seek(0)
                            self.fail("Isolated server exited: " + files[index].read()[-6000:])
                        try:
                            results.append(health(port, token))
                            break
                        except (URLError, TimeoutError):
                            if time.monotonic() >= deadline:
                                files[index].seek(0)
                                self.fail("Isolated startup timed out: " + files[index].read()[-6000:])
                            time.sleep(0.2)
                self.assertNotEqual(results[0]["server_identity"], results[1]["server_identity"])
                for index, (instance, port, _, _, _) in enumerate(bindings):
                    self.assertTrue((instance.state / "server-identity").exists())
                    with self.assertRaises(HTTPError) as wrong_token:
                        health(port, bindings[1 - index][2])
                    self.assertIn(wrong_token.exception.code, (401, 403))

                # Both registered servers see the same native history, but a
                # provider conversation may be imported into only one of them.
                projects = home / ".claude/projects/synthetic"
                projects.mkdir(parents=True)
                for provider_id in ("owned-source", "free-source"):
                    (projects / f"{provider_id}.jsonl").write_text(json.dumps({
                        "type": "user", "cwd": str(home),
                        "message": {"role": "user", "content": "Synthetic import fixture"},
                    }) + "\n")

                def api(index, path, body=None):
                    _, port, token, _, _ = bindings[index]
                    request = Request(f"http://127.0.0.1:{port}{path}",
                                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                      data=None if body is None else json.dumps(body).encode())
                    try:
                        with urlopen(request, timeout=10) as response:
                            return response.status, json.load(response)
                    except HTTPError as error:
                        return error.code, json.load(error)

                def available(index):
                    status, payload = api(index, "/api/local-sessions")
                    self.assertEqual(status, 200, payload)
                    return {row["provider_session_id"] for row in payload["sessions"]}

                self.assertEqual(available(0), {"owned-source", "free-source"})
                self.assertEqual(available(1), {"owned-source", "free-source"})
                resume = {"backend": "claude", "provider_session_id": "owned-source", "import_history": False, "cwd": str(home)}
                status, payload = api(0, "/api/sessions", resume)
                self.assertEqual(status, 200, payload)
                self.assertEqual(available(0), {"free-source"})
                self.assertEqual(available(1), {"free-source"})
                self.assertEqual(api(1, "/api/sessions", resume)[0], 409)
                status, payload = api(1, "/api/sessions/bulk-import", {"items": [{"backend": "claude", "provider_session_id": "owned-source"}]})
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["results"][0]["code"], "already_imported")

                # No provider CLI/model calls: import_history=False only binds
                # the native ID and persists a local session. Exactly one of
                # two concurrent imports of a free ID may commit.
                competing = {**resume, "provider_session_id": "free-source"}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(api, index, "/api/sessions", competing) for index in (0, 1)]
                    responses = [future.result() for future in futures]
                statuses = [response[0] for response in responses]
                self.assertEqual(statuses.count(200), 1, responses)
                loser = 1 if statuses[0] == 200 else 0
                self.assertIn(statuses[loser], (409, 503), responses)
                self.assertEqual(api(loser, "/api/sessions", competing)[0], 409)
                self.assertEqual(available(0), set())
                self.assertEqual(available(1), set())

                # A second process cannot open the first instance's state,
                # even when given a different (otherwise free) port.
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    duplicate_port = reservation.getsockname()[1]
                self.assertNotEqual(duplicate_port, 7850)
                duplicate_command = [*bindings[0][4][:-1], str(duplicate_port)]
                duplicate = subprocess.run(duplicate_command, cwd=home, env=bindings[0][3], capture_output=True, text=True, timeout=60)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("Another process owns", duplicate.stderr)
                processes[0].terminate()
                processes[0].wait(timeout=30)
                self.assertTrue(health(bindings[1][1], bindings[1][2])["ok"])
                self.assertEqual(available(1), set())  # Stopped owner still counts.
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()  # Exact child created above, never a service/listener lookup.
                        process.wait(timeout=10)
                for file in files:
                    file.close()


if __name__ == "__main__":
    unittest.main()
