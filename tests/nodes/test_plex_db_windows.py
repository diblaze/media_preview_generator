"""The Plex database node on Windows, run on any platform: Windows itself (the registry, the drive type) is faked.

Plex on Windows keeps its preferences -- its machine identifier among them -- in the registry
(``HKCU\\Software\\Plex, Inc.\\Plex Media Server``), never in a Preferences.xml, and names its files ``D:\\Media\\...``.
The Windows entry point (``nodes/windows/plex_db_node.py``) has no container ``/config``. ``windows-node.yml`` runs this
file on a real Windows machine too.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from media_preview_generator.loudness import plex_db as loudness_db
from media_preview_generator.loudness.guard import LoudnessPlexDb
from media_preview_generator.markers.publishers import plex_db, plex_remote
from media_preview_generator.markers.publishers.base import Capability
from media_preview_generator.markers.publishers.plex_chapters import ChapterError, LocalChapters
from media_preview_generator.nodes.server import plex_db as plex_db_role
from media_preview_generator.nodes.server import winplatform
from media_preview_generator.servers.ownership import apply_inverse_path_mappings
from tests.loudness.test_plex_db import BEFORE, FIELDS, FIX, PLEX_ANALYSED, PLEX_ITEM_TRIGGERS
from tests.markers.test_plex_db_publisher import _mountinfo
from tests.nodes.plex_db_harness import TOKEN, FlaskSession, node_app
from tests.nodes.test_plex_db_loudness import _extra, _remote
from tests.nodes.test_plex_db_role import _publisher_through

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE = "plex-a"
VERSION = "1.43.4.12345-abc123"
WINDOWS_FILE = r"D:\Media\TV\Show\S01E01.mkv"
AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its database open (the role's lock proof)."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


@pytest.fixture
def registry(monkeypatch, tmp_path) -> dict:
    """This process is the Windows node, beside a Plex that runs as this user with its data in %LOCALAPPDATA%.

    Returns:
        Plex's registry values, for a test to change.
    """
    values = {winplatform.PLEX_MACHINE_ID_VALUE: MACHINE}
    winreg = types.ModuleType("winreg")
    winreg.HKEY_CURRENT_USER = object()

    def open_key(_root, path):
        if path != winplatform.PLEX_REGISTRY_KEY:
            raise FileNotFoundError(path)
        return _Key()

    def query_value(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    winreg.OpenKey, winreg.QueryValueEx = open_key, query_value
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(winplatform, "IS_WINDOWS", True)
    monkeypatch.setattr(plex_db, "_PLEX_PREFS_IN_REGISTRY", True)
    monkeypatch.setattr(plex_db_role, "_ON_WINDOWS", True)
    monkeypatch.setattr(winplatform, "is_local_drive", lambda _p: True)
    monkeypatch.setattr(winplatform, "filesystem_label", lambda _p: "ntfs")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "node-config"))
    return values


@pytest.fixture
def folder(registry, tmp_path) -> Path:
    """Plex's Windows data folder: a 1.43 WAL database with one episode at a ``D:\\`` path, and no Preferences.xml."""
    folder = tmp_path / "AppData" / "Plex Media Server"
    path = Path(plex_db.plex_db_path(str(folder)))
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("BEGIN;\n" + (FIX / "plex_schema_1_43.sql").read_text() + PLEX_ITEM_TRIGGERS + "\nCOMMIT;")
    conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (563, '', 12)")
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (1, 4, 'Ep')")
    conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (1, 1, 424000)")
    conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (1, 1, ?)", (WINDOWS_FILE,))
    conn.execute(
        'INSERT INTO media_streams (id, stream_type_id, "index", codec, extra_data, media_part_id, media_item_id) '
        "VALUES (11, 2, 1, 'aac', ?, 1, 1)",
        (BEFORE,),
    )
    conn.commit()
    conn.close()
    (tmp_path / "node-config").mkdir()
    return folder


@pytest.fixture
def node(folder, tmp_path):
    return node_app(str(folder), mountinfo_path=_mountinfo(tmp_path)).test_client()


