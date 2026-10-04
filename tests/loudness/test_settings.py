"""Per-server loudness settings, their save route and the job route."""

from __future__ import annotations

import pytest

from media_preview_generator.loudness import settings as ls
from media_preview_generator.servers.base import Library, ServerConfig, ServerType

CONFIRMED = {"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00+00:00"}}


def _cfg(loudness, markers=CONFIRMED, type_=ServerType.PLEX):
    return ServerConfig(
        id="p1",
        type=type_,
        name="Plex",
        enabled=True,
        url="http://plex",
        auth={},
        libraries=[
            Library(id="1", name="Filmer", remote_paths=(), enabled=True, kind="movie"),
            Library(id="2", name="TV", remote_paths=(), enabled=True, kind="episode"),
            Library(id="3", name="Musik", remote_paths=(), enabled=True, kind="track"),
        ],
        loudness=loudness,
        markers=markers,
    )


def test_off_by_default_and_for_a_missing_block():
    assert ls.default_server_loudness() == {"enabled": False, "library_ids": None}
    assert ls.load_server_loudness(_cfg({})).enabled is False
    assert ls.loudness_libraries(_cfg({})) == []


@pytest.mark.parametrize("enabled", ["false", "true", "", 0, 1, {}, [], None])
def test_enable_requires_a_boolean_and_invalid_stored_values_stay_off(enabled):
    block, error = ls.validate_server_loudness({"enabled": enabled}, "plex", {})
    assert block is None and error == "loudness.enabled must be a boolean"
    assert ls.load_server_loudness(_cfg({"enabled": enabled})).enabled is False


def test_default_libraries_are_movies_and_tv():
    assert [lib.id for lib in ls.loudness_libraries(_cfg({"enabled": True}))] == ["1", "2"]


def test_explicit_video_choice_does_not_enable_other_libraries():
    assert [lib.id for lib in ls.loudness_libraries(_cfg({"enabled": True, "library_ids": ["2"]}))] == ["2"]


@pytest.mark.parametrize("kind", ["track", "artist", "album", "photo"])
def test_explicit_non_video_library_is_refused_and_never_owned(kind):
    settings = ls.ServerLoudnessSettings(enabled=True, library_ids=("3",))
    assert not ls.library_chosen(settings, library_id="3", kind=kind)
    block, error = ls.validate_server_loudness(
        {"enabled": True, "library_ids": ["3"]}, "plex", {}, library_kinds={"3": kind}
    )
    assert block is None and "movie and TV" in error


@pytest.mark.parametrize("kind", [None, "", "movie", "episode", "show"])
def test_explicit_video_or_legacy_untyped_library_is_supported(kind):
    settings = ls.ServerLoudnessSettings(enabled=True, library_ids=("3",))
    assert ls.library_chosen(settings, library_id="3", kind=kind)
    block, error = ls.validate_server_loudness(
        {"enabled": True, "library_ids": ["3"]}, "plex", {}, library_kinds={"3": kind}
    )
    assert block is not None and not error


def test_stored_music_opt_in_is_disabled():
    assert not ls.load_server_loudness(_cfg({"enabled": True, "library_ids": ["3"]})).enabled


@pytest.mark.parametrize(
    ("raw", "server_type", "markers", "error"),
    [
        ({"enabled": True}, "plex", {"plex": {"agent": {"enabled": True}}}, "helper does not support"),
        ({"enabled": True}, "jellyfin", CONFIRMED, "Plex servers only"),
        ({"library_ids": "all"}, "plex", CONFIRMED, "list or null"),
        ("on", "plex", CONFIRMED, "must be an object"),
    ],
)
def test_invalid_blocks_are_refused(raw, server_type, markers, error):
    block, err = ls.validate_server_loudness(raw, server_type, markers)
    assert block is None and error in err


@pytest.mark.parametrize("markers", [{}, {"enabled": False}, {"plex": {"db_write_confirmed_at": None}}, CONFIRMED])
def test_loudness_opt_in_is_independent_of_marker_settings(markers):
    assert ls.load_server_loudness(_cfg({"enabled": True}, markers=markers)).enabled is True
    assert ls.load_server_loudness(_cfg({}, markers=markers)).enabled is False


def test_an_unsupported_helper_block_reads_as_off_and_is_warned_about_once(monkeypatch):
    monkeypatch.setattr(ls, "_warned", set())
    warnings = []
    sink = ls.logger.add(warnings.append, level="WARNING")
    try:
        for _ in range(3):
            cfg = _cfg({"enabled": True}, markers={"plex": {"agent": {"enabled": True}}})
            assert ls.load_server_loudness(cfg).enabled is False
    finally:
        ls.logger.remove(sink)
    assert len(warnings) == 1


def test_enabled_anywhere_skips_disabled_servers():
    cfg = _cfg({"enabled": True})
    assert ls.loudness_enabled_anywhere([cfg]) is True
    cfg.enabled = False
    assert ls.loudness_enabled_anywhere([cfg]) is False
