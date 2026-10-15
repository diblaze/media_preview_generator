"""The Plex database role (the Plex marker agent, folded into the node server): its guards, its refusals, and the app
writing Plex through it.

The role is mounted on a bare Flask app here: the node server (``nodes.server.app``) owns ``/v1/health`` and the
role's routes keep their own guard, so a stand-in health route proves the two don't leak into each other.
"""

from __future__ import annotations

import re
import sqlite3
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_db, plex_remote
from media_preview_generator.markers.publishers.base import Capability, PublishError, Shown
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.markers.publishers.plex_remote import RemotePlexDb
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.nodes.server import plex_db as plex_db_role
from media_preview_generator.nodes.server import winplatform
from media_preview_generator.servers.base import ServerConfig, ServerType
from tests.markers.test_plex_db_publisher import _make_db, _mountinfo, linux_mount_table, windows_only
from tests.nodes.plex_db_harness import TOKEN, FlaskSession, node_app

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "nodes" / "plex-db"

T = MarkerType
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
LOCKED_INTRO = Marker(T.INTRO, 20_000, 44_000, ("user",), locked=True)
CREDITS = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}


def _requirement_lines(filename: str) -> list[str]:
    """The meaningful lines of a requirements file, with pip's trailing-backslash wrapping undone."""
    text = (AGENT_DIR / filename).read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _direct_requirements() -> list[tuple[str, str]]:
    """The agent's hand-written input requirements, as ``(canonical name, version specifier)`` pairs."""
    parsed = [Requirement(line) for line in _requirement_lines("requirements.txt")]
    return [(canonicalize_name(req.name), str(req.specifier)) for req in parsed]


def _lock_entries() -> dict[str, str]:
    """Each whole ``name==version --hash=sha256:... --hash=...`` block of the lock, keyed by canonical name."""
    entries: dict[str, str] = {}
    for line in _requirement_lines("requirements.lock.txt"):
        pinned = Requirement(line.split(" ", 1)[0])
        entries[canonicalize_name(pinned.name)] = line
    return entries


