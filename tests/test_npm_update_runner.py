"""Signed npm payload tests: no server import, service or provider processes."""
import argparse
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import update_runner


class NpmUpdateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = Ed25519PrivateKey.generate()
        self.key = self.root / "public.pem"
        self.key.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        self.version = "1.0.4-beta.12"

    def archive(self, entries=None):
        buffer = io.BytesIO()
        entries = entries if entries is not None else {
            "package/package.json": json.dumps({"name": "@agentsdock/server", "version": self.version}).encode(),
            "package/server/install.sh": b"#!/bin/sh\nexit 0\n",
            "package/server/VERSION": self.version.encode(),
        }
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, value in entries.items():
                entry = tarfile.TarInfo(name)
                if isinstance(value, tarfile.TarInfo):
                    archive.addfile(value)
                else:
                    entry.size = len(value)
                    archive.addfile(entry, io.BytesIO(value))
        return buffer.getvalue()

    def manifest(self, content=b"payload"):
        return {
            "schema": 2, "distribution": "npm", "version": self.version,
            "track": "beta", "prerelease": True, "api_contract_version": 28,
            "commit": "a" * 40,
            "npm": {"name": "@agentsdock/server", "version": self.version,
                    "integrity": "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()},
            "archive": {"name": f"server-{self.version}.tgz",
                        "url": f"https://registry.npmjs.org/@agentsdock/server/-/server-{self.version}.tgz",
                        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
        }

    def envelope(self, manifest):
        payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        return {"manifest_base64": base64.b64encode(payload).decode(),
                "signature_base64": base64.b64encode(self.private.sign(payload)).decode()}

    def test_exact_signed_package_is_accepted_without_registry_lookup(self):
        manifest = self.manifest()
        with patch.object(update_runner, "download_bytes") as download:
            self.assertEqual(update_runner.verify_npm_release_envelope(self.envelope(manifest), self.key), manifest)
        download.assert_not_called()

    def test_tampered_signed_bytes_are_rejected(self):
        envelope = self.envelope(self.manifest())
        envelope["manifest_base64"] = base64.b64encode(base64.b64decode(envelope["manifest_base64"]) + b" ").decode()
        with self.assertRaises(InvalidSignature):
            update_runner.verify_npm_release_envelope(envelope, self.key)

    def test_signed_descriptor_cannot_choose_arbitrary_package_or_location(self):
        changes = [
            ("npm", "name", "@attacker/server"), ("npm", "version", "1.0.5"),
            ("archive", "name", "other.tgz"),
            ("archive", "url", "http://127.0.0.1/payload"),
            ("archive", "url", self.manifest()["archive"]["url"] + "?redirect=1"),
            ("archive", "size", True), ("archive", "size", update_runner.MAX_ARCHIVE_BYTES + 1),
            ("archive", "sha256", "not-a-hash"), ("npm", "integrity", "sha512-AAAA"),
        ]
        for section, field, value in changes:
            with self.subTest(section=section, field=field, value=value):
                manifest = self.manifest()
                manifest[section][field] = value
                with self.assertRaises(RuntimeError):
                    update_runner.verify_npm_release_envelope(self.envelope(manifest), self.key)

    def test_schema_two_requires_explicit_signed_track(self):
        self.version = "1.0.4"
        manifest = self.manifest()
        manifest.pop("track")
        manifest["prerelease"] = False
        with self.assertRaises(RuntimeError):
            update_runner.verify_npm_release_envelope(self.envelope(manifest), self.key)

    def test_invalid_envelopes_are_rejected_before_signature_or_download(self):
        for envelope in ({}, {"manifest_base64": "?", "signature_base64": "A" * 88},
                         {"manifest_base64": "A" * 11000, "signature_base64": "A" * 88}):
            with self.subTest(envelope_length=len(str(envelope))), self.assertRaises(RuntimeError):
                update_runner.verify_npm_release_envelope(envelope, self.key)

    def test_schema_two_is_not_accepted_through_legacy_discovery(self):
        envelope = self.envelope(self.manifest())
        with self.assertRaisesRegex(RuntimeError, "schema"):
            update_runner.verify_manifest(base64.b64decode(envelope["manifest_base64"]),
                                          base64.b64decode(envelope["signature_base64"]), self.key, track="beta")

    def test_size_and_both_hashes_are_verified(self):
        manifest = self.manifest()
        update_runner.verify_npm_archive(b"payload", manifest)
        for content in (b"payloae", b"payload!", b""):
            with self.subTest(content=content), self.assertRaises(RuntimeError):
                update_runner.verify_npm_archive(content, manifest)
        changed = copy.deepcopy(manifest)
        changed["npm"]["integrity"] = "sha512-" + base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(RuntimeError):
            update_runner.verify_npm_archive(b"payload", changed)

    def test_download_is_exact_bounded_and_rejects_redirected_result(self):
        manifest = self.manifest()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {"Content-Length": "7"}
        response.geturl.return_value = manifest["archive"]["url"]
        response.read.return_value = b"payload"
        opener = Mock()
        opener.open.return_value = response
        with patch.object(update_runner.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(update_runner.download_npm_archive(manifest), b"payload")
            response.read.assert_called_once_with(8)
            self.assertEqual(opener.open.call_args.args[0].full_url, manifest["archive"]["url"])
            response.geturl.return_value = "https://example.invalid/redirect"
            with self.assertRaisesRegex(RuntimeError, "location"):
                update_runner.download_npm_archive(manifest)

    def test_npm_archive_extracts_only_matching_server_payload(self):
        content = self.archive()
        archive = self.root / "server.tgz"
        archive.write_bytes(content)
        source = update_runner.safe_extract(archive, self.root / "unpacked", npm_manifest=self.manifest(content))
        self.assertEqual(source, (self.root / "unpacked/package/server").resolve())
        self.assertEqual((source / "VERSION").read_text(), self.version)

    def test_npm_archive_rejects_links_traversal_and_payload_mismatch(self):
        link = tarfile.TarInfo("package/server/install.sh")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/other"
        cases = [
            {"../escape": b"bad"}, {link.name: link},
            {"package/package.json": b'{"name":"@wrong/server","version":"1.0.4-beta.12"}',
             "package/server/install.sh": b"", "package/server/VERSION": self.version.encode()},
            {"package/package.json": json.dumps({"name": "@agentsdock/server", "version": self.version}).encode(),
             "package/server/install.sh": b"", "package/server/VERSION": b"0.0.1"},
        ]
        for index, entries in enumerate(cases):
            content = self.archive(entries)
            path = self.root / f"invalid-{index}.tgz"
            path.write_bytes(content)
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                update_runner.safe_extract(path, self.root / f"bad-{index}", npm_manifest=self.manifest(content))

    def test_detached_runner_uses_durable_signed_descriptor_and_existing_installer(self):
        content = self.archive()
        manifest = self.manifest(content)
        status_file = self.root / "status.json"
        status_file.write_text(json.dumps({"update_id": "update-npm-test", "phase": "starting",
                                           "_npm_release": self.envelope(manifest)}))
        args = argparse.Namespace(status_file=str(status_file), public_key=str(self.key), port=7850,
                                  bind="127.0.0.1", expected_version=self.version,
                                  current_version="1.0.4-beta.11", track="beta", npm_descriptor=True,
                                  expected_server_identity="test-server", update_id="update-npm-test")
        with patch.object(update_runner, "check_release") as legacy, \
                patch.object(update_runner, "download_npm_archive", return_value=content), \
                patch.object(update_runner, "wait_for_server_idle") as idle, \
                patch.object(update_runner, "run_installer") as install, \
                patch.object(update_runner, "assert_post_update_identity") as identity:
            update_runner.run_update(args)
        legacy.assert_not_called()
        idle.assert_called_once()
        install.assert_called_once()
        self.assertTrue(install.call_args.args[0][0].endswith("package/server/install.sh"))
        self.assertIn("--expected-server-identity", install.call_args.args[0])
        command = install.call_args.args[0]
        self.assertEqual(command[command.index("--expected-api-contract") + 1], "28")
        identity.assert_called_once()
        self.assertEqual(identity.call_args.kwargs["expected_server_version"], self.version)
        self.assertEqual(identity.call_args.kwargs["expected_api_contract_version"], 28)
        self.assertEqual(json.loads(status_file.read_text())["phase"], "complete")

    def test_npm_acceptance_checks_actual_version_and_api_contract(self):
        health = {"ok": True, "server_identity": "test-server", "server_version": self.version,
                  "api_contract_version": 28,
                  "capabilities": {"secure_peer_v1": dict(update_runner.SECURE_PEER_HEALTH_REQUIREMENTS)}}
        arguments = {"token": "test-token", "expected_server_identity": "test-server",
                     "expected_server_version": self.version, "expected_api_contract_version": 28}
        with patch.object(update_runner, "server_health_snapshot", return_value=health):
            update_runner.assert_post_update_identity(7850, **arguments)
        for changed in ({"server_version": "1.0.4-beta.11"}, {"api_contract_version": 29}, {"api_contract_version": True}):
            with self.subTest(changed=changed), \
                    patch.object(update_runner, "server_health_snapshot", return_value={**health, **changed}), \
                    self.assertRaises(RuntimeError):
                update_runner.assert_post_update_identity(7850, **arguments)


if __name__ == "__main__":
    unittest.main()
