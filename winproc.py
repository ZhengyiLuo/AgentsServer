"""winproc — race-safe process ownership for AgentsServer on native Windows.

On POSIX, ``agent_server.py`` spawns CLI agents with ``start_new_session=True``
and tears trees down with ``os.killpg``.  Native Windows has neither process
groups (in the POSIX sense) nor SIGTERM; a bare ``TerminateProcess`` only kills
the immediate child and leaves tool-spawned grandchildren behind.

This module provides the Windows replacement built on a Windows **Job Object**:

* ``spawn_owned()`` launches a child with ``CREATE_SUSPENDED`` (so the new
  process image cannot run or spawn anything yet), creates a containment job
  (no kill-on-close; see below), assigns the still-suspended
  process to the job, and only then resumes the child's threads.  Because the
  process is suspended from ``CreateProcess`` until assignment completes there
  is **no window in which the child can fork off an un-owned descendant** — the
  launch/assignment race that a POSIX ``pgid`` assignment still has is closed
  by construction.  The residual races are:

    - threads created by the child *after* resume are contained anyway: the
      job never sets ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``, so every descendant
      stays a job member and ``TerminateJobObject`` reaches it;
    - if the child spawns a detached/interactive-app grandchild that creates
      its own console *and* escapes via breakaway (requires the app to opt
      into breakaway explicitly), the job cannot see it — nothing short of a
      kernel driver can, and npm CLI shims do not do this;
    - if job *assignment* itself fails (e.g. a pre-Win8 host, or the server
      already lives in a job that forbids nesting) the suspended child is
      terminated immediately, every handle is released, and ``WinSpawnError``
      is raised — the module never returns a half-owned process.

* Closing the job handle (``OwnedProc.close()`` at normal turn end) does
  NOT kill anything. Upstream AgentsDock never reaps the process group after
  a turn finishes normally — tool-launched background jobs must survive turn
  boundaries, just as ``nohup``/``setsid`` jobs survive them on POSIX. A
  prior revision used ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` here and was
  measured killing tool-spawned background jobs at every turn end.
  ``TerminateJobObject`` (stop/cancel/idle-kill) still reaches every job
  member, and job assignment still closes the launch/assignment race.

Graceful-stop semantics: Windows has no SIGTERM.  ``OwnedProc.terminate_tree()``
first posts ``CTRL_BREAK_EVENT`` to the child's process group (possible because
spawning uses ``CREATE_NEW_PROCESS_GROUP`` and the children share the server's
console), waits up to ``grace`` seconds for exit — this is the analogue of
SIGTERM for console-attached CLI agents — then calls ``TerminateJobObject`` on
the whole job (SIGKILL analogue: every member, including grandchildren, dies at
once).  When there is no console (service contexts) or the group is gone, the
graceful step is skipped and the job is terminated immediately.

All blocking pipe reads are bridged to asyncio with daemon threads feeding
``queue.Queue``/events drained through the default executor
(``loop.add_reader`` is unsupported on the Windows ProactorEventLoop).  The
reader threads reassemble newline-delimited lines across arbitrary chunk
boundaries via ``readline()`` and keep draining stderr so a chatty child cannot
deadlock on a full pipe.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import errno
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import suppress
from typing import Any, Optional

logger = logging.getLogger("winproc")


class WinSpawnError(RuntimeError):
    """An owned spawn could not be completed (launch or job assignment failed).


    Raised for unlaunchable executables, unsupported hosts, and any failure to
    create/assign/resume around the job object.  No process or handle is left
    behind when this is raised.
    """


_IS_WINDOWS = os.name == "nt"

# --- Windows constants -------------------------------------------------------
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

CTRL_BREAK_EVENT = 1
ERROR_ACCESS_DENIED = 5

# Exit code used when the job is terminated; mirrors a SIGKILL-ish flavour.
JOB_EXIT_CODE = 1

DEFAULT_TERMINATE_GRACE = 2.0

# --- ctypes kernel32 ----------------------------------------------------------
kernel32: Any = None
if _IS_WINDOWS:
    try:
        _kernel32 = ctypes.windll.kernel32
        _kernel32.CreateJobObjectW  # probe: attribute must exist
        kernel32 = _kernel32
    except Exception:  # pragma: no cover - defensive
        kernel32 = None

SUPPORTED = kernel32 is not None


def _define_signatures() -> None:
    if kernel32 is None:
        return
    k = kernel32
    k.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.wintypes.LPVOID, ctypes.wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = ctypes.wintypes.BOOL
    k.SetInformationJobObject.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.ULONG,
    ]
    k.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.HANDLE,
    ]
    k.TerminateJobObject.restype = ctypes.wintypes.BOOL
    k.TerminateJobObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    k.OpenProcess.restype = ctypes.wintypes.HANDLE
    k.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    k.CloseHandle.restype = ctypes.wintypes.BOOL
    k.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    k.OpenThread.restype = ctypes.wintypes.HANDLE
    k.OpenThread.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    k.ResumeThread.restype = ctypes.wintypes.DWORD
    k.ResumeThread.argtypes = [ctypes.wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    k.Thread32First.restype = ctypes.wintypes.BOOL
    k.Thread32First.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]
    k.Thread32Next.restype = ctypes.wintypes.BOOL
    k.Thread32Next.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]
    k.Process32FirstW.restype = ctypes.wintypes.BOOL
    k.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]
    k.Process32NextW.restype = ctypes.wintypes.BOOL
    k.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]
    k.TerminateProcess.restype = ctypes.wintypes.BOOL
    k.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    k.GenerateConsoleCtrlEvent.restype = ctypes.wintypes.BOOL
    k.GenerateConsoleCtrlEvent.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    k.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
    k.GetExitCodeProcess.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    k.GetLastError.restype = ctypes.wintypes.DWORD


_define_signatures()


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ThreadID", ctypes.wintypes.DWORD),
        ("th32OwnerProcessID", ctypes.wintypes.DWORD),
        ("tpBasePri", ctypes.wintypes.LONG),
        ("tpDeltaPri", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.WCHAR * MAX_PATH),
    ]


def _invalid_handle(handle: Any) -> bool:
    return not handle or handle == ctypes.wintypes.HANDLE(-1).value


def _create_owned_job() -> int:
    """Create a Job Object that groups the turn's process tree for containment.

    Deliberately NOT ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: closing the job
    handle at normal turn end must not reap the tree. Upstream AgentsDock only
    kills the process group on stop/idle-kill (or if the CLI itself is still
    running); tool-launched background jobs are expected to survive turn
    boundaries, exactly as ``nohup``/``setsid`` jobs survive them on POSIX.
    Terminate-on-stop is still available explicitly via
    ``OwnedProc.terminate_tree()`` / ``kill()`` (``TerminateJobObject``).
    Trade-off: if the server dies uncleanly, job members may leak — the same
    trade-off POSIX makes.
    """
    handle = kernel32.CreateJobObjectW(None, None)
    if _invalid_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0
        ok = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_process(pid: int, access: int) -> int:
    handle = kernel32.OpenProcess(access, False, pid)
    if _invalid_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _threads_of(pid: int) -> list[int]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if _invalid_handle(snap):
        raise ctypes.WinError(ctypes.get_last_error())
    tids: list[int] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                tids.append(entry.th32ThreadID)
            ok = kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return tids


def _resume_process_threads(pid: int) -> None:
    """Resume every thread of *pid* (a CREATE_SUSPENDED child).

    The Toolhelp snapshot is taken *before* any ResumeThread call, so the
    initial (suspended) threads are all accounted for.  Threads that start
    with suspend count 0 (none for a suspended create) are left alone.
    """
    first_error = 0
    resumed_any = False
    for tid in _threads_of(pid):
        th = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if _invalid_handle(th):
            if not first_error:
                first_error = ctypes.get_last_error()
            continue
        try:
            prev = kernel32.ResumeThread(th)
            if prev == 0xFFFFFFFF:
                if not first_error:
                    first_error = ctypes.get_last_error()
            elif prev > 0:
                resumed_any = True
        finally:
            kernel32.CloseHandle(th)
    if not resumed_any and first_error:
        raise ctypes.WinError(first_error)


def _try_generate_ctrl_break(pid: int) -> bool:
    """Post CTRL_BREAK_EVENT to the process group *pid* (graceful-stop attempt).

    Only safe/possible for console-attached children created with
    CREATE_NEW_PROCESS_GROUP; returns False when the event cannot be posted.
    """
    if kernel32 is None or pid <= 0 or pid == os.getpid():
        return False
    try:
        return bool(kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid))
    except Exception:
        return False


def _terminate_job(handle: Optional[int], exit_code: int = JOB_EXIT_CODE) -> None:
    if handle is None or kernel32 is None:
        return
    if not kernel32.TerminateJobObject(handle, exit_code):
        raise ctypes.WinError(ctypes.get_last_error())


def _descendant_pids(root_pid: int) -> list[int]:
    """Return *root_pid* plus all descendant pids via a Toolhelp process walk."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if _invalid_handle(snap):
        raise ctypes.WinError(ctypes.get_last_error())
    children: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(
                entry.th32ProcessID
            )
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    ordered: list[int] = []
    stack = [root_pid]
    seen = {root_pid}
    while stack:
        current = stack.pop()
        ordered.append(current)
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return ordered


