"""Nothing reaches a Plex database node that is turned off (or that the server has no record of), nor one the
registry holds back (a new id it can't confirm, or an id no check has heard yet since the app started).

Every path that resolves a server's Plex database node is covered: markers (``plex_database``), chapters
(``plex_chapters._context``), loudness (``create_loudness_db``) and Library Health (``library_health.plex._open_db``).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.library_health import plex as health_plex
from media_preview_generator.loudness import plex_db as loudness_db
from media_preview_generator.loudness.guard import RemoteLoudnessDb, create_loudness_db, loudness_capability
from media_preview_generator.loudness.job import LoudnessContext
from media_preview_generator.loudness.plex_db import LOUDNESS_CAPABILITY
from media_preview_generator.markers import pipeline
from media_preview_generator.markers.publishers import plex_chapters, plex_db
from media_preview_generator.markers.publishers.base import Capability, PublishError
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.markers.publishers.plex_remote import (
    AGENT_DISABLED,
    AGENT_NO_NODE,
    AGENT_UNCONFIRMED_NODE,
    AGENT_UNREACHABLE,
    AgentClient,
    AgentError,
    AgentUnreachableError,
    RemotePlexDb,
    plex_database,
)
from media_preview_generator.markers.settings import load_server
from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.client import NodeProtocolError, NodeUnreachableError
from media_preview_generator.nodes.protocol import KIND_WRONG_NODE, NODE_UID_HEADER, ROLE_PLEX_DB
from media_preview_generator.nodes.registry import PlexDbHold, get_registry, set_registry
from media_preview_generator.servers.base import ServerConfig, ServerType
from tests.loudness.test_plex_db import FIELDS
from tests.markers.pipeline_helpers import _ctx
from tests.markers.test_plex_db_publisher import _make_db, _mountinfo
from tests.nodes.plex_db_harness import NODE_URL, TOKEN, FlaskSession, node_app
from tests.nodes.test_registry import Harness, _info, _record

NODE = {
    "id": "n1",
    "node_uid": "uid-1",
    "name": "Plex box",
    "url": "http://plex-host.lan:9494",
    "key": "node-key",
    "enabled": True,
    "path_mappings": [],
    "added_at": "2026-10-15T00:00:00+00:00",
}
MARKERS = {
    "enabled": True,
    "library_ids": None,
    "plex": {
        "db_write_confirmed_at": "2026-01-01",
        "on_plex_redetect": "restore",
        "agent": {"enabled": True, "node_id": "n1"},
    },
}


@pytest.fixture
def node(tmp_path):
    """Store the node record; ``node(enabled=...)`` sets the one cell under test."""
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    reset_settings_manager()
    sm = get_settings_manager(str(tmp_path))

    def configure(*, enabled: bool, held: str | None = None) -> None:
        sm.set("nodes", [{**NODE, "enabled": enabled}])
        # The registry's word: a new id it couldn't tie to the server's Plex, or an id no check has heard yet.
        hold = PlexDbHold(MESSAGES[held], waits=held == AGENT_UNREACHABLE) if held else None
        registry.plex_db_hold = lambda node_id: hold

    registry = MagicMock()
    set_registry(registry)
    yield configure
    set_registry(None)
    reset_settings_manager()


# (enabled, the registry's hold) -> the refusal's badge state, or None when the node is used.
CELLS = [
    pytest.param((True, None), None, id="enabled"),
    pytest.param((False, None), AGENT_DISABLED, id="disabled"),
    pytest.param((True, AGENT_UNCONFIRMED_NODE), AGENT_UNCONFIRMED_NODE, id="new-id-unconfirmed"),
    pytest.param((True, AGENT_UNREACHABLE), AGENT_UNREACHABLE, id="id-not-checked-yet"),
]
MESSAGES = {
    AGENT_DISABLED: "Plex box is turned off on the Nodes page",
    AGENT_UNCONFIRMED_NODE: "Plex box reports a new id and this app can't confirm it is the same Plex.",
    AGENT_UNREACHABLE: "Checking Plex box before sending it Plex database work.",
}


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        id="p1",
        type=ServerType.PLEX,
        name="Home",
        markers=MARKERS,
        output={"plex_config_folder": str(tmp_path / "plex")},
        server_identity="plex-a",
    )


@pytest.mark.parametrize(("enabled", "refused"), CELLS)
def test_markers(node, enabled, refused):
    node(enabled=enabled[0], held=enabled[1])
    session = MagicMock()
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home", session=session)
    assert isinstance(db, RemotePlexDb)
    if refused is None:
        assert db._client.refusal() is None
        return
    report = db.file_checks(deadline=1e12)
    assert report.state is Capability.AGENT_UNAVAILABLE
    assert report.message.startswith(MESSAGES[refused])
    assert report.details["agent"]["state"] == refused
    session.post.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.parametrize(("enabled", "refused"), CELLS)
def test_chapters(node, monkeypatch, enabled, refused):
    node(enabled=enabled[0], held=enabled[1])
    monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
    config = SimpleNamespace(markers=MARKERS, output={})
    if refused is None:
        backend, *_ = plex_chapters._context(MagicMock(), config)
        assert isinstance(backend, AgentClient) and backend.url == NODE["url"]
        return
    with pytest.raises(plex_chapters.ChapterError) as caught:
        plex_chapters._context(MagicMock(), config)
    assert str(caught.value).startswith(MESSAGES[refused])
    assert caught.value.state is Capability.AGENT_UNAVAILABLE


@pytest.mark.parametrize(("enabled", "refused"), CELLS)
def test_loudness(node, tmp_path, monkeypatch, enabled, refused):
    node(enabled=enabled[0], held=enabled[1])
    sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
    monkeypatch.setattr("requests.Session.post", sent)
    monkeypatch.setattr("requests.Session.get", sent)
    server = MagicMock()
    server._connect.return_value.query.return_value = Element(
        "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
    )
    cfg = _cfg(tmp_path)
    db = create_loudness_db(cfg, server)
    assert isinstance(db, RemoteLoudnessDb)
    if refused is None:
        assert db._remote._client.refusal() is None
        return
    report = loudness_capability(server, cfg)
    # A node still to be checked reads as one that doesn't answer: the loudness job waits for it (job.py WAITING).
    assert report.state is (Capability.UNREACHABLE if refused == AGENT_UNREACHABLE else Capability.AGENT_UNAVAILABLE)
    assert report.message.startswith(MESSAGES[refused])
    sent.assert_not_called()


@pytest.mark.parametrize(("enabled", "refused"), CELLS)
def test_library_health(node, tmp_path, monkeypatch, enabled, refused):
    node(enabled=enabled[0], held=enabled[1])
    sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
    monkeypatch.setattr("requests.Session.post", sent)
    monkeypatch.setattr("requests.Session.get", sent)
    db = health_plex._open_db(_cfg(tmp_path))
    assert isinstance(db, RemotePlexDb)
    if refused is None:
        assert db._client.refusal() is None
        return
    assert health_plex._node_refusal(db).startswith(MESSAGES[refused])
    sent.assert_not_called()


def test_a_node_record_that_is_gone_is_refused_with_a_message_naming_the_fix(node, tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager

    node(enabled=True)
    get_settings_manager().set("nodes", [])
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
    report = db.file_checks(deadline=1e12)
    assert report.state is Capability.AGENT_UNAVAILABLE
    assert report.message == "This server has no Plex database node on the Nodes page. Choose one in Edit → Connection."
    assert report.details["agent"]["state"] == AGENT_NO_NODE


class TestLoudnessWithoutANode:
    """What the user sees when loudness is switched on through a node that can't be resolved."""

    def test_switched_on_without_a_node_chosen(self, node, tmp_path):
        node(enabled=True)
        cfg = _cfg(tmp_path)
        cfg.markers = {"plex": {"agent": {"enabled": True}}}
        report = loudness_capability(MagicMock(), cfg)
        # Kept switched on with no node (as v24 leaves a helper it couldn't move): refused like a removed node.
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.message == (
            "This server has no Plex database node on the Nodes page. Choose one in Edit → Connection."
        )

    def test_switched_on_with_a_node_that_was_removed(self, node, tmp_path):
        from media_preview_generator.web.settings_manager import get_settings_manager

        node(enabled=True)
        get_settings_manager().set("nodes", [])
        server = MagicMock()
        server._connect.return_value.query.return_value = Element(
            "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
        )
        report = loudness_capability(server, _cfg(tmp_path))
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.message.startswith("This server has no Plex database node on the Nodes page.")


