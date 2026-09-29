"""Archive-only fixtures: no real release, signing key, service or publication."""
import importlib.util
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("legacy_reproducibility", Path(__file__).resolve().parents[1] / "scripts/package_release.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


class LegacyArchiveReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="legacy-reproducibility-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "agents-server-1.2.3"
        self.root.mkdir()
        self.regular = self.root / "module.py"
        self.regular.write_text("# fixture\n")
        self.regular.chmod(0o640)
        self.executable = self.root / "install.sh"
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o750)
        # Exercise PAX long names while removing only volatile metadata.
        (self.root / ("x" * 120)).write_text("long-name fixture\n")

    def test_opt_in_archive_is_stable_across_paths_modes_and_filesystem_times(self):
        epoch = 1700000000
        first, second = self.base / "first.tar.gz", self.base / "other-name.tar.gz"
        PACKAGE.write_archive(self.root, first, epoch)
        for path in (self.root, *self.root.iterdir()):
            os.utime(path, (epoch + 500.25, epoch + 500.75))
        self.regular.chmod(0o600)
        self.executable.chmod(0o700)
        self.root.chmod(0o700)
        PACKAGE.write_archive(self.root, second, epoch)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        header = first.read_bytes()[:10]
        self.assertEqual(header[:2], b"\x1f\x8b")
        self.assertEqual(header[3] & 8, 0, "gzip filename must not depend on output path")
        self.assertEqual(int.from_bytes(header[4:8], "little"), epoch)
        with tarfile.open(first) as archive:
            for member in archive:
                self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""))
                self.assertEqual(member.mtime, epoch)
                self.assertTrue({"atime", "ctime", "mtime"}.isdisjoint(member.pax_headers))
                self.assertEqual(member.mode, 0o755 if member.isdir() or member.name.endswith("install.sh") else 0o644)

    def test_default_legacy_archive_retains_existing_metadata_behavior(self):
        stamp = 1700000000.25
        os.utime(self.regular, (stamp, stamp))
        output = self.base / "legacy-default.tar.gz"
        PACKAGE.write_archive(self.root, output)
        with tarfile.open(output) as archive:
            member = archive.getmember(f"{self.root.name}/module.py")
            self.assertEqual(member.mode, 0o640)
            self.assertEqual(member.mtime, stamp)

    def test_epoch_is_optional_bounded_and_unambiguous(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(PACKAGE.source_date_epoch())
        for value in ("0", "1700000000", str(0xFFFFFFFF)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": value}):
                self.assertEqual(PACKAGE.source_date_epoch(), int(value))
        for value in ("", "-1", "01", "1.5", " 1", "1\n", str(0x100000000)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": value}):
                with self.assertRaisesRegex(ValueError, "32-bit Unix timestamp"):
                    PACKAGE.source_date_epoch()


if __name__ == "__main__":
    unittest.main()
