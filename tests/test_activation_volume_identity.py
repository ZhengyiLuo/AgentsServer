from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import activation_transaction as activation
from tests import test_activation_transaction as support


VOLUME = "12345678-1234-5678-9012-123456789012"
OTHER_VOLUME = "87654321-4321-8765-2109-210987654321"


class ActivationVolumeIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.driver = support.ActivationTransactionTests()
        self.enterContext(mock.patch.object(activation.sys, "platform", "darwin"))
        self.volume = self.enterContext(mock.patch.object(activation, "_volume_uuid", return_value=VOLUME))

    def layout(self, **kwargs):
        return support.ActivationLayout(self.base / "case", **kwargs)

    def renumber_journal(self, item):
        def mutate(value):
            value["volume_bindings"] = {
                str(int(device) + 100): binding
                for device, binding in value["volume_bindings"].items()
            }
            for key in ("candidate_release", "old_release", "previous_release", "desired_env", "desired_service", "guard"):
                if value[key]:
                    value[key]["device"] += 100
            if value["hub"]:
                value["hub"]["fence_device"] += 100
        self.driver.rewrite_manifest(item, mutate)

    def rollback(self, item, transaction):
        self.driver.record(item, transaction, "rolling-back")
        self.driver.invoke("restore-files", *self.driver.owned_args(item, transaction))
        self.driver.record(item, transaction, "rolled-back")
        self.driver.record(item, transaction, "rollback-healthy")
        self.driver.invoke("finish", *self.driver.owned_args(item, transaction))

    def test_reboot_device_change_preserves_exact_release_and_config_recovery(self):
        item = self.layout()
        transaction = self.driver.begin(item)
        self.driver.activate_to_linked(item, transaction)
        self.driver.replace_both_configs(item, transaction)
        self.driver.record(item, transaction, "candidate-starting")
        self.renumber_journal(item)
        self.driver.load(item)
        self.rollback(item, transaction)
        self.assertEqual(item.current.resolve(), item.old_source.resolve())
        self.assertEqual(item.env.read_bytes(), item.original_env)
        self.assertEqual(item.service.read_bytes(), item.original_service)
        self.assertFalse(self.driver.manifest_path(item).exists())

    def test_fresh_install_binds_existing_ancestor_before_config_directories_exist(self):
        item = self.layout(fresh_install=True, previous=False, env_exists=False, service_exists=False)
        item.config_root.rmdir()
        item.service_root.rmdir()
        transaction = self.driver.begin(item)
        self.driver.load(item)
        self.driver.activate_to_linked(item, transaction)
        item.config_root.mkdir()
        item.service_root.mkdir()
        self.driver.replace_both_configs(item, transaction)
        self.renumber_journal(item)
        self.rollback(item, transaction)
        self.assertFalse(item.current.exists())
        self.assertFalse(item.env.exists())
        self.assertFalse(item.service.exists())

    def test_missing_directory_capture_still_rejects_unsafe_existing_ancestors(self):
        ancestor = self.base / "owned"
        ancestor.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(ancestor, target_is_directory=True)
        with self.assertRaisesRegex(PermissionError, "volume anchor is unsafe"):
            activation._capture_volume({"format": 2, "volume_bindings": {}}, alias / "missing", allow_missing=True)
        with mock.patch.object(activation.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(PermissionError, "volume anchor is unsafe"):
                activation._capture_volume({"format": 2, "volume_bindings": {}}, ancestor / "missing", allow_missing=True)
        ancestor.chmod(0o777)
        with self.assertRaisesRegex(PermissionError, "volume anchor is unsafe"):
            activation._capture_volume({"format": 2, "volume_bindings": {}}, ancestor / "missing", allow_missing=True)

    def test_interrupted_config_publication_recovers_after_device_change(self):
        item = self.layout()
        transaction = self.driver.begin(item)
        self.driver.activate_to_linked(item, transaction)
        with mock.patch.object(activation, "_publish_staged_config", side_effect=support.SimulatedCrash):
            with self.assertRaises(support.SimulatedCrash):
                self.driver.replace_config(item, transaction, kind="env", value=b"TOKEN=candidate\n", mode=0o600)
        self.renumber_journal(item)
        self.rollback(item, transaction)
        self.assertEqual(item.env.read_bytes(), item.original_env)
        self.assertEqual(list(item.config_root.iterdir()), [item.env])

    def test_changed_volume_is_rejected_even_when_device_number_is_reused(self):
        item = self.layout()
        self.driver.begin(item)
        self.volume.return_value = OTHER_VOLUME
        with self.assertRaisesRegex(RuntimeError, "filesystem identity changed"):
            self.driver.load(item)

    def test_format_two_darwin_journal_cannot_drop_volume_bindings(self):
        item = self.layout()
        self.driver.begin(item)
        self.driver.rewrite_manifest(item, lambda value: value.update(volume_bindings={}))
        with self.assertRaisesRegex(RuntimeError, "volume bindings are missing"):
            self.driver.load(item)

    def test_inode_and_version_substitution_remain_rejected(self):
        item = self.layout()
        self.driver.begin(item)
        self.renumber_journal(item)
        self.driver.rewrite_manifest(item, lambda value: value["old_release"].update(inode=value["old_release"]["inode"] + 1))
        with self.assertRaisesRegex(RuntimeError, "rollback release identity changed"):
            self.driver.load(item)
        self.driver.rewrite_manifest(item, lambda value: value["old_release"].update(inode=item.old_source.stat().st_ino))
        (item.old_source / "VERSION").write_text("changed\n")
        with self.assertRaisesRegex(RuntimeError, "rollback release identity changed"):
            self.driver.load(item)

    def test_volume_anchor_symlink_is_rejected(self):
        item = self.layout()
        self.driver.begin(item)
        alias = self.base / "alias"
        alias.symlink_to(item.root, target_is_directory=True)
        self.driver.rewrite_manifest(item, lambda value: next(iter(value["volume_bindings"].values())).update(anchor=str(alias)))
        with self.assertRaisesRegex(PermissionError, "volume anchor is unsafe"):
            self.driver.load(item)

    def test_legacy_journal_remains_readable_without_remount(self):
        item = self.layout()
        self.driver.begin(item)
        def legacy(value):
            value["format"] = 1
            value.pop("volume_bindings")
        self.driver.rewrite_manifest(item, legacy)
        self.driver.load(item)
        self.driver.rewrite_manifest(item, lambda value: value["old_release"].update(device=value["old_release"]["device"] + 1))
        with self.assertRaisesRegex(RuntimeError, "legacy journal has no persistent volume proof"):
            self.driver.load(item)

    def test_unmigrated_host_reactivation_journal_fails_closed_after_remount(self):
        item = self.layout(intent="host-reactivation")
        self.driver.begin(item)
        self.renumber_journal(item)
        with self.assertRaisesRegex(RuntimeError, "separate recovery journal requires support-assisted"):
            self.driver.load(item)

    def test_two_volume_device_swap_maps_each_coordinate_once(self):
        first = self.base / "first"
        second = self.base / "second"
        value = {
            "format": 2, "intent": "server-update",
            "volume_bindings": {
                "11": {"anchor": str(first), "uuid": VOLUME},
                "22": {"anchor": str(second), "uuid": OTHER_VOLUME},
            },
            "candidate_release": {"device": 11}, "old_release": {"device": 22},
            "previous_release": {}, "desired_env": {"device": 22},
            "desired_service": {"device": 11}, "guard": {"device": 22},
            "hub": {"fence_device": 11},
        }
        with mock.patch.object(activation, "_volume_anchor", side_effect=lambda path: SimpleNamespace(st_dev=22 if path == first else 11)):
            self.volume.side_effect = lambda path, identity: VOLUME if path == first else OTHER_VOLUME
            activation._rebase_volume_bindings(value)
        self.assertEqual([value[key]["device"] for key in ["candidate_release", "old_release", "desired_env", "desired_service", "guard"]], [22, 11, 11, 22, 11])
        self.assertEqual(value["hub"]["fence_device"], 22)
        self.assertEqual(value["volume_bindings"]["22"]["uuid"], VOLUME)
        self.assertEqual(value["volume_bindings"]["11"]["uuid"], OTHER_VOLUME)


if __name__ == "__main__":
    unittest.main()