class TestTurnedOffAfterTheDatabaseWasOpened:
    """A job keeps its database for its whole run: turning the node off mid-job has to stop the very next request,
    not the next job's."""

    @pytest.fixture
    def nothing_sent(self, monkeypatch):
        sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
        monkeypatch.setattr("requests.Session.post", sent)
        monkeypatch.setattr("requests.Session.get", sent)
        return sent

    TURNED = [pytest.param({"enabled": False}, AGENT_DISABLED, id="turned-off")]

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_markers(self, node, nothing_sent, change, refused):
        node(enabled=True)
        db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
        node(**change)
        report = db.file_checks(deadline=1e12)
        assert report.state is Capability.AGENT_UNAVAILABLE and report.message.startswith(MESSAGES[refused])
        assert report.details["agent"]["state"] == refused
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_chapters(self, node, nothing_sent, monkeypatch, change, refused):
        node(enabled=True)
        monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
        backend, *_ = plex_chapters._context(MagicMock(), SimpleNamespace(markers=MARKERS, output={}))
        node(**change)
        with pytest.raises(AgentError) as caught:
            backend.post("/v1/chapters/write", {}, timeout=1)
        assert str(caught.value).startswith(MESSAGES[refused])
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_loudness(self, node, nothing_sent, tmp_path, change, refused):
        node(enabled=True)
        server = MagicMock()
        server._connect.return_value.query.return_value = Element(
            "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
        )
        db = create_loudness_db(_cfg(tmp_path), server)
        node(**change)
        with pytest.raises(PublishError) as caught:
            loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 5)
        assert str(caught.value).startswith(MESSAGES[refused])
        assert caught.value.state is Capability.AGENT_UNAVAILABLE
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_library_health(self, node, nothing_sent, tmp_path, change, refused):
        node(enabled=True)
        db = health_plex._open_db(_cfg(tmp_path))
        node(**change)
        assert health_plex._node_refusal(db).startswith(MESSAGES[refused])
        nothing_sent.assert_not_called()

    def test_turned_back_on_it_is_used_again(self, node, nothing_sent):
        node(enabled=False)
        db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
        assert db.file_checks(deadline=1e12).details["agent"]["state"] == AGENT_DISABLED
        node(enabled=True)
        assert db._client.refusal() is None


