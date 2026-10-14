"""Library Health's database counts through a Plex database node: the same fixed queries, run on Plex's machine.

The node's Plex folder holds a Plex-shaped WAL database (``tests/library_health/plex_fixture.py``); the app's own
Plex folder (a network share in real life) holds the BIFs it stats. Counting through the node must give exactly what
counting the same database locally gives.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.library_health import plex, plex_rows
from media_preview_generator.library_health.models import CellState, Feature
from media_preview_generator.markers.publishers import plex_remote
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
