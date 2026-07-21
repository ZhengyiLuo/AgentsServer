#!/usr/bin/env python3
"""Build a deterministic AgentsServer release archive and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


FILES = (
    "agent_server.py",
    "agentsdock_jobs.py",
    "install.sh",
    "update_runner.py",
    "pyproject.toml",
    "uv.lock",
    "VERSION",
    "release-public-key.pem",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text().strip()
    output = (root / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive_name = f"agents-server-{version}.tar.gz"
    archive_path = output / archive_name

    missing = [name for name in FILES if not (root / name).is_file()]
    if missing:
        raise SystemExit(f"missing release files: {', '.join(missing)}")
    with tempfile.TemporaryDirectory(prefix="agents-server-package-") as temporary:
        package_root = Path(temporary) / f"agents-server-{version}"
        package_root.mkdir()
        for name in FILES:
            target = package_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        (package_root / "install.sh").chmod(0o755)
        (package_root / "agentsdock_jobs.py").chmod(0o755)
        (package_root / "update_runner.py").chmod(0o755)
        with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(package_root, arcname=package_root.name)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    manifest = {
        "schema": 1,
        "version": version,
        "api_contract_version": 8,
        "commit": commit,
        "archive": {
            "name": archive_name,
            "url": f"https://github.com/ZhengyiLuo/AgentsServer/releases/download/v{version}/{archive_name}",
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "size": archive_path.stat().st_size,
        },
    }
    (output / "agents-server-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