def _ok_answer(capabilities: tuple[str, ...] = ()) -> MagicMock:
    answer = MagicMock(status_code=200, headers={})
    agent = {"version": "1.2.0", "protocols": [1, 2], "capabilities": list(capabilities)}
    body = {"ok": True, "agent": agent, "result": {}}
    answer.iter_content.return_value = [json.dumps(body).encode()]
    return answer


class TestNodeEditedAfterTheDatabaseWasOpened:
    """Editing a node's address or key clears its saved id. A job that opened the database before the edit sends its
    next request to the new address with the new key: one without the id at the old address would run on any machine
    there that has the key."""

    EDITED = {"url": "http://new-host.lan:9494", "key": "new-key", "node_uid": None}

    def _edit(self) -> None:
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("nodes", [{**NODE, **self.EDITED}])

    def _assert_sent_to_the_edited_node(self, session: MagicMock) -> None:
        assert [c.args[0] for c in session.post.call_args_list if c.args[0].startswith(NODE["url"])] == []
        sent = session.post.call_args
        assert sent.args[0].startswith(self.EDITED["url"])
        assert sent.kwargs["headers"]["Authorization"] == f"Bearer {self.EDITED['key']}"
        assert NODE_UID_HEADER not in sent.kwargs["headers"]

    def test_markers(self, node):
        node(enabled=True)
        session = MagicMock()
        session.post.return_value = _ok_answer()
        db = plex_database(
            load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home", session=session
        )
        self._edit()
        db._client.post("/v1/item/read", {}, timeout=1)
        self._assert_sent_to_the_edited_node(session)

    def test_chapters(self, node, monkeypatch):
        node(enabled=True)
        monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
        backend, *_ = plex_chapters._context(MagicMock(), SimpleNamespace(markers=MARKERS, output={}))
        session = MagicMock()
        session.post.return_value = _ok_answer()
        backend._session = session
        self._edit()
        backend.post("/v1/chapters/check", {}, timeout=1)
        self._assert_sent_to_the_edited_node(session)