def _locked_versions() -> dict[str, str]:
    """The exact version the lock pins for each distribution."""
    versions: dict[str, str] = {}
    for name, entry in _lock_entries().items():
        specifier = Requirement(entry.split(" ", 1)[0]).specifier
        versions[name] = next(iter(specifier)).version
    return versions


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its database open through this very path (the agent's lock proof)."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


@pytest.fixture
def plex_config(tmp_path):
    """A Plex config folder with a 1.43 database in it, as the agent's container would see it."""
    folder = tmp_path / "Plex Media Server"
    _make_db(folder, journal_mode="wal")
    return folder


@pytest.fixture
def agent(plex_config, tmp_path):
    """The role, pointed at that folder, with everything on a local disk."""
    return node_app(str(plex_config), mountinfo_path=_mountinfo(tmp_path)).test_client()


def _db_file(plex_config: Path) -> Path:
    return Path(plex_db.plex_db_path(str(plex_config)))


def _served(plex_config: Path, item: int = 7) -> list[tuple]:
    """What Plex would serve for the item: its marker rows, in stored times."""
    conn = sqlite3.connect(_db_file(plex_config))
    try:
        return conn.execute(
            "SELECT t.text, t.time_offset, t.end_time_offset FROM taggings t JOIN tags g ON g.id = t.tag_id "
            "WHERE t.metadata_item_id=? AND g.tag_type=12 AND g.tag='' ORDER BY t.text, t.time_offset",
            (item,),
        ).fetchall()
    finally:
        conn.close()


class TestItStartsOnlyWhenItIsSetUp:
    def test_no_shared_key_refuses_to_start(self, plex_config):
        with pytest.raises(SystemExit, match="AGENT_TOKEN"):
            node_app(str(plex_config), key="")

    def test_no_plex_config_folder_refuses_to_start(self):
        with pytest.raises(SystemExit, match="PLEX_CONFIG_DIR"):
            node_app("", key=TOKEN)

    def test_it_leaves_the_health_route_to_the_node_server(self, plex_config):
        app = Flask(__name__)
        plex_db_role.register_plex_db(app, plex_config_dir=str(plex_config), key=TOKEN)
        routes = {rule.rule for rule in app.url_map.iter_rules()}
        assert "/v1/health" not in routes
        assert {"/v1/ping", "/v1/item/write", "/v1/loudness/<operation>", "/v1/library-health/rows"} <= routes


class TestTheGuards:
    """Nothing reaches the database without the key and a protocol both sides know."""

    ENDPOINTS = [
        ("post", "/v1/checks/file", {}),
        ("post", "/v1/checks/db", {}),
        ("post", "/v1/item/read", {"rating_key": 7}),
        ("post", "/v1/item/write", {"write": {}}),
        ("post", "/v1/items/shown", {"items": []}),
        ("post", "/v1/item/exists", {"rating_key": 7}),
        ("post", "/v1/chapters/check", {}),
        ("post", "/v1/loudness/check", {}),
        ("post", "/v1/library-health/rows", {"section_ids": [1]}),
        ("get", "/v1/ping", None),
    ]

    @pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer wrong-key"}, {"Authorization": TOKEN}, {"X-Auth-Token": TOKEN}],
        ids=["no-key", "wrong-key", "no-bearer-prefix", "another-header"],
    )
    def test_every_endpoint_needs_the_shared_key(self, agent, method, path, body, headers):
        send = getattr(agent, method)
        response = send(path, json=body, headers={**headers, plex_remote.PROTOCOL_HEADER: "1"})
        assert response.status_code == 401
        assert response.get_json()["error"] == {"kind": "auth"}

    @pytest.mark.parametrize(
        "offered",
        ["kéy", "ÿ" * 8, "key—dash", "\U0001f511"],
        ids=["latin-1-letter", "high-latin-1-bytes", "em-dash", "astral"],
    )
    def test_a_non_ascii_key_is_refused_rather_than_crashing(self, agent, offered):
        """werkzeug decodes headers latin-1 and ``compare_digest`` raises TypeError on a str above U+007F.

        Our own key is ASCII-validated before it is stored (``markers/settings.py``), so only a third party
        sends one of these -- and a 500 on an auth path tells that third party the endpoint exists.
        """
        response = agent.post(
            "/v1/checks/file",
            json={},
            headers={"Authorization": f"Bearer {offered}", plex_remote.PROTOCOL_HEADER: "1"},
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == {"kind": "auth"}

    @pytest.mark.parametrize("token", ["kéy", "—", "k\U0001f511"], ids=["accent", "em-dash", "astral"])
    def test_an_agent_whose_own_key_is_non_ascii_refuses_to_start(self, tmp_path, token):
        """The other half of the pair. The app stores printable ASCII only (``markers/settings.py``), so such a
        key could never match one it sends: every request would 401 and look like a typo on the app's side."""
        with pytest.raises(SystemExit, match="ASCII"):
            node_app(str(tmp_path), key=token)

    def test_a_key_with_a_space_refuses_to_start(self, tmp_path):
        with pytest.raises(SystemExit, match="no spaces"):
            node_app(str(tmp_path), key="two words")

    def test_the_role_without_a_key_says_how_to_get_one(self, tmp_path):
        """The node server always passes NODE_KEY, AGENT_TOKEN or its generated key; the message says so instead of
        claiming the role needs AGENT_TOKEN."""
        from flask import Flask

        with pytest.raises(SystemExit) as caught:
            plex_db_role.register_plex_db(Flask(__name__), plex_config_dir=str(tmp_path), key="")
        message = str(caught.value)
        assert "generates a key and prints it once" in message
        assert "won't start without" not in message

    def test_the_node_server_starts_the_role_with_its_generated_key(self, tmp_path, monkeypatch):
        from media_preview_generator.nodes.server.app import create_node_app

        monkeypatch.delenv("NODE_KEY", raising=False)
        monkeypatch.delenv("AGENT_TOKEN", raising=False)
        monkeypatch.setenv("PLEX_CONFIG_DIR", str(tmp_path / "plex"))
        app = create_node_app(config_dir=str(tmp_path / "node"), roles=["plex_db"])
        assert "/v1/ping" in {rule.rule for rule in app.url_map.iter_rules()}

    def test_an_address_that_sent_too_many_wrong_keys_is_asked_to_wait_even_with_the_right_one(self, agent):
        from media_preview_generator.nodes.server import identity

        wrong = {"Authorization": "Bearer nope", plex_remote.PROTOCOL_HEADER: "1"}
        right = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: "1"}
        guesser = {"REMOTE_ADDR": "10.0.0.66"}
        for _ in range(identity.WRONG_KEY_BURST):
            assert agent.get("/v1/ping", headers=wrong, environ_base=guesser).status_code == 401
        waited = agent.get("/v1/ping", headers=right, environ_base=guesser)
        assert waited.status_code == 429 and waited.headers["Retry-After"].isdigit()
        assert waited.get_json()["error"] == {"kind": "rate_limited"}
        assert agent.get("/v1/ping", headers=right, environ_base={"REMOTE_ADDR": "10.0.0.5"}).status_code == 200

    def test_a_wrong_key_says_nothing_about_this_plex(self, agent):
        body = agent.post("/v1/checks/file", json={}, headers={"Authorization": "Bearer nope"}).get_data(as_text=True)
        assert "Plex Media Server" not in body and "db_path" not in body

    @pytest.mark.parametrize(
        "asked",
        [None, "2", "0", "one", "", "¹", "1" * 5000],
        ids=["none", "newer", "older", "text", "empty", "superscript", "past-the-int-digit-limit"],
    )
    def test_a_protocol_this_build_doesnt_speak_is_refused_with_both_versions(self, agent, asked):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if asked is not None:
            headers[plex_remote.PROTOCOL_HEADER] = asked
        response = agent.post("/v1/item/read", json={"rating_key": 7}, headers=headers)
        assert response.status_code == 409
        message = response.get_json()["error"]["message"]
        assert plex_db_role.AGENT_VERSION in message and "protocol 1" in message

    def test_a_deeply_nested_body_is_refused_as_malformed(self, agent):
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            plex_remote.PROTOCOL_HEADER: "1",
            "Content-Type": "application/json",
        }
        response = agent.post("/v1/item/read", data="[" * 100_000 + "]" * 100_000, headers=headers)
        assert response.status_code == 400

    @pytest.mark.parametrize("asked", [None, "99"], ids=["no-protocol-header", "another-protocol"])
    def test_the_ping_still_answers_a_caller_whose_protocol_it_doesnt_speak(self, agent, asked):
        # The README's curl command sends the key and nothing else: when the two sides disagree, "which version are
        # you?" is the question the operator needs answered.
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if asked is not None:
            headers[plex_remote.PROTOCOL_HEADER] = asked
        response = agent.get("/v1/ping", headers=headers)
        assert response.status_code == 200
        assert response.get_json()["agent"]["version"] == plex_db_role.AGENT_VERSION

    def test_the_ping_still_needs_the_key(self, agent):
        assert agent.get("/v1/ping", headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_the_roles_guard_doesnt_reach_the_node_servers_own_routes(self, agent):
        # The stand-in /v1/health has no key and no protocol header, as the container's HEALTHCHECK calls it.
        payload = agent.get("/v1/health").get_json()
        assert payload["ok"] is True
        assert payload["agent"] == plex_db_role.agent_block()
        assert "db" not in str(payload)

    def test_every_answer_carries_the_agents_version_so_skew_is_seen_on_the_first_call(self, agent):
        for response in (agent.get("/v1/ping", headers=AUTH), agent.post("/v1/checks/file", json={}, headers=AUTH)):
            assert response.get_json()["agent"] == plex_db_role.agent_block()

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/v1/item/read", {"rating_key": "7"}),
            ("/v1/item/read", {"rating_key": True}),
            ("/v1/item/exists", {}),
            ("/v1/item/write", {"write": {"rating_key": 7}}),
            ("/v1/items/shown", {"items": "all"}),
            ("/v1/items/shown", {"items": [{"rating_key": 7}]}),
            ("/v1/items/shown", {"items": [], "timeout_s": "soon"}),
        ],
    )
    def test_a_body_that_isnt_what_it_should_be_is_refused(self, agent, path, body):
        assert agent.post(path, json=body, headers=AUTH).status_code == 400

    def test_a_body_that_isnt_json_is_refused(self, agent):
        assert agent.post("/v1/item/read", data="[]", content_type="application/json", headers=AUTH).status_code == 400

    def test_a_huge_body_is_refused_before_it_is_parsed(self, agent):
        big = {"items": [{"rating_key": 7, "ours": [], "item_files": ["x" * 4096] * 1000}]}
        assert agent.post("/v1/items/shown", json=big, headers=AUTH).status_code in (400, 413)

    def test_more_items_than_one_read_back_may_carry_is_refused(self, agent):
        items = [{"rating_key": i, "ours": [], "kept_types": [], "item_files": None} for i in range(201)]
        assert agent.post("/v1/items/shown", json={"items": items}, headers=AUTH).status_code == 400


