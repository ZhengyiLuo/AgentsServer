from __future__ import annotations

import base64
import hashlib
import hmac
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from agentsdock_team_hub.security import (
    ACCESS_TOKEN_TTL_SECONDS,
    AccessTokenSigner,
    TokenError,
    canonical_json,
    ensure_private_directory,
    read_secret_file,
    token_hash,
)
from agentsdock_team_hub.cli import main as cli_main


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class AccessTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = b"k" * 32
        self.signer = AccessTokenSigner(self.key)

    def custom_token(self, header: dict, payload: dict) -> str:
        head = encode(canonical_json(header))
        body = encode(canonical_json(payload))
        signed = f"{head}.{body}".encode("ascii")
        signature = encode(hmac.new(self.key, signed, hashlib.sha256).digest())
        return f"{head}.{body}.{signature}"

    def test_access_token_exact_claims_and_time_bounds(self) -> None:
        minted = self.signer.mint("human_12345678", "device_12345678", now=1000)
        payload = self.signer.verify(minted.token, now=1001)
        self.assertEqual(payload["sub"], "human_12345678")
        with self.assertRaises(TokenError):
            self.signer.verify(minted.token, now=1000 + ACCESS_TOKEN_TTL_SECONDS)
        with self.assertRaises(ValueError):
            self.signer.mint(
                "human_12345678",
                "device_12345678",
                now=1000,
                ttl_seconds=ACCESS_TOKEN_TTL_SECONDS + 1,
            )

    def test_malformed_unicode_and_wrong_headers_fail_as_token_errors(self) -> None:
        malformed = ["x.é.x", "é.é.é", "a.b", "...", "a" * 9000]
        for token in malformed:
            with self.subTest(token=token[:20]):
                with self.assertRaises(TokenError):
                    self.signer.verify(token, now=1000)
        with self.assertRaises(TokenError):
            token_hash("valid-length-\ud800-credential")
        payload = {
            "iss": "agentsdock-team-hub",
            "aud": "agentsdock-team-hub-api",
            "sub": "human_12345678",
            "sid": "device_12345678",
            "jti": "jti_12345678",
            "iat": 1000,
            "nbf": 1000,
            "exp": 1100,
        }
        for header in (
            {"alg": "none", "typ": "ADTH-AT1"},
            {"alg": "HS256", "typ": "JWT"},
            {"alg": "HS256", "typ": "ADTH-AT1", "kid": "extra"},
        ):
            with self.assertRaises(TokenError):
                self.signer.verify(self.custom_token(header, payload), now=1001)

    def test_future_bool_and_overlong_claims_fail(self) -> None:
        base_payload = {
            "iss": "agentsdock-team-hub",
            "aud": "agentsdock-team-hub-api",
            "sub": "human_12345678",
            "sid": "device_12345678",
            "jti": "jti_12345678",
            "iat": 1000,
            "nbf": 1000,
            "exp": 1100,
        }
        header = {"alg": "HS256", "typ": "ADTH-AT1"}
        mutations = [
            {"iat": True},
            {"nbf": 1001},
            {"iat": 2000, "nbf": 2000, "exp": 2100},
            {"exp": 1000 + ACCESS_TOKEN_TTL_SECONDS + 1},
            {"aud": "other"},
            {"sub": ""},
            {"sid": "s" * 241},
            {"jti": "j" * 129},
            {"sub": "human\ncontrol"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(TokenError):
                    self.signer.verify(
                        self.custom_token(header, {**base_payload, **mutation}), now=1001
                    )
        with self.assertRaises(ValueError):
            self.signer.mint("human\ud800", "device_12345678", now=1000)

    def test_secret_reader_rejects_symlink_and_wrong_mode(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only Team Hub V1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"s" * 32)
            os.chmod(source, 0o600)
            self.assertEqual(read_secret_file(source), b"s" * 32)
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                read_secret_file(link)
            os.chmod(source, 0o644)
            with self.assertRaises(PermissionError):
                read_secret_file(source)
            real_directory = root / "real-directory"
            real_directory.mkdir(mode=0o700)
            linked_directory = root / "linked-directory"
            linked_directory.symlink_to(real_directory, target_is_directory=True)
            with self.assertRaises(OSError):
                ensure_private_directory(linked_directory)

    def test_cli_tls_arguments_are_both_or_neither_and_remote_requires_tls(self) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            self.assertEqual(
                cli_main(
                    [
                        "serve",
                        "--data-dir",
                        directory,
                        "--ssl-certfile",
                        str(Path(directory) / "cert.pem"),
                    ]
                ),
                2,
            )
            self.assertEqual(
                cli_main(["serve", "--data-dir", directory, "--host", "0.0.0.0"]),
                2,
            )

    def test_cli_rejects_exposed_tls_private_key(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX-only Team Hub V1")
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            certificate = root / "cert.pem"
            private_key = root / "key.pem"
            certificate.write_text("not parsed while uvicorn is mocked\n")
            private_key.write_text("private test key material\n")
            os.chmod(certificate, 0o644)
            os.chmod(private_key, 0o644)
            arguments = [
                "serve",
                "--data-dir",
                str(root / "hub-data"),
                "--ssl-certfile",
                str(certificate),
                "--ssl-keyfile",
                str(private_key),
            ]
            self.assertEqual(cli_main(arguments), 2)
            os.chmod(private_key, 0o600)
            with mock.patch("agentsdock_team_hub.cli.uvicorn.run") as run:
                self.assertEqual(cli_main(arguments), 0)
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