OTHER_NODE = {
    **NODE,
    "id": "n2",
    "node_uid": "uid-2",
    "name": "Other box",
    "url": "http://other-host.lan:9494",
    "key": "other-key",
}
REPOINTED = "This server's Plex database node was changed in Edit → Connection after this job started"


class TestServerRepointedAfterTheDatabaseWasOpened:
    """A job opens a server's database through the node the server named when the job started. Choosing another node
    (or none) in Edit → Connection while it runs sends nothing more through the old one: its work waits for a run that
    opens the new choice. The old node's Plex identifier can't be relied on to refuse it, as a node beside a Plex in a
    custom folder reports none."""

    CHOICES = [
        pytest.param({"enabled": True, "node_id": "n2"}, id="another-node"),
        pytest.param({"enabled": False, "node_id": "n1"}, id="no-node"),
    ]

    @pytest.fixture
    def repoint(self, node):
        from media_preview_generator.web.settings_manager import get_settings_manager

        node(enabled=True)
        sm = get_settings_manager()
        sm.set("nodes", [NODE, OTHER_NODE])

        def save(agent: dict) -> None:
            markers = {**MARKERS, "plex": {**MARKERS["plex"], "agent": agent}}
            sm.set("media_servers", [{"id": "p1", "type": "plex", "name": "Home", "enabled": True, "markers": markers}])

        save(MARKERS["plex"]["agent"])
        return save

    @staticmethod
    def _saved_settings():
        from media_preview_generator.web.settings_manager import get_settings_manager

        return load_server(get_settings_manager().get("media_servers")[0]["markers"], "plex")

    @staticmethod
    def _loudness_server() -> MagicMock:
        server = MagicMock()
        server._connect.return_value.query.return_value = Element(
            "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
        )
        return server

    @pytest.mark.parametrize("agent", CHOICES)
    def test_markers(self, repoint, tmp_path, agent):
        from media_preview_generator.markers.publishers.factory import publisher_for

        publisher = publisher_for(MagicMock(), _cfg(tmp_path), settings_provider=self._saved_settings)
        session = MagicMock()
        session.post.return_value = _ok_answer()
        publisher._db._client._session = session
        publisher._db._client.post("/v1/item/read", {}, timeout=1)
        assert session.post.call_args.args[0].startswith(NODE["url"])

        repoint(agent)
        with pytest.raises(AgentUnreachableError) as caught:
            publisher._db._client.post("/v1/item/read", {}, timeout=1)
        assert str(caught.value).startswith(REPOINTED)
        assert session.post.call_count == 1

    @pytest.mark.parametrize("agent", CHOICES)
    def test_chapters(self, repoint, monkeypatch, agent):
        monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
        server = MagicMock()
        server._server_config = SimpleNamespace(id="p1", markers=MARKERS, output={})
        backend, *_ = plex_chapters._context(server)
        session = MagicMock()
        session.post.return_value = _ok_answer()
        backend._session = session
        backend.post("/v1/chapters/check", {}, timeout=1)
        assert session.post.call_args.args[0].startswith(NODE["url"])

        repoint(agent)
        with pytest.raises(AgentUnreachableError) as caught:
            backend.post("/v1/chapters/register", {}, timeout=1)
        assert str(caught.value).startswith(REPOINTED)
        assert session.post.call_count == 1
        # The job's next file opens through the same (old) settings: refused before anything is sent.
        with pytest.raises(plex_chapters.ChapterError) as caught:
            plex_chapters._context(server)
        assert str(caught.value).startswith(REPOINTED)

    @pytest.mark.parametrize("agent", CHOICES)
    def test_loudness(self, repoint, monkeypatch, tmp_path, agent):
        from media_preview_generator.loudness.job import LoudnessContext

        sent = MagicMock(return_value=_ok_answer(capabilities=(LOUDNESS_CAPABILITY,)))
        monkeypatch.setattr("requests.Session.post", sent)
        monkeypatch.setattr("requests.Session.get", sent)
        registry = MagicMock()
        registry.get.return_value = self._loudness_server()
        db, _why = LoudnessContext(registry=registry, ffmpeg="ffmpeg").db(_cfg(tmp_path))
        assert sent.call_args.args[0].startswith(NODE["url"])
        before = sent.call_count

        repoint(agent)
        with pytest.raises(PublishError) as caught:
            loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 5)
        assert str(caught.value).startswith(REPOINTED)
        assert sent.call_count == before

    def test_the_same_node_kept_is_used(self, repoint, tmp_path):
        from media_preview_generator.markers.publishers.factory import publisher_for

        publisher = publisher_for(MagicMock(), _cfg(tmp_path), settings_provider=self._saved_settings)
        repoint({"enabled": True, "node_id": "n1"})
        assert publisher._db._client.refusal() is None

    def test_a_check_of_settings_not_saved_yet_is_not_refused(self, repoint, tmp_path):
        """The Edit tab checks the node the user just picked, before it is saved."""
        from media_preview_generator.markers.publishers.factory import publisher_for

        picked = {**MARKERS, "plex": {**MARKERS["plex"], "agent": {"enabled": True, "node_id": "n2"}}}
        publisher = publisher_for(MagicMock(), _cfg(tmp_path), settings=load_server(picked, "plex"))
        assert publisher._db._client.refusal() is None
        assert publisher._db._client.url == OTHER_NODE["url"]


