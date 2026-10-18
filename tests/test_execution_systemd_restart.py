"""Opt-in real systemd restart/installer stop-start with one owned detached job.

AGENTSDOCK_SYSTEMD_RESTART_PROOF=1 python -m unittest tests.test_execution_systemd_restart -v

Uses a UUID-named user unit, the production unit renderer and NativeServices
stop/start. The worker is a fixture, not the server: it runs the production
cgroup inspection functions extracted without importing user state. No network,
provider, installed service, environment dump or existing job is used. Cleanup
only stops the owned unit and its exact PID/start-time-identified fixture child.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import execution_install as install
from execution_manage import NativeServices


SOURCE = Path(__file__).resolve().parents[1]


def process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "start_time": fields[19], "state": fields[0]}


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("AGENTSDOCK_SYSTEMD_RESTART_PROOF") == "1",
                     "opt-in isolated real systemd user-service proof")
class SystemdRestartProof(unittest.TestCase):
    def command(self, *args):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode:
            self.fail(f"owned systemd fixture command failed: {args[0]}")
        return result.stdout

    def wait_for(self, predicate, description):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail(f"timed out waiting for {description}")

    def child_alive(self, expected):
        current = process_identity(expected["pid"])
        self.assertEqual(current["start_time"], expected["start_time"])
        self.assertNotEqual(current["state"], "Z")
        return current

    def boots(self):
        path = self.state / "boots.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cleanup_owned(self):
        subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True, timeout=20)
        subprocess.run(["systemctl", "--user", "disable", self.unit], capture_output=True, timeout=15)
        child_file = self.state / "child.json"
        if child_file.exists():
            child = json.loads(child_file.read_text())
            try:
                current = process_identity(child["pid"])
                if current["start_time"] == child["start_time"]:
                    os.kill(child["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        if self.unit_path.exists() and self.unit_path.stat().st_ino == self.unit_inode:
            self.unit_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15)
        subprocess.run(["systemctl", "--user", "reset-failed", self.unit], capture_output=True, timeout=15)

    def fixture_source(self):
        names = {"process_cgroup_paths", "agents_server_systemd_cgroup_from_paths",
                 "agents_server_systemd_cgroup", "cgroup_is_within", "linux_process_ids",
                 "linux_process_still_exists", "systemd_worker_preserves_descendants",
                 "managed_update_service_cgroup_state", "public_managed_update_service_cgroup_state"}
        source = (SOURCE / "agent_server.py").read_text()
        tree = ast.parse(source)
        definitions = [ast.get_source_segment(source, node) for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(definitions), len(names))
        prefix = (
            "from __future__ import annotations\n"
            "import json, os, signal, subprocess, sys, time\n"
            "from pathlib import Path\nfrom typing import Any\n"
            f"AGENTS_SERVER_SYSTEMD_UNIT = {self.unit!r}\nSERVER_RESTART_COUNT_LIMIT = 1000000\n"
        )
        body = '''
state = Path(os.environ['AGENTSDOCK_STATE_DIR'])
def identity(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    return {'pid': pid, 'start_time': fields[19]}
child_file = state / 'child.json'
if not child_file.exists():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_file.write_text(json.dumps(identity(child.pid)))
record = {**identity(os.getpid()), 'child': json.loads(child_file.read_text()),
          'proof': public_managed_update_service_cgroup_state()}
with (state / 'boots.jsonl').open('a') as output:
    output.write(json.dumps(record) + '\\n')
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
while True:
    time.sleep(1)
'''
        return prefix + "\n\n".join(definitions) + "\n" + body

    def test_restart_and_installer_stop_start_preserve_exact_child(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory(prefix="agentsdock-restart-proof-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        root, config, self.state = (base / name for name in ("install", "config", "state"))
        for directory in (root, config, self.state):
            directory.mkdir(mode=0o700)
        self.unit = f"agentsdock-restart-proof-{uuid.uuid4().hex}.service"
        unit_patch = patch.object(install, "WORKER_UNIT", self.unit)
        unit_patch.start()
        self.addCleanup(unit_patch.stop)
        release = root / "releases" / "1.0.0"
        for directory in (root / "releases", release, release / ".venv", release / ".venv/bin"):
            directory.mkdir(mode=0o700)
        (release / "VERSION").write_text("1.0.0\n")
        (release / ".venv/bin/python").symlink_to(Path(sys.executable).resolve())
        (release / "execution_service.py").write_text(self.fixture_source())
        (config / "env").write_text("")
        layout = install.ExecutionLayout(root, config, self.state, Path.home().resolve(),
                                         "Linux", release, release, "127.0.0.1", 17850, {})
        self.unit_path = layout.service_path("worker")
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_body = install.render_service(layout, "worker")
        with self.unit_path.open("xb") as output:
            output.write(unit_body)
        self.unit_inode = self.unit_path.stat().st_ino
        self.addCleanup(self.cleanup_owned)
        services = NativeServices(layout, timeout=15)
        services.reload()
        services.start("worker")
        before = self.wait_for(lambda: self.boots(), "first worker")[0]
        self.child_alive(before["child"])
        self.assertEqual(before["proof"], {
            "safe": True, "unknown_descendant_count": 1, "inspection": "verified-preserved"})
        policy = self.command("show", self.unit, "--no-pager",
                              "--property=LoadState,ActiveState,MainPID,KillMode,SendSIGKILL")
        self.assertIn("KillMode=process\n", policy)
        self.assertIn("SendSIGKILL=no\n", policy)
        self.assertIn(f"MainPID={before['pid']}\n", policy)
        # The restart API terminates its own main process. Restart=always must
        # launch the replacement even though its detached job is still alive.
        self.assertEqual(process_identity(before["pid"])["start_time"], before["start_time"])
        os.kill(before["pid"], signal.SIGTERM)
        after_restart = self.wait_for(lambda: len(self.boots()) >= 2 and self.boots()[1], "automatic restart")
        self.assertNotEqual(after_restart["pid"], before["pid"])
        self.assertEqual(after_restart["child"], before["child"])
        self.child_alive(before["child"])
        # The managed installer uses this exact native adapter. Its stop must
        # complete while the child remains, then start a fresh main process.
        services.stop("worker")
        self.assertEqual(services._observe("worker")["state"], "stopped")
        self.child_alive(before["child"])
        services.start("worker")
        after_start = self.wait_for(lambda: len(self.boots()) >= 3 and self.boots()[2], "explicit stop/start")
        self.assertNotEqual(after_start["pid"], after_restart["pid"])
        self.assertEqual(after_start["child"], before["child"])
        self.child_alive(before["child"])
        for record in (after_restart, after_start):
            self.assertEqual(record["proof"], before["proof"])
        print(json.dumps({"boundary": "real systemd, production unit/native adapter/cgroup proof, fixture worker",
                          "unit": self.unit, "before": before, "after_restart": after_restart,
                          "after_stop_start": after_start}, sort_keys=True))
