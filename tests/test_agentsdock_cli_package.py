"""Real npm packing/bin checks in disposable homes/prefixes; no service install."""
import hashlib
import errno
import json
import os
import pty
from pathlib import Path
import shutil
import select
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import package_agentsdock_cli as cli_package
import package_npm_release as core_package


class AgentsDockCliPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agentsdock-entry-package-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / "source/server"
        source = self.root / "npm/agentsdock"
        source.mkdir(parents=True)
        for name in ("package.json", "cli.cjs", "postinstall.cjs", "README.md"):
            shutil.copyfile(ROOT / "npm/agentsdock" / name, source / name)
        (self.root / "VERSION").write_text("1.2.3-beta.4\n")
        for name in ("LICENSE", "NOTICE"):
            (self.root.parent / name).write_text(f"fixture {name}\n")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root.parent, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root.parent, check=True)
        subprocess.run(["git", "-c", "user.name=CLI Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--quiet", "-m", "fixture"], cwd=self.root.parent, check=True)

    def test_exact_pin_reviewed_hook_and_no_private_files(self):
        (self.root / "npm/agentsdock/.env").write_text("not packaged")
        destination = self.work / "stage"
        version, expected = cli_package.stage_package(self.root, destination)
        metadata = json.loads((destination / "package.json").read_text())
        self.assertEqual(metadata["name"], "agentsdock")
        self.assertEqual(metadata["bin"], {"agentsdock": "cli.cjs"})
        self.assertEqual(metadata["dependencies"], {"@agentsdock/server": version})
        self.assertNotIn("private", metadata)
        self.assertEqual(metadata["scripts"], {"postinstall": "node postinstall.cjs"})
        self.assertEqual({p.name for p in destination.iterdir()}, expected)
        self.assertEqual((destination / "cli.cjs").stat().st_mode & 0o777, 0o755)
        for name in ("LICENSE", "NOTICE"):
            self.assertEqual((destination / name).read_bytes(), (self.root.parent / name).read_bytes())

    def test_rejects_unreviewed_hooks_extra_dependencies_and_wrong_name(self):
        source = self.root / "npm/agentsdock/package.json"
        original = json.loads(source.read_text())
        for change in ({"scripts": {"postinstall": "bad"}}, {"scripts": {}}, {"name": "other"},
                       {"dependencies": {"@agentsdock/server": "latest"}},
                       {"optionalDependencies": {"other": "*"}}):
            source.write_text(json.dumps({**original, **change}))
            with self.assertRaises(ValueError):
                cli_package.stage_package(self.root, self.work / "stage")

    def test_actual_pack_is_reproducible_and_receipt_is_bound_to_bytes(self):
        one = cli_package.prepare(self.root, self.work / "one", require_clean_source=True)
        two = cli_package.prepare(self.root, self.work / "two", require_clean_source=True)
        self.assertEqual(one, two)
        archive = self.work / "one" / one["archive"]["name"]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), one["archive"]["sha256"])
        with tarfile.open(archive) as packed:
            self.assertEqual(set(packed.getnames()), {f"package/{name}" for name in
                             ("package.json", "cli.cjs", "postinstall.cjs", "README.md", "LICENSE", "NOTICE")})
        with self.assertRaises(FileExistsError):
            cli_package.prepare(self.root, self.work / "one")
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), one["archive"]["sha256"])
        (self.root / "VERSION").write_text("1.2.3-beta.5")
        with self.assertRaisesRegex(ValueError, "clean committed"):
            cli_package.prepare(self.root, self.work / "dirty", require_clean_source=True)

    @unittest.skipUnless(shutil.which("npm") and shutil.which("node") and os.getuid() != 0,
                         "real npm CLI smoke needs npm/node and a non-root user")
    def test_real_npm_lifecycle_auto_setup_reinstall_skip_and_private_output(self):
        """Actual npm hook; service creation is a labelled synthetic core fixture."""
        cli = cli_package.prepare(self.root, self.work / "cli")
        core = self.work / "synthetic-core"
        (core / "server").mkdir(parents=True)
        (core / "npm").mkdir()
        version = cli["version"]
        (core / "package.json").write_text(json.dumps({"name": "@agentsdock/server", "version": version}))
        (core / "server/VERSION").write_text(version)
        (core / "npm/cli.cjs").write_text(r'''
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const marker = path.join(process.env.HOME, 'synthetic-service-started');
function ensureFreshInstall() {
  if (fs.existsSync(marker)) throw Object.assign(new Error('fixture existing state'), {code: 'AGENTSDOCK_EXISTING_INSTALLATION'});
}
module.exports = {ensureFreshInstall, run: () => 0};
if (require.main === module) {
  assert.deepEqual(process.argv.slice(2), ['install', '--non-interactive']);
  console.log('private installer output secret-lifecycle-sentinel');
  console.error('private installer diagnostic secret-lifecycle-sentinel');
  if (fs.existsSync(path.join(process.env.HOME, 'synthetic-failure'))) process.exit(7);
  fs.writeFileSync(marker, 'one synthetic service start');
  console.log('AGENTSDOCK_SETUP_RESULT=' + JSON.stringify({server_url:'http://127.0.0.1:7850',
    server_version:require('../package.json').version, access_token:'secret-lifecycle-sentinel'}));
}
''')
        home = self.work / "hook-home"
        home.mkdir(mode=0o700)
        for name in ("user.npmrc", "global.npmrc"):
            (self.work / name).touch()
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
               "NPM_CONFIG_CACHE": str(self.work / "hook-cache"),
               "NPM_CONFIG_USERCONFIG": str(self.work / "user.npmrc"),
               "NPM_CONFIG_GLOBALCONFIG": str(self.work / "global.npmrc"),
               "NPM_CONFIG_UPDATE_NOTIFIER": "false"}
        packed = subprocess.run(["npm", "pack", "--ignore-scripts", "--offline", "--json"],
                                cwd=core, env=env, text=True, capture_output=True, check=True, timeout=30)
        archives = [str(core / json.loads(packed.stdout)[0]["filename"]),
                    str(self.work / "cli" / cli["archive"]["name"])]
        prefix = self.work / "hook-global"
        command = ["npm", "install", "--global", "--prefix", str(prefix),
                   "--no-audit", "--no-fund", "--offline", *archives]
        result = subprocess.run(command, cwd=self.work, env=env, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = home / "synthetic-service-started"
        self.assertEqual(marker.read_text(), "one synthetic service start")
        before = marker.stat().st_mtime_ns
        repeated = subprocess.run([*command, "--force"], cwd=self.work, env=env, text=True, capture_output=True, timeout=45)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(marker.stat().st_mtime_ns, before)

        # Local/CI/explicit opt-out must not run even the synthetic installer.
        for mode in ("local", "ci", "skip"):
            target_home = self.work / ("hook-" + mode)
            target_home.mkdir(mode=0o700)
            mode_env = {**env, "HOME": str(target_home)}
            if mode == "ci":
                mode_env["CI"] = "true"
            if mode == "skip":
                mode_env["AGENTSDOCK_SKIP_SETUP"] = "1"
            args = ["npm", "install", "--prefix", str(self.work / ("prefix-" + mode)),
                    "--no-audit", "--no-fund", "--offline", *archives]
            if mode != "local":
                args.append("--global")
            skipped = subprocess.run(args, cwd=self.work, env=mode_env, text=True, capture_output=True, timeout=45)
            self.assertEqual(skipped.returncode, 0, skipped.stderr)
            self.assertFalse((target_home / "synthetic-service-started").exists())

        failed_home = self.work / "failed-home"
        failed_home.mkdir(mode=0o700)
        (failed_home / "synthetic-failure").touch()
        failed = subprocess.run([*command, "--force"], cwd=self.work,
                                env={**env, "HOME": str(failed_home)}, text=True, capture_output=True, timeout=45)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Automatic setup did not complete", failed.stderr)
        self.assertFalse((failed_home / "synthetic-service-started").exists())
        for output in (result.stdout, result.stderr, repeated.stdout, repeated.stderr, failed.stdout, failed.stderr):
            self.assertNotIn("secret-lifecycle-sentinel", output)
        for log in (self.work / "hook-cache/_logs").glob("*.log"):
            self.assertNotIn("secret-lifecycle-sentinel", log.read_text())

    @unittest.skipUnless(shutil.which("npm") and shutil.which("node") and os.getuid() != 0,
                         "real npm CLI smoke needs npm/node and a non-root user")
    def test_real_global_and_local_npm_entrypoint_with_actual_runtime(self):
        # Actual source payload, npm resolver, bin symlink and Python/bash helpers.
        # Never invoke service creation/control or connect to a real provider.
        core = core_package.prepare(ROOT, self.work / "core")
        cli = cli_package.prepare(ROOT, self.work / "cli")
        archives = [str(self.work / "core" / core["archive"]["name"]),
                    str(self.work / "cli" / cli["archive"]["name"])]
        home = self.work / "home"
        home.mkdir(mode=0o700)
        outside = self.work / "outside"
        outside.mkdir()
        user_config, global_config = self.work / "user.npmrc", self.work / "global.npmrc"
        user_config.touch()
        global_config.touch()
        env = {"HOME": str(home), "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
               "NPM_CONFIG_CACHE": str(self.work / "cache"),
               "NPM_CONFIG_USERCONFIG": str(user_config), "NPM_CONFIG_GLOBALCONFIG": str(global_config),
               "NPM_CONFIG_UPDATE_NOTIFIER": "false"}

        def invoke(args, **kwargs):
            result = subprocess.run(args, cwd=outside, env=env, capture_output=True, text=True,
                                    timeout=90, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        def invoke_terminal(args, replies, term="dumb"):
            """Real prompt/child-installer boundary; only synthetic fixture tokens."""
            master, slave = pty.openpty()
            original_settings = termios.tcgetattr(slave)
            process = subprocess.Popen(args, cwd=outside, env={**env, "TERM": term}, stdin=slave,
                                       stdout=slave, stderr=slave, start_new_session=True)
            output, offset, next_reply = bytearray(), 0, 0
            deadline = time.monotonic() + 30
            try:
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        try:
                            data = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not data:
                            break
                        output.extend(data)
                        if next_reply < len(replies):
                            prompt, answer = replies[next_reply]
                            position = output.find(prompt.encode(), offset)
                            if position >= 0:
                                os.write(master, answer.encode())
                                offset = position + len(prompt)
                                next_reply += 1
                    elif process.poll() is not None:
                        break
                self.assertIsNotNone(process.wait(timeout=3))
                text = output.decode(errors="replace")
                self.assertEqual(process.returncode, 0, text)
                restored_settings = termios.tcgetattr(slave)
                expected_settings = list(original_settings)
                if sys.platform == "darwin":
                    # Darwin's tty driver sets PENDIN when ICANON is restored.
                    # This pending-input state is not a changed terminal mode.
                    restored_settings[3] &= ~termios.PENDIN
                    expected_settings[3] &= ~termios.PENDIN
                self.assertEqual(restored_settings, expected_settings)
                return text
            finally:
                os.close(master)
                os.close(slave)
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

        prefix = self.work / "global"
        invoke(["npm", "install", "--global", "--prefix", str(prefix), "--ignore-scripts",
                "--no-audit", "--no-fund", "--offline", *archives])
        env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
        self.assertEqual(invoke(["agentsdock", "--version"]).strip(), cli["version"])
        self.assertEqual(invoke(["agentsdock", "version"]).strip(), cli["version"])
        self.assertIn("agentsdock setup", invoke(["agentsdock", "--help"]))
        self.assertIn("agentsdock list", invoke(["agentsdock", "help"]))
        self.assertIn("No installations found", invoke(["agentsdock", "servers", "list"]))
        self.assertIn("No installations found", invoke(["agentsdock", "list"]))
        self.assertIn("No installations found", invoke(["agentsdock", "status"]))
        self.assertIn("No unfinished default", invoke(["agentsdock", "recover"]))
        # Private synthetic config; no service or real credential is involved.
        config = home / ".config/agents-server"
        config.mkdir(parents=True, mode=0o700)
        private_env = config / "env"
        private_env.write_text("AGENTSDOCK_AGENT_TOKEN=fixture-only-token\n")
        private_env.chmod(0o600)
        self.assertEqual(invoke(["agentsdock", "token", "default"]).strip(), "fixture-only-token")
        no_selector = subprocess.run(["agentsdock", "token"], cwd=outside, env=env,
                                     capture_output=True, text=True, timeout=20)
        self.assertNotEqual(no_selector.returncode, 0)
        self.assertIn("agentsdock token NAME", no_selector.stderr)
        self.assertNotIn("fixture-only-token", no_selector.stdout + no_selector.stderr)
        named_config = home / ".config/agents-server-instances/work"
        named_config.mkdir(parents=True, mode=0o700)
        named_env = named_config / "env"
        named_env.write_text("AGENTSDOCK_AGENT_TOKEN=separate-fixture-token\n")
        named_env.chmod(0o600)
        self.assertEqual(invoke(["agentsdock", "token", "--instance", "work"]).strip(), "separate-fixture-token")
        self.assertEqual(invoke(["agentsdock", "token", "work"]).strip(), "separate-fixture-token")
        self.assertEqual(invoke(["agentsdock", "token", "--instance", "default"]).strip(), "fixture-only-token")
        prompt = "Enter a number or server name (Enter to cancel): "
        chosen = invoke_terminal(["agentsdock", "token"],
                                 [(prompt, "0\n"), (prompt, "2\n"), ("[y/N] ", "n\n")])
        self.assertIn("Please enter a number from 1 to 2", chosen)
        self.assertIn("Access token (work):", chosen)
        self.assertIn("separate-fixture-token", chosen)
        self.assertNotIn("fixture-only-token", chosen)
        chosen = invoke_terminal(["agentsdock", "token"], [(prompt, "default\n"), ("[y/N] ", "n\n")])
        self.assertIn("Access token (default):", chosen)
        self.assertIn("fixture-only-token", chosen)
        self.assertNotIn("separate-fixture-token", chosen)
        cancelled = invoke_terminal(["agentsdock", "token"], [(prompt, "\n")])
        self.assertIn("Cancelled; no token was shown", cancelled)
        self.assertNotIn("Access token (", cancelled)
        self.assertNotIn("fixture-only-token", cancelled)
        self.assertNotIn("separate-fixture-token", cancelled)
        arrow_hint = "Use ↑/↓ to choose; Enter to select; Esc to cancel."
        for keys, selected, not_selected in (("\x1b[B\r", "separate-fixture-token", "fixture-only-token"),
                                             ("\x1b[B\x1b[A\r", "fixture-only-token", "separate-fixture-token"),
                                             ("\x1bOA\r", "separate-fixture-token", "fixture-only-token")):
            chosen = invoke_terminal(["agentsdock", "token"],
                                     [(arrow_hint, keys), ("[y/N] ", "n\n")], term="xterm-256color")
            self.assertIn(selected, chosen)
            self.assertNotIn(not_selected, chosen)
            self.assertIn("\x1b[?25h", chosen)
        for key in ("\x1b", "\x04"):
            cancelled = invoke_terminal(["agentsdock", "token"], [(arrow_hint, key)], term="xterm-256color")
            self.assertIn("Cancelled; no token was shown", cancelled)
            self.assertNotIn("Access token (", cancelled)
            self.assertNotIn("fixture-only-token", cancelled)
            self.assertNotIn("separate-fixture-token", cancelled)
        statuses = invoke(["agentsdock", "status"])
        self.assertIn("Server (default):", statuses)
        self.assertIn("Server (work):", statuses)
        for field in ("Status:", "Address:", "Version:", "Port:"):
            self.assertEqual(statuses.count(field), 2)
        self.assertNotIn("fixture-token", statuses)
        self.assertNotIn("fixture-only-token", statuses)
        named_status = invoke(["agentsdock", "status", "work"])
        self.assertIn("Server (work):", named_status)
        self.assertNotIn("Server (default):", named_status)
        before = private_env.read_bytes()
        rejected = subprocess.run(["agentsdock", "install", "--dry-run"], cwd=outside,
                                  env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("existing server installation or state", rejected.stderr)
        self.assertIn("If you want to add a new server instance, run: agentsdock new", rejected.stderr)
        self.assertEqual(private_env.read_bytes(), before)
        for command in ("restart", "remove", "uninstall"):
            rejected = subprocess.run(["agentsdock", command], cwd=outside, env=env,
                                      capture_output=True, text=True, timeout=20)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Select exactly one instance or --all", rejected.stderr)
            self.assertEqual(private_env.read_bytes(), before)
        self.assertFalse((home / "Library/LaunchAgents").exists())
        self.assertFalse((home / ".config/systemd").exists())

        local = self.work / "local"
        invoke(["npm", "install", "--prefix", str(local), "--ignore-scripts",
                "--no-audit", "--no-fund", "--offline", *archives])
        self.assertEqual(invoke([str(local / "node_modules/.bin/agentsdock"), "--version"]).strip(), cli["version"])
        # Do not let the global smoke-test command mask a missing local bin.
        env["PATH"] = os.environ["PATH"]
        result = subprocess.run(["npm", "exec", "--offline", "--", "agentsdock", "--version"],
                                cwd=local, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), cli["version"])


if __name__ == "__main__":
    unittest.main()
