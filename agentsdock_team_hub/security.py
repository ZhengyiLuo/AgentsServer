"""Local key lifecycle and compact signed Team Hub access credentials."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any


ACCESS_TOKEN_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
BOOTSTRAP_PROOF_TTL_SECONDS = 15 * 60
TOKEN_ISSUER = "agentsdock-team-hub"
TOKEN_AUDIENCE = "agentsdock-team-hub-api"


class TokenError(RuntimeError):
    """A signed or opaque credential failed closed."""


@dataclass(frozen=True)
class MintedAccessToken:
    token: str
    jti: str
    expires_at: int


def now_seconds() -> int:
    return int(time.time())


def token_hash(value: str) -> bytes:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        hashlib.sha256(b"invalid-team-hub-credential").digest()
        raise TokenError("credential is invalid or unavailable")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        hashlib.sha256(b"invalid-team-hub-credential").digest()
        raise TokenError("credential is invalid or unavailable") from exc
    return hashlib.sha256(encoded).digest()


def opaque_secret(prefix: str) -> tuple[str, bytes]:
    token = f"{prefix}.{secrets.token_urlsafe(32)}"
    return token, hashlib.sha256(token.encode("ascii")).digest()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or len(value) > 8192:
        raise TokenError("credential is invalid or unavailable")
    try:
        return base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise TokenError("credential is invalid or unavailable") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_fingerprint(value: Any) -> bytes:
    return hashlib.sha256(canonical_json(value)).digest()


def _validate_secret_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise PermissionError(f"secret path is not an owner-controlled regular file: {path}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError(f"secret file must have mode 0600: {path}")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4097))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if not 32 <= len(data) <= 4096:
            raise PermissionError(f"secret file has an invalid size: {path}")
        return data
    finally:
        os.close(descriptor)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"Team Hub data path must be an owner-controlled directory: {path}")
        os.fchmod(descriptor, 0o700)
        hardened = os.fstat(descriptor)
        if stat.S_IMODE(hardened.st_mode) != 0o700 or hardened.st_uid != os.getuid():
            raise PermissionError(f"Team Hub data directory must have mode 0700: {path}")
    finally:
        os.close(descriptor)


def create_secret_file(path: Path, value: bytes) -> None:
    """Create an owner-only secret without following links or overwriting data."""

    ensure_private_directory(path.parent)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(path.parent, directory_flags)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(value):
                written += os.write(descriptor, value[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    _validate_secret_file(path)


def load_or_create_signing_key(path: Path) -> bytes:
    try:
        return _validate_secret_file(path)
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            create_secret_file(path, key)
        except FileExistsError:
            return _validate_secret_file(path)
        return key


def read_secret_file(path: Path) -> bytes:
    return _validate_secret_file(path)


def validate_tls_files(certificate_path: Path, private_key_path: Path) -> None:
    """Fail closed on unsafe listener TLS material before starting Uvicorn.

    Certificates are public and may use ordinary read permissions.  The
    private key must be a single-link, current-user-owned regular file with
    exact mode 0600.  Both files are opened without following symlinks and are
    bounded so devices/FIFOs and implausibly large inputs never reach the TLS
    loader.
    """

    def inspect(path: Path, *, private: bool) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError(f"TLS path must be a single-link regular file: {path}")
            if private and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError(
                    f"TLS private key must be owned by the current user with mode 0600: {path}"
                )
            if not 1 <= info.st_size <= 1024 * 1024:
                raise PermissionError(f"TLS file has an invalid size: {path}")
            # Force a read now so an unreadable or unexpectedly truncated file
            # is rejected before the listener is created. Uvicorn performs the
            # cryptographic PEM validation when loading its SSL context.
            if not os.read(descriptor, 1):
                raise PermissionError(f"TLS file is empty: {path}")
        finally:
            os.close(descriptor)

    inspect(Path(certificate_path), private=False)
    inspect(Path(private_key_path), private=True)


class AccessTokenSigner:
    """Mint and verify audience-bound HMAC-SHA256 access tokens.

    The local signing key never enters SQLite, HTTP bodies, logs, or renderer
    state. Authorization still re-reads session, principal, membership and ACL
    state from SQLite for every request.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("access-token signing key must contain at least 32 bytes")
        self._key = bytes(key)

    def mint(
        self,
        human_principal_id: str,
        device_session_id: str,
        *,
        now: int | None = None,
        ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    ) -> MintedAccessToken:
        issued_at = now_seconds() if now is None else int(now)
        if not 1 <= int(ttl_seconds) <= ACCESS_TOKEN_TTL_SECONDS:
            raise ValueError("access-token ttl is outside the supported bound")
        if (
            not 1 <= len(human_principal_id) <= 240
            or not 1 <= len(device_session_id) <= 240
            or not all(33 <= ord(char) <= 126 for char in human_principal_id)
            or not all(33 <= ord(char) <= 126 for char in device_session_id)
        ):
            raise ValueError("access-token subject or session is invalid")
        expires_at = issued_at + int(ttl_seconds)
        jti = secrets.token_urlsafe(18)
        header = {"alg": "HS256", "typ": "ADTH-AT1"}
        payload = {
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "sub": human_principal_id,
            "sid": device_session_id,
            "jti": jti,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": expires_at,
        }
        encoded_header = _b64url_encode(canonical_json(header))
        encoded_payload = _b64url_encode(canonical_json(payload))
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        return MintedAccessToken(
            f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}",
            jti,
            expires_at,
        )

    def verify(self, token: str, *, now: int | None = None) -> dict[str, Any]:
        if not isinstance(token, str) or not 40 <= len(token) <= 8192:
            raise TokenError("credential is invalid or unavailable")
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenError("credential is invalid or unavailable")
        try:
            signing_input = f"{parts[0]}.{parts[1]}".encode("ascii", "strict")
        except UnicodeEncodeError as exc:
            raise TokenError("credential is invalid or unavailable") from exc
        supplied_signature = _b64url_decode(parts[2])
        expected_signature = hmac.new(
            self._key, signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise TokenError("credential is invalid or unavailable")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            payload = json.loads(_b64url_decode(parts[1]))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TokenError("credential is invalid or unavailable") from exc
        if header != {"alg": "HS256", "typ": "ADTH-AT1"}:
            raise TokenError("credential is invalid or unavailable")
        if not isinstance(payload, dict) or set(payload) != {
            "iss", "aud", "sub", "sid", "jti", "iat", "nbf", "exp"
        }:
            raise TokenError("credential is invalid or unavailable")
        if payload["iss"] != TOKEN_ISSUER or payload["aud"] != TOKEN_AUDIENCE:
            raise TokenError("credential is invalid or unavailable")
        if not all(isinstance(payload[name], str) for name in ("sub", "sid", "jti")):
            raise TokenError("credential is invalid or unavailable")
        if (
            not 1 <= len(payload["sub"]) <= 240
            or not 1 <= len(payload["sid"]) <= 240
            or not 1 <= len(payload["jti"]) <= 128
            or any(
                not all(33 <= ord(char) <= 126 for char in payload[name])
                for name in ("sub", "sid", "jti")
            )
        ):
            raise TokenError("credential is invalid or unavailable")
        if not all(
            isinstance(payload[name], int) and not isinstance(payload[name], bool)
            for name in ("iat", "nbf", "exp")
        ):
            raise TokenError("credential is invalid or unavailable")
        timestamp = now_seconds() if now is None else int(now)
        if payload["nbf"] != payload["iat"] or payload["iat"] > timestamp + 5:
            raise TokenError("credential is invalid or unavailable")
        if (
            payload["exp"] <= timestamp
            or payload["exp"] <= payload["iat"]
            or payload["exp"] - payload["iat"] > ACCESS_TOKEN_TTL_SECONDS
        ):
            raise TokenError("credential is invalid or unavailable")
        return payload
