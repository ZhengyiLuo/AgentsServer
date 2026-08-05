import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / "install.sh"
UNINSTALLER = ROOT / "uninstall.sh"
DEPLOYER = ROOT / "deploy.sh"
PACKAGER = ROOT / "scripts" / "package_release.py"


class InstallerContractTests(unittest.TestCase):
    def test_runtime_includes_and_verifies_websocket_support(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        dependencies = project["project"]["dependencies"]
        self.assertTrue(any(item.startswith("websockets") for item in dependencies))
        self.assertTrue(any(item.startswith("croniter") for item in dependencies))
        self.assertTrue(any(item.startswith("python-dateutil") for item in dependencies))
        self.assertTrue(any(item.startswith("tzdata") for item in dependencies))
        self.assertIn("-c 'import websockets'", INSTALLER.read_text())
        self.assertIn("import croniter, dateutil", INSTALLER.read_text())

    def test_shell_syntax_is_valid(self):
        for script in (INSTALLER, UNINSTALLER, DEPLOYER):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_direct_deploy_includes_scheduler_runtime(self):
        source = DEPLOYER.read_text()
        self.assertIn('"$SCRIPT_DIR/agentsdock_jobs.py"', source)
        self.assertIn('"$SCRIPT_DIR/agentsdock_publish.py"', source)
        self.assertIn('"$SCRIPT_DIR/codex_app_server.py"', source)
        self.assertIn("import croniter, cryptography, dateutil, tzdata", source)
        self.assertIn("python-dateutil>=2.9,<3", source)
        self.assertIn("'$REMOTE_SERVER_DIR/codex_app_server.py'", source)

    def test_installer_and_release_archive_include_runtime_tools_and_uninstaller(self):
        installer_source = INSTALLER.read_text()
        packager_source = PACKAGER.read_text()
        self.assertIn(
            "RELEASE_FILES=(agent_server.py agentsdock_jobs.py agentsdock_publish.py codex_app_server.py",
            installer_source,
        )
        self.assertIn('"$STAGE_DIR/agentsdock_publish.py"', installer_source)
        self.assertIn('"$STAGE_DIR/codex_app_server.py"', installer_source)
        self.assertIn('"$STAGE_DIR/uninstall.sh"', installer_source)
        self.assertIn('"agentsdock_publish.py"', packager_source)
        self.assertIn('"codex_app_server.py"', packager_source)
        self.assertIn('"uninstall.sh"', packager_source)

        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(PACKAGER), "--output", temporary],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            version = (ROOT / "VERSION").read_text().strip()
            archive_path = Path(temporary) / f"agents-server-{version}.tar.gz"
            manifest = json.loads(
                (Path(temporary) / "agents-server-manifest.json").read_text()
            )
            with tarfile.open(archive_path, "r:gz") as archive:
                members = set(archive.getnames())
                publisher = archive.getmember(
                    f"agents-server-{version}/agentsdock_publish.py"
                )
                uninstaller = archive.getmember(
                    f"agents-server-{version}/uninstall.sh"
                )
            self.assertIn(
                f"agents-server-{version}/agentsdock_publish.py",
                members,
            )
            self.assertIn(
                f"agents-server-{version}/codex_app_server.py",
                members,
            )
            self.assertIn(f"agents-server-{version}/uninstall.sh", members)
            self.assertNotEqual(publisher.mode & 0o111, 0)
            self.assertNotEqual(uninstaller.mode & 0o111, 0)
            self.assertEqual(
                manifest["track"],
                "beta" if "-" in version.split("+", 1)[0] else "stable",
            )
            self.assertEqual(manifest["prerelease"], manifest["track"] == "beta")

    def test_installer_preserves_state_and_emits_private_result(self):
        source = INSTALLER.read_text()
        self.assertIn('STATE_ROOT="$HOME/.agentsdock"', source)
        self.assertIn('AGENTSDOCK_STATE_DIR=$STATE_ROOT', source)
        self.assertIn('LEGACY_STATE_ROOT="$HOME/.zenithbot-agent"', source)
        self.assertIn('mv "$LEGACY_STATE_ROOT" "$STATE_ROOT"', source)
        self.assertIn("AGENTSDOCK_SETUP_RESULT=", source)
        self.assertIn("AGENTSDOCK_AGENT_TOKEN", source)
        self.assertNotIn("AGENTS_SERVER_ADMIN_TOKEN", source)
        self.assertIn('RELEASES_ROOT="$INSTALL_ROOT/releases"', source)
        self.assertIn('PREVIOUS_LINK="$INSTALL_ROOT/previous"', source)
        self.assertIn('REPLACED_DIR="$RELEASES_ROOT/$RELEASE_VERSION-replaced-', source)
        self.assertIn("ManagedOOMPreference=avoid", source)
        self.assertLess(source.index('OLD_TARGET=""'), source.index('mv "$STAGE_DIR" "$RELEASE_DIR"'))
        self.assertIn("rolling back", source)
        self.assertNotIn('echo "  ${COLOR_BOLD}Access token', source)
        self.assertIsNone(re.search(r"(?m)^\s*sudo\b", source))

    def test_help_does_not_modify_the_machine(self):
        result = subprocess.run(
            ["bash", str(INSTALLER), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installs or updates AgentsServer", result.stdout)

    def test_same_version_reinstall_keeps_the_replaced_runtime_for_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            fake_bin.mkdir()
            home.mkdir()
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                "AGENTSDOCK_STATE_DIR": str(root / "state"),
            }

            for attempt in range(2):
                result = subprocess.run(
                    ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if attempt == 0:
                    (install_root / "current" / "old-runtime-marker").write_text("old\n")

            previous = install_root / "previous"
            current = install_root / "current"
            self.assertTrue(previous.is_symlink())
            self.assertTrue((previous / "old-runtime-marker").is_file())
            self.assertFalse((current / "old-runtime-marker").exists())
            self.assertNotEqual(previous.resolve(), current.resolve())

    def test_dependency_sync_ignores_an_inherited_external_uv_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            external_environment = root / "unrelated-project-venv"
            sync_target_log = root / "uv-sync-target"
            fake_bin.mkdir()
            home.mkdir()
            external_environment.mkdir()
            sentinel = external_environment / "do-not-touch"
            sentinel.write_text("unrelated workspace\n")
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
target="${UV_PROJECT_ENVIRONMENT:-$project/.venv}"
printf '%s\n' "$target" > "$FAKE_UV_SYNC_TARGET_LOG"
mkdir -p "$target/bin"
printf '#!/bin/sh\nexit 0\n' > "$target/bin/python"
chmod 755 "$target/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                "AGENTSDOCK_STATE_DIR": str(root / "state"),
                "FAKE_UV_SYNC_TARGET_LOG": str(sync_target_log),
                "UV_PROJECT_ENVIRONMENT": str(external_environment),
                "VIRTUAL_ENV": str(external_environment),
            }

            result = subprocess.run(
                ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(), "unrelated workspace\n")
            self.assertEqual(list(external_environment.iterdir()), [sentinel])
            sync_target = Path(sync_target_log.read_text().strip())
            self.assertEqual(sync_target.name, ".venv")
            self.assertEqual(sync_target.parent.parent, install_root / "releases")
            self.assertTrue((install_root / "current" / ".venv" / "bin" / "python").is_file())

    def test_dependency_sync_rejects_a_missing_staged_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", "#!/bin/sh\nexit 0\n")

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not create the isolated release runtime", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_default_legacy_state_is_moved_and_linked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home.mkdir()
            legacy = home / ".zenithbot-agent"
            legacy.mkdir()
            (legacy / "sessions.json").write_text('{"kept": true}\n')
            legacy_runtime = home / "Zenithbot"
            legacy_runtime.mkdir()
            preserved_token = "legacy_token_abcdefghijklmnopqrstuvwxyz012345"
            (legacy_runtime / ".env").write_text(
                f"ZENITHDOCK_AGENT_TOKEN={preserved_token}\n"
                "NOTION_TOKEN=keep-private-runtime-setting\n"
                f"PATH={fake_bin}:/legacy/runtime/bin:/usr/bin:/bin\n"
            )
            legacy_service_dir = home / ".config" / "systemd" / "user"
            legacy_service_dir.mkdir(parents=True)
            (legacy_service_dir / "zenithbot-agent.service").write_text(
                "[Service]\nEnvironmentFile=%h/Zenithbot/.env\n"
            )
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENTS_SERVER_INSTALL_DIR": str(root / "install"),
                "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
            }
            for name in ("AGENTSDOCK_STATE_DIR", "AGENTS_SERVER_STATE_DIR", "ZENITHBOT_AGENT_DIR"):
                environment.pop(name, None)

            result = subprocess.run(
                ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            canonical = home / ".agentsdock"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.resolve(), canonical.resolve())
            self.assertEqual((canonical / "sessions.json").read_text(), '{"kept": true}\n')
            installed_env = (root / "config" / "env").read_text()
            self.assertIn(f"AGENTSDOCK_AGENT_TOKEN={preserved_token}\n", installed_env)
            self.assertIn("NOTION_TOKEN=keep-private-runtime-setting\n", installed_env)
            self.assertIn("/legacy/runtime/bin", installed_env)
            self.assertNotIn("ZENITHDOCK_AGENT_TOKEN=", installed_env)
            self.assertIn(f'"access_token":"{preserved_token}"', result.stdout)

    def test_broken_tmux_is_optional_and_installation_still_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            config_root = root / "config"
            state_root = root / "state"
            config_root.mkdir()
            state_root.mkdir()
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            token = "preserved_token_abcdefghijklmnopqrstuvwxyz0123456789"
            (config_root / "env").write_text(f"AGENTSDOCK_AGENT_TOKEN={token}\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_fake_uv(fake_bin)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Optional prerequisite unavailable: tmux", result.stderr)
            self.assertIn("sudo apt install tmux", result.stderr)
            self.assertIn("everything else about AgentsServer works without it", result.stderr)
            self.assertTrue((install_root / "current" / "agent_server.py").is_file())
            self.assertEqual((state_root / "sessions.json").read_text(), '{"keep": true}\n')
            human_output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith("AGENTSDOCK_SETUP_RESULT=")
            )
            self.assertNotIn(token, human_output)
            self.assertNotIn("Access token", result.stdout)
            self.assertEqual(result.stdout.count(token), 1)

    def test_preflight_reports_every_missing_platform_prerequisite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home.mkdir()
            self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux", "curl"))
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "wget", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 1\n")
            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": str(fake_bin),
                    "AGENTS_SERVER_INSTALL_DIR": str(root / "install"),
                    "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                    "AGENTSDOCK_STATE_DIR": str(root / "state"),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Optional prerequisite unavailable: tmux", result.stderr)
            self.assertIn("Unavailable prerequisite: curl or wget", result.stderr)
            self.assertIn("Unavailable prerequisite: systemctl --user session", result.stderr)
            final_error = result.stderr.strip().splitlines()[-1]
            self.assertNotIn("tmux", final_error)
            self.assertIn("Missing prerequisites: curl or wget, systemctl --user session.", final_error)
            self.assertFalse((root / "install").exists())
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "state").exists())

    def test_explicit_port_is_pinned_without_allow_port_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_fake_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            try:
                self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
                environment.update({
                    "AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                })
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(INSTALLER),
                        "--port",
                        str(port),
                        "--non-interactive",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                listener.close()

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"AgentsServer {self.release_version()} did not become healthy", result.stderr)
            self.assertNotIn("Selecting port", result.stderr)
            self.assertNotIn("AGENTSDOCK_SETUP_RESULT=", result.stdout)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_explicit_port_fallback_is_opt_in_and_persists_selected_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            config_root = root / "config"
            config_root.mkdir()
            token = "preserved_token_abcdefghijklmnopqrstuvwxyz0123456789"
            (config_root / "env").write_text(f"AGENTSDOCK_AGENT_TOKEN={token}\n")
            self.write_fake_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            try:
                self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
                environment.update({
                    "AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                })
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(INSTALLER),
                        "--port",
                        str(port),
                        "--allow-port-fallback",
                        "--non-interactive",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                listener.close()

            selected_port = port + 1
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"Selecting port {selected_port} instead",
                result.stderr,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            installed_env = (config_root / "env").read_text()
            self.assertIn(f"AGENTSDOCK_AGENT_PORT={selected_port}\n", installed_env)
            self.assertNotIn(f"AGENTSDOCK_AGENT_PORT={port}\n", installed_env)
            service = (home / ".config" / "systemd" / "user" / "agents-server.service").read_text()
            self.assertIn(f"--port {selected_port}", service)
            self.assertIn("ManagedOOMPreference=avoid", service)
            result_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("AGENTSDOCK_SETUP_RESULT=")
            )
            payload = json.loads(result_line.removeprefix("AGENTSDOCK_SETUP_RESULT="))
            self.assertTrue(payload["server_url"].endswith(f":{selected_port}"))
            self.assertEqual(payload["access_token"], token)
            human_output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith("AGENTSDOCK_SETUP_RESULT=")
            )
            self.assertNotIn(token, human_output)
            self.assertNotIn("Access token", result.stdout)

    def test_newly_started_unhealthy_server_rolls_back_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_fake_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            listener.close()
            self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
            environment.update({
                "AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive", "--port", str(port), "--allow-port-fallback"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Selecting port", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_uv_bootstrap_curl_is_bounded_and_reports_recovery_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            curl_log = root / "curl.log"
            self.write_executable(fake_bin / "curl", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
echo 'curl: simulated network timeout' >&2
exit 28
""")
            self.write_executable(fake_bin / "wget", "#!/bin/sh\nexit 127\n")
            environment.update({
                "FAKE_CURL_LOG": str(curl_log),
                "AGENTS_SERVER_DOWNLOAD_CONNECT_TIMEOUT_SECONDS": "1",
                "AGENTS_SERVER_DOWNLOAD_TIMEOUT_SECONDS": "2",
                "AGENTS_SERVER_DOWNLOAD_RETRIES": "2",
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            invocation = curl_log.read_text()
            self.assertIn("--connect-timeout 1", invocation)
            self.assertIn("--max-time 2", invocation)
            self.assertIn("--retry 2", invocation)
            self.assertIn("--retry-delay 1", invocation)
            self.assertIn("Could not download uv", result.stderr)
            self.assertIn("Check DNS, proxy, firewall, and outbound HTTPS access", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_uv_bootstrap_wget_fallback_is_bounded_and_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            wget_log = root / "wget.log"
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "wget", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
printf '%s\n' "$*" >> "$FAKE_WGET_LOG"
echo 'wget: simulated network timeout' >&2
exit 4
""")
            environment.update({
                "FAKE_WGET_LOG": str(wget_log),
                "AGENTS_SERVER_DOWNLOAD_TIMEOUT_SECONDS": "2",
                "AGENTS_SERVER_DOWNLOAD_RETRIES": "2",
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            invocation = wget_log.read_text()
            self.assertIn("--timeout=2", invocation)
            self.assertIn("--tries=3", invocation)
            self.assertIn("--waitretry=1", invocation)
            self.assertIn("Could not download uv", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_dependency_sync_streams_progress_and_times_out_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            descendant_marker = root / "uv-descendant-survived"
            self.write_executable(fake_bin / "curl", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
exit 22
""")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
echo 'FAKE_UV_VISIBLE_PROGRESS'
(
  /bin/sleep 2
  echo survived > "$FAKE_DESCENDANT_MARKER"
) &
wait
""")
            environment.update({
                "AGENTS_SERVER_DEPENDENCY_TIMEOUT_SECONDS": "1",
                "AGENTS_SERVER_INSTALL_HEARTBEAT_SECONDS": "1",
                "FAKE_DESCENDANT_MARKER": str(descendant_marker),
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            # If timeout cleanup killed only the uv parent, its worker would
            # outlive setup and create this marker two seconds after launch.
            time.sleep(1.5)

            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertIn("FAKE_UV_VISIBLE_PROGRESS", result.stdout)
            self.assertIn("Still working on dependency resolution", result.stdout)
            self.assertIn("dependency resolution timed out after 1s.", result.stderr)
            self.assertIn("Review the uv output above.", result.stderr)
            self.assertIn("the active release was not changed.", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())
            self.assertEqual(list((install_root / "releases").glob(".staging-*")), [])
            self.assertFalse(descendant_marker.exists())
            self.assertFalse((install_root / ".install-lock").exists())

    def test_active_install_lock_rejects_a_second_installer_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            lock = install_root / ".install-lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n")
            before = self.snapshot_trees(install_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another AgentsServer installation is already running", result.stderr)
            self.assertIn("cancel it from AgentsDock before retrying", result.stderr)
            self.assertEqual(self.snapshot_trees(install_root), before)

    def test_unavailable_systemd_user_domain_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            config_root = root / "config"
            state_root = root / "state"
            fake_bin.mkdir()
            home.mkdir()
            release = install_root / "releases" / "0.0.9"
            release.mkdir(parents=True)
            (release / "runtime-marker").write_text("keep runtime\n")
            (install_root / "current").symlink_to(release, target_is_directory=True)
            config_root.mkdir()
            (config_root / "env").write_text("AGENTSDOCK_AGENT_TOKEN=keep-token\n")
            state_root.mkdir()
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux", "curl"))
            self.write_executable(fake_bin / "systemctl", """#!/bin/sh
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then
  echo 'Failed to connect to bus: No medium found' >&2
  exit 1
fi
exit 97
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": str(fake_bin),
                "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                "AGENTS_SERVER_CONFIG_DIR": str(config_root),
                "AGENTSDOCK_STATE_DIR": str(state_root),
            }
            before = self.snapshot_trees(install_root, config_root, state_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            final_error = result.stderr.strip().splitlines()[-1]
            self.assertIn("systemctl --user session", final_error)
            self.assertIn("systemctl --user show-environment", final_error)
            self.assertIn("no state, release, configuration, or service changes were made.", final_error)
            self.assertEqual(self.snapshot_trees(install_root, config_root, state_root), before)

    def test_saved_custom_path_is_used_for_prerequisite_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            bootstrap_bin = root / "bootstrap-bin"
            custom_bin = root / "custom-bin"
            install_root = root / "install"
            config_root = root / "config"
            bootstrap_bin.mkdir()
            custom_bin.mkdir()
            home.mkdir()
            config_root.mkdir()
            self.prepare_preflight_path(bootstrap_bin, os_name="Linux", commands=())
            expected_tmux = custom_bin / "tmux"
            systemctl_log = root / "systemctl.log"
            (config_root / "env").write_text(f"PATH={custom_bin}\n")
            self.write_executable(expected_tmux, "#!/bin/sh\nexit 0\n")
            self.write_executable(custom_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(custom_bin / "systemctl", """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then
  [ "$(command -v tmux)" = "$EXPECTED_TMUX" ] || exit 91
fi
exit 0
""")
            self.write_executable(custom_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": str(bootstrap_bin),
                "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                "AGENTS_SERVER_CONFIG_DIR": str(config_root),
                "AGENTSDOCK_STATE_DIR": str(root / "state"),
                "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                "EXPECTED_TMUX": str(expected_tmux),
            }

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            installed_path = next(
                line.removeprefix("PATH=")
                for line in (config_root / "env").read_text().splitlines()
                if line.startswith("PATH=")
            )
            self.assertEqual(installed_path.split(":", 1)[0], str(custom_bin))
            self.assertIn("--user show-environment", systemctl_log.read_text().splitlines())

    def test_uninstall_removes_runtime_and_config_but_preserves_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(install_root.exists())
            self.assertFalse(config_root.exists())
            self.assertFalse(service_file.exists())
            self.assertTrue((state_root / "sessions.json").is_file())
            self.assertIn("Preserved chat history", result.stdout)

    def test_uninstall_purge_state_requires_interactive_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes", "--purge-state"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to purge state without an interactive terminal", result.stderr)
            self.assertIn("--yes never bypasses", result.stderr)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )

    def test_uninstall_refuses_broad_and_overlapping_managed_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            home.mkdir()
            fake_bin.mkdir()
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            sentinel = home / "do-not-delete"
            sentinel.write_text("preserved\n")
            base_environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                "AGENTSDOCK_STATE_DIR": str(root / "state"),
            }

            broad_result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env={**base_environment, "AGENTS_SERVER_INSTALL_DIR": str(home)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(broad_result.returncode, 2)
            self.assertIn("Refusing unsafe AGENTS_SERVER_INSTALL_DIR target", broad_result.stderr)
            self.assertEqual(sentinel.read_text(), "preserved\n")

            install_root = root / "managed"
            config_root = install_root / "config"
            install_root.mkdir()
            config_root.mkdir()
            overlap_sentinel = install_root / "do-not-delete"
            overlap_sentinel.write_text("preserved\n")
            overlap_result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env={
                    **base_environment,
                    "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                    "AGENTS_SERVER_CONFIG_DIR": str(config_root),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(overlap_result.returncode, 2)
            self.assertIn("Refusing overlapping install, configuration, and state roots", overlap_result.stderr)
            self.assertEqual(overlap_sentinel.read_text(), "preserved\n")

    def test_uninstall_refuses_active_install_lock_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )
            lock = install_root / ".install-lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n")
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install/update is active", result.stderr)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )

    def test_uninstall_stop_failure_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root, stop_failure=True)
            )
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not stop agents-server.service; no files were removed", result.stderr)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )

    def test_darwin_restart_waits_for_bootout_and_retries_transient_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_darwin_environment(root)
            launchctl_state = root / "launchctl-state"
            launchctl_log = root / "launchctl.log"
            transient_done = root / "transient-done"
            launchctl_state.write_text("loaded\n")
            self.write_executable(fake_bin / "launchctl", """#!/bin/sh
command="$1"
case "$command" in
  print)
    state="$(cat "$FAKE_LAUNCHCTL_STATE")"
    printf 'print:%s\n' "$state" >> "$FAKE_LAUNCHCTL_LOG"
    case "$state" in
      loaded) exit 0 ;;
      removing:*)
        remaining="${state#removing:}"
        if [ "$remaining" -gt 0 ]; then
          printf 'removing:%s\n' "$((remaining - 1))" > "$FAKE_LAUNCHCTL_STATE"
          exit 0
        fi
        printf 'absent\n' > "$FAKE_LAUNCHCTL_STATE"
        exit 3
        ;;
      *) exit 3 ;;
    esac
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    printf 'removing:2\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
  bootstrap)
    printf 'bootstrap\n' >> "$FAKE_LAUNCHCTL_LOG"
    if [ ! -f "$FAKE_TRANSIENT_DONE" ]; then
      : > "$FAKE_TRANSIENT_DONE"
      echo 'Bootstrap failed: 5: Input/output error' >&2
      echo 'Try re-running the command as root for richer errors.' >&2
      exit 5
    fi
    printf 'loaded\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
esac
exit 2
""")
            environment.update({
                "FAKE_LAUNCHCTL_STATE": str(launchctl_state),
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                "FAKE_TRANSIENT_DONE": str(transient_done),
            })

            result = subprocess.run(
                ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = launchctl_log.read_text().splitlines()
            self.assertLess(calls.index("bootout"), calls.index("print:removing:2"))
            self.assertLess(calls.index("print:removing:0"), calls.index("bootstrap"))
            self.assertEqual(calls.count("bootstrap"), 2)
            self.assertEqual(launchctl_state.read_text().strip(), "loaded")
            self.assertTrue((install_root / "current" / "agent_server.py").is_file())

    def test_darwin_nontransient_bootstrap_failure_restores_previous_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, fake_bin, install_root, environment = self.fake_darwin_environment(root)
            old_runtime = install_root / "current"
            old_runtime.mkdir(parents=True)
            (old_runtime / "old-runtime-marker").write_text("preserved\n")
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist = launch_agents / "com.agentsdock.server.plist"
            plist.write_text("OLD_SERVICE_CONFIG\n")
            launchctl_state = root / "launchctl-state"
            launchctl_log = root / "launchctl.log"
            launchctl_state.write_text("loaded\n")
            self.write_executable(fake_bin / "launchctl", """#!/bin/sh
command="$1"
case "$command" in
  print)
    state="$(cat "$FAKE_LAUNCHCTL_STATE")"
    printf 'print:%s\n' "$state" >> "$FAKE_LAUNCHCTL_LOG"
    [ "$state" = "loaded" ]
    exit $?
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    printf 'absent\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
  bootstrap)
    if grep -q OLD_SERVICE_CONFIG "$3"; then
      printf 'bootstrap:old\n' >> "$FAKE_LAUNCHCTL_LOG"
      printf 'loaded\n' > "$FAKE_LAUNCHCTL_STATE"
      exit 0
    fi
    printf 'bootstrap:new\n' >> "$FAKE_LAUNCHCTL_LOG"
    echo 'Bootstrap failed: 78: Invalid property list' >&2
    exit 78
    ;;
esac
exit 2
""")
            environment.update({
                "FAKE_LAUNCHCTL_STATE": str(launchctl_state),
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
            })

            result = subprocess.run(
                ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("The previous release and service were restored.", result.stderr)
            calls = launchctl_log.read_text().splitlines()
            self.assertEqual(calls.count("bootstrap:new"), 1)
            self.assertEqual(calls.count("bootstrap:old"), 1)
            self.assertTrue((install_root / "current" / "old-runtime-marker").is_file())
            self.assertEqual(plist.read_text(), "OLD_SERVICE_CONFIG\n")
            self.assertEqual(launchctl_state.read_text().strip(), "loaded")

    def fake_darwin_environment(self, root: Path):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        temporary = root / "tmp"
        fake_bin.mkdir()
        home.mkdir()
        temporary.mkdir()
        self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
        self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
        self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
        self.write_executable(fake_bin / "sleep", "#!/bin/sh\nprintf 'sleep:%s\\n' \"$1\" >> \"$FAKE_LAUNCHCTL_LOG\"\n")
        self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "TMPDIR": str(temporary),
            "AGENTS_SERVER_INSTALL_DIR": str(install_root),
            "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
            "AGENTSDOCK_STATE_DIR": str(root / "state"),
            "FAKE_LAUNCHCTL_LOG": str(root / "launchctl.log"),
        }
        return home, fake_bin, install_root, environment

    def fake_linux_preinstall_environment(self, root: Path):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        home.mkdir()
        fake_bin.mkdir()
        self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux", "systemctl"))
        old_release = install_root / "releases" / "0.0.9"
        old_release.mkdir(parents=True)
        (old_release / "runtime-marker").write_text("preserved\n")
        (install_root / "current").symlink_to(old_release, target_is_directory=True)
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "AGENTS_SERVER_INSTALL_DIR": str(install_root),
            "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
            "AGENTSDOCK_STATE_DIR": str(root / "state"),
        }
        return home, fake_bin, install_root, environment

    def fake_linux_uninstall_environment(self, root: Path, *, stop_failure: bool = False):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        config_root = root / "config"
        state_root = root / "state"
        home.mkdir()
        fake_bin.mkdir()
        install_root.mkdir()
        config_root.mkdir()
        state_root.mkdir()
        (install_root / "runtime-marker").write_text("preserved runtime\n")
        (config_root / "env").write_text(
            "AGENTSDOCK_AGENT_TOKEN=preserved_token_abcdefghijklmnopqrstuvwxyz0123456789\n"
        )
        (state_root / "sessions.json").write_text('{"keep": true}\n')
        service_file = home / ".config" / "systemd" / "user" / "agents-server.service"
        service_file.parent.mkdir(parents=True)
        service_file.write_text("[Service]\nExecStart=/keep/running\n")
        self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
        if stop_failure:
            systemctl_source = """#!/bin/sh
case "${2:-}" in
  show-environment) exit 0 ;;
  disable) exit 1 ;;
  is-active) exit 0 ;;
