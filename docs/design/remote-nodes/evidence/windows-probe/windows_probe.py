"""Throwaway Windows probe for the remote-nodes design. Prints a JSON-ish report; never asserts.

Questions it answers with real Windows behaviour:
  Q1  Which -shm bytes does a live SQLite WAL connection hold locked on Windows (is the DMS byte 128)?
  Q2  Does a non-blocking LockFileEx probe on that byte tell "a process holds the DB open" from "nobody does"?
  Q3  If the probe holds that byte exclusively for a moment, what does a NEW connection opening the DB get?
  Q4  Does Restart Manager list the holder process for the -shm / db file without taking any lock?
  Q5  GetDriveTypeW for a fixed disk, a mapped network drive, a UNC path and a subst drive.
  Q6  Does the app's Plex DB module import on Windows (fcntl)?
  Q7  SQLite version shipped with this Python.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
LOCKFILE_FAIL_IMMEDIATELY, LOCKFILE_EXCLUSIVE_LOCK = 0x1, 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


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
k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]


def open_rw(path: str) -> int:
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE or h is None:
        raise OSError(ctypes.get_last_error(), f"CreateFileW {path}")
    return h


def try_lock(h: int, offset: int, exclusive: bool = True) -> tuple[bool, int]:
    ov = OVERLAPPED(Offset=offset)
    flags = LOCKFILE_FAIL_IMMEDIATELY | (LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    ok = bool(k32.LockFileEx(h, flags, 0, 1, 0, ctypes.byref(ov)))
    return ok, (0 if ok else ctypes.get_last_error())


def unlock(h: int, offset: int) -> None:
    ov = OVERLAPPED(Offset=offset)
    k32.UnlockFileEx(h, 0, 1, 0, ctypes.byref(ov))


HOLDER = r"""
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE IF NOT EXISTS t(x)")
c.execute("INSERT INTO t VALUES (1)")
c.execute("SELECT count(*) FROM t").fetchone()
print("ready", flush=True)
sys.stdin.readline()
c.close()
"""

OPENER = r"""
import sqlite3, sys, time
t0 = time.time()
try:
    c = sqlite3.connect(sys.argv[1], timeout=float(sys.argv[2]), isolation_level=None)
    n = c.execute("SELECT count(*) FROM t").fetchone()[0]
    print(f"ok rows={n} after {time.time()-t0:.3f}s")
except Exception as e:
    print(f"ERR {type(e).__name__}: {e} after {time.time()-t0:.3f}s")
