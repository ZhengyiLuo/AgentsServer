"""Real-ConPTY tests for winterminal.py.

Every test here spawns real shells through pywinpty on this box (no mocks);
mock-only tests were explicitly called out as insufficient evidence.

Run:  .venv/Scripts/python.exe -m unittest test_winterminal -v
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

import winterminal
from winterminal import (
    SUPPORTED,
    HISTORY_MAX_BYTES,
    READ_CHUNK_CHARS,
    READ_QUEUE_MAX_CHUNKS,
    TerminalManager,
    TerminalSession,
    unavailable_reason,
    WinTerminalError,
)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

PY = sys.executable
TICK_CODE = (
    "import time;"
    "[(print('tick',i,flush=True),time.sleep(0.2)) for i in range(50)]"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b.")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


async def read_until(sess, pred, timeout=25.0):
    """Accumulate decoded output until ``pred(text)`` is true."""
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        chunk = await sess.read(timeout=min(0.5, max(0.05, remaining)))
        if chunk:
            text += chunk.decode("utf-8", "replace")
        if pred(text):
            return text
        if sess.at_eof:
            break
    raise AssertionError(f"timed out; tail of output: {text[-500:]!r}")


def max_tick(text: str) -> int:
    nums = [int(m) for m in re.findall(r"tick (\d+)", text)]
    return max(nums) if nums else -1


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestSupport(unittest.TestCase):
    def test_supported_and_reason(self):
        self.assertTrue(SUPPORTED)
        self.assertIsNone(unavailable_reason())


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestCmdSession(unittest.IsolatedAsyncioTestCase):
    async def test_1_cmd_echo_roundtrip(self):
        """cmd.exe in a temp cwd: prompt appears, echo MARKER123 comes back."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(cwd=tmp, columns=80, rows=24)
        self.addCleanup(sess.terminate)
        banner = await read_until(
            sess, lambda t: strip_ansi(t).rstrip().endswith(">")
        )
        self.assertIn(">", strip_ansi(banner))
        sess.write(b"echo MARKER123\r\n")
        out = await read_until(sess, lambda t: "MARKER123" in strip_ansi(t))
        self.assertIn("MARKER123", strip_ansi(out))
        self.assertTrue(sess.alive)

    async def test_3_resize(self):
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(cwd=tmp, columns=80, rows=24)
        self.addCleanup(sess.terminate)
        await read_until(sess, lambda t: strip_ansi(t).rstrip().endswith(">"))
        sess.resize(200, 50)
        # pywinpty-level round trip: getwinsize() is (rows, cols).
        self.assertEqual(sess._proc.getwinsize(), (50, 200))
        # Best-effort console-reported size; not asserted per spec.
        sess.write(b"mode con\r\n")
        try:
            out = await read_until(
                sess, lambda t: "Columns" in strip_ansi(t), timeout=8.0
            )
            m = re.search(r"Columns:\s*(\d+)", strip_ansi(out))
            if m:
                print(f"mode con reports columns={m.group(1)} (expected 200)")
        except AssertionError:
            print("mode con output inconclusive; resize verified via getwinsize()")

    async def test_6_exit_detected(self):
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(cwd=tmp, columns=80, rows=24)
        self.addCleanup(sess.terminate)
        await read_until(sess, lambda t: strip_ansi(t).rstrip().endswith(">"))
        sess.write(b"exit\r\n")
        # Drain until EOF marks the session dead (observed ~1.75s on this box).
        deadline = time.monotonic() + 25.0
        while sess.alive and not sess.at_eof and time.monotonic() < deadline:
            await sess.read(timeout=0.5)
        self.assertFalse(sess.alive, "session still alive after exit")
        # ``alive`` flips False as soon as the process handle dies; the reader
        # thread marks at_eof a moment later once the buffer drains -- wait
        # for it (bounded).
        deadline = time.monotonic() + 15.0
        while not sess.at_eof and time.monotonic() < deadline:
            await sess.read(timeout=0.5)
        self.assertTrue(sess.at_eof)
        self.assertIsNotNone(sess.exit_code)


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestPythonShell(unittest.IsolatedAsyncioTestCase):
    async def test_2_python_interactive(self):
        """Interactive Python REPL: proves bidirectional I/O beyond cmd builtins."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(
            cwd=tmp, columns=80, rows=24, shell=PY, shell_args=["-i"]
        )
        self.addCleanup(sess.terminate)
        await read_until(sess, lambda t: strip_ansi(t).rstrip().endswith(">>>"))
        sess.write("print(6*7)\r\n")
        out = await read_until(sess, lambda t: "42" in t)
        self.assertIn("42", out)


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestManager(unittest.IsolatedAsyncioTestCase):
    async def test_4_detach_reconnect_persists(self):
        """detach() keeps the shell running; get_or_create returns the same session."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mgr = TerminalManager(max_sessions=8)
        sess = mgr.get_or_create(
            "chat-1", cwd=tmp, columns=80, rows=24, shell=PY, shell_args=["-c", TICK_CODE]
        )
        self.addCleanup(mgr.close_all)
        text = await read_until(sess, lambda t: max_tick(t) >= 2, timeout=15.0)
        seen = max_tick(text)
        mgr.detach("chat-1")  # simulate WebSocket drop; shell must keep running
        again = mgr.get_or_create("chat-1", cwd=tmp, columns=80, rows=24)
        self.assertIs(again, sess, "reconnect must return the same live session")
        more = await read_until(
            again, lambda t: max_tick(t) >= seen + 3, timeout=15.0
        )
        self.assertGreaterEqual(max_tick(more), seen + 3)

    async def test_5_snapshot_history(self):
        """snapshot(N) returns the last N retained lines including recent ticks."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mgr = TerminalManager(max_sessions=8)
        sess = mgr.get_or_create(
            "chat-hist", cwd=tmp, columns=80, rows=24, shell=PY, shell_args=["-c", TICK_CODE]
        )
        self.addCleanup(mgr.close_all)
        await read_until(sess, lambda t: max_tick(t) >= 5, timeout=15.0)
        await asyncio.sleep(0.3)  # let a few more ticks land in the ring
        snap = sess.snapshot(5)
        lines = snap.split("\n")
        self.assertLessEqual(len(lines), 5)
        self.assertIn("tick", lines[-1], f"last line should be a recent tick: {snap!r}")
        self.assertIn("tick", snap)

    @unittest.skipIf(psutil is None, "psutil not installed")
    async def test_7_terminate_midrun(self):
        """terminate() kills a running child; pid disappears; no winpty-agent."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(
            cwd=tmp, columns=80, rows=24, shell=PY, shell_args=["-c", TICK_CODE]
        )
        self.addCleanup(sess.terminate)
        pid = sess.pid
        self.assertTrue(psutil.pid_exists(pid))
        await read_until(sess, lambda t: max_tick(t) >= 1, timeout=15.0)
        sess.terminate()
        self.assertFalse(sess.alive)
        deadline = time.monotonic() + 8.0
        while psutil.pid_exists(pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        self.assertFalse(
            psutil.pid_exists(pid), f"pid {pid} still alive after terminate()"
        )
        # ConPTY backend: no winpty-agent processes should exist at all.
        agents = [
            p.pid
            for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower().startswith("winpty-agent")
        ]
        self.assertEqual(agents, [], "winpty-agent processes lingering")

    async def test_8_backpressure_bounded(self):
        """Detached flood: history capped, queue bounded, no unbounded growth."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        flood = (
            "import time;"
            "[(print('FLOOD',i,'y'*120,flush=True),time.sleep(0.0004))"
            " for i in range(9000)]"
        )  # ~1.2 MB over ~4s, nothing reads it
        mgr = TerminalManager(max_sessions=8)
        sess = mgr.get_or_create(
            "chat-flood", cwd=tmp, columns=80, rows=24, shell=PY, shell_args=["-c", flood]
        )
        self.addCleanup(mgr.close_all)
        mgr.detach("chat-flood")
        deadline = time.monotonic() + 60.0
        while not sess.at_eof and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        self.assertTrue(sess.at_eof, "flood child did not finish in time")
        # Ring buffer must respect the byte cap (plus one chunk tolerance).
        chunk_slack = 4 * READ_CHUNK_CHARS
        self.assertLessEqual(sess._history_bytes, HISTORY_MAX_BYTES + chunk_slack)
        snap = sess.snapshot(100000)
        self.assertLessEqual(len(snap.encode("utf-8")), HISTORY_MAX_BYTES + chunk_slack)
        self.assertIn("FLOOD", snap)
        # Unread queue bounded: count and total bytes within documented limits.
        if sess._queue is not None:
            pending = [sess._queue.get_nowait() for _ in range(sess._queue.qsize())]
            self.assertLessEqual(len(pending), READ_QUEUE_MAX_CHUNKS)
            total = sum(len(c) for c in pending if c)
            self.assertLessEqual(total, READ_QUEUE_MAX_CHUNKS * chunk_slack)
            for c in pending:
                if c is not None:
                    sess._queue.put_nowait(c)

    async def test_10_manager_independence_and_close_all(self):
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mgr = TerminalManager(max_sessions=8)
        self.addCleanup(mgr.close_all)
        s1 = mgr.get_or_create("a", cwd=tmp, columns=80, rows=24)
        s2 = mgr.get_or_create("b", cwd=tmp, columns=80, rows=24)
        self.assertIsNot(s1, s2)
        self.assertNotEqual(s1.pid, s2.pid)
        await read_until(s1, lambda t: strip_ansi(t).rstrip().endswith(">"))
        await read_until(s2, lambda t: strip_ansi(t).rstrip().endswith(">"))
        s1.write(b"echo ONLY_SESSION_AAA\r\n")
        s2.write(b"echo ONLY_SESSION_BBB\r\n")
        o1 = await read_until(s1, lambda t: "ONLY_SESSION_AAA" in strip_ansi(t))
        o2 = await read_until(s2, lambda t: "ONLY_SESSION_BBB" in strip_ansi(t))
        self.assertIn("ONLY_SESSION_AAA", strip_ansi(o1))
        self.assertNotIn("ONLY_SESSION_BBB", strip_ansi(o1))
        self.assertIn("ONLY_SESSION_BBB", strip_ansi(o2))
        self.assertNotIn("ONLY_SESSION_AAA", strip_ansi(o2))
        self.assertEqual(len(mgr), 2)
        mgr.close_all()
        self.assertEqual(len(mgr), 0)
        self.assertFalse(s1.alive)
        self.assertFalse(s2.alive)


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestValidation(unittest.TestCase):
    def test_9_cwd_validation(self):
        bad = os.path.join(tempfile.gettempdir(), "winterminal-no-such-dir-xyz")
        self.assertFalse(os.path.exists(bad))
        with self.assertRaises(WinTerminalError):
            TerminalSession.create(cwd=bad, columns=80, rows=24)

    def test_bad_sizes_raise_valueerror(self):
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(ValueError):
            TerminalSession.create(cwd=tmp, columns=0, rows=24)
        sess = TerminalSession.create(cwd=tmp, columns=80, rows=24)
        self.addCleanup(sess.terminate)
        with self.assertRaises(ValueError):
            sess.resize(80, -1)
        sess.terminate()
        with self.assertRaises(WinTerminalError):
            sess.write(b"echo nope\r\n")


@unittest.skipUnless(SUPPORTED, f"winterminal unsupported here: {unavailable_reason()}")
class TestReaderFailure(unittest.IsolatedAsyncioTestCase):
    async def test_reader_fails_closed_on_persistent_read_errors(self):
        """A pty that keeps raising OSError outside terminate() must not
        busy-loop: the reader thread dies within a bounded time, the session
        reports dead, and blocked async readers get the EOF sentinel."""
        tmp = tempfile.mkdtemp(prefix="winterminal-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = TerminalSession.create(cwd=tmp, columns=80, rows=24)
        self.addCleanup(sess.terminate)
        reader = sess._reader

        calls = 0

        def failing_read(size=1024):
            nonlocal calls
            calls += 1
            raise OSError("simulated persistent pty failure")

        sess._proc.read = failing_read
        deadline = time.monotonic() + 5.0
        while reader.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(
            reader.is_alive(),
            f"reader thread still alive after persistent read errors ({calls} calls)",
        )
        # Fail-closed fast: a busy loop would have made far more calls. The
        # implementation retries at most a couple of times with backoff.
        self.assertLessEqual(calls, 10, f"read retried too many times ({calls}): busy loop?")
        self.assertTrue(sess.at_eof)
        self.assertFalse(sess.alive)
        # Async waiters are unblocked with the EOF sentinel.
        saw_eof = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            chunk = await sess.read(timeout=0.5)
            if chunk is None:
                saw_eof = True
                break
        self.assertTrue(saw_eof, "read() never returned EOF after reader died")


if __name__ == "__main__":
    unittest.main()
