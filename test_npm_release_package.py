"""npm payload checks without provider imports, service mutation or publication."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
EXECUTION_MODULES = {
    "execution_control.py", "execution_install.py", "execution_maintenance.py",
    "execution_manage.py", "execution_ownership.py", "execution_service.py", "execution_transport.py",
}
sys.path.insert(0, str(ROOT / "scripts"))
import package_npm_release as package


class NpmReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agentsdock-npm-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source" / "server"
        self.root.mkdir(parents=True)
        for name in package.runtime_files():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"fixture: {name}\n")
        (self.root / "VERSION").write_text("1.2.3-beta.4\n")
        (self.root / "agent_server.py").write_text("API_CONTRACT_VERSION = 28\n")
        (self.root / "npm").mkdir()
        for name in ("README.md", "cli.cjs"):
            shutil.copyfile(ROOT / "npm" / name, self.root / "npm" / name)
        shutil.copyfile(ROOT / "package.json", self.root / "package.json")
        for name in ("LICENSE", "NOTICE"):
            (self.root.parent / name).write_text(f"canonical fixture {name}\n")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root.parent, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root.parent, check=True)
        subprocess.run(["git", "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture"], cwd=self.root.parent, check=True)

    def test_standalone_export_legal_documents_match_canonical_checkout(self):
        checkout = package.legal_document_root(ROOT)
        for name in ("LICENSE", "NOTICE"):
            self.assertFalse((ROOT / name).is_symlink())
            self.assertEqual((ROOT / name).read_bytes(), (checkout / name).read_bytes())

    def test_exact_payload_no_tests_secrets_or_lifecycle_scripts(self):
        (self.root / ".env").write_text("must not ship")
        (self.root / "test_private.py").write_text("must not ship")
        for name in ("LICENSE", "NOTICE"):
            (self.root / name).write_text(f"nested decoy {name}\n")
        staged = Path(self.temporary.name) / "package"
        version, expected = package.stage_package(self.root, staged)
        metadata = json.loads((staged / "package.json").read_text())
        self.assertEqual(version, "1.2.3-beta.4")
        self.assertEqual(metadata["version"], version)
        self.assertNotIn("private", metadata)
        self.assertNotIn("scripts", metadata)
        self.assertEqual({p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}, expected)
        self.assertFalse((staged / "server/.env").exists())
        self.assertEqual((staged / "server/install.sh").stat().st_mode & 0o777, 0o755)
        self.assertTrue(json.loads((self.root / "package.json").read_text())["private"])
        for name in ("LICENSE", "NOTICE"):
            self.assertEqual((staged / name).read_bytes(), (self.root.parent / name).read_bytes())

    def standalone_fixture(self):
        # Its folder is deliberately also called server: layout must follow the
        # checkout boundary, not a directory name or an arbitrary parent file.
        standalone = Path(self.temporary.name) / "server"
        shutil.copytree(self.root, standalone)
        for name in ("LICENSE", "NOTICE"):
            (standalone / name).write_text(f"standalone fixture {name}\n")
            (standalone.parent / name).write_text(f"unrelated parent {name}\n")
        subprocess.run(["git", "init", "--quiet"], cwd=standalone, check=True)
        subprocess.run(["git", "add", "."], cwd=standalone, check=True)
        subprocess.run(["git", "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "standalone fixture"], cwd=standalone, check=True)
        return standalone

    def test_missing_standalone_notice_does_not_borrow_parent_notice(self):
        standalone = self.standalone_fixture()
        (standalone / "NOTICE").unlink()
        with self.assertRaisesRegex(SystemExit, "missing release files: NOTICE"):
            package.stage_package(standalone, Path(self.temporary.name) / "bad")

    def test_noncanonical_nested_source_is_rejected(self):
        nested = self.root.with_name("backend")
        self.root.rename(nested)
        with self.assertRaisesRegex(ValueError, "checkout root or its server directory"):
            package.stage_package(nested, Path(self.temporary.name) / "bad")

    def test_rejects_linked_payload_and_unlisted_hub_members(self):
        target = self.root / "agent_server.py"
        target.unlink()
        target.symlink_to(self.root / "VERSION")
        with self.assertRaisesRegex(SystemExit, "linked release"):
            package.stage_package(self.root, Path(self.temporary.name) / "bad")
        target.unlink()
        target.write_text("API_CONTRACT_VERSION = 28\n")
        (self.root / "agentsdock_team_hub/unlisted.py").write_text("not reviewed")
        with self.assertRaisesRegex(SystemExit, "unexpected files"):
            package.stage_package(self.root, Path(self.temporary.name) / "bad")

    def test_minimum_contract_must_be_valid(self):
        for minimum in [0, 29, True]:
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ValueError, "minimum server API"):
                package.prepare(self.root, Path(self.temporary.name) / "bad", minimum_server_api_contract=minimum)

    def test_release_mode_refuses_modified_or_untracked_source(self):
        for filename in ["agent_server.py", "untracked-source.py"]:
            target = self.root / filename
            target.write_text("uncommitted source\n")
            with self.assertRaisesRegex(ValueError, "clean committed source"):
                package.prepare(self.root, Path(self.temporary.name) / "bad", require_clean_source=True)
            subprocess.run(["git", "add", "."], cwd=self.root.parent, check=True)
            subprocess.run(["git", "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture change"], cwd=self.root.parent, check=True)

    @unittest.skipUnless(shutil.which("npm"), "npm required for actual offline pack proof")
    def test_actual_npm_and_legacy_archives_preserve_legal_and_execution_runtime(self):
        # Exercise both real packagers; checking only their arrays would not
        # prove the documents survive the different distribution layouts.
        for name in ("LICENSE", "NOTICE"):
            shutil.copyfile(self.root.parent / name, self.root / name)
        # Copy real execution sources so this checks actual runtime bytes in
        # both layouts, including modules whose entry points run with python -m.
        self.assertTrue(EXECUTION_MODULES <= set(package.runtime_files()))
        for name in EXECUTION_MODULES:
            shutil.copyfile(ROOT / name, self.root / name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(ROOT / "scripts/package_release.py", scripts / "package_release.py")
        legacy_output = Path(self.temporary.name) / "legacy-output"
        subprocess.run([sys.executable, str(scripts / "package_release.py"), "--output", str(legacy_output)], capture_output=True, text=True, check=True)
        npm_output = Path(self.temporary.name) / "npm-output"
        descriptor = package.prepare(self.root, npm_output)
        version = descriptor["version"]
        with tarfile.open(npm_output / descriptor["archive"]["name"]) as npm, tarfile.open(legacy_output / f"agents-server-{version}.tar.gz") as legacy:
            for name in ("LICENSE", "NOTICE"):
                expected = (self.root / name).read_bytes()
                for archive, member_name in [(npm, f"package/{name}"), (npm, f"package/server/{name}"), (legacy, f"agents-server-{version}/{name}")]:
                    with self.subTest(member=member_name):
                        member = archive.getmember(member_name)
                        self.assertTrue(member.isfile())
                        self.assertEqual(member.mode & 0o111, 0)
                        self.assertEqual(archive.extractfile(member).read(), expected)
            for name in sorted(EXECUTION_MODULES):
                expected = (ROOT / name).read_bytes()
                for archive, member_name in [(npm, f"package/server/{name}"), (legacy, f"agents-server-{version}/{name}")]:
                    with self.subTest(member=member_name):
                        member = archive.getmember(member_name)
                        self.assertTrue(member.isfile())
                        self.assertEqual(archive.extractfile(member).read(), expected)

    @unittest.skipUnless(shutil.which("npm"), "npm required for actual offline pack proof")
    def test_real_standalone_npm_pack_keeps_its_own_license_notice_and_source_commit(self):
        standalone = self.standalone_fixture()
        output = Path(self.temporary.name) / "standalone-output"
        manifest = package.prepare(standalone, output, require_clean_source=True)
        expected_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=standalone, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(manifest["commit"], expected_commit)
        with tarfile.open(output / manifest["archive"]["name"]) as tar:
            for name in ("LICENSE", "NOTICE"):
                self.assertEqual(tar.extractfile(f"package/{name}").read(), (standalone / name).read_bytes())
            for name in package.runtime_files():
                self.assertEqual(tar.extractfile(f"package/server/{name}").read(), (standalone / name).read_bytes())

    @unittest.skipUnless(shutil.which("npm"), "npm required for actual offline pack proof")
    def test_real_offline_npm_pack_exact_bytes_hashes_and_version(self):
        output = Path(self.temporary.name) / "output"
        manifest = package.prepare(self.root, output, minimum_server_api_contract=9)
        archive = output / "server-1.2.3-beta.4.tgz"
        data = archive.read_bytes()
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["minimum_server_api_contract"], 9)
        self.assertEqual(manifest["api_contract_version"], 28)
        self.assertEqual(manifest["npm"]["integrity"], "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode())
        self.assertEqual(manifest["archive"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest["archive"]["url"], "https://registry.npmjs.org/@agentsdock/server/-/server-1.2.3-beta.4.tgz")
        with tarfile.open(archive) as tar:
            self.assertEqual(json.load(tar.extractfile("package/package.json"))["version"], "1.2.3-beta.4")
            for name in package.runtime_files():
                self.assertEqual(tar.extractfile(f"package/server/{name}").read(), (self.root / name).read_bytes())
        with self.assertRaises(FileExistsError):
            package.prepare(self.root, output)


if __name__ == "__main__":
    unittest.main()