esac
exit 0
"""
        else:
            systemctl_source = """#!/bin/sh
case "${2:-}" in
  show-environment) exit 0 ;;
  is-active) exit 1 ;;
esac
exit 0
"""
        self.write_executable(fake_bin / "systemctl", systemctl_source)
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "AGENTS_SERVER_INSTALL_DIR": str(install_root),
            "AGENTS_SERVER_CONFIG_DIR": str(config_root),
            "AGENTSDOCK_STATE_DIR": str(state_root),
        }
        return home, fake_bin, install_root, config_root, state_root, service_file, environment

    def write_fake_uv(self, fake_bin: Path):
        self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")

    def write_health_curl(self, fake_bin: Path, *, conflict_port: int, healthy_port: int):
        self.write_executable(fake_bin / "curl", f"""#!/bin/sh
case "$*" in
  *":{conflict_port}/api/health"*) exit 22 ;;
  *":{healthy_port}/api/health"*) exit 0 ;;
esac
exit 0
""")

    @staticmethod
    def adjacent_port_listener() -> tuple[socket.socket, int]:
        for _attempt in range(100):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if port >= 65535:
                listener.close()
                continue
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port + 1))
            except OSError:
                listener.close()
                continue
            finally:
                probe.close()
            listener.listen(8)
            return listener, port
        raise AssertionError("could not reserve a listener with a free adjacent port")

    @staticmethod
    def release_version() -> str:
        return (ROOT / "VERSION").read_text().strip()

    def prepare_preflight_path(self, fake_bin: Path, *, os_name: str, commands: tuple[str, ...]):
        self.write_executable(fake_bin / "uname", f"#!/bin/sh\necho {os_name}\n")
        for name in ("dirname", "sed", "tail", "tr"):
            resolved = shutil.which(name)
            if resolved is None:
                self.fail(f"test host is missing required utility {name}")
            (fake_bin / name).symlink_to(resolved)
        for name in commands:
            self.write_executable(fake_bin / name, "#!/bin/sh\nexit 0\n")

    @staticmethod
    def snapshot_trees(*roots: Path):
        snapshot = {}
        for root in roots:
            if not root.exists() and not root.is_symlink():
                snapshot[str(root)] = ("missing", None)
                continue
            for path in (root, *sorted(root.rglob("*"))):
                if path.is_symlink():
                    value = ("symlink", os.readlink(path))
                elif path.is_file():
                    value = ("file", path.read_bytes())
                else:
                    value = ("directory", None)
                snapshot[str(path)] = value
        return snapshot

    @staticmethod
    def write_executable(path: Path, source: str):
        path.write_text(source)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
