import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import cursor_process_guard as guard_module


class CursorProcessGuardTests(unittest.TestCase):
    def test_spawn_failure_is_verified_without_changing_legacy_cursor_exit(self):
        with patch.object(guard_module.signal, "signal"), \
             patch.object(guard_module.subprocess, "Popen", side_effect=FileNotFoundError):
            args = ["--parent-pid", str(os.getppid()), "--", "missing-fixture"]
            self.assertEqual(guard_module.main(args), 127)
            self.assertEqual(guard_module.main(["--verify-cleanup", *args]), 71)

    def test_required_process_inspection_never_treats_failure_as_empty(self):
        with patch.object(guard_module.os.path, "isfile", return_value=False):
            self.assertEqual(guard_module._process_snapshot(), {})
            with self.assertRaises(RuntimeError):
                guard_module._process_snapshot(required=True)
        with patch.object(guard_module.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", .2)):
            with self.assertRaises(RuntimeError):
                guard_module._process_snapshot(required=True)

    def test_verified_exit_contract_distinguishes_native_failure_and_inspection_failure(self):
        for native_exit, expected in ((0, 0), (1, 71), (70, 71)):
            child = Mock(pid=999_999, returncode=native_exit)
            child.poll.return_value = native_exit
            with self.subTest(native_exit=native_exit), patch.object(guard_module.signal, "signal"), \
                 patch.object(guard_module.subprocess, "Popen", return_value=child), \
                 patch.object(guard_module, "_process_snapshot", return_value={}), \
                 patch.object(guard_module, "_terminate_child_group"):
                self.assertEqual(guard_module.main(["--verify-cleanup", "--parent-pid", str(os.getppid()), "--", "fixture"]), expected)
        with patch.object(guard_module.signal, "signal"), \
             patch.object(guard_module.subprocess, "Popen", return_value=child), \
             patch.object(guard_module, "_process_snapshot", side_effect=RuntimeError("no inspection")), \
             patch.object(guard_module, "_terminate_child_group"):
            self.assertEqual(guard_module.main(["--verify-cleanup", "--parent-pid", str(os.getppid()), "--", "fixture"]), 70)

    def test_guard_terminates_cursor_group_when_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            child = root / "child.py"
            child.write_text(
                "import os, pathlib, time\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            guard = subprocess.Popen([
                sys.executable,
                str(guard_script),
                "--parent-pid",
                str(os.getpid()),
                "--",
                sys.executable,
                str(child),
            ])
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text())
                guard.terminate()
                self.assertEqual(guard.wait(timeout=3), 143)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=3)

    def test_guard_force_kills_a_child_that_ignores_termination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "stubborn-child.pid"
            child = root / "stubborn-child.py"
            child.write_text(
                "import os, pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            guard = subprocess.Popen([
                sys.executable,
                str(guard_script),
                "--parent-pid",
                str(os.getpid()),
                "--",
                sys.executable,
                str(child),
            ])
            child_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                child_pid = int(pid_file.read_text())
                guard.terminate()
                self.assertEqual(guard.wait(timeout=3), 143)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("stubborn Cursor child survived guard teardown")
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=3)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "Cursor process guard is POSIX-only")
    def test_guard_kills_a_detached_descendant_that_ignores_termination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "detached-descendant.pid"
            descendant = root / "detached-descendant.py"
            descendant.write_text(
                "import os, pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            child = root / "child-with-detached-descendant.py"
            child.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(descendant)!r}], "
                "start_new_session=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            guard = subprocess.Popen([
                sys.executable,
                str(guard_script),
                "--parent-pid",
                str(os.getpid()),
                "--",
                sys.executable,
                str(child),
            ])
            descendant_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                descendant_pid = int(pid_file.read_text())

                guard.terminate()
                self.assertEqual(guard.wait(timeout=3), 143)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        os.kill(descendant_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("detached Cursor descendant survived guard teardown")
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=3)
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "Cursor process guard is POSIX-only")
    def test_guard_cleans_same_group_child_after_leader_exits_normally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "same-group-child.pid"
            descendant = root / "same-group-child.py"
            descendant.write_text(
                "import os, pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            leader = root / "normal-leader.py"
            leader.write_text(
                "import pathlib, subprocess, sys, time\n"
                f"pid_file = pathlib.Path({str(pid_file)!r})\n"
                f"subprocess.Popen([sys.executable, {str(descendant)!r}])\n"
                "deadline = time.monotonic() + 3\n"
                "while not pid_file.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n",
                encoding="utf-8",
            )
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            guard = subprocess.Popen([
                sys.executable,
                str(guard_script),
                "--parent-pid",
                str(os.getpid()),
                "--",
                sys.executable,
                str(leader),
            ])
            descendant_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                descendant_pid = int(pid_file.read_text())
                self.assertEqual(guard.wait(timeout=3), 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant_pid, 0)
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=3)
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "Cursor process guard is POSIX-only")
    def test_guard_cleans_detached_child_after_leader_exits_normally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "normal-detached-child.pid"
            descendant = root / "normal-detached-child.py"
            descendant.write_text(
                "import os, pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            leader = root / "normal-detached-leader.py"
            leader.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(descendant)!r}], "
                "start_new_session=True)\n"
                "time.sleep(0.3)\n",
                encoding="utf-8",
            )
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            guard = subprocess.Popen([
                sys.executable,
                str(guard_script),
                "--parent-pid",
                str(os.getpid()),
                "--",
                sys.executable,
                str(leader),
            ])
            descendant_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                descendant_pid = int(pid_file.read_text())
                self.assertEqual(guard.wait(timeout=3), 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(descendant_pid, 0)
            finally:
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=3)
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, 9)
                    except ProcessLookupError:
                        pass

    def test_guard_rejects_invalid_invocation(self) -> None:
        guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
        result = subprocess.run(
            [sys.executable, str(guard_script), "--bad"],
            check=False,
        )
        self.assertEqual(result.returncode, 64)

    def test_guard_does_not_spawn_after_parent_is_already_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "spawned"
            guard_script = (Path(__file__).resolve().parents[1] / "cursor_process_guard.py")
            result = subprocess.run(
                [
                    sys.executable,
                    str(guard_script),
                    "--parent-pid",
                    "2147483647",
                    "--",
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ],
                check=False,
                timeout=3,
            )
            self.assertEqual(result.returncode, 143)
            self.assertFalse(marker.exists())

    def test_standalone_deploy_includes_the_guard(self) -> None:
        deploy = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(deploy.count("cursor_process_guard.py"), 2)
        self.assertIn("cursor_process_guard, secure_peer_delivery", deploy)


if __name__ == "__main__":
    unittest.main()
