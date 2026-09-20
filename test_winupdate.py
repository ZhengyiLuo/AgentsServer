#!/usr/bin/env python3
"""Tests for winupdate (Windows-native update staging/switch/rollback).

Everything runs in tempfile.mkdtemp directories with fake release trees and a
fake HTTP health server polled on an ephemeral port (>= 48951). The real
install.sh path is never exercised; nothing is registered with schtasks.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import update_runner
import winupdate


FAKE_SERVER = '''
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
healthy = sys.argv[2] == "healthy"
parent = int(sys.argv[3])


def parent_alive():
    if os.name != "nt":
        try:
            os.kill(parent, 0)
            return True
        except OSError:
            return False
    # os.kill(pid, 0) is unreliable from CREATE_NO_WINDOW children on current
    # CPython builds (WinError 87); use OpenProcess directly.
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, parent)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def watch_parent():
    while True:
        if not parent_alive():
            os._exit(0)
        time.sleep(0.25)


threading.Thread(target=watch_parent, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": healthy}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

LOCK_HOLDER = '''
import ctypes
import sys
import time

path, ready = sys.argv[1], sys.argv[2]
handle = ctypes.windll.kernel32.CreateFileW(path, 0x80000000, 0, None, 3, 0, None)
if handle == -1:
    sys.exit(2)
with open(ready, "w") as stream:
    stream.write("ready")
time.sleep(60)
'''

SWITCH_DRIVER = '''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, @REPO_ROOT@)

import winupdate
config = json.loads(Path(sys.argv[1]).read_text())
live = Path(config["live"])
staged = Path(config["staged"])
port = config["port"]
processes = []


def start():
    process = subprocess.Popen(
        [sys.executable, str(live / "fake_server.py"), str(port), "healthy", str(os.getpid())],
        cwd=str(live),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    processes.append(process)
    Path(config["pidfile"]).write_text(str(process.pid))


def stop():
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except Exception:
                process.kill()
                process.wait()
    processes.clear()


marker = Path(config["marker"])


def health():
    marker.write_text("called")
    while True:
        time.sleep(0.2)


try:
    winupdate.switch_with_rollback(
        staged_dir=staged,
        live_dir=live,
        stop_server=stop,
        start_server=start,
        health_check=health,
        settle_seconds=0.5,
        attempts=60,
        status_path=Path(config["status"]),
    )
    Path(config["result"]).write_text("switched")
except BaseException:
    Path(config["result"]).write_text("error")
'''.replace("@REPO_ROOT@", repr(str(REPO_ROOT)))


def ephemeral_port() -> int:
    for _ in range(50):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        if port >= 48951:
            return port
    raise RuntimeError("no ephemeral port >= 48951 available")


def write_fake_release(target: Path, version: str, healthy: bool = True) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    (target / "VERSION").write_text(version + "\n", encoding="utf-8")
    (target / "HEALTH").write_text("healthy" if healthy else "sick", encoding="utf-8")
    (target / "fake_server.py").write_text(FAKE_SERVER, encoding="utf-8")
    return target


def make_tar_gz(root_name: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode()
            entry = tarfile.TarInfo(f"{root_name}/{name}")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_manifest(version: str = "1.2.3", sha256: str = "a" * 64) -> dict:
    name = f"agents-server-{version}.tar.gz"
    return {
        "schema": 1,
        "version": version,
        "archive": {
            "name": name,
            "url": f"{update_runner.RELEASE_BASE}/download/v{version}/{name}",
            "sha256": sha256,
        },
    }


def sign_manifest(private_key: Ed25519PrivateKey, manifest_bytes: bytes) -> bytes:
    return private_key.sign(manifest_bytes)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="winupdate-test-")
        self.root = Path(self._tmp)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def make_keypair(self):
        private = Ed25519PrivateKey.generate()
        public_path = self.root / "test-public.pem"
        public_path.write_bytes(
            private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        return private, public_path


class ServerHarness:
    """Start/stop/health for the fake release server rooted at a live dir."""

    def __init__(self, live_dir: Path, port: int):
        self.live_dir = Path(live_dir)
        self.port = port
        self.processes: list[subprocess.Popen] = []

    def start(self, live_dir: Path | None = None, flavor: str | None = None) -> None:
        live = Path(live_dir) if live_dir is not None else self.live_dir
        if flavor is None:
            try:
                flavor = (live / "HEALTH").read_text(encoding="utf-8").strip() or "healthy"
            except OSError:
                flavor = "healthy"
        process = subprocess.Popen(
            [
                sys.executable,
                str(live / "fake_server.py"),
                str(self.port),
                flavor,
                str(os.getpid()),
            ],
            cwd=str(live),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.processes.clear()

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/health", timeout=2
            ) as response:
                return json.loads(response.read()).get("ok") is True
        except Exception:
            return False

    def wait_healthy(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.health():
                return True
            time.sleep(0.2)
        return False


@contextlib.contextmanager
def status_spy():
    calls: list[tuple[Path, dict]] = []
    real = winupdate.update_status

    def spy(path, **changes):
        calls.append((Path(path), dict(changes)))
        return real(path, **changes)

    with mock.patch.object(winupdate, "update_status", side_effect=spy):
        yield calls


def recorded_phases(calls) -> list[str]:
    return [changes.get("phase") for _, changes in calls if changes.get("phase")]


class VerifyReleaseTests(TempCase):
    def _signed_release(self, version="1.2.3", sha256=None):
        private, public_path = self.make_keypair()
        manifest = make_manifest(version, sha256 or "b" * 64)
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        signature = sign_manifest(private_key=private, manifest_bytes=manifest_bytes)
        return manifest, manifest_bytes, signature, public_path

    def test_accepts_valid_manifest(self):
        manifest, manifest_bytes, signature, public_path = self._signed_release()
        verified = winupdate.verify_release(manifest_bytes, signature, public_path)
        self.assertEqual(verified["version"], "1.2.3")
        self.assertEqual(verified["archive"]["sha256"], "b" * 64)

    def test_rejects_bad_signature(self):
        _manifest, manifest_bytes, _sig, public_path = self._signed_release()
        other_private, _ = self.make_keypair()
        bad_signature = other_private.sign(b"tampered")
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.verify_release(manifest_bytes, bad_signature, public_path)

    def test_rejects_wrong_expected_version(self):
        _manifest, manifest_bytes, signature, public_path = self._signed_release()
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.verify_release(
                manifest_bytes, signature, public_path, expected_version="9.9.9"
            )

    def test_rejects_malformed_sha256(self):
        _manifest, manifest_bytes, signature, public_path = self._signed_release(
            sha256="not-hex"
        )
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.verify_release(manifest_bytes, signature, public_path)

    def test_rejects_prerelease_on_stable_track(self):
        private, public_path = self.make_keypair()
        manifest = make_manifest("1.2.3-beta.1")
        manifest["prerelease"] = True
        manifest["track"] = "beta"
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        signature = private.sign(manifest_bytes)
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.verify_release(manifest_bytes, signature, public_path, track="stable")
        verified = winupdate.verify_release(manifest_bytes, signature, public_path, track="beta")
        self.assertEqual(verified["version"], "1.2.3-beta.1")

    def test_rejects_non_ed25519_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_path = self.root / "rsa-public.pem"
        public_path.write_bytes(
            rsa_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        _manifest, manifest_bytes, signature, _ = self._signed_release()
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.verify_release(manifest_bytes, signature, public_path)


class ExtractSafeTests(TempCase):
    def test_happy_path_tar_gz_returns_single_root(self):
        archive = make_tar_gz(
            "agents-server-1.2.3", {"VERSION": b"1.2.3\n", "app/main.py": b"x = 1\n"}
        )
        extracted = winupdate.extract_safe(archive, self.root / "out")
        self.assertEqual(extracted.name, "agents-server-1.2.3")
        self.assertEqual((extracted / "VERSION").read_text().strip(), "1.2.3")

    def test_happy_path_zip(self):
        archive = make_zip({"VERSION": b"2.0.0\n"})
        extracted = winupdate.extract_safe(archive, self.root / "out")
        self.assertEqual((extracted / "VERSION").read_text().strip(), "2.0.0")

    def test_rejects_tar_traversal(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("../evil.txt")
            payload = b"evil"
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        with self.assertRaisesRegex(winupdate.WindowsUpdateError, "unsafe path"):
            winupdate.extract_safe(buffer.getvalue(), self.root / "out")
        self.assertFalse((self.root / "evil.txt").exists())

    def test_rejects_zip_traversal_forward_and_backslash(self):
        for name in ("../evil.txt", "..\\evil.txt"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(winupdate.WindowsUpdateError, "unsafe path"):
                    winupdate.extract_safe(make_zip({name: b"evil"}), self.root / "out")
                self.assertFalse((self.root / "evil.txt").exists())

    def test_rejects_tar_symlink_member(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("pkg/link")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "target"
            archive.addfile(entry)
        with self.assertRaisesRegex(winupdate.WindowsUpdateError, "must not contain links"):
            winupdate.extract_safe(buffer.getvalue(), self.root / "out")

    def test_rejects_zip_symlink_member(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("pkg/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        with self.assertRaisesRegex(winupdate.WindowsUpdateError, "must not contain links"):
            winupdate.extract_safe(buffer.getvalue(), self.root / "out")

    def test_rejects_member_count_cap(self):
        files = {f"pkg/f{i:05d}.txt": b"x" for i in range(winupdate.MAX_MEMBERS + 1)}
        with self.assertRaisesRegex(winupdate.WindowsUpdateError, "members"):
            winupdate.extract_safe(make_zip(files), self.root / "out")

    def test_rejects_oversized_archive_bytes(self):
        big = b"\x1f\x8b" + b"0" * (winupdate.MAX_ARCHIVE_BYTES + 1)
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.extract_safe(big, self.root / "out")

    def test_rejects_unknown_format(self):
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.extract_safe(b"not an archive", self.root / "out")


class StageReleaseTests(TempCase):
    def _archive_and_manifest(self, version="1.2.3"):
        archive = make_tar_gz(
            f"agents-server-{version}", {"VERSION": f"{version}\n", "app.py": b"# app\n"}
        )
        manifest = make_manifest(version, hashlib.sha256(archive).hexdigest())
        return manifest, archive

    def test_stage_happy_path(self):
        manifest, archive = self._archive_and_manifest()
        status_path = self.root / "status.json"
        staging_root = self.root / "staging"
        staged = winupdate.stage_release(
            status_path=status_path,
            manifest=manifest,
            archive_bytes=archive,
            staging_root=staging_root,
        )
        self.assertTrue(staged.is_dir())
        self.assertEqual(staged.parent, staging_root.resolve())
        self.assertEqual((staged / "VERSION").read_text().strip(), "1.2.3")
        status = json.loads(status_path.read_text())
        self.assertEqual(status["phase"], "staged")
        self.assertEqual(status["target_version"], "1.2.3")

    def test_stage_rejects_checksum_mismatch(self):
        manifest, archive = self._archive_and_manifest()
        manifest["archive"]["sha256"] = "c" * 64
        status_path = self.root / "status.json"
        with self.assertRaisesRegex(winupdate.ReleaseVerificationError, "checksum"):
            winupdate.stage_release(
                status_path=status_path,
                manifest=manifest,
                archive_bytes=archive,
                staging_root=self.root / "staging",
            )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["phase"], "failed")
        self.assertFalse((self.root / "staging").exists())

    def test_stage_cleans_temp_on_bad_archive(self):
        manifest = make_manifest("1.2.3", hashlib.sha256(b"garbage").hexdigest())
        with self.assertRaises(winupdate.WindowsUpdateError):
            winupdate.stage_release(
                status_path=self.root / "status.json",
                manifest=manifest,
                archive_bytes=b"garbage",
                staging_root=self.root / "staging",
            )
        leftovers = [
            entry
            for entry in (self.root / "staging").iterdir()
            if entry.name.startswith(".extract") or entry.name.startswith("staged-")
        ]
        self.assertEqual(leftovers, [])


class SwitchTests(TempCase):
    def setUp(self):
        super().setUp()
        self.live = self.root / "live"
        self.status_path = self.root / "status.json"
        self.port = ephemeral_port()
        write_fake_release(self.live, "1.0.0")
        self.harness = ServerHarness(self.live, self.port)
        self.addCleanup(self.harness.stop)

    def _switch(self, staged, *, attempts=6, settle=0.4, **overrides):
        options = dict(
            staged_dir=staged,
            live_dir=self.live,
            stop_server=self.harness.stop,
            start_server=lambda: self.harness.start(),
            health_check=self.harness.health,
            settle_seconds=settle,
            attempts=attempts,
            status_path=self.status_path,
        )
        options.update(overrides)
        return winupdate.switch_with_rollback(**options)

    def test_happy_path_switches_and_removes_backup(self):
        self.harness.start()
        self.assertTrue(self.harness.wait_healthy())
        staged = write_fake_release(self.root / "staged-v2", "2.0.0")
        with status_spy() as calls:
            result = self._switch(staged)
        self.assertTrue(result.switched)
        self.assertFalse(result.rolled_back)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "2.0.0")
        self.assertFalse((self.root / "live.old").exists())
        self.assertFalse((self.root / "live.failed").exists())
        self.assertTrue(self.harness.wait_healthy())
        phases = recorded_phases(calls)
        for expected in ("stopping", "switching", "starting", "health_check", "complete"):
            self.assertIn(expected, phases)
        self.assertLess(phases.index("stopping"), phases.index("switching"))
        self.assertLess(phases.index("switching"), phases.index("health_check"))

    def test_health_failure_rolls_back_to_previous(self):
        self.harness.start()
        self.assertTrue(self.harness.wait_healthy())
        staged = write_fake_release(self.root / "staged-v2", "2.0.0", healthy=False)
        with status_spy() as calls:
            result = self._switch(staged)
        self.assertFalse(result.switched)
        self.assertTrue(result.rolled_back)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertFalse((self.root / "live.old").exists())
        self.assertFalse((self.root / "live.failed").exists())
        self.assertTrue(self.harness.wait_healthy())
        self.assertEqual(recorded_phases(calls)[-1], "rolled_back")

    def test_interrupted_health_exception_then_clean_retry(self):
        self.harness.start()
        self.assertTrue(self.harness.wait_healthy())
        staged = write_fake_release(self.root / "staged-v2", "2.0.0")

        def failing_health_for(target_version):
            def health():
                try:
                    if (self.live / "VERSION").read_text().strip() == target_version:
                        raise RuntimeError("simulated updater termination during health check")
                except FileNotFoundError:
                    pass
                return self.harness.health()

            return health

        result = self._switch(staged, health_check=failing_health_for("2.0.0"))
        self.assertTrue(result.rolled_back)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertTrue(self.harness.wait_healthy())

        staged_v3 = write_fake_release(self.root / "staged-v3", "3.0.0")
        result = self._switch(staged_v3)
        self.assertTrue(result.switched)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "3.0.0")
        self.assertFalse((self.root / "live.old").exists())
        self.assertFalse((self.root / "live.failed").exists())
        self.assertTrue(self.harness.wait_healthy())

    def test_full_pipeline_stage_then_switch(self):
        self.harness.start()
        self.assertTrue(self.harness.wait_healthy())
        version = "4.1.0"
        archive = make_tar_gz(
            f"agents-server-{version}",
            {
                "VERSION": f"{version}\n",
                "HEALTH": "healthy\n",
                "fake_server.py": FAKE_SERVER,
            },
        )
        manifest = make_manifest(version, hashlib.sha256(archive).hexdigest())
        private, public_path = self.make_keypair()
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        signature = private.sign(manifest_bytes)
        verified = winupdate.verify_release(manifest_bytes, signature, public_path)
        staged = winupdate.stage_release(
            status_path=self.status_path,
            manifest=verified,
            archive_bytes=archive,
            staging_root=self.root / "staging",
        )
        result = self._switch(staged)
        self.assertTrue(result.switched)
        self.assertEqual((self.live / "VERSION").read_text().strip(), version)
        self.assertTrue(self.harness.wait_healthy())

    @unittest.skipUnless(os.name == "nt", "Windows file locking semantics")
    def test_locked_file_raises_naming_path_and_recovers(self):
        locked = self.live / "data.db"
        locked.write_bytes(b"locked")
        holder_script = self.root / "lock_holder.py"
        holder_script.write_text(LOCK_HOLDER, encoding="utf-8")
        ready = self.root / "lock.ready"
        holder = subprocess.Popen(
            [sys.executable, str(holder_script), str(locked), str(ready)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if holder.poll() is not None:
                self.fail("lock holder exited early")
            time.sleep(0.1)
        self.assertTrue(ready.exists())

        staged = write_fake_release(self.root / "staged-v2", "2.0.0")
        with self.assertRaises(winupdate.FileLockError) as caught:
            self._switch(staged, move_attempts=3, retry_base_seconds=0.05)
        self.assertIn(str(self.live.resolve()), str(caught.exception))

        holder.kill()
        holder.wait(timeout=10)
        time.sleep(0.2)  # let the OS release the sharing lock

        result = self._switch(staged)
        self.assertTrue(result.switched)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "2.0.0")
        self.assertTrue(self.harness.wait_healthy())

    def test_locked_file_simulated_when_not_reproducible(self):
        real_replace = os.replace
        staged = write_fake_release(self.root / "staged-v2", "2.0.0")

        def flaky(source, target):
            if Path(source) == self.live.resolve():
                raise PermissionError(13, "Access is denied", str(source))
            return real_replace(source, target)

        with mock.patch.object(os, "replace", side_effect=flaky):
            with self.assertRaises(winupdate.WindowsUpdateError) as caught:
                self._switch(staged, move_attempts=2, retry_base_seconds=0.02)
        self.assertIn(str(self.live.resolve()), str(caught.exception))
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertFalse((self.root / "live.old").exists())
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "failed")

    def test_process_killed_mid_switch_recovers_on_next_run(self):
        staged_v2 = write_fake_release(self.root / "staged-v2", "2.0.0")
        driver = self.root / "switch_driver.py"
        driver.write_text(SWITCH_DRIVER, encoding="utf-8")
        config = {
            "live": str(self.live),
            "staged": str(staged_v2),
            "port": self.port,
            "marker": str(self.root / "health.marker"),
            "pidfile": str(self.root / "server.pid"),
            "status": str(self.status_path),
            "result": str(self.root / "driver.result"),
        }
        config_path = self.root / "driver.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(driver), str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._terminate, process)
        deadline = time.monotonic() + 60
        while not Path(config["marker"]).exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("driver exited before its health check")
            time.sleep(0.1)
        self.assertTrue(Path(config["marker"]).exists())
        process.kill()
        process.wait(timeout=10)

        # The moves completed before the kill: v2 is live, v1 is the backup.
        self.assertEqual((self.live / "VERSION").read_text().strip(), "2.0.0")
        backup = self.root / "live.old"
        self.assertEqual((backup / "VERSION").read_text().strip(), "1.0.0")
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "health_check")

        # The orphaned grandchild exits on its own once the driver is gone.
        pid = int(Path(config["pidfile"]).read_text().strip())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.2)

        restored = winupdate.rollback(
            self.live,
            backup,
            stop_server=self.harness.stop,
            start_server=lambda: self.harness.start(),
            health_check=self.harness.health,
            settle_seconds=0.4,
            attempts=6,
            status_path=self.status_path,
        )
        self.assertTrue(restored)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertFalse(backup.exists())
        self.assertTrue(self.harness.wait_healthy())

        # A subsequent switch is not confused by the interrupted run.
        staged_v3 = write_fake_release(self.root / "staged-v3", "3.0.0")
        result = self._switch(staged_v3)
        self.assertTrue(result.switched)
        self.assertEqual((self.live / "VERSION").read_text().strip(), "3.0.0")
        self.assertTrue(self.harness.wait_healthy())

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def test_first_install_without_backup_keeps_failed_tree_aside(self):
        fresh_live = self.root / "fresh"  # deliberately absent: no backup exists
        staged = write_fake_release(self.root / "staged-v2", "2.0.0", healthy=False)
        harness = ServerHarness(fresh_live, self.port)
        self.addCleanup(harness.stop)
        with self.assertRaises(winupdate.HealthCheckError):
            self._switch(
                staged,
                live_dir=fresh_live,
                stop_server=harness.stop,
                start_server=lambda: harness.start(live_dir=fresh_live),
            )
        self.assertFalse(fresh_live.exists())
        sick = self.root / "fresh.failed"
        self.assertEqual((sick / "VERSION").read_text().strip(), "2.0.0")


class RollbackTests(TempCase):
    def test_rollback_requires_backup(self):
        live = write_fake_release(self.root / "live", "1.0.0")
        with self.assertRaisesRegex(winupdate.WindowsUpdateError, "no backup"):
            winupdate.rollback(
                live,
                self.root / "live.old",
                stop_server=lambda: None,
                start_server=lambda: None,
                health_check=lambda: True,
            )


class StatusHeartbeatTests(TempCase):
    def test_final_status_after_success_and_failure(self):
        port = ephemeral_port()
        live = self.root / "live"
        write_fake_release(live, "1.0.0")
        harness = ServerHarness(live, port)
        self.addCleanup(harness.stop)
        status_path = self.root / "status.json"

        harness.start()
        self.assertTrue(harness.wait_healthy())
        staged_v2 = write_fake_release(self.root / "staged-v2", "2.0.0")
        winupdate.switch_with_rollback(
            staged_dir=staged_v2,
            live_dir=live,
            stop_server=harness.stop,
            start_server=lambda: harness.start(),
            health_check=harness.health,
            settle_seconds=0.4,
            attempts=6,
            status_path=status_path,
        )
        status = json.loads(status_path.read_text())
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["installed_version"], "2.0.0")
        self.assertIn("updated_at", status)

        staged_v3 = write_fake_release(self.root / "staged-v3", "3.0.0", healthy=False)
        result = winupdate.switch_with_rollback(
            staged_dir=staged_v3,
            live_dir=live,
            stop_server=harness.stop,
            start_server=lambda: harness.start(),
            health_check=harness.health,
            settle_seconds=0.4,
            attempts=6,
            status_path=status_path,
        )
        self.assertTrue(result.rolled_back)
        status = json.loads(status_path.read_text())
        self.assertEqual(status["phase"], "rolled_back")
        self.assertTrue(status["rolled_back"])
        self.assertEqual(status["installed_version"], "2.0.0")
        self.assertTrue(harness.wait_healthy())


class AutostartTests(TempCase):
    def test_returns_schtasks_argv_with_onlogon_and_supervisor(self):
        argv = winupdate.autostart_command(48999, self.root / "supervisor.log")
        self.assertEqual(argv[0], "schtasks")
        self.assertIn("/Create", argv)
        self.assertIn("/SC", argv)
        self.assertEqual(argv[argv.index("/SC") + 1], "ONLOGON")
        self.assertIn("/RU", argv)
        self.assertTrue(argv[argv.index("/RU") + 1])
        task_command = argv[argv.index("/TR") + 1]
        self.assertIn("agents_server_supervise.py", task_command)
        self.assertIn("48999", task_command)
        self.assertIn(str(self.root.resolve() / "supervisor.log"), task_command)
        self.assertTrue((REPO_ROOT / "scripts" / "agents_server_supervise.py").is_file())
        self.assertTrue(all(isinstance(part, str) for part in argv))

    def test_no_registration_side_effects(self):
        argv = winupdate.autostart_command(48999, self.root / "supervisor.log")
        self.assertEqual(argv[:2], ["schtasks", "/Create"])
        # Nothing was executed and no log/task artifacts were produced.
        self.assertFalse((self.root / "supervisor.log").exists())


class SupervisorTests(TempCase):
    def _load_supervisor(self):
        path = REPO_ROOT / "scripts" / "agents_server_supervise.py"
        spec = importlib.util.spec_from_file_location("agents_server_supervise", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_supervisor_script_compiles(self):
        py_compile.compile(
            str(REPO_ROOT / "scripts" / "agents_server_supervise.py"), doraise=True
        )

    def test_backoff_is_capped_exponential_then_stable(self):
        module = self._load_supervisor()
        now = 1_000.0
        self.assertEqual(module.compute_backoff([], now), 1.0)
        stamps = [now]
        self.assertEqual(module.compute_backoff(stamps, now), 2.0)
        stamps = [now, now - 1.0]
        self.assertEqual(module.compute_backoff(stamps, now), 4.0)
        windowed = [now - i * 10 for i in range(module.BACKOFF_WINDOW_RESTARTS)]
        self.assertEqual(module.compute_backoff(windowed, now), module.BACKOFF_CAP_SECONDS)

    def test_once_mode_runs_single_lifetime_and_loads_server_env(self):
        module_root = self.root / "repo"
        scripts_dir = module_root / "scripts"
        scripts_dir.mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "scripts" / "agents_server_supervise.py",
            scripts_dir / "agents_server_supervise.py",
        )
        (module_root / "agent_server.py").write_text(
            "import os, sys\n"
            "print('marker=' + str(os.environ.get('MY_MARKER')))\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        state_dir = self.root / "state"
        state_dir.mkdir()
        (state_dir / "server.env").write_text("# comment\nMY_MARKER=from-server-env\n\n")
        log_path = self.root / "supervisor.log"
        env = os.environ.copy()
        env["AGENTSDOCK_STATE_DIR"] = str(state_dir)
        env.pop("MY_MARKER", None)
        result = subprocess.run(
            [
                sys.executable,
                str(scripts_dir / "agents_server_supervise.py"),
                "--once",
                "--port",
                str(ephemeral_port()),
                "--log",
                str(log_path),
            ],
            cwd=str(module_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("marker=from-server-env", content)
        self.assertIn("exited with code 7", content)
        # --once never restarts: exactly one lifetime is logged.
        self.assertEqual(content.count("exited with code"), 1)


class CliDriveTests(TempCase):
    """In-process tests for the winupdate drive stage (mocks the handoff)."""

    def _args(self, **overrides):
        options = dict(
            mode="drive",
            status_file=str(self.root / "status.json"),
            public_key=str(self.root / "public.pem"),
            port=ephemeral_port(),
            bind="127.0.0.1",
            live_dir=str(self.root / "live"),
            staging_root=str(self.root / "staging"),
            state_dir=str(self.root / "state"),
            expected_version="",
            current_version="",
            track="stable",
            auth_token_file=None,
            staged_dir=None,
            server_pid=None,
            health_token_file=None,
            exit_timeout=5.0,
        )
        options.update(overrides)
        return argparse.Namespace(**options)

    def _manifest_and_archive(self, version="2.0.0"):
        archive = make_tar_gz(
            f"agents-server-{version}", {"VERSION": f"{version}\n", "app.py": b"# app\n"}
        )
        return make_manifest(version, hashlib.sha256(archive).hexdigest()), archive

    def test_drive_stages_verifies_idle_and_detaches_finish_stage(self):
        write_fake_release(self.root / "live", "1.0.0")
        manifest, archive = self._manifest_and_archive()
        token_file = self.root / ".server-update-abc.auth.json"
        token_file.write_text(json.dumps({"token": "synthetic-token"}))
        spawned: dict = {}

        def fake_spawn(argv, *, cwd, log_path=None):
            spawned["argv"] = [str(part) for part in argv]
            spawned["cwd"] = Path(cwd)
            spawned["log"] = log_path
            return SimpleNamespace(pid=4321)

        with mock.patch.object(winupdate, "check_release", return_value=manifest) as check, \
             mock.patch.object(winupdate, "download_release", return_value=archive) as download, \
             mock.patch.object(winupdate, "assert_server_idle") as idle, \
             mock.patch.object(winupdate, "_spawn_detached", side_effect=fake_spawn), \
             mock.patch("os.getppid", return_value=777):
            rc = winupdate.run_drive(
                self._args(
                    expected_version="2.0.0",
                    current_version="1.0.0",
                    auth_token_file=str(token_file),
                )
            )

        self.assertEqual(rc, 0)
        check.assert_called_once()
        download.assert_called_once()
        idle.assert_called_once()
        self.assertFalse(token_file.exists())  # one-time credential consumed
        staged = list((self.root / "staging").glob("staged-2.0.0-*"))
        self.assertEqual(len(staged), 1)
        argv = spawned["argv"]
        self.assertEqual(Path(argv[0]), winupdate.find_base_interpreter(self.root / "live"))
        self.assertEqual(Path(argv[1]), Path(winupdate.__file__).resolve())
        self.assertEqual(argv[argv.index("--mode") + 1], "finish")
        self.assertEqual(argv[argv.index("--server-pid") + 1], "777")
        self.assertEqual(Path(argv[argv.index("--staged-dir") + 1]).resolve(), staged[0].resolve())
        self.assertEqual(Path(argv[argv.index("--live-dir") + 1]).resolve(), (self.root / "live").resolve())
        self.assertEqual(Path(argv[argv.index("--state-dir") + 1]).resolve(), (self.root / "state").resolve())
        health_token = Path(argv[argv.index("--health-token-file") + 1])
        self.assertEqual(json.loads(health_token.read_text())["token"], "synthetic-token")
        health_token.unlink()
        status = json.loads((self.root / "status.json").read_text())
        self.assertEqual(status["phase"], "awaiting_server_exit")
        self.assertEqual(status["update_pid"], 4321)

    def test_drive_marks_failed_when_release_check_errors(self):
        write_fake_release(self.root / "live", "1.0.0")
        with mock.patch.object(
            winupdate, "check_release", side_effect=RuntimeError("network down")
        ):
            rc = winupdate.run_drive(self._args())
        self.assertEqual(rc, 1)
        status = json.loads((self.root / "status.json").read_text())
        self.assertEqual(status["phase"], "failed")
        self.assertIn("network down", status["message"])
        self.assertFalse((self.root / "staging").exists())

    def test_drive_rejects_non_forward_transition(self):
        write_fake_release(self.root / "live", "2.0.0")
        manifest, archive = self._manifest_and_archive()  # same version as current
        with mock.patch.object(winupdate, "check_release", return_value=manifest), \
             mock.patch.object(winupdate, "download_release", return_value=archive):
            rc = winupdate.run_drive(self._args(current_version="2.0.0"))
        self.assertEqual(rc, 1)
        status = json.loads((self.root / "status.json").read_text())
        self.assertIn("not newer", status["message"])
        self.assertFalse(list((self.root / "staging").glob("staged-*")))


class RecordingHealthServer:
    """Threaded /api/health stub that records the Authorization header."""

    def __init__(self, port: int, ok: bool = True):
        self.port = port
        self.ok = ok
        self.headers_seen: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.headers_seen.append(self.headers.get("Authorization"))
                body = json.dumps({"ok": outer.ok}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CliFinishTests(TempCase):
    """End-to-end finish stage: real subprocess under the base interpreter.

    The finish driver runs as a real process (like production); the fake
    server it spawns watches the driver's pid and exits on its own.
    """

    def setUp(self):
        super().setUp()
        self.live = self.root / "live"
        write_fake_release(self.live, "1.0.0")
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.status_path = self.root / "status.json"
        (self.root / "public.pem").write_text("key")

    def _dead_pid(self) -> int:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        return process.pid

    def _run_finish(self, staged, *, server_pid, port, token_file=None, exit_timeout=60.0):
        base = winupdate.find_base_interpreter(self.live)
        self.assertIsNotNone(base)
        argv = [
            str(base),
            str(Path(winupdate.__file__).resolve()),
            "--mode", "finish",
            "--status-file", str(self.status_path),
            "--public-key", str(self.root / "public.pem"),
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--live-dir", str(self.live),
            "--staging-root", str(self.root / "staging"),
            "--state-dir", str(self.state_dir),
            "--staged-dir", str(staged),
            "--server-pid", str(server_pid),
            "--exit-timeout", str(exit_timeout),
        ]
        if token_file is not None:
            argv.extend(["--health-token-file", str(token_file)])
        return subprocess.run(
            argv,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_finish_switches_starts_new_server_and_completes(self):
        port = ephemeral_port()
        staged = write_fake_release(self.root / "staged-v2", "2.0.0")
        token_file = self.root / "finish.auth.json"
        token_file.write_text(json.dumps({"token": "synthetic"}))
        result = self._run_finish(
            staged, server_pid=self._dead_pid(), port=port, token_file=token_file
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual((self.live / "VERSION").read_text().strip(), "2.0.0")
        self.assertFalse((self.root / "live.old").exists())
        self.assertFalse(token_file.exists())
        self.assertFalse(winupdate.switch_lock_active(self.state_dir))
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["installed_version"], "2.0.0")

    def test_finish_rolls_back_when_the_new_release_is_sick(self):
        port = ephemeral_port()
        staged = write_fake_release(self.root / "staged-v2", "2.0.0", healthy=False)
        result = self._run_finish(staged, server_pid=self._dead_pid(), port=port)
        self.assertEqual(result.returncode, 1, result.stderr[-2000:])
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertFalse((self.root / "live.old").exists())
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "rolled_back")
        self.assertTrue(status["rolled_back"])

    def test_finish_aborts_without_mutation_when_the_server_never_exits(self):
        port = ephemeral_port()
        staged = write_fake_release(self.root / "staged-v2", "2.0.0")
        token_file = self.root / "finish.auth.json"
        token_file.write_text(json.dumps({"token": "synthetic"}))
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(SwitchTests._terminate, sleeper)
        try:
            with RecordingHealthServer(port, ok=True) as health:
                result = self._run_finish(
                    staged,
                    server_pid=sleeper.pid,
                    port=port,
                    token_file=token_file,
                    exit_timeout=2.0,
                )
                self.assertEqual(health.headers_seen.count("Bearer synthetic") >= 1, True)
        finally:
            SwitchTests._terminate(sleeper)
        self.assertEqual(result.returncode, 1, result.stderr[-2000:])
        self.assertEqual((self.live / "VERSION").read_text().strip(), "1.0.0")
        self.assertTrue(staged.is_dir())  # staged tree untouched
        self.assertFalse(winupdate.switch_lock_active(self.state_dir))
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "failed")
        self.assertIn("did not exit", status["message"])


FAKE_AGENT_SERVER = '''
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = 7850
args = sys.argv[1:]
for index, value in enumerate(args):
    if value == "--port" and index + 1 < len(args):
        port = int(args[index + 1])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


class _FakeProc:
    def __init__(self, code: int = 0):
        self._code = code

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return self._code


class _FakeClock:
    """Deterministic monotonic clock + sleep driving the supervisor loop.

    The requested sleep duration advances the fake clock, so deadline-based
    backoff loops elapse after the right number of iterations while real time
    barely passes.
    """

    def __init__(self, on_call=None):
        self.now = time.monotonic()
        self.calls = 0
        self._on_call = on_call
        # Capture the real functions before the test patches module.time
        # (module.time IS this module, so time.sleep would recurse).
        self._real_sleep = time.sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.calls += 1
        self.now += float(seconds)
        if self._on_call is not None:
            self._on_call(self.calls)
        self._real_sleep(0.005)


class SupervisorRecoveryAndDeferralTests(TempCase):
    """Finding 3 (status deferral) and Finding 4 (spawn guard + recovery)."""

    def _load(self):
        path = REPO_ROOT / "scripts" / "agents_server_supervise.py"
        spec = importlib.util.spec_from_file_location("agents_server_supervise", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _write_update_status(self, path: Path, phase: str, age_seconds: float = 0.0) -> None:
        updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        path.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "updated_at": updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        )

    def _write_fake_repo(self, target: Path, version: str) -> None:
        target.mkdir(parents=True)
        (target / "VERSION").write_text(version + "\n", encoding="utf-8")
        (target / "agent_server.py").write_text(FAKE_AGENT_SERVER, encoding="utf-8")

    def _terminate_handles(self, handles) -> None:
        for process in handles:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def test_update_status_active_fresh_boundaries(self):
        module = self._load()
        state_dir = self.root / "state"
        (state_dir / "admin").mkdir(parents=True)
        status_file = state_dir / "admin" / "server-update.json"

        self.assertFalse(module.update_status_active_fresh(state_dir))  # missing
        status_file.write_text("{not json")
        self.assertFalse(module.update_status_active_fresh(state_dir))  # corrupt

        for phase in ("starting", "checking", "awaiting_server_exit", "switching"):
            self._write_update_status(status_file, phase)
            self.assertTrue(module.update_status_active_fresh(state_dir), phase)

        for phase in ("complete", "rolled_back", "failed", "current", "idle"):
            self._write_update_status(status_file, phase)
            self.assertFalse(module.update_status_active_fresh(state_dir), phase)

        self._write_update_status(status_file, "awaiting_server_exit", age_seconds=600)
        self.assertFalse(module.update_status_active_fresh(state_dir))  # stale

    def test_loop_defers_while_update_active_and_resumes_after_terminal(self):
        module = self._load()
        state_dir = self.root / "state"
        (state_dir / "admin").mkdir(parents=True)
        status_file = state_dir / "admin" / "server-update.json"
        self._write_update_status(status_file, "awaiting_server_exit")
        log_path = self.root / "supervisor.log"
        repo = self.root / "repo"
        repo.mkdir()
        port = ephemeral_port()
        spawn_calls: list = []

        def fake_popen(*args, **kwargs):
            spawn_calls.append(1)
            return _FakeProc(0)

        def on_sleep(call: int) -> None:
            if call == 3:
                self._write_update_status(status_file, "complete")
            if call >= 4:
                raise KeyboardInterrupt

        clock = _FakeClock(on_sleep)
        with mock.patch.object(module, "default_state_dir", return_value=state_dir), \
             mock.patch.object(module.os, "chdir"), \
             mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(module.time, "monotonic", side_effect=clock.monotonic), \
             mock.patch.object(module.time, "sleep", side_effect=clock.sleep):
            rc = module.serve(repo, {}, "127.0.0.1", port, log_path, once=False)

        self.assertEqual(rc, 130)
        self.assertEqual(len(spawn_calls), 1)  # no respawn during the active window
        content = log_path.read_text(encoding="utf-8")
        self.assertGreaterEqual(content.count("update in progress; deferring restart"), 2)
        self.assertIn("server exited with code 0", content)

    def test_spawn_failure_backs_off_and_retries_without_crashing(self):
        module = self._load()
        state_dir = self.root / "state"
        state_dir.mkdir()
        log_path = self.root / "supervisor.log"
        repo = self.root / "repo"
        repo.mkdir()
        port = ephemeral_port()
        attempts: list = []

        def flaky_popen(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("live tree mid-move")
            return _FakeProc(0)

        def on_sleep(call: int) -> None:
            if call >= 4:
                raise KeyboardInterrupt

        clock = _FakeClock(on_sleep)
        with mock.patch.object(module, "default_state_dir", return_value=state_dir), \
             mock.patch.object(module.os, "chdir"), \
             mock.patch.object(module.subprocess, "Popen", side_effect=flaky_popen), \
             mock.patch.object(module.time, "monotonic", side_effect=clock.monotonic), \
             mock.patch.object(module.time, "sleep", side_effect=clock.sleep):
            rc = module.serve(repo, {}, "127.0.0.1", port, log_path, once=False)

        self.assertEqual(rc, 130)
        self.assertEqual(len(attempts), 2)  # failed once, retried, stayed alive
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("could not start server", content)
        self.assertIn("retrying in", content)

    def test_once_mode_spawn_failure_returns_failure_code(self):
        module = self._load()
        state_dir = self.root / "state"
        state_dir.mkdir()
        log_path = self.root / "supervisor.log"
        repo = self.root / "repo"
        repo.mkdir()

        with mock.patch.object(module, "default_state_dir", return_value=state_dir), \
             mock.patch.object(module.os, "chdir"), \
             mock.patch.object(
                 module.subprocess,
                 "Popen",
                 side_effect=RuntimeError("tree gone"),
             ):
            rc = module.serve(repo, {}, "127.0.0.1", ephemeral_port(), log_path, once=True)

        self.assertEqual(rc, 1)
        self.assertIn("could not start server", log_path.read_text(encoding="utf-8"))

    def test_recovery_restores_backup_and_starts_old_release(self):
        module = self._load()
        state_dir = self.root / "state"
        state_dir.mkdir()
        repo = self.root / "repo"
        self._write_fake_repo(repo, "2.0.0")
        backup = self.root / "repo.old"
        self._write_fake_repo(backup, "1.0.0")
        port = ephemeral_port()
        log_path = self.root / "recovery.log"
        handles: list = []
        real_popen = subprocess.Popen

        def capturing(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            handles.append(process)
            return process

        with open(log_path, "a", encoding="utf-8") as log, \
             mock.patch.object(module.subprocess, "Popen", side_effect=capturing):
            recovered = module.recover_interrupted_update(
                repo, state_dir, dict(os.environ), "127.0.0.1", port, log
            )
        try:
            self.assertTrue(recovered)
            self.assertEqual((repo / "VERSION").read_text().strip(), "1.0.0")
            self.assertFalse(backup.exists())
            self.assertIn("restored the previous release", log_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 15
            healthy = False
            while time.monotonic() < deadline:
                if module.health_endpoint_ok("127.0.0.1", port, {}):
                    healthy = True
                    break
                time.sleep(0.2)
            self.assertTrue(healthy)
        finally:
            self._terminate_handles(handles)

    def test_recovery_skips_when_backup_missing(self):
        module = self._load()
        state_dir = self.root / "state"
        state_dir.mkdir()
        repo = self.root / "repo"
        self._write_fake_repo(repo, "2.0.0")

        recovered = module.recover_interrupted_update(
            repo, state_dir, {}, "127.0.0.1", ephemeral_port(), io.StringIO()
        )

        self.assertFalse(recovered)
        self.assertEqual((repo / "VERSION").read_text().strip(), "2.0.0")

    def test_recovery_skips_while_an_update_is_active(self):
        module = self._load()
        state_dir = self.root / "state"
        (state_dir / "admin").mkdir(parents=True)
        self._write_update_status(state_dir / "admin" / "server-update.json", "awaiting_server_exit")
        repo = self.root / "repo"
        self._write_fake_repo(repo, "2.0.0")
        backup = self.root / "repo.old"
        self._write_fake_repo(backup, "1.0.0")

        recovered = module.recover_interrupted_update(
            repo, state_dir, {}, "127.0.0.1", ephemeral_port(), io.StringIO()
        )

        self.assertFalse(recovered)
        self.assertEqual((repo / "VERSION").read_text().strip(), "2.0.0")
        self.assertTrue(backup.is_dir())


if __name__ == "__main__":
    unittest.main()