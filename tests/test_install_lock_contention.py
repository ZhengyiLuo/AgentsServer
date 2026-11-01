"""Real installer lock functions with disposable files/processes, never services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from execution_manage import InstallationLock
from tests import test_installer_activation_recovery as recovery_tests


class PreparedInstallerLockContentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the exact production shell extraction; this never sources the
        # installer's top-level prerequisites, services or dependency stages.
        recovery_tests.InstallerActivationRecoveryTests.setUpClass()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="installer-lock-contention-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "install"
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.hooks = self.home / "test-hooks"
        self.hooks.mkdir(mode=0o700)
        self.trace = self.home / "trace.jsonl"
        self.observed = self.home / "observed"
        self.resume = self.home / "resume"
        self.hook_configuration = self.home / "hook.json"
        self.hook_configuration.write_text(json.dumps({"mode": "observe", "root": str(self.root),
            "trace": str(self.trace), "observed": str(self.observed), "resume": str(self.resume)}))
        # Clock/process instrumentation exists only in the extracted unit-test
        # subprocess. There is no new production env override or shorter bound.
        (self.hooks / "sitecustomize.py").write_text('''
import json, os, stat, time
from pathlib import Path
c = json.loads(Path(os.environ["INSTALLER_UNIT_HOOK"]).read_text())
real_sleep, real_monotonic, real_kill, real_fstat, real_open = time.sleep, time.monotonic, os.kill, os.fstat, os.open
clock = [0.0]
def record(kind, **value):
    with open(c["trace"], "a") as stream:
        stream.write(json.dumps({"kind": kind, **value}) + "\\n")
def wait_for_resume():
    Path(c["observed"]).touch()
    deadline = real_monotonic() + 5
    while not Path(c["resume"]).exists():
        if real_monotonic() >= deadline:
            raise RuntimeError("unit-test interleaving exceeded its bound")
        real_sleep(0.005)
def sleep(delay):
    record("sleep", delay=delay, clock=clock[0])
    Path(c["observed"]).touch()
    if c["mode"] in {"fast-clock", "changing-owner"}:
        clock[0] += 10.0
        if c["mode"] == "changing-owner":
            owner = Path(c["root"]) / ".install-lock/pid"
            owner.write_text(str(c["pids"][int(clock[0] / 10) % 2]) + "\\n")
            owner.chmod(0o600)
        return
    real_sleep(delay)
def kill(pid, number):
    if number != 0:
        raise AssertionError("installer lock must never signal an incumbent")
    if c["mode"] == "swap-at-liveness":
        wait_for_resume()
    return real_kill(pid, number)
def fstat(fd):
    value = real_fstat(fd)
    match = (c["mode"] == "foreign-pid-owner" and stat.S_ISREG(value.st_mode)
             or c["mode"] == "foreign-lock-owner" and stat.S_ISDIR(value.st_mode))
    if match and value.st_ino == c.get("foreign_inode"):
        values = list(value)
        values[4] += 1
        return os.stat_result(values)
    return value
def open_path(path, flags, *args, **kwargs):
    if c["mode"] == "retire-before-open" and str(path) == c["root"] + "/.install-lock":
        wait_for_resume()
    return real_open(path, flags, *args, **kwargs)
time.sleep = sleep
os.kill = kill
os.fstat = fstat
os.open = open_path
if c["mode"] in {"fast-clock", "changing-owner"}:
    time.monotonic = lambda: clock[0]
''')
        self.environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C",
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(self.hooks),
                            "INSTALLER_UNIT_HOOK": str(self.hook_configuration)}

    def configure_hook(self, **changes):
        value = json.loads(self.hook_configuration.read_text())
        self.hook_configuration.write_text(json.dumps({**value, **changes}))

    def prefix(self, mode="prepare", **overrides):
        extraction = recovery_tests.InstallerActivationRecoveryTests()
        flags = {"PREPARE_ONLY": "true" if mode == "prepare" else "false",
                 "ACTIVATE_PREPARED": str(self.root / ".prepared-receipts/fixture.json") if mode == "activate" else "",
                 "RECOVER_ONLY": "true" if mode == "recover" else "false", "RECOVER_UNARMED_ONLY": "false",
                 "RELEASE_VERSION": "1.0.8-beta.5", "EXPECTED_API_CONTRACT": "28",
                 "PREPARED_ARCHIVE_SHA256": "a" * 64, "PREPARED_RECEIPT": str(self.root / ".prepared-receipts/fixture.json")}
        flags.update(overrides)
        return extraction._lock_prefix(self.root) + "\n" + "\n".join(
            f"{name}={shlex.quote(value)}" for name, value in flags.items()) + "\n"

    def script(self, mode="prepare", **overrides):
        return self.prefix(mode, **overrides) + '''
set -e
acquire_install_lock
test "$(cat "$INSTALL_ROOT/.install-lock/pid")" = "$$"
echo ATOMIC_ACQUISITION_WON
release_install_lock
'''

    def start(self, script=None):
        process = subprocess.Popen(["/bin/bash", "-c", script or self.script()], env=self.environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        def clean():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        self.addCleanup(clean)
        return process

    def wait_observed(self, process):
        deadline = time.monotonic() + 5
        while not self.observed.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                self.fail(f"contender exited before observing contention: {process.returncode}; {stdout}; {stderr}")
            self.assertLess(time.monotonic(), deadline, "contender never observed the exact held lock")
            time.sleep(0.005)

    def trace_rows(self):
        return [json.loads(row) for row in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def make_live_lock(self):
        lock = self.root / ".install-lock"
        lock.mkdir(mode=0o700)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        (lock / "pid").chmod(0o600)
        return lock

    def test_prepare_and_activate_wait_for_real_recovery_lock_then_win_atomic_acquisition(self):
        for mode in ("prepare", "activate"):
            with self.subTest(mode=mode):
                self.observed.unlink(missing_ok=True)
                with InstallationLock(self.root):
                    lock = self.root / ".install-lock"
                    identity = (lock.stat().st_dev, lock.stat().st_ino)
                    owner = (lock / "pid").read_bytes()
                    process = self.start(self.script(mode))
                    self.wait_observed(process)
                    self.assertIsNone(process.poll())
                    self.assertEqual((lock.stat().st_dev, lock.stat().st_ino), identity)
                    self.assertEqual((lock / "pid").read_bytes(), owner)
                    self.assertEqual(self.root.stat().st_mode & 0o777, 0o755 if mode == "prepare" else 0o700)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("ATOMIC_ACQUISITION_WON", stdout)
                self.assertFalse(lock.exists())
                self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_ordinary_and_recovery_installers_still_refuse_live_owner_without_waiting(self):
        with InstallationLock(self.root):
            for mode in ("ordinary", "recover"):
                with self.subTest(mode=mode):
                    result = subprocess.run(["/bin/bash", "-c", self.script(mode)], env=self.environment,
                                            capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("ATOMIC_ACQUISITION_WON", result.stdout)
                    self.assertFalse(self.trace.exists(), "ordinary/recovery mode must not enter wait policy")
                    self.assertEqual((self.root / ".install-lock/pid").read_text().strip(), str(os.getpid()))

    def test_prepared_wait_requires_all_pins_and_never_applies_to_recovery(self):
        with InstallationLock(self.root):
            for overrides in ({"PREPARED_ARCHIVE_SHA256": ""}, {"PREPARED_ARCHIVE_SHA256": "X" * 64},
                              {"EXPECTED_API_CONTRACT": ""}, {"EXPECTED_API_CONTRACT": "0"},
                              {"RELEASE_VERSION": ""}, {"RECOVER_ONLY": "true"},
                              {"RECOVER_UNARMED_ONLY": "true"}):
                with self.subTest(overrides=overrides):
                    result = subprocess.run(["/bin/bash", "-c", self.script(**overrides)],
                                            env=self.environment, capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("ATOMIC_ACQUISITION_WON", result.stdout)
                    self.assertEqual(self.trace_rows(), [], "unscoped operation must stay fail-fast")
                    self.assertEqual((self.root / ".install-lock/pid").read_text().strip(), str(os.getpid()))

    def test_prepared_wait_has_one_fixed_monotonic_deadline_and_never_steals_live_owner(self):
        self.configure_hook(mode="fast-clock")
        with InstallationLock(self.root):
            lock = self.root / ".install-lock"
            inode, owner = lock.stat().st_ino, (lock / "pid").read_bytes()
            result = subprocess.run(["/bin/bash", "-c", self.script()], env=self.environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AgentsServer prepared update timed out waiting for the installation lock", result.stderr)
            self.assertNotIn("ATOMIC_ACQUISITION_WON", result.stdout)
            self.assertEqual(lock.stat().st_ino, inode)
            self.assertEqual((lock / "pid").read_bytes(), owner)
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        sleeps = [row for row in self.trace_rows() if row["kind"] == "sleep"]
        self.assertGreater(len(sleeps), 0)
        self.assertLessEqual(len(sleeps), 3, "30-second deadline must not reset between observations")

    def test_live_owner_changes_do_not_restart_the_fixed_deadline(self):
        other_owner = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(20)"],
                                       env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        def stop_owner():
            if other_owner.poll() is None:
                other_owner.terminate()
            other_owner.wait(timeout=5)
        self.addCleanup(stop_owner)
        lock = self.make_live_lock()
        inode = lock.stat().st_ino
        self.configure_hook(mode="changing-owner", pids=[os.getpid(), other_owner.pid])
        result = subprocess.run(["/bin/bash", "-c", self.script()], env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AgentsServer prepared update timed out waiting for the installation lock", result.stderr)
        self.assertNotIn("ATOMIC_ACQUISITION_WON", result.stdout)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertIn(int((lock / "pid").read_text()), (os.getpid(), other_owner.pid))
        self.assertIsNone(other_owner.poll())
        self.assertEqual(len([row for row in self.trace_rows() if row["kind"] == "sleep"]), 3)

    def test_retirement_between_failed_atomic_rename_and_open_retries_acquisition(self):
        self.configure_hook(mode="retire-before-open")
        with InstallationLock(self.root):
            process = self.start()
            self.wait_observed(process)
            self.assertIsNone(process.poll())
        self.resume.touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("ATOMIC_ACQUISITION_WON", stdout)
        self.assertFalse((self.root / ".install-lock").exists())

    def test_waiting_does_not_bypass_repeated_post_acquisition_admission_guards(self):
        source = recovery_tests.InstallerActivationRecoveryTests.installer_source
        between = recovery_tests._between
        guards = (between(source, "refuse_execution_layout() {", 'INSTALL_ROOT="$(normalize_managed_path')
                  + between(source, "fresh_install_scaffold_is_empty() {", "validate_fresh_install_state || exit 1")
                  + between(source, "validate_exclusive_install_state() {", "# Existing installs and ordinary supported hosts"))
        self.assertEqual(source.count("  acquire_install_lock || exit 1\n  validate_exclusive_install_state || exit 1"), 2)
        for variant in ("execution-transaction", "fresh-state"):
            with self.subTest(variant=variant):
                self.observed.unlink(missing_ok=True)
                config = self.home / "config"
                state = self.home / "state"
                config.mkdir(exist_ok=True)
                state.mkdir(exist_ok=True)
                flags = {"FRESH_INSTALL_ONLY": "true" if variant == "fresh-state" else "false",
                         "CONFIG_ROOT": str(config), "STATE_ROOT": str(state),
                         "SOURCE_DIR": str(self.root / "runtime"), "EXECUTION_MODE_EXPLICIT": "false",
                         "EXECUTION_MODE": "legacy"}
                script = self.prefix() + "\n".join(f"{name}={shlex.quote(value)}" for name, value in flags.items())
                # These real guards reject before any native service query. The
                # sentinels ensure the focused test can never touch host services.
                script += '''
launchctl() { echo NATIVE_SERVICE_QUERY_FORBIDDEN >&2; return 99; }
systemctl() { echo NATIVE_SERVICE_QUERY_FORBIDDEN >&2; return 99; }
'''
                script += guards + '''
set -e
trap release_install_lock EXIT
acquire_install_lock
echo ATOMIC_ACQUISITION_WON
validate_exclusive_install_state
echo ADMISSION_PASSED
'''
                with InstallationLock(self.root):
                    process = self.start(script)
                    self.wait_observed(process)
                    marker = self.root / ".execution-uninstall.json" if variant == "execution-transaction" else config / "env"
                    marker.write_text("owned fixture state created while contender waits")
                try:
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn("ATOMIC_ACQUISITION_WON", stdout)
                    self.assertNotIn("ADMISSION_PASSED", stdout)
                    self.assertNotIn("NATIVE_SERVICE_QUERY_FORBIDDEN", stderr)
                    self.assertIn("pending split-service uninstall" if variant == "execution-transaction"
                                  else "Fresh install refused", stderr)
                    self.assertEqual(marker.read_text(), "owned fixture state created while contender waits")
                    self.assertFalse((self.root / ".install-lock").exists())
                finally:
                    marker.unlink(missing_ok=True)

    def test_cancellation_during_wait_never_acquires_or_removes_incumbent_lock(self):
        with InstallationLock(self.root):
            lock = self.root / ".install-lock"
            before = (lock.stat().st_ino, (lock / "pid").read_bytes())
            process = self.start()
            self.wait_observed(process)
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn("ATOMIC_ACQUISITION_WON", stdout)
            self.assertEqual((lock.stat().st_ino, (lock / "pid").read_bytes()), before)
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(self.root.glob(".install-lock.*.tmp")), [])

    def test_cancellation_never_removes_a_substituted_private_draft(self):
        with InstallationLock(self.root):
            lock = self.root / ".install-lock"
            before = (lock.stat().st_ino, (lock / "pid").read_bytes())
            process = self.start()
            self.wait_observed(process)
            drafts = list(self.root.glob(".install-lock.*.tmp"))
            self.assertEqual(len(drafts), 1)
            draft = drafts[0]
            saved_draft = self.root / "saved-original-draft"
            draft.rename(saved_draft)
            draft.mkdir(mode=0o700)
            (draft / "pid").write_bytes((saved_draft / "pid").read_bytes())
            (draft / "pid").chmod(0o600)
            replacement_before = (draft.stat().st_ino, (draft / "pid").read_bytes())
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn("ATOMIC_ACQUISITION_WON", stdout)
            self.assertEqual((draft.stat().st_ino, (draft / "pid").read_bytes()), replacement_before)
            self.assertTrue((saved_draft / "pid").exists())
            self.assertEqual((lock.stat().st_ino, (lock / "pid").read_bytes()), before)

    def test_unsafe_lock_identity_is_not_reclassified_as_waitable_contention(self):
        for variant in ("pid-symlink", "lock-symlink", "pid-hardlink", "pid-mode", "lock-mode",
                        "foreign-pid-owner", "foreign-lock-owner", "pid-text", "pid-zero", "pid-long",
                        "unknown-entry"):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory(dir=self.home) as temporary:
                    previous = self.root
                    self.root = Path(temporary)
                    try:
                        self.root.chmod(0o755)
                        self.configure_hook(mode=variant, root=str(self.root))
                        lock = self.make_live_lock()
                        pid = lock / "pid"
                        if variant == "pid-symlink":
                            target = self.root / "saved-owner"
                            pid.rename(target)
                            pid.symlink_to(target)
                        elif variant == "lock-symlink":
                            target = self.root / "saved-lock"
                            lock.rename(target)
                            lock.symlink_to(target, target_is_directory=True)
                        elif variant == "pid-hardlink": os.link(pid, self.root / "owner-hardlink")
                        elif variant == "pid-mode": pid.chmod(0o666)
                        elif variant == "lock-mode": lock.chmod(0o755)
                        elif variant == "foreign-pid-owner": self.configure_hook(foreign_inode=pid.stat().st_ino)
                        elif variant == "foreign-lock-owner": self.configure_hook(foreign_inode=lock.stat().st_ino)
                        elif variant == "pid-text": pid.write_text("not-a-pid\n")
                        elif variant == "pid-zero": pid.write_text("0\n")
                        elif variant == "pid-long": pid.write_text("9" * 33)
                        elif variant == "unknown-entry": (lock / "private-canary-never-log").write_text("private-canary")
                        before = {str(path.relative_to(self.root)): (path.lstat().st_ino, path.read_bytes())
                                  for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()}
                        result = subprocess.run(["/bin/bash", "-c", self.script()], env=self.environment,
                                                capture_output=True, text=True, timeout=5)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("ATOMIC_ACQUISITION_WON", result.stdout)
                        self.assertNotIn("private-canary", result.stdout + result.stderr)
                        self.assertEqual(self.trace_rows(), [], "unsafe ownership must fail before sleeping")
                        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
                        for name, value in before.items():
                            path = self.root / name
                            self.assertEqual((path.lstat().st_ino, path.read_bytes()), value)
                    finally:
                        self.root = previous
                        self.configure_hook(mode="observe", root=str(previous))

    def test_root_replacement_during_wait_is_rejected_without_mutating_either_owner(self):
        lock = self.make_live_lock()
        owner_before = (lock.stat().st_ino, (lock / "pid").read_bytes())
        self.configure_hook(mode="swap-at-liveness")
        process = self.start()
        self.wait_observed(process)
        original_root = self.home / "saved-original-root"
        self.root.rename(original_root)
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        marker = self.root / "new-root-canary"
        marker.write_text("must remain unchanged")
        replacement_identity = self.root.stat().st_ino
        self.resume.touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("install root changed while waiting", stderr)
        self.assertNotIn("ATOMIC_ACQUISITION_WON", stdout)
        self.assertEqual(self.root.stat().st_ino, replacement_identity)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.root.iterdir()), [marker])
        saved_lock = original_root / ".install-lock"
        self.assertEqual((saved_lock.stat().st_ino, (saved_lock / "pid").read_bytes()), owner_before)

    def test_live_owner_path_swap_during_observation_never_deletes_replacement(self):
        lock = self.make_live_lock()
        saved = self.root / "saved-live-lock"
        victim = self.root / "replacement-victim"
        victim.mkdir(mode=0o700)
        (victim / "pid").write_text(f"{os.getpid()}\n")
        (victim / "pid").chmod(0o600)
        self.configure_hook(mode="swap-at-liveness")
        process = self.start()
        self.wait_observed(process)
        lock.rename(saved)
        lock.symlink_to(victim, target_is_directory=True)
        victim_before = ((victim / "pid").stat().st_ino, (victim / "pid").read_bytes())
        self.resume.touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertNotIn("ATOMIC_ACQUISITION_WON", stdout)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(((victim / "pid").stat().st_ino, (victim / "pid").read_bytes()), victim_before)
        self.assertEqual((saved / "pid").read_text().strip(), str(os.getpid()))


if __name__ == "__main__":
    unittest.main()
