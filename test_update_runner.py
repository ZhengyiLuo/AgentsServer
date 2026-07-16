import io
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
        manifest = {
            "schema": 1,
            "version": version,
            "api_contract_version": 7,
            "archive": {
                "name": f"agents-server-{version}.tar.gz",
                "url": f"https://github.com/ZhengyiLuo/AgentsServer/releases/download/v{version}/agents-server-{version}.tar.gz",
                "sha256": "a" * 64,
            },
        }
        payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        return private, payload, private.sign(payload)

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
        missing = HTTPError(update_runner.LATEST_MANIFEST_URL, 404, "Not Found", {}, None)
        with patch.object(update_runner, "download_bytes", side_effect=missing):
            with self.assertRaisesRegex(update_runner.ReleaseUnavailableError, "No signed AgentsServer release"):
                update_runner.check_release(Path("unused.pem"))


if __name__ == "__main__":
    unittest.main()
