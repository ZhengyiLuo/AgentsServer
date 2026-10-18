"""Read-only logs: real temporary files, mocked journal; no installed services."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import execution_install
import server_instances as instances


class InstanceLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agentsdock-logs-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.registry = instances.Registry(self.home)
        self.default = instances.Instance("default", self.home)
        self.work = instances.Instance("work", self.home)

    def configured(self, instance, platform="darwin"):
        for path in (instance.runtime, instance.config, instance.state, instance.logs):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {**instance.environment(), "AGENTSDOCK_AGENT_PORT": "7851",
               "AGENTSDOCK_AGENT_TOKEN": "fixture-private-token"}
        (instance.config / "env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        service = instance.service_file(platform)
        service.parent.mkdir(parents=True, exist_ok=True)
        if platform == "darwin":
            service.write_bytes(plistlib.dumps({"Label": instances.launchd_label(instance.name),
                "ProgramArguments": [str(instance.runtime / "current/.venv/bin/python"),
                                     str(instance.runtime / "current/agent_server.py"), "serve"],
                "EnvironmentVariables": env}))
        else:
            service.write_text(f"[Service]\nEnvironmentFile={instance.config / 'env'}\n"
                f"ExecStart={instance.runtime / 'current/.venv/bin/python'} "
                f"{instance.runtime / 'current/agent_server.py'} serve --port 7851\n")

    def split(self, platform="Darwin"):
        self.configured(self.default, "darwin" if platform == "Darwin" else "linux")
        release = self.default.runtime / "releases/test"
        release.mkdir(parents=True, exist_ok=True)
        (release / "VERSION").write_text("1.0.10\n")
        (release / "execution_service.py").write_text("# fixture runtime\n")
        (release / ".venv/bin").mkdir(parents=True, exist_ok=True, mode=0o700)
        python = release / ".venv/bin/python"
        if not python.exists():
            python.symlink_to(Path(sys.executable).resolve())
        layout = execution_install.ExecutionLayout(self.default.runtime, self.default.config,
            self.default.state, self.home, platform, release, release, "127.0.0.1", 7850, {})
        manifest = self.default.runtime / "execution-layout.json"
        manifest.write_text(json.dumps(execution_install.layout_manifest(layout)))
        manifest.chmod(0o600)
        return layout

    def test_cli_defaults_and_flags(self):
        args = instances.parser().parse_args(["logs"])
        self.assertEqual(args.lines, 100)
        self.assertFalse(args.follow)
        args = instances.parser().parse_args(["logs", "work", "-n", "0", "-f"])
        self.assertEqual((args.name, args.lines, args.follow), ("work", 0, True))
        with patch.object(instances, "Registry", return_value=self.registry), \
                patch.object(self.registry, "instances", return_value=[self.default, self.work]), \
                patch.object(instances, "show_logs", return_value=0) as show:
            self.assertEqual(instances.main(["logs"]), 0)
            show.assert_called_once_with(self.default, lines=100, follow=False)
            show.reset_mock()
            self.assertEqual(instances.main(["logs", "--instance", "work", "-n", "8"]), 0)
            show.assert_called_once_with(self.work, lines=8, follow=False)
        self.assertFalse(self.registry.root.exists())

    def test_invalid_flags_and_unknown_instances_cannot_spawn_or_write(self):
        for args in [["--all"], ["../other"], ["-n", "-1"], ["-n", "10001"],
                     ["-n", "all"], ["--port", "7851"]]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                instances.parser().parse_args(["logs", *args])
        with patch.object(instances, "Registry", return_value=self.registry), \
                patch.object(instances.subprocess, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(instances.main(["logs", "missing"]), 1)
            self.assertIn("agentsdock list", errors.getvalue())
            self.assertEqual(instances.main(["logs", "work", "--instance", "default"]), 1)
            run.assert_not_called()
        self.assertFalse(self.registry.root.exists())

    def test_snapshot_reads_only_selected_instance_and_never_configuration(self):
        self.configured(self.work)
        self.configured(self.default)
        (self.work.logs / "server.log").write_text("old\nmiddle\nrecent 中文\n")
        (self.work.logs / "server-error.log").write_text("last error\n")
        (self.default.logs / "server.log").write_text("other-instance-private\n")
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(instances.show_logs(self.work, lines=2, platform="darwin"), 0)
        self.assertIn("Logs (work):", output.getvalue())
        self.assertIn("Port: 7851", output.getvalue())
        self.assertIn("middle\nrecent 中文", output.getvalue())
        self.assertNotIn("old\n", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("other-instance", output.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})

    def test_tail_handles_missing_newline_and_zero_lines(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        for raw in (b"one\ntwo\nthree", b"one\ntwo\nthree\n"):
            log.write_bytes(raw)
            data, cursor, truncated = instances.read_log_slice(self.work, log, 2)
            self.assertEqual(data, raw[4:])
            self.assertEqual(cursor[2], len(raw))
            self.assertFalse(truncated)
            self.assertEqual(instances.read_log_slice(self.work, log, 0)[0], b"")

    def test_initial_byte_limit_is_explicit_and_does_not_read_giant_lines(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        log.write_bytes(b"x" * (instances.LOG_READ_BYTES + 1) + b"\nlast\n")
        data, cursor, truncated = instances.read_log_slice(self.work, log, 10000)
        self.assertEqual(data, b"last\n")
        self.assertTrue(truncated)
        self.assertEqual(cursor[2], log.stat().st_size)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            instances.show_logs(self.work, platform="darwin")
        self.assertIn("Older bytes omitted", output.getvalue())

    def test_append_rotation_and_truncation(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        log.write_bytes(b"before\n")
        _, cursor, _ = instances.read_log_slice(self.work, log, 100)
        with log.open("ab") as handle:
            handle.write(b"after\n")
        data, cursor, _ = instances.read_log_slice(self.work, log, 100, cursor)
        self.assertEqual(data, b"after\n")
        log.rename(log.with_suffix(".old"))
        log.write_bytes(b"rotated\n")
        data, cursor, _ = instances.read_log_slice(self.work, log, 100, cursor)
        self.assertEqual(data, b"rotated\n")
        log.write_bytes(b"new\n")
        self.assertEqual(instances.read_log_slice(self.work, log, 100, cursor)[0], b"new\n")

    def test_missing_log_created_during_follow_starts_at_beginning(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        data, cursor, _ = instances.read_log_slice(self.work, log, 0)
        self.assertEqual(data, b"")
        log.write_bytes(b"created\n")
        self.assertEqual(instances.read_log_slice(self.work, log, 0, cursor)[0], b"created\n")

    def test_follow_reads_large_append_in_bounded_chunks_without_skipping(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        log.write_bytes(b"old\n")
        _, cursor, _ = instances.read_log_slice(self.work, log, 0)
        appended = b"new\n" * (instances.LOG_READ_BYTES // 4 + 3)
        with log.open("ab") as handle:
            handle.write(appended)
        first, cursor, _ = instances.read_log_slice(self.work, log, 0, cursor)
        second, cursor, _ = instances.read_log_slice(self.work, log, 0, cursor)
        self.assertEqual(len(first), instances.LOG_READ_BYTES)
        self.assertEqual(first + second, appended)
        self.assertEqual(cursor[2], log.stat().st_size)

    def test_linked_log_directory_is_rejected(self):
        self.configured(self.work)
        self.work.logs.rmdir()
        self.work.logs.symlink_to(self.work.config, target_is_directory=True)
        with self.assertRaises(ValueError):
            instances.read_log_slice(self.work, self.work.logs / "env", 100)

    def test_unsafe_log_types_and_permissions_are_rejected(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        other = self.home / "not-a-log"
        other.write_text("private")
        for kind in ("symlink", "hardlink", "fifo", "writable"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    log.symlink_to(other)
                elif kind == "hardlink":
                    os.link(other, log)
                elif kind == "fifo":
                    os.mkfifo(log)
                else:
                    log.write_text("unsafe")
                    log.chmod(0o666)
                try:
                    with self.assertRaises((ValueError, OSError)):
                        instances.read_log_slice(self.work, log, 100)
                finally:
                    log.unlink()

    def test_follow_rejects_linked_replacement(self):
        self.configured(self.work)
        log = self.work.logs / "server.log"
        log.write_text("first\n")
        _, cursor, _ = instances.read_log_slice(self.work, log, 100)
        log.unlink()
        log.symlink_to(self.work.config / "env")
        with self.assertRaises(ValueError):
            instances.read_log_slice(self.work, log, 100, cursor)

    def test_terminal_controls_are_escaped(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            instances.print_log_data(b"\x1b]52;c;not-clipboard\x07\rhello\tworld\n")
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("\\x1b]52", output.getvalue())
        self.assertIn("\\x07\\x0dhello\tworld", output.getvalue())

    def test_follow_ctrl_c_does_not_stop_a_service(self):
        self.configured(self.work)
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.object(instances.time, "sleep", side_effect=KeyboardInterrupt), \
                patch.object(instances.subprocess, "run") as run:
            self.assertEqual(instances.show_logs(self.work, follow=True, platform="darwin"), 0)
            run.assert_not_called()
        self.assertIn("Ctrl+C stops viewing, not the server", output.getvalue())
        self.assertIn("Stopped viewing logs.", output.getvalue())

    def test_split_default_includes_worker_and_gateway(self):
        layout = self.split()
        paths, units = instances.log_sources(self.default, "darwin")
        self.assertEqual(paths, [layout.runtime_dir / "logs" / name for name in
            ("gateway.stdout.log", "gateway.stderr.log", "worker.stdout.log", "worker.stderr.log")])
        self.assertEqual(units, ["agents-server-gateway.service", "agents-server.service"])
        paths[0].parent.mkdir(parents=True)
        for path in paths:
            path.write_text(path.name + " entry\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            instances.show_logs(self.default, platform="darwin")
        for path in paths:
            self.assertIn(path.name + " entry", output.getvalue())

    def test_corrupt_or_foreign_split_layout_fails_without_falling_back(self):
        self.split()
        manifest = self.default.runtime / "execution-layout.json"
        manifest.write_text("{}")
        with self.assertRaises(ValueError):
            instances.log_sources(self.default, "darwin")
        self.split()
        with self.assertRaises(ValueError):
            instances.log_sources(self.default, "linux")

    def test_linux_uses_exact_user_units_and_no_pager(self):
        self.configured(self.work, "linux")
        with patch.object(instances.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(instances.show_logs(self.work, lines=12, platform="linux"), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["journalctl", "--user", "--no-pager", "--output=short-iso",
                                  "--lines", "12", "--unit", "agents-server-work.service"])
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(kwargs["env"]["SYSTEMD_COLORS"], "0")
        self.assertNotIn("shell", kwargs)

    def test_linux_split_follows_both_units_and_preserves_errors(self):
        self.split("Linux")
        with patch.object(instances.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(instances.show_logs(self.default, lines=0, follow=True, platform="linux"), 7)
        self.assertEqual(run.call_args.args[0][-5:],
                         ["--unit", "agents-server-gateway.service", "--unit", "agents-server.service", "--follow"])
        self.assertIsNone(run.call_args.kwargs["timeout"])

    def test_linux_snapshot_timeout_is_bounded(self):
        self.configured(self.work, "linux")
        with patch.object(instances.subprocess, "run", side_effect=subprocess.TimeoutExpired("journalctl", 15)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "timed out"):
            instances.show_logs(self.work, platform="linux")


if __name__ == "__main__":
    unittest.main()