class TestItOwnsThePath:
    """The caller says what to write, never where."""

    def test_the_database_path_comes_from_the_container_not_the_request(self, agent, plex_config, tmp_path):
        elsewhere = tmp_path / "somebody-elses"
        _make_db(elsewhere, journal_mode="wal")
        body = {
            "rating_key": 7,
            "db_path": str(_db_file(elsewhere)),
            "config_dir": str(elsewhere),
            "plex_config_folder": str(elsewhere),
        }
        assert agent.post("/v1/item/read", json=body, headers=AUTH).status_code == 200
        # Proven by writing: the row lands in the agent's own database, never in the one the body named.
        write = _write_body(rating_key=7, wanted=[INTRO])
        assert agent.post("/v1/item/write", json={**write, **body}, headers=AUTH).status_code == 200
        assert _served(plex_config) == [("intro", 11_000, 37_000)]
        assert _served(elsewhere) == []

    def test_the_ping_says_whether_the_database_is_even_there(self, agent, plex_config):
        assert agent.get("/v1/ping", headers=AUTH).get_json()["result"]["db_present"] is True
        _db_file(plex_config).unlink()
        assert agent.get("/v1/ping", headers=AUTH).get_json()["result"]["db_present"] is False


def _write_body(*, rating_key: int, wanted: list[Marker], prior=(), keep_plex=False, kept_types=()) -> dict:
    from media_preview_generator.markers.publishers.plex_db import Part, WriteRequest

    request = WriteRequest(
        rating_key=rating_key,
        parts=[Part(1, 1, "/data/tv/S01E01.mkv", None, None)],
        wanted=list(wanted),
        prior=list(prior),
        duration_ms=DUR,
        own_prior=[],
        calling_part_ids=(1,),
        kept_types=frozenset(kept_types),
        keep_plex=keep_plex,
    )
    return {"deadline_s": 5.0, "write": plex_remote.write_request_to_json(request)}


