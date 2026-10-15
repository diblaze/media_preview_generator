"""Windows answers to the Linux-only questions the Plex database role asks before it opens Plex's database.

On Linux, ``markers/publishers/plex_db.py`` proves Plex has its database open with a POSIX lock query (``fcntl``), tells
a local disk from a network share with ``/proc/self/mountinfo`` (``markers/fs.py``) and names users by uid. None of
those exist on Windows. Here the same answers come from Restart Manager and ``LockFileEx`` (who has the database
open), ``GetDriveTypeW`` (where it sits) and the file's own access check and owner (who may write it).

Pure ctypes and the standard library, so the Windows build needs nothing more and the module imports anywhere: each
function that needs Windows raises :class:`WindowsOnlyError` elsewhere. Facts measured on a Windows runner
(``docs/design/remote-nodes/README.md`` §9): a live WAL connection holds a shared lock on exactly byte 128 of
``<db>-shm``; an exclusive ``LockFileEx`` probe on it fails with error 33 while one is open; ``GetDriveTypeW`` answers 3
for C:\\ and a ``subst`` drive, 4 for a mapped drive and a UNC path.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
from ctypes import wintypes
from types import SimpleNamespace

IS_WINDOWS = sys.platform == "win32"

# SQLite's WAL index ("-shm") dead-man-switch byte: every open WAL connection holds a shared lock on it.
SHM_DMS_BYTE = 128

DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = range(7)
# What ``file_checks`` reports as the database's filesystem for a drive that isn't a fixed disk. "network drive" is in
# ``markers.fs.NETWORK_FS_TYPES``, so the app words a refusal from a Windows node as a network share.
DRIVE_LABELS = {
    DRIVE_UNKNOWN: "unknown drive",
    DRIVE_NO_ROOT_DIR: "missing drive",
    DRIVE_REMOVABLE: "removable drive",
    DRIVE_REMOTE: "network drive",
    DRIVE_CDROM: "cd-rom drive",
    DRIVE_RAMDISK: "ram disk",
}

# Plex on Windows keeps its preferences here, not in a Preferences.xml (support.plex.tv/articles/201105343).
PLEX_REGISTRY_KEY = r"Software\Plex, Inc.\Plex Media Server"
PLEX_REGISTRY_VALUE = "LocalAppDataPath"
PLEX_MACHINE_ID_VALUE = "ProcessedMachineIdentifier"

_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_FILE_ADD_FILE = 0x0002
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_LOCKFILE_FAIL_IMMEDIATELY, _LOCKFILE_EXCLUSIVE_LOCK = 0x1, 0x2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND = 2, 3
_ERROR_LOCK_VIOLATION = 33
_ERROR_MORE_DATA = 234
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_CCH_RM_SESSION_KEY = 32
_CCH_RM_MAX_APP_NAME, _CCH_RM_MAX_SVC_NAME = 255, 63


class WindowsOnlyError(RuntimeError):
    """A Windows-only call made on another platform."""


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (_CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (_CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


def _last_error() -> int:
    return ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows-only API


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise WindowsOnlyError(f"Windows only (this is {sys.platform})")


@functools.cache
def _api() -> SimpleNamespace:
    """kernel32, advapi32 and Restart Manager with the signatures used here, loaded once."""
    _require_windows()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only API
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only API
    rm = ctypes.WinDLL("rstrtmgr")  # type: ignore[attr-defined]  # Windows-only API

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    dword = wintypes.DWORD
    k32.LockFileEx.argtypes = [wintypes.HANDLE, dword, dword, dword, dword, ctypes.POINTER(_OVERLAPPED)]
    k32.UnlockFileEx.argtypes = [wintypes.HANDLE, dword, dword, dword, ctypes.POINTER(_OVERLAPPED)]
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    advapi.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_int),
    ]

    rm.RmStartSession.restype = wintypes.DWORD
    rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    rm.RmRegisterResources.restype = wintypes.DWORD
    rm.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
    ]
    rm.RmGetList.restype = wintypes.DWORD
    rm.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(_RM_PROCESS_INFO),
        ctypes.POINTER(wintypes.DWORD),
    ]
    rm.RmEndSession.restype = wintypes.DWORD
    rm.RmEndSession.argtypes = [wintypes.DWORD]
    return SimpleNamespace(k32=k32, advapi=advapi, rm=rm)


def _open(path: str, access: int, flags: int = 0) -> int | None:
    """A handle on ``path`` that shares everything with every other opener, or None (see ``ctypes.get_last_error``)."""
    handle = _api().k32.CreateFileW(path, access, _FILE_SHARE_ALL, None, _OPEN_EXISTING, flags, None)
    return None if handle in (None, _INVALID_HANDLE_VALUE) else handle


def restart_manager_holders(path: str) -> list[int] | None:
    """The ids of the processes that have ``path`` open, as Restart Manager lists them.

    Restart Manager only asks the system who has the file open; it takes no lock on it. It lists this process too when
    this process has the file open.

    Args:
        path: The file.

    Returns:
        The process ids, sorted; None when Restart Manager couldn't answer.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    rm = _api().rm
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return None
    try:
        files = (wintypes.LPCWSTR * 1)(path)
        if rm.RmRegisterResources(session.value, 1, files, 0, None, 0, None):
            return None
        size = 16
        # A process can open the file between two calls, so "more data" is asked again a few times at the new size.
        for _attempt in range(4):
            needed, count, reasons = wintypes.UINT(0), wintypes.UINT(size), wintypes.DWORD(0)
            infos = (_RM_PROCESS_INFO * size)()
            rc = rm.RmGetList(session.value, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
            if rc == _ERROR_MORE_DATA:
                size = max(size * 2, needed.value)
                continue
            if rc:
                return None
            return sorted({infos[i].Process.dwProcessId for i in range(count.value)})
        return None
    finally:
        rm.RmEndSession(session.value)


def dms_lock_held(shm_path: str) -> bool | None:
    """Whether some open SQLite connection holds the WAL dead-man-switch lock (byte 128) of ``shm_path``.

    An exclusive ``LockFileEx`` on that byte that fails at once (error 33) means a connection holds it; one that
    succeeds is released straight away. While it is held, a connection that opens the database right then waits for
    it (SQLite's WAL retry loop; measured: a new connection waited out a 1.5 s hold and then opened, busy timeout 0),
    so the window is this function's few microseconds. Locks are per handle on Windows: this process's own open
    connections count as holders too, so call it with none of ours open (``plex_db`` holds its own per-database lock
    around the probe, as it does on Linux).

    Args:
        shm_path: The database's ``-shm`` file.

    Returns:
        True when held, False when not (or there is no ``-shm`` file), None when the probe itself failed.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    k32 = _api().k32
    handle = _open(shm_path, _GENERIC_READ)
    if handle is None:
        return False if _last_error() in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND) else None
    try:
        flags = _LOCKFILE_FAIL_IMMEDIATELY | _LOCKFILE_EXCLUSIVE_LOCK
        if k32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_OVERLAPPED(Offset=SHM_DMS_BYTE))):
            k32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_OVERLAPPED(Offset=SHM_DMS_BYTE)))
            return False
        return True if _last_error() == _ERROR_LOCK_VIOLATION else None
    finally:
        k32.CloseHandle(handle)


def plex_holds_db(db_path: str) -> bool | None:
    """Whether another process (Plex) has the WAL database ``db_path`` open: the Windows ``F_GETLK`` probe.

    Restart Manager first, without our own process: when no other process has ``<db>-shm`` open, no other connection
    can hold its lock, and the answer is no without touching the lock at all. When another process has it open (or
    Restart Manager can't answer), the ``LockFileEx`` probe proves it is a live SQLite connection rather than, say, a
    virus scanner reading the file; against a live connection that probe fails at once and so never holds the byte.
    The probe's brief exclusive hold (see :func:`dms_lock_held`) therefore only happens when some process has the file
    open without a connection: then a connection opening in those microseconds retries and opens.

    Args:
        db_path: The database file; a rollback-journal database (no ``-shm``) is never held, as on Linux.

    Returns:
        True when another process holds it, False when none does, None when neither check could tell.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    _require_windows()
    shm = f"{db_path}-shm"
    if not os.path.exists(shm):
        return False
    holders = restart_manager_holders(shm)
    if holders is not None and not [pid for pid in holders if pid != os.getpid()]:
        return False
    return dms_lock_held(shm)


def volume_root(path: str) -> str:
    """The root of the volume ``path`` is on (``C:\\``, ``\\\\server\\share\\``, a mounted folder).

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    buf = ctypes.create_unicode_buffer(32_768)
    if _api().k32.GetVolumePathNameW(os.path.abspath(path), buf, len(buf)):
        return buf.value
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive.rstrip("\\") + "\\"


def drive_type(path: str) -> int:
    """``GetDriveTypeW`` of the volume ``path`` is on (``DRIVE_FIXED``, ``DRIVE_REMOTE``, ...).

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    return int(_api().k32.GetDriveTypeW(volume_root(path)))


def is_local_drive(path: str) -> bool:
    """Whether ``path`` is on a fixed disk of this machine, where SQLite's locks and shared memory are safe.

    A ``subst`` drive reports the fixed disk under it; a mapped drive or UNC path is remote, and anything Windows
    can't classify is not local either.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    return drive_type(path) == DRIVE_FIXED


def filesystem_label(path: str) -> str:
    """What to call the filesystem ``path`` is on: the volume's own name ("ntfs", "refs") on a fixed disk, else the
    drive kind (``DRIVE_LABELS``).

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    kind = drive_type(path)
    if kind != DRIVE_FIXED:
        return DRIVE_LABELS.get(kind, DRIVE_LABELS[DRIVE_UNKNOWN])
    name = ctypes.create_unicode_buffer(64)
    if _api().k32.GetVolumeInformationW(volume_root(path), None, 0, None, None, None, name, len(name)):
        return name.value.lower() or "fixed disk"
    return "fixed disk"


def same_user_can_write(path: str) -> bool:
    """Whether this process may write ``path`` (a file) or create files in it (a folder), by the file's own ACL.

    Opens it for writing and closes it at once, writing nothing: Windows checks the ACL and the read-only attribute
    on open. The handle shares everything, so it never stands in the way of Plex, and closing it drops no lock of
    anyone's (Windows locks belong to the handle that took them).

    Args:
        path: A file or folder that exists.

    Returns:
        False when Windows refuses to open it that way (access denied, read-only, held without sharing).

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    if os.path.isdir(path):
        handle = _open(path, _FILE_ADD_FILE, _FILE_FLAG_BACKUP_SEMANTICS)
    else:
        handle = _open(path, _GENERIC_READ | _GENERIC_WRITE)
    if handle is None:
        return False
    _api().k32.CloseHandle(handle)
    return True


def current_user() -> str:
    """This process's user, as ``DOMAIN\\name`` when the domain is known.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    size = wintypes.DWORD(257)
    buf = ctypes.create_unicode_buffer(size.value)
    name = buf.value if _api().advapi.GetUserNameW(buf, ctypes.byref(size)) else os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{name}" if domain and name else name or "unknown"


def file_owner(path: str) -> str | None:
    """The owner of ``path`` as ``DOMAIN\\name`` (the SID string's account when it has no name), or None.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    api = _api()
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    rc = api.advapi.GetNamedSecurityInfoW(
        path,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if rc:
        return None
    try:
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        name_len, domain_len, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
        found = api.advapi.LookupAccountSidW(
            None, owner, name, ctypes.byref(name_len), domain, ctypes.byref(domain_len), ctypes.byref(use)
        )
        if not found:
            return None
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        api.k32.LocalFree(descriptor)


def _plex_registry_value(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PLEX_REGISTRY_KEY) as key:  # type: ignore[attr-defined]  # Windows-only API
            value, _kind = winreg.QueryValueEx(key, name)  # type: ignore[attr-defined]  # Windows-only API
    except OSError:
        return None
    return str(value).strip() or None


def default_plex_config_dir() -> str:
    """The "Plex Media Server" folder of the Plex that runs as this user.

    Plex keeps it under ``%LOCALAPPDATA%``, or under the folder its ``LocalAppDataPath`` registry value names when the
    user moved it there (Plex's own documented way to move its data off the system disk).

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    _require_windows()
    moved = os.path.expandvars(_plex_registry_value(PLEX_REGISTRY_VALUE) or "").strip()
    base = moved or os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(base, "Plex Media Server")


def plex_machine_identifier(plex_config_dir: str) -> str:
    """The ``ProcessedMachineIdentifier`` of the Plex whose data folder is ``plex_config_dir``, from the registry.

    The registry holds the preferences of the one Plex that runs as this user, so its value only names a folder that
    is that Plex's own (:func:`default_plex_config_dir`); any other folder belongs to some other Plex, and gets "".

    Args:
        plex_config_dir: Plex's data folder as this node was given it.

    Returns:
        The identifier, or "" when it isn't there or the folder isn't this user's Plex's.

    Raises:
        WindowsOnlyError: Not on Windows.
    """
    _require_windows()

    def same(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    if same(plex_config_dir) != same(default_plex_config_dir()):
        return ""
    return _plex_registry_value(PLEX_MACHINE_ID_VALUE) or ""
