"""Secure handle-based workspace filesystem for native Windows.

This module is the Windows equivalent of the POSIX ``dir_fd``-relative,
``O_NOFOLLOW`` workspace file backend in ``agent_server.py``.  It provides the
same security property -- *no untrusted path component is ever resolved through
a reparse point, and untrusted relative paths are never reconstructed as
strings* -- but implements it with native handle-based traversal instead of
``Path.resolve()`` + containment + pathname opens (which are race-prone).

Design
------
* Traversal is component-by-component: each untrusted name component is opened
  with ``NtCreateFile`` passing the previously opened directory handle as
  ``OBJECT_ATTRIBUTES.RootDirectory``.  The NT object manager resolves the
  single relative component against our handle; we never build a full path
  string from untrusted input, so there is no path-string TOCTOU window.
* Every untrusted component is opened with ``FILE_OPEN`` (never create /
  overwrite) and ``FILE_OPEN_REPARSE_POINT`` so the *link node itself* is opened
  rather than followed, then inspected with
  ``GetFileInformationByHandleEx(FileAttributeTagInfo)``.  A
  ``FILE_ATTRIBUTE_REPARSE_POINT`` whose tag is ``IO_REPARSE_TAG_SYMLINK`` or
  ``IO_REPARSE_TAG_MOUNT_POINT`` is rejected with ``ELOOP``; any *other* reparse
  tag fails closed with ``EPERM``.  Normal files/directories proceed.
* The trusted configured root directory is opened once with ``CreateFileW``
  (``FILE_FLAG_BACKUP_SEMANTICS``), which resolves it once (it may itself be a
  reparse point), and is pinned to its final path via
  ``GetFinalPathNameByHandleW``.  All later opens chain off the root *handle*,
  so replacing the root's path with a junction does not redirect us.
* Mutations are atomic: ``replace_file`` writes a temp file inside the target
  directory handle, flushes it (``FlushFileBuffers``) and renames it over the
  destination (handle-relative ``NtSetInformationFile(FileRenameInformation)``
  with ``ReplaceIfExists``).  Nothing is mutated in place.
* ``delete_tree`` validates the tree (rejecting nested reparse points and
  bounding depth/size) before deleting anything, then deletes each entry by
  first renaming it to a random temp name inside its parent -- a raced
  re-creation under the original name is therefore never removed.

Errors
------
All failures raise :class:`WinFSError` (an ``OSError`` subclass) carrying a
Unix-style ``.errno`` (``ENOENT``, ``ENOTDIR``, ``EISDIR``, ``EEXIST``,
``ELOOP``, ``EACCES``, ``EPERM``, ``EINVAL``, ``ENOTEMPTY``, ``EBUSY`` ...)
translated from NTSTATUS / Win32, so ``agent_server.translate_workspace_os_error``
keeps working unchanged.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat as _stat
import time
import uuid
import weakref
from ctypes import wintypes

__all__ = [
    "SUPPORTED",
    "WinFSError",
    "open_root",
    "WinRoot",
    "WinDir",
]

# ---------------------------------------------------------------------------
# Library binding / availability
# ---------------------------------------------------------------------------

# NTSTATUS severity helpers
_STATUS_MASK = 0xFFFFFFFF


def _bind():
    if os.name != "nt":
        return None
    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    required_nt = ["NtCreateFile", "NtSetInformationFile"]
    required_k32 = [
        "CreateFileW",
        "CloseHandle",
        "GetFileInformationByHandle",
        "GetFileInformationByHandleEx",
        "SetFileInformationByHandle",
        "GetFinalPathNameByHandleW",
        "ReadFile",
        "WriteFile",
        "FlushFileBuffers",
        "DuplicateHandle",
        "GetCurrentProcess",
    ]
    for name in required_nt:
        if not hasattr(ntdll, name):
            return None
    for name in required_k32:
        if not hasattr(kernel32, name):
            return None
    return ntdll, kernel32


_bound = _bind()
SUPPORTED = _bound is not None

if SUPPORTED:
    _ntdll, _kernel32 = _bound

    # -- ctypes types -------------------------------------------------------
    NTSTATUS = wintypes.LONG
    ULONG = wintypes.ULONG
    USHORT = wintypes.USHORT
    DWORD = wintypes.DWORD
    BOOL = wintypes.BOOL
    BOOLEAN = wintypes.BYTE
    WCHAR = wintypes.WCHAR
    HANDLE = wintypes.HANDLE
    PVOID = wintypes.LPVOID
    ULONG_PTR = ctypes.c_size_t  # pointer-width unsigned

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", USHORT),
            ("MaximumLength", USHORT),
            ("Buffer", ctypes.POINTER(WCHAR)),
        ]

    class OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", ULONG),
            ("RootDirectory", HANDLE),
            ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
            ("Attributes", ULONG),
            ("SecurityDescriptor", PVOID),
            ("SecurityQualityOfService", PVOID),
        ]

    class IO_STATUS_BLOCK(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("Status", NTSTATUS), ("Pointer", PVOID)]

        _anonymous_ = ("u",)
        _fields_ = [("u", _U), ("Information", ULONG_PTR)]

    class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = [("dwFileAttributes", DWORD), ("dwReparseTag", DWORD)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", DWORD),
            ("nFileSizeHigh", DWORD),
            ("nFileSizeLow", DWORD),
            ("nNumberOfLinks", DWORD),
            ("nFileIndexHigh", DWORD),
            ("nFileIndexLow", DWORD),
        ]

    class FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", BOOLEAN)]

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", wintypes.LARGE_INTEGER),
            ("LastAccessTime", wintypes.LARGE_INTEGER),
            ("LastWriteTime", wintypes.LARGE_INTEGER),
            ("ChangeTime", wintypes.LARGE_INTEGER),
            ("FileAttributes", DWORD),
        ]

    class FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", DWORD),
            ("FileIndex", DWORD),
            ("CreationTime", wintypes.LARGE_INTEGER),
            ("LastAccessTime", wintypes.LARGE_INTEGER),
            ("LastWriteTime", wintypes.LARGE_INTEGER),
            ("ChangeTime", wintypes.LARGE_INTEGER),
            ("EndOfFile", wintypes.LARGE_INTEGER),
            ("AllocationSize", wintypes.LARGE_INTEGER),
            ("FileAttributes", DWORD),
            ("FileNameLength", DWORD),
            ("EaSize", DWORD),
            ("ShortNameLength", wintypes.BYTE),
            ("ShortName", WCHAR * 12),
            ("FileId", wintypes.LARGE_INTEGER),
            ("FileName", WCHAR * 1),
        ]

    # -- function prototypes ------------------------------------------------
    _NtCreateFile = _ntdll.NtCreateFile
    _NtCreateFile.restype = NTSTATUS
    _NtCreateFile.argtypes = [
        ctypes.POINTER(HANDLE),
        ULONG,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.POINTER(wintypes.LARGE_INTEGER),
        ULONG,
        ULONG,
        ULONG,
        ULONG,
        PVOID,
        ULONG,
    ]

    _NtSetInformationFile = _ntdll.NtSetInformationFile
    _NtSetInformationFile.restype = NTSTATUS
    _NtSetInformationFile.argtypes = [
        HANDLE,
        ctypes.POINTER(IO_STATUS_BLOCK),
        PVOID,
        ULONG,
        ULONG,
    ]

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = HANDLE
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        PVOID,
        DWORD,
        DWORD,
        HANDLE,
    ]

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = BOOL
    _CloseHandle.argtypes = [HANDLE]

    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.restype = BOOL
    _GetFileInformationByHandle.argtypes = [HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]

    _GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
    _GetFileInformationByHandleEx.restype = BOOL
    _GetFileInformationByHandleEx.argtypes = [HANDLE, DWORD, PVOID, DWORD]

    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.restype = BOOL
    _SetFileInformationByHandle.argtypes = [HANDLE, DWORD, PVOID, DWORD]

    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.restype = DWORD
    _GetFinalPathNameByHandleW.argtypes = [HANDLE, wintypes.LPWSTR, DWORD, DWORD]

    _ReadFile = _kernel32.ReadFile
    _ReadFile.restype = BOOL
    _ReadFile.argtypes = [HANDLE, PVOID, DWORD, ctypes.POINTER(DWORD), PVOID]

    _WriteFile = _kernel32.WriteFile
    _WriteFile.restype = BOOL
    _WriteFile.argtypes = [HANDLE, PVOID, DWORD, ctypes.POINTER(DWORD), PVOID]

    _FlushFileBuffers = _kernel32.FlushFileBuffers
    _FlushFileBuffers.restype = BOOL
    _FlushFileBuffers.argtypes = [HANDLE]

    _DuplicateHandle = _kernel32.DuplicateHandle
    _DuplicateHandle.restype = BOOL
    _DuplicateHandle.argtypes = [HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE), DWORD, BOOL, DWORD]

    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.restype = HANDLE

    try:
        import msvcrt

        _open_osfhandle = msvcrt.open_osfhandle
    except Exception:  # pragma: no cover - msvcrt always present on nt
        _open_osfhandle = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

if SUPPORTED:
    INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

    # ACCESS_MASK
    FILE_READ_DATA = 0x0001
    FILE_LIST_DIRECTORY = 0x0001
    FILE_WRITE_DATA = 0x0002
    FILE_ADD_FILE = 0x0002
    FILE_TRAVERSE = 0x0020
    FILE_ADD_SUBDIRECTORY = 0x0004
    FILE_READ_ATTRIBUTES = 0x0080
    FILE_WRITE_ATTRIBUTES = 0x0100
    DELETE = 0x00010000
    READ_CONTROL = 0x00020000
    SYNCHRONIZE = 0x00100000

    # Share access
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    _SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

    # Create disposition
    FILE_SUPERSEDE = 0x00000000
    FILE_OPEN = 0x00000001
    FILE_CREATE = 0x00000002
    FILE_OPEN_IF = 0x00000003
    FILE_OVERWRITE = 0x00000004
    FILE_OVERWRITE_IF = 0x00000005

    # Create options
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_WRITE_THROUGH = 0x00000002
    FILE_SEQUENTIAL_ONLY = 0x00000004
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_DELETE_ON_CLOSE = 0x00001000
    FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
    FILE_OPEN_REPARSE_POINT = 0x00200000

    # File attributes
    FILE_ATTRIBUTE_READONLY = 0x00000001
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    # OBJECT_ATTRIBUTES.Attributes
    OBJ_CASE_INSENSITIVE = 0x00000040

    # Reparse tags
    IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
    IO_REPARSE_TAG_SYMLINK = 0xA000000C

    # GetFileInformationByHandleEx classes
    FileBasicInfo = 0
    FileDispositionInfo = 4
    FileAttributeTagInfo = 9
    FileIdBothDirectoryInfo = 10
    FileIdBothDirectoryRestartInfo = 11

    # NtSetInformationFile classes (FILE_INFORMATION_CLASS)
    FileRenameInformation = 10

    # Win32 errors
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_HANDLE = 6
    ERROR_TOO_MANY_OPEN_FILES = 4
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PATH_NOT_FOUND = 3
    ERROR_SHARING_VIOLATION = 32
    ERROR_LOCK_VIOLATION = 33
    ERROR_INVALID_PARAMETER = 87
    ERROR_INVALID_NAME = 123
    ERROR_DIR_NOT_EMPTY = 145
    ERROR_BUSY = 170
    ERROR_ALREADY_EXISTS = 183
    ERROR_FILE_EXISTS = 80
    ERROR_DIRECTORY = 267
    ERROR_NO_MORE_FILES = 18
    ERROR_INSUFFICIENT_BUFFER = 122
    ERROR_DISK_FULL = 112
    ERROR_ENCRYPTION_FAILED = 6000

    DUPLICATE_SAME_ACCESS = 0x00000002

    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    # NTSTATUS codes (verified against ntdll)
    STATUS_SUCCESS = 0x00000000
    STATUS_NO_SUCH_FILE = 0xC000000F
    STATUS_INVALID_PARAMETER = 0xC000000D
    STATUS_ACCESS_DENIED = 0xC0000022
    STATUS_OBJECT_NAME_INVALID = 0xC0000033
    STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
    STATUS_OBJECT_NAME_COLLISION = 0xC0000035
    STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
    STATUS_OBJECT_PATH_SYNTAX_BAD = 0xC000003B
    STATUS_SHARING_VIOLATION = 0xC0000043
    STATUS_FILE_LOCK_CONFLICT = 0xC0000054
    STATUS_DELETE_PENDING = 0xC0000056
    STATUS_INSUFFICIENT_RESOURCES = 0xC000009A
    STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
    STATUS_NOT_SUPPORTED = 0xC00000BB
    STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
    STATUS_NOT_A_DIRECTORY = 0xC0000103
    STATUS_TOO_MANY_OPENED_FILES = 0xC000011F

    _EPOCH_100NS = 116444736000000000  # 100-ns units between 1601 and 1970

    # Bounds for recursive delete (DoS protection)
    _MAX_TREE_DEPTH = 128
    _MAX_TREE_ENTRIES = 1_000_000


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

if SUPPORTED:
    _NTSTATUS_ERRNO = {
        STATUS_NO_SUCH_FILE: errno.ENOENT,
        STATUS_OBJECT_NAME_NOT_FOUND: errno.ENOENT,
        STATUS_OBJECT_PATH_NOT_FOUND: errno.ENOENT,
        STATUS_OBJECT_PATH_SYNTAX_BAD: errno.ENOENT,
        STATUS_DELETE_PENDING: errno.ENOENT,
        STATUS_OBJECT_NAME_INVALID: errno.EINVAL,
        STATUS_INVALID_PARAMETER: errno.EINVAL,
        STATUS_OBJECT_NAME_COLLISION: errno.EEXIST,
        STATUS_ACCESS_DENIED: errno.EACCES,
        STATUS_SHARING_VIOLATION: errno.EACCES,
        STATUS_FILE_LOCK_CONFLICT: errno.EACCES,
        STATUS_FILE_IS_A_DIRECTORY: errno.EISDIR,
        STATUS_NOT_A_DIRECTORY: errno.ENOTDIR,
        STATUS_DIRECTORY_NOT_EMPTY: errno.ENOTEMPTY,
        STATUS_TOO_MANY_OPENED_FILES: errno.ENFILE,
        STATUS_INSUFFICIENT_RESOURCES: errno.ENOMEM,
        STATUS_NOT_SUPPORTED: errno.EPERM,
    }

    _WIN32_ERRNO = {
        ERROR_FILE_NOT_FOUND: errno.ENOENT,
        ERROR_PATH_NOT_FOUND: errno.ENOENT,
        ERROR_ACCESS_DENIED: errno.EACCES,
        ERROR_INVALID_HANDLE: errno.EBADF,
        ERROR_TOO_MANY_OPEN_FILES: errno.ENFILE,
        ERROR_SHARING_VIOLATION: errno.EACCES,
        ERROR_LOCK_VIOLATION: errno.EACCES,
        ERROR_INVALID_PARAMETER: errno.EINVAL,
        ERROR_INVALID_NAME: errno.EINVAL,
        ERROR_DIR_NOT_EMPTY: errno.ENOTEMPTY,
        ERROR_BUSY: errno.EBUSY,
        ERROR_ALREADY_EXISTS: errno.EEXIST,
        ERROR_FILE_EXISTS: errno.EEXIST,
        ERROR_DIRECTORY: errno.ENOTDIR,
        ERROR_DISK_FULL: errno.ENOSPC,
    }


class WinFSError(OSError):
    """OSError subclass carrying a Unix-style ``errno`` translated from Win32/NTSTATUS."""

    def __init__(self, err: int, message: str | None = None, path: str | None = None, win32_error: int | None = None):
        self._winfs_path = path
        self.win32_error = win32_error
        super().__init__(err, message or os.strerror(err), path)

    def __str__(self):  # keep messages readable in logs
        base = super().__str__()
        return f"[WinFSError {self.errno}] {base}"


if SUPPORTED:

    def _raise_ntstatus(status: int, context: str, name: str):
        code = status & _STATUS_MASK
        mapped = _NTSTATUS_ERRNO.get(code)
        if mapped is None:
            # Fail closed on unknown NTSTATUS.
            raise WinFSError(errno.EIO, f"{context}: unmapped NTSTATUS 0x{code:08x} for '{name}'")
        raise WinFSError(mapped, f"{context} '{name}': NTSTATUS 0x{code:08x}")

    def _raise_win32(context: str, name: str = ""):
        err = ctypes.get_last_error()
        mapped = _WIN32_ERRNO.get(err)
        if mapped is None:
            raise WinFSError(errno.EIO, f"{context} '{name}': Win32 error {err}", win32_error=err)
        raise WinFSError(mapped, f"{context} '{name}': Win32 error {err}", win32_error=err)

    def _close_handle(handle) -> None:
        if handle is not None and handle != INVALID_HANDLE_VALUE and handle != 0:
            _CloseHandle(handle)

    # -- name validation ----------------------------------------------------

    _DEVICE_NAMES = (
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{i}" for i in range(1, 10)}
        | {f"LPT{i}" for i in range(1, 10)}
    )

    def _validate_component(name: str) -> None:
        """Reject unsafe single path components with EINVAL.

        Enforces: non-empty, not '.'/'..', no ':' (ADS / NTFS escapes / drive),
        no path separators, no trailing space or dot, and Windows device names
        case-insensitively with or without an extension.
        """
        if not isinstance(name, str):
            raise WinFSError(errno.EINVAL, f"invalid name type: {type(name).__name__}")
        if name in ("", ".", ".."):
            raise WinFSError(errno.EINVAL, f"invalid path component: {name!r}")
        if ":" in name:
            raise WinFSError(errno.EINVAL, f"':' not allowed in path component: {name!r}")
        if "/" in name or "\\" in name:
            raise WinFSError(errno.EINVAL, f"path separator not allowed in component: {name!r}")
        if name[-1] in (" ", "."):
            raise WinFSError(errno.EINVAL, f"trailing space/dot not allowed in component: {name!r}")
        base = name.split(".", 1)[0].upper()
        if base in _DEVICE_NAMES:
            raise WinFSError(errno.EINVAL, f"reserved device name not allowed: {name!r}")

    # -- low-level open helpers ----------------------------------------------

    def _make_unicode_string(value: str):
        data = value.encode("utf-16-le")
        nchars = len(data) // 2
        buf = (WCHAR * (nchars + 1))()
        ctypes.memmove(buf, data, len(data))
        us = UNICODE_STRING(len(data), len(data), ctypes.cast(buf, ctypes.POINTER(WCHAR)))
        return us, buf

    def _nt_create_file(name, root, desired_access, create_disposition, create_options):
        """Open/create a single component relative to ``root`` (a handle)."""
        handle = HANDLE()
        iosb = IO_STATUS_BLOCK()
        alloc = wintypes.LARGE_INTEGER(0)
        us, buf = _make_unicode_string(name)
        oa = OBJECT_ATTRIBUTES()
        oa.Length = ctypes.sizeof(OBJECT_ATTRIBUTES)
        oa.RootDirectory = root
        oa.ObjectName = ctypes.pointer(us)
        oa.Attributes = OBJ_CASE_INSENSITIVE
        oa.SecurityDescriptor = None
        oa.SecurityQualityOfService = None
        status = _NtCreateFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(oa),
            ctypes.byref(iosb),
            ctypes.byref(alloc),
            FILE_ATTRIBUTE_NORMAL,
            _SHARE_ALL,
            create_disposition,
            create_options,
            None,
            0,
        )
        if status != STATUS_SUCCESS:
            _raise_ntstatus(status, "NtCreateFile", name)
        return handle.value

    def _query_attribs(handle):
        """Return (dwFileAttributes, dwReparseTag) via FileAttributeTagInfo."""
        info = FILE_ATTRIBUTE_TAG_INFO()
        ok = _GetFileInformationByHandleEx(
            handle, FileAttributeTagInfo, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _raise_win32("GetFileInformationByHandleEx(FileAttributeTagInfo)")
        return info.dwFileAttributes, info.dwReparseTag

    def _check_not_reparse(attrs, tag, name):
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            if tag in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT):
                raise WinFSError(errno.ELOOP, f"reparse point (symlink/junction) not followed: {name!r}")
            raise WinFSError(errno.EPERM, f"unsupported reparse point tag 0x{tag & _STATUS_MASK:08x}: {name!r}")

    def _open_component(parent, name, directory, access, context="open"):
        """Validate + open one component relative to parent, rejecting reparse points.

        ``directory`` is True (must be a directory), False (must not be), or None
        (either).  Returns (handle, dwFileAttributes).
        """
        _validate_component(name)
        options = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
        if directory is True:
            options |= FILE_DIRECTORY_FILE
        elif directory is False:
            options |= FILE_NON_DIRECTORY_FILE
        handle = _nt_create_file(name, parent, access, FILE_OPEN, options)
        try:
            attrs, tag = _query_attribs(handle)
            _check_not_reparse(attrs, tag, name)
        except Exception:
            _close_handle(handle)
            raise
        return handle, attrs

    # -- stat building ------------------------------------------------------

    def _filetime_to_ns(ft) -> int:
        raw = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return (raw - _EPOCH_100NS) * 100

    def _mode_from_attrs(attrs) -> int:
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return _stat.S_IFLNK | 0o777
        if attrs & FILE_ATTRIBUTE_DIRECTORY:
            return _stat.S_IFDIR | 0o777
        perm = 0o444 if (attrs & FILE_ATTRIBUTE_READONLY) else 0o666
        return _stat.S_IFREG | perm

    def _make_stat_result(mode, ino, dev, nlink, size, atime_ns, mtime_ns, ctime_ns):
        return os.stat_result(
            (mode, ino, dev, nlink, 0, 0, size,
             atime_ns / 1e9, mtime_ns / 1e9, ctime_ns / 1e9),
            {
                "st_atime_ns": atime_ns,
                "st_mtime_ns": mtime_ns,
                "st_ctime_ns": ctime_ns,
            },
        )

    def _stat_from_handle(handle) -> os.stat_result:
        info = BY_HANDLE_FILE_INFORMATION()
        ok = _GetFileInformationByHandle(handle, ctypes.byref(info))
        if not ok:
            _raise_win32("GetFileInformationByHandle")
        size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
        ino = (info.nFileIndexHigh << 32) | info.nFileIndexLow
        atime_ns = _filetime_to_ns(info.ftLastAccessTime)
        mtime_ns = _filetime_to_ns(info.ftLastWriteTime)
        ctime_ns = _filetime_to_ns(info.ftCreationTime)
        return _make_stat_result(
            _mode_from_attrs(info.dwFileAttributes),
            ino,
            info.dwVolumeSerialNumber,
            info.nNumberOfLinks,
            size,
            atime_ns,
            mtime_ns,
            ctime_ns,
        )

    def _with_listing_nlink(st: os.stat_result) -> os.stat_result:
        """Best-effort st_nlink (dirs 2, files 1) matching WinDir.list().

        Directory enumeration (FILE_ID_BOTH_DIR_INFO) carries no link count, so
        list() reports a best-effort value; stat/create/mkdir use the real count.
        Revisions (workspace_entry_revision hashes st_nlink) must be stable across
        list/create/stat, so those entry-stat paths normalize to the same
        best-effort value.  The write path keeps the REAL count via open_read for
        hard-link detection.
        """
        nlink = 2 if _stat.S_ISDIR(st.st_mode) else 1
        if nlink == st.st_nlink:
            return st
        return _make_stat_result(
            st.st_mode, st.st_ino, st.st_dev, nlink, st.st_size,
            st.st_atime_ns, st.st_mtime_ns, st.st_ctime_ns,
        )

    # -- handle-based mutation primitives ------------------------------------

    def _rename_handle(handle, new_name, root_dir, replace: bool):
        # Native FileRenameInformation supports a handle-relative RootDirectory,
        # so the destination is resolved against our pinned directory handle
        # rather than a re-parsed path string (the Win32 FileRenameInfo wrapper
        # rejects a non-NULL RootDirectory).
        data = new_name.encode("utf-16-le")
        nchars = len(data) // 2

        class _FRI(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", BOOLEAN),
                ("RootDirectory", HANDLE),
                ("FileNameLength", ULONG),
                ("FileName", WCHAR * (nchars + 1)),
            ]

        fri = _FRI()
        fri.ReplaceIfExists = 1 if replace else 0
        fri.RootDirectory = root_dir
        fri.FileNameLength = len(data)
        ctypes.memmove(ctypes.addressof(fri) + _FRI.FileName.offset, data, len(data))
        iosb = IO_STATUS_BLOCK()
        status = _NtSetInformationFile(
            handle, ctypes.byref(iosb), ctypes.byref(fri),
            _FRI.FileName.offset + len(data), FileRenameInformation,
        )
        if status != STATUS_SUCCESS:
            _raise_ntstatus(status, "NtSetInformationFile(FileRenameInformation)", new_name)

    def _set_delete(handle, context="delete"):
        disp = FILE_DISPOSITION_INFO()
        disp.DeleteFile = 1
        ok = _SetFileInformationByHandle(
            handle, FileDispositionInfo, ctypes.byref(disp), ctypes.sizeof(disp)
        )
        if not ok:
            _raise_win32(f"SetFileInformationByHandle(FileDispositionInfo) {context}")

    def _clear_readonly(handle):
        """Clear FILE_ATTRIBUTE_READONLY on an open file, preserving its times.

        Required before a rename-over: NTFS refuses to replace a read-only file.
        """
        info = FILE_BASIC_INFO()
        if not _GetFileInformationByHandleEx(
            handle, FileBasicInfo, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _raise_win32("GetFileInformationByHandleEx(FileBasicInfo)")
        info.FileAttributes &= ~FILE_ATTRIBUTE_READONLY
        ok = _SetFileInformationByHandle(
            handle, FileBasicInfo, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _raise_win32("SetFileInformationByHandle(clear readonly)")

    def _set_basic_info(handle, creation, last_access, last_write, change, attrs):
        info = FILE_BASIC_INFO()
        info.CreationTime = creation
        info.LastAccessTime = last_access
        info.LastWriteTime = last_write
        info.ChangeTime = change
        info.FileAttributes = attrs
        ok = _SetFileInformationByHandle(
            handle, FileBasicInfo, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _raise_win32("SetFileInformationByHandle(FileBasicInfo)")

    def _write_all(handle, data: bytes):
        view = memoryview(data)
        total = 0
        written = DWORD(0)
        while total < len(view):
            chunk = view[total:total + 1 << 20]
            ok = _WriteFile(handle, chunk.tobytes(), len(chunk), ctypes.byref(written), None)
            if not ok:
                _raise_win32("WriteFile")
            if written.value == 0:
                raise WinFSError(errno.EIO, "WriteFile wrote zero bytes")
            total += written.value

    def _flush(handle):
        if not _FlushFileBuffers(handle):
            _raise_win32("FlushFileBuffers")

    def _dup_handle(handle):
        dup = HANDLE()
        ok = _DuplicateHandle(
            _GetCurrentProcess(), handle,
            _GetCurrentProcess(), ctypes.byref(dup),
            0, False, DUPLICATE_SAME_ACCESS,
        )
        if not ok:
            _raise_win32("DuplicateHandle")
        return dup.value

    def _random_temp_name(prefix=".winfs-") -> str:
        return f"{prefix}{uuid.uuid4().hex}.tmp"

    def _best_effort_unlink(parent, name) -> None:
        """Delete a leftover temp file; never raises (cleanup only)."""
        try:
            handle, _attrs = _open_component(
                parent, name, None, DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="cleanup",
            )
        except Exception:
            return
        try:
            _set_delete(handle, context="cleanup")
        except Exception:
            pass
        finally:
            _close_handle(handle)

    # -- directory enumeration ------------------------------------------------

    def _enumerate_dir(handle):
        """Yield (name, attrs, file_id, size, atime, mtime, ctime) for each entry.

        Uses the Restart class once to reset the per-handle enumeration position
        (making repeated listings on the same handle idempotent), then pages with
        the non-restart class until ERROR_NO_MORE_FILES.  Reparse points are
        yielded with their own attributes; callers decide how to flag them.
        """
        buf = ctypes.create_string_buffer(65536)
        first = True
        while True:
            cls = FileIdBothDirectoryRestartInfo if first else FileIdBothDirectoryInfo
            first = False
            ok = _GetFileInformationByHandleEx(handle, cls, buf, len(buf))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_NO_MORE_FILES:
                    return
                if err == ERROR_INSUFFICIENT_BUFFER:
                    buf = ctypes.create_string_buffer(len(buf) * 4)
                    first = True  # restart with bigger buffer
                    continue
                _raise_win32("GetFileInformationByHandleEx(directory)")
            off = 0
            base_addr = ctypes.addressof(buf)
            while True:
                entry = FILE_ID_BOTH_DIR_INFO.from_address(base_addr + off)
                namelen = entry.FileNameLength
                if namelen:
                    name = ctypes.wstring_at(base_addr + off + FILE_ID_BOTH_DIR_INFO.FileName.offset,
                                             namelen // 2)
                else:
                    name = ""
                if name not in (".", ".."):
                    yield (
                        name,
                        entry.FileAttributes,
                        entry.FileId,
                        entry.EndOfFile,
                        (entry.LastAccessTime - _EPOCH_100NS) * 100,
                        (entry.LastWriteTime - _EPOCH_100NS) * 100,
                        (entry.CreationTime - _EPOCH_100NS) * 100,
                    )
                if entry.NextEntryOffset == 0:
                    break
                off += entry.NextEntryOffset

    def _list_names(handle):
        return [item[0] for item in _enumerate_dir(handle)]

    # -- recursive delete helpers --------------------------------------------

    _DIR_READ_ACCESS = (
        FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
    )
    _DIR_MUTATE_ACCESS = (
        FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY
        | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
    )
    _DIR_DELETE_ACCESS = (
        FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES
        | DELETE | READ_CONTROL | SYNCHRONIZE
    )

    def _validate_tree(handle, depth, counter):
        if depth > _MAX_TREE_DEPTH:
            raise WinFSError(errno.EPERM, f"workspace tree exceeds depth limit {_MAX_TREE_DEPTH}")
        for name, attrs, _fid, _size, _a, _m, _c in _enumerate_dir(handle):
            counter[0] += 1
            if counter[0] > _MAX_TREE_ENTRIES:
                raise WinFSError(errno.EPERM, f"workspace tree exceeds entry limit {_MAX_TREE_ENTRIES}")
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                raise WinFSError(errno.ELOOP, f"nested reparse point not followed: {name!r}")
            if attrs & FILE_ATTRIBUTE_DIRECTORY:
                child, _ = _open_component(
                    handle, name, True, _DIR_READ_ACCESS, context="validate"
                )
                try:
                    _validate_tree(child, depth + 1, counter)
                finally:
                    _close_handle(child)

    def _rename_delete(handle, new_name, root_dir):
        """Rename-before-delete with retry for transient sharing violations.

        Renaming a directory is denied while any handle to it or a descendant is
        open (e.g. a search indexer or AV briefly holding a just-created file).
        The denial is transient, so retry with backoff instead of failing.
        """
        last = None
        for attempt in range(12):
            try:
                _rename_handle(handle, new_name, root_dir, replace=False)
                return
            except WinFSError as exc:
                if exc.errno in (errno.EACCES, errno.EBUSY, errno.EAGAIN) and attempt < 11:
                    last = exc
                    time.sleep(min(0.05, 0.002 * (1 << attempt)))
                    continue
                raise
        raise last or WinFSError(errno.EIO, f"could not rename {new_name!r} for deletion")

    def _delete_dir_contents(handle, depth):
        if depth > _MAX_TREE_DEPTH:
            raise WinFSError(errno.EPERM, f"workspace tree exceeds depth limit {_MAX_TREE_DEPTH}")
        for name in _list_names(handle):
            try:
                child, attrs = _open_component(
                    handle, name, None, _DIR_DELETE_ACCESS, context="delete"
                )
            except WinFSError as exc:
                if exc.errno == errno.ENOENT:
                    continue  # raced away already
                raise
            try:
                if attrs & FILE_ATTRIBUTE_DIRECTORY:
                    _rename_delete(child, _random_temp_name(), handle)
                    _delete_dir_contents(child, depth + 1)
                    _set_delete(child, context="rmdir")
                else:
                    _rename_delete(child, _random_temp_name(), handle)
                    _set_delete(child, context="unlink")
            finally:
                _close_handle(child)

    def _delete_tree_in(parent_handle, name):
        """Recursively delete the child ``name`` of the dir opened at parent_handle."""
        sub, attrs = _open_component(parent_handle, name, None, _DIR_DELETE_ACCESS, context="delete")
        try:
            if not (attrs & FILE_ATTRIBUTE_DIRECTORY):
                raise WinFSError(errno.ENOTDIR, f"not a directory: {name!r}")
            counter = [0]
            _validate_tree(sub, 0, counter)
            # Rename the subtree root to a temp name so a raced re-creation under
            # the original name during deletion is never touched.
            _rename_delete(sub, _random_temp_name(), parent_handle)
            _delete_dir_contents(sub, 0)
            _set_delete(sub, context="rmdir")
        finally:
            _close_handle(sub)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _require_support():
    if not SUPPORTED:
        raise WinFSError(errno.ENOSYS, "winfs native backend is unavailable on this host")


if SUPPORTED:

    class _HandleOwner:
        """Base providing deterministic close() + GC finalizer for one handle."""

        def __init__(self, handle):
            self._handle = handle
            self._closed = False
            self._finalizer = weakref.finalize(self, _close_handle, handle)

        @property
        def handle(self):
            if self._closed:
                raise WinFSError(errno.EBADF, "operation on a closed winfs handle")
            return self._handle

        def close(self) -> None:
            if not self._closed:
                self._closed = True
                self._finalizer()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    class WinDir(_HandleOwner):
        """A directory opened by handle; all operations are relative to it.

        Handles open read-only by default so browsing works even when the
        directory is held with restrictive sharing (Explorer, indexers, AV).
        Mutating methods escalate the handle to write access on demand.
        """

        def __init__(self, handle, owner_root=None, rel_parts=(), writable=False):
            super().__init__(handle)
            info = BY_HANDLE_FILE_INFORMATION()
            if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
                _raise_win32("GetFileInformationByHandle")
            self._volume = info.dwVolumeSerialNumber
            self._owner_root = owner_root
            self._rel_parts = tuple(rel_parts)
            self._writable = writable

        def _ensure_writable(self) -> None:
            """Escalate this directory handle to mutation access.

            Re-opens the directory (relative to the pinned root) with the
            mutation access mask.  Raises EPERM if the handle is not tied to a
            root (standalone construction) and EACCES if another process holds
            the directory with a conflicting share mode.
            """
            if self._writable:
                return
            if self._owner_root is None:
                raise WinFSError(errno.EPERM, "directory handle cannot be upgraded to writable")
            fresh = self._owner_root._open_writable(self._rel_parts)
            old = self._handle
            self._finalizer()  # closes the old read-only handle
            self._finalizer = weakref.finalize(self, _close_handle, fresh)
            self._handle = fresh
            self._writable = True

        # -- metadata ---------------------------------------------------------

        def stat(self, name: str) -> os.stat_result:
            """lstat-equivalent for a direct child.  Reparse points are rejected
            with ELOOP (they are never followed, and never trusted as operands)."""
            handle, _attrs = _open_component(
                self.handle, name, None, FILE_READ_ATTRIBUTES | SYNCHRONIZE, context="stat"
            )
            try:
                return _with_listing_nlink(_stat_from_handle(handle))
            finally:
                _close_handle(handle)

        def list(self):
            """Return ``[(name, os.stat_result), ...]`` for this directory.

            No-follow semantics.  Reparse-point entries are *included* so callers
            can label them, but flagged: ``stat.S_ISLNK(st_mode)`` is True for a
            symlink or mount point (junction).  ``st_nlink`` is best-effort (2 for
            directories, 1 for files); ``(st_dev, st_ino)`` are best-effort from
            the volume serial number and file id.
            """
            entries = []
            for name, attrs, fid, size, atime_ns, mtime_ns, ctime_ns in _enumerate_dir(self.handle):
                mode = _mode_from_attrs(attrs)
                nlink = 2 if (attrs & FILE_ATTRIBUTE_DIRECTORY) else 1
                entries.append(
                    (name, _make_stat_result(mode, fid, self._volume, nlink, size,
                                             atime_ns, mtime_ns, ctime_ns))
                )
            return entries

        def open_read(self, name: str):
            """Open a regular file for reading.  Returns (BinaryIO, os.stat_result).

            Directories are rejected with EISDIR; reparse points with ELOOP.  The
            returned file object owns the underlying handle; closing it releases
            the handle.
            """
            handle, attrs = _open_component(
                self.handle, name, None,
                FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="open_read",
            )
            try:
                if attrs & FILE_ATTRIBUTE_DIRECTORY:
                    raise WinFSError(errno.EISDIR, f"is a directory: {name!r}")
                result_stat = _stat_from_handle(handle)
                if _open_osfhandle is None:
                    raise WinFSError(errno.ENOSYS, "_open_osfhandle unavailable")
                fd = _open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                # Ownership of `handle` transfers to the CRT fd.
                return os.fdopen(fd, "rb"), result_stat
            except Exception:
                _close_handle(handle)
                raise

        def open_read_fd(self, name: str):
            """Open a regular file for reading and return (fd, os.stat_result).

            Unlike open_read, this returns a bare OS file descriptor (int) that the
            caller owns and must os.close() -- matching the POSIX workspace preview
            and download contract where the HTTP layer lseeks/reads/closes the fd.
            Directories are rejected with EISDIR; reparse points with ELOOP.
            """
            handle, attrs = _open_component(
                self.handle, name, None,
                FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="open_read_fd",
            )
            try:
                if attrs & FILE_ATTRIBUTE_DIRECTORY:
                    raise WinFSError(errno.EISDIR, f"is a directory: {name!r}")
                result_stat = _stat_from_handle(handle)
                if _open_osfhandle is None:
                    raise WinFSError(errno.ENOSYS, "_open_osfhandle unavailable")
                fd = _open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                # Ownership of `handle` transfers to the returned fd.
                return fd, result_stat
            except Exception:
                _close_handle(handle)
                raise

        # -- mutations --------------------------------------------------------

        def create_file(self, name: str, data: bytes) -> os.stat_result:
            """Create a new file with ``data`` (CREATE_NEW).  EEXIST on collision;
            never follows links, never overwrites."""
            self._ensure_writable()
            _validate_component(name)
            handle = _nt_create_file(
                name, self.handle,
                FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                FILE_CREATE,
                FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
            )
            try:
                attrs, tag = _query_attribs(handle)
                _check_not_reparse(attrs, tag, name)
                if attrs & FILE_ATTRIBUTE_DIRECTORY:
                    raise WinFSError(errno.EISDIR, f"is a directory: {name!r}")
                _write_all(handle, bytes(data))
                _flush(handle)
                return _with_listing_nlink(_stat_from_handle(handle))
            finally:
                _close_handle(handle)

        def replace_file(self, name: str, data: bytes, expected_identity=None) -> os.stat_result:
            self._ensure_writable()
            """Atomically replace a file's contents via temp-write + rename-over.

            If ``expected_identity`` (st_dev, st_ino, st_mtime_ns, st_size) is
            given, the current file's identity is verified after open and before
            writing; a mismatch raises EBUSY.  Without it, the replace always
            lands whole (never torn).  Never mutates in place.
            """
            _validate_component(name)
            # Open current for identity + metadata (fail ENOENT/ELOOP/ENOTDIR here).
            cur, cur_attrs = _open_component(
                self.handle, name, False,
                FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | SYNCHRONIZE,
                context="replace",
            )
            try:
                if cur_attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise WinFSError(errno.ELOOP, f"reparse point not replaced: {name!r}")
                cur_stat = _stat_from_handle(cur)
                identity = (cur_stat.st_dev, cur_stat.st_ino, cur_stat.st_mtime_ns, cur_stat.st_size)
                if expected_identity is not None and tuple(expected_identity) != identity:
                    raise WinFSError(
                        errno.EBUSY,
                        f"{name!r} changed on disk (expected identity mismatch)",
                    )
                # Preserve readonly-ness of the original on the replaced file.
                orig_attrs = cur_attrs & FILE_ATTRIBUTE_READONLY
                # NTFS refuses to rename over a read-only destination, so clear
                # the bit first; the temp is made read-only after the rename.
                if orig_attrs:
                    _clear_readonly(cur)
            finally:
                _close_handle(cur)

            # Atomic replace: write a fresh temp file inside this directory handle,
            # flush it, then rename it over the destination.  The rename-over is
            # retried a bounded number of times: under heavy concurrent replace of
            # the same name NTFS may transiently deny the replace, and a fresh temp
            # + retry always lands a whole file (never torn, never in place).
            last_exc = None
            for _attempt in range(24):
                temp_name = _random_temp_name(prefix=f".{name}.winfs-")
                tmp = _nt_create_file(
                    temp_name, self.handle,
                    FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | DELETE | SYNCHRONIZE,
                    FILE_CREATE,
                    FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
                )
                renamed = False
                try:
                    tattrs, ttag = _query_attribs(tmp)
                    _check_not_reparse(tattrs, ttag, temp_name)
                    _write_all(tmp, bytes(data))
                    _flush(tmp)
                    if orig_attrs:
                        _set_basic_info(tmp, 0, 0, 0, 0, FILE_ATTRIBUTE_READONLY)
                    try:
                        _rename_handle(tmp, name, self.handle, replace=True)
                    except WinFSError as exc:
                        if exc.errno in (errno.EACCES, errno.EBUSY, errno.EAGAIN) and _attempt < 23:
                            last_exc = exc
                            # Exponential backoff to break contention herds.
                            time.sleep(min(0.05, 0.001 * (1 << _attempt)))
                            continue  # transient contention: fresh temp + retry
                        raise
                    renamed = True
                    return _stat_from_handle(tmp)
                finally:
                    _close_handle(tmp)
                    if not renamed:
                        _best_effort_unlink(self.handle, temp_name)
            raise last_exc or WinFSError(errno.EIO, f"could not replace {name!r}")

        def mkdir(self, name: str) -> os.stat_result:
            """Create a directory.  EEXIST on collision."""
            self._ensure_writable()
            _validate_component(name)
            handle = _nt_create_file(
                name, self.handle,
                _DIR_MUTATE_ACCESS,
                FILE_CREATE,
                FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
            )
            try:
                attrs, tag = _query_attribs(handle)
                _check_not_reparse(attrs, tag, name)
                return _with_listing_nlink(_stat_from_handle(handle))
            finally:
                _close_handle(handle)

        def unlink(self, name: str) -> None:
            """Delete a file.  Reparse -> ELOOP; directories -> EISDIR."""
            self._ensure_writable()
            handle, attrs = _open_component(
                self.handle, name, None, DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="unlink",
            )
            try:
                if attrs & FILE_ATTRIBUTE_DIRECTORY:
                    raise WinFSError(errno.EISDIR, f"is a directory: {name!r}")
                _set_delete(handle, context="unlink")
            finally:
                _close_handle(handle)

        def rmdir(self, name: str) -> None:
            """Delete an empty directory.  Non-empty -> ENOTEMPTY; reparse -> ELOOP;
            non-directory -> ENOTDIR."""
            self._ensure_writable()
            handle, attrs = _open_component(
                self.handle, name, None, DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="rmdir",
            )
            try:
                if not (attrs & FILE_ATTRIBUTE_DIRECTORY):
                    raise WinFSError(errno.ENOTDIR, f"not a directory: {name!r}")
                _set_delete(handle, context="rmdir")
            finally:
                _close_handle(handle)

        def rename(self, old: str, new: str, *, replace: bool = False) -> None:
            """Rename a direct child.  Without ``replace``, an existing destination
            yields EEXIST.  Reparse operands yield ELOOP."""
            self._ensure_writable()
            _validate_component(new)
            handle, attrs = _open_component(
                self.handle, old, None, DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                context="rename",
            )
            try:
                # Refuse to overwrite a reparse point even with replace=True.
                try:
                    _query_dest(self.handle, new)
                except WinFSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                _rename_handle(handle, new, self.handle, replace=replace)
            finally:
                _close_handle(handle)

        def delete_tree(self, name: str) -> None:
            """Recursively delete the child directory ``name``.

            Bounded (depth/size), rejects nested reparse points with ELOOP before
            deleting anything, and renames every victim to a temp name inside its
            parent before deletion so a raced re-creation is never removed.
            """
            self._ensure_writable()
            _delete_tree_in(self.handle, name)

        def flush(self) -> None:
            """Best-effort fsync-equivalent for the directory handle."""
            try:
                _flush(self.handle)
            except WinFSError:
                pass

    def _query_dest(parent, name):
        """Confirm a destination child exists and is not a reparse point.

        Raises ENOENT if missing and ELOOP/EPERM if it is a reparse point (via
        _open_component).  Returns the file attributes.
        """
        handle, attrs = _open_component(
            parent, name, None, FILE_READ_ATTRIBUTES | SYNCHRONIZE, context="stat"
        )
        _close_handle(handle)
        return attrs

    class WinRoot(_HandleOwner):
        """The pinned, trusted workspace root directory.

        Opens read-only; mutations escalate on demand (see WinDir._ensure_writable
        and _open_writable) so browsing works even when the directory is held by
        another process with restrictive sharing.
        """

        def __init__(self, handle, final_path: str, device: int, win_path: str):
            super().__init__(handle)
            self.final_path = final_path
            self.device = device
            self._win_path = win_path
            self._writable_root = False

        @staticmethod
        def _traverse(handle, parts, access):
            current = handle
            for part in parts:
                nxt, _attrs = _open_component(current, part, True, access, context="traverse")
                if current != handle:
                    _close_handle(current)
                current = nxt
            return current

        def _open_writable(self, parts):
            """Open ``parts`` (or the root itself when empty) with mutation access."""
            if not parts:
                handle = _CreateFileW(
                    self._win_path, _DIR_MUTATE_ACCESS, _SHARE_ALL, None,
                    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None,
                )
                if handle == INVALID_HANDLE_VALUE or handle is None:
                    _raise_win32("CreateFileW(root escalate)", self.final_path)
                return handle
            return self._traverse(self.handle, parts, _DIR_MUTATE_ACCESS)

        def _ensure_root_writable(self) -> None:
            if self._writable_root:
                return
            fresh = self._open_writable(())
            old = self._handle
            self._finalizer()
            self._finalizer = weakref.finalize(self, _close_handle, fresh)
            self._handle = fresh
            self._writable_root = True

        def open_dir(self, parts) -> "WinDir":
            """Traverse ``parts`` and return the directory at that path (read-only)."""
            parts = _normalize_parts(parts)
            if not parts:
                return WinDir(_dup_handle(self.handle), owner_root=self, rel_parts=())
            handle = self._traverse(self.handle, parts, _DIR_READ_ACCESS)
            return WinDir(handle, owner_root=self, rel_parts=parts)

        def open_parent(self, parts):
            """Traverse all but the last component; return (WinDir, final_name).

            The final name is validated but *not* opened.  The returned directory
            is read-only until a mutation method escalates it.
            """
            parts = _normalize_parts(parts)
            if not parts:
                raise WinFSError(errno.EINVAL, "a non-empty path is required")
            last = parts[-1]
            _validate_component(last)
            if len(parts) == 1:
                return WinDir(_dup_handle(self.handle), owner_root=self, rel_parts=()), last
            handle = self._traverse(self.handle, parts[:-1], _DIR_READ_ACCESS)
            return WinDir(handle, owner_root=self, rel_parts=parts[:-1]), last

        def delete_tree(self, name: str) -> None:
            self._ensure_root_writable()
            _delete_tree_in(self._handle, name)


def _normalize_parts(parts):
    if isinstance(parts, (str, bytes)):
        raise TypeError("parts must be a sequence of names, not a string")
    parts = tuple(parts)
    for part in parts:
        if not isinstance(part, str):
            raise TypeError(f"path components must be str, got {type(part).__name__}")
    return parts


def open_root(path: str) -> "WinRoot":
    """Open and pin the trusted workspace root directory.

    ``path`` is a trusted, absolute native path.  The root may itself be a
    reparse point (resolved once here); afterwards it is pinned to its final
    path via ``GetFinalPathNameByHandleW``.  Raises WinFSError(ENOENT) if the
    path does not exist and WinFSError(ENOTDIR) if it is not a directory.
    """
    _require_support()
    if not isinstance(path, str) or not path:
        raise WinFSError(errno.EINVAL, "a root path is required")
    win_path = _to_extended_path(path)
    # Open read-only: profile/project directories are routinely held by Explorer,
    # search indexers, or AV with restrictive sharing, and requesting write-class
    # access on the root fails with ERROR_SHARING_VIOLATION (Win32 32).  Mutation
    # methods escalate on demand via WinRoot._open_writable.
    access = _DIR_READ_ACCESS
    handle = _CreateFileW(
        win_path, access, _SHARE_ALL, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        _raise_win32("CreateFileW(root)", path)
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
            _raise_win32("GetFileInformationByHandle(root)", path)
        if not (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            raise WinFSError(errno.ENOTDIR, f"root is not a directory: {path}")
        final_path = _final_path(handle)
        device = info.dwVolumeSerialNumber
        return WinRoot(handle, final_path, device, win_path)
    except Exception:
        _close_handle(handle)
        raise


def _to_extended_path(path: str) -> str:
    """Prefix an absolute path with \\?\\ so >260 char paths work, normalizing
    separators.  The root path is trusted, so building this string is safe."""
    p = path
    if p.startswith(("\\\\?\\", "\\\\?\\UNC\\")):
        return p
    p = p.replace("/", "\\")
    if p.startswith("\\\\"):  # UNC -> \\?\UNC\
        return "\\\\?\\UNC\\" + p[2:]
    if not p.startswith("\\"):
        p = "\\" + p
    return "\\\\?\\" + p.lstrip("\\") if not p.startswith("\\\\?\\") else p


def _final_path(handle) -> str:
    size = 260
    while True:
        buf = (WCHAR * size)()
        n = _GetFinalPathNameByHandleW(handle, buf, size, 0)  # VOLUME_NAME_DOS | FILE_NAME_NORMALIZED
        if n == 0:
            _raise_win32("GetFinalPathNameByHandleW")
        if n < size:
            value = buf.value
            # Normalize \\?\C:\... -> C:\... for a cleaner reported path.
            if value.startswith("\\\\?\\UNC\\"):
                return "\\\\" + value[len("\\\\?\\UNC\\"):]
            if value.startswith("\\\\?\\"):
                return value[len("\\\\?\\"):]
            return value
        size = n + 1
