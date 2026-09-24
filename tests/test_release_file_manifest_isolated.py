"""Manifest-only release regression: no server, installer, or packager execution."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import re
import shlex
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_MODULES = {
    "execution_control.py", "execution_install.py", "execution_maintenance.py",
    "execution_manage.py", "execution_ownership.py", "execution_service.py", "execution_transport.py",
}
NEW_MODULES = {
    "local_session_ownership.py",
    "cursor_history.py",
    "workspace_git.py",
    "codex_auth.py",
    "codex_provider.py",
    "side_questions.py", "codex_side_question.py", "claude_side_question.py",
    "title_generation.py",
    "server_instances.py",
    "chat_mailbox.py",
    "claude_background_reconciliation.py",
    "claude_goals.py",
    "opencode_agent_client.py",
    "team_mail_runtime.py", "team_mail_websocket.py",
    "team_mail_grants.py",
    "claude_history_repair.py", "claude_history_provenance.py", "codex_history_repair.py", "public_chat_shares.py",
    "public_chat_transcript.py", "public_chat_share_routes.py",
    "interactive_chat_shares.py", "interactive_chat_share_routes.py",
    "interactive_chat_share_web.py", "interactive_chat_projection.py", "interactive_chat_runtime.py",
    "interactive_chat_native.py", "interactive_chat_controls.py", "shared_chat_videos.py", "shared_chat_video_stream.py",
}
NEW_MIGRATION = "migrations/0020_team_mail_arrivals.sql"
THREAD_MIGRATION = "migrations/0021_team_mail_threads.sql"
BULLETIN_MIGRATION = "migrations/0022_team_bulletin_changes.sql"
SEARCH_MIGRATION = "migrations/0023_team_message_search.sql"


def shell_array(source, name):
    match = re.search(rf"(?ms)^{name}=\((.*?)\)", source)
    if match is None:
        raise AssertionError(f"Missing release array {name}")
    return tuple(shlex.split(match[1]))


def manifest_helpers():
    source = ROOT / "scripts" / "package_release.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    constants = {}
    helpers = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"FILES", "DIRECTORY_FILES"}:
                    constants[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.FunctionDef) and node.name in {"validate_release_files", "validate_release_directory"}:
            helpers.append(node)
    if set(constants) != {"FILES", "DIRECTORY_FILES"} or len(helpers) != 2:
        raise AssertionError("Incomplete allowlisted release manifest helpers")
    # Compile only read-only validators. Do not import the packaging module,
    # execute main(), create an archive, or invoke any installer subprocess.
    namespace = {"Path": Path, **constants}
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[])), str(source), "exec"), namespace)
    return namespace


class ReleaseFileManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = manifest_helpers()
        cls.installer = (ROOT / "install.sh").read_text()
        cls.deployer = (ROOT / "deploy.sh").read_text()

    def test_package_and_installer_require_the_same_complete_runtime(self):
        package = self.manifest["FILES"]
        self.assertEqual(shell_array(self.installer, "RELEASE_FILES"), package)
        self.assertEqual(len(package), len(set(package)))
        self.assertTrue(NEW_MODULES <= set(package))
        self.assertTrue(EXECUTION_MODULES <= set(package))
        self.manifest["validate_release_files"](ROOT)

    def test_license_and_notice_are_required_for_packaging_installation_and_deployment(self):
        legal_files = {"LICENSE", "NOTICE"}
        self.assertTrue(legal_files <= set(self.manifest["FILES"]))
        self.assertTrue(legal_files <= set(shell_array(self.installer, "RELEASE_FILES")))
        deployed = {name.removeprefix("$SCRIPT_DIR/") for name in shell_array(self.deployer, "RUNTIME_FILES")}
        self.assertTrue(legal_files <= deployed)
        with tempfile.TemporaryDirectory(prefix="release-legal-manifest-") as temporary:
            root = Path(temporary)
            for name in self.manifest["FILES"]:
                (root / name).write_text("synthetic release member\n")
            for name in sorted(legal_files):
                selected = root / name
                selected.unlink()
                with self.subTest(name=name), self.assertRaisesRegex(SystemExit, name):
                    self.manifest["validate_release_files"](root)
                selected.write_text("synthetic release member\n")

    def test_package_and_installer_require_migration_20_and_exact_hub_members(self):
        package = self.manifest["DIRECTORY_FILES"]["agentsdock_team_hub"]
        self.assertEqual(shell_array(self.installer, "TEAM_HUB_RELEASE_FILES"), package)
        self.assertIn(NEW_MIGRATION, package)
        self.assertIn(THREAD_MIGRATION, package)
        self.assertIn(BULLETIN_MIGRATION, package)
        self.assertIn(SEARCH_MIGRATION, package)
        self.assertIn("notification_hints.py", package)
        self.assertIn("mail_hints.py", package)
        # Validate a clean release tree, not interpreter caches left by other
        # local tests. The real packager still rejects generated bytecode.
        with tempfile.TemporaryDirectory(prefix="release-hub-manifest-") as temporary:
            staged = Path(temporary) / "agentsdock_team_hub"
            for name in package:
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / "agentsdock_team_hub" / name).read_bytes())
            self.manifest["validate_release_directory"](staged)
            (staged / NEW_MIGRATION).unlink()
            with self.assertRaisesRegex(SystemExit, re.escape(NEW_MIGRATION)):
                self.manifest["validate_release_directory"](staged)

    def test_each_new_module_is_required_before_packaging(self):
        with tempfile.TemporaryDirectory(prefix="release-manifest-") as temporary:
            root = Path(temporary)
            for name in self.manifest["FILES"]:
                (root / name).write_text("synthetic release member\n")
            for name in sorted(NEW_MODULES | EXECUTION_MODULES):
                selected = root / name
                selected.unlink()
                with self.subTest(name=name), self.assertRaisesRegex(SystemExit, re.escape(name)):
                    self.manifest["validate_release_files"](root)
                selected.write_text("synthetic release member\n")

    def test_staged_and_direct_deploy_compile_lists_cover_new_modules(self):
        runtime = {name.removeprefix("$SCRIPT_DIR/") for name in shell_array(self.deployer, "RUNTIME_FILES")}
        self.assertTrue(NEW_MODULES | EXECUTION_MODULES <= runtime)
        for name in NEW_MODULES | EXECUTION_MODULES:
            with self.subTest(name=name):
                self.assertIn(f'"$STAGE_DIR/{name}"', self.installer)
                self.assertIn(f"'$REMOTE_SERVER_DIR/{name}'", self.deployer)
                self.assertIn(name.removesuffix(".py"), self.installer.split("PYTHONPATH=\"$STAGE_DIR\"")[-1])

    def test_opencode_translation_layer_is_import_smoked_before_activation(self):
        module = "opencode_agent_client"
        installer_smoke = self.installer.split('PYTHONPATH="$STAGE_DIR"')[-1]
        deploy_smoke = "\n".join(
            line for line in self.deployer.splitlines()
            if "PYTHONPATH='$REMOTE_SERVER_DIR'" in line
        )
        self.assertRegex(installer_smoke, rf"\bimport [^'\n;]*\b{module}\b")
        self.assertRegex(deploy_smoke, rf"\bimport [^'\n;]*\b{module}\b")

    def test_staging_does_not_execute_library_modules_after_chmod(self):
        staging = self.installer.split('chmod 755 "$STAGE_DIR/agent_server.py"', 1)[1]
        before_dependencies = staging.split('echo "[2/7]', 1)[0]
        self.assertEqual(len(before_dependencies.strip().splitlines()), 1)

    def test_frozen_hub_manifest_includes_new_migration_and_current_hashes(self):
        tree = ast.parse((ROOT / "tests" / "test_team_hub_host.py").read_text())
        suite = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VendoredTeamHubParityTests")
        expected = next(ast.literal_eval(node.value) for node in ast.walk(suite)
                        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "expected" for target in node.targets))
        self.assertIn(NEW_MIGRATION, expected)
        self.assertIn(THREAD_MIGRATION, expected)
        self.assertIn(BULLETIN_MIGRATION, expected)
        self.assertIn(SEARCH_MIGRATION, expected)
        self.assertIn("notification_hints.py", expected)
        self.assertEqual(set(expected), set(self.manifest["DIRECTORY_FILES"]["agentsdock_team_hub"]))
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT / "agentsdock_team_hub" / name).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