def test_a_node_still_to_be_checked_is_refused_as_not_answering_so_the_work_waits(node):
    """Each feature waits for a node that doesn't answer (loudness waits, chapters retry); a refusal fails the file."""
    node(enabled=True, held=AGENT_UNREACHABLE)
    session = MagicMock()
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home", session=session)
    with pytest.raises(AgentUnreachableError) as caught:
        db._client.post("/v1/item/read", {}, timeout=1)
    assert str(caught.value).startswith(MESSAGES[AGENT_UNREACHABLE])
    assert db._client.state == AGENT_UNREACHABLE
    session.post.assert_not_called()


def _wrong_node_answer() -> MagicMock:
    answer = MagicMock(status_code=409, headers={})
    body = {"ok": False, "agent": {"version": "1.2.0", "protocols": [1, 2]}, "error": {"kind": KIND_WRONG_NODE}}
    answer.iter_content.return_value = [json.dumps(body).encode()]
    return answer


def test_markers_name_the_saved_id_and_another_machine_there_has_the_node_checked_again(node):
    node(enabled=True)
    session = MagicMock()
    session.post.return_value = _wrong_node_answer()
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home", session=session)

    with pytest.raises(AgentUnreachableError):
        db.item_exists(7, deadline=time.monotonic() + 5)

    assert session.post.call_args.kwargs["headers"][NODE_UID_HEADER] == NODE["node_uid"]
    get_registry().report_unavailable.assert_called_once_with(NODE["id"])