def _terminate_pid_hard(pid: int) -> bool:
    """Terminate one process unconditionally; True when it was signalled."""
    if kernel32 is None:
        return False
    try:
        handle = _open_process(pid, PROCESS_TERMINATE)
    except OSError:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, JOB_EXIT_CODE))
    finally:
        kernel32.CloseHandle(handle)


def _kill_tree_by_pid(pid: int) -> bool:
    """Best-effort tree kill for legacy call sites that only hold a pid.

    Without the job handle the ownership boundary cannot be enforced; this
    enumerates descendants with Toolhelp and terminates them (leaves first).
    New descendants spawned between the snapshot and the kills escape — which
    is exactly why ``spawn_owned()`` exists.
    """
    if kernel32 is None:
        return False
    try:
        ordered = _descendant_pids(pid)
    except OSError:
        ordered = [pid]
    signalled = False
    for target in reversed(ordered):
        signalled = _terminate_pid_hard(target) or signalled
    return signalled


# --- CLI argv resolution ------------------------------------------------------

_QUOTED_TOKEN = re.compile(r'"([^"]+)"')
_NPM_SHIM_EXTENSIONS = (".cmd", ".bat")


def _expand_shim_token(token: str, shim_dir: str) -> str:
    """Expand the batch specials npm shims rely on.

    ``%~dp0`` is the shim's own directory *with* a trailing backslash; npm
    also does ``SET dp0=%~dp0`` and then uses ``%dp0%``.
    """
    out = token
    # Replacement strings are passed via lambda: re.sub treats backslashes in
    # the *replacement* as escapes, and shim dirs are full of them.
    out = re.sub(r"%~dp0", lambda _m: shim_dir + os.sep, out, flags=re.IGNORECASE)
    out = re.sub(r"%dp0%", lambda _m: shim_dir + os.sep, out, flags=re.IGNORECASE)
    return os.path.normpath(out) if (os.sep in out or "/" in out) else out


