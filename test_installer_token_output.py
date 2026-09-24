"""Token UX in real pseudo-terminals, with fake clipboard tools and credentials."""
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import server_instances as instances

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-token-for-copy-tests-only"


class TokenOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "install.sh").read_text()
        start = cls.source.index("interactive_token_output() {")
        cls.functions = cls.source[start:cls.source.index('\nif [[ "$SHOW_TOKEN" == "true" ]]', start)]

    def script(self, clipboard, *, flags="", platform="Darwin", tool="pbcopy", failure=False, managed=False):
        # A shell function always intercepts the selected tool. PATH is empty
        # during the tested functions, preventing use of any real clipboard.
        fake = "" if tool is None else f'''{tool}() {{
            printf '%s\\n' "$*" > {shlex.quote(str(clipboard) + '.args')}
            local incoming=''
            IFS= read -r incoming || true
            printf '%s' "$incoming" > {shlex.quote(str(clipboard))}
            {'echo private-tool-error >&2; return 1' if failure else 'return 0'}
        }}\n'''
        return f'''set -euo pipefail
NON_INTERACTIVE=false
EXPECTED_SERVER_IDENTITY=''
INSTANCE_NAME=local-test
TOKEN={shlex.quote(TOKEN)}
unset SSH_CONNECTION SSH_CLIENT SSH_TTY DISPLAY WAYLAND_DISPLAY
uname() {{ printf '%s\\n' {shlex.quote(platform)}; }}
{fake}
{self.functions}
{flags}
PATH=''
{'EXPECTED_SERVER_IDENTITY=managed-server' if managed else ''}
if [[ -z "$EXPECTED_SERVER_IDENTITY" ]]; then
  if interactive_token_output; then
    print_token_for_copy "$TOKEN"
  else
    printf '%s\\n' "$TOKEN"
  fi
fi
'''

    def run_terminal(self, script, answer="yes\n", environment=None, prompt_answers=None):
        master, slave = pty.openpty()
        process = None
        output = b""
        try:
            process = subprocess.Popen(["/bin/bash", "-c", script], stdin=slave, stdout=slave, stderr=slave, env=environment)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 5
            pending = list(prompt_answers) if prompt_answers is not None else [(b"[y/N]", answer)]
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    if pending and pending[0][0] in output:
                        _, reply = pending.pop(0)
                        os.write(master, reply.encode())
                elif process.poll() is not None:
                    break
            # PTY EOF can arrive just before waitpid observes process exit.
            self.assertEqual(process.wait(timeout=1), 0, output.decode(errors="replace"))
            return output.decode().replace("\r\n", "\n")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            os.close(master)
            if slave is not None:
                os.close(slave)

    def test_terminal_yes_copies_exact_token_via_stdin_and_displays_standalone_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            output = self.run_terminal(self.script(clipboard))
            self.assertIn(f"Access token (local-test):\n{TOKEN}\n", output)
            self.assertIn("Copied to your clipboard.", output)
            self.assertEqual(clipboard.read_text(), TOKEN)
            self.assertEqual(Path(str(clipboard) + ".args").read_text(), "\n")

    def test_real_show_token_entrypoint_in_terminal_only_reads_synthetic_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, config = root / "home", root / "config"
            home.mkdir()
            config.mkdir()
            token_file = config / "env"
            token_file.write_text(f"AGENTSDOCK_AGENT_TOKEN={TOKEN}\n")
            token_file.chmod(0o600)
            clipboard = root / "clipboard"
            environment = {
                "PATH": "/usr/bin:/bin", "HOME": str(home),
                "AGENTS_SERVER_INSTALL_DIR": str(root / "runtime"),
                "AGENTS_SERVER_CONFIG_DIR": str(config),
                "AGENTSDOCK_STATE_DIR": str(root / "state"),
            }
            script = f'''uname() {{ case "$1" in -s) echo Darwin;; -m) echo arm64;; esac; }}
pbcopy() {{ local incoming=''; IFS= read -r incoming || true; printf '%s' "$incoming" > {shlex.quote(str(clipboard))}; }}
export -f uname pbcopy
exec /bin/bash {shlex.quote(str(ROOT / 'install.sh'))} --show-token
'''
            output = self.run_terminal(script, environment=environment)
            self.assertIn(f"Access token (default):\n{TOKEN}\n", output)
            self.assertIn("Copied to your clipboard.", output)
            self.assertEqual(clipboard.read_text(), TOKEN)
            self.assertEqual(token_file.read_text(), f"AGENTSDOCK_AGENT_TOKEN={TOKEN}\n")
            self.assertFalse((root / "runtime").exists())
            self.assertFalse((root / "state").exists())

    def test_no_empty_or_eof_never_changes_clipboard(self):
        for answer in ("no\n", "\n", "\x04"):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as temporary:
                clipboard = Path(temporary) / "clipboard"
                clipboard.write_text("keep existing clipboard")
                output = self.run_terminal(self.script(clipboard), answer)
                self.assertEqual(clipboard.read_text(), "keep existing clipboard")
                self.assertNotIn("Copied to your clipboard", output)

    def test_explicit_noninteractive_never_prompts_even_with_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            output = self.run_terminal(self.script(clipboard, flags="NON_INTERACTIVE=true"))
            self.assertEqual(output, TOKEN + "\n")
            self.assertFalse(clipboard.exists())

    def test_redirected_show_token_stays_raw_and_never_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            result = subprocess.run(["/bin/bash", "-c", self.script(clipboard)], input="yes\n", capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, TOKEN + "\n")
            self.assertEqual(result.stderr, "")
            self.assertFalse(clipboard.exists())

    def test_missing_clipboard_tool_is_nonfatal_without_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            output = self.run_terminal(self.script(clipboard, tool=None))
            self.assertIn("copy the token line above manually", output)
            self.assertNotIn("[y/N]", output)

    def test_failed_clipboard_tool_reports_failure_without_exposing_its_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.run_terminal(self.script(Path(temporary) / "clipboard", failure=True))
            self.assertIn("Could not copy", output)
            self.assertNotIn("private-tool-error", output)
            self.assertNotIn("Copied to", output)

    def test_ssh_never_modifies_remote_clipboard_or_emits_escape_sequences(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            output = self.run_terminal(self.script(clipboard, flags="SSH_CONNECTION=synthetic-ssh"))
            self.assertIn("unavailable over SSH", output)
            self.assertNotIn("[y/N]", output)
            self.assertNotIn("\x1b", output)
            self.assertFalse(clipboard.exists())

    def test_linux_existing_clipboard_backends(self):
        for tool, flags, arguments in (
            ("wl-copy", "WAYLAND_DISPLAY=wayland-0", ""),
            ("xclip", "DISPLAY=:0", "-selection clipboard"),
            ("xsel", "DISPLAY=:0", "--clipboard --input"),
        ):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as temporary:
                clipboard = Path(temporary) / "clipboard"
                output = self.run_terminal(self.script(clipboard, platform="Linux", tool=tool, flags=flags), "YES\n")
                self.assertIn("Copied to your clipboard", output)
                self.assertEqual(clipboard.read_text(), TOKEN)
                self.assertEqual(Path(str(clipboard) + ".args").read_text().strip(), arguments)

    def test_headless_linux_never_attempts_installed_clipboard_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            output = self.run_terminal(self.script(clipboard, platform="Linux", tool="xclip"))
            self.assertIn("unavailable here", output)
            self.assertFalse(clipboard.exists())

    def test_managed_update_never_displays_or_copies_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            clipboard = Path(temporary) / "clipboard"
            self.assertEqual(self.run_terminal(self.script(clipboard, managed=True)), "")
            self.assertFalse(clipboard.exists())

    def test_installer_keeps_machine_result_and_only_offers_after_success(self):
        end = self.source[self.source.rindex('if [[ -z "$EXPECTED_SERVER_IDENTITY" ]]; then'):]
        self.assertIn("AGENTSDOCK_SETUP_RESULT=", end)
        self.assertIn('if interactive_token_output; then\n    print_token_for_copy "$TOKEN"', end)
        show_start = self.source.index('if [[ "$SHOW_TOKEN" == "true" ]]')
        self.assertIn('print_token_for_copy "$TOKEN_TO_SHOW"', self.source[show_start:show_start + 400])

    def ready_banner_script(self, *, previous="absent", legacy="absent", managed=False):
        color_start = self.source.index('if [[ -t 1 ]] && [[ "${TERM:-}" != "dumb" ]]')
        color_end = self.source.index('CHECK_MARK=', color_start)
        ready_start = self.source.index('echo "[7/7] AgentsServer')
        ready_end = self.source.index('echo "  ${COLOR_BOLD}Server URL', ready_start)
        return (
            "set -eu\nRELEASE_VERSION=test\n"
            f"PRIOR_SERVICE_STATE={shlex.quote(previous)}\n"
            f"PRIOR_LEGACY_SERVICE_STATE={shlex.quote(legacy)}\n"
            f"EXPECTED_SERVER_IDENTITY={'synthetic-server' if managed else ''}\n"
            + self.source[color_start:color_end] + self.source[ready_start:ready_end]
        )

    def test_new_service_banner_is_green_with_dividers_and_blank_lines(self):
        output = self.run_terminal(self.ready_banner_script(), environment={"TERM": "xterm", "PATH": "/usr/bin:/bin"})
        self.assertEqual(output, "[7/7] AgentsServer test is ready\n\n\033[32m================================\nYour new service is up!\n================================\033[0m\n\n")

    def test_new_service_banner_redirected_output_is_plain(self):
        result = subprocess.run(["/bin/bash", "-c", self.ready_banner_script()], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\n\n================================\nYour new service is up!\n================================\n\n", result.stdout)
        self.assertNotIn("\033[", result.stdout)

    def test_new_service_banner_respects_no_color_and_dumb_terminal(self):
        for settings in ({"TERM": "dumb"}, {"TERM": "xterm", "NO_COLOR": ""}, {"TERM": "xterm", "NO_COLOR": "1"}):
            with self.subTest(settings=settings):
                output = self.run_terminal(self.ready_banner_script(), environment={"PATH": "/usr/bin:/bin", **settings})
                self.assertIn("Your new service is up!", output)
                self.assertNotIn("\033[", output)

    def test_updates_and_legacy_migrations_do_not_claim_a_new_service(self):
        for flags in ({"previous": "running"}, {"previous": "stopped"}, {"legacy": "running"}, {"managed": True}):
            with self.subTest(flags=flags):
                output = self.run_terminal(self.ready_banner_script(**flags))
                self.assertEqual(output, "[7/7] AgentsServer test is ready\n\n")

    def test_instance_manager_preserves_terminal_interaction_only_when_both_streams_are_tty(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance = instances.Instance("test", Path(temporary))
            for stdin_tty, stdout_tty in ((True, True), (False, True), (True, False), (False, False)):
                with self.subTest(stdin=stdin_tty, stdout=stdout_tty), patch.object(instances.sys.stdin, "isatty", return_value=stdin_tty), patch.object(
                    instances.sys.stdout, "isatty", return_value=stdout_tty,
                ), patch.object(instances, "run") as run:
                    instances.install_instance(instance, 17851, "127.0.0.1")
                    command = run.call_args.args[0]
                    self.assertEqual("--non-interactive" in command, not (stdin_tty and stdout_tty))
                    self.assertIn("--no-port-fallback", command)


if __name__ == "__main__":
    unittest.main()
