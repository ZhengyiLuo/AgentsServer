"""State ownership across maintained entry points, without model requests."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from execution_ownership import acquire_state_ownership, _release_state_ownership_for_tests


ROOT = Path(__file__).resolve().parent


class StateOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="state-owner-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.owners = []
        self.addCleanup(lambda: [_release_state_ownership_for_tests(owner) for owner in reversed(self.owners)])

    def acquire(self):
        owner = acquire_state_ownership(self.root)
        self.owners.append(owner)
        return owner

    def test_same_process_reuses_one_noninheritable_descriptor(self):
        owner = self.acquire()
        with ThreadPoolExecutor(max_workers=8) as executor:
            owners = list(executor.map(acquire_state_ownership, [self.root] * 24))
        self.assertTrue(all(candidate is owner for candidate in owners))
        self.assertFalse(os.get_inheritable(owner.descriptor))
        self.assertEqual(owner.path.stat().st_mode & 0o777, 0o600)

    def test_another_process_is_refused_until_owner_exits(self):
        owner = self.acquire()
        command = [sys.executable, "-B", "-c", "from pathlib import Path; from execution_ownership import acquire_state_ownership; import sys; acquire_state_ownership(Path(sys.argv[1]))", str(self.root)]
        denied = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("already owns this state directory", denied.stderr)
        inode = owner.path.stat().st_ino
        _release_state_ownership_for_tests(self.owners.pop())
        accepted = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(owner.path.stat().st_ino, inode)

    def test_unsafe_lock_and_parent_are_not_repaired_or_replaced(self):
        admin = self.root / "admin"
        admin.mkdir()
        lock = admin / "state-owner.lock"
        lock.write_text("preserve")
        lock.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "0600 regular file"):
            acquire_state_ownership(self.root)
        self.assertEqual(lock.read_text(), "preserve")
        lock.unlink()
        target = self.root / "target"
        target.write_text("preserve")
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            acquire_state_ownership(self.root)
        lock.unlink()
        admin.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, "not writable"):
            acquire_state_ownership(self.root)
        self.assertEqual(target.read_text(), "preserve")

    def test_held_path_replacement_does_not_create_a_second_owner(self):
        owner = self.acquire()
        owner.path.rename(owner.path.with_suffix(".retained"))
        owner.path.touch(mode=0o600)
        with self.assertRaisesRegex(RuntimeError, "changed while held"):
            acquire_state_ownership(self.root)


PRODUCTION_BOOTSTRAP = r'''
import json, os, pathlib, subprocess, sys
import agent_server as server

fixture = pathlib.Path(os.environ["OWNERSHIP_FIXTURE"])
role = os.environ["OWNERSHIP_ROLE"]
child = None
original_load = server.STORE.load
original_sweep = server.sweep_orphaned_provider_children

async def load():
    global child
    (fixture / (role + ".load")).write_text("entered")
    await original_load()
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(300)",
                              "codex-owned-ownership-fixture-" + fixture.name], start_new_session=True)
    server.register_provider_child(child.pid, child.pid)
    (fixture / (role + ".child.json")).write_text(json.dumps({"pid": child.pid}))

def sweep():
    (fixture / (role + ".sweep")).write_text("entered")
    return original_sweep()

server.STORE.load = load
server.sweep_orphaned_provider_children = sweep
try:
    if role == "legacy":
        sys.argv = ["agent_server.py", "serve", "--bind", "127.0.0.1", "--port", os.environ["OWNERSHIP_PORT"]]
        raise SystemExit(server.main())
    import execution_service
    sys.argv = ["execution_service.py", "worker", "--runtime-dir", str(server.STATE_DIR / "execution"),
                "--bind", "127.0.0.1", "--port", os.environ["OWNERSHIP_PORT"]]
    raise SystemExit(execution_service.main())
finally:
    if child is not None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        (fixture / (role + ".child.json")).unlink(missing_ok=True)
'''


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"),
                     "production server dependencies required")
class ProductionStateOwnershipTests(unittest.TestCase):
    def run_order(self, first_role, second_role):
        with tempfile.TemporaryDirectory(prefix="ad-own-", dir="/tmp") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            for name in ("home", "state", "config", "workspace", "codex", "claude", "tmp"):
                (root / name).mkdir(mode=0o700)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHDOCK_", "ZENITHBOT_"))}
            token = "ownership-fixture-" + "x" * 40
            env.update({"HOME": str(root / "home"), "AGENTSDOCK_STATE_DIR": str(root / "state"),
                        "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                        "AGENTSDOCK_AGENT_CWD": str(root / "workspace"),
                        "AGENTSDOCK_AGENT_TOKEN": token, "AGENTSDOCK_AUTO_TITLES": "0",
                        "AGENTSDOCK_TEAM_HUB_MODE": "disabled", "AGENTSDOCK_TEAM_HUB_TRANSPORT": "loopback",
                        "CODEX_HOME": str(root / "codex"), "CODEX_SESSIONS_ROOT": str(root / "codex"),
                        "CLAUDE_CONFIG_DIR": str(root / "claude"), "CLAUDE_PROJECTS_ROOT": str(root / "claude"),
                        "TMPDIR": str(root / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
                        "OWNERSHIP_FIXTURE": str(root)})
            processes, logs = [], []

            def launch(role):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                log = (root / f"{role}.log").open("wb")
                logs.append(log)
                process = subprocess.Popen([sys.executable, "-B", "-c", PRODUCTION_BOOTSTRAP], cwd=ROOT,
                    env={**env, "OWNERSHIP_ROLE": role, "OWNERSHIP_PORT": str(port)},
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                processes.append(process)
                return process, port

            def health(port):
                origin = f"http://127.0.0.1:{port}"
                if first_role == "worker":
                    origin = json.loads((root / "state/execution/worker.json").read_text())["callback_origin"]
                request = urllib.request.Request(origin + "/api/health", headers={"X-AgentsDock-Token": token})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
                    return json.load(response)

            try:
                incumbent, port = launch(first_role)
                deadline = time.monotonic() + 30
                while True:
                    self.assertIsNone(incumbent.poll(), (root / f"{first_role}.log").read_text()[-4000:])
                    try:
                        if health(port).get("ok"):
                            break
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
                    self.assertLess(time.monotonic(), deadline, "production incumbent did not become ready")
                    time.sleep(0.05)
                child_pid = json.loads((root / f"{first_role}.child.json").read_text())["pid"]
                registry = root / "state/admin/provider-children.json"
                before = registry.read_bytes()
                self.assertIn(child_pid, [item["pid"] for item in json.loads(before)["children"]])
                newcomer, _ = launch(second_role)
                newcomer.wait(timeout=30)
                self.assertIn("already owns this state directory", (root / f"{second_role}.log").read_text())
                self.assertFalse((root / f"{second_role}.load").exists())
                self.assertFalse((root / f"{second_role}.sweep").exists())
                self.assertFalse((root / f"{second_role}.child.json").exists())
                self.assertEqual(registry.read_bytes(), before)
                self.assertIsNone(incumbent.poll())
                os.kill(child_pid, 0)
                self.assertTrue(health(port)["ok"])
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                # If a forced fixture shutdown bypasses Python finally, kill
                # only its recorded controlled child after its parent exits.
                for child_file in root.glob("*.child.json"):
                    pid = json.loads(child_file.read_text())["pid"]
                    observed = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                        capture_output=True, text=True, timeout=5)
                    if "codex-owned-ownership-fixture-" + root.name not in observed.stdout:
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                for log in logs:
                    log.close()

    def test_legacy_owner_refuses_split_worker_before_state_load_or_orphan_sweep(self):
        self.run_order("legacy", "worker")

    def test_split_worker_owner_refuses_legacy_before_state_load_or_orphan_sweep(self):
        self.run_order("worker", "legacy")


if __name__ == "__main__":
    unittest.main()
