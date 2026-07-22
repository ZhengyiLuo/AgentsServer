import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ReleaseContractTests(unittest.TestCase):
    def test_candidate_version_stays_on_0_1_12_beta_line(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "0.1.12-beta.3")

    def test_workflow_marks_prerelease_tags(self):
        workflow = (ROOT / ".github" / "workflows" / "server-release.yml").read_text()
        self.assertIn("release_args+=(--prerelease)", workflow)
        self.assertIn('"${RELEASE_TAG#v}" == *-*', workflow)
        self.assertIn("rm -rf dist", workflow)

    def test_updater_does_not_use_mutable_latest_asset_urls(self):
        updater = (ROOT / "update_runner.py").read_text()
        self.assertNotIn("/latest/download/", updater)

    def test_beta_package_manifest_and_archive_are_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    str(ROOT / ".venv" / "bin" / "python"),
                    str(ROOT / "scripts" / "package_release.py"),
                    "--output",
                    temporary,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((Path(temporary) / "agents-server-manifest.json").read_text())
            self.assertEqual(manifest["version"], "0.1.12-beta.3")
            self.assertEqual(manifest["api_contract_version"], 10)
            self.assertTrue(manifest["prerelease"])
            self.assertEqual(manifest["track"], "beta")
            archive = Path(temporary) / manifest["archive"]["name"]
            with tarfile.open(archive, "r:gz") as bundle:
                names = set(bundle.getnames())
            prefix = "agents-server-0.1.12-beta.3"
            self.assertIn(f"{prefix}/agent_server.py", names)
            self.assertIn(f"{prefix}/codex_app_server.py", names)
            self.assertIn(f"{prefix}/update_runner.py", names)


if __name__ == "__main__":
    unittest.main()
