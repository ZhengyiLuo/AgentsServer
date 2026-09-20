"""winterminal -- ConPTY-backed persistent terminal sessions for AgentsServer.

This module replaces the Unix-side tmux + ``pty.openpty`` + ``fcntl.ioctl``
stack on Windows.  It is built on ``pywinpty`` (``winpty.PtyProcess``), which
talks to the Windows ConPTY layer (Windows 10 1809+ / Windows 11).  On this
box pywinpty 3.0.5 defaults to the ConPTY backend, so no ``winpty-agent``
processes are created and each session is serviced by a transient
``conhost.exe`` that exits when the pseudoterminal handle is closed.

Verified pywinpty 3.0.5 API facts (this box, Windows 11):

* ``PtyProcess.spawn(argv, cwd=None, env=None, dimensions=(24, 80),
  backend=None)`` -- ``dimensions`` is ``(rows, cols)``; ``argv`` is a list
  whose first element is resolved against ``PATH`` (absolute paths work);
  ``env`` is a full replacement mapping (``os.environ`` is used when falsy);
  a single-element argv spawns plain ``cmd.exe`` which stays interactive
  under ConPTY (no ``/k`` needed).
* ``read(size=1024) -> str`` -- **blocking**; returns decoded utf-8 text;
  raises ``EOFError`` only after the child has died *and* the internal
  buffer has drained.  The reliable "is the shell dead" signal is
  ``isalive()`` going False, not ``eof()``.
* ``write(s: str) -> int`` -- requires ``str`` (bytes are rejected with
  TypeError); raises ``EOFError`` when the child is gone.
* ``setwinsize(rows, cols)`` / ``getwinsize() -> (rows, cols)`` -- note the
  low-level ``PTY.set_size(cols, rows)`` takes the arguments in the opposite
  order; PtyProcess flips them for us.
* ``exitstatus`` -- ``None`` while the child runs; the exit code (int) once
  the child has been reaped (natural exit observed as ``0``).
* ``terminate(force=False)`` -> sends SIGINT via ``os.kill`` which on
  Windows unconditionally calls ``TerminateProcess`` (exit code = signal
  number, e.g. 2).  It does **not** kill console-attached grandchildren, so
  :meth:`TerminalSession.terminate` additionally walks the process tree with
  psutil when it is importable.
* ``close(force=False)`` -- idempotent; closes the internal loopback socket,
  then terminates a still-running child.

Design / policies:

* **Async read bridge**: one daemon thread per session loops on the blocking
  ``PtyProcess.read()`` and forwards chunks to an ``asyncio.Queue`` bound to
  the consuming event loop via ``loop.call_soon_threadsafe``.  ``read()``
  must be awaited from the loop that first calls it.  ``loop.add_reader`` is
  never used (not available on Windows' ProactorEventLoop).
* **Backpressure (drop policy)**: the queue is bounded
  (``READ_QUEUE_MAX_CHUNKS`` x up to ``READ_CHUNK_CHARS`` utf-8 chars).
  When it is full, the *newest* chunk is dropped and counted in
  ``dropped_chunks`` rather than blocking the reader thread, so a detached
  session whose child floods output can never grow memory without bound.
  Interactive clients that consume promptly never hit this.
* **History retention**: all output also flows through a bounded byte ring
  (``HISTORY_MAX_BYTES``, default 512 KiB) implemented as a chunk deque;
  when the cap is exceeded the *oldest* bytes are discarded.  ``snapshot()``
  decodes it utf-8/replace and returns the last N lines -- this is the
  tmux ``capture-pane`` replacement for freshly attached WebSocket clients.
* **EOF/exit detection**: when the reader thread sees ``EOFError`` (or any
  fatal read error) the session is marked dead, the exit code is reaped via
  ``exitstatus``, and a ``None`` sentinel is queued.  ``read()`` returns
  ``None`` both at EOF and on timeout -- check ``alive`` / ``at_eof`` to
  distinguish ("no data yet" vs "shell is gone").
* **Writes pass through verbatim**: no ``\\n`` -> ``\\r\\n`` translation;
  terminal clients are expected to send ``\\r`` as Enter, matching what a
  real pty consumer sends.  ``write()`` accepts ``bytes`` (decoded utf-8,
  undecodable bytes replaced) or ``str``.
* **Cleanup**: ``terminate()`` is idempotent, kills the process tree
  (psutil, optional), closes the pty (child + conhost exit), wakes the
  reader thread, and never raises -- safe from ``atexit`` /
  ``KeyboardInterrupt`` teardown.  ``TerminalManager.close_all()`` is
  registered with ``atexit`` when a manager is created.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque

try:
    from winpty import PtyProcess
    from winpty import WinptyError as _WinptyError

    _WINPTY_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only off-Windows
    PtyProcess = None
    _WinptyError = Exception
    _WINPTY_IMPORT_ERROR = exc

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - optional dependency
    _psutil = None

__all__ = [
    "SUPPORTED",
    "unavailable_reason",
    "WinTerminalError",
    "TerminalSession",
    "TerminalManager",
    "HISTORY_MAX_BYTES",
    "READ_QUEUE_MAX_CHUNKS",
]

SUPPORTED: bool = PtyProcess is not None and sys.platform == "win32"

#: History ring capacity in bytes (tmux capture-pane equivalent).
HISTORY_MAX_BYTES: int = 512 * 1024

#: Maximum number of unread chunks queued for async consumers.  Beyond this,
#: newest chunks are dropped (see module docstring).
READ_QUEUE_MAX_CHUNKS: int = 128

#: Maximum chunk size requested from pywinpty per read; bounds the size of a
#: single queue item (<= ~4 bytes/char * CHUNK utf-8 chars).
READ_CHUNK_CHARS: int = 16384


def unavailable_reason() -> str | None:
    """Return None if this backend can run here, else a human-readable reason."""
    if SUPPORTED:
        return None
    if sys.platform != "win32":
        return "winterminal requires Windows with the ConPTY layer (win32)"
    return f"pywinpty is not importable: {_WINPTY_IMPORT_ERROR}"


class WinTerminalError(Exception):
    """Raised for terminal-session failures (bad cwd, closed session, ...)."""


def _default_shell() -> str:
    shell = os.environ.get("COMSPEC")
    if shell:
        return shell
    shell = shutil.which("cmd.exe")
    if shell:
        return shell
    raise WinTerminalError(
        "No shell available: COMSPEC is unset and cmd.exe was not found on PATH"
    )


def _kill_process_tree(pid: int) -> None:
    """Best-effort kill of the session's process tree (children first).

    pywinpty's own ``terminate()`` only TerminateProcess()es the root pid;
    console-attached grandchildren (e.g. cmd.exe -> python.exe) would be
    orphaned.  psutil is optional; without it this is a no-op and the caller
    relies on ``PtyProcess.close(force=True)``.
    """
    if _psutil is None or pid is None:
        return
    try:
        parent = _psutil.Process(pid)
        children = parent.children(recursive=True)
    except Exception:
        return
    for child in children:
        try:
            child.kill()
        except Exception:
            pass


class TerminalSession:
    """One persistent interactive shell running under a Windows ConPTY.

    Create with :meth:`create` from within (or for) the asyncio event loop
    that will consume :meth:`read`.  The session keeps running until
    :meth:`terminate` is called -- dropping all Python references or the
    WebSocket disconnecting does *not* kill the shell (see
    ``TerminalManager.detach``).
    """

    def __init__(self) -> None:
        raise TypeError("Use TerminalSession.create()")

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
        cwd: str,
        columns: int,
        rows: int,
        shell: str | None = None,
        env: dict | None = None,
        shell_args: list | None = None,
    ) -> "TerminalSession":
        """Spawn ``shell`` (default: %COMSPEC% -> cmd.exe) in a new ConPTY.

        ``shell_args`` is an optional argv tail for the shell (extension
        beyond the bare spec, e.g. ``shell=sys.executable,
        shell_args=["-i"]`` spawns an interactive Python REPL); each element
        is stringified and quoted by pywinpty.

        Raises :class:`WinTerminalError` for a missing/unwritable cwd or an
        unspawnable shell; :class:`ValueError` for non-positive sizes.
        """
        cwd = os.path.abspath(str(cwd))
        if not os.path.isdir(cwd):
            raise WinTerminalError(f"cwd does not exist or is not a directory: {cwd!r}")
        cls._validate_size(columns, rows)
        shell = shell or _default_shell()

        full_env = dict(os.environ)
        if env:
            full_env.update({str(k): str(v) for k, v in env.items()})

        if PtyProcess is None:  # pragma: no cover - SUPPORTED guards this
            raise WinTerminalError(unavailable_reason() or "pywinpty unavailable")

        try:
            argv = [shell, *[str(a) for a in (shell_args or [])]]
            proc = PtyProcess.spawn(
                argv,
                cwd=cwd,
                env=full_env,
                dimensions=(int(rows), int(columns)),
            )
        except FileNotFoundError as exc:
            raise WinTerminalError(f"shell not found: {shell!r} ({exc})") from exc
        except _WinptyError as exc:
            raise WinTerminalError(f"failed to spawn shell {shell!r}: {exc}") from exc
        except OSError as exc:
            raise WinTerminalError(f"failed to spawn shell {shell!r}: {exc}") from exc

        self = object.__new__(cls)
        self._proc = proc
        self.pid: int = proc.pid
        self.shell: str = shell
        self.cwd: str = cwd
        self.columns: int = int(columns)
        self.rows: int = int(rows)
        self.detached: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._pending: deque[bytes | None] = deque()  # pre-loop-bound chunks
        self._pending_lock = threading.Lock()

        self._history: deque[bytes] = deque()
        self._history_bytes = 0
        self._history_lock = threading.Lock()

        self._alive = True
        self._at_eof = False
        self._exit_code: int | None = None
        self._terminated = False
        self._sentinel_sent = False
        self.dropped_chunks: int = 0
        self._state_lock = threading.Lock()

        self._stop = threading.Event()
        self._reader = threading.Thread(
            target=self._reader_main,
            name=f"winterminal-reader-{proc.pid}",
            daemon=True,
        )
        self._reader.start()
        return self

    @staticmethod
    def _validate_size(columns: int, rows: int) -> None:
        for name, value in (("columns", columns), ("rows", rows)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
            if value > 0x7FFF:
                raise ValueError(f"{name} is unreasonably large: {value!r}")

    # -- reader thread ----------------------------------------------------

    def _reader_main(self) -> None:
        """Drain the pty into history + the async queue until EOF/close."""
        proc = self._proc
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                data = proc.read(READ_CHUNK_CHARS)
            except EOFError:
                break  # child exited and buffer drained
            except (OSError, ValueError):
                if self._stop.is_set():
                    break  # socket closed by terminate()
                # A persistently erroring pty (e.g. conhost killed externally)
                # must fail closed like EOF -- unblocking waiters, recording
                # exit state, and ending this thread -- instead of
                # busy-looping on a CPU core for the session's lifetime.
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                time.sleep(0.01)  # small backoff between retries
                continue
            except Exception:
                break  # unexpected: stop feeding, mark dead below
            consecutive_errors = 0
            if data:
                chunk = data.encode("utf-8", "replace")
                self._append_history(chunk)
                self._enqueue(chunk)
        self._mark_dead()

    def _append_history(self, chunk: bytes) -> None:
        with self._history_lock:
            self._history.append(chunk)
            self._history_bytes += len(chunk)
            while self._history_bytes > HISTORY_MAX_BYTES and self._history:
                old = self._history.popleft()
                self._history_bytes -= len(old)
            # A single oversized chunk is trimmed from the front rather than
            # retained whole, so the bound holds even for huge burst writes.
            if self._history_bytes > HISTORY_MAX_BYTES and self._history:
                excess = self._history_bytes - HISTORY_MAX_BYTES
                first = self._history[0]
                self._history[0] = first[excess:]
                self._history_bytes -= excess

    def _enqueue(self, item: bytes | None) -> None:
        """Forward a chunk/sentinel to the asyncio queue, or buffer it.

        Before the consumer loop is known, items wait in ``_pending`` (still
        bounded: pending + history share the same drop discipline).  Once the
        loop is bound, a full queue drops the *newest* chunk (never blocks
        the reader thread).
        """
        loop = self._loop
        if loop is None:
            with self._pending_lock:
                if len(self._pending) >= READ_QUEUE_MAX_CHUNKS:
                    if item is not None:
                        self.dropped_chunks += 1
                    return
                self._pending.append(item)
            return
        try:
            loop.call_soon_threadsafe(self._queue_put_nowait_drop, item)
        except RuntimeError:
            # Loop closed (server shutdown); history retention continues.
            pass

    def _queue_put_nowait_drop(self, item: bytes | None) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            if item is not None:
                self.dropped_chunks += 1

    def _mark_dead(self) -> None:
        with self._state_lock:
            if self._at_eof:
                return
            self._at_eof = True
            self._alive = False
            try:
                self._exit_code = self._proc.exitstatus
            except Exception:
                self._exit_code = None
        self._enqueue_sentinel()

    def _enqueue_sentinel(self) -> None:
        with self._state_lock:
            if self._sentinel_sent:
                return
            self._sentinel_sent = True
        self._enqueue(None)

    # -- async reading ----------------------------------------------------

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise WinTerminalError(
                "TerminalSession.read() must be called from an asyncio event loop"
            ) from exc
        with self._pending_lock:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=READ_QUEUE_MAX_CHUNKS)
            while self._pending:
                item = self._pending.popleft()
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    if item is not None:
                        self.dropped_chunks += 1

    async def read(self, timeout: float | None = None) -> bytes | None:
        """Return the next output chunk (bytes), or ``None``.

        ``None`` means either EOF (``at_eof`` / ``not alive``) or that
        ``timeout`` seconds passed with no output -- check ``alive`` to tell
        them apart.  Raises :class:`WinTerminalError` when called outside an
        event loop.
        """
        self._ensure_loop()
        if self._at_eof and self._queue.empty():
            return None
        try:
            if timeout is None:
                item = await self._queue.get()
            else:
                item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if item is None:
            # Keep EOF sticky for later callers even after the sentinel is
            # consumed.
            self._at_eof = True
            return None
        return item

    def stream(self, timeout: float | None = 0.5):
        """Async generator of output chunks; ends at EOF."""

        async def _gen():
            while True:
                chunk = await self.read(timeout=timeout)
                if chunk is None:
                    if self.at_eof:
                        return
                    continue
                yield chunk

        return _gen()

    # -- writing / resizing ------------------------------------------------

    def write(self, data: bytes | str) -> int:
        """Write ``data`` to the shell verbatim (no CR/LF translation).

        Accepts ``bytes`` (utf-8) or ``str``.  Clients must terminate input
        lines with ``\\r`` (Enter) themselves.  Raises :class:`WinTerminalError`
        when the session is closed or the pipe is broken.
        """
        if isinstance(data, (bytes, bytearray, memoryview)):
            text = bytes(data).decode("utf-8", "replace")
        elif isinstance(data, str):
            text = data
        else:
            raise TypeError(f"write() accepts bytes or str, got {type(data).__name__}")
        if not self.alive:
            raise WinTerminalError("terminal session is closed")
        try:
            return int(self._proc.write(text))
        except EOFError as exc:
            raise WinTerminalError("terminal session is closed") from exc
        except (_WinptyError, OSError) as exc:
            raise WinTerminalError(f"terminal write failed: {exc}") from exc

    def resize(self, columns: int, rows: int) -> None:
        """Resize the console window.  Raises :class:`ValueError` on bad sizes."""
        self._validate_size(columns, rows)
        if not self.alive:
            raise WinTerminalError("terminal session is closed")
        try:
            self._proc.setwinsize(int(rows), int(columns))
        except (_WinptyError, OSError) as exc:
            raise WinTerminalError(f"terminal resize failed: {exc}") from exc
        self.columns, self.rows = int(columns), int(rows)

    # -- state -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """True while the shell process is running."""
        if self._alive:
            try:
                self._alive = self._proc.isalive()
            except Exception:
                self._alive = False
        return self._alive

    @property
    def at_eof(self) -> bool:
        """True once the reader has seen EOF (all output drained)."""
        return self._at_eof

    @property
    def exit_code(self) -> int | None:
        """Shell exit code once reaped, else ``None`` (see module docstring)."""
        if self._exit_code is None and self._at_eof:
            try:
                self._exit_code = self._proc.exitstatus
            except Exception:
                self._exit_code = None
        return self._exit_code

    def snapshot(self, line_count: int) -> str:
        """Return the last ``line_count`` lines of retained history.

        Decoded utf-8 with errors replaced; lines are split on universal
        newlines and joined with ``\\n``.  Retention is bounded by
        ``HISTORY_MAX_BYTES`` (oldest bytes discarded first), so this is the
        tmux ``capture-pane`` equivalent for freshly attached clients.
        """
        if line_count < 1:
            raise ValueError("line_count must be >= 1")
        with self._history_lock:
            blob = b"".join(self._history)
        text = blob.decode("utf-8", "replace")
        lines = text.splitlines()
        return "\n".join(lines[-line_count:])

    # -- teardown ----------------------------------------------------------

    def terminate(self) -> None:
        """Kill the shell (process tree), close the pty, stop the reader.

        Idempotent and never raises -- safe from ``atexit`` and
        ``KeyboardInterrupt`` teardown.  After this call ``alive`` is False.
        """
        if self._terminated:
            return
        self._terminated = True
        self._stop.set()
        try:
            _kill_process_tree(self.pid)
        except Exception:
            pass
        try:
            self._proc.close(force=True)
        except Exception:
            pass
        # pywinpty's close() skips socket teardown once isalive() has flipped
        # its ``closed`` flag (i.e. the child exited on its own); release the
        # internal loopback pair ourselves so no fd leaks.
        for attr in ("fileobj", "_server"):
            sock = getattr(self._proc, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        with self._state_lock:
            self._alive = False
        self._enqueue_sentinel()
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2.0)


class TerminalManager:
    """Registry of :class:`TerminalSession` keyed by chat-session id.

    All methods are synchronous and cheap enough to call directly from HTTP
    route handlers.  The registry is bounded (default 64); when full, dead
    sessions are evicted LRU first, and if every session is alive a
    :class:`WinTerminalError` is raised rather than killing a live shell.

    A manager registers ``close_all`` with ``atexit`` so shells do not
    outlive the interpreter (including KeyboardInterrupt exits).
    """

    def __init__(self, max_sessions: int = 64) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max = int(max_sessions)
        self._sessions: OrderedDict[str, TerminalSession] = OrderedDict()
        self._lock = threading.Lock()
        atexit.register(self.close_all)

    def get(self, session_id: str) -> TerminalSession | None:
        """Return the live-registered session, or None."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                self._sessions.move_to_end(session_id)
            return sess

    def get_or_create(
        self,
        session_id: str,
        *,
        cwd: str,
        columns: int,
        rows: int,
        shell: str | None = None,
        env: dict | None = None,
        shell_args: list | None = None,
    ) -> TerminalSession:
        """Fetch the existing session or spawn a new shell for ``session_id``.

        An existing session is returned as-is (its shell, cwd and history are
        preserved across WebSocket reconnects); ``cwd``/``columns``/``rows``
        only apply to newly created sessions.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                self._sessions.move_to_end(session_id)
                return existing

        # Spawn outside the registry lock (slow-ish); route handlers for a
        # given session are serialized, so duplicate creates are not expected.
        sess = TerminalSession.create(
            cwd=cwd, columns=columns, rows=rows, shell=shell, env=env,
            shell_args=shell_args,
        )

        with self._lock:
            while len(self._sessions) >= self._max:
                dead = [
                    (sid, s)
                    for sid, s in self._sessions.items()
                    if not s.alive
                ]
                if not dead:
                    try:
                        sess.terminate()
                    finally:
                        raise WinTerminalError(
                            f"terminal session limit reached ({self._max} sessions)"
                        )
                sid, old = dead[0]
                del self._sessions[sid]
                old.terminate()
            self._sessions[session_id] = sess
            return sess

    def detach(self, session_id: str) -> TerminalSession | None:
        """Mark the session as detached; the shell keeps running.

        WebSocket disconnects must not kill the shell, so the session stays
        in the registry (``get_or_create`` returns it again on reconnect) and
        is only torn down by ``close``/eviction/``close_all``.
        """
        sess = self.get(session_id)
        if sess is not None:
            sess.detached = True
        return sess

    def close(self, session_id: str) -> bool:
        """Terminate and forget the session.  Returns True if one existed."""
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.terminate()
        return True

    def close_all(self) -> None:
        """Terminate every registered session (idempotent, never raises)."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            try:
                sess.terminate()
            except Exception:
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