"""


def start_holder(db: str) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", HOLDER, db], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


def stop_holder(p: subprocess.Popen) -> None:
    p.stdin.write("\n")
    p.stdin.flush()
    p.wait(10)


def scan_locked_bytes(shm: str, lo: int = 0, hi: int = 140) -> list[int]:
    h = open_rw(shm)
    held = []
    try:
        for off in range(lo, hi):
            ok, _ = try_lock(h, off)
            if ok:
                unlock(h, off)
            else:
                held.append(off)
    finally:
        k32.CloseHandle(h)
    return held


# --- Restart Manager -------------------------------------------------------------------------------
rm = ctypes.WinDLL("rstrtmgr")
CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME, CCH_RM_MAX_SVC_NAME = 255, 63


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


def rm_holders(path: str) -> tuple[int, list[tuple[int, str]]]:
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    rc = rm.RmStartSession(ctypes.byref(session), 0, key)
    if rc:
        return rc, []
    try:
        files = (wintypes.LPCWSTR * 1)(path)
        rc = rm.RmRegisterResources(session, 1, files, 0, None, 0, None)
        if rc:
            return rc, []
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(16), wintypes.DWORD()
        infos = (RM_PROCESS_INFO * 16)()
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
        return rc, [(infos[i].Process.dwProcessId, infos[i].strAppName) for i in range(count.value)]
    finally:
        rm.RmEndSession(session)


def main() -> None:
    print("Q7 sqlite", sqlite3.sqlite_version, "python", sys.version.split()[0])
    base = os.path.join(os.environ.get("RUNNER_TEMP", r"C:\tmp"), "probe")
    os.makedirs(base, exist_ok=True)
    db = os.path.join(base, "library.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    shm = db + "-shm"

    holder = start_holder(db)
    print("Q1 holder pid", holder.pid, "shm exists", os.path.exists(shm))
    held = scan_locked_bytes(shm)
    print("Q1 bytes held by a live WAL connection (exclusive probe fails):", held)
    rc, procs = rm_holders(shm)
    print("Q4 RestartManager on -shm: rc", rc, "procs", procs, "holder listed", any(p == holder.pid for p, _ in procs))
    rc, procs = rm_holders(db)
    print("Q4 RestartManager on db:   rc", rc, "procs", procs, "holder listed", any(p == holder.pid for p, _ in procs))

    # Shared (not exclusive) probe on 128 while held: does a shared lock succeed alongside the holder's?
    h = open_rw(shm)
    ok, err = try_lock(h, 128, exclusive=False)
    print("Q2 shared probe on 128 with holder:", ok, err)
    if ok:
        unlock(h, 128)
    ok, err = try_lock(h, 128, exclusive=True)
    print("Q2 exclusive probe on 128 with holder:", ok, "err", err, "(33 = ERROR_LOCK_VIOLATION)")
    if ok:
        unlock(h, 128)
    k32.CloseHandle(h)
    stop_holder(holder)
    print("Q2 after holder closed: shm exists", os.path.exists(shm), "wal exists", os.path.exists(db + "-wal"))

    # Keep a -shm around with no holder: open the db, then kill the process hard so files stay.
    p = subprocess.Popen([sys.executable, "-c", HOLDER, db], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    p.kill()
    p.wait(10)
    print("Q2 after holder killed: shm exists", os.path.exists(shm))
    if os.path.exists(shm):
        h = open_rw(shm)
        ok, err = try_lock(h, 128)
        print("Q2 exclusive probe on 128 with NO holder:", ok, err)
        rc, procs = rm_holders(shm)
        print("Q4 RestartManager with no holder: rc", rc, "procs", procs)
        # Q3: hold 128 exclusively while a new connection opens, with sqlite busy timeouts 0 and 5 s.
        for timeout in ("0", "5"):
            opener = subprocess.Popen([sys.executable, "-c", OPENER, db, timeout], stdout=subprocess.PIPE, text=True)
            time.sleep(1.5)
            unlock(h, 128)
            out = opener.communicate(timeout=30)[0].strip()
            print(f"Q3 new connection while probe held 128 for 1.5 s (busy timeout {timeout}s):", out)
            try_lock(h, 128)
        unlock(h, 128)
        k32.CloseHandle(h)

    # Q3b: same, but a holder is ALSO running (realistic: Plex open) - probe fails so nothing is held.
    holder = start_holder(db)
    out = subprocess.run([sys.executable, "-c", OPENER, db, "0"], capture_output=True, text=True).stdout.strip()
    print("Q3b new connection with holder running, no probe:", out)
    stop_holder(holder)

    # Q5 drive types: 2 removable, 3 fixed, 4 remote, 5 cdrom, 6 ramdisk, 1 no root dir
    os.system(r"subst S: %RUNNER_TEMP% >NUL 2>&1")
    os.system(r"net use Z: \\localhost\C$ >NUL 2>&1")
    for p in ("C:\\", "S:\\", "Z:\\", "\\\\localhost\\C$\\"):
        print("Q5 GetDriveTypeW", repr(p), "->", k32.GetDriveTypeW(p))

    # Q6 import the app's Plex DB module
    try:
        import media_preview_generator.markers.publishers.plex_db  # noqa: F401

        print("Q6 plex_db import: ok")
    except Exception as e:  # noqa: BLE001
        print("Q6 plex_db import:", type(e).__name__, e)


if __name__ == "__main__":
    main()
