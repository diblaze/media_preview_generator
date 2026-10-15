"""Library Health's database counts through a Plex database node: the same fixed queries, run on Plex's machine.

The node's Plex folder holds a Plex-shaped WAL database (``tests/library_health/plex_fixture.py``); the app's own
Plex folder (a network share in real life) holds the BIFs it stats. Counting through the node must give exactly what
counting the same database locally gives.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from media_preview_generator.library_health import plex, plex_rows
from media_preview_generator.library_health.models import CellState, Feature
from media_preview_generator.loudness.guard import RemoteLoudnessDb
from media_preview_generator.markers.publishers import plex_remote
from media_preview_generator.markers.publishers.base import Capability, DatabaseBusyError, PublishError
from media_preview_generator.markers.publishers.plex_chapters import CHAPTER_CAPABILITY
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb, plex_db_path
from media_preview_generator.markers.publishers.plex_remote import RemotePlexDb
from media_preview_generator.nodes.server import plex_db as plex_db_role
from tests.library_health.plex_fixture import SD_FLAG, build_plex_db, write_bif
from tests.library_health.test_plex import _FULL_LOUDNESS, _THUMB, _cell, _cfg, _episode, _lib, _movie, _with_chapters
from tests.markers.test_plex_db_publisher import _mountinfo, linux_mount_table
from tests.nodes.plex_db_harness import NODE_URL, TOKEN, FlaskSession, node_app

AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}

ITEMS = [
    _movie("A", "/m/a.mkv", "aa11", SD_FLAG, [_FULL_LOUDNESS], ["credits"]),
    _movie("B", "/m/b.mkv", "bb22", '{"ma:container":"mkv"}', [None]),
    _movie("C", "/m/c.mkv", None, None, [None]),
    _with_chapters(_movie("D", "/m/d.mkv", "dd44"), [(0, _THUMB), (60_000, "")]),
    _episode("E", "/tv/e.mkv", tags=["intro"], bundle_hash="ee55"),
]


@pytest.fixture
def node_folder(tmp_path) -> Path:
    """Plex's config folder as the node sees it, with the library's database (WAL, as Plex keeps it)."""
    folder = tmp_path / "node" / "Plex Media Server"
    path = Path(plex_db_path(str(folder)))
    path.parent.mkdir(parents=True)
    build_plex_db(path, ITEMS)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    return folder


@pytest.fixture
def cfg(tmp_path):
    write_bif(tmp_path / "plex", "aa11")
    write_bif(tmp_path / "plex", "bb22")
    config = _cfg(tmp_path, [_lib(), _lib("2", "Shows", "show")])
    config.output["chapter_thumbnails"] = True
    return config


def _count(cfg):
    return plex.count_plex(cfg, MagicMock(), nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None)


def _through_node(monkeypatch, node_folder, tmp_path, *, fs="ext4") -> FlaskSession:
    session = FlaskSession(node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path, fs=fs)).test_client())
    monkeypatch.setattr(plex, "_open_db", lambda _cfg: RemotePlexDb(NODE_URL, TOKEN, session=session))
    return session


class TestTheCountsAreTheLocalCounts:
    def test_every_cell_and_every_file_to_do_match_a_local_count(self, monkeypatch, node_folder, tmp_path, cfg):
        monkeypatch.setattr(plex, "_open_db", lambda _cfg: LocalPlexDb(lambda: plex_db_path(str(node_folder))))
        monkeypatch.setattr(plex, "filesystem_type", lambda _path: "ext4")
        local = _count(cfg)

        session = _through_node(monkeypatch, node_folder, tmp_path)
        remote = _count(cfg)

        assert local.access == plex.ACCESS_DIRECT and remote.access == plex.ACCESS_NODE
        assert remote.libraries == local.libraries
        assert remote.todo == local.todo
        assert session.paths == ["/v1/ping", "/v1/library-health/rows"]
        # And they are real counts, not two empty answers agreeing.
        previews = _cell(remote, "1", Feature.PREVIEWS)
        assert (previews.total, previews.done, previews.not_showing) == (4, 2, 1)
        assert _cell(remote, "1", Feature.LOUDNESS).done == 1
        assert _cell(remote, "1", Feature.CREDITS).done == 1
        assert _cell(remote, "2", Feature.INTRO).done == 1
        assert _cell(remote, "1", Feature.CHAPTERS).todo == 1

    def test_the_route_answers_in_the_queries_own_shape(self, node_folder, tmp_path):
        node = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        body = {"section_ids": [1], "want_markers": False, "want_chapters": False, "deadline_s": 5}
        result = node.post("/v1/library-health/rows", json=body, headers=AUTH).get_json()["result"]
        assert result["capability"] == plex_rows.LIBRARY_HEALTH_CAPABILITY
        conn = sqlite3.connect(plex_db_path(str(node_folder)))
        try:
            expected = plex_rows.read_rows(conn, [1], want_markers=False, want_chapters=False)
        finally:
            conn.close()
        assert result["rows"] == expected
        assert result["rows"]["markers"] == [] and result["rows"]["chapters"] == []
        assert len(result["rows"]["parts"]) == 4


class TestWhenTheNodeCantCount:
    def test_an_older_helper_counts_previews_through_the_api_and_says_to_update_it(
        self, monkeypatch, node_folder, tmp_path, cfg
    ):
        monkeypatch.setattr(plex_db_role, "CAPABILITIES", [CHAPTER_CAPABILITY])
        session = _through_node(monkeypatch, node_folder, tmp_path)
        server = MagicMock()
        server._connect.return_value.query.return_value = ET.fromstring("<MediaContainer/>")

        result = plex.count_plex(cfg, server, nothing_found={}, cancel_check=lambda: False, progress=lambda *_: None)

        assert session.paths == ["/v1/ping"]
        assert result.access == plex.ACCESS_API_ONLY
        loudness = _cell(result, "1", Feature.LOUDNESS)
        assert loudness.state == CellState.UNAVAILABLE and "1.2.0 or newer" in loudness.reason

    @linux_mount_table
    def test_a_database_on_a_network_share_on_the_node_is_an_error_naming_it(
        self, monkeypatch, node_folder, tmp_path, cfg
    ):
        _through_node(monkeypatch, node_folder, tmp_path, fs="nfs4")
        result = _count(cfg)
        previews = _cell(result, "1", Feature.PREVIEWS)
        assert previews.state == CellState.ERROR and "network share (nfs4)" in previews.reason

    def test_rows_in_another_shape_are_an_error_not_a_crash(self, monkeypatch, cfg):
        node = MagicMock(spec=RemotePlexDb)
        node.library_health_rows.return_value = {"parts": 5}
        monkeypatch.setattr(plex, "_open_db", lambda _cfg: node)
        result = _count(cfg)
        assert _cell(result, "1", Feature.PREVIEWS).state == CellState.ERROR


class TestADatabaseThatCantBeReadIsRefusedAsItIs:
    """Opening Plex's database read-only reads nothing, so a lock or damage shows at the first row. Read once the
    answer's 200 had gone out, either reached the app as "an answer this app can't read" instead of what it is: a
    busy database (tried again on the next run) or one that needs repairing."""

    BODY = {"section_ids": [1], "want_markers": True, "want_chapters": True}

    @pytest.fixture
    def journal_folder(self, tmp_path) -> Path:
        """A rollback-journal database: a writer holding it locks every reader out (a WAL one doesn't)."""
        folder = tmp_path / "journal" / "Plex Media Server"
        path = Path(plex_db_path(str(folder)))
        path.parent.mkdir(parents=True)
        build_plex_db(path, ITEMS)
        return folder

    def _rows(self, session: FlaskSession, wait_s: float = 5.0) -> dict:
        return RemotePlexDb(NODE_URL, TOKEN, session=session).library_health_rows(
            [1], want_markers=True, want_chapters=True, deadline=time.monotonic() + wait_s, needs="1.2.0"
        )

    def test_a_database_plex_is_writing_is_busy_and_lets_go_of_the_slot(self, journal_folder, tmp_path):
        from media_preview_generator.markers.publishers import plex_db
        from media_preview_generator.nodes.server import app as app_module

        app = node_app(str(journal_folder), mountinfo_path=_mountinfo(tmp_path))
        app.config[app_module.SERVER_THREADS] = 2  # one slot for the role
        session = FlaskSession(app.test_client())
        writer = sqlite3.connect(plex_db_path(str(journal_folder)), isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            with pytest.raises(DatabaseBusyError) as busy:
                self._rows(session, wait_s=0.3)
        finally:
            writer.close()

        assert busy.value.state == Capability.UNREACHABLE
        assert str(busy.value).startswith("Plex is busy writing its database; ")
        assert not plex_db._db_lock(plex_db_path(str(journal_folder))).locked()
        assert len(self._rows(session)["parts"]) == 4  # the slot was given back: the next run counts

    def test_a_database_another_node_task_holds_is_busy_without_naming_intro_and_credits(
        self, journal_folder, tmp_path
    ):
        from media_preview_generator.markers.publishers import plex_db

        db = plex_db_path(str(journal_folder))
        entered, release = threading.Event(), threading.Event()

        def loudness_write_running() -> None:
            # The node's own lock, held alone as its loudness or marker write holds it.
            with plex_db._holding_db_lock(db, time.monotonic() + 5):
                entered.set()
                release.wait(10)

        holder = threading.Thread(target=loudness_write_running)
        holder.start()
        assert entered.wait(5)
        try:
            with pytest.raises(DatabaseBusyError) as busy:
                self._rows(
                    FlaskSession(node_app(str(journal_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()),
                    wait_s=0.5,
                )
        finally:
            release.set()
            holder.join()
        assert str(busy.value) == "Another task is still using this Plex database; trying again on the next run"
        assert busy.value.state == Capability.UNREACHABLE

    @pytest.mark.parametrize(
        ("damage", "message"),
        [
            ("not-a-database", "Plex's database file can't be opened or written (file is not a database)"),
            (
                "malformed",
                # Library Health counts, it writes no markers: the words name none.
                "Database damaged: Plex's database needs repairing before this app can use it (database disk image "
                "is malformed)",
            ),
        ],
    )
    def test_a_damaged_database_is_refused_with_what_is_wrong(self, journal_folder, tmp_path, damage, message):
        path = Path(plex_db_path(str(journal_folder)))
        data = bytearray(path.read_bytes())
        if damage == "not-a-database":
            data[:16] = b"x" * 16  # SQLite's own header string
        else:
            data[100:400] = b"\xff" * 300  # the schema, on the first page after the file header
        path.write_bytes(bytes(data))
        node = node_app(str(journal_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()

        answer = node.post("/v1/library-health/rows", json={**self.BODY, "deadline_s": 5}, headers=AUTH)

        assert answer.status_code == 409
        assert answer.get_json()["error"] == {"kind": "publish", "message": message, "state": "misconfigured"}
        with pytest.raises(PublishError) as refused:
            self._rows(FlaskSession(node))
        assert type(refused.value) is PublishError
        assert (str(refused.value), refused.value.state) == (message, Capability.MISCONFIGURED)


class TestTheRouteRunsOnlyItsOwnQueries:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"section_ids": []},
            {"section_ids": "1"},
            {"section_ids": ["1"]},
            {"section_ids": [0]},
            {"section_ids": [True]},
            {"section_ids": list(range(1, 1002))},
            {"section_ids": [1], "want_markers": "yes"},
            {"section_ids": [1], "want_chapters": 1},
            {"section_ids": [1], "sql": "DELETE FROM media_parts"},
        ],
    )
    def test_a_body_that_isnt_what_it_should_be_is_refused(self, node_folder, tmp_path, body):
        node = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        response = node.post("/v1/library-health/rows", json=body, headers=AUTH)
        if "sql" in body:
            # Extra fields are ignored, never run: the answer is the fixed queries' rows.
            assert response.status_code == 200
            conn = sqlite3.connect(plex_db_path(str(node_folder)))
            try:
                assert conn.execute("SELECT COUNT(*) FROM media_parts").fetchone()[0] == 5
            finally:
                conn.close()
        else:
            assert response.status_code == 400


class TestTheNodeIsBesideThisServersPlex:
    """The app sends the server's ``machineIdentifier``; a node beside another Plex refuses to count."""

    @staticmethod
    def _claim(node_folder: Path, machine: str) -> None:
        (node_folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{machine}"/>')

    @pytest.mark.parametrize(
        ("node_machine", "asked", "status"),
        [
            ("plex-a", "plex-a", 200),
            ("plex-b", "plex-a", 409),
            ("", "plex-a", 200),
            ("plex-b", "", 200),
        ],
        ids=["same-plex", "another-plex", "node-unknown", "app-unknown"],
    )
    def test_the_route_compares_the_identifiers(self, node_folder, tmp_path, node_machine, asked, status):
        if node_machine:
            self._claim(node_folder, node_machine)
        node = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        body = {"section_ids": [1], "machine_identifier": asked, "deadline_s": 5}
        response = node.post("/v1/library-health/rows", json=body, headers=AUTH)
        assert response.status_code == status
        if status == 409:
            error = response.get_json()["error"]
            assert error["state"] == "misconfigured" and "beside a different Plex" in error["message"]
            assert "rows" not in (response.get_json().get("result") or {})

    def test_a_machine_identifier_that_isnt_text_is_refused(self, node_folder, tmp_path):
        node = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        body = {"section_ids": [1], "machine_identifier": 7}
        assert node.post("/v1/library-health/rows", json=body, headers=AUTH).status_code == 400

    @pytest.mark.parametrize(("server_identity", "counted"), [("plex-a", True), ("plex-b", False)])
    def test_the_app_sends_the_servers_identifier(self, monkeypatch, node_folder, tmp_path, server_identity, counted):
        self._claim(node_folder, "plex-a")
        session = _through_node(monkeypatch, node_folder, tmp_path)
        write_bif(tmp_path / "plex", "aa11")
        cfg = _cfg(tmp_path, [_lib()], server_identity=server_identity)

        result = _count(cfg)

        assert session.bodies["/v1/library-health/rows"]["machine_identifier"] == server_identity
        previews = _cell(result, "1", Feature.PREVIEWS)
        if counted:
            assert previews.state != CellState.ERROR
        else:
            assert previews.state == CellState.ERROR and "beside a different Plex" in previews.reason


class TestABigLibraryIsKeptCompressedUntilSent:
    """A big library's rows held at once, then their JSON, took a Plex database node past its memory limit (50,000
    episodes at 256 MB, 100,000 at 512 MB): it was killed, and the marker and loudness writes it was running failed.
    The node keeps the answer compressed instead, from the read until the app has it."""

    BODY = {"section_ids": [1], "want_markers": True, "want_chapters": True, "deadline_s": 5}

    @pytest.fixture
    def big_folder(self, tmp_path) -> Path:
        folder = tmp_path / "big" / "Plex Media Server"
        path = Path(plex_db_path(str(folder)))
        path.parent.mkdir(parents=True)
        extra = "x" * 600  # about the size of a real part's extra_data
        build_plex_db(path, [_movie(f"M{i}", f"/m/{i}.mkv", f"{i:040x}", extra, [None]) for i in range(6000)])
        return folder

    def test_the_rows_are_never_all_in_the_node_s_memory_as_they_are_sent(self, big_folder, tmp_path):
        node = node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()

        def send() -> int:
            answer = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH, buffered=False)
            received = sum(len(piece) for piece in answer.response)
            answer.close()
            return received

        send()  # untraced warm-up: imports and caches other tests may or may not have filled already
        tracemalloc.start()
        try:
            received = send()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert received > 4_000_000  # the parts' rows alone
        # The kept answer is compressed (about a quarter of its size); the rows all held at once would be at least
        # its whole size, so half leaves room for allocator noise without letting that through.
        assert peak < received / 2, f"the node held {peak} bytes to send {received}"

    def test_the_streamed_answer_is_the_rows_the_queries_read(self, big_folder, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 512)
        node = node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        result = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH).get_json()["result"]
        conn = sqlite3.connect(plex_db_path(str(big_folder)))
        try:
            expected = plex_rows.read_rows(conn, [1], want_markers=True, want_chapters=True)
        finally:
            conn.close()
        assert result == {"rows": expected, "capability": plex_rows.LIBRARY_HEALTH_CAPABILITY}

    @staticmethod
    def _failing_partway(monkeypatch, failure: Exception) -> None:
        """The node's ``audio_counts`` read stops with ``failure`` after 10 rows, long after the 200 went out."""
        queries = plex_db_role.query_rows

        def failing_partway(*args, **kwargs):
            rows = queries(*args, **kwargs)

            def audio_counts():
                yield from list(rows["audio_counts"])[:10]
                raise failure

            return {**rows, "audio_counts": audio_counts()}

        monkeypatch.setattr(plex_db_role, "query_rows", failing_partway)

    @staticmethod
    def _rows(big_folder, tmp_path) -> dict:
        session = FlaskSession(node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path)).test_client())
        return RemotePlexDb(NODE_URL, TOKEN, session=session).library_health_rows(
            [1], want_markers=True, want_chapters=True, deadline=time.monotonic() + 5, needs="1.2.0"
        )

    @pytest.mark.parametrize(
        ("failure", "raised", "message", "state"),
        [
            (
                sqlite3.OperationalError("database is locked"),
                DatabaseBusyError,
                "Plex is busy writing its database; ",
                Capability.UNREACHABLE,
            ),
            (
                sqlite3.DatabaseError("database disk image is malformed"),
                PublishError,
                "Database damaged: ",
                Capability.MISCONFIGURED,
            ),
            (
                sqlite3.OperationalError("disk I/O error"),
                PublishError,
                "Plex's database file can't be opened or written ",
                Capability.MISCONFIGURED,
            ),
            # Library Health counts, it writes no markers: the words name none.
            (
                sqlite3.OperationalError("no such table: media_streams"),
                PublishError,
                "Plex's database returned an unexpected error (",
                Capability.UNSUPPORTED_SCHEMA,
            ),
        ],
        ids=["busy", "damaged", "io-error", "unexpected"],
    )
    def test_a_read_that_stops_partway_is_the_database_s_refusal_not_a_smaller_library(
        self, big_folder, tmp_path, monkeypatch, failure, raised, message, state
    ):
        self._failing_partway(monkeypatch, failure)
        with pytest.raises(PublishError) as refused:
            self._rows(big_folder, tmp_path)
        assert type(refused.value) is raised
        assert str(refused.value).startswith(message) and str(failure) in str(refused.value)
        assert refused.value.state == state

    def test_rows_the_node_can_t_send_partway_are_an_answer_the_app_can_t_use_not_a_smaller_library(
        self, big_folder, tmp_path, monkeypatch
    ):
        self._failing_partway(monkeypatch, TypeError("Object of type bytes is not JSON serializable"))
        with pytest.raises(plex_remote.AgentError, match="can't read"):
            self._rows(big_folder, tmp_path)

    def test_spaces_keep_the_app_waiting_while_a_big_library_is_read_and_the_app_reads_past_them(
        self, big_folder, tmp_path, monkeypatch
    ):
        # The answer's first byte waits for the whole read: on a slow machine that outlasted the app's wait for it.
        monkeypatch.setattr(plex_db_role, "ROWS_KEEPALIVE_S", 0.0)
        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 512)
        node = node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        body = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH).get_data()
        assert body.startswith(b"    ") and body.lstrip(b" ").startswith(b'{"ok":true')

        rows = self._rows(big_folder, tmp_path)
        conn = sqlite3.connect(plex_db_path(str(big_folder)))
        try:
            assert rows == plex_rows.read_rows(conn, [1], want_markers=True, want_chapters=True)
        finally:
            conn.close()

    def test_the_node_stops_reading_past_what_the_app_reads_and_the_app_refuses_it_as_too_large(
        self, big_folder, tmp_path, monkeypatch
    ):
        from media_preview_generator.markers.publishers import plex_db

        monkeypatch.setattr(plex_remote, "MAX_ANSWER_BYTES", 100_000)
        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 512)
        node = node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        sent = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH).get_data()
        assert 100_000 < len(sent) < 100_000 + 2 * 512 + 1000  # one piece past the cap, of a 4 MB library
        assert not plex_db._db_lock(plex_db_path(str(big_folder))).locked()

        with pytest.raises(plex_remote.AgentError, match="too large"):
            self._rows(big_folder, tmp_path)

    @pytest.mark.parametrize("pieces_read", [0, 1], ids=["before-the-first-byte", "while-the-rows-are-read"])
    def test_an_answer_the_app_hung_up_on_lets_go_of_the_database_and_its_slot(
        self, big_folder, tmp_path, monkeypatch, pieces_read
    ):
        from media_preview_generator.markers.publishers import plex_db
        from media_preview_generator.nodes.server import app as app_module

        monkeypatch.setattr(plex_db_role, "ROWS_KEEPALIVE_S", 0.0)  # a space after the first piece read
        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 512)
        app = node_app(str(big_folder), mountinfo_path=_mountinfo(tmp_path))
        app.config[app_module.SERVER_THREADS] = 2  # one slot for the role
        node = app.test_client()
        abandoned = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH, buffered=False)
        pieces = iter(abandoned.response)
        assert [next(pieces) for _ in range(pieces_read)] == [b" "] * pieces_read
        abandoned.close()

        assert not plex_db._db_lock(plex_db_path(str(big_folder))).locked()
        again = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH)
        assert again.status_code == 200
        assert again.get_json()["result"]["capability"] == plex_rows.LIBRARY_HEALTH_CAPABILITY


