"""Read-only setup diagnostics: all Tailscale commands/files are mocked."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import server_instances as instances

ROOT = Path(__file__).resolve().parents[1]
APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
HOME_APP = "/synthetic/home/Applications/Tailscale.app/Contents/MacOS/Tailscale"
CONNECTED = {"BackendState": "Running", "Self": {"Online": True}, "TailscaleIPs": ["100.97.237.26", "fd7a:115c:a1e0::1"]}


class TailscaleDiscoveryTests(unittest.TestCase):
    def probe(self, *, executables=(), apps=(), on_path=None, status=None, error=None, platform="darwin"):
        with ExitStack() as stack:
            stack.enter_context(patch.object(instances.shutil, "which", return_value=on_path))
            stack.enter_context(patch.object(Path, "is_file", lambda path: str(path) in executables))
            stack.enter_context(patch.object(Path, "is_dir", lambda path: str(path) in apps))
            stack.enter_context(patch.object(Path, "resolve", lambda path: path))
            stack.enter_context(patch.object(instances.os, "access", side_effect=lambda path, _mode: path in executables))
            response = subprocess.CompletedProcess([], 0, json.dumps(CONNECTED if status is None else status), "")
            run = stack.enter_context(patch.object(instances.subprocess, "run", return_value=response, side_effect=error))
            result = instances.tailscale_status(home=Path("/synthetic/home"), platform=platform)
            return result, run

    def test_mac_app_without_path_cli_is_detected(self):
        result, run = self.probe(executables=[APP], apps=["/Applications/Tailscale.app"])
        self.assertEqual(result, {"status": "connected", "ipv4": "100.97.237.26"})
        self.assertEqual(run.call_args.args[0], [APP, "status", "--json"])
        self.assertEqual(run.call_args.kwargs["env"]["TAILSCALE_BE_CLI"], "1")
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_user_app_directory_is_detected(self):
        result, run = self.probe(executables=[HOME_APP])
        self.assertEqual(result["status"], "connected")
        self.assertEqual(run.call_args.args[0][0], HOME_APP)

    def test_path_cli_is_reused_and_app_is_not_launched(self):
        result, run = self.probe(on_path="/synthetic/tailscale", executables=["/synthetic/tailscale", APP])
        self.assertEqual(result["status"], "connected")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "/synthetic/tailscale")

    def test_known_linux_cli_is_detected_without_path_entry(self):
        result, run = self.probe(executables=["/usr/bin/tailscale"], platform="linux")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/tailscale")

    def test_missing_installation_never_invokes_commands(self):
        result, run = self.probe()
        self.assertEqual(result, {"status": "not-installed", "ipv4": ""})
        run.assert_not_called()

    def test_installed_app_without_executable_is_not_marked_missing(self):
        result, run = self.probe(apps=["/Applications/Tailscale.app"])
        self.assertEqual(result["status"], "unavailable")
        run.assert_not_called()

    def test_stopped_or_logged_out_does_not_advertise_stale_ip(self):
        for state in ("Stopped", "NeedsLogin", "NeedsMachineAuth", "Starting"):
            with self.subTest(state=state):
                result, _ = self.probe(executables=[APP], status={**CONNECTED, "BackendState": state})
                self.assertEqual(result, {"status": "disconnected", "ipv4": ""})

    def test_offline_self_does_not_advertise_cached_ip(self):
        result, _ = self.probe(executables=[APP], status={**CONNECTED, "Self": {"Online": False}})
        self.assertEqual(result, {"status": "disconnected", "ipv4": ""})

    def test_timeout_is_nonfatal_and_does_not_say_reinstall(self):
        result, run = self.probe(executables=[APP], error=subprocess.TimeoutExpired("tailscale", 3))
        self.assertEqual(result, {"status": "unavailable", "ipv4": ""})
        run.assert_called_once()

    def test_missing_socket_or_permission_failure_is_nonfatal(self):
        result, _ = self.probe(executables=[APP], error=OSError("unavailable socket"))
        self.assertEqual(result["status"], "unavailable")

    def test_nonzero_command_status_is_not_installation_absence(self):
        response = subprocess.CompletedProcess([], 1, "", "private details")
        result, _ = self.probe(executables=[APP], error=[response])
        self.assertEqual(result["status"], "unavailable")

    def test_malformed_status_is_not_installation_absence(self):
        for status in ([], "text", {"TailscaleIPs": []}, {**CONNECTED, "TailscaleIPs": "100.1.1.1"}):
            with self.subTest(status=status):
                result, _ = self.probe(executables=[APP], status=status)
                self.assertEqual(result["status"], "unavailable")
        response = subprocess.CompletedProcess([], 0, "not json", "")
        result, _ = self.probe(executables=[APP], error=[response])
        self.assertEqual(result["status"], "unavailable")

    def test_rejects_invalid_or_non_remote_addresses(self):
        for value in ("100.1.2.3;echo injected", "100.1.2.3\nextra", "0.0.0.0", "127.0.0.1", "224.1.1.1", "169.254.1.1", 123, None):
            with self.subTest(value=value):
                result, _ = self.probe(executables=[APP], status={**CONNECTED, "TailscaleIPs": [value]})
                self.assertEqual(result["ipv4"], "")

    def test_ipv6_only_is_connected_without_an_ipv4_url(self):
        result, _ = self.probe(executables=[APP], status={**CONNECTED, "TailscaleIPs": ["fd7a:115c:a1e0::1"]})
        self.assertEqual(result, {"status": "connected", "ipv4": ""})

    def test_probe_does_not_inherit_server_credentials(self):
        with patch.dict(os.environ, {"AGENTSDOCK_AGENT_TOKEN": "private", "OPENAI_API_KEY": "private"}):
            _, run = self.probe(executables=[APP])
        self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])

    def test_broken_path_cli_falls_back_to_existing_app(self):
        result, run = self.probe(on_path="/synthetic/tailscale", executables=["/synthetic/tailscale", APP], error=[subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0, json.dumps(CONNECTED), "")])
        self.assertEqual(result["status"], "connected")
        self.assertEqual(len(run.call_args_list), 2)
        self.assertTrue(all(call.args[0][1:] == ["status", "--json"] for call in run.call_args_list))


class SetupOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "install.sh").read_text()
        cls.summary_functions = cls.source[cls.source.index("setup_network_summary() {"):cls.source.index("\nsetup_network_summary\n")]

    def bindings(self, bind="0.0.0.0", state="connected", ip="100.97.237.26"):
        with patch.object(instances, "tailscale_status", return_value={"status": state, "ipv4": ip}), patch.object(instances, "candidate_addresses", return_value=["http://127.0.0.1:7851", "http://100.97.237.26:7851", "http://192.168.1.201:7851"]):
            return instances.setup_network_bindings(bind, 7851)

    def render(self, bindings):
        script = "set -eu\nCHECK_MARK=ok\nDOT_MARK=note\nPORT=7851\n" + self.summary_functions + "\n" + bindings + "\nprint_tailscale_summary\n"
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def checklist(self, *, bind="0.0.0.0", state="connected", tmux=True, claude=True, codex=False, platform="Darwin"):
        values = {
            "CHECK_MARK": "✓", "DOT_MARK": "○", "PORT": "7851",
            "COLOR_GREEN": "", "COLOR_BOLD": "", "COLOR_RESET": "",
            "TMUX_WARNING": "" if tmux else "synthetic tmux warning",
            "CLAUDE_READY": "true" if claude else "false",
            "CODEX_READY": "true" if codex else "false", "OS_NAME": platform,
        }
        script = "set -eu\n" + "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n"
        script += self.summary_functions + "\n" + self.bindings(bind, state, "100.97.237.26" if state == "connected" else "") + "\nprint_setup_checklist\n"
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_all_green_shows_all_set_two_checks_and_no_next_steps(self):
        output = self.checklist()
        self.assertIn("You are all set", output)
        self.assertEqual(output.count("✓"), 2)
        self.assertIn("✓ tmux available", output)
        self.assertIn("✓ Tailscale already connected: http://100.97.237.26:7851", output)
        self.assertNotIn("next steps", output.lower())
        self.assertNotIn("install", output)

    def test_either_supported_cli_is_enough_for_summary(self):
        self.assertIn("You are all set", self.checklist(claude=False, codex=True))

    def test_missing_tmux_is_optional_with_short_mac_command(self):
        output = self.checklist(tmux=False)
        self.assertIn("You already have", output)
        self.assertIn("✓ AgentsServer running", output)
        self.assertIn("✓ Tailscale already connected", output)
        self.assertIn("Optional next steps", output)
        self.assertIn("You can skip", output)
        self.assertIn("brew install tmux", output)
        self.assertNotIn("✓ tmux", output)
        self.assertNotIn("You are all set", output)

    def test_missing_tmux_on_linux_uses_package_manager_hint(self):
        output = self.checklist(tmux=False, platform="Linux")
        self.assertIn("sudo apt install tmux", output)
        self.assertNotIn("brew install", output)

    def test_missing_tailscale_is_optional_with_link_and_lan_alternative(self):
        output = self.checklist(state="not-installed")
        self.assertIn("✓ tmux available", output)
        self.assertIn("Optional next steps", output)
        self.assertIn("https://tailscale.com/download", output)
        self.assertIn("LAN address", output)
        self.assertNotIn("✓ Tailscale", output)
        self.assertNotIn("You are all set", output)

    def test_installed_disconnected_tailscale_gets_connection_not_install_hint(self):
        output = self.checklist(state="disconnected")
        self.assertIn("✓ Tailscale installed", output)
        self.assertIn("open Tailscale and sign in/connect", output)
        self.assertNotIn("download", output)
        self.assertNotIn("You are all set", output)

    def test_unknown_network_status_gets_check_hint_without_claiming_ready(self):
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                output = self.checklist(state=state)
                self.assertIn("tailscale status", output)
                self.assertIn("Optional next steps", output)
                self.assertNotIn("You are all set", output)
                self.assertNotIn("download", output)

    def test_loopback_binding_is_not_mislabeled_all_set_for_phones(self):
        output = self.checklist(bind="127.0.0.1")
        self.assertIn("✓ Tailscale installed", output)
        self.assertIn("phones cannot connect directly", output)
        self.assertIn("https://github.com/ZhengyiLuo/AgentsServer", output)
        self.assertNotIn("You are all set", output)

    def test_missing_agent_cli_is_separate_from_optional_features(self):
        output = self.checklist(claude=False, codex=False)
        self.assertIn("You already have", output)
        self.assertIn("To start chats", output)
        self.assertIn("npm install -g @anthropic-ai/claude-code", output)
        self.assertIn("npm install -g @openai/codex", output)
        self.assertNotIn("Optional next steps", output)  # tmux and network are ready.
        self.assertNotIn("You are all set", output)

    def test_connected_summary_uses_reachable_bind_and_existing_port(self):
        bindings = self.bindings()
        values = dict(value.split("=", 1) for value in shlex.split(bindings))
        self.assertEqual(values["SERVER_URL"], "http://100.97.237.26:7851")
        self.assertEqual(values["TAILSCALE_IP"], "100.97.237.26")
        self.assertIn("http://192.168.1.201:7851", values["NETWORK_URLS"])
        output = self.render(bindings)
        self.assertIn("already connected", output)
        self.assertNotIn("download", output)
        self.assertNotIn("install and connect", output)

    def test_loopback_bind_never_claims_phone_connectivity(self):
        for bind in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(bind=bind):
                bindings = self.bindings(bind)
                values = dict(value.split("=", 1) for value in shlex.split(bindings))
                self.assertEqual(values["TAILSCALE_BIND_MATCH"], "false")
                self.assertEqual(values["SERVER_LOCAL_ONLY"], "true")
                self.assertNotIn("100.97.237.26", values["SERVER_URL"])
                self.assertIn("phones cannot connect directly", self.render(bindings))

    def test_lan_only_bind_does_not_claim_a_tailnet_listener(self):
        bindings = self.bindings("192.168.1.201")
        values = dict(value.split("=", 1) for value in shlex.split(bindings))
        self.assertEqual(values["SERVER_URL"], "http://192.168.1.201:7851")
        self.assertEqual(values["TAILSCALE_BIND_MATCH"], "false")
        self.assertIn("configured bind", self.render(bindings))

    def test_specific_tailnet_bind_is_usable(self):
        values = dict(value.split("=", 1) for value in shlex.split(self.bindings("100.97.237.26")))
        self.assertEqual(values["TAILSCALE_BIND_MATCH"], "true")

    def test_ipv6_wildcard_does_not_assume_ipv4_works(self):
        values = dict(value.split("=", 1) for value in shlex.split(self.bindings("::")))
        self.assertEqual(values["SERVER_URL"], "http://[::1]:7851")
        self.assertEqual(values["TAILSCALE_BIND_MATCH"], "false")

    def test_installed_disconnected_or_unavailable_never_suggests_installing(self):
        for state in ("disconnected", "unavailable"):
            with self.subTest(state=state):
                output = self.render(self.bindings(state=state, ip=""))
                self.assertIn("already installed", output)
                self.assertNotIn("download", output)

    def test_install_reminder_is_only_for_missing_installation(self):
        output = self.render(self.bindings(state="not-installed", ip=""))
        self.assertIn("not found", output)
        self.assertIn("optional", output)

    def test_optional_helper_failure_does_not_fail_installation(self):
        script = "set -eu\nPORT=7851\nBIND_ADDRESS=0.0.0.0\nCURRENT_LINK=/synthetic-missing-release\nhealth_origin() { echo http://127.0.0.1:7851; }\n" + self.summary_functions + '\nsetup_network_summary\nprintf "%s %s\\n" "$TAILSCALE_STATUS" "$SERVER_URL"\n'
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "unknown http://127.0.0.1:7851")

    def test_installer_consumes_network_bindings_and_keeps_setup_json_fields(self):
        bindings = self.bindings()
        with tempfile.TemporaryDirectory(prefix="setup-network-fixture-") as temporary:
            current = Path(temporary)
            python = current / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(bindings) + "\n")
            python.chmod(0o755)
            script = "set -eu\nPORT=7851\nBIND_ADDRESS=0.0.0.0\nCURRENT_LINK=" + shlex.quote(str(current)) + "\nhealth_origin() { echo http://127.0.0.1:7851; }\n" + self.summary_functions
            script += '\nsetup_network_summary\nprintf "%s %s\\n" "$SERVER_URL" "$TAILSCALE_IP"\n'
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "http://100.97.237.26:7851 100.97.237.26")
        self.assertIn('"$SERVER_URL" "$TOKEN" "$SERVICE_KIND" "$TAILSCALE_IP" "$RELEASE_VERSION"', self.source)

    def test_dependency_setup_is_private_quiet_and_keeps_errors(self):
        start = self.source.index("sync_release_dependencies() (")
        body = self.source[start:self.source.index("validate_staged_release_runtime()", start)]
        self.assertIn('UV_PROJECT_ENVIRONMENT="$STAGE_DIR/.venv"', body)
        self.assertIn("--quiet", body)
        self.assertIn("--frozen", body)
        self.assertNotIn("2>/dev/null", body)
        self.assertIn("Reusing uv's package cache where available", self.source)
        self.assertIn("This does not reinstall Claude Code, Codex, or tmux", self.source)
        with tempfile.TemporaryDirectory(prefix="setup-uv-fixture-") as temporary:
            root = Path(temporary)
            uv = root / "uv"
            uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\necho dependency-error >&2\nexit 42\n')
            uv.chmod(0o755)
            script = f"set -eu\nSTAGE_DIR={shlex.quote(str(root / 'stage'))}\nUV_BIN={shlex.quote(str(uv))}\n" + body + "\nsync_release_dependencies\n"
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 42)
            self.assertIn("--quiet", result.stdout)
            self.assertIn("dependency-error", result.stderr)


if __name__ == "__main__":
    unittest.main()
