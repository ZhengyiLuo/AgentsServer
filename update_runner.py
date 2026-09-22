#!/usr/bin/env python3
"""Download, verify, and atomically install an AgentsServer release."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


RELEASE_REPOSITORY = "ZhengyiLuo/AgentsServer"
RELEASE_BASE = f"https://github.com/{RELEASE_REPOSITORY}/releases"
RELEASES_API_URL = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases?per_page=100"
MAX_METADATA_BYTES = 1_000_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
SERVER_IDLE_CHECK_TIMEOUT_SECONDS = 10.0
SERVER_STARTUP_READINESS_TIMEOUT_SECONDS = 45.0
SERVER_STARTUP_READINESS_POLL_SECONDS = 1.0
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
# The standalone installer allows dependency synchronization to run for up to
# 1,200 seconds. Keep this enclosing budget comfortably above that so the
# updater cannot terminate a healthy installer first.
INSTALLER_TIMEOUT_SECONDS = 1_800
INSTALLER_HEARTBEAT_SECONDS = 10.0
# After install.sh starts there is no trustworthy out-of-process proof that it
# has not activated a candidate. Its TERM handler may therefore be stopping the
# candidate, restoring an arbitrarily large verified Team Hub snapshot, and
# restarting the old release. Never put a finite SIGKILL deadline around that
# recovery transaction. Polling keeps the owning update observable while the
# installer's signal-masked rollback reaches a safe terminal state.
INSTALLER_TERMINATION_POLL_SECONDS = 10.0
INSTALLER_LOG_TAIL_BYTES = 64 * 1024
INSTALLER_LOG_TAIL_LINES = 12
INSTALLER_ERROR_MAX_CHARS = 4_000
INSTALLER_LOG_MAX_BYTES = 1024 * 1024
INSTALLER_ENVIRONMENT_SELECTORS = (
    "AGENTSDOCK_AGENT_TOKEN",
    "AGENTSDOCK_EXPECTED_SERVICE_CGROUP",
    "AGENTSDOCK_MANAGED_UPDATE_ID",
    "AGENTSDOCK_PROVIDER_AUTHORITY_FILE",
    "AGENTSDOCK_PUBLISH_TOKEN",
    "CONDA_PREFIX",
    "PYTHONHOME",
    "PYTHONPATH",
    "UV_CONFIG_FILE",
    "UV_NO_PROJECT",
    "UV_PROJECT",
    "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON",
    "UV_WORKING_DIR",
    "VIRTUAL_ENV",
    "ZENITHBOT_AGENT_TOKEN",
    "ZENITHDOCK_AGENT_TOKEN",
)
RELEASE_TRACKS = {"stable", "beta"}
NPM_PACKAGE_NAME = "@agentsdock/server"
NPM_REGISTRY_BASE = "https://registry.npmjs.org/@agentsdock/server/-/"
MAX_NPM_MANIFEST_BYTES = 8_192
RUNNER_OWNED_ACTIVE_PHASES = {
    "starting",
    "checking",
    "downloading",
    "verifying",
    "installing",
    "restarting",
}
SECURE_PEER_HEALTH_REQUIREMENTS = {
    "available": True,
    "state_available": True,
    "state_error_code": None,
    "required": False,
    "version": 1,
    "control_path": "/api/admin/secure-peers/v1/status",
    "proxy_prefix": "/api/team-hub-secure",
}


class ReleaseUnavailableError(RuntimeError):
    """Raised when the repository has not published a signed release yet."""


class ReleaseRateLimitedError(RuntimeError):
    """The release host asked us to wait before requesting more metadata."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("GitHub temporarily limited update requests. Try again shortly.")


