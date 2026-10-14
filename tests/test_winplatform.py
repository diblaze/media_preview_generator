"""winplatform: the Windows answers the Plex database role needs, against the real Windows APIs.

Everything here but ``TestOffWindows`` runs on Windows only (``.github/workflows/windows-node.yml``); a real SQLite
WAL database held open by another process stands in for Plex.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import string
import subprocess
import sys
import uuid

import pytest

from media_preview_generator.nodes.server import winplatform

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="calls the Windows API")

_HOLDER = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
    "conn.execute('SELECT COUNT(*) FROM t').fetchall()\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


def _db(tmp_path, journal: str = "wal") -> str:
    path = str(tmp_path / "library.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    return path


@contextlib.contextmanager
def _holding(db: str):
    """Another process with the database open, as Plex has it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, db], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def _killed_holder_leaves_shm(db: str) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, db], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()
    proc.wait(timeout=10)
    assert os.path.exists(f"{db}-shm")


class TestOffWindows:
    @pytest.mark.skipif(sys.platform == "win32", reason="the off-Windows behaviour")
    @pytest.mark.parametrize(
        "call",
        [
            lambda: winplatform.plex_holds_db("/x/library.db"),
            lambda: winplatform.is_local_drive("/x"),
            lambda: winplatform.same_user_can_write("/x"),
            winplatform.current_user,
            winplatform.default_plex_config_dir,
        ],
        ids=["plex_holds_db", "is_local_drive", "same_user_can_write", "current_user", "default_plex_config_dir"],
    )
    def test_windows_calls_raise_elsewhere(self, call):
        with pytest.raises(winplatform.WindowsOnlyError):
            call()


@windows_only
class TestPlexHoldsDb:
    def test_another_process_holding_the_database_is_seen(self, tmp_path):
        db = _db(tmp_path)
        with _holding(db) as holder:
            assert holder.pid in winplatform.restart_manager_holders(f"{db}-shm")
            assert winplatform.dms_lock_held(f"{db}-shm") is True
            assert winplatform.plex_holds_db(db) is True

    def test_nobody_holds_it_once_the_holder_closed(self, tmp_path):
        db = _db(tmp_path)
        with _holding(db):
            pass
        assert winplatform.plex_holds_db(db) is False

    def test_a_shm_left_by_a_killed_holder_is_not_held(self, tmp_path):
        db = _db(tmp_path)
        _killed_holder_leaves_shm(db)
        assert [p for p in winplatform.restart_manager_holders(f"{db}-shm") if p != os.getpid()] == []
        assert winplatform.dms_lock_held(f"{db}-shm") is False
        assert winplatform.plex_holds_db(db) is False

    def test_our_own_connection_is_not_plex(self, tmp_path):
        # Locks are per handle on Windows, so the lock alone would take our own connection for Plex; Restart
        # Manager's list, without our own process id, is what tells them apart.
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        try:
            conn.execute("SELECT COUNT(*) FROM t").fetchall()
            assert os.getpid() in winplatform.restart_manager_holders(f"{db}-shm")
            assert winplatform.dms_lock_held(f"{db}-shm") is True
            assert winplatform.plex_holds_db(db) is False
        finally:
            conn.close()

    def test_a_rollback_journal_database_is_never_held(self, tmp_path):
        db = _db(tmp_path, journal="delete")
        with _holding(db):
            assert winplatform.plex_holds_db(db) is False

    def test_the_lock_decides_when_restart_manager_cannot_answer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(winplatform, "restart_manager_holders", lambda _path: None)
        db = _db(tmp_path)
        with _holding(db):
            assert winplatform.plex_holds_db(db) is True
        _killed_holder_leaves_shm(db)
        assert winplatform.plex_holds_db(db) is False

    def test_a_process_with_the_shm_open_but_no_lock_is_not_plex(self, tmp_path):
        # A virus scanner or backup tool reading the file: Restart Manager lists it, the lock says no connection.
        db = _db(tmp_path)
        _killed_holder_leaves_shm(db)
        reader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; f = open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read()",
                f"{db}-shm",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert reader.stdout.readline().strip() == "ready"
            assert reader.pid in winplatform.restart_manager_holders(f"{db}-shm")
            assert winplatform.plex_holds_db(db) is False
        finally:
            reader.stdin.close()
            reader.wait(timeout=10)


def _free_drive_letter() -> str:
    used = {d[0].upper() for d in os.listdrives()} if hasattr(os, "listdrives") else set()
    for letter in reversed(string.ascii_uppercase[3:]):
        if letter not in used and not os.path.exists(f"{letter}:\\"):
            return letter
    pytest.skip("no free drive letter")


