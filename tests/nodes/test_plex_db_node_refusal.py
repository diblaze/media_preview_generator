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
from media_preview_generator.markers.publishers import plex_chapters
from media_preview_generator.markers.publishers.base import Capability, PublishError
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
from media_preview_generator.nodes.protocol import KIND_WRONG_NODE, NODE_UID_HEADER
from media_preview_generator.nodes.registry import PlexDbHold, get_registry, set_registry
from media_preview_generator.servers.base import ServerType
from tests.loudness.test_plex_db import FIELDS

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
