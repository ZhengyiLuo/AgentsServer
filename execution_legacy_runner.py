"""Read-only ownership proof for old Darwin updaters without runner_pid.

This is an additional installer admission proof, never an alternative to the
caller's authenticated native listener, idle, cgroup and installation-lock
checks. It does not repair status, launch jobs, or stop any process.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
from typing import Any

MAX_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_ANCESTORS = 24
ACTIVE_PHASES = {"starting", "checking", "downloading", "verifying", "installing", "restarting"}


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    ruid: int
    started: tuple[int, int]
    executable: str
    argv: tuple[str, ...]


class _BSDInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h: proc_bsdinfo, PROC_PIDTBSDINFO=3.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


def _arguments(raw: bytes) -> tuple[str, ...]:
    """Parse exactly argc strings from KERN_PROCARGS2; never parse environment."""
    if not 5 <= len(raw) <= MAX_ARGUMENT_BYTES:
        raise RuntimeError("legacy process argument buffer is invalid")
    count = struct.unpack_from("=i", raw)[0]
    if not 1 <= count <= 512:
        raise RuntimeError("legacy process argument count is invalid")
    end = raw.find(b"\0", 4)
    if end < 5:
        raise RuntimeError("legacy process executable argument is missing")
    cursor = end + 1
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1
    result = []
    for _ in range(count):
        end = raw.find(b"\0", cursor)
        if end < 0:
            raise RuntimeError("legacy process arguments are truncated")
        value = raw[cursor:end].decode("utf-8", "strict")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise RuntimeError("legacy process argument contains control characters")
        result.append(value)
        cursor = end + 1
    return tuple(result)


def _process(pid: int) -> Process:
    if sys.platform != "darwin" or type(pid) is not int or pid <= 1:
        raise RuntimeError("legacy runner proof requires a live Darwin process")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    def info():
        value = _BSDInfo()
        if library.proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value)) != ctypes.sizeof(value):
            raise RuntimeError("legacy process identity is unavailable")
        if (value.pid != pid or value.uid != os.getuid() or value.ruid != os.getuid() or value.svuid != os.getuid()
                or value.status == 5 or not value.start_sec):  # SZOMB
            raise RuntimeError("legacy process owner or incarnation is invalid")
        return value
    first = info()
    path = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, path, len(path)) <= 0:
        raise RuntimeError("legacy process executable is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                           ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 0 < size.value <= MAX_ARGUMENT_BYTES:
        raise RuntimeError("legacy process arguments are unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise RuntimeError("legacy process arguments changed while reading")
    argv = _arguments(buffer.raw[:size.value])
    last = info()
    identity = lambda x: (x.pid, x.ppid, x.uid, x.ruid, x.start_sec, x.start_usec)
    if identity(first) != identity(last):
        raise RuntimeError("legacy process incarnation changed while reading")
    return Process(pid, first.ppid, first.uid, first.ruid,
                   (first.start_sec, first.start_usec), os.fsdecode(path.value), argv)


def _chain(start: int) -> tuple[Process, ...]:
    result = []
    seen = set()
    for _ in range(MAX_ANCESTORS):
        if start <= 1:
            break
        if start in seen:
            raise RuntimeError("legacy process ancestry contains a cycle")
        seen.add(start)
        item = _process(start)
        result.append(item)
        start = item.ppid
    return tuple(result)


def _directory(path: Path, *, private=False) -> tuple:
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise RuntimeError("legacy proof directory must be canonical")
    value = path.lstat()
    if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o022 or (private and stat.S_IMODE(value.st_mode) != 0o700)):
        raise RuntimeError("legacy proof directory is not owned and safe")
    return value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode)


def _read(path: Path, *, private=False) -> tuple[bytes, tuple]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        def identity(s):
            return (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink,
                    s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o022
                or (private and stat.S_IMODE(before.st_mode) != 0o600)
                or before.st_size > 16 * 1024 * 1024):
            raise RuntimeError("legacy proof file is not owned and safe")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(fd))
                or identity(before) != identity(path.lstat())):
            raise RuntimeError("legacy proof file changed while reading")
        return raw, (*identity(before), hashlib.sha256(raw).hexdigest())
    finally:
        os.close(fd)


def _json(path: Path, *, with_binding=False):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate legacy proof field")
            value[key] = item
        return value
    raw, binding = _read(path, private=True)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("legacy proof JSON exceeds limit")
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise RuntimeError("legacy proof JSON is invalid")
    return (value, binding) if with_binding else value


def _options(argv: tuple[str, ...]) -> dict[str, str]:
    if len(argv) % 2:
        raise RuntimeError("legacy runner options are malformed")
    values = {}
    for key, value in zip(argv[::2], argv[1::2]):
        if not key.startswith("--") or "=" in key or key in values:
            raise RuntimeError("legacy runner options are ambiguous")
        values[key] = value
    return values


def _python_proof(interpreter: Path) -> tuple[tuple[Path, ...], tuple]:
    """Bind a Python executable and only its exact macOS framework companion.

    CPython's framework bin/pythonX.Y launcher re-execs the same version's
    Python.app executable and replaces argv[0]. This is not a generic alias
    rule: both binaries and every directory inside that framework are pinned.
    """
    resolved = interpreter.resolve(strict=True)
    executables = [resolved]
    directories = []
    version = re.fullmatch(r"python(\d+\.\d+)", resolved.name)
    if (version and resolved.parent.name == "bin"
            and resolved.parent.parent.name == version[1]
            and resolved.parents[2].name == "Versions"
            and resolved.parents[3].name == "Python.framework"):
        framework = resolved.parents[3]
        companion = resolved.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
        if companion.resolve(strict=True) != companion:
            raise RuntimeError("legacy Python framework companion is not canonical")
        executables.append(companion)
        for executable in executables:
            directory = executable.parent
            while directory != framework.parent:
                if directory not in directories:
                    directories.append(directory)
                directory = directory.parent
    bindings = []
    for path in (*executables, *directories):
        info = path.lstat()
        is_file = path in executables
        if (info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022
                or (is_file and (not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK)))
                or (not is_file and not stat.S_ISDIR(info.st_mode))):
            raise RuntimeError("legacy Python executable or framework directory is unsafe")
        bindings.append((str(path), info.st_dev, info.st_ino, info.st_uid,
                         info.st_mode, info.st_nlink, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns))
    return tuple(executables), tuple(bindings)


def _tmux(session: str) -> tuple:
    # Old server bootstrap deliberately drops TMUX/TMUX_TMPDIR: its namespace
    # is the default socket. -S prevents a caller environment selecting another.
    socket = Path("/tmp").resolve() / f"tmux-{os.getuid()}" / "default"
    binary = shutil.which("tmux")
    if not binary:
        raise RuntimeError("legacy tmux executable is unavailable")
    return _tmux_at(session, socket, Path(binary).resolve(strict=True))


def _launch_arguments(display: str) -> tuple[str, ...]:
    """Decode tmux's display of one shell command, without evaluating it.

    Older tmux exposes the command text directly. Newer tmux quotes the single
    command argument passed to new-session, adding one serialization layer.
    The caller still compares every decoded argument to the kernel snapshot.
    """
    if any(ord(character) < 32 or ord(character) == 127 for character in display):
        raise RuntimeError("legacy tmux launch display contains control characters")
    arguments = tuple(shlex.split(display))
    if len(arguments) == 1:
        arguments = tuple(shlex.split(arguments[0]))
    if len(arguments) < 2:
        raise RuntimeError("legacy tmux launch display is missing arguments or nested")
    return arguments


def _tmux_at(session: str, socket: Path, executable: Path) -> tuple:
    """Read one pane; production caller always supplies the default socket."""
    _directory(socket.parent, private=True)
    socket_info = socket.lstat()
    if (not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid()
            or socket_info.st_mode & 0o007 or socket_info.st_nlink != 1):
        raise RuntimeError("legacy tmux socket is not owned")
    binary = str(executable)
    info = Path(binary).stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022 or not os.access(binary, os.X_OK)):
        raise RuntimeError("legacy tmux executable is unsafe")
    fmt = "#{session_name}|#{session_id}|#{pane_id}|#{pane_pid}|#{pane_dead}|#{pid}|#{pane_start_command}"
    result = subprocess.run([binary, "-S", str(socket), "list-panes", "-s", "-t", "=" + session, "-F", fmt],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, check=False, env={"PATH":"/usr/bin:/bin", "HOME":str(Path.home()), "LC_ALL":"C"})
    if result.returncode or len(result.stdout) > 64 * 1024:
        raise RuntimeError("legacy updater tmux pane is unavailable")
    rows = result.stdout.decode("utf-8", "strict").splitlines()
    if len(rows) != 1:
        raise RuntimeError("legacy updater tmux session must have one pane")
    fields = rows[0].split("|", 6)
    if (len(fields) != 7 or fields[0] != session or not re.fullmatch(r"\$\d+", fields[1])
            or not re.fullmatch(r"%\d+", fields[2]) or not fields[3].isdigit()
            or fields[4] != "0" or not fields[5].isdigit()):
        raise RuntimeError("legacy updater tmux pane identity is invalid")
    server = _process(int(fields[5]))
    if Path(server.executable).resolve() != Path(binary):
        raise RuntimeError("legacy tmux process executable changed")
    current = socket.lstat()
    binding = (socket_info.st_dev, socket_info.st_ino, socket_info.st_uid, socket_info.st_mode)
    if binding != (current.st_dev, current.st_ino, current.st_uid, current.st_mode):
        raise RuntimeError("legacy tmux socket changed")
    return (*fields[:3], int(fields[3]), server, _launch_arguments(fields[6]), binding)


def verify_legacy_runner(args: Any, status: dict[str, Any]) -> str:
    """Prove only an absent-PID Darwin legacy owner, without mutating anything."""
    if (sys.platform != "darwin" or args.platform != "Darwin" or args.managed_update_id
            or "runner_pid" in status):
        raise RuntimeError("legacy ancestry fallback does not apply")
    identifier = status.get("update_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise RuntimeError("legacy update identity is invalid")
    root, state = Path(args.root), Path(args.state_root)
    roots = {path:_directory(path) for path in (root, root / "releases", state, state / "admin")}
    current = root / "current"
    link_info = current.lstat()
    if not stat.S_ISLNK(link_info.st_mode) or link_info.st_uid != os.getuid():
        raise RuntimeError("legacy current release is not an owned link")
    link_target = os.readlink(current)
    release = current.resolve(strict=True)
    if release.parent != root / "releases" or release.name.startswith("."):
        raise RuntimeError("legacy updater must belong to the retained installed release")
    roots[release] = _directory(release)
    files = {name:_read(release / name) for name in (
        "VERSION", "update_runner.py", "agent_server.py", "release-public-key.pem")}
    old_version = files["VERSION"][0].decode().strip()
    interpreter = release / ".venv/bin/python"
    python_paths, python_binding = _python_proof(interpreter)
    stable_info = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    link_binding = stable_info(link_info)
    status_file = state / "admin/server-update.json"
    health_path, update_path = Path(args.health_file), Path(args.update_file)
    for path in (health_path, update_path):
        if path.parent != state / "admin" or path.resolve() != path:
            raise RuntimeError("legacy authenticated proof is outside private admin directory")
    health, health_binding = _json(health_path, with_binding=True)
    admitted, admitted_binding = _json(update_path, with_binding=True)
    def status_pins(value):
        if ("runner_pid" in value or value.get("update_id") != identifier
                or value.get("phase") not in ACTIVE_PHASES
                or value.get("target_version") != args.release_version
                or value.get("track") not in {"stable", "beta"}):
            raise RuntimeError("legacy admitted update changed")
        return value["update_id"], value["target_version"], value["track"]
    pins = status_pins(status)
    if status_pins(_json(status_file)) != pins or status_pins(admitted) != pins:
        raise RuntimeError("legacy authenticated update does not match durable status")
    if (health.get("server_identity") != args.expected_server_identity
            or admitted.get("server_identity") != args.expected_server_identity
            or not health.get("server_instance_id")
            or admitted.get("server_instance_id") != health["server_instance_id"]
            or health.get("server_version") != old_version
            or type(health.get("active_count")) is not int or health["active_count"] != 0
            or health.get("update_blocking_queued_count", sum(health.get("queued", {}).values())) != 0
            or health.get("update_service_cgroup", {}).get("safe") is not True
            or health.get("update_service_cgroup", {}).get("unknown_descendant_count") != 0):
        raise RuntimeError("legacy authenticated idle health does not match")
    native = _process(args.expected_native_pid)
    native_executable = Path(native.executable).resolve()
    def python_process(process: Process, script: str):
        return (len(process.argv) >= 2 and all(Path(v).is_absolute() for v in (process.executable, *process.argv[:2]))
                and native_executable in python_paths
                and Path(process.executable).resolve() == native_executable
                and Path(process.argv[0]).resolve() in python_paths
                and Path(process.argv[1]).resolve() == release / script)
    if not python_process(native, "agent_server.py"):
        raise RuntimeError("legacy native process belongs to another installed release")
    native_options = _options(native.argv[3:])
    if native.argv[2:3] != ("serve",) or native_options != {"--bind":args.bind, "--port":str(args.port)}:
        raise RuntimeError("legacy native listener arguments differ")
    expected = {"--status-file":str(status_file), "--public-key":str(release / "release-public-key.pem"),
        "--port":str(args.port), "--bind":args.bind, "--expected-version":args.release_version,
        "--current-version":old_version, "--track":pins[2], "--update-id":identifier,
        "--expected-server-identity":args.expected_server_identity}
    allowed = set(expected) | {"--auth-token-file", "--expected-team-hub-id", "--expected-team-hub-transport",
        "--expected-team-hub-url", "--expected-team-hub-direct-ip-url", "--team-hub-snapshot", "--team-hub-data-dir"}
    chain = _chain(os.getpid())
    runners = [process for process in chain[1:] if python_process(process, "update_runner.py")]
    if len(runners) != 1:
        raise RuntimeError("legacy updater ancestor is not unique")
    runner = runners[0]
    options = _options(runner.argv[2:])
    if set(options) - allowed or not set(expected) <= set(options):
        raise RuntimeError("legacy updater options differ from the installed protocol")
    for key, value in expected.items():
        observed = options[key]
        if key in {"--status-file", "--public-key"}:
            observed = str(Path(observed).resolve(strict=True))
        if observed != value:
            raise RuntimeError("legacy updater arguments do not match admitted update")
    if options.get("--auth-token-file") not in (None, str(state / "admin" / f".server-update-{identifier}.auth.json")):
        raise RuntimeError("legacy updater credential handoff path differs")
    pane = _tmux("agents_server_update_" + identifier)
    pane_pid, tmux_server, launch = pane[3:6]
    if (not launch or launch[1:] != runner.argv[1:] or not Path(launch[0]).is_absolute()
            or Path(launch[0]).resolve() not in python_paths):
        raise RuntimeError("legacy updater launch command does not match kernel arguments")
    pids = [process.pid for process in chain]
    if pane_pid not in pids or tmux_server.pid not in pids or not pids.index(runner.pid) <= pids.index(pane_pid) < pids.index(tmux_server.pid):
        raise RuntimeError("legacy updater is outside its admitted tmux pane")
    # Repeat every mutable authority binding; no cached proof authorizes stop.
    if (_chain(os.getpid()) != chain or _process(args.expected_native_pid) != native
            or _tmux(pane[0]) != pane or status_pins(_json(status_file)) != pins
            or _json(health_path, with_binding=True) != (health, health_binding)
            or _json(update_path, with_binding=True) != (admitted, admitted_binding)):
        raise RuntimeError("legacy updater authority changed during verification")
    if any(_directory(path) != binding for path,binding in roots.items()):
        raise RuntimeError("legacy release directory changed during verification")
    if (stable_info(current.lstat()) != link_binding or os.readlink(current) != link_target
            or current.resolve() != release or _python_proof(interpreter) != (python_paths, python_binding)
            or any(_read(release / name) != value for name,value in files.items())):
        raise RuntimeError("legacy installed runtime changed during verification")
    return identifier
