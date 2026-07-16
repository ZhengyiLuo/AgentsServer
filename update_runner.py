#!/usr/bin/env python3
"""Download, verify, and atomically install an AgentsServer release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from urllib.error import HTTPError
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


RELEASE_REPOSITORY = "ZhengyiLuo/AgentsServer"
RELEASE_BASE = f"https://github.com/{RELEASE_REPOSITORY}/releases"
LATEST_MANIFEST_URL = f"{RELEASE_BASE}/latest/download/agents-server-manifest.json"
LATEST_SIGNATURE_URL = f"{RELEASE_BASE}/latest/download/agents-server-manifest.sig"
MAX_METADATA_BYTES = 1_000_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


class ReleaseUnavailableError(RuntimeError):
    """Raised when the repository has not published a signed release yet."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def update_status(path: Path, **changes: Any) -> dict[str, Any]:
    try:
        current = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        current = {}
    current.update(changes)
    current["updated_at"] = utc_now()
    atomic_json(path, current)
    return current


def download_bytes(url: str, limit: int, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AgentsServer-Updater/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > limit:
            raise RuntimeError(f"download exceeds the {limit}-byte safety limit")
        content = response.read(limit + 1)
    if len(content) > limit:
        raise RuntimeError(f"download exceeds the {limit}-byte safety limit")
    return content


def verify_manifest(manifest_bytes: bytes, signature: bytes, public_key_path: Path) -> dict[str, Any]:
    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise RuntimeError("release public key is not an Ed25519 key")
    key.verify(signature, manifest_bytes)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest must be a JSON object")
    version = str(manifest.get("version") or "")
    if not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError("release manifest contains an invalid version")
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise RuntimeError("release manifest is missing archive metadata")
    expected_name = f"agents-server-{version}.tar.gz"
    archive_name = str(archive.get("name") or "")
    archive_url = str(archive.get("url") or "")
    archive_sha = str(archive.get("sha256") or "").lower()
    expected_prefix = f"{RELEASE_BASE}/download/v{version}/"
    if archive_name != expected_name or archive_url != expected_prefix + expected_name:
        raise RuntimeError("release archive location is not trusted")
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha):
        raise RuntimeError("release archive checksum is invalid")
    return manifest


def check_release(public_key_path: Path) -> dict[str, Any]:
    try:
        manifest_bytes = download_bytes(LATEST_MANIFEST_URL, MAX_METADATA_BYTES)
        signature = download_bytes(LATEST_SIGNATURE_URL, MAX_METADATA_BYTES)
    except HTTPError as exc:
        if exc.code == 404:
            raise ReleaseUnavailableError("No signed AgentsServer release has been published yet.") from exc
        raise
    return verify_manifest(manifest_bytes, signature, public_key_path)


def safe_extract(archive_path: Path, destination: Path) -> Path:
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if destination != target and destination not in target.parents:
                raise RuntimeError("release archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise RuntimeError("release archive must not contain links")
        archive.extractall(destination, members=members, filter="data")
    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install.sh").is_file():
        raise RuntimeError("release archive has an invalid layout")
    return roots[0]


def run_update(args: argparse.Namespace) -> None:
    status_path = Path(args.status_file).expanduser().resolve()
    public_key = Path(args.public_key).expanduser().resolve()
    update_status(status_path, phase="checking", message="Checking the signed release manifest.")
    manifest = check_release(public_key)
    version = str(manifest["version"])
    if args.expected_version and version != args.expected_version:
        raise RuntimeError(f"latest signed release is {version}, not {args.expected_version}")

    with tempfile.TemporaryDirectory(prefix="agents-server-update-") as temporary:
        root = Path(temporary)
        archive_path = root / str(manifest["archive"]["name"])
        update_status(status_path, phase="downloading", target_version=version, message=f"Downloading AgentsServer {version}.")
        archive_bytes = download_bytes(str(manifest["archive"]["url"]), MAX_ARCHIVE_BYTES, timeout=120.0)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if digest != manifest["archive"]["sha256"]:
            raise RuntimeError("release archive checksum does not match the signed manifest")
        archive_path.write_bytes(archive_bytes)

        update_status(status_path, phase="verifying", message="Signature and archive checksum verified.")
        source = safe_extract(archive_path, root / "extracted")
        install = source / "install.sh"
        install.chmod(0o755)
        command = [
            str(install),
            "--non-interactive",
            "--release-version", version,
            "--port", str(args.port),
            "--bind", args.bind,
        ]
        update_status(status_path, phase="installing", message=f"Installing AgentsServer {version} with rollback protection.")
        result = subprocess.run(command, cwd=source, text=True, capture_output=True, timeout=600, check=False)
        log_path = status_path.with_name("server-update.log")
        log_path.write_text((result.stdout or "") + (result.stderr or ""))
        os.chmod(log_path, 0o600)
        if result.returncode != 0:
            tail = "\n".join(((result.stderr or result.stdout or "").strip().splitlines())[-8:])
            raise RuntimeError(f"installer failed ({result.returncode}): {tail or 'no output'}")

    update_status(
        status_path,
        phase="complete",
        message=f"AgentsServer {version} is installed and healthy.",
        installed_version=version,
        finished_at=utc_now(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    try:
        run_update(args)
        return 0
    except Exception as exc:
        update_status(
            Path(args.status_file).expanduser().resolve(),
            phase="failed",
            message=str(exc),
            finished_at=utc_now(),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
