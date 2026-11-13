import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_server


class TerminalWorkingDirectoryTests(unittest.TestCase):
    def test_tmux_client_never_inherits_a_removable_checkout(self) -> None:
        with patch.object(agent_server, "tmux_bin", return_value="tmux"), patch.object(
            agent_server, "tmux_bootstrap_environment", return_value={"PATH": "/usr/bin"}
        ), patch.object(agent_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            agent_server.run_tmux(["has-session", "-t", "owned"])
        self.assertEqual(run.call_args.kwargs["cwd"], "/")
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin"})

    def test_new_windows_and_both_splits_enter_the_selected_directory(self) -> None:
        target = "/workspace/a folder's $(not-a-command)"
        for action, prefix in (
            ("new-window", ["new-window"]),
            ("split-right", ["split-window", "-h"]),
            ("split-down", ["split-window", "-v"]),
        ):
            with self.subTest(action=action), patch.dict(
                agent_server.STORE.sessions, {"chat": {"id": "chat", "cwd": target}}
            ), patch.object(agent_server, "require_existing_terminal_session", return_value="owned"), patch.object(
                agent_server, "existing_cwd", return_value=target
            ), patch.object(agent_server, "resolve_terminal_login_shell", return_value="/bin/sh"), patch.object(
                agent_server, "terminal_session_path", return_value="/usr/bin:/bin"
            ), patch.object(agent_server, "terminal_windows_snapshot", return_value={}), patch.object(
                agent_server, "run_tmux", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
            ) as run:
                agent_server.terminal_action("chat", action)
                self.assertEqual(run.call_args.args[0], [
                    *prefix, "-t", "owned", "-c", target,
                    f"cd -- {shlex.quote(target)} && SHELL=/bin/sh PATH=/usr/bin:/bin exec /bin/sh -l",
                ])

    @unittest.skipUnless(os.name == "posix", "Terminal PTYs require POSIX")
    def test_deleted_inherited_cwd_is_repaired_before_login_startup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            removed = base / "removed"
            removed.mkdir()
            # Test quoting and non-ASCII paths as well as the actual process cwd.
            target = base / "工作 dir's $(touch SHOULD_NOT_EXIST)"
            target.mkdir()
            shell = base / "login-probe"
            shell.write_text("#!/bin/sh\nprintf 'LOGIN_ARG=%s\\n' \"$1\"\n/bin/pwd\nprintf 'STDOUT_OK\\n'\nprintf 'STDERR_OK\\n' >&2\n")
            shell.chmod(0o700)
            command = agent_server.terminal_login_command(str(shell), "/usr/bin:/bin", cwd=str(target))
            # Reproduce a pre-existing daemon's dead cwd without touching any
            # installed daemon, user's shell startup files, or real workspace.
            result = subprocess.run([
                sys.executable, "-c",
                "import os,sys; os.chdir(sys.argv[1]); os.rmdir(sys.argv[1]); "
                "os.execl('/bin/sh', 'sh', '-c', sys.argv[2])",
                str(removed), command,
            ], cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LOGIN_ARG=-l", result.stdout)
            self.assertIn(str(target.resolve()), result.stdout)
            self.assertIn("STDOUT_OK", result.stdout)
            self.assertIn("STDERR_OK", result.stderr)
            self.assertFalse((target / "SHOULD_NOT_EXIST").exists())

    @unittest.skipUnless(os.name == "posix", "Terminal PTYs require POSIX")
    def test_directory_removed_between_validation_and_spawn_does_not_start_shell_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "started"
            shell = Path(root) / "login-probe"
            shell.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
            shell.chmod(0o700)
            command = agent_server.terminal_login_command(str(shell), cwd=str(Path(root) / "missing"))
            result = subprocess.run(["/bin/sh", "-c", command], cwd=root, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn("missing", result.stderr)
