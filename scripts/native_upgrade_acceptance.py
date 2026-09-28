#!/usr/bin/env python3
"""Observe a signed legacy -> candidate install on disposable hosted runners.

This exercises the real installer and default native services. It does not
exercise the managed-update API, a desktop updater, or a provider conversation.
"""
from __future__ import annotations

import argparse
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
    for path in (args.baseline, args.candidate, args.work, args.report):
        need(path.is_absolute() and path.resolve().is_relative_to(temporary)
             and not path.is_symlink(), "All test artifacts must remain under the disposable runner directory.")
    need(not args.work.exists() and not args.report.exists(), "Use fresh work and report paths.")
    need(re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is not None, "Exact source SHA is required.")
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


def request(port: int, secret: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


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
    install_package(candidate)
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
    PHASE = "candidate-native-restart"
    start(stop(home, install))
    restarted = health(port, secret, args.version)
    need(restarted["server_instance_id"] != after["server_instance_id"]
         and restarted["server_identity"] == before["server_identity"] and token(home) == secret,
         "Installed candidate did not survive a real native service restart.")
    verify_runtime(install, candidate)
    need(path.read_bytes().startswith(persisted), "Restart changed persisted synthetic history.")
    PHASE = "complete"
    return {"schema": 1, "kind": "signed-native-direct-installer-upgrade", "status": "passed",
            "source_sha": args.source_sha, "baseline_version": args.baseline_version, "version": args.version,
            "platform": platform.system(), "run_id": os.environ["GITHUB_RUN_ID"],
            "baseline_install_root_mode": args.legacy_root_mode,
            "candidate_manifest_sha256": candidate["manifest_sha256"], "candidate_archive_sha256": candidate["archive_sha256"],
            "baseline_archive_sha256": baseline["archive_sha256"], "runtime_files_compared": runtime_count,
            "observations": ["production-signatures-verified", "real-default-native-installation",
                             "existing-installation-upgraded", "identity-and-token-preserved",
                             "api-created-session-preserved", "synthetic-persisted-history-preserved",
                             "per-chat-queue-recovery-ready", "exact-runtime-files-and-modes",
                             "installed-runtime-independent-of-staging-after-native-restart"],
            "not_exercised": ["managed-update-api", "desktop-updater", "provider-conversation",
                              "busy-or-queued-work", "rollback", "logout-or-reboot"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "work", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
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