def test_chapters_name_the_saved_id_and_another_machine_there_has_the_node_checked_again(node, monkeypatch):
    node(enabled=True)
    monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
    backend, *_ = plex_chapters._context(MagicMock(), SimpleNamespace(markers=MARKERS, output={}))
    session = MagicMock()
    session.post.return_value = _wrong_node_answer()
    backend._session = session

    with pytest.raises(AgentUnreachableError):
        backend.post("/v1/chapters/check", {}, timeout=1)

    assert session.post.call_args.kwargs["headers"][NODE_UID_HEADER] == NODE["node_uid"]
    get_registry().report_unavailable.assert_called_once_with(NODE["id"])


@pytest.fixture
def held(tmp_path, monkeypatch):
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)
    folder = tmp_path / "Plex Media Server"
    _make_db(folder, journal_mode="wal")
    node_client = node_app(str(folder), node_uid="uid-1", mountinfo_path=_mountinfo(tmp_path)).test_client()
    record = _record(node_uid="uid-1", url=NODE_URL, key=TOKEN, name="Plex box", path_mappings=[])
    reset_settings_manager()
    sm = get_settings_manager(str(tmp_path / "app"))
    sm.set("nodes", [record])
    sm.set("media_servers", [{"id": "plex-1", "type": "plex", "server_identity": "plex-a", "markers": MARKERS}])
    harness = Harness(nodes=[record])
    reg.set_registry(harness.registry)
    config = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex:32400",
        auth={},
        output={"plex_config_folder": "/nowhere/plex"},
        path_mappings=[],
    )
    settings = load_server(MARKERS, "plex")
    server = MagicMock()
    server.get_server_status.return_value = {"plex_pass": True, "version": "1.43.4"}
    session = FlaskSession(node_client)
    db = plex_database(settings, path_provider=lambda: None, label="Plex", session=session)
    publisher = PlexMarkerPublisher(server, config, settings, db=db, ui_details=False)
    yield harness, config, publisher, session
    reg.set_registry(None)
    harness.registry.stop()
    reset_settings_manager()


class TestAHeldNodeIsUsedOnceACheckConfirmsIt:
    """A Plex database node that was down (the Plex host rebooted) is held after it answers again, until the
    registry's check hears its saved id, at most ``WAITING_RECHECK_S`` later. A markers job that asked in that window
    was told "Checking ... nothing is lost", and kept that answer for the capability cache's five minutes: every file
    was skipped although the node was confirmed seconds later."""

    def test_a_markers_job_writes_once_the_check_confirms_the_node_it_asked_about_while_held(self, held):
        harness, config, publisher, _session = held
        client = harness.client()
        client.default_info = NodeUnreachableError("down")
        harness.registry.check_now("n1")  # the Plex host rebooted: no answer, the node's next check is backed off
        back = _info(uid="uid-1", roles=(ROLE_PLEX_DB,))
        back["plex_db"] = {"machine_identifier": ""}
        client.default_info = back  # it answers again, with the saved id

        ctx = _ctx(MagicMock(), MagicMock())
        first = pipeline.cached_capability(ctx, config, publisher)  # the job's first file, before that check
        assert first.state is Capability.AGENT_UNAVAILABLE
        assert "nothing is lost" in first.message

        harness.clock.now += reg.WAITING_RECHECK_S
        harness.registry.check_now("n1")  # the check the hold brought forward hears the saved id
        assert harness.registry.plex_db_hold("n1") is None

        again = pipeline.cached_capability(ctx, config, publisher)  # the job's next file
        assert again.ready, f"still the held answer: {again.state.value}: {again.message}"

    def test_a_node_that_did_not_answer_is_asked_again_for_the_job_s_next_file(self, held):
        harness, config, publisher, session = held
        harness.registry.check_now("n1")  # heard with its saved id: nothing held
        session.stopped = True  # the node's host is unplugged
        ctx = _ctx(MagicMock(), MagicMock())
        down = pipeline.cached_capability(ctx, config, publisher)
        assert down.state is Capability.AGENT_UNAVAILABLE and "nothing is lost" in down.message

        session.stopped = False  # back before the registry's next check
        assert pipeline.cached_capability(ctx, config, publisher).ready