class TestTheNodeKnowsWhichPlexItIsBeside:
    def test_from_the_registry_on_every_answer_that_names_it(self, folder, node):
        assert not (folder / "Preferences.xml").exists()
        assert plex_db_role.machine_identifier(str(folder)) == MACHINE
        assert plex_db_role.info_block(str(folder))["machine_identifier"] == MACHINE
        assert node.get("/v1/ping", headers=AUTH).get_json()["result"]["machine_identifier"] == MACHINE
        checked = node.post("/v1/checks/file", json={"deadline_s": 5}, headers=AUTH).get_json()["result"]
        assert checked["machine_identifier"] == MACHINE

    def test_a_folder_that_isnt_this_users_plex_is_not_given_its_identity(self, folder, tmp_path):
        other = tmp_path / "Other" / "Plex Media Server"
        other.mkdir(parents=True)
        assert plex_db_role.machine_identifier(str(other)) == ""

    def test_plexs_moved_data_folder_is_still_its_own(self, folder, registry, tmp_path, monkeypatch):
        registry[winplatform.PLEX_REGISTRY_VALUE] = str(tmp_path / "AppData")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))
        assert plex_db_role.machine_identifier(str(folder)) == MACHINE


class TestEveryWriteRouteChecksThePlex:
    def test_loudness_is_ready_for_its_own_plex_and_refused_for_another(self, folder, tmp_path):
        def db(machine: str) -> LoudnessPlexDb:
            return LoudnessPlexDb(
                str(folder), label="w", identity=lambda: (machine, VERSION), mountinfo_path=_mountinfo(tmp_path)
            )

        assert db(MACHINE).capability(deadline=time.monotonic() + 10).state is Capability.READY
        refused = db("plex-b").capability(deadline=time.monotonic() + 10)
        assert refused.state is Capability.MISCONFIGURED and "Cannot prove" in refused.message

    def test_chapters_prove_their_own_plex_and_refuse_another(self, folder, tmp_path):
        chapters = LocalChapters(str(folder), mountinfo_path=_mountinfo(tmp_path))
        assert chapters.capability(MACHINE, VERSION, deadline=time.monotonic() + 10).state is Capability.READY
        with pytest.raises(ChapterError, match="Cannot prove"):
            chapters._identity("plex-b", VERSION)

    def test_markers_for_another_plex_are_refused_by_the_app(self, node):
        publisher = _publisher_through(FlaskSession(node), identity="plex-b")
        report = publisher.capability()
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.details["agent"]["wrong_plex"] is True
        assert _publisher_through(FlaskSession(node), identity=MACHINE).capability().state is Capability.READY

    def test_library_health_for_another_plex_is_refused(self, node):
        body = {"section_ids": [1], "deadline_s": 5}
        refused = node.post("/v1/library-health/rows", json={**body, "machine_identifier": "plex-b"}, headers=AUTH)
        assert refused.status_code == 409
        assert "different Plex" in refused.get_json()["error"]["message"]
        answered = node.post("/v1/library-health/rows", json={**body, "machine_identifier": MACHINE}, headers=AUTH)
        assert answered.status_code == 200


class TestAWindowsPlexsFilesAreFound:
    MAPPINGS = [{"plex_prefix": "D:\\Media", "local_prefix": "/data"}]

    def test_loudness_finds_and_writes_a_file_plex_names_with_backslashes(self, node, folder):
        db, _ = _remote(node)
        plex_paths = apply_inverse_path_mappings("/data/TV/Show/S01E01.mkv", self.MAPPINGS)
        streams, in_plex = loudness_db.read_streams(db, plex_paths, deadline=time.monotonic() + 10)
        assert in_plex and [s.id for s in streams] == [11]
        assert loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 10, expected=streams[0])
        assert _extra(folder, "media_streams", 11) == PLEX_ANALYSED


