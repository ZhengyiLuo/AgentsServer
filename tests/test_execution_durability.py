import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import execution_durability as durability


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(mode=0o700)
        (self.runtime / "VERSION").write_text("1.0.4-beta.12\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_complete_tree_includes_hardlinks_and_legacy_private_descendants(self):
        dependency = self.runtime / ".venv/lib"
        dependency.mkdir(parents=True)
        dependency.chmod(0o775)
        payload = dependency / "code.py"
        payload.write_text("preserved dependency\n")
        os.link(payload, dependency / "hardlink.py")
        (self.runtime / ".venv/lib64").symlink_to("lib", target_is_directory=True)
        seen = []
        actual = durability._file_barrier
        def record(fd):
            seen.append(os.fstat(fd).st_ino)
            actual(fd)
        with patch.object(durability, "_file_barrier", side_effect=record):
            result = durability.flush_tree(self.runtime)
        self.assertEqual(seen.count(payload.stat().st_ino), 2)
        self.assertEqual(result["entries"], 6)
        self.assertEqual(dependency.stat().st_mode & 0o777, 0o775)

    def test_external_link_and_special_file_are_rejected(self):
        outside = self.base / "private-data"
        outside.write_text("must not be traversed")
        link = self.runtime / "outside"
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "external durability link"):
            durability.flush_tree(self.runtime)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaisesRegex(ValueError, "special file"):
            durability.flush_tree(self.runtime)
        self.assertEqual(outside.read_text(), "must not be traversed")

    def test_late_tree_change_is_rejected(self):
        a, b = self.runtime / "a", self.runtime / "b"
        a.write_text("original")
        b.write_text("later")
        actual = durability._file_barrier
        def mutate(fd):
            actual(fd)
            if os.fstat(fd).st_ino == b.stat().st_ino:
                a.write_text("changed after earlier flush")
        with patch.object(durability, "_file_barrier", side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, "changed after flush"):
                durability.flush_tree(self.runtime)

    def test_file_flush_failure_does_not_change_runtime(self):
        before = (self.runtime / "VERSION").read_bytes()
        with patch.object(durability, "_file_barrier", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                durability.flush_tree(self.runtime)
        self.assertEqual((self.runtime / "VERSION").read_bytes(), before)

    def test_owned_uv_interpreter_prefix_is_flushed_but_unknown_prefix_is_not(self):
        prefix = self.base / "cpython-3.13.12-macos-aarch64-none"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib/python3.13").mkdir(parents=True)
        (prefix / "BUILD").write_text("20260921")
        stdlib = prefix / "lib/python3.13/stdlib.py"
        stdlib.write_text("stdlib bytes")
        executable = prefix / "bin/python3.13"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o775)
        (self.runtime / ".venv/bin").mkdir(parents=True)
        (self.runtime / ".venv/bin/python").symlink_to(executable)
        seen = []
        with patch.object(durability, "_file_barrier", side_effect=lambda fd: seen.append(os.fstat(fd).st_ino)), patch.object(durability, "_full_barrier"):
            durability.flush_tree(self.runtime)
        self.assertIn(stdlib.stat().st_ino, seen)
        other = self.base / "unrelated-prefix"
        prefix.rename(other)
        (self.runtime / ".venv/bin/python").unlink()
        (self.runtime / ".venv/bin/python").symlink_to(other / "bin/python3.13")
        seen.clear()
        with patch.object(durability, "_file_barrier", side_effect=lambda fd: seen.append(os.fstat(fd).st_ino)), patch.object(durability, "_full_barrier"):
            durability.flush_tree(self.runtime)
        self.assertNotIn((other / "lib/python3.13/stdlib.py").stat().st_ino, seen)

    def test_activation_flushes_candidate_and_distinct_retained_generations(self):
        old = self.base / "old"
        old.mkdir(mode=0o700)
        (old / "VERSION").write_text("1.0.4-beta.9")
        def identity(path):
            return durability.activation._release_identity(path, path)
        candidate, retained = identity(self.runtime), identity(old)
        value = {"phase": "prepared", "candidate_release": candidate, "old_release": retained,
                 "execution": {"old_worker_release": copy.deepcopy(retained)}}
        with patch.object(durability, "flush_tree") as flush, patch.object(durability, "_full_barrier"):
            durability.flush_activation(self.base, value)
        self.assertEqual([call.args[0] for call in flush.call_args_list], [self.runtime, old])
        value["phase"] = "quiescing"
        with self.assertRaisesRegex(RuntimeError, "precede native quiescing"):
            durability.flush_activation(self.base, value)

    def test_activation_flushes_group_writable_uv_used_by_candidate_and_old_release(self):
        prefix = self.base / "cpython-3.13.12-linux-x86_64-gnu"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib/python3.13").mkdir(parents=True)
        (prefix / "BUILD").write_text("20260921")
        stdlib = prefix / "lib/python3.13/stdlib.py"
        stdlib.write_text("retained standard library\n")
        executable = prefix / "bin/python3.13"
        executable.write_text("#!/bin/sh\nexit 0\n")
        (prefix / "bin/python3").symlink_to("python3.13")
        (prefix / "lib/python3.13/stdlib-link.py").symlink_to("stdlib.py")
        for path in [prefix, *prefix.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o775 if path.is_dir() else 0o664)
        old = self.base / "old"
        old.mkdir(mode=0o700)
        (old / "VERSION").write_text("1.0.7-beta.2\n")
        for release in (self.runtime, old):
            (release / ".venv/bin").mkdir(parents=True)
            (release / ".venv/bin/python").symlink_to(executable)

        for executable_mode in (0o755, 0o775):
            with self.subTest(executable_mode=oct(executable_mode)):
                executable.chmod(executable_mode)
                identities = {path: durability._identity(path.lstat())
                              for path in [prefix, *prefix.rglob("*")]}
                candidate = durability.activation._release_identity(self.runtime, self.runtime)
                retained = durability.activation._release_identity(old, old)
                value = {"phase": "prepared", "candidate_release": candidate,
                         "old_release": retained,
                         "execution": {"old_worker_release": copy.deepcopy(retained)}}
                seen = []
                actual = durability._file_barrier
                def record(fd):
                    seen.append(os.fstat(fd).st_ino)
                    actual(fd)
                with patch.object(durability, "_file_barrier", side_effect=record):
                    durability.flush_activation(self.base, value)
                self.assertEqual(seen.count(stdlib.stat().st_ino), 2)
                self.assertEqual(identities, {path: durability._identity(path.lstat())
                                              for path in identities})

    def test_installer_flush_failure_precedes_native_mutation(self):
        source = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        begin = source.index('if [[ "${EXECUTION_MODE:-legacy}" == "split" ]]; then\n  if ! execution_activation_command durability')
        end = source.index('\nfi\n', begin) + len('\nfi\n')
        events = self.base / "events"
        original = (self.runtime / "VERSION").read_bytes()
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            script = '''set -eu
EXECUTION_MODE=split
STAGE_DIR=owned-fixture
COLD_TEAM_HUB_HANDOFF=false
execution_activation_command() { echo "$1" >> "$EVENTS"; [[ "$1" != durability ]]; }
execution_recovery_arm() { echo arm >> "$EVENTS"; }
record_activation_phase() { echo "$1" >> "$EVENTS"; }
execution_stop_services() { kill "$PROVIDER_PID"; }
''' + source[begin:end]
            result = subprocess.run(["/bin/bash", "-c", script], env={**os.environ, "EVENTS": str(events), "PROVIDER_PID": str(child.pid)}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(events.read_text().splitlines(), ["durability"])
            self.assertIsNone(child.poll())
            self.assertEqual((self.runtime / "VERSION").read_bytes(), original)
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
