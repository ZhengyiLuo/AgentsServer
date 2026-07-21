import io
import argparse
import json
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
        prerelease = update_runner.version_is_prerelease(version)
        manifest = {
            "schema": 1,
            "version": version,
            "prerelease": prerelease,
            "track": "beta" if prerelease else "stable",
            "api_contract_version": 10,
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
            "prerelease": update_runner.version_is_prerelease(version) if prerelease is None else prerelease,
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
        private, payload, signature = self.signed_manifest("1.2.3-beta.1")
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
                    expected_version="1.2.3-beta.2",
                )

    def test_stable_track_excludes_prereleases_and_beta_uses_semver_order(self):
        releases = [
            self.release("1.2.4-beta.10"),
            self.release("1.2.4-beta.2"),
            self.release("1.2.3"),
            self.release("1.2.2"),
            self.release("9.0.0", draft=True),
            self.release("8.0.0-beta.1", prerelease=False),
        ]
        self.assertEqual(
            update_runner.release_candidates(releases, "stable"),
            ["1.2.3", "1.2.2"],
        )
        self.assertEqual(
            update_runner.release_candidates(releases, "beta"),
            ["1.2.4-beta.10", "1.2.4-beta.2", "1.2.3", "1.2.2"],
        )

    def test_beta_track_moves_to_a_newer_stable_release(self):
        releases = [
            self.release("1.2.4-beta.3"),
            self.release("1.2.4"),
            self.release("1.2.3"),
        ]
        self.assertEqual(update_runner.release_candidates(releases, "beta")[0], "1.2.4")

    def test_signed_beta_release_uses_only_versioned_asset_urls(self):
        private, payload, signature = self.signed_manifest("1.2.4-beta.2")
        releases = json.dumps([
            self.release("1.2.4-beta.2"),
            self.release("1.2.3"),
        ]).encode()
        expected_urls = {
            update_runner.RELEASES_API_URL: releases,
            update_runner.release_manifest_url("1.2.4-beta.2"): payload,
            update_runner.release_signature_url("1.2.4-beta.2"): signature,
        }

        def download(url, _limit, timeout=30.0):
            self.assertNotIn("/latest/", url)
            return expected_urls[url]

        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with patch.object(update_runner, "download_bytes", side_effect=download):
                manifest = update_runner.check_release(public_path, "beta")

        self.assertEqual(manifest["version"], "1.2.4-beta.2")
        self.assertTrue(manifest["prerelease"])
        self.assertEqual(manifest["resolved_track"], "beta")

    def test_signed_stable_track_never_fetches_prerelease_assets(self):
        private, payload, signature = self.signed_manifest("1.2.3")
        releases = json.dumps([
            self.release("2.0.0-beta.1"),
            self.release("1.2.3"),
        ]).encode()
        seen: list[str] = []

        def download(url, _limit, timeout=30.0):
            seen.append(url)
            if url == update_runner.RELEASES_API_URL:
                return releases
            if url == update_runner.release_manifest_url("1.2.3"):
                return payload
            if url == update_runner.release_signature_url("1.2.3"):
                return signature
            raise AssertionError(f"unexpected download: {url}")

        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with patch.object(update_runner, "download_bytes", side_effect=download):
                manifest = update_runner.check_release(public_path, "stable")

        self.assertEqual(manifest["version"], "1.2.3")
        self.assertFalse(any("2.0.0-beta.1" in url for url in seen))

    def test_beta_track_resolves_final_release_after_its_prerelease(self):
        private, payload, signature = self.signed_manifest("1.2.4")
        releases = json.dumps([
            self.release("1.2.4-beta.9"),
            self.release("1.2.4"),
        ]).encode()
        assets = {
            update_runner.RELEASES_API_URL: releases,
            update_runner.release_manifest_url("1.2.4"): payload,
            update_runner.release_signature_url("1.2.4"): signature,
        }
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with patch.object(update_runner, "download_bytes", side_effect=lambda url, *_args, **_kwargs: assets[url]):
                manifest = update_runner.check_release(public_path, "beta")

        self.assertEqual(manifest["version"], "1.2.4")
        self.assertFalse(manifest["prerelease"])
        self.assertEqual(manifest["resolved_track"], "beta")

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

    def test_track_without_any_matching_release_has_a_clear_error(self):
        releases = json.dumps([self.release("2.0.0-beta.1")]).encode()
        with patch.object(update_runner, "download_bytes", return_value=releases):
            with self.assertRaisesRegex(update_runner.ReleaseUnavailableError, "stable track"):
                update_runner.check_release(Path("unused.pem"), "stable")

    def test_rate_limited_release_api_uses_public_release_pages(self):
        private, payload, signature = self.signed_manifest("1.2.4-beta.1")
        limited = HTTPError(update_runner.RELEASES_API_URL, 403, "rate limit", {}, None)
        release_html = (
            f'<a href="/ZhengyiLuo/AgentsServer/releases/tag/v1.2.3">stable</a>'
            f'<a href="/ZhengyiLuo/AgentsServer/releases/tag/v1.2.4-beta.1">beta</a>'
        ).encode()

        def download(url, _limit, timeout=30.0):
            if url == update_runner.RELEASES_API_URL:
                raise limited
            if url == update_runner.RELEASES_PAGE_URL:
                return release_html
            if url == update_runner.release_manifest_url("1.2.4-beta.1"):
                return payload
            if url == update_runner.release_signature_url("1.2.4-beta.1"):
                return signature
            raise AssertionError(f"unexpected download: {url}")

        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with patch.object(update_runner, "download_bytes", side_effect=download):
                manifest = update_runner.check_release(public_path, "beta")

        self.assertEqual(manifest["version"], "1.2.4-beta.1")

    def test_detached_runner_rejects_downgrades_before_download(self):
        args = argparse.Namespace(
            status_file="unused-status.json",
            public_key="unused-key.pem",
            port=7850,
            bind="127.0.0.1",
            expected_version="1.2.3-beta.1",
            current_version="1.2.3",
            track="beta",
        )
        manifest = {"version": "1.2.3-beta.1"}
        with patch.object(update_runner, "update_status"), \
             patch.object(update_runner, "check_release", return_value=manifest), \
             patch.object(update_runner, "download_bytes") as download:
            with self.assertRaisesRegex(RuntimeError, "do not perform downgrades"):
                update_runner.run_update(args)
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