def _plex_db_info(uid: str, machine: str) -> dict:
    info = _info(uid=uid, roles=(ROLE_PLEX_DB,))
    info["plex_db"] = {"machine_identifier": machine}
    info["agent"] = {"version": "1.2.0", "protocols": [1, 2], "capabilities": []}
    return info


def _save_node(harness: Harness, **fields) -> None:
    """Change the node's record in settings and in the registry's copy, as the Nodes page does."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    nodes = sm.nodes
    nodes[0].update(fields)
    sm.update_nodes(nodes)
    harness.change(**fields)


# What stops the node's work, and how the user (or the registry's next check) lifts it.
REFUSALS = {
    "another-machine-at-its-address-then-it-left": AGENT_UNCONFIRMED_NODE,
    "rolled-back-to-1.1.2-then-updated": AGENT_UNCONFIRMED_NODE,
    "turned-off-then-on": AGENT_DISABLED,
    "recreated-then-its-key-typed-again": AGENT_UNCONFIRMED_NODE,
}


def _refuse(harness: Harness, refusal: str) -> None:
    client = harness.client()
    if refusal == "turned-off-then-on":
        _save_node(harness, enabled=False)
    elif refusal == "rolled-back-to-1.1.2-then-updated":
        client.default_info = NodeProtocolError("no /v1/info")  # 1.1.2 has no /v1/info, so it gives no id
        harness.legacy = ("connected", "1.1.2")
    else:  # another machine took the address with the same key, or the node lost its /config: a new id
        client.default_info = _plex_db_info("uid-2", "plex-b")
    harness.registry.check_now("n1")


def _lift(harness: Harness, refusal: str) -> None:
    client = harness.client()
    if refusal == "turned-off-then-on":
        _save_node(harness, enabled=True)
    elif refusal == "recreated-then-its-key-typed-again":
        _save_node(harness, node_uid=None)
    else:
        client.default_info = _plex_db_info("uid-1", "plex-a")  # the saved machine answers there again
        harness.clock.now += reg.WAITING_RECHECK_S
    harness.registry.check_now("n1")


class TestARefusalLiftedWhileAJobRunsReachesItsNextFile:
    """A node this app refuses (turned off, or a new id or none held until the saved machine answers or its key is
    typed again) is lifted by the Nodes page or the registry's next check, not by the node. A job that kept the
    refusal (markers' capability cache, loudness's recheck delay) skipped or failed files after it was lifted."""

    @pytest.fixture
    def refused(self, held, request):
        harness, config, publisher, _session = held
        harness.client().default_info = _plex_db_info("uid-1", "plex-a")
        harness.registry.check_now("n1")
        assert harness.registry.plex_db_hold("n1") is None
        _refuse(harness, request.param)
        return harness, config, publisher, request.param

    @pytest.mark.parametrize("refused", list(REFUSALS), indirect=True)
    def test_a_markers_job_writes_the_next_file(self, refused):
        harness, config, publisher, refusal = refused
        ctx = _ctx(MagicMock(), MagicMock())
        first = pipeline.cached_capability(ctx, config, publisher)
        assert first.state is Capability.AGENT_UNAVAILABLE
        assert first.details["agent"]["state"] == REFUSALS[refusal]

        _lift(harness, refusal)
        assert harness.registry.plex_db_hold("n1") is None

        again = pipeline.cached_capability(ctx, config, publisher)
        assert again.ready, f"still the lifted refusal: {again.state.value}: {again.message}"

    @pytest.mark.parametrize("refused", list(REFUSALS), indirect=True)
    def test_a_loudness_job_writes_the_next_file(self, refused):
        harness, config, publisher, refusal = refused
        ctx = LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
        ctx._dbs[config.id] = RemoteLoudnessDb(MagicMock(), publisher._db, server_name="Plex")
        _db, why = ctx.db(config)
        assert why and ctx._states[config.id] is Capability.AGENT_UNAVAILABLE

        _lift(harness, refusal)

        _db, why = ctx.db(config)
        assert why == "", f"still the lifted refusal: {why}"
