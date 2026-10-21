"""Held Plex re-reads: the durable list and the flush that sends them once Plex's window opens."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing import plex_held_rereads as held


@pytest.fixture(autouse=True)
def _config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))


def _hold(server_id="plex-1", path="/media/a.mkv", **kwargs):
    fields = {
        "item_id": "42",
        "library_id": "1",
        "output_paths": (Path("/out/index-sd.bif"),),
        "token": "tok",
        "source_fingerprint": (1, 2, 3, 4, 5),
    }
    held.hold(server_id, path, **{**fields, **kwargs})


def test_hold_round_trips_every_field_through_the_file():
    _hold()
    [entry] = held.entries()
    assert (entry.server_id, entry.canonical_path, entry.item_id, entry.library_id) == (
        "plex-1",
        "/media/a.mkv",
        "42",
        "1",
    )
    assert entry.output_paths == ("/out/index-sd.bif",)
    assert entry.token == "tok"
    assert entry.source_fingerprint == (1, 2, 3, 4, 5)
    assert entry.held_at > 0


def test_missing_optional_fields_round_trip_as_none():
    _hold(item_id=None, library_id=None, token=None, source_fingerprint=None, output_paths=())
    [entry] = held.entries()
    assert (entry.item_id, entry.library_id, entry.token, entry.source_fingerprint, entry.output_paths) == (
        None,
        None,
        None,
        None,
        (),
    )


def test_hold_replaces_the_earlier_request_for_the_same_file():
    _hold(token="old")
    _hold(token="new")
    assert [e.token for e in held.entries()] == ["new"]


def test_release_forgets_only_that_file_and_ignores_unknown_ones():
    _hold(path="/media/a.mkv")
    _hold(path="/media/b.mkv")
    held.release("plex-1", "/media/a.mkv")
    held.release("plex-1", "/media/never-held.mkv")
    assert held.held_paths("plex-1") == {"/media/b.mkv"}


def test_entries_and_held_paths_are_per_server():
    _hold("plex-1", "/media/a.mkv")
    _hold("plex-2", "/media/b.mkv")
    assert held.held_paths("plex-1") == {"/media/a.mkv"}
    assert {e.server_id for e in held.entries()} == {"plex-1", "plex-2"}
    assert [e.server_id for e in held.entries("plex-2")] == ["plex-2"]


def test_held_requests_survive_reopening_the_database(tmp_path):
    _hold()
    assert (tmp_path / "plex_held_rereads.db").exists()
    assert held.held_paths("plex-1") == {"/media/a.mkv"}


def test_an_unreadable_file_counts_as_empty(tmp_path):
    (tmp_path / "plex_held_rereads.db").write_text("not a database")
    assert held.entries() == []


def _flush_with(monkeypatch, *, servers, hold_answers, slots, queued=()):
    """Run the flush with a registry of ``servers`` ({id: enabled}) and per-server should_hold answers."""
    live = {sid: SimpleNamespace(id=sid, name=f"Plex {sid}") for sid in servers}
    registry = MagicMock()
    registry.get.side_effect = live.get
    registry.get_config.side_effect = lambda sid: SimpleNamespace(enabled=servers[sid]) if sid in servers else None
    enqueue = MagicMock(return_value=True)
    monkeypatch.setattr("media_preview_generator.servers.ServerRegistry.from_settings", lambda _raw: registry)
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(get=lambda _key: []),
    )
    monkeypatch.setattr("media_preview_generator.processing.plex_refresh.enqueue_plex_refresh", enqueue)
    monkeypatch.setattr("media_preview_generator.processing.plex_refresh.free_plex_refresh_slots", lambda: slots)
    monkeypatch.setattr(
        "media_preview_generator.processing.plex_refresh.is_plex_refresh_queued",
        lambda server_id, path: (server_id, path) in queued,
    )
    monkeypatch.setattr(held, "should_hold", lambda server, _library: hold_answers[server.id])
    held.flush_held_rereads()
    return enqueue


def test_flush_sends_only_servers_whose_hold_no_longer_applies(monkeypatch):
    _hold("plex-1", "/media/a.mkv")
    _hold("plex-2", "/media/b.mkv")

    enqueue = _flush_with(
        monkeypatch,
        servers={"plex-1": True, "plex-2": True},
        hold_answers={"plex-1": False, "plex-2": True},
        slots=100,
    )

    enqueue.assert_called_once()
    args, kwargs = enqueue.call_args
    assert (args[0].id, args[1], args[2]) == ("plex-1", "/media/a.mkv", "42")
    assert kwargs == {
        "library_id": "1",
        "output_paths": (Path("/out/index-sd.bif"),),
        "notification_token": "tok",
        "source_fingerprint": (1, 2, 3, 4, 5),
    }
    # The worker releases an entry once Plex answers, so the flush leaves both in place.
    assert held.held_paths("plex-2") == {"/media/b.mkv"}
    assert held.held_paths("plex-1") == {"/media/a.mkv"}


def test_flush_sends_no_more_than_the_queue_has_room_for(monkeypatch):
    for name in "abc":
        _hold(path=f"/media/{name}.mkv")
    enqueue = _flush_with(monkeypatch, servers={"plex-1": True}, hold_answers={"plex-1": False}, slots=34)
    assert enqueue.call_count == 2
    assert len(held.entries()) == 3


def test_flush_sends_nothing_when_the_queue_is_full(monkeypatch):
    _hold()
    enqueue = _flush_with(monkeypatch, servers={"plex-1": True}, hold_answers={"plex-1": False}, slots=0)
    enqueue.assert_not_called()


def test_flush_leaves_headroom_in_the_queue(monkeypatch):
    _hold()
    enqueue = _flush_with(monkeypatch, servers={"plex-1": True}, hold_answers={"plex-1": False}, slots=32)
    enqueue.assert_not_called()


def test_flush_skips_files_already_queued_so_a_newer_token_is_not_overwritten(monkeypatch):
    _hold(path="/media/a.mkv")
    _hold(path="/media/b.mkv")
    enqueue = _flush_with(
        monkeypatch,
        servers={"plex-1": True},
        hold_answers={"plex-1": False},
        slots=100,
        queued={("plex-1", "/media/a.mkv")},
    )
    assert [call.args[1] for call in enqueue.call_args_list] == ["/media/b.mkv"]


def test_flush_drops_requests_for_servers_that_are_gone_or_disabled(monkeypatch):
    _hold("gone", "/media/a.mkv")
    _hold("off", "/media/b.mkv")
    _hold("plex-1", "/media/c.mkv")
    enqueue = _flush_with(
        monkeypatch, servers={"off": False, "plex-1": True}, hold_answers={"off": False, "plex-1": True}, slots=100
    )
    enqueue.assert_not_called()
    assert held.held_paths("gone") == set()
    assert held.held_paths("off") == set()
    assert held.held_paths("plex-1") == {"/media/c.mkv"}


def test_flush_reads_settings_of_every_enabled_plex_server_even_with_nothing_held(monkeypatch):
    from media_preview_generator.servers import ServerType

    configs = [
        SimpleNamespace(id="plex-1", enabled=True, type=ServerType.PLEX),
        SimpleNamespace(id="plex-off", enabled=False, type=ServerType.PLEX),
        SimpleNamespace(id="jelly-1", enabled=True, type=ServerType.JELLYFIN),
        SimpleNamespace(id="plex-2", enabled=True, type=ServerType.PLEX),
    ]
    registry = MagicMock()
    registry.configs.return_value = configs
    registry.get.side_effect = lambda sid: SimpleNamespace(id=sid, name=sid)
    read = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.ServerRegistry.from_settings", lambda _raw: registry)
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(get=lambda _key: []),
    )
    monkeypatch.setattr(held, "read_settings", read)

    held.flush_held_rereads()

    assert [call.args[0].id for call in read.call_args_list] == ["plex-1", "plex-2"]
    assert all(call.kwargs == {"fresh": True} for call in read.call_args_list)