def _parse_npm_shim(text: str, shim_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Find ``(node_exe, cli_js)`` tokens inside an npm ``.cmd``/``.bat`` shim."""
    node_token: Optional[str] = None
    js_token: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.lstrip("@").strip().upper()
        if upper.startswith(("REM ", "::", "ECHO ")) or upper == "REM":
            continue
        for match in _QUOTED_TOKEN.finditer(line):
            token = match.group(1)
            lower = token.lower()
            if js_token is None and lower.endswith(".js"):
                js_token = token
            if node_token is None and lower.endswith("node.exe"):
                node_token = token
    node_path: Optional[str] = None
    if node_token is not None:
        candidate = _expand_shim_token(node_token, shim_dir)
        if os.path.isfile(candidate):
            node_path = os.path.abspath(candidate)
    if node_path is None:
        node_path = shutil.which("node")
    js_path: Optional[str] = None
    if js_token is not None:
        candidate = _expand_shim_token(js_token, shim_dir)
        if os.path.isfile(candidate):
            js_path = os.path.abspath(candidate)
    return node_path, js_path


def resolve_cli_argv(executable: str) -> list[str]:
    """Resolve a CLI name/path to an exec argv list, bypassing cmd.exe shims.

    PATHEXT-aware resolution goes through :func:`shutil.which`.  When the
    resolved target is an npm ``.cmd``/``.bat`` shim, the shim is parsed and
    the underlying Node CLI is returned as ``[node, cli_js]`` with absolute
    paths (``%~dp0`` is expanded against the shim's directory).  Because the
    result is an argv list, no cmd.exe quoting rules apply and arguments with
    spaces, quotes, ``&``, ``%`` or non-ASCII text survive verbatim.

    If the Node executable or the ``cli.js`` cannot be resolved, falls back to
    ``[resolved_cmd]`` (the original, shim-indirected path) and logs why.
    """
    if not executable:
        return [executable]
    resolved = shutil.which(executable) if _IS_WINDOWS else None
    if resolved is None:
        # Already a direct path, or non-Windows: nothing to de-shim.
        return [executable]
    if not resolved.lower().endswith(_NPM_SHIM_EXTENSIONS):
        return [resolved]
    shim_dir = os.path.dirname(resolved)
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        logger.info(
            "winproc: cannot read npm shim %r (%s); using it directly",
            resolved,
            exc,
        )
        return [resolved]
    node_path, js_path = _parse_npm_shim(text, shim_dir)
    if node_path and js_path:
        return [node_path, js_path]
    logger.info(
        "winproc: npm shim %r did not yield node+cli.js (node=%r js=%r); "
        "falling back to the shim itself",
        resolved,
        node_path,
        js_path,
    )
    return [resolved]


# --- Owned process ------------------------------------------------------------


class _OwnedReader:
    """Duck-types ``asyncio.StreamReader`` over the bridge queues.

    ``readline()`` streams lines; ``read()`` returns everything through EOF
    (the ``n`` argument is accepted for signature compatibility and only -1
    semantics are provided).
    """

    def __init__(self, line_reader: Any, all_reader: Any) -> None:
        self._line_reader = line_reader
        self._all_reader = all_reader

    def __bool__(self) -> bool:
        return True

    async def readline(self) -> Optional[bytes]:
        return await self._line_reader()

    async def read(self, n: int = -1) -> bytes:
        del n  # only read-to-EOF semantics are offered
        return await self._all_reader()


class _OwnedWriter:
    """Duck-types ``asyncio.StreamWriter``: write()+flush() eagerly, no-op drain."""

    def __init__(self, owner: "OwnedProc") -> None:
        self._owner = owner

    def __bool__(self) -> bool:
        return not (self._owner._stdin_broken or self._owner._closed)

    def write(self, data: bytes) -> None:
        self._owner.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._owner.close_stdin()

    async def wait_closed(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return self._owner._stdin_broken or self._owner._closed


class OwnedProc:
    """A spawned process owned by a kill-on-close Windows Job Object.

    Shape mirrors what the ``agent_server.py`` async run loops need from
    ``asyncio.subprocess.Process``: ``pid``, ``returncode``, ``await
    readline()``, ``await read_stderr()``, ``write(bytes)``, ``await wait()``,
    plus tree-aware ``terminate_tree()``, ``kill()`` and resource cleanup via
    ``close()``.
    """

    def __init__(
        self,
        popen: subprocess.Popen[bytes],
        job: Optional[int],
        argv: list[str],
    ) -> None:
        self._proc = popen
        self._job = job
        self.argv = list(argv)
        self._terminated = False
        self._closed = False
        self._stdin_broken = False
        self._line_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._err_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._async_line_q: Optional[asyncio.Queue] = None
        self._async_err_q: Optional[asyncio.Queue] = None
        self._stderr_result: Optional[bytes] = None
        self._stdout_result: Optional[bytes] = None
        self._bridge_lock = threading.Lock()
        self._pump_threads: list[threading.Thread] = []
        self._stdout_reader: Optional["_OwnedReader"] = None
        self._stderr_reader: Optional["_OwnedReader"] = None
        self._stdin_writer: Optional["_OwnedWriter"] = None
        self._start_pumps()

    # -- asyncio.subprocess.Process-compatible stream facades -------------------
    # The agent_server run loops (and codex_app_server's JSON-RPC stdio) were
    # written against asyncio.Process streams.  These facades duck-type that
    # surface — proc.stdout.readline(), proc.stderr.read()/readline(),
    # proc.stdin.write()/drain()/close() — so the same loop bodies run
    # unmodified over the thread-bridged pipes.
    @property
    def stdout(self) -> Optional["_OwnedReader"]:
        if self._proc.stdout is None:
            return None
        if self._stdout_reader is None:
            self._stdout_reader = _OwnedReader(self.readline, self._read_stdout_all)
        return self._stdout_reader

    @property
    def stderr(self) -> Optional["_OwnedReader"]:
        if self._proc.stderr is None:
            return None
        if self._stderr_reader is None:
            self._stderr_reader = _OwnedReader(self._read_stderr_line, self.read_stderr)
        return self._stderr_reader

    @property
    def stdin(self) -> Optional["_OwnedWriter"]:
        if self._proc.stdin is None:
            return None
        if self._stdin_writer is None:
            self._stdin_writer = _OwnedWriter(self)
        return self._stdin_writer

    async def _read_stdout_all(self) -> bytes:
        if self._stdout_result is not None:
            return self._stdout_result
        q = self._ensure_line_bridge()
        chunks: list[bytes] = []
        while True:
            item = await q.get()
            if item is None:
                break
            chunks.append(item)
        self._stdout_result = b"".join(chunks)
        return self._stdout_result

    async def _read_stderr_line(self) -> Optional[bytes]:
        q = self._ensure_err_bridge()
        item = await q.get()
        if item is None:
            return None
        return item

    # -- basics ---------------------------------------------------------------
    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    @property
    def popen(self) -> subprocess.Popen[bytes]:
        """The underlying ``subprocess.Popen`` (integration escape hatch)."""
        return self._proc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = self._proc.returncode
        return f"<OwnedProc pid={self.pid} returncode={state} job={self._job!r}>"

    # -- pump threads ----------------------------------------------------------
    def _start_pumps(self) -> None:
        if self._proc.stdout is not None:
            self._spawn_pump(self._pump_lines, self._proc.stdout, self._line_q)
        else:
            self._line_q.put(None)
        if self._proc.stderr is not None:
            # stderr is pumped line-by-line too: consumers use both
            # read_stderr() (whole stream) and stderr.readline() (JSON-RPC
            # stdio tails) — line mode satisfies both.
            self._spawn_pump(self._pump_lines, self._proc.stderr, self._err_q)
        else:
            self._err_q.put(None)

    def _spawn_pump(self, target: Any, *args: Any) -> None:
        thread = threading.Thread(
            target=target,
            args=args,
            name=f"winproc-pump-{self.pid}-{len(self._pump_threads)}",
            daemon=True,
        )
        self._pump_threads.append(thread)
        thread.start()

    @staticmethod
    def _pump_lines(stream: Any, q: "queue.Queue[Optional[bytes]]") -> None:
        # readline() blocks until a FULL newline-terminated line (or EOF), so
        # chunk boundaries in the pipe never leak into readline() results.
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    @staticmethod
    def _bridge_thread(
        loop: asyncio.AbstractEventLoop,
        src: "queue.Queue[Optional[bytes]]",
        dst: asyncio.Queue,
    ) -> None:
        # Dedicated daemon bridge: blocks on the thread-side queue, then
        # schedules the item onto the asyncio queue from the loop's own
        # thread.  Cancellation of the asyncio consumer therefore never
        # strands a worker (run_in_executor would), and no loop.add_reader
        # is needed (unsupported on the Windows ProactorEventLoop).
        while True:
            item = src.get()
            try:
                loop.call_soon_threadsafe(dst.put_nowait, item)
            except RuntimeError:
                return  # consumer loop is closed; nothing left to feed
            if item is None:
                return

    def _ensure_line_bridge(self) -> asyncio.Queue:
        with self._bridge_lock:
            if self._async_line_q is None:
                self._async_line_q = asyncio.Queue()
                self._spawn_pump(
                    self._bridge_thread,
                    asyncio.get_running_loop(),
                    self._line_q,
                    self._async_line_q,
                )
            return self._async_line_q

    def _ensure_err_bridge(self) -> asyncio.Queue:
        with self._bridge_lock:
            if self._async_err_q is None:
                self._async_err_q = asyncio.Queue()
                self._spawn_pump(
                    self._bridge_thread,
                    asyncio.get_running_loop(),
                    self._err_q,
                    self._async_err_q,
                )
            return self._async_err_q

    # -- async I/O surface -----------------------------------------------------
    async def readline(self) -> Optional[bytes]:
        """Next newline-terminated stdout line (newline included); None at EOF.

        Lines are reassembled across arbitrary pipe chunk boundaries by the
        pump thread's blocking ``readline()``; the dedicated bridge thread
        forwards complete lines to the asyncio queue.
        """
        q = self._ensure_line_bridge()
        item = await q.get()
        if item is None:
            return None
        return item

    async def read_stderr(self) -> bytes:
        """All remaining stderr output (drained continuously until EOF).

        Idempotent: after EOF the buffered result is returned to every caller.
        """
        if self._stderr_result is not None:
            return self._stderr_result
        q = self._ensure_err_bridge()
        chunks: list[bytes] = []
        while True:
            item = await q.get()
            if item is None:
                break
            chunks.append(item)
        self._stderr_result = b"".join(chunks)
        return self._stderr_result

    # -- stdin -----------------------------------------------------------------
    def write(self, data: bytes) -> None:
        """Write bytes to the child's stdin, raising BrokenPipeError if closed."""
        if self._closed or self._stdin_broken or self._proc.stdin is None:
            raise BrokenPipeError(errno.EPIPE, "stdin is closed")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except BrokenPipeError:
            self._stdin_broken = True
            raise
        except OSError as exc:
            self._stdin_broken = True
            raise BrokenPipeError(errno.EPIPE, str(exc)) from exc

    def close_stdin(self) -> None:
        self._stdin_broken = True
        if self._proc.stdin is not None:
            with suppress(Exception):
                self._proc.stdin.close()

    # -- lifecycle --------------------------------------------------------------
    async def wait(self) -> int:
        """Wait for process exit; returns the exit code."""
        loop = asyncio.get_running_loop()
        code = await loop.run_in_executor(None, self._proc.wait)
        return int(code)

    async def terminate_tree(self, grace: float = DEFAULT_TERMINATE_GRACE) -> bool:
        """Graceful-stop, then kill the WHOLE job tree.

        Windows has no SIGTERM, so the graceful step posts CTRL_BREAK_EVENT to
        the child's own process group (console-attached CLI agents such as
        Node CLIs handle it like SIGINT).  After *grace* seconds — or
        immediately when the console event cannot be delivered — the job is
        terminated, killing every member (including grandchildren) at once.
        Returns True when a stop signal was delivered.
        """
        if self._proc.returncode is not None:
            return False
        delivered = False
        if not self._terminated and _try_generate_ctrl_break(self.pid):
            delivered = True
            if grace > 0:
                try:
                    await asyncio.wait_for(self.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    pass
        # Whether the root exited politely within the grace window or not,
        # the rest of the job tree is terminated now — the analogue of
        # killpg(SIGKILL) reaching every process the agent spawned (console
        # ctrl events can only target one process group, so grandchildren
        # could not be asked to stop politely).
        self._terminated = True
        if self._job is not None:
            with suppress(OSError):
                _terminate_job(self._job, JOB_EXIT_CODE)
        else:
            with suppress(OSError):
                self._proc.kill()
        try:
            await asyncio.wait_for(self.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            logger.warning(
                "winproc: pid %d did not exit within 10s of job termination",
                self.pid,
            )
        return True

    def kill(self) -> None:
        """Hard-kill the whole job tree immediately (no wait)."""
        self._terminated = True
        if self._job is not None:
            with suppress(OSError):
                _terminate_job(self._job, JOB_EXIT_CODE)
        else:
            with suppress(OSError):
                self._proc.kill()

    def close(self) -> None:
        """Release resources. Normal close does NOT reap the job tree.

        Upstream AgentsDock never kills the process group when a turn ends
        normally, so tool-launched background jobs survive turn boundaries
        (POSIX: nohup/setsid equivalents). A grandchild that inherited our
        pipes can withhold EOF, so streams are closed only after their pump
        thread has exited — ``BufferedReader.close()`` blocks on the lock a
        pump holds while parked in ``readline()``, which would deadlock
        close() while a surviving background job keeps the pipe open. Pumps
        are daemon threads: skipped streams are reclaimed with the Popen
        object when the surviving writers eventually close the pipe.
        """
        if self._closed:
            return
        self._closed = True
        self._stdin_broken = True
        if self._proc.stdin is not None:
            with suppress(Exception):
                self._proc.stdin.close()
        if self._job is not None and kernel32 is not None:
            handle, self._job = self._job, None
            with suppress(OSError):
                kernel32.CloseHandle(handle)
        elif self._job is None and not _IS_WINDOWS and self._proc.returncode is None:
            with suppress(Exception):
                os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        if self._proc.returncode is None:
            # Natural exit or TerminateJobObject ended the tree; reap so
            # Popen.__del__ never warns about an un-waited, running child.
            with suppress(Exception):
                self._proc.wait(timeout=5)
        deadline = time.monotonic() + 5.0
        for thread in self._pump_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        # Closing a stream whose pump is still parked in readline() would
        # block on the buffer lock forever (a surviving background job can
        # hold the pipe write end), so only close streams with exited pumps.
        if not any(thread.is_alive() for thread in self._pump_threads):
            for stream in (self._proc.stdout, self._proc.stderr):
                if stream is None:
                    continue
                with suppress(Exception):
                    stream.close()

    async def __aenter__(self) -> "OwnedProc":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - GC safety net
        with suppress(Exception):
            self.close()


# --- Spawning ------------------------------------------------------------------


def _spawn_job_owned(
    argv: list[str],
    cwd: Optional[str],
    env: Optional[dict],
    stdin_mode: str,
) -> tuple[subprocess.Popen[bytes], int]:
    """Create a suspended process, own it in a kill-on-close job, then resume.

    The child is created with CREATE_SUSPENDED, so between process creation
    and AssignProcessToJobObject it can neither execute code nor spawn
    children: the launch/assignment race is closed by construction.  Any
    failure before resume terminates the suspended child and releases all
    handles, raising WinSpawnError.
    """
    stdin = subprocess.PIPE if stdin_mode == "pipe" else subprocess.DEVNULL
    creationflags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    try:
        popen = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise WinSpawnError(f"cannot spawn {argv[0]!r}: {exc}") from exc
    except OSError as exc:
        raise WinSpawnError(f"cannot spawn {argv[0]!r}: {exc}") from exc

    job: Optional[int] = None
    try:
        job = _create_owned_job()
        process_handle = _open_process(
            popen.pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE
        )
        try:
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process_handle)
        _resume_process_threads(popen.pid)
    except WinSpawnError:
        raise
    except BaseException as exc:
        # The child is still suspended (or partially resumed): kill it, then
        # release every handle so nothing leaks.
        with suppress(Exception):
            popen.kill()
        with suppress(Exception):
            popen.wait(timeout=5)
        for stream in (popen.stdin, popen.stdout, popen.stderr):
            if stream is not None:
                with suppress(Exception):
                    stream.close()
        if job is not None:
            with suppress(OSError):
                kernel32.CloseHandle(job)
        raise WinSpawnError(
            f"failed to take job ownership of pid {popen.pid}: {exc}"
        ) from exc
    return popen, job


def _spawn_posix_fallback(
    argv: list[str],
    cwd: Optional[str],
    env: Optional[dict],
    stdin_mode: str,
) -> tuple[subprocess.Popen[bytes], None]:
    stdin = subprocess.PIPE if stdin_mode == "pipe" else subprocess.DEVNULL
    try:
        popen = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise WinSpawnError(f"cannot spawn {argv[0]!r}: {exc}") from exc
    except OSError as exc:
        raise WinSpawnError(f"cannot spawn {argv[0]!r}: {exc}") from exc
    return popen, None


def spawn_owned(
    argv: list[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    stdin_mode: str = "pipe",
) -> OwnedProc:
    """Spawn *argv* as a job-owned process tree and return its :class:`OwnedProc`.

    Raises :class:`WinSpawnError` on launch failure or when job ownership
    cannot be established (nothing is left running or leaking in that case).
    On non-Windows hosts a POSIX ``start_new_session`` fallback is used so the
    API stays usable; check :data:`SUPPORTED` for real job semantics.
    """
    argv = [str(a) for a in argv]
    if not argv:
        raise WinSpawnError("empty argv")
    if stdin_mode not in ("pipe", "devnull"):
        raise WinSpawnError(f"invalid stdin_mode {stdin_mode!r}")
    if _IS_WINDOWS:
        if not SUPPORTED:
            raise WinSpawnError(
                "kernel32 job-object API unavailable on this Windows host"
            )
        popen, job = _spawn_job_owned(argv, cwd, env, stdin_mode)
        return OwnedProc(popen, job, argv)
    popen, job = _spawn_posix_fallback(argv, cwd, env, stdin_mode)
    return OwnedProc(popen, job, argv)


def terminate_tree_for(target: Any, grace: float = 0.0) -> bool:
    """Terminate a process tree for legacy call sites holding only pid/Popen.

    Accepts an :class:`OwnedProc`, a ``Popen``/``asyncio.Process``-like object
    with ``.pid``, or a raw int pid.  For :class:`OwnedProc` the job handle is
    used (full tree kill).  For bare pids the tree is enumerated best-effort
    with Toolhelp and each member is terminated (see :func:`_kill_tree_by_pid`
    for the residual race).  On POSIX the fallback is ``killpg`` with SIGTERM
    then SIGKILL when *grace* elapses.  Returns True when a stop was signalled.
    """
    if isinstance(target, OwnedProc):
        if target._job is not None:
            target._terminated = True
            with suppress(OSError):
                _terminate_job(target._job, JOB_EXIT_CODE)
                return True
        with suppress(OSError):
            target._proc.kill()
            return True
        return False
    pid: Optional[int]
    if isinstance(target, int):
        pid = target
    else:
        pid = getattr(target, "pid", None)
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        if kernel32 is None:
            return False
        return _kill_tree_by_pid(pid)
    # POSIX fallback for symmetry with agent_server's killpg call sites.
    try:
        pgid = os.getpgid(pid)
        if pgid == os.getpgrp():
            return False
        os.killpg(pgid, signal.SIGTERM)
        if grace <= 0:
            return True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with suppress(ProcessLookupError):
                os.kill(pgid, 0)
                time.sleep(0.05)
                continue
            return True
        os.killpg(pgid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


# --- Process rows ---------------------------------------------------------------


def process_rows(limit: int = 20) -> list[dict[str, Any]]:
    """Top-N process rows matching ``agent_server.ps_process_rows()`` shape.

    Keys per row: ``pid``, ``ppid``, ``pgid``, ``sid``, ``stat``,
    ``elapsed_seconds``, ``cpu_percent``, ``mem_percent``, ``rss_kb``,
    ``command``, ``args`` — with ``pgid``/``sid``/``stat`` set to None because
    Windows has no honest POSIX equivalents (it has sessions, but not of the
    tty kind the UI renders).  Sorted by ``cpu_percent`` descending; ``limit``
    caps the row count (values <= 0 mean no cap).  Returns ``[]`` rather than
    ever raising.
    """
    try:
        import psutil

        now = time.time()
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter():
            try:
                with proc.oneshot():
                    cpu = float(proc.cpu_percent(interval=None))
                    rss = int(proc.memory_info().rss) // 1024
                    create_time = float(proc.create_time())
                    comm = str(proc.name())
                    cmdline = proc.cmdline()
                    ppid = int(proc.ppid())
                    try:
                        mem_percent = float(proc.memory_percent())
                    except Exception:
                        mem_percent = 0.0
            except Exception:
                continue
            args = " ".join(cmdline) if cmdline else comm
            rows.append(
                {
                    "pid": int(proc.pid),
                    "ppid": ppid,
                    "pgid": None,
                    "sid": None,
                    "stat": None,
                    "elapsed_seconds": int(max(0.0, now - create_time)),
                    "cpu_percent": cpu,
                    "mem_percent": mem_percent,
                    "rss_kb": rss,
                    "command": comm,
                    "args": args,
                }
            )
        rows.sort(key=lambda row: row["cpu_percent"], reverse=True)
        if limit and int(limit) > 0:
            return rows[: int(limit)]
        return rows
    except Exception:
        return []
