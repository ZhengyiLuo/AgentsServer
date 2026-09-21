#!/usr/bin/env python3
"""Prepare a publishable server npm tarball and its separately signable descriptor.

No npm login, installation, lifecycle script, service operation, or publication
is performed. The legacy release allowlist remains the runtime source of truth.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from package_release import FILES, DIRECTORY_FILES, validate_release_files, validate_release_directory

PACKAGE_NAME = "@agentsdock/server"
MANIFEST_NAME = "agents-server-npm-manifest.json"
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-beta\.([1-9][0-9]*))?$")
EXECUTABLE_FILES = {
    "install.sh", "uninstall.sh", "agentsdock_jobs.py", "agentsdock_chats.py",
    "agentsdock_emergency.py", "agentsdock_publish.py", "agentsdock_mail.py",
    "agentsdock_team.py", "update_runner.py",
}


def runtime_files() -> tuple[str, ...]:
    return (*FILES, *(f"{directory}/{name}" for directory, names in DIRECTORY_FILES.items() for name in names))


def copy_regular(source: Path, target: Path, *, executable: bool = False) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"package input must be a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if executable else 0o644)


def legal_document_root(root: Path) -> Path:
    # Both supported layouts keep legal documents at the checkout boundary:
    # AgentsDock/server is nested; the standalone compatibility export is not.
    # Never fall back to unrelated parent files or mix documents across layouts.
    checkout = Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()).resolve()
    if root.resolve() not in (checkout, checkout / "server"):
        raise ValueError("server source must be the checkout root or its server directory")
    return checkout


def stage_package(root: Path, destination: Path) -> tuple[str, set[str]]:
    version = (root / "VERSION").read_text().strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must be a stable or beta.N semantic version")
    validate_release_files(root)
    for directory in DIRECTORY_FILES:
        validate_release_directory(root / directory)
    metadata = json.loads((root / "package.json").read_text())
    if metadata.get("name") != PACKAGE_NAME or metadata.get("private") is not True:
        raise ValueError("source package must have the expected name and remain private")
    if metadata.get("scripts") or metadata.get("dependencies") or metadata.get("optionalDependencies"):
        raise ValueError("server wrapper must not run lifecycle scripts or install npm dependencies")
    legal_root = legal_document_root(root)
    metadata["version"] = version
    metadata.pop("private")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "package.json").write_text(json.dumps(metadata, indent=2) + "\n")
    copy_regular(root / "npm" / "README.md", destination / "README.md")
    copy_regular(legal_root / "LICENSE", destination / "LICENSE")
    copy_regular(legal_root / "NOTICE", destination / "NOTICE")
    copy_regular(root / "npm" / "cli.cjs", destination / "npm" / "cli.cjs", executable=True)
    for name in runtime_files():
        copy_regular(root / name, destination / "server" / name, executable=name in EXECUTABLE_FILES)
    expected = {"package.json", "README.md", "LICENSE", "NOTICE", "npm/cli.cjs"}
    expected.update(f"server/{name}" for name in runtime_files())
    return version, expected


def verify_archive(path: Path, expected: set[str], staged: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or set(names) != {f"package/{name}" for name in expected}:
            raise ValueError("npm archive does not contain the exact staged file allowlist")
        for member in members:
            if not member.isfile():
                raise ValueError("npm archive contains a link or non-regular member")
            source = staged / member.name.removeprefix("package/")
            stream = archive.extractfile(member)
            if stream is None or stream.read() != source.read_bytes():
                raise ValueError(f"npm changed a staged payload: {member.name}")
            if member.mode != (source.stat().st_mode & 0o777):
                raise ValueError(f"npm changed executable permissions: {member.name}")


def prepare(root: Path, output: Path, *, npm: str = "npm", minimum_server_api_contract: int = 8, require_clean_source: bool = False) -> dict:
    if require_clean_source and subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, capture_output=True, text=True, check=True).stdout.strip():
        raise ValueError("release preparation requires a clean committed source checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release requires a full source commit")
    contract = re.search(r"(?m)^API_CONTRACT_VERSION = ([0-9]+)$", (root / "agent_server.py").read_text())
    if contract is None:
        raise ValueError("server API contract version is missing")
    if type(minimum_server_api_contract) is not int or not 1 <= minimum_server_api_contract <= int(contract.group(1)):
        raise ValueError("minimum server API contract must be positive and no greater than the provided API contract")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentsdock-npm-") as temporary:
        work = Path(temporary)
        staged = work / "package"
        version, expected = stage_package(root, staged)
        user_config, global_config = work / "user.npmrc", work / "global.npmrc"
        user_config.touch()
        global_config.touch()
        environment = {key: value for key, value in os.environ.items() if not key.lower().startswith("npm_config_") and key not in {"NODE_AUTH_TOKEN", "NPM_TOKEN"}}
        environment.update({"NPM_CONFIG_CACHE": str(work / "cache"), "NPM_CONFIG_USERCONFIG": str(user_config), "NPM_CONFIG_GLOBALCONFIG": str(global_config)})
        result = subprocess.run([npm, "pack", "--ignore-scripts", "--offline", "--json", "--pack-destination", str(work)], cwd=staged, env=environment, capture_output=True, text=True, check=True)
        records = json.loads(result.stdout)
        filename = records[0]["filename"] if isinstance(records, list) and len(records) == 1 else ""
        if not re.fullmatch(r"agentsdock-server-[A-Za-z0-9.-]+\.tgz", filename):
            raise ValueError("npm returned an unexpected package filename")
        packed = work / filename
        verify_archive(packed, expected, staged)
        archive_bytes = packed.read_bytes()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode("ascii")
        if records[0].get("integrity") != integrity:
            raise ValueError("npm integrity did not match the prepared archive")
        archive_name = f"server-{version}.tgz"
        manifest = {
            "schema": 2, "version": version,
            "track": "beta" if "-" in version else "stable", "prerelease": "-" in version,
            "api_contract_version": int(contract.group(1)), "commit": commit,
            "minimum_server_api_contract": minimum_server_api_contract,
            "distribution": "npm", "npm": {"name": PACKAGE_NAME, "version": version, "integrity": integrity},
            "archive": {"name": archive_name, "url": f"https://registry.npmjs.org/@agentsdock/server/-/{archive_name}", "sha256": hashlib.sha256(archive_bytes).hexdigest(), "size": len(archive_bytes)},
        }
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if len(manifest_text.encode("utf-8")) > 8192:
            raise ValueError("signed descriptor exceeds the server's 8 KiB contract")
        if (output / archive_name).exists() or (output / MANIFEST_NAME).exists():
            raise FileExistsError("release output already contains a prepared artifact")
        # Exclusive creation prevents silently replacing a previously reviewed candidate.
        with (output / archive_name).open("xb") as destination:
            destination.write(archive_bytes)
        with (output / MANIFEST_NAME).open("x") as destination:
            destination.write(manifest_text)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-server-api-contract", type=int, default=8)
    parser.add_argument("--require-clean-source", action="store_true")
    args = parser.parse_args()
    manifest = prepare(Path(__file__).resolve().parents[1], args.output.resolve(), minimum_server_api_contract=args.minimum_server_api_contract, require_clean_source=args.require_clean_source)
    print(json.dumps({"archive": str(args.output / manifest["archive"]["name"]), "manifest": str(args.output / MANIFEST_NAME), "version": manifest["version"], "integrity": manifest["npm"]["integrity"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
