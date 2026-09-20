"""Real-filesystem security tests for the native Windows winfs backend.

Run with:  .venv/Scripts/python.exe -m unittest test_winfs -v

These tests use only ``tempfile.mkdtemp`` directories.  Junctions are created
with ``cmd /c mklink /J`` (no elevation required).  Directory symlink cases are
reported as unavailable when the host lacks the symlink privilege rather than
skipping the rest of the suite.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import winfs

CANARY = b"WINFS-OUTSIDE-CANARY-7f3d9a1c"


def _mkdtemp() -> str:
    return tempfile.mkdtemp(prefix="winfs-test-")


def _mklink_junction(link: str, target: str) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mklink /J failed: {result.stdout} {result.stderr}")


def _try_dir_symlink(target: str, link: str) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _is_reparse(path: str) -> bool:
    return bool(os.stat(path, follow_symlinks=False).st_file_attributes & 0x400) if os.name == "nt" else os.path.islink(path)


@unittest.skipUnless(winfs.SUPPORTED, "winfs native backend unavailable")
class WinFSHelpersTest(unittest.TestCase):
    def test_supported_and_error_is_oserror(self):
        self.assertTrue(winfs.SUPPORTED)
        self.assertTrue(issubclass(winfs.WinFSError, OSError))
        err = winfs.WinFSError(errno.ENOENT, "boom", "x")
        self.assertEqual(err.errno, errno.ENOENT)
        self.assertIsInstance(err, OSError)


@unittest.skipUnless(winfs.SUPPORTED, "winfs native backend unavailable")
class WinFSOperationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _mkdtemp()
        self.root = winfs.open_root(self.tmp)

    def tearDown(self):
        try:
            self.root.close()
        except Exception:
            pass

    # -- group 1: happy path -------------------------------------------------

    def test_create_stat_read_list_roundtrip(self):
        d = self.root.open_dir([])
        st = d.create_file("hello.txt", b"hello world")
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(st.st_size, 11)

        st2 = d.stat("hello.txt")
        self.assertEqual(st2.st_size, 11)
        self.assertTrue(stat.S_ISREG(st2.st_mode))
        self.assertTrue(st2.st_mode & 0o222)
        self.assertEqual(st2.st_dev, self.root.device)

        fobj, fst = d.open_read("hello.txt")
        with fobj:
            self.assertEqual(fobj.read(), b"hello world")
        self.assertEqual(fst.st_size, 11)

        names = {name for name, _ in d.list()}
        self.assertEqual(names, {"hello.txt"})

    def test_mkdir_rename_unlink_rmdir(self):
        d = self.root.open_dir([])
        dstat = d.mkdir("subdir")
        self.assertTrue(stat.S_ISDIR(dstat.st_mode))
        sub = self.root.open_dir(["subdir"])
        sub.create_file("a.txt", b"a")
        sub.close()

        d.rename("subdir", "renamed")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "renamed")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "subdir")))

        d.create_file("f.txt", b"f")
        d.unlink("f.txt")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "f.txt")))

        with self.assertRaises(winfs.WinFSError) as cm:
            d.rmdir("renamed")  # non-empty
        self.assertEqual(cm.exception.errno, errno.ENOTEMPTY)
        d.delete_tree("renamed")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "renamed")))

    def test_open_parent_returns_validated_last_name(self):
        d = self.root.open_dir([])
        d.mkdir("a")
        a = self.root.open_dir(["a"])
        a.create_file("leaf.txt", b"leaf")
        a.close()
        parent, name = self.root.open_parent(["a", "leaf.txt"])
        self.assertEqual(name, "leaf.txt")
        fobj, _ = parent.open_read(name)
        with fobj as f:
            self.assertEqual(f.read(), b"leaf")
        parent.close()

    def test_revision_identity_changes_across_replace(self):
        d = self.root.open_dir([])
        d.create_file("ident.txt", b"aaa")
        s1 = d.stat("ident.txt")
        d.replace_file("ident.txt", b"bbb")
        s2 = d.stat("ident.txt")
        self.assertEqual(s1.st_dev, s2.st_dev)
        self.assertNotEqual(s1.st_ino, s2.st_ino)
        self.assertEqual(s2.st_size, 3)
        self.assertNotEqual((s1.st_ino, s1.st_mtime_ns), (s2.st_ino, s2.st_mtime_ns))

    def test_replace_preserves_readonly_attribute(self):
        d = self.root.open_dir([])
        d.create_file("ro.txt", b"data")
        os.chmod(os.path.join(self.tmp, "ro.txt"), 0o444)
        d.replace_file("ro.txt", b"newdata")
        self.assertFalse(os.stat(os.path.join(self.tmp, "ro.txt")).st_mode & 0o200)

    # -- group 5: no-overwrite semantics -------------------------------------

    def test_rename_no_replace_eexist_and_replace(self):
        d = self.root.open_dir([])
        d.create_file("x.txt", b"x")
        d.create_file("y.txt", b"y")
        with self.assertRaises(winfs.WinFSError) as cm:
            d.rename("x.txt", "y.txt", replace=False)
        self.assertEqual(cm.exception.errno, errno.EEXIST)
        d.rename("x.txt", "y.txt", replace=True)
        self.assertEqual(d.stat("y.txt").st_size, 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.txt")))

    def test_create_file_collision_eexist(self):
        d = self.root.open_dir([])
        d.create_file("c.txt", b"1")
        with self.assertRaises(winfs.WinFSError) as cm:
            d.create_file("c.txt", b"2")
        self.assertEqual(cm.exception.errno, errno.EEXIST)
        self.assertEqual(d.stat("c.txt").st_size, 1)

    # -- group 10: error mapping ----------------------------------------------

    def test_missing_file_enoent(self):
        d = self.root.open_dir([])
        with self.assertRaises(winfs.WinFSError) as cm:
            d.stat("missing.txt")
        self.assertEqual(cm.exception.errno, errno.ENOENT)
        with self.assertRaises(winfs.WinFSError) as cm:
            d.open_read("missing.txt")
        self.assertEqual(cm.exception.errno, errno.ENOENT)

    def test_path_through_file_enotdir(self):
        d = self.root.open_dir([])
        d.create_file("afile", b"data")
        with self.assertRaises(winfs.WinFSError) as cm:
            self.root.open_dir(["afile", "child"])
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)

    def test_open_dir_on_file_enotdir_and_open_read_dir_eisdir(self):
        d = self.root.open_dir([])
        d.mkdir("realdir")
        # open_read on a directory -> EISDIR
        with self.assertRaises(winfs.WinFSError) as cm:
            d.open_read("realdir")
        self.assertEqual(cm.exception.errno, errno.EISDIR)
        # unlink on a directory -> EISDIR
        with self.assertRaises(winfs.WinFSError) as cm:
            d.unlink("realdir")
        self.assertEqual(cm.exception.errno, errno.EISDIR)
        # rmdir on a file -> ENOTDIR
        d.create_file("reg.txt", b"r")
        with self.assertRaises(winfs.WinFSError) as cm:
            d.rmdir("reg.txt")
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)

    def test_open_root_missing_enotdir_and_file(self):
        with self.assertRaises(winfs.WinFSError) as cm:
            winfs.open_root(os.path.join(self.tmp, "does-not-exist"))
        self.assertEqual(cm.exception.errno, errno.ENOENT)
        afile = os.path.join(self.tmp, "afile")
        with open(afile, "w") as f:
            f.write("x")
        with self.assertRaises(winfs.WinFSError) as cm:
            winfs.open_root(afile)
        self.assertEqual(cm.exception.errno, errno.ENOTDIR)

    # -- group 4: name attacks -------------------------------------------------

    def test_name_attacks_rejected_einval(self):
        d = self.root.open_dir([])
        d.mkdir("sub")
        bad = ["", ".", "..", "a:b", "CON", "con.txt", "nul.txt", "prn",
               "name.", "name ", "a\\b", "COM1", "lpt9.log", "aux"]
        for name in bad:
            with self.assertRaises(winfs.WinFSError, msg=f"name {name!r}") as cm:
                d.create_file(name, b"x")
            self.assertEqual(cm.exception.errno, errno.EINVAL, msg=f"name {name!r}")
            with self.assertRaises(winfs.WinFSError, msg=f"name {name!r}") as cm:
                d.stat(name)
            self.assertEqual(cm.exception.errno, errno.EINVAL, msg=f"name {name!r}")
            with self.assertRaises(winfs.WinFSError, msg=f"name {name!r}") as cm:
                self.root.open_dir([name])
            self.assertEqual(cm.exception.errno, errno.EINVAL, msg=f"name {name!r}")
            with self.assertRaises(winfs.WinFSError, msg=f"name {name!r}") as cm:
                self.root.open_parent(["sub", name])
            self.assertEqual(cm.exception.errno, errno.EINVAL, msg=f"name {name!r}")

    def test_absolute_and_unc_parts_rejected(self):
        d = self.root.open_dir([])
        for part in [r"C:\Windows", r"C:foo", r"\\server\share", "/etc/passwd"]:
            with self.assertRaises(winfs.WinFSError, msg=f"part {part!r}"):
                d.create_file(part, b"x")
        # nothing was created as a side effect
        self.assertEqual([n for n, _ in d.list()], [])

    def test_ads_attack_creates_no_stream(self):
        d = self.root.open_dir([])
        with self.assertRaises(winfs.WinFSError):
            d.create_file("a:b", b"x")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a:b")))
        self.assertNotIn("a", [n for n, _ in d.list()])

    # -- group 2: junction / symlink escape ------------------------------------

    def test_junction_traversal_raises_eloop_and_list_flags(self):
        outside = _mkdtemp()
        with open(os.path.join(outside, "secret.txt"), "wb") as f:
            f.write(CANARY)
        link = os.path.join(self.tmp, "link")
        _mklink_junction(link, outside)
        self.assertTrue(_is_reparse(link))

        with self.assertRaises(winfs.WinFSError) as cm:
            self.root.open_dir(["link"])
        self.assertEqual(cm.exception.errno, errno.ELOOP)

        with self.assertRaises(winfs.WinFSError) as cm:
            self.root.open_dir(["link", "secret.txt"])
        self.assertEqual(cm.exception.errno, errno.ELOOP)

        d = self.root.open_dir([])
        with self.assertRaises(winfs.WinFSError) as cm:
            d.stat("link")
        self.assertEqual(cm.exception.errno, errno.ELOOP)
        with self.assertRaises(winfs.WinFSError) as cm:
            d.open_read("link")
        self.assertEqual(cm.exception.errno, errno.ELOOP)

        entries = dict(d.list())
        self.assertIn("link", entries)
        self.assertTrue(stat.S_ISLNK(entries["link"].st_mode))
        # The canary must still be only reachable outside, never through the link.
        with open(os.path.join(outside, "secret.txt"), "rb") as f:
            self.assertEqual(f.read(), CANARY)

    def test_symlink_dir_escape_if_privileged(self):
        outside = _mkdtemp()
        with open(os.path.join(outside, "s.txt"), "wb") as f:
            f.write(CANARY)
        link = os.path.join(self.tmp, "slink")
        if not _try_dir_symlink(outside, link):
            self.__class__._symlink_unavailable = True
            # Report as unavailable without failing the suite.
            sys.stderr.write("\n[winfs-test] directory symlink privilege unavailable; "
                             "symlink case reported unavailable\n")
            return
        with self.assertRaises(winfs.WinFSError) as cm:
            self.root.open_dir(["slink"])
        self.assertEqual(cm.exception.errno, errno.ELOOP)
        d = self.root.open_dir([])
        entries = dict(d.list())
        self.assertTrue(stat.S_ISLNK(entries["slink"].st_mode))

    # -- group 9: root pinning -------------------------------------------------

    def test_root_pinning_survives_path_swap(self):
        base = _mkdtemp()
        real = os.path.join(base, "real")
        os.makedirs(real)
        with open(os.path.join(real, "keep.txt"), "wb") as f:
            f.write(b"ORIGINAL")
        other = os.path.join(base, "other")
        os.makedirs(other)
        with open(os.path.join(other, "keep.txt"), "wb") as f:
            f.write(b"OTHER")

        root = winfs.open_root(real)
        final_before = root.final_path
        # Swap the root's path for a junction to `other`.
        moved = os.path.join(base, "real-moved")
        os.rename(real, moved)
        _mklink_junction(real, other)

        # Reads must resolve through the pinned ORIGINAL directory, never `other`.
        with root.open_dir([]) as d:
            fobj, _ = d.open_read("keep.txt")
            with fobj as f:
                self.assertEqual(f.read(), b"ORIGINAL")
        self.assertEqual(root.final_path, final_before)
        root.close()

    # -- group 8: long paths and unicode ----------------------------------------

    def test_long_paths_and_unicode(self):
        d = self.root.open_dir([])
        comp = "d" * 20
        current = d
        parts = []
        for _ in range(30):  # >600 chars total, exceeds MAX_PATH
            current.mkdir(comp)
            parts.append(comp)
            nxt = self.root.open_dir(parts)
            current.close()
            current = nxt
        current.create_file("héllo wörld.txt", "héllo wörld".encode("utf-8"))
        fobj, _ = current.open_read("héllo wörld.txt")
        with fobj as f:
            self.assertEqual(f.read().decode("utf-8"), "héllo wörld")
        full = os.path.join(self.tmp, *parts, "héllo wörld.txt")
        self.assertGreater(len(full), 260)
        with open("\\\\?\\" + full, "rb") as f:  # proves the file really is there
            self.assertEqual(f.read().decode("utf-8"), "héllo wörld")
        entries = dict(current.list())
        self.assertIn("héllo wörld.txt", entries)
        current.close()

    # -- group 6: atomic replace under concurrency --------------------------------

    def test_replace_without_identity_lands_whole(self):
        d = self.root.open_dir([])
        d.create_file("hot.txt", b"initial")
        contents = [f"c-{i:04d}".encode() for i in range(8)]  # uniform length
        errors = []

        def worker(payload):
            try:
                for _ in range(25):
                    d.replace_file("hot.txt", payload)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(c,)) for c in contents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        fobj, _ = d.open_read("hot.txt")
        with fobj as f:
            final = f.read()
        # Must be one complete version, never torn.
        self.assertIn(final, contents)
        self.assertEqual(len(final), len(contents[0]))

    def test_replace_with_identity_busy_on_mismatch(self):
        d = self.root.open_dir([])
        d.create_file("guard.txt", b"v0")
        s0 = d.stat("guard.txt")
        identity0 = (s0.st_dev, s0.st_ino, s0.st_mtime_ns, s0.st_size)
        # External change invalidates the identity.
        d.replace_file("guard.txt", b"v1-changed")
        with self.assertRaises(winfs.WinFSError) as cm:
            d.replace_file("guard.txt", b"v2", expected_identity=identity0)
        self.assertEqual(cm.exception.errno, errno.EBUSY)
        # Matching identity succeeds.
        s1 = d.stat("guard.txt")
        identity1 = (s1.st_dev, s1.st_ino, s1.st_mtime_ns, s1.st_size)
        d.replace_file("guard.txt", b"v2", expected_identity=identity1)
        fobj, _ = d.open_read("guard.txt")
        with fobj as f:
            self.assertEqual(f.read(), b"v2")

    # -- group 7: recursive delete ------------------------------------------------

    def test_delete_tree_three_levels(self):
        d = self.root.open_dir([])
        d.mkdir("a")
        a = self.root.open_dir(["a"])
        a.mkdir("b")
        a.close()
        b = self.root.open_dir(["a", "b"])
        b.mkdir("c")
        b.close()
        c = self.root.open_dir(["a", "b", "c"])
        for i in range(5):
            c.create_file(f"f{i}.txt", f"data{i}".encode())
        c.close()

        d.delete_tree("a")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a")))
        leftover = []
        for base, dirs, files in os.walk(self.tmp):
            leftover.extend(dirs)
            leftover.extend(files)
        self.assertEqual(leftover, [])

    def test_delete_tree_nested_junction_aborts_intact(self):
        outside = _mkdtemp()
        d = self.root.open_dir([])
        d.mkdir("tree")
        tree = self.root.open_dir(["tree"])
        tree.mkdir("real")
        real = self.root.open_dir(["tree", "real"])
        real.create_file("keep.txt", b"keep")
        real.close()
        tree.close()
        _mklink_junction(os.path.join(self.tmp, "tree", "link"), outside)

        with self.assertRaises(winfs.WinFSError) as cm:
            d.delete_tree("tree")
        self.assertEqual(cm.exception.errno, errno.ELOOP)
        # Everything must still be intact.
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "tree")))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "tree", "real")))
        with open(os.path.join(self.tmp, "tree", "real", "keep.txt"), "rb") as f:
            self.assertEqual(f.read(), b"keep")
        self.assertTrue(_is_reparse(os.path.join(self.tmp, "tree", "link")))

    def test_delete_tree_raced_recreation_not_removed(self):
        d = self.root.open_dir([])
        d.mkdir("victim")
        victim = self.root.open_dir(["victim"])
        victim.create_file("keep.txt", b"original")
        for i in range(200):
            victim.create_file(f"pad{i}.txt", b"pad")
        victim.close()

        a_path = os.path.join(self.tmp, "victim")
        keep_path = os.path.join(a_path, "keep.txt")
        fired = threading.Event()
        stop = threading.Event()

        def racer():
            deadline = time.time() + 10
            while time.time() < deadline and not stop.is_set():
                if not os.path.lexists(a_path):
                    try:
                        os.makedirs(a_path, exist_ok=True)
                        with open(keep_path, "wb") as f:
                            f.write(b"raced")
                        fired.set()
                        return
                    except OSError:
                        continue
                time.sleep(0.0005)

        t = threading.Thread(target=racer)
        t.start()
        d.delete_tree("victim")
        stop.set()
        t.join(timeout=15)

        self.assertTrue(fired.is_set(), "racer never recreated the tree")
        # The raced re-creation under the original name must have survived.
        self.assertTrue(os.path.isfile(keep_path))
        with open(keep_path, "rb") as f:
            self.assertEqual(f.read(), b"raced")

    # -- handle leak capacity -----------------------------------------------------

    def test_handle_open_close_capacity(self):
        d = self.root.open_dir([])
        d.mkdir("dir")
        d.create_file("file.txt", b"data")
        for _ in range(2000):
            sub = self.root.open_dir(["dir"])
            sub.close()
        for _ in range(2000):
            fobj, _ = d.open_read("file.txt")
            with fobj as f:
                f.read()
        # Also exercise traversal opens.
        for _ in range(500):
            parent, name = self.root.open_parent(["dir"])
            parent.close()
        d.delete_tree("dir")

    def test_finalizer_closes_on_gc(self):
        import gc
        d = self.root.open_dir([])
        d.mkdir("gc")
        for _ in range(2000):
            sub = self.root.open_dir(["gc"])
            del sub
        gc.collect()
        d.delete_tree("gc")


@unittest.skipUnless(winfs.SUPPORTED, "winfs native backend unavailable")
class WinFSRaceTest(unittest.TestCase):
    """Group 3: ancestor-replacement race."""

    def test_ancestor_replacement_never_reads_canary(self):
        tmp = _mkdtemp()
        outside = _mkdtemp()
        with open(os.path.join(outside, "secret.txt"), "wb") as f:
            f.write(CANARY)

        a = os.path.join(tmp, "a")
        m = os.path.join(a, "m")
        os.makedirs(m)

        root = winfs.open_root(tmp)
        stop = threading.Event()
        counts = {"eloop": 0, "enoent": 0, "transient": 0, "canary": 0, "ok": 0}
        lock = threading.Lock()

        def attacker():
            # Keep swapping `m` between absent and a junction pointing outside,
            # leaving the junction in place long enough to be observed.
            while not stop.is_set():
                try:
                    if os.path.lexists(m):
                        os.rmdir(m)  # removes an empty dir or the junction itself
                    _mklink_junction(m, outside)
                    time.sleep(0.001)
                except Exception:
                    time.sleep(0.0003)

        def reader():
            deadline = time.time() + 30
            attempts = 0
            while time.time() < deadline and counts["eloop"] < 300 and attempts < 300000:
                attempts += 1
                try:
                    d = root.open_dir(["a", "m"])
                except winfs.WinFSError as exc:
                    with lock:
                        if exc.errno == errno.ELOOP:
                            counts["eloop"] += 1
                        elif exc.errno == errno.ENOENT:
                            counts["enoent"] += 1
                        elif exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY):
                            # Fail-closed transients while the attacker mutates the
                            # tree (sharing violation / delete pending). Security
                            # invariant remains canary == 0.
                            counts["transient"] += 1
                        else:
                            counts[f"other:{exc.errno}"] += 1
                    continue
                try:
                    with lock:
                        counts["ok"] += 1
                    try:
                        fobj, _ = d.open_read("secret.txt")
                    except winfs.WinFSError:
                        continue
                    with fobj:
                        if fobj.read() == CANARY:
                            with lock:
                                counts["canary"] += 1
                finally:
                    d.close()

        at = threading.Thread(target=attacker)
        at.start()
        rt = threading.Thread(target=reader)
        rt.start()
        rt.join(timeout=60)
        stop.set()
        at.join(timeout=30)
        root.close()

        self.assertEqual(counts["canary"], 0,
                         f"outside canary was read! counts={counts}")
        self.assertGreaterEqual(counts["eloop"], 300,
                                f"junction path under-exercised: {counts}")
        unknown = {k: v for k, v in counts.items() if k.startswith("other")}
        self.assertEqual(unknown, {}, f"unexpected errors: {counts}")

@unittest.skipUnless(os.name == "nt", "winfs is Windows-only")
class WinFSAccessSplitTest(unittest.TestCase):
    """Read-only browsing must work under a conflicting directory holder.

    Regression test for profile/project directories held by Explorer, search
    indexers, or AV with restrictive sharing: opening the workspace root must
    not request write-class access (that fails with ERROR_SHARING_VIOLATION),
    and mutations must escalate on demand with an accurate EACCES.
    """

    @staticmethod
    def _conflicting_holder(path: str):
        import ctypes
        k32 = ctypes.windll.kernel32
        FILE_LIST_DIRECTORY = 0x1
        FILE_SHARE_READ = 0x1
        FILE_FLAG_BACKUP_SEMANTICS = 0x2000000
        OPEN_EXISTING = 3
        handle = k32.CreateFileW(
            path, FILE_LIST_DIRECTORY, FILE_SHARE_READ, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle == -1 or handle is None:
            raise OSError(ctypes.get_last_error(), "could not place conflicting holder")
        return k32, handle

    def test_read_only_browse_succeeds_under_conflicting_holder(self):
        tmp = _mkdtemp()
        probe = winfs.open_root(tmp)
        probe.open_dir([]).mkdir("held-sub")
        probe.close()
        k32, holder = self._conflicting_holder(tmp)
        try:
            root = winfs.open_root(tmp)
            try:
                d = root.open_dir([])
                self.assertEqual([n for n, _ in d.list()], ["held-sub"])
                self.assertEqual(root.open_dir(["held-sub"]).list(), [])
                # stat/listing flows also stay read-only
                self.assertTrue(d.stat("held-sub").st_mode & stat.S_IFDIR)
            finally:
                root.close()
        finally:
            k32.CloseHandle(holder)

    def test_mutation_escalates_and_reports_conflict_then_succeeds(self):
        tmp = _mkdtemp()
        k32, holder = self._conflicting_holder(tmp)
        try:
            root = winfs.open_root(tmp)
            try:
                d = root.open_dir([])
                with self.assertRaises(winfs.WinFSError) as raised:
                    d.create_file("probe.txt", b"x")
                self.assertIn(raised.exception.errno, (errno.EACCES, errno.EPERM, errno.EBUSY))
                self.assertEqual(getattr(raised.exception, "win32_error", None), 32)
            finally:
                root.close()
        finally:
            k32.CloseHandle(holder)
        # Once the holder is gone, the same flow escalates and mutates.
        root = winfs.open_root(tmp)
        try:
            d = root.open_dir([])
            st = d.create_file("probe.txt", b"x")
            self.assertEqual(st.st_size, 1)
            d.unlink("probe.txt")
        finally:
            root.close()


class _NullCtx:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
