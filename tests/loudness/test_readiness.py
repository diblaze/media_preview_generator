"""Loudness health reports unusable local writers independently of markers."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.servers.loudness_readiness import loudness_readiness_section

from .test_settings import _cfg


@pytest.mark.parametrize("block", [{}, {"enabled": False}])
def test_disabled_loudness_never_probes_database(block):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        assert loudness_readiness_section(MagicMock(), _cfg(block)) is None
    capability.assert_not_called()


@pytest.mark.parametrize("enabled", ["false", "true", "", 0, 1, {}, [], None])
def test_invalid_enable_is_reported_without_probing_the_database(enabled):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": enabled}))
    capability.assert_not_called()
    assert not section["ok"]
    assert section["checks"][0]["reason"] == "loudness.enabled must be a boolean"


@pytest.mark.parametrize("ready", [True, False])
def test_health_reports_capability_without_marker_consent(ready):
    config = _cfg({"enabled": True}, markers={"enabled": False})
    server = MagicMock()
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability",
        return_value=MagicMock(ready=ready, message="Wrong server"),
    ) as capability:
        section = loudness_readiness_section(server, config)
    capability.assert_called_once_with(server, config)
    assert section["ok"] is ready
    check = section["checks"][0]
    assert check["reason"] == (None if ready else "Wrong server")
    assert check["severity"] == ("recommended" if ready else "critical")
    assert check["actions"] == {}


def test_failed_probe_is_unhealthy_with_actionable_message():
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True}))
    assert not section["ok"]
    assert "Check the Plex connection" in section["checks"][0]["reason"]


def test_stored_unsupported_music_selection_is_not_reported_as_ready():
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True, "library_ids": ["3"]}))
    capability.assert_not_called()
    assert not section["ok"]
    assert "movie and TV" in section["checks"][0]["reason"]