class TestItRefusesWhatTheInProcessWriterRefuses:
    """The same guards, because it is the same code — never a second implementation."""

    @linux_mount_table
    def test_a_database_on_a_network_share_is_refused_here_too(self, plex_config, tmp_path):
        app = node_app(str(plex_config), mountinfo_path=_mountinfo(tmp_path, fs="nfs4"))
        report = app.test_client().post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["report"]
        assert report["state"] == Capability.NEEDS_LOCAL_DB.value
        assert "network share" in report["message"]

    @windows_only
    def test_a_database_on_a_network_drive_is_refused_here_too(self, plex_config, monkeypatch):
        monkeypatch.setattr(winplatform, "drive_type", lambda _path: winplatform.DRIVE_REMOTE)
        app = node_app(str(plex_config))
        report = app.test_client().post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["report"]
        assert report["state"] == Capability.NEEDS_LOCAL_DB.value
        assert "network share (network drive)" in report["message"]

    def test_plex_not_holding_the_database_is_refused_here_too(self, agent, monkeypatch):
        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        report = agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["report"]
        assert report["state"] == Capability.UNREACHABLE.value

    def test_a_schema_it_doesnt_know_stops_the_write(self, agent, plex_config):
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute("CREATE TRIGGER t AFTER INSERT ON taggings BEGIN SELECT 1; END")
        conn.commit()
        conn.close()
        response = agent.post("/v1/item/write", json=_write_body(rating_key=7, wanted=[INTRO]), headers=AUTH)
        assert response.status_code == 409
        error = response.get_json()["error"]
        assert error["state"] == Capability.UNSUPPORTED_SCHEMA.value and "triggers" in error["message"]
        assert _served(plex_config) == []

    def test_a_plex_that_never_made_its_marker_tag_row_is_refused_and_none_is_created(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, tag_row=None, journal_mode="wal")
        app = node_app(str(folder), mountinfo_path=_mountinfo(tmp_path))
        response = app.test_client().post(
            "/v1/item/write", json=_write_body(rating_key=7, wanted=[INTRO]), headers=AUTH
        )
        assert response.status_code == 409
        assert response.get_json()["error"]["state"] == Capability.NEEDS_PLEX_DETECTION_ONCE.value
        conn = sqlite3.connect(Path(plex_db.plex_db_path(str(folder))))
        try:
            assert conn.execute("SELECT COUNT(*) FROM tags WHERE tag_type=12").fetchone()[0] == 0
        finally:
            conn.close()

    def test_an_item_this_plex_doesnt_have_is_an_item_not_found(self, agent):
        response = agent.post("/v1/item/read", json={"rating_key": 999}, headers=AUTH)
        assert response.status_code == 200 and response.get_json()["result"]["item"]["exists"] is False


