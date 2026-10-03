#!/usr/bin/env python3
"""Prepare the public agentsdock CLI; never publish or mutate server services."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from package_npm_release import VERSION_PATTERN, copy_regular, legal_document_root, verify_archive


def stage_package(root: Path, destination: Path) -> tuple[str, set[str]]:
    version = (root / "VERSION").read_text().strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must be a stable or beta.N semantic version")
    source = root / "npm" / "agentsdock"
    metadata = json.loads((source / "package.json").read_text())
    if (metadata.get("name") != "agentsdock" or metadata.get("private") is not True
            or metadata.get("bin") != {"agentsdock": "cli.cjs"}
            or metadata.get("dependencies") != {"@agentsdock/server": "0.0.0-development"}
            or metadata.get("scripts") != {"postinstall": "node postinstall.cjs"}
            or metadata.get("optionalDependencies")):
        raise ValueError("Expected private agentsdock CLI source with its exact dependency and reviewed postinstall hook")
    metadata["version"] = version
    metadata["dependencies"] = {"@agentsdock/server": version}
    metadata.pop("private")
    legal_root = legal_document_root(root)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "package.json").write_text(json.dumps(metadata, indent=2) + "\n")
    copy_regular(source / "cli.cjs", destination / "cli.cjs", executable=True)
    copy_regular(source / "postinstall.cjs", destination / "postinstall.cjs")
    copy_regular(source / "README.md", destination / "README.md")
    for name in ("LICENSE", "NOTICE"):
        copy_regular(legal_root / name, destination / name)
    return version, {"package.json", "cli.cjs", "postinstall.cjs", "README.md", "LICENSE", "NOTICE"}


def prepare(root: Path, output: Path, *, npm: str = "npm", require_clean_source: bool = False) -> dict:
    if require_clean_source and subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip():
        raise ValueError("CLI release preparation requires a clean committed source checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="agentsdock-cli-pack-") as temporary:
        work = Path(temporary)
        staged = work / "package"
        version, expected = stage_package(root, staged)
        for name in ("user.npmrc", "global.npmrc"):
            (work / name).touch()
        environment = {key: value for key, value in os.environ.items()
                       if not key.lower().startswith("npm_config_") and key not in {"NODE_AUTH_TOKEN", "NPM_TOKEN"}}
        environment.update({"NPM_CONFIG_CACHE": str(work / "cache"),
                            "NPM_CONFIG_USERCONFIG": str(work / "user.npmrc"),
                            "NPM_CONFIG_GLOBALCONFIG": str(work / "global.npmrc")})
        result = subprocess.run([npm, "pack", "--ignore-scripts", "--offline", "--json", "--pack-destination", str(work)],
                                cwd=staged, env=environment, capture_output=True, text=True, check=True)
        records = json.loads(result.stdout)
        archive_name = f"agentsdock-{version}.tgz"
        if not isinstance(records, list) or len(records) != 1 or records[0].get("filename") != archive_name:
            raise ValueError("npm returned an unexpected CLI archive")
        archive = work / archive_name
        verify_archive(archive, expected, staged)
        data = archive.read_bytes()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
        if records[0].get("integrity") != integrity:
            raise ValueError("npm integrity did not match the CLI archive")
        receipt = {"schema": 1, "name": "agentsdock", "version": version, "commit": commit,
                   "runtime": {"name": "@agentsdock/server", "version": version},
                   "archive": {"name": archive_name, "sha256": hashlib.sha256(data).hexdigest(),
                               "integrity": integrity, "size": len(data)}}
        output.mkdir(parents=True, exist_ok=True)
        # A pair is published only into a fresh preparation directory, preventing
        # one half from replacing already reviewed bytes after a failed retry.
        archive_target = output / archive_name
        receipt_target = output / "agentsdock-cli-receipt.json"
        if archive_target.exists() or receipt_target.exists():
            raise FileExistsError("Output already contains a prepared CLI artifact")
        with archive_target.open("xb") as stream:
            stream.write(data)
        with receipt_target.open("x") as stream:
            stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-clean-source", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(Path(__file__).resolve().parents[1], args.output.resolve(),
                             require_clean_source=args.require_clean_source), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