class TestAnAnswerTheAppIsSlowToReadHoldsNothingOfTheNodes:
    """Sent as it was read, Library Health's answer held the node's lock on Plex's database, its read transaction and
    the request's slot until the app had read all but its last 16 MB (waitress stops the sending thread there). An
    app that read slowly, or stopped without hanging up (its machine dropped off the network), left every file check
    and marker or loudness write on the node waiting ("Another task is still using this Plex database") and Plex's WAL unable to checkpoint, for as long as that lasted."""

    BODY = {"section_ids": [1], "want_markers": True, "want_chapters": True, "deadline_s": 5}

    @pytest.fixture(autouse=True)
    def plex_holds_the_database(self, monkeypatch):
        """The real lock probe runs, taking the node's lock; only its answer (Plex has the file open) is stubbed."""
        from media_preview_generator.markers.publishers import plex_db

        monkeypatch.setattr(plex_db, "_shm_dms_locked_elsewhere", lambda _db: True)

    @staticmethod
    def _ask(app, asked: str):
        remote = RemotePlexDb(NODE_URL, TOKEN, session=FlaskSession(app.test_client()))
        deadline = time.monotonic() + 3
        if asked == "file check":
            return remote.file_checks(deadline=deadline).state
        if asked == "loudness file check":
            return RemoteLoudnessDb(MagicMock(), remote, server_name="Plex").file_checks(deadline=deadline).state
        rows = remote.library_health_rows([1], want_markers=True, want_chapters=True, deadline=deadline, needs="1.2.0")
        return len(rows["parts"])

    @pytest.mark.parametrize(
        ("asked", "answered"),
        [("file check", Capability.READY), ("loudness file check", Capability.READY), ("Library Health", 4)],
    )
    def test_the_node_answers_while_an_answer_waits_for_the_app(
        self, monkeypatch, node_folder, tmp_path, asked, answered
    ):
        from media_preview_generator.nodes.server import app as app_module

        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 1)  # a piece per row: the app stops mid-read
        app = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path))
        app.config[app_module.SERVER_THREADS] = 2  # one slot for the role
        waiting = app.test_client().post("/v1/library-health/rows", json=self.BODY, headers=AUTH, buffered=False)
        try:
            next(iter(waiting.response))  # the app read the first piece, then stopped
            # Asked on another of the node's threads, as the server would.
            with ThreadPoolExecutor(1) as another_thread:
                assert another_thread.submit(self._ask, app, asked).result(timeout=30) == answered
        finally:
            waiting.close()

    def test_plex_checkpoints_its_wal_while_an_answer_waits_for_the_app(self, monkeypatch, node_folder, tmp_path):
        monkeypatch.setattr(plex_db_role, "ROWS_CHUNK_CHARS", 1)
        node = node_app(str(node_folder), mountinfo_path=_mountinfo(tmp_path)).test_client()
        waiting = node.post("/v1/library-health/rows", json=self.BODY, headers=AUTH, buffered=False)
        plex = sqlite3.connect(plex_db_path(str(node_folder)), timeout=0)
        try:
            next(iter(waiting.response))
            with plex:
                plex.execute("UPDATE metadata_items SET title = title || ' (2)'")
            busy, _frames, _done = plex.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            plex.close()
            waiting.close()
        assert busy == 0

    @staticmethod
    def _served(tmp_path: Path, movies: int, extra_chars: int) -> Any:
        """A node on waitress, as both entry points serve it, beside a library of ``movies`` films."""
        from media_preview_generator.nodes.server import wsgi

        folder = tmp_path / "served" / "Plex Media Server"
        path = Path(plex_db_path(str(folder)))
        path.parent.mkdir(parents=True)
        extra = "x" * extra_chars
        build_plex_db(path, [_movie(f"M{i}", f"/m/{i}.mkv", f"{i:040x}", extra, [None]) for i in range(movies)])
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        server = wsgi.create_node_server(
            node_app(str(folder), mountinfo_path=_mountinfo(tmp_path)), host="127.0.0.1", port=0, threads=4
        )
        threading.Thread(target=server.run, daemon=True).start()
        return server

    def _asked_over_http(self, port: int) -> socket.socket:
        """A connection that has sent the Library Health request, reading its answer through a 64 KB buffer."""
        body = json.dumps(self.BODY).encode()
        head = "\r\n".join(
            [
                "POST /v1/library-health/rows HTTP/1.1",
                f"Host: 127.0.0.1:{port}",
                *(f"{name}: {value}" for name, value in AUTH.items()),
                "Content-Type: application/json",
                f"Content-Length: {len(body)}",
                "Connection: close",
            ]
        )
        app = socket.create_connection(("127.0.0.1", port))
        app.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        app.sendall(head.encode() + b"\r\n\r\n" + body)
        return app

    def test_an_app_that_stopped_reading_a_big_answer_over_http_leaves_the_database_free(self, tmp_path):
        # Bigger than waitress's 16 MB of unsent answer plus the sockets' buffers, so its sending thread stops.
        server = self._served(tmp_path, 8000, 4000)
        port = server.effective_port
        stalled = self._asked_over_http(port)
        try:
            received = stalled.recv(64 * 1024)
            assert received.startswith(b"HTTP/1.1 200")
            # The app stops reading here without hanging up; a Setup Health or job check of the node follows.
            report = RemotePlexDb(f"http://127.0.0.1:{port}", TOKEN).file_checks(deadline=time.monotonic() + 5)
            # Then it reads again, at full speed.
            stalled.settimeout(30)
            stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            while piece := stalled.recv(1024 * 1024):
                received += piece
        finally:
            stalled.close()
            server.close()

        assert report.state is Capability.READY, report.message
        # And the answer that waited is whole once the app reads again: the capability comes last.
        assert len(received) > 32_000_000
        assert f'"capability":"{plex_rows.LIBRARY_HEALTH_CAPABILITY}"}}}}'.encode() in received[-200:]

    def test_an_app_reading_slowly_gets_the_whole_answer_from_a_node_without_a_writable_temp_folder(
        self, tmp_path, monkeypatch
    ):
        # waitress moved the part of an answer the app hadn't read yet past 1 MB to a temp file: on a node run with
        # --read-only there is none, so the answer stopped partway and Library Health never counted the library.
        read_only = tmp_path / "read-only-tmp"
        read_only.mkdir()
        read_only.chmod(0o500)  # what --read-only makes of /tmp, and of every other folder tempfile tries
        monkeypatch.setattr(tempfile, "tempdir", str(read_only))
        server = self._served(tmp_path, 12000, 600)  # a ~9 MB answer
        app = self._asked_over_http(server.effective_port)
        received = b""
        try:
            app.settimeout(15)
            with contextlib.suppress(TimeoutError):
                while piece := app.recv(64 * 1024):
                    received += piece
                    time.sleep(0.02)  # an app on another machine reads slower than the node sends
        finally:
            app.close()
            server.close()
            read_only.chmod(0o700)
        assert received.startswith(b"HTTP/1.1 200")
        # The capability comes last (then the end of waitress's chunked encoding).
        assert f'"capability":"{plex_rows.LIBRARY_HEALTH_CAPABILITY}"}}}}'.encode() in received[-200:], (
            f"the answer stopped after {len(received):,} bytes"
        )