def _publisher_through(session, *, keep_plex=False, identity=None) -> PlexMarkerPublisher:
    """The app's Plex publisher with no access to the database at all — only the agent."""
    config = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex:32400",
        auth={},
        # Deliberately a folder that doesn't exist here: this app cannot see Plex's database.
        output={"plex_config_folder": "/nowhere/plex"},
        path_mappings=[],
        server_identity=identity,
    )
    settings = ServerMarkersSettings(
        True,
        None,
        "2026-09-13T00:00:00+00:00",
        "keep_plex" if keep_plex else "restore",
        agent_enabled=True,
        agent_url="http://plex-host.lan:9494",
        agent_token=TOKEN,
    )
    server = MagicMock()
    server.get_server_status.return_value = {"plex_pass": True, "version": "1.43.4"}
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    return PlexMarkerPublisher(
        server, config, settings, db=RemotePlexDb(settings.agent_url, settings.agent_token, session=session)
    )


class TestTheAppWritesPlexThroughTheAgent:
    """The whole path, both halves: nothing here opens the database except the agent."""

    def test_markers_are_written_read_back_and_cleaned_up(self, agent, plex_config):
        session = FlaskSession(agent)
        publisher = _publisher_through(session)

        ours = publisher.write(
            "7", [INTRO, CREDITS], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )

        assert ours == [INTRO, CREDITS]
        assert publisher.last_write_changed is True
        assert _served(plex_config) == [("credits", 1_297_000, DUR), ("intro", 11_000, 37_000)]
        assert publisher.shows_many([("7", ours, frozenset(), publisher.last_item_files)]) == {"7": Shown.OURS}

        # Nothing decided any more: the rows this app left go, and nothing else is touched.
        assert publisher.write("7", [], previous=ours, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv") == []
        assert _served(plex_config) == []

    def test_the_second_run_changes_nothing(self, agent, plex_config):
        publisher = _publisher_through(FlaskSession(agent))
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        again = _publisher_through(FlaskSession(agent))
        again.write("7", [INTRO], previous=[INTRO], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert again.last_write_changed is False
        assert _served(plex_config) == [("intro", 11_000, 37_000)]

    def test_a_locked_marker_overrides_keep_plexs(self, agent, plex_config):
        # Plex's own intro row is there and the server is set to keep Plex's; a marker the user locked still wins.
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (7, 563, 0, 'intro', 990, 29306, '', 1, '{\"pv:version\":\"5\"}')"
        )
        conn.commit()
        conn.close()

        publisher = _publisher_through(FlaskSession(agent), keep_plex=True)
        ours = publisher.write(
            "7", [LOCKED_INTRO], previous=None, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )

        assert ours == [LOCKED_INTRO]
        assert _served(plex_config) == [("intro", 20_000, 44_000)]
        assert publisher.last_replaced_own_types == frozenset({T.INTRO})
        assert publisher.last_kept_types == frozenset()

    def test_an_unlocked_marker_still_leaves_plexs_own_rows_alone(self, agent, plex_config):
        conn = sqlite3.connect(_db_file(plex_config))
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (7, 563, 0, 'intro', 990, 29306, '', 1, '{\"pv:version\":\"5\"}')"
        )
        conn.commit()
        conn.close()

        publisher = _publisher_through(FlaskSession(agent), keep_plex=True)
        publisher.write("7", [INTRO], previous=None, duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")

        assert _served(plex_config) == [("intro", 990, 29306)]
        assert publisher.last_kept_types == frozenset({T.INTRO})

    def test_the_capability_is_ready_and_shows_the_agents_own_path(self, agent, plex_config):
        report = _publisher_through(FlaskSession(agent)).capability()
        assert report.state is Capability.READY
        assert report.details["db_path"] == str(_db_file(plex_config))
        assert report.details["agent"]["state"] == "connected"
        assert report.details["agent"]["version"] == plex_db_role.AGENT_VERSION

    def test_an_agent_that_stops_mid_publish_fails_the_write_and_leaves_plex_untouched(self, agent, plex_config):
        session = FlaskSession(agent)
        publisher = _publisher_through(session)
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        session.stopped = True

        with pytest.raises(PublishError) as ei:
            publisher.write("7", [CREDITS], previous=[INTRO], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")

        assert ei.value.state is Capability.AGENT_UNAVAILABLE
        assert "Can't reach the Plex database node" in str(ei.value)
        assert _served(plex_config) == [("intro", 11_000, 37_000)]  # exactly what the first run left

    def test_a_stopped_agent_makes_the_server_not_ready_rather_than_crashing(self, agent):
        session = FlaskSession(agent)
        session.stopped = True
        report = _publisher_through(session).capability()
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.details["agent"]["state"] == "unreachable"

    def test_a_wrong_key_writes_nothing_and_says_which_side_to_fix(self, agent, plex_config):
        publisher = _publisher_through(FlaskSession(agent))
        publisher._db = RemotePlexDb("http://plex-host.lan:9494", "not-the-key", session=FlaskSession(agent))
        with pytest.raises(PublishError) as ei:
            publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert "refused this app's key" in str(ei.value)
        assert _served(plex_config) == []

    def test_an_item_missing_from_plex_is_reported_through_the_agent(self, agent):
        publisher = _publisher_through(FlaskSession(agent))
        assert publisher.item_missing("7") is False
        assert publisher.item_missing("999") is True


class TestTheImageStaysInStepWithTheApp:
    def test_every_pinned_dependency_is_the_app_s_own(self):
        # The agent runs the app's code; a different Flask or requests here would be a second, untested environment.
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        app_deps = set(pyproject["project"]["dependencies"])
        lines = [
            line.strip()
            for line in (AGENT_DIR / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines, "the agent image must pin its dependencies"
        assert set(lines) <= app_deps

    def test_every_direct_dependency_is_pinned_in_the_lock_the_image_installs(self):
        # requirements.txt is the hand-written input; requirements.lock.txt is what the Dockerfile installs, with
        # --require-hashes. Editing one without regenerating the other means the image ships something nobody chose.
        locked = _locked_versions()
        assert locked, "the agent image must install a hash-pinned lock"
        for name, _ in _direct_requirements():
            assert name in locked, f"{name} is in requirements.txt but not pinned in requirements.lock.txt"

    def test_the_lock_pins_a_version_the_input_actually_allows(self):
        # A lock regenerated against a stale requirements.txt would sail past the test above: every name is still
        # there, just at a version the range no longer permits.
        locked = _locked_versions()
        for name, specifier in _direct_requirements():
            assert Version(locked[name]) in SpecifierSet(specifier), (
                f"requirements.lock.txt pins {name}=={locked[name]}, which requirements.txt's '{specifier}' excludes"
            )

    def test_the_dockerfile_actually_installs_the_lock_the_tests_above_guard(self):
        # Without this, the three lock tests guard a file the image need not use: reverting the Dockerfile to
        # `-r requirements.txt`, or to a floating `FROM python:3.12-slim-bookworm`, leaves them all green while the
        # README's reproducibility claim quietly becomes false.
        # Instructions only, with line continuations joined — a comment that happens to quote the right flags must
        # not satisfy this, and a wrapped RUN must not hide from it.
        dockerfile = (AGENT_DIR / "Dockerfile").read_text().replace("\\\n", " ")
        instructions = [line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")]
        installs = [line for line in instructions if "pip wheel" in line and "-r " in line]
        assert len(installs) == 1, f"expected exactly one dependency install, found {installs}"
        assert "--require-hashes" in installs[0] and "requirements.lock.txt" in installs[0]
        assert any("COPY nodes/plex-db/requirements.lock.txt" in line for line in instructions)

        bases = [line for line in instructions if line.startswith("FROM ")]
        assert bases, "the image must declare a base"
        for base in bases:
            assert "@sha256:" in base, f"base pinned by tag, not digest: {base}"

    def test_every_locked_distribution_carries_hashes(self):
        # --require-hashes refuses the whole file if one entry has none, so a hand-edited lock breaks the build
        # rather than the image. Catch it here instead.
        for name, entry in _lock_entries().items():
            assert "--hash=sha256:" in entry, f"{name} is pinned without a hash; --require-hashes will refuse the file"

    def test_the_version_the_agent_reports_is_one_this_app_accepts(self):
        assert not plex_remote.agent_too_old(plex_db_role.AGENT_VERSION)
        assert plex_remote.AGENT_PROTOCOL in plex_db_role.PROTOCOLS

    @pytest.mark.parametrize("document", ["docker-compose.yml", "README.md"])
    def test_the_image_tag_people_are_told_to_pull_is_the_version_the_agent_reports(self, document):
        # AGENT_VERSION is the one place this number is decided. docker-compose.yml and README.md hand the user a
        # copy of it, and the app refuses an agent whose version it doesn't accept — so a tag that drifts from the
        # source sends people to an image that argues with the app they just configured.
        text = (AGENT_DIR / document).read_text()
        tags = set(re.findall(r"ghcr\.io/[\w.-]+/plex-marker-agent:(\S+)", text))
        assert tags, f"{document} should name the image tag the user is told to pull"
        assert tags == {plex_db_role.AGENT_VERSION}

    def test_a_new_install_is_told_to_set_node_key_and_an_old_agent_token_still_works(self):
        # NODE_KEY must be 16 characters or more; AGENT_TOKEN predates the rule and works at any length.
        compose = (AGENT_DIR / "docker-compose.yml").read_text()
        readme = (AGENT_DIR / "README.md").read_text()
        assert "#   NODE_KEY=<paste the output of: openssl rand -hex 24>" in compose
        assert "NODE_KEY: ${NODE_KEY:-}" in compose and "AGENT_TOKEN: ${AGENT_TOKEN:-}" in compose
        assert "printf 'NODE_KEY=%s\\n'" in readme and "printf 'AGENT_TOKEN" not in readme


class TestWhichPlexTheAgentServes:
    """The agent names its Plex from Preferences.xml, so the app can refuse one set up beside another server."""

    def _preferences(self, plex_config: Path, identifier: str) -> None:
        (plex_config / "Preferences.xml").write_text(
            f'<?xml version="1.0" encoding="utf-8"?>\n<Preferences MachineIdentifier="uuid-form" '
            f'ProcessedMachineIdentifier="{identifier}" TranscoderTempDirectory="/transcode"/>\n'
        )

    def test_the_file_checks_carry_it(self, agent, plex_config):
        self._preferences(plex_config, "plex-aaa")
        result = agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]
        assert result["machine_identifier"] == "plex-aaa"

    def test_no_preferences_file_is_unknown_not_a_mismatch(self, agent):
        assert agent.post("/v1/checks/file", json={}, headers=AUTH).get_json()["result"]["machine_identifier"] == ""

    def test_the_app_writes_through_an_agent_beside_the_right_plex(self, agent, plex_config):
        self._preferences(plex_config, "plex-aaa")
        publisher = _publisher_through(FlaskSession(agent), identity="plex-aaa")
        publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert _served(plex_config) == [("intro", 11_000, 37_000)]

    def test_the_app_refuses_an_agent_beside_another_plex_before_it_writes(self, agent, plex_config):
        self._preferences(plex_config, "plex-bbb")
        publisher = _publisher_through(FlaskSession(agent), identity="plex-aaa")
        with pytest.raises(PublishError) as ei:
            publisher.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert "different Plex server" in str(ei.value)
        assert _served(plex_config) == []
        assert publisher.capability().state is Capability.AGENT_UNAVAILABLE


class TestTheAgentBlock:
    """What old apps read off every answer and off ``/v1/health``: the version, the protocols, the capabilities."""

    def test_it_names_the_version_the_protocol_and_every_optional_capability(self):
        assert plex_db_role.agent_block() == {
            "version": plex_db_role.AGENT_VERSION,
            "protocols": [1],
            "capabilities": ["chapters_v1", "loudness_v1", "library_health_v1"],
        }

    def test_the_capabilities_are_the_names_the_app_asks_for(self):
        from media_preview_generator.library_health.plex_rows import LIBRARY_HEALTH_CAPABILITY
        from media_preview_generator.loudness.plex_db import LOUDNESS_CAPABILITY
        from media_preview_generator.markers.publishers.plex_chapters import CHAPTER_CAPABILITY

        capabilities = plex_db_role.agent_block()["capabilities"]
        assert {CHAPTER_CAPABILITY, LOUDNESS_CAPABILITY, LIBRARY_HEALTH_CAPABILITY} == set(capabilities)

    def test_a_caller_changing_its_copy_changes_nothing_here(self):
        block = plex_db_role.agent_block()
        block["capabilities"].append("anything")
        block["protocols"].append(2)
        assert plex_db_role.agent_block()["capabilities"] == plex_db_role.CAPABILITIES
        assert plex_db_role.agent_block()["protocols"] == [1]

    def test_the_version_is_the_one_the_apps_update_messages_name(self):
        from media_preview_generator.library_health.plex import LIBRARY_HEALTH_AGENT_VERSION
        from media_preview_generator.loudness.guard import LOUDNESS_AGENT_VERSION

        # The app names these versions in "update the helper" messages; a build that ships the capabilities under
        # another number would send people to the wrong image.
        assert plex_db_role.AGENT_VERSION == LOUDNESS_AGENT_VERSION == LIBRARY_HEALTH_AGENT_VERSION


class TestTheInfoBlock:
    def test_it_names_the_folder_whether_the_database_is_there_and_which_plex(self, plex_config):
        (plex_config / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="plex-aaa"/>')
        node_app(str(plex_config))
        assert plex_db_role.info_block() == {
            "config_dir": str(plex_config),
            "db_present": True,
            "machine_identifier": "plex-aaa",
        }

    def test_a_folder_without_a_database_says_so(self, tmp_path):
        assert plex_db_role.info_block(str(tmp_path)) == {
            "config_dir": str(tmp_path),
            "db_present": False,
            "machine_identifier": "",
        }


class TestTheNodeSaysWhetherItHasTheRole:
    """The full node server (``create_node_app``): what the app learns about the role from its answers."""

    KEY = "plex-db-node-key-0123456789"

    def test_v1_info_carries_the_roles_version_so_an_older_role_needs_an_update(self, tmp_path, monkeypatch):
        from media_preview_generator.nodes import registry as reg
        from media_preview_generator.nodes.server import app as node_app_module

        plex = tmp_path / "Plex Media Server"
        plex.mkdir()
        monkeypatch.setenv("PLEX_CONFIG_DIR", str(plex))
        monkeypatch.setattr(plex_db_role, "AGENT_VERSION", "1.2.0")
        monkeypatch.setattr(plex_remote, "MIN_AGENT_VERSION", "9.0.0")
        app = node_app_module.create_node_app(config_dir=str(tmp_path / "cfg"), roles=["plex_db"], key=self.KEY)
        try:
            headers = {"Authorization": f"Bearer {self.KEY}", "X-MPG-Node-Protocol": "1"}
            info = app.test_client().get("/v1/info", headers=headers).get_json()
        finally:
            app.extensions["mpg_node"].close()
        assert info["agent"]["version"] == "1.2.0"
        record = {"id": "n1", "name": "plex-box", "url": "http://plex:9494", "key": self.KEY, "enabled": True}
        registry = reg.NodeRegistry(nodes_provider=lambda: [record], app_version=lambda: "5.2.0")
        outcome = registry._read_info(record, info)
        assert outcome.state == reg.STATE_UPDATE_NEEDED and "9.0.0" in outcome.reason

    def test_a_worker_only_node_has_no_agent_block(self, tmp_path, monkeypatch):
        import media_preview_generator.nodes.server.info as info_module
        from media_preview_generator.nodes.server import app as node_app_module

        monkeypatch.setattr(info_module, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "7.1"})
        app = node_app_module.create_node_app(config_dir=str(tmp_path), roles=["worker"], ops={}, key=self.KEY)
        app.extensions[node_app_module.EXTENSION].worker_info._detect = lambda: []
        try:
            headers = {"Authorization": f"Bearer {self.KEY}", "X-MPG-Node-Protocol": "1"}
            assert "agent" not in app.test_client().get("/v1/info", headers=headers).get_json()
        finally:
            app.extensions["mpg_node"].close()

    def test_a_node_without_the_role_is_named_as_such_not_as_unreachable(self, tmp_path, monkeypatch):
        import time

        import media_preview_generator.nodes.server.info as info_module
        from media_preview_generator.markers import readiness
        from media_preview_generator.nodes.server import app as node_app_module

        monkeypatch.setattr(info_module, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "7.1"})
        app = node_app_module.create_node_app(config_dir=str(tmp_path), roles=["worker"], ops={}, key=self.KEY)
        app.extensions[node_app_module.EXTENSION].worker_info._detect = lambda: []
        try:
            db = RemotePlexDb("http://gpu-box:9494", self.KEY, session=FlaskSession(app.test_client()))
            report = db.file_checks(deadline=time.monotonic() + 5)
        finally:
            app.extensions["mpg_node"].close()
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert db._client.state == plex_remote.AGENT_NO_ROLE
        assert "Plex database role is off" in report.message
        assert readiness.AGENT_STATES[plex_remote.AGENT_NO_ROLE][1] == "role off"