class TestLibraryHealthReadsOnlyALocalDrive:
    def test_a_network_drive_is_refused_like_on_every_other_route(self, folder, tmp_path, monkeypatch):
        monkeypatch.setattr(winplatform, "is_local_drive", lambda _p: False)
        monkeypatch.setattr(winplatform, "filesystem_label", lambda _p: "network drive")
        # Windows has no mountinfo: the drive type alone has to refuse it.
        node = node_app(str(folder), mountinfo_path=str(tmp_path / "no-proc" / "mountinfo")).test_client()
        response = node.post("/v1/library-health/rows", json={"section_ids": [1], "deadline_s": 5}, headers=AUTH)
        assert response.status_code == 409
        error = response.get_json()["error"]
        assert error["state"] == Capability.NEEDS_LOCAL_DB.value and "network share" in error["message"]

    def test_a_fixed_disk_is_read(self, folder, tmp_path):
        node = node_app(str(folder), mountinfo_path=str(tmp_path / "no-proc" / "mountinfo")).test_client()
        response = node.post("/v1/library-health/rows", json={"section_ids": [1], "deadline_s": 5}, headers=AUTH)
        assert response.status_code == 200


def _entry_point():
    spec = importlib.util.spec_from_file_location("plex_db_node", REPO_ROOT / "nodes" / "windows" / "plex_db_node.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheWindowsEntryPoint:
    @pytest.fixture
    def started(self, tmp_path, monkeypatch):
        """Run the entry point's main() up to serving; returns what the node app saw when it was created."""
        import media_preview_generator.nodes.server.app as node_app_module

        monkeypatch.delenv("CONFIG_DIR", raising=False)
        monkeypatch.setenv("NODE_KEY", "k" * 32)
        seen: dict = {}

        def create_node_app(**kwargs):
            seen["config_dir"] = kwargs["config_dir"]
            seen["undo_record"] = loudness_db.write_log_path()
            seen["undo_problem"] = plex_db_role.undo_record_problem()
            return object()

        class Server:
            def run(self):
                seen["served"] = True

        def create_server(app, **kwargs):
            seen["server_settings"] = kwargs
            return Server()

        waitress = types.ModuleType("waitress")
        waitress.create_server = create_server
        monkeypatch.setitem(sys.modules, "waitress", waitress)
        monkeypatch.setattr(node_app_module, "create_node_app", create_node_app)
        monkeypatch.setattr(plex_db_role, "_ON_WINDOWS", True)
        entry = _entry_point()
        monkeypatch.setattr(entry, "_log_to", lambda d: os.path.join(d, "node.log"))
        node_dir = tmp_path / "LocalAppData" / "MediaPreviewGenerator" / "node"
        plex_dir = tmp_path / "Plex Media Server"
        plex_dir.mkdir()
        assert entry.main(["--config-dir", str(node_dir), "--plex-config-dir", str(plex_dir)]) == 0
        return node_dir, seen

    def test_the_loudness_undo_record_lives_in_the_nodes_own_folder(self, started):
        node_dir, seen = started
        assert seen["config_dir"] == str(node_dir)
        assert os.path.dirname(seen["undo_record"]) == str(node_dir)
        assert seen["undo_problem"] == ""

    def test_it_serves_with_the_same_request_limits_as_the_docker_node(self, started):
        from media_preview_generator.nodes.server import wsgi

        _node_dir, seen = started
        settings = seen["server_settings"]
        assert seen["served"] is True
        assert settings["channel_timeout"] == wsgi.CHANNEL_TIMEOUT_S
        assert settings["max_request_body_size"] == wsgi.MAX_REQUEST_BODY_BYTES
        assert settings["connection_limit"] in (wsgi.CONNECTION_LIMIT, wsgi.SELECT_CONNECTION_LIMIT)
        assert settings["asyncore_use_poll"] is True and settings["ident"] == "mpg-plex-db-node"

    def test_a_windows_node_without_its_own_folder_writes_no_loudness(self, monkeypatch):
        monkeypatch.setattr(plex_db_role, "_ON_WINDOWS", True)
        monkeypatch.delenv("CONFIG_DIR", raising=False)
        assert "CONFIG_DIR" in plex_db_role.undo_record_problem()

    def test_log_sinks_never_print_a_tracebacks_variables(self, tmp_path, monkeypatch):
        entry = _entry_point()
        sinks: list[dict] = []
        monkeypatch.setattr(entry.logger, "remove", lambda *_a: None)
        monkeypatch.setattr(entry.logger, "add", lambda _sink, **kwargs: sinks.append(kwargs))
        monkeypatch.setattr(entry.logging, "basicConfig", lambda **_kw: None)
        entry._log_to(str(tmp_path))
        assert sinks and all(sink.get("diagnose") is False for sink in sinks)

    @pytest.mark.parametrize("installed_with_a_key", [False, True], ids=["key-it-made", "key-in-node-json"])
    def test_node_log_never_holds_the_key_it_announces(self, tmp_path, monkeypatch, installed_with_a_key):
        import secrets

        from loguru import logger

        from media_preview_generator.nodes.server import identity, nodelog

        monkeypatch.delenv("NODE_KEY", raising=False)
        monkeypatch.delenv("AGENT_TOKEN", raising=False)
        node_dir = tmp_path / "node"
        if installed_with_a_key:
            # A node.json someone (an older install.ps1) wrote with the key and announced still false.
            node_dir.mkdir()
            (node_dir / "node.json").write_text(
                json.dumps({"id": "a" * 32, "key": secrets.token_hex(24), "announced": False})
            )
        entry = _entry_point()
        assert entry.CONSOLE_ONLY == nodelog.SECRET_EXTRA
        monkeypatch.setattr(entry.logging, "basicConfig", lambda **_kw: None)
        try:
            log_file = entry._log_to(str(node_dir))
            me = identity.load_identity(str(node_dir))
            logger.info("an ordinary line")
            logger.complete()
        finally:
            logger.remove()
            logger.add(sys.stderr, level="WARNING")
        text = Path(log_file).read_text(encoding="utf-8")
        assert "an ordinary line" in text
        assert me.key not in text

    def test_undo_loudness_runs_the_undo_on_the_nodes_record_and_plexs_database(self, tmp_path, monkeypatch):
        from media_preview_generator.loudness import undo

        node_dir, plex_dir = tmp_path / "node", tmp_path / "Plex Media Server"
        node_dir.mkdir()
        (node_dir / "loudness-writes.jsonl").write_text("")
        calls: list[list[str]] = []
        monkeypatch.setattr(undo, "main", lambda argv: calls.append(argv) or 0)
        monkeypatch.delenv("CONFIG_DIR", raising=False)
        entry = _entry_point()
        argv = ["--config-dir", str(node_dir), "--plex-config-dir", str(plex_dir), "--undo-loudness", "--dry-run"]
        assert entry.main(argv) == 0
        assert calls == [
            [
                "--db",
                plex_db.plex_db_path(str(plex_dir)),
                "--log",
                str(node_dir / "loudness-writes.jsonl"),
                "--dry-run",
            ]
        ]

    def test_undo_loudness_without_a_record_says_so(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("CONFIG_DIR", raising=False)
        entry = _entry_point()
        argv = ["--config-dir", str(tmp_path / "node"), "--plex-config-dir", str(tmp_path), "--undo-loudness"]
        assert entry.main(argv) == 1
        assert "No loudness undo record" in capsys.readouterr().err


class TestTheInstaller:
    """install.ps1 can't run here; ``windows-node.yml`` runs it. These pin what it must never do again."""

    SCRIPT = (REPO_ROOT / "nodes" / "windows" / "install.ps1").read_text()

    def test_the_key_it_prints_is_kept_as_already_shown(self):
        # Kept as not shown, the node announces it again at its next start.
        assert "-NotePropertyName announced -NotePropertyValue $true" in self.SCRIPT

    def test_it_never_recreates_the_run_key(self):
        # New-Item -Force on an existing registry key deletes it: every other program's logon entry with it.
        assert "New-Item -Path $RunKey -Force" not in self.SCRIPT


def test_off_windows_the_preferences_file_beside_the_database_names_plex(tmp_path, monkeypatch):
    folder = tmp_path / "Plex Media Server"
    folder.mkdir()
    (folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{MACHINE}"/>')
    monkeypatch.setattr(plex_db, "_PLEX_PREFS_IN_REGISTRY", False)
    assert plex_db_role.machine_identifier(str(folder)) == MACHINE
    (folder / "Preferences.xml").write_text(json.dumps({"not": "xml"}))
    assert plex_db_role.machine_identifier(str(folder)) == ""
