import argparse
import io
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import update_runner


class UpdateRunnerTests(unittest.TestCase):
    def signed_manifest(self, version: str = "1.2.3"):
        private = Ed25519PrivateKey.generate()
        manifest = {
            "schema": 1,
            "version": version,
            "api_contract_version": 9,
            "archive": {
                "name": f"agents-server-{version}.tar.gz",
                "url": f"https://github.com/ZhengyiLuo/AgentsServer/releases/download/v{version}/agents-server-{version}.tar.gz",
                "sha256": "a" * 64,
            },
        }
        payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        return private, payload, private.sign(payload)

    @staticmethod
    def release(version: str, *, prerelease: bool | None = None, draft: bool = False):
        return {
            "tag_name": f"v{version}",
            "prerelease": ("-" in version) if prerelease is None else prerelease,
            "draft": draft,
        }

    def test_signed_manifest_accepts_only_trusted_release_location(self):
        private, payload, signature = self.signed_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            manifest = update_runner.verify_manifest(payload, signature, public_path)
        self.assertEqual(manifest["version"], "1.2.3")

    def test_manifest_signature_tampering_is_rejected(self):
        private, payload, signature = self.signed_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with self.assertRaises(Exception):
                update_runner.verify_manifest(payload + b" ", signature, public_path)

    def test_manifest_must_match_immutable_release_tag(self):
        private, payload, signature = self.signed_manifest("1.2.3")
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with self.assertRaisesRegex(RuntimeError, "immutable release tag"):
                update_runner.verify_manifest(
                    payload,
                    signature,
                    public_path,
                    expected_version="1.2.4",
                )

    def test_stable_release_candidates_exclude_prereleases_and_drafts(self):
        releases = [
            self.release("1.2.4-beta.2"),
            self.release("1.2.3"),
            self.release("1.2.2"),
            self.release("9.0.0", draft=True),
            self.release("8.0.0", prerelease=True),
        ]
        self.assertEqual(update_runner.stable_release_candidates(releases), ["1.2.3", "1.2.2"])

    def test_signed_stable_release_uses_only_versioned_asset_urls(self):
        private, payload, signature = self.signed_manifest("1.2.3")
        releases = json.dumps([
            self.release("2.0.0-beta.1"),
            self.release("1.2.3"),
        ]).encode()
        assets = {
            update_runner.RELEASES_API_URL: releases,
            update_runner.release_manifest_url("1.2.3"): payload,
            update_runner.release_signature_url("1.2.3"): signature,
        }
        seen: list[str] = []

        def download(url, _limit, timeout=30.0):
            seen.append(url)
            self.assertNotIn("/latest/", url)
            return assets[url]

        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with patch.object(update_runner, "download_bytes", side_effect=download):
                manifest = update_runner.check_release(public_path)

        self.assertEqual(manifest["version"], "1.2.3")
        self.assertFalse(any("2.0.0-beta.1" in url for url in seen))

    def test_safe_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "bad.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                entry = tarfile.TarInfo("../outside")
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                update_runner.safe_extract(archive_path, root / "extract")

    def test_status_write_is_durable_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admin" / "status.json"
            update_runner.update_status(path, phase="checking", update_id="abc")
            update_runner.update_status(path, phase="complete")
            value = json.loads(path.read_text())
        self.assertEqual(value["phase"], "complete")
        self.assertEqual(value["update_id"], "abc")

    def test_missing_release_has_a_clear_error(self):
        missing = HTTPError(update_runner.RELEASES_API_URL, 404, "Not Found", {}, None)
        with patch.object(update_runner, "download_bytes", side_effect=missing):
            with self.assertRaisesRegex(update_runner.ReleaseUnavailableError, "No signed AgentsServer release"):
                update_runner.check_release(Path("unused.pem"))

    def test_detached_runner_rejects_downgrades_before_download(self):
        args = argparse.Namespace(
            status_file="unused-status.json",
            public_key="unused-key.pem",
            port=7850,
            bind="127.0.0.1",
            expected_version="1.2.3",
            current_version="1.2.4",
        )
        with patch.object(update_runner, "update_status"), \
             patch.object(update_runner, "check_release", return_value={"version": "1.2.3"}), \
             patch.object(update_runner, "download_bytes") as download:
            with self.assertRaisesRegex(RuntimeError, "do not perform downgrades"):
                update_runner.run_update(args)
        download.assert_not_called()

    def test_successful_update_completes_without_track_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
                installer = b"#!/bin/sh\nexit 0\n"
                entry = tarfile.TarInfo("agents-server-1.2.4/install.sh")
                entry.mode = 0o755
                entry.size = len(installer)
                archive.addfile(entry, io.BytesIO(installer))
            archive_bytes = archive_buffer.getvalue()
            manifest = {
                "version": "1.2.4",
                "archive": {
                    "name": "agents-server-1.2.4.tar.gz",
                    "url": "https://example.invalid/agents-server-1.2.4.tar.gz",
                    "sha256": hashlib.sha256(archive_bytes).hexdigest(),
                },
            }
            args = argparse.Namespace(
                status_file=str(root / "server-update.json"),
                public_key=str(root / "release-public-key.pem"),
                port=7850,
                bind="127.0.0.1",
                expected_version="1.2.4",
                current_version="1.2.3",
            )
            statuses: list[dict] = []

            def record_status(_path, **changes):
                statuses.append(changes)
                return changes

            with patch.object(update_runner, "check_release", return_value=manifest), \
                 patch.object(update_runner, "download_bytes", return_value=archive_bytes), \
                 patch.object(update_runner, "update_status", side_effect=record_status), \
                 patch.object(
                     update_runner.subprocess,
                     "run",
                     return_value=subprocess.CompletedProcess([], 0, stdout="installed\n", stderr=""),
                 ):
                update_runner.run_update(args)

        self.assertEqual(statuses[-1]["phase"], "complete")
        self.assertEqual(statuses[-1]["installed_version"], "1.2.4")
        self.assertNotIn("track", statuses[-1])


if __name__ == "__main__":
    unittest.main()