@windows_only
class TestDrives:
    def test_the_system_disk_is_a_local_ntfs_drive(self):
        system = os.environ.get("SystemDrive", "C:") + "\\Windows"
        assert winplatform.drive_type(system) == winplatform.DRIVE_FIXED
        assert winplatform.is_local_drive(system) is True
        assert winplatform.filesystem_label(system) == "ntfs"

    def test_the_temp_folder_is_local(self, tmp_path):
        assert winplatform.is_local_drive(str(tmp_path)) is True

    def test_a_subst_drive_is_the_fixed_disk_under_it(self, tmp_path):
        letter = _free_drive_letter()
        subprocess.run(["subst", f"{letter}:", str(tmp_path)], check=True, capture_output=True)
        try:
            assert winplatform.drive_type(f"{letter}:\\") == winplatform.DRIVE_FIXED
            assert winplatform.is_local_drive(f"{letter}:\\library.db") is True
        finally:
            subprocess.run(["subst", f"{letter}:", "/D"], check=False, capture_output=True)

    def test_a_unc_path_is_a_network_drive(self, tmp_path):
        drive, rest = os.path.splitdrive(str(tmp_path))
        unc = rf"\\localhost\{drive[0]}${rest}"
        if not os.path.isdir(unc):
            pytest.skip("the administrative share isn't reachable here")
        assert winplatform.drive_type(unc) == winplatform.DRIVE_REMOTE
        assert winplatform.is_local_drive(unc) is False
        assert winplatform.filesystem_label(unc) == "network drive"

    def test_a_mapped_drive_is_a_network_drive(self, tmp_path):
        letter = _free_drive_letter()
        drive = os.path.splitdrive(str(tmp_path))[0]
        mapped = subprocess.run(["net", "use", f"{letter}:", rf"\\localhost\{drive[0]}$"], capture_output=True)
        if mapped.returncode:
            pytest.skip("can't map a network drive here")
        try:
            assert winplatform.drive_type(f"{letter}:\\") == winplatform.DRIVE_REMOTE
            assert winplatform.is_local_drive(f"{letter}:\\x\\library.db") is False
        finally:
            subprocess.run(["net", "use", f"{letter}:", "/delete", "/y"], check=False, capture_output=True)


@windows_only
class TestWritable:
    def test_a_file_and_folder_of_ours_are_writable(self, tmp_path):
        path = tmp_path / "library.db"
        path.write_bytes(b"")
        assert winplatform.same_user_can_write(str(path)) is True
        assert winplatform.same_user_can_write(str(tmp_path)) is True

    def test_a_read_only_file_is_not(self, tmp_path):
        path = tmp_path / "library.db"
        path.write_bytes(b"")
        path.chmod(stat.S_IREAD)
        try:
            assert winplatform.same_user_can_write(str(path)) is False
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_a_folder_denied_to_us_is_not(self, tmp_path):
        folder = tmp_path / "locked"
        folder.mkdir()
        user = os.environ["USERNAME"]
        subprocess.run(["icacls", str(folder), "/deny", f"{user}:(WD,AD)"], check=True, capture_output=True)
        try:
            assert winplatform.same_user_can_write(str(folder)) is False
        finally:
            subprocess.run(["icacls", str(folder), "/remove:d", user], check=True, capture_output=True)

    def test_a_database_held_open_by_another_process_is_still_writable(self, tmp_path):
        db = _db(tmp_path)
        with _holding(db):
            assert winplatform.same_user_can_write(db) is True
            assert winplatform.same_user_can_write(f"{db}-shm") is True
            assert winplatform.plex_holds_db(db) is True  # opening and closing it dropped none of the holder's locks

    def test_users_are_named(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"")
        assert os.environ["USERNAME"].lower() in winplatform.current_user().lower()
        owner = winplatform.file_owner(str(path))
        assert owner and "\\" in owner


@windows_only
class TestDefaultPlexFolder:
    @pytest.fixture
    def plex_key(self, monkeypatch):
        import winreg

        key = rf"Software\MediaPreviewGeneratorTests\{uuid.uuid4().hex}"
        monkeypatch.setattr(winplatform, "PLEX_REGISTRY_KEY", key)
        yield key
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)

    def test_local_app_data_by_default(self, tmp_path, monkeypatch, plex_key):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert winplatform.default_plex_config_dir() == str(tmp_path / "Plex Media Server")

    def test_plex_s_registry_value_moves_it(self, tmp_path, monkeypatch, plex_key):
        import winreg

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, plex_key) as key:
            winreg.SetValueEx(key, winplatform.PLEX_REGISTRY_VALUE, 0, winreg.REG_SZ, str(tmp_path / "PlexData"))
        assert winplatform.default_plex_config_dir() == str(tmp_path / "PlexData" / "Plex Media Server")
