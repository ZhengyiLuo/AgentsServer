"""Preparation receipts validate a real staged filesystem without activation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import execution_preparation as preparation


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="prepared-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "install"
        self.root.mkdir(mode=0o700)
        (self.root / "releases").mkdir(mode=0o700)
        self.candidate = self.root / "releases/.prepared-1.2.3-beta.4-fixture"
        self.candidate.mkdir(mode=0o700)
        self.source = {
            "VERSION": b"1.2.3-beta.4\n", "agent_server.py": b"API_CONTRACT_VERSION = 28\n",
            "execution_service.py": b"# inert fixture\n", "pyproject.toml": b"[project]\n",
            "uv.lock": b"version = 1\n", "agentsdock_team_hub/__init__.py": b"# fixture package\n",
        }
        for name, data in self.source.items():
            path = self.candidate / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        self.inventory = self.base / "source.json"
        self.inventory.write_text(json.dumps({"format": 1, "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in self.source.items()}}))
        self.inventory.chmod(0o600)
        self.receipt = self.root / ".prepared-receipts/fixture.json"
        self.pins = dict(root=self.root, version="1.2.3-beta.4", api_contract=28, archive_sha256="a" * 64)

        # Exercise uv's actual link shapes: external executable interpreter,
        # cache-linked package file, and internal lib64 directory alias.
        (self.candidate / ".venv/bin").mkdir(mode=0o700, parents=True)
        (self.candidate / ".venv/lib/site-packages/fixture").mkdir(mode=0o700, parents=True)
        (self.candidate / ".venv/bin/python").symlink_to(Path(sys.executable).resolve())
        (self.candidate / ".venv/bin/python3").symlink_to("python")
        (self.candidate / ".venv/lib64").symlink_to("lib", target_is_directory=True)
        self.cache_file = self.base / "uv-cache-package.py"
        self.cache_file.write_bytes(b"# cached dependency\n")
        self.cache_file.chmod(0o644)
        self.dependency = self.candidate / ".venv/lib/site-packages/fixture/__init__.py"
        os.link(self.cache_file, self.dependency)
        (self.candidate / "__pycache__").mkdir(mode=0o700)
        (self.candidate / "__pycache__/execution_service.fixture.pyc").write_bytes(b"compiled fixture")

    def write(self):
        return preparation.write_prepared(candidate=self.candidate, inventory=self.inventory,
                                          output=self.receipt, **self.pins)

    def validate(self, **overrides):
        return preparation.validate_prepared(receipt=self.receipt, **{**self.pins, **overrides})

    def rewrite_receipt(self, mutation):
        value = json.loads(self.receipt.read_text())
        mutation(value)
        self.receipt.write_text(json.dumps(value))

    def test_real_uv_links_and_complete_tree_are_read_only_on_validation(self):
        current = self.root / "current"
        current.symlink_to("releases/existing-runtime")
        marker = self.root / "execution-layout.json"
        marker.write_text("existing layout must be untouched")
        before = marker.read_bytes(), marker.stat(), current.lstat(), os.readlink(current)
        written = self.write()
        self.assertEqual(self.validate(), written)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)
        self.assertEqual(self.receipt.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(self.receipt.parent.stat().st_mode), 0o700)
        self.assertEqual((marker.read_bytes(), marker.stat(), current.lstat(), os.readlink(current)), before)
        self.assertEqual(set(written["runtime_inventory"]), {
            *self.source, "agentsdock_team_hub", ".venv", ".venv/bin", ".venv/bin/python",
            ".venv/bin/python3", ".venv/lib", ".venv/lib64", ".venv/lib/site-packages",
            ".venv/lib/site-packages/fixture", ".venv/lib/site-packages/fixture/__init__.py",
            "__pycache__", "__pycache__/execution_service.fixture.pyc"})
        self.assertEqual(self.dependency.stat().st_nlink, 2)
        self.assertEqual(written["runtime_inventory"][".venv/bin/python"]["content"]["kind"], "file")
        self.assertFalse((self.root / ".execution-transaction").exists())
        self.assertFalse((self.root / "state").exists())

    def test_source_modified_during_dependency_install_is_rejected_before_receipt(self):
        (self.candidate / "execution_service.py").write_text("# modified after archive verification\n")
        with self.assertRaisesRegex(RuntimeError, "verified archive"):
            self.write()
        self.assertFalse(self.receipt.parent.exists())

    def test_source_dependency_cache_and_generated_bytecode_changes_are_rejected(self):
        self.write()
        for path in (self.candidate / "execution_service.py", self.cache_file,
                     self.candidate / "__pycache__/execution_service.fixture.pyc"):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b" altered")
                with self.assertRaises(RuntimeError):
                    self.validate()
                path.write_bytes(original)
                self.validate()

    def test_new_or_deleted_members_are_rejected_including_empty_directories(self):
        self.write()
        for relative, directory in (("unexpected.py", False), ("empty-unknown", True),
                                    (".venv/unexpected.py", False), (".venv/empty", True)):
            with self.subTest(relative=relative):
                path = self.candidate / relative
                path.mkdir() if directory else path.write_text("new")
                with self.assertRaises((ValueError, RuntimeError)):
                    self.validate()
                path.rmdir() if directory else path.unlink()
        self.dependency.unlink()
        with self.assertRaisesRegex(RuntimeError, "runtime changed"):
            self.validate()

    def test_exact_content_with_changed_candidate_inode_is_rejected(self):
        self.write()
        old = self.candidate.with_name("old-candidate")
        self.candidate.rename(old)
        shutil.copytree(old, self.candidate, symlinks=True)
        with self.assertRaisesRegex(RuntimeError, "directory identity"):
            self.validate()

    def test_digest_version_and_api_are_exact_pins(self):
        self.write()
        for override in ({"version": "1.2.3-beta.5"}, {"api_contract": 29}, {"archive_sha256": "b" * 64}):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "signed candidate"):
                self.validate(**override)
        self.receipt.unlink()
        self.pins["api_contract"] = 29
        with self.assertRaisesRegex(ValueError, "API differs"):
            self.write()
        self.pins["api_contract"] = 28
        self.pins["version"] = "1.2.3-beta.5"
        with self.assertRaisesRegex(ValueError, "version differs"):
            self.write()

    def test_receipt_is_immutable_and_not_replaced_by_repeated_preparation(self):
        self.write()
        before = self.receipt.read_bytes(), self.receipt.stat().st_ino
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual((self.receipt.read_bytes(), self.receipt.stat().st_ino), before)
        self.assertEqual(list(self.receipt.parent.iterdir()), [self.receipt])

    def test_process_death_between_link_and_unlink_preserves_rejected_evidence(self):
        script = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import execution_preparation as preparation
original = os.link
def publish_then_die(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(97)
os.link = publish_then_die
preparation.write_prepared(candidate=pathlib.Path(sys.argv[2]),root=pathlib.Path(sys.argv[3]),
    inventory=pathlib.Path(sys.argv[4]),output=pathlib.Path(sys.argv[5]),
    version='1.2.3-beta.4',api_contract=28,archive_sha256='a'*64)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(preparation.__file__).parent),
                                 str(self.candidate), str(self.root), str(self.inventory), str(self.receipt)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 97)
        self.assertEqual(self.receipt.stat().st_nlink, 2)
        before = {path.name: (path.read_bytes(), path.stat().st_ino) for path in self.receipt.parent.iterdir()}
        with self.assertRaises(PermissionError):
            self.validate()
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_ino) for path in self.receipt.parent.iterdir()}, before)
        # The runner marks this attempt failed and uses a new preparation ID;
        # it does not overwrite or guess which retained evidence to delete.
        with self.assertRaises(FileExistsError):
            self.write()

    def test_receipt_links_permissions_and_ownership_fail_closed(self):
        self.write()
        for mode in (0o644, 0o620, 0o666):
            with self.subTest(mode=mode):
                self.receipt.chmod(mode)
                with self.assertRaises(PermissionError):
                    self.validate()
        self.receipt.chmod(0o600)
        link = self.receipt.with_name("hardlink.json")
        os.link(self.receipt, link)
        with self.assertRaises(PermissionError):
            self.validate()
        link.unlink()
        self.receipt.rename(link)
        self.receipt.symlink_to(link)
        with self.assertRaises(ValueError):
            self.validate()
        self.receipt.unlink()
        link.rename(self.receipt)
        with mock.patch.object(preparation.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                self.validate()

    def test_unsafe_directory_file_modes_and_special_files_are_rejected(self):
        for path in (self.root, self.candidate, self.dependency):
            with self.subTest(path=path.name):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o777)
                with self.assertRaises(PermissionError):
                    self.write()
                path.chmod(original)
        special = self.candidate / ".venv/fifo"
        os.mkfifo(special, 0o600)
        with self.assertRaises(PermissionError):
            self.write()

    def test_source_links_and_hardlinks_are_not_dependency_exceptions(self):
        path = self.candidate / "execution_service.py"
        content = path.read_bytes()
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                path.unlink()
                target = self.base / "external-source"
                target.write_bytes(content)
                path.symlink_to(target) if kind == "symlink" else os.link(target, path)
                with self.assertRaises(PermissionError):
                    self.write()
                path.unlink()
                path.write_bytes(content)

    def test_external_arbitrary_dependency_link_is_rejected_before_file_read(self):
        external = self.base / "private-do-not-read"
        external.write_text("fixture secret")
        (self.candidate / ".venv/credentials").symlink_to(external)
        with mock.patch.object(preparation, "_file", wraps=preparation._file) as reads:
            with self.assertRaisesRegex(ValueError, "not an interpreter"):
                self.write()
        self.assertNotIn(external, [call.args[0] for call in reads.call_args_list])

    def test_external_dependency_directory_link_is_rejected(self):
        (self.candidate / ".venv/escape").symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory link"):
            self.write()

    def test_uv_world_writable_empty_lock_is_the_only_writable_file_exception(self):
        lock = self.candidate / ".venv/.lock"
        lock.touch(mode=0o666)
        lock.chmod(0o666)
        self.write()
        self.validate()
        lock.write_bytes(b"not an empty advisory lock")
        with self.assertRaises(PermissionError):
            self.validate()
        lock.write_bytes(b"")
        alternate = self.candidate / ".venv/other.lock"
        alternate.touch()
        alternate.chmod(0o666)
        with self.assertRaises(PermissionError):
            self.validate()
        alternate.unlink()
        os.link(lock, alternate)
        with self.assertRaises(PermissionError):
            self.validate()

    def test_changed_external_python_bytes_are_rejected(self):
        interpreter = self.base / "python3"
        interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o755)
        python = self.candidate / ".venv/bin/python"
        python.unlink()
        python.symlink_to(interpreter)
        self.write()
        interpreter.write_bytes(b"#!/bin/sh\nexit 1\n")
        with self.assertRaisesRegex(RuntimeError, "runtime changed"):
            self.validate()

    def test_owned_group_writable_python_is_recorded_without_changing_permissions(self):
        interpreter = self.base / "python3"
        interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o775)
        python = self.candidate / ".venv/bin/python"
        python.unlink()
        python.symlink_to(interpreter)
        before = interpreter.stat()
        receipt = self.write()
        self.assertEqual(self.validate(), receipt)
        self.assertEqual(interpreter.stat(), before)
        self.assertEqual(receipt["runtime_inventory"][".venv/bin/python"]["content"]["mode"], 0o775)
        interpreter.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "runtime changed"):
            self.validate()
        interpreter.chmod(0o777)
        with self.assertRaises(PermissionError):
            self.write()

    def test_receipt_outside_private_install_directory_is_rejected(self):
        for output in (self.base / "outside.json", self.candidate / "receipt.json"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "inside its installation"):
                    preparation.write_prepared(candidate=self.candidate, inventory=self.inventory,
                                               output=output, **self.pins)
                self.assertFalse(output.exists())
        self.receipt.parent.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            self.write()

    def test_inventory_rejects_paths_missing_members_and_duplicate_json_keys(self):
        original = self.inventory.read_text()
        for bad in ("../escape", "/absolute", "a//b", ".venv/escape", "a\\b"):
            value = json.loads(original)
            value["files"][bad] = value["files"]["VERSION"]
            self.inventory.write_text(json.dumps(value))
            with self.subTest(member=bad), self.assertRaises(ValueError):
                self.write()
        value = json.loads(original)
        del value["files"]["uv.lock"]
        self.inventory.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.write()
        self.inventory.write_text('{"format":1,"format":1,"files":{}}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.write()

    def test_darwin_remount_rebases_device_only_with_same_uuid_and_inode(self):
        volume = "12345678-1234-5678-9012-123456789012"
        with mock.patch.object(preparation.sys, "platform", "darwin"), mock.patch.object(preparation, "_volume_uuid", return_value=volume) as uuid:
            self.write()
            self.rewrite_receipt(lambda value: [item.update(device=item["device"] + 100) for item in value["identities"].values()])
            self.validate()
            uuid.return_value = "87654321-4321-8765-2109-210987654321"
            with self.assertRaisesRegex(RuntimeError, "filesystem identity"):
                self.validate()
            uuid.return_value = volume
            self.rewrite_receipt(lambda value: value["identities"]["candidate"].update(inode=1))
            with self.assertRaisesRegex(RuntimeError, "directory identity"):
                self.validate()

    def test_linux_device_change_without_persistent_volume_proof_is_rejected(self):
        with mock.patch.object(preparation.sys, "platform", "linux"):
            self.write()
            self.rewrite_receipt(lambda value: value["identities"]["candidate"].update(device=1))
            with self.assertRaisesRegex(RuntimeError, "filesystem identity"):
                self.validate()

    def test_mutation_during_file_read_is_rejected(self):
        path = self.candidate / "execution_service.py"
        original_read = os.read
        target = path.stat()
        mutated = False

        def mutate_read(fd, count):
            nonlocal mutated
            result = original_read(fd, count)
            info = os.fstat(fd)
            if not mutated and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                mutated = True
                path.write_bytes(b"# raced archive source\n")
            return result

        with mock.patch.object(preparation.os, "read", side_effect=mutate_read):
            with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                self.write()
        self.assertTrue(mutated)
        self.assertFalse(self.receipt.exists())

    def test_receipt_schema_cannot_drop_inventory_or_directory_binding(self):
        self.write()
        original = self.receipt.read_bytes()
        for mutation in (
            lambda value: value.pop("runtime_inventory"),
            lambda value: value.update(candidate=None),
            lambda value: value.update(format=True),
            lambda value: value.update(api_contract=True),
            lambda value: value["identities"].pop("candidate"),
            lambda value: value["source_inventory"]["files"].pop("uv.lock"),
        ):
            self.rewrite_receipt(mutation)
            with self.assertRaises(ValueError):
                self.validate()
            self.receipt.write_bytes(original)

    def test_real_cli_roundtrip_and_error_do_not_echo_input_contents(self):
        script = Path(preparation.__file__)
        args = ["--root", str(self.root), "--version", self.pins["version"],
                "--api-contract", "28", "--archive-sha256", self.pins["archive_sha256"]]
        written = subprocess.run([sys.executable, "-B", str(script), "write", *args,
                                  "--candidate", str(self.candidate), "--inventory", str(self.inventory),
                                  "--output", str(self.receipt)], capture_output=True, text=True, timeout=20)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout.strip(), str(self.receipt))
        validated = subprocess.run([sys.executable, "-B", str(script), "validate", *args,
                                    "--receipt", str(self.receipt)], capture_output=True, text=True, timeout=20)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(validated.stdout.strip(), str(self.candidate))
        self.receipt.write_text('private malformed fixture')
        rejected = subprocess.run([sys.executable, "-B", str(script), "validate", *args,
                                   "--receipt", str(self.receipt)], capture_output=True, text=True, timeout=20)
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(rejected.stdout, "")
        self.assertNotIn("private malformed fixture", rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
