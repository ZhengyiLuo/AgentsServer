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
RELEASES_API_URL = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases?per_page=100"
RELEASES_PAGE_URL = f"https://github.com/{RELEASE_REPOSITORY}/releases"
MAX_METADATA_BYTES = 1_000_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
UPDATE_TRACKS = {"stable", "beta"}


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


def version_is_prerelease(version: str) -> bool:
    return "-" in version.split("+", 1)[0]


def version_key(version: str) -> tuple[Any, ...]:
    """Return a SemVer-compatible comparison key for trusted release versions."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid release version: {version}")
    without_build = version.split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers: tuple[tuple[int, int | str], ...] = ()
    if separator:
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    # A release without prerelease identifiers sorts after all prereleases of
    # the same core version, as required by SemVer.
    return major, minor, patch, 0 if separator else 1, identifiers


def release_manifest_url(version: str) -> str:
    return f"{RELEASE_BASE}/download/v{version}/agents-server-manifest.json"


def release_signature_url(version: str) -> str:
    return f"{RELEASE_BASE}/download/v{version}/agents-server-manifest.sig"


def release_candidates(releases: Any, track: str) -> list[str]:
    if track not in UPDATE_TRACKS:
        raise ValueError(f"update track must be one of: {', '.join(sorted(UPDATE_TRACKS))}")
    if not isinstance(releases, list):
        raise RuntimeError("GitHub releases response must be a JSON array")
    candidates: set[str] = set()
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith("v"):
            continue
        version = tag[1:]
        if not VERSION_PATTERN.fullmatch(version):
            continue
        prerelease = version_is_prerelease(version)
        # Require GitHub release metadata and SemVer to agree. This prevents a
        # stable-looking tag accidentally published as a prerelease, or vice
        # versa, from crossing tracks.
        if bool(release.get("prerelease")) != prerelease:
            continue
        if track == "stable" and prerelease:
            continue
        candidates.add(version)
    return sorted(candidates, key=version_key, reverse=True)


def release_versions_from_html(content: bytes) -> set[str]:
    """Extract public release tags from GitHub's release-list fallback page."""
    text = content.decode("utf-8", "replace")
    prefix = f"/{RELEASE_REPOSITORY}/releases/tag/v"
    versions = {
        match.group(1)
        for match in re.finditer(re.escape(prefix) + r"([^\"'<>/?#]+)", text)
        if VERSION_PATTERN.fullmatch(match.group(1))
    }
    return versions


def release_candidates_from_public_pages(track: str, max_pages: int = 20) -> list[str]:
    """Resolve public releases without consuming GitHub API rate limit."""
    versions: set[str] = set()
    for page in range(1, max_pages + 1):
        url = RELEASES_PAGE_URL if page == 1 else f"{RELEASES_PAGE_URL}?page={page}"
        content = download_bytes(url, MAX_METADATA_BYTES)
        page_versions = release_versions_from_html(content)
        versions.update(page_versions)
        next_page = f"{RELEASES_PAGE_URL.removeprefix('https://github.com')}?page={page + 1}"
        if next_page not in content.decode("utf-8", "replace"):
            break
    releases = [
        {
            "tag_name": f"v{version}",
            "prerelease": version_is_prerelease(version),
            "draft": False,
        }
        for version in versions
    ]
    return release_candidates(releases, track)


def verify_manifest(
    manifest_bytes: bytes,
    signature: bytes,
    public_key_path: Path,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
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
    if expected_version is not None and version != expected_version:
        raise RuntimeError("release manifest version does not match its immutable release tag")
    manifest_prerelease = manifest.get("prerelease")
    if manifest_prerelease is not None and bool(manifest_prerelease) != version_is_prerelease(version):
        raise RuntimeError("release manifest prerelease metadata is inconsistent")
    manifest_track = manifest.get("track")
    expected_track = "beta" if version_is_prerelease(version) else "stable"
    if manifest_track is not None and manifest_track != expected_track:
        raise RuntimeError("release manifest track metadata is inconsistent")
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


def check_release(public_key_path: Path, track: str = "stable") -> dict[str, Any]:
    if track not in UPDATE_TRACKS:
        raise ValueError(f"update track must be one of: {', '.join(sorted(UPDATE_TRACKS))}")
    try:
        releases_bytes = download_bytes(RELEASES_API_URL, MAX_METADATA_BYTES)
    except HTTPError as exc:
        if exc.code == 404:
            raise ReleaseUnavailableError("No signed AgentsServer release has been published yet.") from exc
        if exc.code not in {403, 429}:
            raise
        candidates = release_candidates_from_public_pages(track)
    else:
        try:
            releases = json.loads(releases_bytes)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub releases response is invalid JSON") from exc
        candidates = release_candidates(releases, track)
    if not candidates:
        raise ReleaseUnavailableError(f"No signed AgentsServer release is available on the {track} track.")

    for version in candidates:
        try:
            manifest_bytes = download_bytes(release_manifest_url(version), MAX_METADATA_BYTES)
            signature = download_bytes(release_signature_url(version), MAX_METADATA_BYTES)
        except HTTPError as exc:
            # A release can briefly exist before its workflow finishes
            # uploading assets. Only missing assets are skipped; malformed or
            # incorrectly signed assets fail closed.
            if exc.code == 404:
                continue
            raise
        manifest = verify_manifest(
            manifest_bytes,
            signature,
            public_key_path,
            expected_version=version,
        )
        prerelease = version_is_prerelease(version)
        if track == "stable" and prerelease:
            raise RuntimeError("stable update track resolved a prerelease")
        manifest["resolved_track"] = track
        manifest["prerelease"] = prerelease
        return manifest
    raise ReleaseUnavailableError(f"No signed AgentsServer release is available on the {track} track.")


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
    update_status(
        status_path,
        phase="checking",
        track=args.track,
        message=f"Checking the signed {args.track} release track.",
    )
    manifest = check_release(public_key, args.track)
    version = str(manifest["version"])
    if args.expected_version and version != args.expected_version:
        raise RuntimeError(f"latest signed release is {version}, not {args.expected_version}")
    if args.current_version:
        if version_key(version) <= version_key(args.current_version):
            raise RuntimeError(
                f"resolved release {version} is not newer than installed version {args.current_version}; "
                "managed updates do not perform downgrades or reinstalls"
            )

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
        track=args.track,
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
    parser.add_argument("--current-version")
    parser.add_argument("--track", choices=sorted(UPDATE_TRACKS), default="stable")
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
