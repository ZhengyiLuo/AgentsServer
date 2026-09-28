#!/usr/bin/env python3
"""Observe a signed managed upgrade on disposable hosted native runners.

The real authenticated updater replaces the default installed service. Only
the unpublished npm tarball's HTTPS transport is replayed locally.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import shlex
import socket
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

from native_release_replay import replay_signed_npm_archive


ROOT = Path(__file__).resolve().parents[1]
PHASE = "host-validation"
INSTALLER_FAILURE_DIAGNOSTICS: dict | None = None
SELECTORS = ("AGENTS_SERVER_INSTALL_DIR", "AGENTS_SERVER_CONFIG_DIR", "AGENTS_SERVER_STATE_DIR",
             "AGENTSDOCK_STATE_DIR", "ZENITHBOT_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
             "AGENTS_SERVER_INSTANCE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


def need(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sanitized_installer_tail(stdout: bytes, stderr: bytes, secrets: list[str]) -> list[str]:
    """Return bounded fixture-only diagnostics without credential-bearing lines."""
    raw = (stdout[-32768:] + b"\n" + stderr[-32768:]).decode("utf-8", errors="replace")
    for secret in secrets:
        if secret:
            raw = raw.replace(secret, "[REDACTED]")
    safe = []
    for line in raw.splitlines():
        # Never echo environment dumps, auth headers, token-related lines or
        # potentially credential-bearing URLs, including installer-generated
        # onboarding links. Only fixed diagnostic text and owned paths remain.
        if re.search(r"(?i)authorization|bearer\s|token|password|secret|credential|api[_ -]?key|"
                     r"https?://|\b[A-Z_][A-Z0-9_]*=", line):
            safe.append("[credential, environment, or URL line withheld]")
            continue
        # Remove terminal control characters and bound pathological lines.
        safe.append("".join(char for char in line if char == "\t" or ord(char) >= 32)[:2000])
    return safe[-40:]


def command(arguments: list[str], *, timeout: int = 60, env: dict | None = None,
            allowed: tuple[int, ...] = (0,), diagnostic_home: Path | None = None) -> subprocess.CompletedProcess:
    # Installer and service output can contain the disposable token. Never
    # publish raw command output, even on a failed acceptance run.
    result = subprocess.run(arguments, capture_output=True, timeout=timeout, env=env)
    if result.returncode not in allowed and diagnostic_home is not None:
        global INSTALLER_FAILURE_DIAGNOSTICS
        secrets = []
        try:
            secrets.append(token(diagnostic_home))
        except (OSError, ValueError, RuntimeError):
            pass
        INSTALLER_FAILURE_DIAGNOSTICS = {
            "kind": "sanitized-disposable-installer-failure", "phase": PHASE,
            "exit_status": result.returncode,
            "tail": sanitized_installer_tail(result.stdout, result.stderr, secrets),
        }
        print(json.dumps(INSTALLER_FAILURE_DIAGNOSTICS), flush=True)
    need(result.returncode in allowed,
         f"Native command failed ({Path(arguments[0]).name}, status {result.returncode}); output withheld.")
    return result


def guard(args: argparse.Namespace) -> Path:
    env = os.environ
    need(env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
         and env.get("GITHUB_REPOSITORY") == "ZhengyiLuo/AgentsServer"
         and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
         and env.get("AGENTSDOCK_NATIVE_UPGRADE_ACCEPTANCE") == "true"
         and os.getuid() != 0, "This harness only operates on disposable hosted release runners.")
    need(platform.system() in {"Darwin", "Linux"}
         and (platform.system() != "Darwin" or platform.machine() == "arm64"),
         "Expected Linux or Apple silicon macOS.")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    need(Path(env.get("HOME", "")).resolve() == home, "The real disposable account home is required.")
    # Hosted Ubuntu can export its ordinary default XDG path.
    for key in SELECTORS:
        value = env.get(key)
        if key == "XDG_CONFIG_HOME" and platform.system() == "Linux" and value == str(home / ".config"):
            continue
        need(not value, "Custom installation/provider roots are not supported by this native test.")
    temporary = Path(env["RUNNER_TEMP"]).resolve()
    for path in (args.baseline, args.candidate, args.npm, args.work, args.report):
        need(path.is_absolute() and path.resolve().is_relative_to(temporary)
             and not path.is_symlink(), "All test artifacts must remain under the disposable runner directory.")
    need(not args.work.exists() and not args.report.exists(), "Use fresh work and report paths.")
    need(re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is not None, "Exact source SHA is required.")
    need(re.fullmatch(r"[0-9a-f]{40}", args.npm_source_sha) is not None, "Exact npm source SHA is required.")
    need(command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.decode().strip() == args.source_sha,
         "Harness checkout differs from the signed candidate source.")
    return home


def service_paths(home: Path) -> list[Path]:
    if platform.system() == "Darwin":
        return [home / "Library/LaunchAgents" / f"{name}.plist"
                for name in ("com.agentsdock.server", "com.agentsdock.gateway")]
    return [home / ".config/systemd/user" / name
            for name in ("agents-server.service", "agents-server-gateway.service")]


def clean_host(home: Path) -> None:
    paths = [home / ".local/share/agents-server", home / ".agentsdock", home / ".config/agents-server",
             home / ".zenithbot-agent", *service_paths(home)]
    need(all(not path.exists() and not path.is_symlink() for path in paths),
         "Runner already contains server data or native service definitions.")
    if platform.system() == "Darwin":
        command(["/bin/launchctl", "print", f"gui/{os.getuid()}"])
        for path in service_paths(home):
            result = command(["/bin/launchctl", "print", f"gui/{os.getuid()}/{path.stem}"], allowed=(0, 3, 5, 113))
            need(result.returncode != 0 and b"could not find service" in (result.stdout + result.stderr).lower(),
                 "Existing or unverifiable launchd service on runner.")
    else:
        command(["systemctl", "--user", "show-environment"])
        for name in ("agents-server.service", "agents-server-gateway.service", "zenithbot-agent.service"):
            result = command(["systemctl", "--user", "show", name, "--property=LoadState", "--value"], allowed=(0, 1))
            need(result.stdout.strip() == b"not-found", "Existing native systemd service on runner.")


def package(directory: Path, destination: Path, expected_version: str, expected_commit: str | None = None) -> dict:
    need(re.fullmatch(r"1\.0\.7-beta\.[1-9][0-9]*", expected_version) is not None, "Unexpected exact beta version.")
    manifest_path, signature = directory / "agents-server-manifest.json", directory / "agents-server-manifest.sig"
    raw = manifest_path.read_bytes()
    need(len(raw) < 8192 and signature.stat().st_size == 64, "Invalid signed manifest size.")
    command(["node", "-e", "const f=require('node:fs'),c=require('node:crypto');if(!c.verify(null,f.readFileSync(process.argv[1]),f.readFileSync(process.argv[3]),f.readFileSync(process.argv[2])))process.exit(1)",
             str(manifest_path), str(signature), str(ROOT / "release-public-key.pem")])
    manifest = json.loads(raw)
    need(manifest.get("schema") == 1 and manifest.get("version") == expected_version,
         "Signed package version differs from the requested exact version.")
    if expected_commit:
        need(manifest.get("commit") == expected_commit, "Signed candidate source commit differs.")
    archive = manifest["archive"]
    name = f"agents-server-{expected_version}.tar.gz"
    need(archive.get("name") == name and archive.get("url") ==
         f"https://github.com/ZhengyiLuo/AgentsServer/releases/download/v{expected_version}/{name}",
         "Unexpected package identity or download origin.")
    payload = (directory / name).read_bytes()
    need(len(payload) <= 200 * 1024 * 1024 and len(payload) == archive.get("size")
         and digest(payload) == archive.get("sha256"), "Package bytes differ from the production signature.")
    destination.mkdir(mode=0o700)
    with tarfile.open(directory / name) as tar:
        members = tar.getmembers()
        need(0 < len(members) <= 600 and sum(item.size for item in members) <= 200 * 1024 * 1024,
             "Unexpected package inventory size.")
        names = set()
        for item in members:
            path = Path(item.name)
            need(path.parts and path.parts[0] == f"agents-server-{expected_version}" and not path.is_absolute()
                 and ".." not in path.parts and item.name not in names and (item.isfile() or item.isdir())
                 and not item.mode & 0o6000, "Unsafe package member.")
            names.add(item.name)
        tar.extractall(destination, filter="data")
    source = destination / f"agents-server-{expected_version}"
    need((source / "VERSION").read_text().strip() == expected_version
         and (source / "release-public-key.pem").read_bytes() == (ROOT / "release-public-key.pem").read_bytes(),
         "Extracted runtime version or established trust root differs.")
    return {"source": source, "manifest_sha256": digest(raw), "archive_sha256": digest(payload),
            "version": expected_version, "archive": directory / name}


def npm_package(args: argparse.Namespace, candidate: dict) -> dict:
    manifest_path = args.npm / "agents-server-npm-manifest.json"
    signature = args.npm / "agents-server-npm-manifest.sig"
    raw, signed = manifest_path.read_bytes(), signature.read_bytes()
    need(len(raw) < 8192 and len(signed) == 64, "Invalid signed npm descriptor size.")
    command(["node", "-e", "const f=require('node:fs'),c=require('node:crypto');if(!c.verify(null,f.readFileSync(process.argv[1]),f.readFileSync(process.argv[3]),f.readFileSync(process.argv[2])))process.exit(1)",
             str(manifest_path), str(signature), str(ROOT / "release-public-key.pem")])
    manifest = json.loads(raw)
    need(manifest.get("schema") == 2 and manifest.get("distribution") == "npm"
         and manifest.get("version") == args.version and manifest.get("commit") == args.npm_source_sha
         and manifest.get("track") == "beta" and manifest.get("prerelease") is True
         and manifest.get("npm", {}).get("name") == "@agentsdock/server"
         and manifest["npm"].get("version") == args.version,
         "Signed npm descriptor differs from the exact candidate identity.")
    archive = manifest["archive"]
    name = f"server-{args.version}.tgz"
    need(archive.get("name") == name and archive.get("url") ==
         f"https://registry.npmjs.org/@agentsdock/server/-/{name}", "Unexpected signed npm archive origin.")
    payload = (args.npm / name).read_bytes()
    need(len(payload) <= 200 * 1024 * 1024 and len(payload) == archive.get("size")
         and digest(payload) == archive.get("sha256")
         and manifest["npm"].get("integrity") == "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode(),
         "Npm archive differs from its production-signed hashes.")
    # Compare package payloads without modifying or re-signing either archive.
    with tarfile.open(args.npm / name) as tar:
        members = tar.getmembers()
        need(0 < len(members) <= 600 and sum(item.size for item in members) <= 200 * 1024 * 1024,
             "Unexpected npm package inventory size.")
        names = set()
        runtime = set()
        for item in members:
            path = Path(item.name)
            need(path.parts and path.parts[0] == "package" and not path.is_absolute()
                 and ".." not in path.parts and item.name not in names and item.isfile()
                 and not item.mode & 0o6000, "Unsafe npm package member.")
            names.add(item.name)
            if item.name.startswith("package/server/"):
                relative = item.name.removeprefix("package/server/")
                runtime.add(relative)
                original = candidate["source"] / relative
                need(original.is_file() and not original.is_symlink()
                     and original.read_bytes() == tar.extractfile(item).read(),
                     "Npm runtime differs from the independently signed legacy candidate.")
        with tarfile.open(candidate["archive"]) as legacy:
            expected = {str(Path(*Path(item.name).parts[1:])) for item in legacy.getmembers() if item.isfile()}
        need(runtime == expected, "Npm and legacy runtime inventories differ.")
    return {"manifest": manifest, "manifest_sha256": digest(raw), "archive": args.npm / name,
            "envelope": {"manifest_base64": base64.b64encode(raw).decode(),
                         "signature_base64": base64.b64encode(signed).decode()}}


@contextmanager
def native_trust_environment(environment: dict, env: dict):
    """Temporarily provide fixture TLS trust to real services and detached updater."""
    # An empty default tmux server is owned by this fixture, outside the server
    # service's cgroup. Its global environment reaches the real detached runner.
    probe = command(["tmux", "list-sessions"], allowed=(0, 1))
    need(probe.returncode == 1 and re.search(rb"no server running|failed to connect|error connecting", probe.stderr),
         "The disposable runner already has a default tmux server.")
    saved = {}
    changed = []
    tmux_started = False
    if platform.system() == "Linux":
        manager = command(["systemctl", "--user", "show-environment"]).stdout.decode()
        saved = dict(line.split("=", 1) for line in manager.splitlines() if "=" in line)
    else:
        for name in environment:
            result = command(["/bin/launchctl", "getenv", name], allowed=(0, 1))
            if result.returncode == 0 and result.stdout.strip():
                saved[name] = result.stdout.decode().rstrip("\n")
    try:
        for name, value in environment.items():
            if platform.system() == "Linux":
                command(["systemctl", "--user", "set-environment", f"{name}={value}"])
            else:
                command(["/bin/launchctl", "setenv", name, value])
            changed.append(name)
        command(["tmux", "new-session", "-d", "-s", "agentsdock_native_tls_fixture", "sleep 7200"],
                env={**env, **environment})
        tmux_started = True
        command(["tmux", "set-option", "-g", "exit-empty", "off"])
        command(["tmux", "kill-session", "-t", "agentsdock_native_tls_fixture"])
        for name, value in environment.items():
            command(["tmux", "set-environment", "-g", name, value])
            need(command(["tmux", "show-environment", "-g", name]).stdout.decode().strip() == f"{name}={value}",
                 "Detached updater fixture trust was not installed.")
        yield
    finally:
        # This tmux daemon did not exist before the fixture. No real user panes
        # or provider conversations can be present on the guarded empty runner.
        cleanup_failed = False
        if tmux_started:
            try:
                command(["tmux", "kill-server"])
            except (OSError, subprocess.SubprocessError, RuntimeError):
                cleanup_failed = True
        for name in reversed(changed):
            try:
                if platform.system() == "Linux":
                    arguments = (["set-environment", f"{name}={saved[name]}"] if name in saved else ["unset-environment", name])
                    command(["systemctl", "--user", *arguments])
                else:
                    arguments = (["setenv", name, saved[name]] if name in saved else ["unsetenv", name])
                    command(["/bin/launchctl", *arguments])
            except (OSError, subprocess.SubprocessError, RuntimeError):
                cleanup_failed = True
        need(not cleanup_failed, "Native fixture trust cleanup failed after attempting every owned cleanup step.")
        if platform.system() == "Linux":
            restored = dict(line.split("=", 1) for line in command(["systemctl", "--user", "show-environment"]).stdout.decode().splitlines() if "=" in line)
            need(all(restored.get(name) == saved.get(name) for name in changed), "Native fixture trust cleanup failed.")
        else:
            for name in changed:
                result = command(["/bin/launchctl", "getenv", name], allowed=(0, 1))
                need(result.stdout.decode().rstrip("\n") == saved.get(name, ""), "Native fixture trust cleanup failed.")


def token(home: Path) -> str:
    path = home / ".config/agents-server/env"
    need(stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.getuid(),
         "Installer credential file has unexpected ownership or mode.")
    values = [line.split("=", 1)[1] for line in path.read_text().splitlines()
              if line.startswith("AGENTSDOCK_AGENT_TOKEN=")]
    need(len(values) == 1, "Expected one installer-issued authentication token.")
    parsed = shlex.split(values[0])
    need(len(parsed) == 1 and len(parsed[0]) >= 32, "Invalid installer-issued token.")
    return parsed[0]


def request(port: int, secret: str, path: str, body: dict | None = None, *, timeout: int = 10) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-AgentsDock-Token": secret, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def update_diagnostics(home: Path, secret: str, status: dict, *, error: bytes = b"") -> None:
    global INSTALLER_FAILURE_DIAGNOSTICS
    selected = {name: status.get(name) for name in
                ("phase", "stage", "message", "error_code", "error_action", "installed_version", "version", "reconciliation")}
    log = home / ".agentsdock/admin/server-update.log"
    log_tail = b""
    if log.is_file() and not log.is_symlink() and log.stat().st_uid == os.getuid():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 32768))
            log_tail = stream.read(32768)
    INSTALLER_FAILURE_DIAGNOSTICS = {
        "kind": "sanitized-managed-update-failure", "phase": PHASE,
        "tail": sanitized_installer_tail(json.dumps(selected).encode() + b"\n" + error[:32768], log_tail, [secret]),
    }
    print(json.dumps(INSTALLER_FAILURE_DIAGNOSTICS), flush=True)


def managed_upgrade(home: Path, install: Path, port: int, secret: str, before: dict, npm: dict, version: str) -> None:
    body = {**npm["envelope"], "expected_server_identity": before["server_identity"],
            "expected_server_instance_id": before["server_instance_id"]}
    status = {}
    try:
        status = request(port, secret, "/api/admin/update/ensure", body, timeout=120)
    except urllib.error.HTTPError as error:
        update_diagnostics(home, secret, status, error=error.read(32768))
        raise RuntimeError("The installed baseline rejected the real managed update request.") from None
    except (OSError, ValueError):
        update_diagnostics(home, secret, status)
        raise RuntimeError("The installed baseline did not acknowledge the real managed update request.") from None
    deadline = time.monotonic() + 900
    previous = None
    while time.monotonic() < deadline:
        try:
            status = request(port, secret, "/api/admin/update")
        except (OSError, ValueError):
            time.sleep(1)
            continue  # Real native service replacement temporarily closes HTTP.
        marker = (status.get("phase"), status.get("stage"))
        if marker != previous:
            print(json.dumps({"kind": "managed-update-progress", "phase": marker[0], "stage": marker[1]}), flush=True)
            previous = marker
        if status.get("phase") == "failed":
            update_diagnostics(home, secret, status)
            raise RuntimeError("The real managed updater reported failure.")
        if status.get("phase") == "complete" and status.get("installed_version") == version:
            need(not (install / ".activation-transaction").exists()
                 and not (install / ".execution-transaction").exists(),
                 "Managed update completion retained an activation or execution transaction.")
            return
        time.sleep(1)
    update_diagnostics(home, secret, status)
    raise RuntimeError("The real managed updater did not complete within its acceptance deadline.")


def health(port: int, secret: str, version: str) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            result = request(port, secret, "/api/health")
            if result.get("ok") is True and result.get("server_version") == version:
                need(bool(result.get("server_identity")) and bool(result.get("server_instance_id")),
                     "Native health omitted its server/process identity.")
                return result
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Native service did not reach authenticated health for the exact version.")


def services(home: Path, install: Path) -> list[Path]:
    files = [path for path in service_paths(home) if path.exists()]
    need(files and files[0] == service_paths(home)[0], "Worker service definition is missing.")
    for path in files:
        need(not path.is_symlink() and path.stat().st_uid == os.getuid(), "Unexpected service definition ownership.")
        raw = path.read_bytes()
        if platform.system() == "Darwin":
            value = plistlib.loads(raw)
            need(value.get("Label") == path.stem, "Unexpected native service label.")
            arguments = value.get("ProgramArguments", [])
        else:
            lines = [line[10:] for line in raw.decode().splitlines() if line.startswith("ExecStart=")]
            need(len(lines) == 1, "Expected one native worker executable.")
            arguments = shlex.split(lines[0])
            if arguments and arguments[0] == "/usr/bin/env":
                arguments = arguments[1:]
                while arguments and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", arguments[0]):
                    arguments = arguments[1:]
        need(arguments and arguments[0].startswith(str(install) + "/"),
             "Service does not use its permanent installed runtime.")
    return files


def stop(home: Path, install: Path) -> list[Path]:
    files = services(home, install)
    for path in reversed(files):
        if platform.system() == "Darwin":
            target = f"gui/{os.getuid()}/{path.stem}"
            command(["/bin/launchctl", "bootout", target], timeout=240, allowed=(0, 3, 5, 113))
            deadline = time.monotonic() + 185
            while True:
                result = command(["/bin/launchctl", "print", target], allowed=(0, 3, 5, 113))
                if result.returncode != 0:
                    need(b"could not find service" in (result.stdout + result.stderr).lower(), "Native service absence was not proven.")
                    break
                need(time.monotonic() < deadline, "Native service did not finish stopping.")
                time.sleep(0.5)
        else:
            command(["systemctl", "--user", "stop", path.name], timeout=240)
    return files


def start(files: list[Path]) -> None:
    for path in files:
        if platform.system() == "Darwin":
            command(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], timeout=60)
        else:
            command(["systemctl", "--user", "start", path.name], timeout=60)


def verify_runtime(install: Path, candidate: dict) -> int:
    runtime = install / "releases" / candidate["version"]
    need((install / "current").resolve() == runtime, "Current link does not identify the exact candidate.")
    count = 0
    with tarfile.open(candidate["archive"]) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = Path(*Path(member.name).parts[1:])
            actual = runtime / relative
            expected = archive.extractfile(member).read()
            need(actual.is_file() and not actual.is_symlink() and actual.read_bytes() == expected,
                 "Installed file bytes differ from the signed candidate archive.")
            mode = member.mode & 0o777
            if str(relative) == "agent_server.py":
                mode = 0o755  # install.sh's explicit entrypoint chmod
            need(stat.S_IMODE(actual.stat().st_mode) == mode, "Installed file mode differs from the package contract.")
            count += 1
    need((runtime / "queue_projection.py").is_file(), "New queue recovery module was not installed.")
    return count


def run(args: argparse.Namespace) -> dict:
    global PHASE
    home = guard(args)
    clean_host(home)
    args.work.mkdir(mode=0o700)
    PHASE = "signed-package-validation"
    candidate = package(args.candidate, args.work / "candidate", args.version, args.source_sha)
    baseline = package(args.baseline, args.work / "baseline", args.baseline_version)
    npm = npm_package(args, candidate)
    install = home / ".local/share/agents-server"
    state = home / ".agentsdock"
    env = {name: value for name, value in os.environ.items() if name in
           {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM",
            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(UV_CACHE_DIR=str(args.work / "uv-cache"), NO_COLOR="1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    def install_package(package: dict) -> None:
        command(["/bin/bash", str(package["source"] / "install.sh"), "--release-version", package["version"],
                 "--port", str(port), "--bind", "127.0.0.1", "--non-interactive"], env=env, timeout=1500,
                diagnostic_home=home)
    archive = npm["manifest"]["archive"]
    PHASE = "native-transport-fixture-setup"
    with replay_signed_npm_archive(npm["archive"], archive["url"], args.work / "https-replay",
                                   expected_sha256=archive["sha256"], expected_size=archive["size"]) as replay:
        with native_trust_environment(replay.environment, env):
            env.update(replay.environment)
            PHASE = "baseline-install"
            install_package(baseline)
            secret = token(home)
            first = health(port, secret, args.baseline_version)
            services(home, install)
            workspace = args.work / "synthetic-workspace"
            workspace.mkdir()
            PHASE = "synthetic-state-seeding"
            session = request(port, secret, "/api/sessions", {"title": "Synthetic upgrade preservation fixture",
                              "auto_title_enabled": False, "cwd": str(workspace), "backend": "codex", "import_history": False,
                              "codex_approval_policy": "on-request", "codex_sandbox_mode": "workspace-write"})["session"]
            identifier = session["id"]
            need(re.fullmatch(r"[A-Za-z0-9_-]+", identifier) is not None, "Unexpected fixture session ID.")
            files = stop(home, install)
            path = state / "sessions" / identifier / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = path.read_bytes() if path.exists() else b""
            sequence = max([json.loads(line).get("seq", 0) for line in previous.splitlines()] or [0]) + 1
            event = {"seq": sequence, "id": "native-upgrade-synthetic-event", "session_id": identifier,
                     "type": "assistant_message", "ts": "2026-09-28T00:00:00Z",
                     "text": "Synthetic persisted history fixture; no provider conversation was run."}
            with path.open("ab") as stream:
                stream.write(json.dumps(event).encode() + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            persisted = path.read_bytes()
            start(files)
            before = health(port, secret, args.baseline_version)
            detail = request(port, secret, f"/api/sessions/{identifier}?limit=1000&tail=false")
            need(any(item.get("id") == event["id"] for item in detail.get("events", [])), "Seeded history is not exposed through the baseline API.")
            keys = ("id", "title", "cwd", "backend", "codex_provider", "session_id", "codex_thread_id", "model", "effort")
            expected_session = {key: detail["session"].get(key) for key in keys}
            # Exercise both previously supported installation modes across the native
            # matrix, with an existing populated installation rather than empty roots.
            install.chmod(int(args.legacy_root_mode, 8))
            need(stat.S_IMODE(install.stat().st_mode) == int(args.legacy_root_mode, 8), "Baseline root mode was not applied.")
            PHASE = "existing-installation-upgrade"
            managed_upgrade(home, install, port, secret, before, npm, args.version)
            PHASE = "candidate-state-verification"
            after = health(port, secret, args.version)
            need(after["server_identity"] == before["server_identity"] == first["server_identity"]
                 and token(home) == secret and after["server_instance_id"] != before["server_instance_id"],
                 "Upgrade failed to preserve authentication/identity or replace the runtime process.")
            runtime_count = verify_runtime(install, candidate)
            for _ in range(60):
                detail = request(port, secret, f"/api/sessions/{identifier}?limit=1000&tail=false")
                if detail.get("queue_recovery", {}).get("ready") is True:
                    break
                time.sleep(0.5)
            need(detail.get("queue_recovery", {}).get("ready") is True, "Migrated chat did not complete its own queue recovery.")
            need({key: detail["session"].get(key) for key in keys} == expected_session
                 and path.read_bytes().startswith(persisted)
                 and any(item.get("id") == event["id"] for item in detail.get("events", [])),
                 "Upgrade changed the persisted session or synthetic history.")
            # Retire staging, then restart both actual native services; the installed
            # runtime must be independent of extracted package sources.
            baseline["source"].parent.rename(args.work / "baseline-retired")
            candidate["source"].parent.rename(args.work / "candidate-retired")
    need(replay.receipt.get("successful_gets", 0) >= 1
         and replay.receipt.get("successful_bytes", 0) >= archive["size"]
         and replay.receipt.get("hosts_restored") is True and replay.receipt.get("fixture_keys_removed") is True,
         "Signed tarball replay was not consumed or its host mapping was not restored.")
    PHASE = "candidate-native-restart"
    start(stop(home, install))
    restarted = health(port, secret, args.version)
    need(restarted["server_instance_id"] != after["server_instance_id"]
         and restarted["server_identity"] == before["server_identity"] and token(home) == secret,
         "Installed candidate did not survive a real native service restart.")
    verify_runtime(install, candidate)
    need(path.read_bytes().startswith(persisted), "Restart changed persisted synthetic history.")
    PHASE = "complete"
    return {"schema": 1, "kind": "signed-native-managed-upgrade", "status": "passed",
            "source_sha": args.source_sha, "npm_source_sha": args.npm_source_sha, "baseline_version": args.baseline_version, "version": args.version,
            "platform": platform.system(), "run_id": os.environ["GITHUB_RUN_ID"],
            "baseline_install_root_mode": args.legacy_root_mode,
            "candidate_manifest_sha256": candidate["manifest_sha256"], "candidate_archive_sha256": candidate["archive_sha256"],
            "baseline_archive_sha256": baseline["archive_sha256"], "runtime_files_compared": runtime_count,
            "npm_manifest_sha256": npm["manifest_sha256"], "transport_fixture": replay.receipt,
            "observations": ["production-signatures-verified", "real-default-native-installation",
                             "authenticated-managed-update-api", "real-native-admission-and-replacement",
                             "fixture-trust-and-host-mapping-restored", "existing-installation-upgraded", "identity-and-token-preserved",
                             "api-created-session-preserved", "synthetic-persisted-history-preserved",
                             "per-chat-queue-recovery-ready", "exact-runtime-files-and-modes",
                             "installed-runtime-independent-of-staging-after-native-restart"],
            "not_exercised": ["public-npm-registry-download", "npm-cli", "desktop-updater", "provider-conversation",
                              "busy-or-queued-work", "rollback", "logout-or-reboot"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "npm", "work", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--npm-source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--baseline-version", default="1.0.7-beta.11")
    parser.add_argument("--legacy-root-mode", choices=("0755", "0750"), required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:
        # Fixed class only: exception text can contain URLs/paths or command
        # output. A failed native run is never a fabricated passing receipt.
        result = {"status": "failed", "phase": PHASE, "error_type": type(error).__name__}
        if type(error) is RuntimeError:
            result["reason"] = str(error)  # Only fixed messages from need()/health().
        if INSTALLER_FAILURE_DIAGNOSTICS is not None:
            result["installer_diagnostics"] = INSTALLER_FAILURE_DIAGNOSTICS
        # PHASE only advances after guard(), clean_host() and creation of the
        # disposable work directory. A rejected host never writes a report.
        if PHASE != "host-validation":
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        raise SystemExit(1) from None
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
