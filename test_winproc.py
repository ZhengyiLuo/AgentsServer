"""Real-process tests for winproc (no mocks of the ownership mechanism)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

import winproc

SLEEP_600 = "import time; time.sleep(600)"


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


@unittest.skipUnless(winproc.SUPPORTED, "winproc job-object support unavailable")
class WinprocTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._cwd = tempfile.mkdtemp(prefix="winproc-test-")
        self._to_cleanup: list[object] = []

    def tearDown(self) -> None:
        for item in self._to_cleanup:
            if isinstance(item, winproc.OwnedProc):
                item.close()
            elif isinstance(item, subprocess.Popen):
                with contextlib.suppress(Exception):
                    item.kill()
                    item.wait(timeout=5)
        shutil.rmtree(self._cwd, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------
    def _spawn(
        self, argv: list[str], *, stdin_mode: str = "pipe", env: dict | None = None
    ) -> winproc.OwnedProc:
        proc = winproc.spawn_owned(argv, cwd=self._cwd, env=env, stdin_mode=stdin_mode)
        self._to_cleanup.append(proc)
        return proc

    def _popen(self, argv: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(argv)
        self._to_cleanup.append(proc)
        return proc

    @staticmethod
    async def _line(proc: winproc.OwnedProc, timeout: float = 20.0) -> bytes | None:
        return await asyncio.wait_for(proc.readline(), timeout=timeout)

    # -- 1: tree kill -----------------------------------------------------------
    async def test_terminate_tree_kills_descendants_and_spares_unrelated(self) -> None:
        child_script = _write(
            os.path.join(self._cwd, "tree_child.py"),
            "import json, subprocess, sys, time\n"
            "gc = subprocess.Popen([sys.executable, '-c', "
            + repr(SLEEP_600)
            + "])\n"
            "print(json.dumps({'grandchild': gc.pid}), flush=True)\n"
            "time.sleep(600)\n",
        )
        unrelated = self._popen([sys.executable, "-c", SLEEP_600])
        self.assertTrue(psutil.pid_exists(unrelated.pid))

        proc = self._spawn([sys.executable, child_script])
        info = json.loads((await self._line(proc)).decode())
        grandchild_pid = int(info["grandchild"])
        self.assertTrue(psutil.pid_exists(grandchild_pid))
        proc_pid = proc.pid

        stopped = await proc.terminate_tree()
        self.assertTrue(stopped)

        self.assertTrue(_wait_gone(proc_pid), f"child {proc_pid} survived")
        self.assertTrue(
            _wait_gone(grandchild_pid), f"grandchild {grandchild_pid} survived"
        )
        self.assertTrue(
            psutil.pid_exists(unrelated.pid),
            f"unrelated sleeper {unrelated.pid} was killed",
        )

    # -- 2: normal turn end must NOT reap the tree --------------------------------
    async def test_normal_close_leaves_tool_jobs_alive(self) -> None:
        """A tool-launched background job must survive the turn boundary.

        Upstream AgentsDock does not kill the process group when a turn ends
        normally; POSIX tool jobs survive via nohup/setsid semantics. A
        previous winfs-era revision killed every job member when the job
        handle closed (KILL_ON_JOB_CLOSE) — regression-guarded here.
        """
        child_script = _write(
            os.path.join(self._cwd, "orphan_child.py"),
            "import json, subprocess, sys\n"
            "gc = subprocess.Popen([sys.executable, '-c', "
            + repr(SLEEP_600)
            + "])\n"
            "print(json.dumps({'grandchild': gc.pid}), flush=True)\n"
            "import os; os._exit(0)\n",
        )
        proc = self._spawn([sys.executable, child_script])
        info = json.loads((await self._line(proc)).decode())
        grandchild_pid = int(info["grandchild"])

        exit_code = await asyncio.wait_for(proc.wait(), timeout=20)
        self.assertEqual(exit_code, 0)
        # Grandchild is still alive, parent is gone — and because the
        # grandchild inherited the stdout pipe, EOF must NOT arrive yet.
        self.assertTrue(psutil.pid_exists(grandchild_pid))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(proc.readline(), timeout=2)

        proc.close()  # normal turn end: closing the job handle kills NOTHING
        self.assertTrue(
            psutil.pid_exists(grandchild_pid),
            f"grandchild {grandchild_pid} was reaped by close()",
        )
        # Explicit stop semantics still reach the whole tree.
        proc_terminate = psutil.Process(grandchild_pid)
        proc_terminate.terminate()
        self.assertTrue(_wait_gone(grandchild_pid))
        # With every writer dead, EOF now reaches the pump.
        self.assertIsNone(await asyncio.wait_for(proc.readline(), timeout=20))

    # -- 3: launch failure / early exit -----------------------------------------
    async def test_launch_failure_raises_and_leaks_nothing(self) -> None:
        missing = os.path.join(self._cwd, "definitely-missing-executable-xyz.exe")
        before = {p.pid for p in psutil.Process().children(recursive=True)}
        for _ in range(10):
            with self.assertRaises(winproc.WinSpawnError):
                winproc.spawn_owned([missing], cwd=self._cwd)
        after = {p.pid for p in psutil.Process().children(recursive=True)}
        self.assertEqual(before, after)

        # A child that exits immediately cannot escape ownership: it stays
        # suspended until the job assignment has completed.
        fast = self._spawn([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(await asyncio.wait_for(fast.wait(), timeout=20), 3)

    # -- 4: stdio echo + chunk reassembly ----------------------------------------
    async def test_stdio_echo_reassembles_chunks(self) -> None:
        echo_script = _write(
            os.path.join(self._cwd, "echo.py"),
            "import sys\n"
            "for line in sys.stdin:\n"
            "    sys.stdout.write(line)\n"
            "    sys.stdout.flush()\n"
            "data = sys.stdin.read()\n"
            "sys.stderr.write('TAIL:' + data + '\\n')\n"
            "sys.stderr.flush()\n",
        )
        proc = self._spawn([sys.executable, echo_script], stdin_mode="pipe")

        proc.write(b"hello world\n")
        # The child's text-mode stdout translates \n to \r\n on Windows.
        self.assertEqual(await self._line(proc), b"hello world\r\n")

        big = b"x" * 100_000 + b"\n"  # far larger than any single pipe chunk
        proc.write(big)
        self.assertEqual(await self._line(proc), big[:-1] + b"\r\n")

        proc.close_stdin()
        self.assertIsNone(await asyncio.wait_for(proc.readline(), timeout=20))
        stderr = await asyncio.wait_for(proc.read_stderr(), timeout=20)
        self.assertIn(b"TAIL:", stderr)

    # -- 5: argv fidelity ----------------------------------------------------------
    async def test_argv_roundtrip_direct_and_resolved(self) -> None:
        argv_script = _write(
            os.path.join(self._cwd, "argv_printer.py"),
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:]), flush=True)\n",
        )
        args = [
            "plain",
            "with space",
            'with "quotes"',
            "amp&ersand",
            "percent%s%d%PATH%",
            "héllo wörld",
            "tab\there",
            "x" * 200,
            "trailing\\",
        ]
        for argv in (
            [sys.executable, argv_script, *args],
            [*winproc.resolve_cli_argv(sys.executable), argv_script, *args],
        ):
            proc = self._spawn(argv)
            line = await self._line(proc)
            self.assertEqual(json.loads(line.decode()), args)
            self.assertEqual(await asyncio.wait_for(proc.wait(), timeout=20), 0)

    # -- 5b: npm shim resolution (real node, real shim layout) ----------------------
    async def test_resolve_cli_argv_parses_npm_shim(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        shim_dir = os.path.join(self._cwd, "npm-shim")
        os.makedirs(os.path.join(shim_dir, "node_modules", "fakecli"))
        cli_js = _write(
            os.path.join(shim_dir, "node_modules", "fakecli", "cli.js"),
            "console.log(JSON.stringify(process.argv.slice(2)));\n",
        )
        _write(
            os.path.join(shim_dir, "mycli.cmd"),
            "@ECHO off\n"
            "@IF EXIST \"%~dp0\\node.exe\" (\n"
            "  \"%~dp0\\node.exe\"  \"%~dp0\\node_modules\\fakecli\\cli.js\" %*\n"
            ") ELSE (\n"
            "  node  \"%~dp0\\node_modules\\fakecli\\cli.js\" %*\n"
            ")\n",
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = shim_dir + os.pathsep + old_path
        try:
            resolved = winproc.resolve_cli_argv("mycli")
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(len(resolved), 2, resolved)
        self.assertTrue(resolved[0].lower().endswith("node.exe"), resolved)
        self.assertEqual(os.path.normcase(resolved[1]), os.path.normcase(cli_js))

        args = ["a b", "héllo wörld", "100%-raw", 'q"q', "&"]
        proc = self._spawn([*resolved, *args])
        line = await self._line(proc)
        self.assertEqual(json.loads(line.decode()), args)

        # Broken shim (js target absent) falls back to the shim path itself.
        os.makedirs(os.path.join(shim_dir, "node_modules", "broken"))
        _write(
            os.path.join(shim_dir, "broken.cmd"),
            '@IF EXIST "%~dp0\\node.exe" (\n'
            '  "%~dp0\\node.exe"  "%~dp0\\node_modules\\broken\\missing.js" %*\n'
            ") ELSE (\n"
            '  node  "%~dp0\\node_modules\\broken\\missing.js" %*\n'
            ")\n",
        )
        os.environ["PATH"] = shim_dir + os.pathsep + old_path
        try:
            fallback = winproc.resolve_cli_argv("broken")
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(len(fallback), 1)
        self.assertTrue(fallback[0].lower().endswith("broken.cmd"))

    # -- 6: process_rows ---------------------------------------------------------
    async def test_process_rows_shape(self) -> None:
        sleeper = self._spawn([sys.executable, "-c", SLEEP_600])
        rows = winproc.process_rows(limit=100_000)
        self.assertIsInstance(rows, list)
        by_pid = {row["pid"]: row for row in rows}
        row = by_pid[sleeper.pid]
        self.assertEqual(row["ppid"], os.getpid())
        self.assertIsNone(row["pgid"])
        self.assertIsNone(row["sid"])
        self.assertIsNone(row["stat"])
        self.assertGreaterEqual(row["rss_kb"], 0)
        self.assertGreaterEqual(row["elapsed_seconds"], 0)
        self.assertTrue(row["command"])
        self.assertIn("sleep", row["args"])
        for key in (
            "pid",
            "ppid",
            "pgid",
            "sid",
            "stat",
            "elapsed_seconds",
            "cpu_percent",
            "mem_percent",
            "rss_kb",
            "command",
            "args",
        ):
            self.assertIn(key, row)
        self.assertLessEqual(len(winproc.process_rows(limit=3)), 3)
        # Never raises, even with a nonsense limit.
        self.assertIsInstance(winproc.process_rows(limit=-1), list)

    # -- 7: ownership boundary ----------------------------------------------------
    async def test_terminating_one_proc_leaves_other_alive(self) -> None:
        first = self._spawn([sys.executable, "-c", SLEEP_600])
        second = self._spawn([sys.executable, "-c", SLEEP_600])
        self.assertTrue(psutil.pid_exists(first.pid))
        self.assertTrue(psutil.pid_exists(second.pid))

        await first.terminate_tree()
        self.assertTrue(_wait_gone(first.pid), f"first {first.pid} survived")
        self.assertTrue(
            psutil.pid_exists(second.pid),
            f"second {second.pid} was killed by first's terminate",
        )
        await second.terminate_tree()
        self.assertTrue(_wait_gone(second.pid), f"second {second.pid} survived")

    # -- 8: asyncio.Process-shaped facade (integration surface) ---------------------
    async def test_stream_facade_matches_asyncio_process_shape(self) -> None:
        echo_script = _write(
            os.path.join(self._cwd, "facade_echo.py"),
            "import sys\n"
            "for line in sys.stdin:\n"
            "    sys.stdout.write(line)\n"
            "    sys.stdout.flush()\n"
            "sys.stderr.write('facade-stderr\\n')\n"
            "sys.stderr.flush()\n",
        )
        proc = self._spawn([sys.executable, echo_script], stdin_mode="pipe")
        self.assertTrue(proc.stdin)
        proc.stdin.write(b"facade-line\n")
        await proc.stdin.drain()
        self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 20), b"facade-line\r\n")
        proc.stdin.close()
        self.assertTrue(proc.stderr)
        self.assertIsNone(await asyncio.wait_for(proc.stdout.readline(), 20))
        stderr = await asyncio.wait_for(proc.stderr.read(), 20)
        self.assertIn(b"facade-stderr", stderr)
        self.assertEqual(await asyncio.wait_for(proc.wait(), 20), 0)

    # -- probe: asyncio CREATE_SUSPENDED finding -----------------------------------
    async def test_probe_asyncio_create_subprocess_exec_suspended(self) -> None:
        # Documents the design finding: on Windows the kwarg passes through to
        # Popen (no TypeError) but asyncio exposes no thread-resume, so a
        # CREATE_SUSPENDED child hangs forever.  spawn_owned() therefore uses
        # Popen directly.  If this test ever fails, asyncio may have grown a
        # resume API and the Popen bridge can be revisited.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('awake')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=winproc.CREATE_SUSPENDED,
        )
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(proc.stdout.readline(), timeout=3)
        finally:
            proc.kill()
            await proc.wait()


if __name__ == "__main__":
    unittest.main()
