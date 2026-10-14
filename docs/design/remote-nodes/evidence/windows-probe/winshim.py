"""pytest plugin (-p winshim): the smallest Windows layer under the app's Plex DB code, to see what else breaks.

Replaces only the two Linux calls found so far: the fcntl lock query and os.geteuid.
"""

import ctypes
import os
import signal
import sys
import types
from ctypes import wintypes

for _name, _num in (("SIGBUS", 7), ("SIGKILL", 9), ("SIGSTOP", 19), ("SIGCONT", 18), ("SIGQUIT", 3), ("SIGHUP", 1)):
    if not hasattr(signal, _name):
        setattr(signal, _name, _num)

stub = types.ModuleType("fcntl")
stub.F_WRLCK, stub.F_UNLCK, stub.F_GETLK = 1, 2, 5
sys.modules.setdefault("fcntl", stub)
if not hasattr(os, "geteuid"):
    os.geteuid = lambda: 0  # type: ignore[attr-defined]

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


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
k32.LockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(OVERLAPPED),
]
k32.UnlockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(OVERLAPPED),
]


def _shm_dms_locked_elsewhere_win(db_path: str) -> bool:
    h = k32.CreateFileW(f"{db_path}-shm", 0xC0000000, 7, None, 3, 0, None)
    if h in (None, ctypes.c_void_p(-1).value):
        return False
    try:
        ov = OVERLAPPED(Offset=128)
        if k32.LockFileEx(h, 0x3, 0, 1, 0, ctypes.byref(ov)):
            k32.UnlockFileEx(h, 0, 1, 0, ctypes.byref(OVERLAPPED(Offset=128)))
            return False
        return ctypes.get_last_error() == 33
    finally:
        k32.CloseHandle(h)


k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]


def _filesystem_type_win(path, *, mountinfo_path=None):
    root = os.path.splitdrive(os.path.abspath(path))[0] + os.sep
    return {3: "ntfs", 4: "smb3", 2: "vfat"}.get(k32.GetDriveTypeW(root))


def pytest_configure(config):
    from media_preview_generator.markers import fs
    from media_preview_generator.markers.publishers import plex_chapters, plex_db, plex_remote

    plex_db._shm_dms_locked_elsewhere = _shm_dms_locked_elsewhere_win
    fs.LOCAL_FS_TYPES = fs.LOCAL_FS_TYPES | {"ntfs"}
    for mod in (fs, plex_db, plex_chapters, plex_remote):
        if hasattr(mod, "filesystem_type"):
            mod.filesystem_type = _filesystem_type_win
