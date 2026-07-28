import argparse
import io
import hashlib
import json
import os
import sys
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
    def signed_manifest(
        self,
        version: str = "1.2.3",
        *,
        track: str | None = None,
        prerelease: bool | None = None,
    ):
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
        if track is not None:
            manifest["track"] = track
        if prerelease is not None:
            manifest["prerelease"] = prerelease
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

    def test_legacy_beta_manifest_is_accepted_only_on_beta_track(self):
        private, payload, signature = self.signed_manifest("1.3.0-beta.2")
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            manifest = update_runner.verify_manifest(
                payload,
                signature,
                public_path,
                track="beta",
            )
            with self.assertRaisesRegex(RuntimeError, "requested stable track"):
                update_runner.verify_manifest(payload, signature, public_path)
        self.assertEqual(manifest["version"], "1.3.0-beta.2")

    def test_manifest_track_metadata_must_match_version(self):
        private, payload, signature = self.signed_manifest(
            "1.3.0-beta.2",
            track="stable",
            prerelease=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            public_path = Path(temporary) / "public.pem"
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            with self.assertRaisesRegex(RuntimeError, "track metadata"):
                update_runner.verify_manifest(
                    payload,
                    signature,
                    public_path,
                    track="beta",
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

    def test_beta_release_candidates_exclude_stable_mislabeled_and_drafts(self):
        releases = [
            self.release("1.3.0-beta.2"),
            self.release("1.3.0-beta.1"),
            self.release("1.2.3"),
            self.release("9.0.0-beta.1", draft=True),
            self.release("8.0.0-beta.1", prerelease=False),
        ]
        self.assertEqual(
            update_runner.release_candidates(releases, "beta"),
            ["1.3.0-beta.2", "1.3.0-beta.1"],
        )

    def test_html_release_discovery_filters_by_track(self):
        content = b"""
        <a href="/ZhengyiLuo/AgentsServer/releases/tag/v1.2.3">stable</a>
        <a href="/ZhengyiLuo/AgentsServer/releases/tag/v1.3.0-beta.2">beta</a>
        """
        self.assertEqual(update_runner.release_versions_from_html(content), {"1.2.3"})
        self.assertEqual(
            update_runner.release_versions_from_html(content, "beta"),
            {"1.3.0-beta.2"},
        )

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

    def test_signed_beta_release_uses_only_versioned_asset_urls(self):
        private, payload, signature = self.signed_manifest("1.3.0-beta.2")
        releases = json.dumps([
            self.release("1.3.0-beta.2"),
            self.release("1.2.3"),
        ]).encode()
        assets = {
            update_runner.RELEASES_API_URL: releases,
            update_runner.release_manifest_url("1.3.0-beta.2"): payload,
            update_runner.release_signature_url("1.3.0-beta.2"): signature,
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
                manifest = update_runner.check_release(public_path, "beta")

        self.assertEqual(manifest["version"], "1.3.0-beta.2")
        self.assertFalse(any("/v1.2.3/" in url for url in seen))

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

    def test_installer_streams_log_and_records_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status_path = root / "server-update.json"
            log_path = root / "server-update.log"
            update_runner.run_installer(
                [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(0.08); print('finished')",
                ],
                cwd=root,
                status_path=status_path,
                log_path=log_path,
                version="1.2.3",
                timeout_seconds=2,
                heartbeat_seconds=0.02,
            )

            self.assertEqual(log_path.read_text().splitlines(), ["started", "finished"])
            self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)
            status = json.loads(status_path.read_text())
            self.assertEqual(status["phase"], "installing")
            self.assertGreaterEqual(status["elapsed_seconds"], 1)
            self.assertIn("elapsed", status["message"])

    def test_installer_failure_includes_bounded_log_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, r"installer failed \(7\): useful diagnostic"):
                update_runner.run_installer(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('useful diagnostic', file=sys.stderr, flush=True); raise SystemExit(7)",
                    ],
                    cwd=root,
                    status_path=root / "server-update.json",
                    log_path=root / "server-update.log",
                    version="1.2.3",
                    timeout_seconds=2,
                    heartbeat_seconds=0.02,
                )

    def test_installer_timeout_includes_log_tail_and_stops_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, r"timed out after 0.08 seconds: started"):
                update_runner.run_installer(
                    [
                        sys.executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(5)",
                    ],
                    cwd=root,
                    status_path=root / "server-update.json",
                    log_path=root / "server-update.log",
                    version="1.2.3",
                    timeout_seconds=0.08,
                    heartbeat_seconds=0.02,
                )

    def test_installer_drops_inherited_workspace_environment_selectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured_path = root / "installer-environment.json"
            hostile = {
                name: f"/unrelated/{name.lower()}"
                for name in update_runner.INSTALLER_ENVIRONMENT_SELECTORS
            }
            with patch.dict(os.environ, hostile):
                update_runner.run_installer(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import json, os, pathlib; "
                            f"pathlib.Path({str(captured_path)!r}).write_text("
                            "json.dumps(dict(os.environ)))"
                        ),
                    ],
                    cwd=root,
                    status_path=root / "server-update.json",
                    log_path=root / "server-update.log",
                    version="1.2.3",
                    timeout_seconds=2,
                    heartbeat_seconds=0.02,
                )

            captured = json.loads(captured_path.read_text())
            for name in update_runner.INSTALLER_ENVIRONMENT_SELECTORS:
                self.assertNotIn(name, captured)
            self.assertEqual(captured.get("PATH"), os.environ.get("PATH"))

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
            with self.assertRaisesRegex(RuntimeError, "only permit forward updates"):
                update_runner.run_update(args)
        download.assert_not_called()

    def test_transition_allows_only_forward_or_beta_to_stable(self):
        self.assertTrue(update_runner.release_transition_allowed("1.2.3", "1.2.4", "stable"))
        self.assertTrue(update_runner.release_transition_allowed("1.3.0-beta.2", "1.2.4", "stable"))
        self.assertFalse(update_runner.release_transition_allowed("1.2.4", "1.2.3", "stable"))
        self.assertFalse(update_runner.release_transition_allowed("1.3.0-beta.2", "1.3.0-beta.1", "beta"))
        self.assertFalse(update_runner.release_transition_allowed("1.3.0", "1.4.0-beta.1", "stable"))
        self.assertFalse(update_runner.release_transition_allowed("1.2.3", "1.2.3", "stable"))

    def test_successful_update_records_default_stable_track(self):
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
                 patch.object(update_runner, "run_installer") as install:
                update_runner.run_update(args)

        self.assertEqual(statuses[-1]["phase"], "complete")
        self.assertEqual(statuses[-1]["installed_version"], "1.2.4")
        self.assertEqual(statuses[-1]["track"], "stable")
        install.assert_called_once()

    def test_detached_runner_allows_explicit_beta_to_latest_stable_switch(self):
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
                current_version="1.3.0-beta.2",
                track="stable",
            )
            statuses: list[dict] = []

            with patch.object(update_runner, "check_release", return_value=manifest) as check, \
                 patch.object(update_runner, "download_bytes", return_value=archive_bytes), \
                 patch.object(update_runner, "update_status", side_effect=lambda _path, **changes: statuses.append(changes) or changes), \
                 patch.object(update_runner, "run_installer") as install:
                update_runner.run_update(args)

        check.assert_called_once_with(Path(args.public_key).resolve(), "stable")
        install.assert_called_once()
        self.assertEqual(statuses[-1]["phase"], "complete")
        self.assertEqual(statuses[-1]["installed_version"], "1.2.4")
        self.assertEqual(statuses[-1]["track"], "stable")


if __name__ == "__main__":
    unittest.main()