def release_rate_limit_delay(error: HTTPError) -> int | None:
    headers = {name.lower(): value for name, value in (error.headers or {}).items()}
    if error.code != 429 and not (
        error.code == 403 and (
            headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        )
    ):
        return None
    delays = []
    retry_after = headers.get("retry-after", "")
    try:
        delays.append(float(retry_after))
    except ValueError:
        try:
            delays.append(parsedate_to_datetime(retry_after).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        delays.append(float(headers.get("x-ratelimit-reset", "")) - time.time())
    except ValueError:
        pass
    valid = [math.ceil(delay) for delay in delays if math.isfinite(delay) and delay > 0]
    return max(valid, default=60)


class UpdateOwnershipLostError(RuntimeError):
    """Raised when a detached updater no longer owns the durable status row."""


class InstallerRolledBack(RuntimeError):
    """The recovery-only installer verified and restored the prior installation."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def server_update_status_lock(path: Path):
    """Cross-process lock shared by the server and detached updater."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise PermissionError("server update status lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_status_unlocked(path: Path) -> dict[str, Any]:
    try:
        current = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return current if isinstance(current, dict) else {}


def _update_status_unlocked(path: Path, current: dict[str, Any], **changes: Any) -> dict[str, Any]:
    current = dict(current)
    current.update(changes)
    current["updated_at"] = utc_now()
    atomic_json(path, current)
    return current


def update_status(
    path: Path,
    *,
    expected_update_id: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    with server_update_status_lock(path):
        current = _read_status_unlocked(path)
        if (
            expected_update_id is not None
            and (
                str(current.get("update_id") or "") != expected_update_id
                or str(current.get("phase") or "")
                not in RUNNER_OWNED_ACTIVE_PHASES
            )
        ):
            raise UpdateOwnershipLostError(
                "detached updater no longer owns the server update status"
            )
        return _update_status_unlocked(path, current, **changes)


def trim_installer_log(log_path: Path, limit: int = INSTALLER_LOG_MAX_BYTES) -> None:
    """Retain bounded history between attempts, while no installer is writing."""
    try:
        with log_path.open("r+b") as log:
            log.seek(0, os.SEEK_END)
            if log.tell() <= limit:
                return
            log.seek(-limit, os.SEEK_END)
            tail = log.read(limit)
            # Do not retain an unclassifiable partial secret line.
            tail = tail.partition(b"\n")[2]
            log.seek(0)
            log.write(tail)
            log.truncate()
    except FileNotFoundError:
        pass


def installer_log_tail(log_path: Path, *, start_offset: int = 0) -> str:
    """Read a bounded diagnostic tail without loading a large install log."""
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(start_offset, size - INSTALLER_LOG_TAIL_BYTES)
            truncated = start > start_offset
            stream.seek(start)
            content = stream.read(INSTALLER_LOG_TAIL_BYTES)
    except OSError:
        return ""
    lines = content.decode("utf-8", "replace").splitlines()
    if truncated and lines:
        # The bounded read may begin inside a secret whose identifying key was
        # before the cutoff. Never surface that unclassifiable partial line.
        lines = lines[1:]
    safe_lines: list[str] = []
    for line in lines:
        if "AGENTSDOCK_SETUP_RESULT=" in line:
            continue
        line = re.sub(
            r"(?i)(Authorization\s*:\s*Bearer\s+)\S+",
            r"\1[REDACTED]",
            line,
        )
        line = re.sub(
            r"(?i)(\bBearer\s+)\S+",
            r"\1[REDACTED]",
            line,
        )
        line = re.sub(
            r"(?i)([\"']?(?:AGENTSDOCK_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|AGENTSDOCK_PUBLISH_TOKEN|AGENTSDOCK_PROVIDER_AUTHORITY_FILE|access_token)[\"']?\s*[=:]\s*).*$",
            r"\1[REDACTED]",
            line,
        )
        safe_lines.append(line)
    tail = "\n".join(safe_lines[-INSTALLER_LOG_TAIL_LINES:])
    return tail[-INSTALLER_ERROR_MAX_CHARS:].strip()


def terminate_installer(
    process: subprocess.Popen[Any],
    *,
    on_wait: Callable[[], None] | None = None,
) -> None:
    """Request installer termination and join its protected recovery.

    install.sh cannot currently prove to this process whether activation has
    begun. Once it has begun, SIGKILL can strand the service stopped or sever a
    Team Hub restore before the old release is restarted. TERM is cooperative:
    the installer kills ordinary stage workers, then masks further termination
    while it completes rollback. Wait without a force-kill deadline.
    """
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
    else:
        process.terminate()
    while True:
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                # Losing status ownership must not turn a safe join back into
                # a detached installer or a force-kill boundary.
                pass
        try:
            process.wait(timeout=INSTALLER_TERMINATION_POLL_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def installer_environment() -> dict[str, str]:
    """Return an installer environment detached from the caller's workspace."""
    environment = os.environ.copy()
    for name in INSTALLER_ENVIRONMENT_SELECTORS:
        environment.pop(name, None)
    return environment


def run_installer(
    command: list[str],
    *,
    cwd: Path,
    status_path: Path,
    log_path: Path,
    version: str,
    expected_update_id: str | None = None,
    managed_update_id: str | None = None,
    expected_service_cgroup: str | None = None,
    timeout_seconds: float = INSTALLER_TIMEOUT_SECONDS,
    heartbeat_seconds: float = INSTALLER_HEARTBEAT_SECONDS,
    on_started: Callable[[], None] | None = None,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> None:
    """Run the installer with live logging and a durable status heartbeat."""
    if expected_update_id is not None:
        update_status(
            status_path,
            expected_update_id=expected_update_id,
            heartbeat_at=utc_now(),
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"\n--- AgentsServer update {expected_update_id or 'manual'} "
        f"to {version} at {utc_now()} ---\n"
    ).encode()
    trim_installer_log(log_path, INSTALLER_LOG_MAX_BYTES - len(header))
    started = time.monotonic()
    deadline = started + timeout_seconds
    environment = installer_environment()
    if managed_update_id is not None:
        environment["AGENTSDOCK_MANAGED_UPDATE_ID"] = managed_update_id
    if expected_service_cgroup is not None:
        environment["AGENTSDOCK_EXPECTED_SERVICE_CGROUP"] = expected_service_cgroup
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        log.write(header)
        log.flush()
        attempt_start = log.tell()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        )
        if on_started is not None:
            on_started()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                def report_protected_recovery() -> None:
                    elapsed = max(1, int(time.monotonic() - started))
                    update_status(
                        status_path,
                        expected_update_id=expected_update_id,
                        phase="installing",
                        message=(
                            "Installer timeout reached; waiting for its protected "
                            f"rollback to finish ({elapsed}s elapsed)."
                        ),
                        heartbeat_at=utc_now(),
                        elapsed_seconds=elapsed,
                    )

                terminate_installer(process, on_wait=report_protected_recovery)
                log.flush()
                tail = installer_log_tail(log_path, start_offset=attempt_start)
                trim_installer_log(log_path)
                detail = f": {tail}" if tail else ""
                raise RuntimeError(
                    f"installer timed out after {timeout_seconds:g} seconds{detail}"
                )
            try:
                returncode = process.wait(timeout=min(heartbeat_seconds, remaining))
                break
            except subprocess.TimeoutExpired:
                elapsed = max(1, int(time.monotonic() - started))
                try:
                    update_status(
                        status_path,
                        expected_update_id=expected_update_id,
                        phase="installing",
                        message=f"Installing AgentsServer {version} ({elapsed}s elapsed).",
                        heartbeat_at=utc_now(),
                        elapsed_seconds=elapsed,
                    )
                except UpdateOwnershipLostError:
                    terminate_installer(process)
                    log.flush()
                    trim_installer_log(log_path)
                    raise
        log.flush()

    tail = installer_log_tail(log_path, start_offset=attempt_start) if returncode else ""
    trim_installer_log(log_path)
    if returncode == 75 and returncode in accepted_returncodes:
        raise InstallerRolledBack("interrupted activation was rolled back")
    if returncode != 0:
        raise RuntimeError(
            f"installer failed ({returncode}): {tail or 'no output; inspect server-update.log'}"
        )


def download_bytes(url: str, limit: int, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AgentsServer-Updater/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > limit:
            raise RuntimeError(f"download exceeds the {limit}-byte safety limit")
        content = response.read(limit + 1)
    if len(content) > limit:
        raise RuntimeError(f"download exceeds the {limit}-byte safety limit")
    return content


def server_health_snapshot(
    port: int,
    *,
    token: str | None = None,
    timeout: float = SERVER_IDLE_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read one bounded authenticated AgentsServer health document."""

    headers = {"User-Agent": "AgentsServer-Updater/1"}
    clean_token = str(token or "").strip()
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/health",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(MAX_METADATA_BYTES + 1)
    if len(content) > MAX_METADATA_BYTES:
        raise RuntimeError("server health response exceeds its safety limit")
    try:
        health = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("server health response is invalid JSON") from exc
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise RuntimeError("server health response is not healthy")
    return health


def server_work_snapshot(
    port: int,
    *,
    token: str | None = None,
    timeout: float = SERVER_IDLE_CHECK_TIMEOUT_SECONDS,
    require_cgroup_safe: bool = False,
    require_verified_service_cgroup: bool = False,
    expected_server_identity: str | None = None,
) -> tuple[int, int]:
    """Read the live workload immediately before invoking the installer."""

    health = server_health_snapshot(port, token=token, timeout=timeout)
    if (
        expected_server_identity is not None
        and health.get("server_identity") != expected_server_identity
    ):
        raise RuntimeError("AgentsServer stable identity changed before restart")

    active = health.get("active")
    raw_active_count = health.get("active_count")
    if isinstance(raw_active_count, bool):
        raise RuntimeError("server health response has an invalid active count")
    if isinstance(raw_active_count, int):
        active_count = max(0, raw_active_count)
    elif isinstance(active, list):
        active_count = len(active)
    else:
        raise RuntimeError("server health response is missing its active count")

    raw_blocking_queued_count = health.get("update_blocking_queued_count")
    if raw_blocking_queued_count is not None:
        if (
            isinstance(raw_blocking_queued_count, bool)
            or not isinstance(raw_blocking_queued_count, int)
            or raw_blocking_queued_count < 0
        ):
            raise RuntimeError(
                "server health response has an invalid update-blocking queued count"
            )
        queued_count = raw_blocking_queued_count
    else:
        # Older servers did not distinguish durable preserved messages from
        # volatile queue state. Retain their fail-closed behavior.
        queued = health.get("queued")
        if not isinstance(queued, dict):
            raise RuntimeError("server health response is missing its queued turns")
        queued_count = 0
        for value in queued.values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError("server health response has an invalid queued count")
            queued_count += value
    if require_cgroup_safe and not active_count and not queued_count:
        cgroup = health.get("update_service_cgroup")
        if not isinstance(cgroup, dict):
            raise RuntimeError(
                "server health response is missing its service-cgroup admission proof"
            )
        raw_count = cgroup.get("unknown_descendant_count")
        if (
            cgroup.get("safe") is not True
            or isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count != 0
        ):
            raise RuntimeError(
                "AgentsServer has an unverified or nonempty service cgroup"
            )
        if (
            require_verified_service_cgroup
            and cgroup.get("inspection") != "verified"
        ):
            raise RuntimeError(
                "AgentsServer did not return a verified systemd service-cgroup proof"
            )
    return active_count, queued_count


def assert_post_update_identity(
    port: int,
    *,
    token: str | None,
    expected_server_identity: str,
    expected_team_hub_id: str | None = None,
    expected_team_hub_transport: str | None = None,
    expected_team_hub_url: str | None = None,
    expected_team_hub_direct_ip_url: str | None = None,
    expected_server_version: str | None = None,
    expected_api_contract_version: int | None = None,
) -> None:
    """Fence a replacement by stable server and managed Hub identities."""

    health = server_health_snapshot(port, token=token)
    if str(health.get("server_identity") or "") != expected_server_identity:
        raise RuntimeError("updated AgentsServer stable identity does not match")
    if expected_server_version is not None and health.get("server_version") != expected_server_version:
        raise RuntimeError("updated AgentsServer version does not match the signed release")
    if expected_server_version is not None and ("gateway" in health or "execution_service" in health):
        for component in ("gateway", "execution_service"):
            identity = health.get(component)
            if not isinstance(identity, dict) or identity.get("version") != expected_server_version:
                raise RuntimeError("updated AgentsServer gateway and execution versions have not converged")
        if health["execution_service"].get("maintenance_held") is not False:
            raise RuntimeError("updated AgentsServer activation has not released admission")
    if expected_api_contract_version is not None and (
        type(health.get("api_contract_version")) is not int
        or health["api_contract_version"] != expected_api_contract_version
    ):
        raise RuntimeError("updated AgentsServer API contract does not match the signed release")
    capabilities = health.get("capabilities")
    secure_peer_capability = (
        capabilities.get("secure_peer_v1")
        if isinstance(capabilities, dict)
        else None
    )
    if not isinstance(secure_peer_capability, dict) or any(
        secure_peer_capability.get(name) != value
        for name, value in SECURE_PEER_HEALTH_REQUIREMENTS.items()
    ):
        raise RuntimeError("updated AgentsServer secure-peer state is unavailable")
    if expected_team_hub_id is None:
        if (
            expected_team_hub_transport is not None
            or expected_team_hub_url is not None
            or expected_team_hub_direct_ip_url is not None
        ):
            raise RuntimeError("managed Team Hub transport has no bound Hub identity")
        return
    capability = (
        capabilities.get("team_hub_v1")
        if isinstance(capabilities, dict)
        else None
    )
    expected_capability = {
        "available": True,
        "designated_host": True,
        "version": 1,
        "base_path": "/api/team-hub",
        "hub_id": expected_team_hub_id,
        "host_server_identity": expected_server_identity,
    }
    if not isinstance(capability, dict) or any(
        capability.get(name) != value
        for name, value in expected_capability.items()
    ):
        raise RuntimeError("updated AgentsServer lost or changed its Team Hub identity")
    actual_transport = capability.get("transport")
    actual_hub_url = capability.get("hub_url")
    if expected_team_hub_transport is None:
        if actual_transport not in {None, "loopback"} or actual_hub_url is not None:
            raise RuntimeError("updated AgentsServer changed its legacy Team Hub transport")
    elif (
        actual_transport != expected_team_hub_transport
        or actual_hub_url != expected_team_hub_url
    ):
        raise RuntimeError("updated AgentsServer changed its Team Hub transport")
    if expected_team_hub_direct_ip_url is not None:
        expected_routes = [
            {
                "transport": expected_team_hub_transport or "loopback",
                "hub_url": expected_team_hub_url,
            }
        ]
        if (
            expected_team_hub_direct_ip_url
            and expected_team_hub_transport != "direct_ip"
        ):
            expected_routes.append(
                {
                    "transport": "direct_ip",
                    "hub_url": expected_team_hub_direct_ip_url,
                }
            )
        if capability.get("routes") != expected_routes:
            raise RuntimeError("updated AgentsServer changed its Team Hub routes")


def assert_repaired_team_hub_identity(
    port: int,
    *,
    token: str | None,
    expected_server_identity: str,
    expected_team_hub_transport: str,
    expected_team_hub_url: str | None,
    expected_team_hub_direct_ip_url: str,
) -> str:
    """Verify that an unavailable managed host was repaired in place."""

    health = server_health_snapshot(port, token=token)
    capabilities = health.get("capabilities")
    capability = (
        capabilities.get("team_hub_v1")
        if isinstance(capabilities, dict)
        else None
    )
    required = {
        "available": True,
        "designated_host": True,
        "version": 1,
        "base_path": "/api/team-hub",
        "host_server_identity": expected_server_identity,
        "transport": expected_team_hub_transport,
        "hub_url": expected_team_hub_url,
    }
    if not isinstance(capability, dict) or any(
        capability.get(name) != value for name, value in required.items()
    ):
        raise RuntimeError("updated AgentsServer did not repair its Team Hub host")
    hub_id = capability.get("hub_id")
    if not isinstance(hub_id, str) or re.fullmatch(
        r"[A-Za-z0-9_.:-]{8,240}", hub_id
    ) is None:
        raise RuntimeError("repaired Team Hub identity is invalid")
    expected_routes = [
        {
            "transport": expected_team_hub_transport,
            "hub_url": expected_team_hub_url,
        }
    ]
    if (
        expected_team_hub_direct_ip_url
        and expected_team_hub_transport != "direct_ip"
    ):
        expected_routes.append(
            {
                "transport": "direct_ip",
                "hub_url": expected_team_hub_direct_ip_url,
            }
        )
    if capability.get("routes") != expected_routes:
        raise RuntimeError("repaired Team Hub routes changed")
    return hub_id


def assert_server_idle(
    port: int,
    *,
    token: str | None = None,
    require_verified_service_cgroup: bool = False,
    expected_server_identity: str | None = None,
    timeout: float = SERVER_IDLE_CHECK_TIMEOUT_SECONDS,
) -> None:
    """Fail closed if work appeared after the update was accepted."""

    try:
        active_count, queued_count = server_work_snapshot(
            port,
            token=token,
            require_cgroup_safe=True,
            require_verified_service_cgroup=require_verified_service_cgroup,
            expected_server_identity=expected_server_identity,
            timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(
            f"could not verify that AgentsServer is idle before restart: {exc}"
        ) from exc
    if active_count or queued_count:
        parts: list[str] = []
        if active_count:
            parts.append(
                f"{active_count} active agent run{'s' if active_count != 1 else ''}"
            )
        if queued_count:
            parts.append(
                f"{queued_count} queued turn{'s' if queued_count != 1 else ''}"
            )
        raise RuntimeError(
            "server became busy before restart: "
            + " and ".join(parts)
            + "; retry the update after work finishes"
        )


def transient_server_readiness_error(error: BaseException | None) -> bool:
    """Recognize unavailable transport, never failed authentication or proof."""
    if isinstance(error, HTTPError):
        return error.code == 503
    if isinstance(error, URLError):
        error = error.reason
    return isinstance(error, (
        ConnectionRefusedError,
        ConnectionResetError,
        ConnectionAbortedError,
        TimeoutError,
    ))


def wait_for_server_idle(
    port: int,
    *,
    status_path: Path,
    expected_update_id: str,
    expected_server_identity: str,
    token: str | None = None,
    require_verified_service_cgroup: bool = False,
    timeout_seconds: float = SERVER_STARTUP_READINESS_TIMEOUT_SECONDS,
) -> None:
    """Allow a forced-update startup to finish opening its native listener.

    The replacement process advances its reserved update during lifespan,
    before HTTP startup completes. Keep the exact update alive while transport
    becomes ready, but never retry a response that fails the idle/safety proof.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: RuntimeError | None = None
    while True:
        # This CAS also stops a cancelled/finalized or superseded updater.
        update_status(
            status_path,
            expected_update_id=expected_update_id,
            heartbeat_at=utc_now(),
            message="Waiting for AgentsServer startup before checking installation safety.",
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"AgentsServer startup readiness timed out after {timeout_seconds:g} seconds"
            ) from last_error
        try:
            assert_server_idle(
                port,
                token=token,
                require_verified_service_cgroup=require_verified_service_cgroup,
                expected_server_identity=expected_server_identity,
                timeout=min(SERVER_IDLE_CHECK_TIMEOUT_SECONDS, remaining),
            )
        except RuntimeError as exc:
            if not transient_server_readiness_error(exc.__cause__):
                raise
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(SERVER_STARTUP_READINESS_POLL_SECONDS, remaining))
        else:
            # Ownership can change while the health request is in flight.
            update_status(
                status_path,
                expected_update_id=expected_update_id,
                heartbeat_at=utc_now(),
                message="AgentsServer startup, identity, and idle checks passed.",
            )
            return


def consume_auth_token_file(path: str | None) -> str:
    """Read and immediately remove the updater's one-time health credential."""

    clean_path = str(path or "").strip()
    if not clean_path:
        return ""
    token_path = Path(clean_path).expanduser().resolve()
    try:
        value = json.loads(token_path.read_text())
        token = str(value.get("token") or "") if isinstance(value, dict) else ""
    finally:
        try:
            token_path.unlink()
        except FileNotFoundError:
            pass
    if not token:
        raise RuntimeError("server update health credential is empty")
    return token


def clear_team_hub_maintenance(args: argparse.Namespace) -> bool:
    """Clear the exact durable update fence without opening the Hub DB."""

    hub_id = str(getattr(args, "expected_team_hub_id", "") or "").strip()
    snapshot = str(getattr(args, "team_hub_snapshot", "") or "").strip()
    data_dir = str(getattr(args, "team_hub_data_dir", "") or "").strip()
    host_identity = str(
        getattr(args, "expected_server_identity", "") or ""
    ).strip()
    operation_id = str(getattr(args, "update_id", "") or "").strip()
    if not any((hub_id, snapshot, data_dir)):
        return False
    if not all((hub_id, snapshot, data_dir, host_identity, operation_id)):
        raise RuntimeError("managed Team Hub maintenance arguments are incomplete")
    from agentsdock_team_hub.store import HubStore

    return HubStore.clear_maintenance_fence_control(
        Path(data_dir),
        expected_hub_id=hub_id,
        expected_host_identity=host_identity,
        expected_reason="server-update",
        expected_operation_id=operation_id,
        expected_snapshot=Path(snapshot),
    )


def team_hub_maintenance_fence_present(args: argparse.Namespace) -> bool:
    """Return whether this exact update still has an on-disk Hub fence."""

    hub_id = str(getattr(args, "expected_team_hub_id", "") or "").strip()
    snapshot = str(getattr(args, "team_hub_snapshot", "") or "").strip()
    data_dir = str(getattr(args, "team_hub_data_dir", "") or "").strip()
    host_identity = str(
        getattr(args, "expected_server_identity", "") or ""
    ).strip()
    operation_id = str(getattr(args, "update_id", "") or "").strip()
    if not any((hub_id, snapshot, data_dir)):
        return False
    if not all((hub_id, snapshot, data_dir, host_identity, operation_id)):
        raise RuntimeError("managed Team Hub maintenance arguments are incomplete")
    from agentsdock_team_hub.store import HubStore

    return HubStore.maintenance_fence_matches_control(
        Path(data_dir),
        expected_hub_id=hub_id,
        expected_host_identity=host_identity,
        expected_reason="server-update",
        expected_operation_id=operation_id,
        expected_snapshot=Path(snapshot),
    )


def version_is_prerelease(version: str) -> bool:
    return "-" in version.split("+", 1)[0]


def release_track(version: str) -> str:
    return "beta" if version_is_prerelease(version) else "stable"


def normalized_release_track(track: str | None) -> str:
    value = str(track or "stable").strip().lower()
    if value not in RELEASE_TRACKS:
        raise ValueError(f"invalid release track: {track}")
    return value


def version_key(version: str) -> tuple[Any, ...]:
    """Return a SemVer-compatible key for a trusted release version."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid release version: {version}")
    without_build = version.split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers: tuple[tuple[int, int | str], ...] = ()
    if separator:
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return major, minor, patch, 0 if separator else 1, identifiers


def release_manifest_url(version: str) -> str:
    return f"{RELEASE_BASE}/download/v{version}/agents-server-manifest.json"


def release_signature_url(version: str) -> str:
    return f"{RELEASE_BASE}/download/v{version}/agents-server-manifest.sig"


def release_candidates(releases: Any, track: str = "stable") -> list[str]:
    track = normalized_release_track(track)
    if not isinstance(releases, list):
        raise RuntimeError("GitHub releases response must be a JSON array")
    candidates: set[str] = set()
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith("v"):
            continue
        version = tag[1:]
        if not VERSION_PATTERN.fullmatch(version) or release_track(version) != track:
            continue
        declared_prerelease = release.get("prerelease")
        if declared_prerelease is not None and bool(declared_prerelease) != version_is_prerelease(version):
            continue
        candidates.add(version)
    return sorted(candidates, key=version_key, reverse=True)


def stable_release_candidates(releases: Any) -> list[str]:
    """Backward-compatible stable release discovery."""
    return release_candidates(releases, "stable")


def release_versions_from_html(content: bytes, track: str = "stable") -> set[str]:
    track = normalized_release_track(track)
    text = content.decode("utf-8", "replace")
    prefix = f"/{RELEASE_REPOSITORY}/releases/tag/v"
    return {
        match.group(1)
        for match in re.finditer(re.escape(prefix) + r"([^\"'<>/?#]+)", text)
        if VERSION_PATTERN.fullmatch(match.group(1)) and release_track(match.group(1)) == track
    }


def verify_manifest(
    manifest_bytes: bytes,
    signature: bytes,
    public_key_path: Path,
    *,
    expected_version: str | None = None,
    track: str = "stable",
    allow_npm: bool = False,
) -> dict[str, Any]:
    track = normalized_release_track(track)
    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise RuntimeError("release public key is not an Ed25519 key")
    key.verify(signature, manifest_bytes)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest must be a JSON object")
    version = str(manifest.get("version") or "")
    if not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError("release manifest contains an invalid version")
    actual_track = release_track(version)
    if actual_track != track:
        raise RuntimeError(f"release manifest is not on the requested {track} track")
    if expected_version is not None and version != expected_version:
        raise RuntimeError("release manifest version does not match its immutable release tag")
    expected_prerelease = actual_track == "beta"
    if manifest.get("prerelease") not in {None, expected_prerelease}:
        raise RuntimeError("release manifest prerelease metadata is inconsistent")
    if manifest.get("track") not in {None, actual_track}:
        raise RuntimeError("release manifest track metadata is inconsistent")
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise RuntimeError("release manifest is missing archive metadata")
    schema = manifest.get("schema", 1)
    if type(schema) is not int or schema not in ({1, 2} if allow_npm else {1}):
        raise RuntimeError("release manifest schema is not supported")
    if schema == 2:
        npm = manifest.get("npm")
        if (manifest.get("distribution") != "npm" or manifest.get("track") != actual_track
                or not isinstance(npm, dict)
                or npm.get("name") != NPM_PACKAGE_NAME or npm.get("version") != version
                or "+" in version):
            raise RuntimeError("release npm package identity is not trusted")
        integrity = npm.get("integrity")
        try:
            digest = base64.b64decode(integrity[7:], validate=True) if isinstance(integrity, str) and integrity.startswith("sha512-") else b""
        except (ValueError, binascii.Error):
            digest = b""
        if len(digest) != 64 or integrity != "sha512-" + base64.b64encode(digest).decode("ascii"):
            raise RuntimeError("release npm integrity is invalid")
        size = archive.get("size")
        if type(size) is not int or not 1 <= size <= MAX_ARCHIVE_BYTES:
            raise RuntimeError("release npm archive size is invalid")
        if type(manifest.get("api_contract_version")) is not int or manifest["api_contract_version"] < 1:
            raise RuntimeError("release API contract is invalid")
        expected_name = f"server-{version}.tgz"
        expected_prefix = NPM_REGISTRY_BASE
    else:
        expected_name = f"agents-server-{version}.tar.gz"
        expected_prefix = f"{RELEASE_BASE}/download/v{version}/"
    archive_name = str(archive.get("name") or "")
    archive_url = str(archive.get("url") or "")
    archive_sha = str(archive.get("sha256") or "").lower()
    if archive_name != expected_name or archive_url != expected_prefix + expected_name:
        raise RuntimeError("release archive location is not trusted")
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha):
        raise RuntimeError("release archive checksum is invalid")
    return manifest


def verify_npm_release_envelope(
    envelope: Any, public_key_path: Path, *, expected_version: str | None = None,
) -> dict[str, Any]:
    """Verify original publisher-signed bytes, never unsigned registry metadata."""
    if not isinstance(envelope, dict) or set(envelope) != {"manifest_base64", "signature_base64"}:
        raise RuntimeError("signed npm release envelope is invalid")
    encoded = envelope.get("manifest_base64")
    signature = envelope.get("signature_base64")
    if (not isinstance(encoded, str) or not 1 <= len(encoded) <= ((MAX_NPM_MANIFEST_BYTES + 2) // 3) * 4
            or not isinstance(signature, str) or len(signature) != 88):
        raise RuntimeError("signed npm release envelope exceeds its bounds")
    try:
        payload = base64.b64decode(encoded, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        candidate = json.loads(payload)
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise RuntimeError("signed npm release envelope is invalid") from exc
    if len(payload) > MAX_NPM_MANIFEST_BYTES or len(signature_bytes) != 64 or not isinstance(candidate, dict):
        raise RuntimeError("signed npm release envelope is invalid")
    manifest = verify_manifest(
        payload, signature_bytes, public_key_path, expected_version=expected_version,
        track=str(candidate.get("track") or ""), allow_npm=True,
    )
    if manifest.get("schema") != 2 or manifest.get("distribution") != "npm":
        raise RuntimeError("a signed npm release descriptor is required")
    return manifest


def verify_npm_archive(content: bytes, manifest: dict[str, Any]) -> None:
    """Check both publisher-bound hashes and exact size before extraction."""
    if (len(content) != manifest["archive"]["size"]
            or hashlib.sha256(content).hexdigest() != manifest["archive"]["sha256"].lower()
            or "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode("ascii") != manifest["npm"]["integrity"]):
        raise RuntimeError("npm archive does not match the signed release descriptor")


def download_npm_archive(manifest: dict[str, Any]) -> bytes:
    """Fetch the exact signed registry location without following redirects."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError("npm release archive redirects are not allowed")

    request = urllib.request.Request(manifest["archive"]["url"], headers={"User-Agent": "AgentsServer-Updater/1"})
    size = manifest["archive"]["size"]
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=120.0) as response:
        if response.geturl() != manifest["archive"]["url"]:
            raise RuntimeError("npm release archive location changed")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) != size:
            raise RuntimeError("npm release archive size changed")
        content = response.read(size + 1)
    verify_npm_archive(content, manifest)
    return content


def check_release(
    public_key_path: Path,
    track: str = "stable",
    *,
    expected_version: str | None = None,
    require_latest: bool = False,
) -> dict[str, Any]:
    try:
        return _check_release(
            public_key_path, track, expected_version=expected_version,
            require_latest=require_latest,
        )
    except HTTPError as exc:
        delay = release_rate_limit_delay(exc)
        if delay is not None:
            exc.close()
            raise ReleaseRateLimitedError(delay) from exc
        raise


def _check_release(
    public_key_path: Path,
    track: str = "stable",
    *,
    expected_version: str | None = None,
    require_latest: bool = False,
) -> dict[str, Any]:
    track = normalized_release_track(track)
    if expected_version is not None:
        version = str(expected_version).strip()
        if not VERSION_PATTERN.fullmatch(version):
            raise RuntimeError("expected release version is invalid")
        if release_track(version) != track:
            raise RuntimeError(
                f"expected release {version} is not on the requested {track} track"
            )
        if require_latest:
            latest_manifest = check_release(public_key_path, track)
            latest_version = str(latest_manifest.get("version") or "")
            if latest_version != version:
                raise RuntimeError(
                    f"requested {track} release {version} is no longer the latest "
                    f"signed {track} release {latest_version}"
                )
            # The discovery path verified this manifest against the immutable
            # versioned tag URL. Return that exact pinned document instead of
            # fetching a mutable 'latest' alias or selecting a newer release.
            return latest_manifest
        try:
            manifest_bytes = download_bytes(
                release_manifest_url(version),
                MAX_METADATA_BYTES,
            )
            signature = download_bytes(
                release_signature_url(version),
                MAX_METADATA_BYTES,
            )
        except HTTPError as exc:
            if exc.code == 404:
                raise ReleaseUnavailableError(
                    f"Signed {track} AgentsServer release {version} is unavailable."
                ) from exc
            raise
        return verify_manifest(
            manifest_bytes,
            signature,
            public_key_path,
            expected_version=version,
            track=track,
        )

    try:
        releases_bytes = download_bytes(RELEASES_API_URL, MAX_METADATA_BYTES)
    except HTTPError as exc:
        if exc.code == 404:
            raise ReleaseUnavailableError("No signed AgentsServer release has been published yet.") from exc
        # Do not turn a rejected API request into a burst of HTML requests.
        # The caller shares the Retry-After delay across subsequent checks.
        raise
    else:
        try:
            releases = json.loads(releases_bytes)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub releases response is invalid JSON") from exc
        candidates = release_candidates(releases, track)
    if not candidates:
        raise ReleaseUnavailableError(f"No signed {track} AgentsServer release is available.")

    for version in candidates:
        try:
            manifest_bytes = download_bytes(release_manifest_url(version), MAX_METADATA_BYTES)
            signature = download_bytes(release_signature_url(version), MAX_METADATA_BYTES)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        return verify_manifest(
            manifest_bytes,
            signature,
            public_key_path,
            expected_version=version,
            track=track,
        )
    raise ReleaseUnavailableError(f"No signed {track} AgentsServer release is available.")


def release_transition_allowed(current: str, target: str, track: str = "stable") -> bool:
    """Allow forward updates and an explicit prerelease-to-stable channel exit."""
    track = normalized_release_track(track)
    if release_track(target) != track:
        return False
    if version_key(target) > version_key(current):
        return True
    return (
        version_key(target) < version_key(current)
        and track == "stable"
        and version_is_prerelease(current)
        and not version_is_prerelease(target)
    )


def safe_extract(archive_path: Path, destination: Path, *, npm_manifest: dict[str, Any] | None = None) -> Path:
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if destination != target and destination not in target.parents:
                raise RuntimeError("release archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise RuntimeError("release archive must not contain links")
            if npm_manifest is not None and not (member.isfile() or member.isdir()):
                raise RuntimeError("npm release archive contains a special file")
        if npm_manifest is not None:
            if (len(members) > 4096 or sum(member.size for member in members) > 1024 * 1024 * 1024
                    or len({member.name for member in members}) != len(members)):
                raise RuntimeError("npm release archive exceeds its extraction bounds")
        archive.extractall(destination, members=members, filter="data")
    if npm_manifest is not None:
        if {entry.name for entry in destination.iterdir()} != {"package"}:
            raise RuntimeError("npm release archive has an invalid layout")
        package = destination / "package"
        metadata = json.loads((package / "package.json").read_text())
        source = package / "server"
        if (metadata.get("name") != NPM_PACKAGE_NAME or metadata.get("version") != npm_manifest["version"]
                or not (source / "install.sh").is_file()
                or (source / "VERSION").read_text().strip() != npm_manifest["version"]):
            raise RuntimeError("npm release archive payload identity does not match")
        return source
    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install.sh").is_file():
        raise RuntimeError("release archive has an invalid layout")
    return roots[0]


def run_update(args: argparse.Namespace) -> None:
    status_path = Path(args.status_file).expanduser().resolve()
    public_key = Path(args.public_key).expanduser().resolve()
    track = normalized_release_track(getattr(args, "track", "stable"))
    auth_token = consume_auth_token_file(getattr(args, "auth_token_file", None))
    expected_server_identity = str(
        getattr(args, "expected_server_identity", "") or ""
    ).strip()
    if not expected_server_identity:
        raise RuntimeError("managed update is missing the stable server identity")
    update_id = str(getattr(args, "update_id", "") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", update_id) is None:
        raise RuntimeError("managed update is missing a valid update ID")
    expected_service_cgroup = str(
        getattr(args, "expected_service_cgroup", "") or ""
    ).strip() or None
    if expected_service_cgroup is not None and re.fullmatch(
        r"/(?:[A-Za-z0-9_.@:-]+/)*[A-Za-z0-9_.@:-]+",
        expected_service_cgroup,
    ) is None:
        raise RuntimeError("managed update has an invalid service cgroup")
    repair_failed_team_hub_host = bool(
        getattr(args, "repair_failed_team_hub_host", False)
    )
    expected_team_hub_id = str(
        getattr(args, "expected_team_hub_id", "") or ""
    ).strip() or None
    raw_team_hub_transport = getattr(args, "expected_team_hub_transport", None)
    expected_team_hub_transport = (
        str(raw_team_hub_transport).strip()
        if raw_team_hub_transport is not None
        else None
    )
    raw_team_hub_url = getattr(args, "expected_team_hub_url", None)
    expected_team_hub_url = (
        str(raw_team_hub_url).strip()
        if raw_team_hub_url is not None
        else None
    )
    raw_team_hub_direct_ip_url = getattr(
        args, "expected_team_hub_direct_ip_url", None
    )
    expected_team_hub_direct_ip_url = (
        str(raw_team_hub_direct_ip_url).strip()
        if raw_team_hub_direct_ip_url is not None
        else None
    )
    team_hub_snapshot = str(
        getattr(args, "team_hub_snapshot", "") or ""
    ).strip() or None
    team_hub_data_dir = str(
        getattr(args, "team_hub_data_dir", "") or ""
    ).strip() or None
    if any((expected_team_hub_id, team_hub_snapshot, team_hub_data_dir)) and not all(
        (expected_team_hub_id, team_hub_snapshot, team_hub_data_dir)
    ):
        raise RuntimeError("managed Team Hub rollback arguments must be complete")
    if repair_failed_team_hub_host and any(
        (expected_team_hub_id, team_hub_snapshot, team_hub_data_dir)
    ):
        raise RuntimeError(
            "failed Team Hub repair cannot reuse live-host rollback arguments"
        )
    if expected_team_hub_id is None and not repair_failed_team_hub_host:
        if (
            expected_team_hub_transport is not None
            or expected_team_hub_url is not None
            or expected_team_hub_direct_ip_url is not None
        ):
            raise RuntimeError("managed Team Hub transport arguments require a Hub identity")
    if repair_failed_team_hub_host and (
        raw_team_hub_transport is None
        or raw_team_hub_url is None
        or raw_team_hub_direct_ip_url is None
    ):
        raise RuntimeError(
            "failed Team Hub repair requires exact transport continuity arguments"
        )
    if (
        expected_team_hub_id is not None or repair_failed_team_hub_host
    ) and expected_team_hub_transport is None:
        if raw_team_hub_url is not None:
            raise RuntimeError("legacy loopback Team Hub cannot have a remote URL")
    elif expected_team_hub_id is not None or repair_failed_team_hub_host:
        if expected_team_hub_transport == "loopback":
            if raw_team_hub_url is None or expected_team_hub_url != "":
                raise RuntimeError("loopback Team Hub cannot have a remote URL")
            expected_team_hub_url = None
        elif expected_team_hub_transport in {"tailscale_serve", "direct_ip"}:
            if expected_team_hub_url is None:
                raise RuntimeError("remote Team Hub transport requires its exact URL")
            try:
                from team_hub_host import (  # Imported only for the managed-Hub path.
                    TEAM_HUB_MODE_HOST,
                    configured_team_hub_endpoint,
                )

                resolved_transport, resolved_url, _host, config_error = (
                    configured_team_hub_endpoint(
                        TEAM_HUB_MODE_HOST,
                        expected_team_hub_url,
                        expected_team_hub_transport,
                        args.port,
                    )
                )
            except Exception as exc:
                raise RuntimeError("could not validate the expected Team Hub URL") from exc
            if (
                config_error is not None
                or resolved_transport != expected_team_hub_transport
                or resolved_url != expected_team_hub_url
            ):
                raise RuntimeError("expected Team Hub URL is invalid")
        else:
            raise RuntimeError("managed Team Hub transport is invalid")
    if expected_team_hub_direct_ip_url is not None:
        if expected_team_hub_id is None and not repair_failed_team_hub_host:
            raise RuntimeError("managed Team Hub direct-IP route requires a Hub identity")
        if expected_team_hub_direct_ip_url:
            try:
                from team_hub_host import (
                    TEAM_HUB_MODE_HOST,
                    configured_team_hub_endpoint,
                )

                direct_transport, direct_url, _host, direct_error = (
                    configured_team_hub_endpoint(
                        TEAM_HUB_MODE_HOST,
                        expected_team_hub_direct_ip_url,
                        "direct_ip",
                        args.port,
                    )
                )
            except Exception as exc:
                raise RuntimeError(
                    "could not validate the expected Team Hub direct-IP route"
                ) from exc
            if (
                direct_error is not None
                or direct_transport != "direct_ip"
                or direct_url != expected_team_hub_direct_ip_url
            ):
                raise RuntimeError("expected Team Hub direct-IP route is invalid")
        if (
            expected_team_hub_transport == "direct_ip"
            and expected_team_hub_direct_ip_url != expected_team_hub_url
        ):
            raise RuntimeError("primary direct-IP Team Hub route changed")
    if repair_failed_team_hub_host:
        with server_update_status_lock(status_path):
            admitted = _read_status_unlocked(status_path)
        expected_repair_routes = [
            {
                "transport": expected_team_hub_transport,
                "hub_url": expected_team_hub_url,
            }
        ]
        if (
            expected_team_hub_direct_ip_url
            and expected_team_hub_transport != "direct_ip"
        ):
            expected_repair_routes.append(
                {
                    "transport": "direct_ip",
                    "hub_url": expected_team_hub_direct_ip_url,
                }
            )
        expected_status = {
            "update_id": update_id,
            "phase": "starting",
            "team_hub_repair_mode": "failed_start",
            "team_hub_host_server_identity": expected_server_identity,
            "team_hub_transport": expected_team_hub_transport,
            "team_hub_url": expected_team_hub_url,
            "team_hub_direct_ip_url": expected_team_hub_direct_ip_url,
            "team_hub_routes": expected_repair_routes,
        }
        if any(admitted.get(name) != value for name, value in expected_status.items()):
            raise RuntimeError(
                "failed Team Hub repair is not owned by the exact admitted update"
            )
    update_status(
        status_path,
        expected_update_id=update_id,
        phase="checking",
        track=track,
        runner_pid=os.getpid(),
        heartbeat_at=utc_now(),
        message=f"Checking the signed {track} release manifest.",
    )
    expected_version = (
        str(getattr(args, "expected_version", "") or "").strip() or None
    )
    current_version = str(
        getattr(args, "current_version", "") or ""
    ).strip()
    require_latest = (
        track == "stable"
        and bool(current_version)
        and version_is_prerelease(current_version)
    )
    prepared_update = None
    prepared_receipt = getattr(args, "prepared_receipt", None)
    if prepared_receipt:
        from update_preparation import verify_prepared_status
        with server_update_status_lock(status_path):
            admitted = _read_status_unlocked(status_path)
        if admitted.get("update_id") != update_id or admitted.get("phase") not in RUNNER_OWNED_ACTIVE_PHASES:
            raise RuntimeError("Prepared release is not owned by this update")
        if admitted.get("_prepared_update", {}).get("receipt_sha256") != getattr(args, "prepared_receipt_sha256", None):
            raise RuntimeError("Prepared release receipt digest changed")
        prepared_update = verify_prepared_status(admitted,
            root=Path(os.environ["AGENTS_SERVER_INSTALL_DIR"]).expanduser().resolve(), public_key=public_key)
        if prepared_update["receipt"] != prepared_receipt:
            raise RuntimeError("Prepared release receipt path changed")
        manifest = prepared_update["manifest"]
        if manifest["track"] != track:
            raise RuntimeError("Prepared release track changed")
    elif getattr(args, "npm_descriptor", False):
        with server_update_status_lock(status_path):
            admitted = _read_status_unlocked(status_path)
        if admitted.get("update_id") != update_id or admitted.get("phase") not in RUNNER_OWNED_ACTIVE_PHASES:
            raise RuntimeError("signed npm release is not owned by this update")
        manifest = verify_npm_release_envelope(admitted.get("_npm_release"), public_key, expected_version=expected_version)
        if manifest["track"] != track or release_track(current_version) != track:
            raise RuntimeError("automatic npm updates cannot change release channels")
    else:
        manifest = check_release(
            public_key,
            track,
            expected_version=expected_version,
            require_latest=require_latest,
        )
    version = str(manifest["version"])
    if expected_version and version != expected_version:
        raise RuntimeError(
            f"resolved signed release is {version}, not {expected_version}"
        )
    if current_version and not release_transition_allowed(current_version, version, track):
        raise RuntimeError(
            f"resolved release {version} is not newer than installed version {current_version}; "
            "managed updates only permit forward updates or an explicit beta-to-stable channel switch"
        )

    with tempfile.TemporaryDirectory(prefix="agents-server-update-") as temporary:
        root = Path(temporary)
        if prepared_update is not None:
            source = Path(prepared_update["candidate"])
            update_status(status_path, expected_update_id=update_id, phase="verifying",
                target_version=version, message="Prepared runtime and signed release verified.")
        else:
            archive_path = root / str(manifest["archive"]["name"])
            update_status(
                status_path,
                expected_update_id=update_id,
                phase="downloading",
                track=track,
                target_version=version,
                message=f"Downloading AgentsServer {version}.",
            )
            archive_bytes = (download_npm_archive(manifest) if manifest.get("schema") == 2 else
                             download_bytes(str(manifest["archive"]["url"]), MAX_ARCHIVE_BYTES, timeout=120.0))
            digest = hashlib.sha256(archive_bytes).hexdigest()
            if digest != manifest["archive"]["sha256"]:
                raise RuntimeError("release archive checksum does not match the signed manifest")
            archive_path.write_bytes(archive_bytes)

            update_status(
                status_path,
                expected_update_id=update_id,
                phase="verifying",
                message="Signature and archive checksum verified.",
            )
            source = (safe_extract(archive_path, root / "extracted", npm_manifest=manifest)
                      if manifest.get("schema") == 2 else safe_extract(archive_path, root / "extracted"))
        install = source / "install.sh"
        if prepared_update is None:
            install.chmod(0o755)
        command = [
            str(install),
            "--non-interactive",
            "--release-version", version,
            "--port", str(args.port),
            "--bind", args.bind,
            "--expected-server-identity", expected_server_identity,
        ]
        if prepared_update is not None:
            command.extend(["--activate-prepared", prepared_update["receipt"],
                            "--prepared-archive-sha256", manifest["archive"]["sha256"],
                            "--execution-mode", "split"])
        handoff_file = getattr(args, "execution_handoff_file", None)
        if handoff_file:
            command.extend(["--execution-handoff-file", handoff_file])
        if manifest.get("schema") == 2 or prepared_update is not None:
            command.extend(["--expected-api-contract", str(manifest["api_contract_version"])])
        if expected_team_hub_id is not None:
            command.extend(
                [
                    "--expected-team-hub-id", expected_team_hub_id,
                    "--team-hub-snapshot", str(team_hub_snapshot),
                    "--team-hub-data-dir", str(team_hub_data_dir),
                    "--team-hub-operation-id", update_id,
                ]
            )
            if expected_team_hub_transport is not None:
                command.extend(
                    [
                        "--expected-team-hub-transport",
                        expected_team_hub_transport,
                        "--expected-team-hub-url",
                        expected_team_hub_url or "",
                    ]
                )
            if expected_team_hub_direct_ip_url is not None:
                command.extend(
                    [
                        "--expected-team-hub-direct-ip-url",
                        expected_team_hub_direct_ip_url,
                    ]
                )
        elif repair_failed_team_hub_host:
            command.extend(
                [
                    "--repair-failed-team-hub-host",
                    "--managed-update-id",
                    update_id,
                    "--expected-team-hub-transport",
                    expected_team_hub_transport or "",
                    "--expected-team-hub-url",
                    expected_team_hub_url or "",
                    "--expected-team-hub-direct-ip-url",
                    expected_team_hub_direct_ip_url or "",
                ]
            )
        wait_for_server_idle(
            args.port,
            status_path=status_path,
            expected_update_id=update_id,
            expected_server_identity=expected_server_identity,
            token=auth_token,
            require_verified_service_cgroup=expected_service_cgroup is not None,
        )
        update_status(
            status_path,
            expected_update_id=update_id,
            phase="installing",
            message=f"Installing AgentsServer {version} with rollback protection.",
        )
        if prepared_update is not None:
            from update_recovery import activation_intent
            update_status(status_path, expected_update_id=update_id,
                _activation_recovery=activation_intent(
                    root=Path(os.environ["AGENTS_SERVER_INSTALL_DIR"]), candidate=source,
                    version=version, api_contract=manifest["api_contract_version"],
                    update_id=update_id, server_identity=expected_server_identity))
        log_path = status_path.with_name("server-update.log")
        run_installer(
            command,
            cwd=source,
            status_path=status_path,
            log_path=log_path,
            version=version,
            expected_update_id=update_id,
            managed_update_id=(
                update_id
                if expected_service_cgroup is not None
                or repair_failed_team_hub_host
                else None
            ),
            expected_service_cgroup=expected_service_cgroup,
        )
        identity_arguments: dict[str, Any] = {
            "token": auth_token,
            "expected_server_identity": expected_server_identity,
            "expected_team_hub_id": expected_team_hub_id,
        }
        if manifest.get("schema") == 2:
            identity_arguments.update(
                expected_server_version=version,
                expected_api_contract_version=manifest["api_contract_version"],
            )
        if expected_team_hub_transport is not None and not repair_failed_team_hub_host:
            identity_arguments["expected_team_hub_transport"] = (
                expected_team_hub_transport
            )
        if expected_team_hub_url is not None and not repair_failed_team_hub_host:
            identity_arguments["expected_team_hub_url"] = expected_team_hub_url
        if (
            expected_team_hub_direct_ip_url is not None
            and not repair_failed_team_hub_host
        ):
            identity_arguments["expected_team_hub_direct_ip_url"] = (
                expected_team_hub_direct_ip_url
            )
        assert_post_update_identity(args.port, **identity_arguments)
        if repair_failed_team_hub_host:
            assert_repaired_team_hub_identity(
                args.port,
                token=auth_token,
                expected_server_identity=expected_server_identity,
                expected_team_hub_transport=expected_team_hub_transport or "",
                expected_team_hub_url=expected_team_hub_url,
                expected_team_hub_direct_ip_url=(
                    expected_team_hub_direct_ip_url or ""
                ),
            )
        # install.sh owns the success clear while it can still stop the
        # candidate, restore the verified snapshot, and restart the old
        # release. The runner clears only failures before install starts.

    update_status(
        status_path,
        expected_update_id=update_id,
        phase="complete",
        update_available=False,
        message=f"AgentsServer {version} is installed and healthy.",
        track=track,
        installed_version=version,
        heartbeat_at=None,
        elapsed_seconds=None,
        runner_pid=None,
        error_code=None,
        error_action=None,
        retryable=None,
        finished_at=utc_now(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--current-version")
    parser.add_argument("--track", choices=sorted(RELEASE_TRACKS), default="stable")
    parser.add_argument("--npm-descriptor", action="store_true")
    parser.add_argument("--prepared-receipt")
    parser.add_argument("--prepared-receipt-sha256")
    parser.add_argument("--execution-handoff-file")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--recovery-transaction")
    parser.add_argument("--auth-token-file")
    parser.add_argument("--expected-server-identity", required=True)
    parser.add_argument("--update-id", required=True)
    parser.add_argument("--expected-service-cgroup")
    parser.add_argument("--expected-team-hub-id")
    parser.add_argument("--expected-team-hub-transport")
    parser.add_argument("--expected-team-hub-url")
    parser.add_argument("--expected-team-hub-direct-ip-url")
    parser.add_argument("--team-hub-snapshot")
    parser.add_argument("--team-hub-data-dir")
    parser.add_argument("--repair-failed-team-hub-host", action="store_true")
    args = parser.parse_args()
    try:
        if args.recover_only:
            from update_recovery import run_recovery
            run_recovery(args)
        else:
            run_update(args)
        return 0
    except Exception as exc:
        status_path = Path(args.status_file).expanduser().resolve()
        update_id = str(getattr(args, "update_id", "") or "").strip()
        if args.recover_only:
            # Only install.sh may release an interrupted transaction's fences.
            # Keep the journal/hold intact and expose a retryable recovery error.
            try:
                update_status(status_path, expected_update_id=update_id,
                    phase="installing", runner_pid=None, heartbeat_at=None, retryable=True,
                    error_code="server_update_recovery_failed",
                    error_action="Retry recovery from the application update settings.",
                    message="The interrupted server update could not finish recovery. See server-update.log.",
                    finished_at=utc_now())
            except UpdateOwnershipLostError:
                pass
            return 1
        try:
            release_handoff = False
            with server_update_status_lock(status_path):
                current = _read_status_unlocked(status_path)
                if (
                    str(current.get("update_id") or "") != update_id
                    or str(current.get("phase") or "")
                    not in RUNNER_OWNED_ACTIVE_PHASES
                ):
                    return 1
                phase = str(current.get("phase") or "")
                if phase in {"installing", "restarting"} and current.get("_activation_recovery"):
                    from update_recovery import journal_present
                    if journal_present(Path(os.environ["AGENTS_SERVER_INSTALL_DIR"]).expanduser().resolve()):
                        _update_status_unlocked(status_path, current, runner_pid=None,
                            heartbeat_at=None, retryable=True,
                            error_code="server_update_recovery_pending",
                            message="The installer was interrupted. Its retained activation will be recovered.")
                        return 1
                if phase in {
                    "starting",
                    "checking",
                    "downloading",
                    "verifying",
                }:
                    # Keep the owning active status in place until its exact
                    # Hub fence is cleared. A clear failure therefore remains
                    # fail-closed instead of publishing a false terminal row.
                    clear_team_hub_maintenance(args)
                    release_handoff = bool(getattr(args, "execution_handoff_file", None))
                elif phase in {"installing", "restarting"} and \
                        team_hub_maintenance_fence_present(args):
                    # Once install.sh starts, only its verified rollback or
                    # successful candidate handoff may clear the fence. Keep
                    # the active row if recovery was not proven complete.
                    return 1
                # Installer preflight can fail after the public phase changed
                # to installing but before it creates a transaction. The same
                # exact old hold then needs cleanup; the helper refuses any
                # journal, takeover or changed native worker before releasing.
                release_handoff = release_handoff or bool(
                    current.get("_execution_handoff") and getattr(args, "execution_handoff_file", None))
                _update_status_unlocked(
                    status_path,
                    current,
                    phase="failed",
                    message=str(exc),
                    heartbeat_at=None,
                    runner_pid=None,
                    **({"error_code": "server_update_handoff_release_failed", "retryable": True,
                        "error_action": "Retry the update to finish releasing its execution hold."}
                       if release_handoff else {}),
                    finished_at=utc_now(),
                )
            # The sealed lease still owns admission after the failed status
            # is durable. Release outside the status flock: the authenticated
            # worker callback reads this same status while proving it is idle.
            if release_handoff:
                from update_handoff import release_existing_handoff
                try:
                    release_existing_handoff(
                        Path(os.environ["AGENTS_SERVER_INSTALL_DIR"]).expanduser().resolve(),
                        Path(args.execution_handoff_file),
                    )
                    with server_update_status_lock(status_path):
                        current = _read_status_unlocked(status_path)
                        if current.get("update_id") == update_id and current.get("phase") == "failed":
                            _update_status_unlocked(status_path, current, _execution_handoff=None,
                                error_code=None, error_action=None, retryable=True)
                except Exception:
                    with server_update_status_lock(status_path):
                        current = _read_status_unlocked(status_path)
                        if current.get("update_id") == update_id and current.get("phase") == "failed":
                            _update_status_unlocked(status_path, current, retryable=True,
                                error_code="server_update_handoff_release_failed",
                                error_action="Retry the update to finish releasing its execution hold.",
                                message="The update failed and its execution hold could not be released safely.")
                    raise
        except (UpdateOwnershipLostError, RuntimeError, OSError, ValueError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
