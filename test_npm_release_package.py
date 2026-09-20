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
            shutil.copyfile(ROOT.parent / name, self.root.parent / name)
        subprocess.run(["git", "init", "--quiet"], cwd=self.root.parent, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root.parent, check=True)
        subprocess.run(["git", "-c", "user.name=Package Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture"], cwd=self.root.parent, check=True)

    def test_exact_payload_no_tests_secrets_or_lifecycle_scripts(self):
        (self.root / ".env").write_text("must not ship")
        (self.root / "test_private.py").write_text("must not ship")
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
