"""Inspector loudness readouts and live job updates through the shipped browser UI."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from . import _inspector_fixtures as fx
from ._mocks import _fulfill_json
from .test_inspector_behaviours import _SOCKET_STUB, _emit


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _stream(**overrides: object) -> dict:
    return {
        "item_id": "100",
        "part_id": "200",
        "stream_id": "300",
        "index": 1,
        "codec": "aac",
        "language": "English",
        "channels": 2,
        "title": "Stereo",
        "default": True,
        "part_file": "episode.mkv",
        "state": "available",
        "note": None,
        "integrated_lufs": -23.12,
        "true_peak_dbtp": -1.02,
        "lra_lu": 7.2,
        "threshold_lufs": -33.12,
        "gain_offset_db": 0,
        "analysis_version": "0.02",
        "normalization_available": True,
        **overrides,
    }


def _server(**overrides: object) -> dict:
    return {
        "server_id": "plex-1",
        "server_name": "Plex",
        "server_type": "plex",
        "enabled": False,
        "state": "available",
        "note": None,
        "source": "plex_api",
        "streams": [_stream()],
        **overrides,
    }


def _open(page: Page, app_url: str, loudness: list[dict], job: dict | None = None) -> fx.InspectorApi:
    file, item = fx.checked_episode()
    file.update(loudness=loudness, job=job)
    api = fx.InspectorApi()
    api.add(file, item)
    fx.install(page, api)
    page.goto(f"{app_url}/inspector?path={quote(fx.EPISODE)}")
    expect(page.locator("#inspLoading")).to_have_count(0)
    return api


@pytest.mark.e2e
class TestLoudness:
    def test_native_measurements_remain_visible_with_analysis_off(self, authed_page: Page, app_url: str) -> None:
        _open(authed_page, app_url, [_server()])
        card = authed_page.locator("#inspLoudness")
        expect(card).to_contain_text("Measurements reported by Plex")
        expect(card).to_contain_text("Loudness analysis by this app is off for this file")
        expect(card.locator(".insp-loudness-values dd")).to_have_text(
            ["-23.12 LUFS", "-1.02 dBTP", "7.2 LU", "-33.12 LUFS", "0 dB"]
        )
        expect(card).to_contain_text("Analysis version 0.02 · Normalization available")
        expect(card.locator(".insp-loudness-track-name")).to_have_text(
            "Audio track 1 · Stereo · English · AAC · 2 channels · Default"
        )
        expect(card.get_by_role("button")).to_have_count(0)

    @pytest.mark.parametrize(
        ("state", "label", "note"),
        [
            ("not_analysed", "Not analysed", "Plex has not reported loudness measurements."),
            ("partial", "Partial measurements", "Plex reported only part of the measurement."),
            ("unavailable", "Couldn’t read loudness", "Could not read this file from Plex."),
            ("unsupported", "Unsupported", "Loudness is only supported on Plex."),
        ],
    )
    def test_read_states_are_distinct(self, authed_page: Page, app_url: str, state: str, label: str, note: str) -> None:
        _open(authed_page, app_url, [_server(state=state, note=note, enabled=True, streams=[])])
        card = authed_page.locator("#inspLoudness")
        expect(card.locator(".insp-loudness-status")).to_have_text(label)
        expect(card).to_contain_text(note)
        expect(card.locator("dl")).to_have_count(0)
        expect(card).not_to_contain_text("analysis by this app is off")

    def test_silence_partial_and_normalization_unknown_do_not_become_zero(
        self, authed_page: Page, app_url: str
    ) -> None:
        _open(
            authed_page,
            app_url,
            [
                _server(
                    streams=[
                        _stream(
                            integrated_lufs="-inf",
                            true_peak_dbtp="-inf",
                            threshold_lufs=None,
                            lra_lu=None,
                            gain_offset_db=None,
                            normalization_available=None,
                            state="partial",
                        )
                    ]
                )
            ],
        )
        card = authed_page.locator("#inspLoudness")
        expect(card.locator("dd")).to_have_text(["−∞ LUFS", "−∞ dBTP", "Not reported", "Not reported", "Not reported"])
        expect(card).to_contain_text("Normalization not reported")

    def test_multiple_tracks_servers_and_parts_escape_text(self, authed_page: Page, app_url: str) -> None:
        unsafe = '<img src=x onerror="window.__loudnessXss = true">'
        first = _server(
            server_name=unsafe,
            streams=[
                _stream(title=unsafe),
                _stream(
                    stream_id="301",
                    title="Commentary",
                    language="French",
                    normalization_available=False,
                ),
                _stream(item_id="101", part_id="201", stream_id="302", part_file=unsafe),
            ],
        )
        second = _server(server_id="plex-2", server_name="Second Plex", streams=[_stream(integrated_lufs=-19)])
        _open(authed_page, app_url, [first, second])
        card = authed_page.locator("#inspLoudness")
        expect(card.locator(".insp-loudness-track")).to_have_count(4)
        expect(card).to_contain_text("Plex item 100 · Part 200")
        expect(card).to_contain_text("Plex item 101 · Part 201")
        expect(card).to_contain_text("Normalization unavailable")
        expect(card.locator("[data-server-id='plex-2']")).to_contain_text("-19 LUFS")
        expect(card).to_contain_text(unsafe)
        expect(card.locator("img")).to_have_count(0)
        assert authed_page.evaluate("window.__loudnessXss") is None

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_loudness_job_is_labelled_and_refreshes_measurements(
        self, authed_page: Page, app_url: str, status: str
    ) -> None:
        authed_page.add_init_script(_SOCKET_STUB)
        api = _open(
            authed_page,
            app_url,
            [_server(state="not_analysed", streams=[])],
            {
                "id": "loudness-1",
                "kind": "loudness",
                "status": status,
                "name": "Audio analysis",
                "percent": 25,
            },
        )
        banner = authed_page.locator("#inspJobBanner")
        expect(banner).to_contain_text("Plex loudness job")
        _emit(authed_page, "job_progress", {"job_id": "loudness-1", "progress": {"percent": 73}})
        expect(banner).to_contain_text("73%")
        api.files[fx.EPISODE].update(job=None, loudness=[_server()])
        _emit(authed_page, "job_completed", {"id": "loudness-1", "kind": "loudness", "status": "completed"})
        expect(authed_page.locator("#inspLoudness")).to_contain_text("-23.12 LUFS")
        expect(banner).to_be_hidden()
        assert "bif=" not in authed_page.url

    @pytest.mark.parametrize("unavailable", [False, True])
    def test_progress_discovers_kind_without_inheriting_preview(
        self, authed_page: Page, app_url: str, unavailable: bool
    ) -> None:
        authed_page.add_init_script(_SOCKET_STUB)
        authed_page.route(
            "**/api/jobs/loudness-new",
            lambda route: _fulfill_json(
                route,
                {
                    "id": "loudness-new",
                    "kind": "loudness",
                    "status": "running",
                    "library_name": "Audio analysis",
                }
                if not unavailable
                else {"error": "Unavailable"},
                status=200 if not unavailable else 503,
            ),
        )
        _open(
            authed_page,
            app_url,
            [_server()],
            {
                "id": "old-preview",
                "kind": "previews",
                "status": "running",
                "name": "Preview",
                "percent": 5,
            },
        )
        _emit(
            authed_page,
            "job_progress",
            {
                "job_id": "loudness-new",
                "progress": {"current_file": fx.EPISODE, "percent": 25},
            },
        )
        banner = authed_page.locator("#inspJobBanner")
        expect(banner).to_contain_text("Processing job" if unavailable else "Plex loudness job")
        expect(banner).not_to_contain_text("Preview job")

    @pytest.mark.parametrize("theme,width", [("dark", 1440), ("light", 390)])
    def test_measurements_wrap_on_desktop_and_mobile(
        self, authed_page: Page, app_url: str, theme: str, width: int
    ) -> None:
        authed_page.set_viewport_size({"width": width, "height": 900})
        _open(
            authed_page,
            app_url,
            [
                _server(
                    streams=[
                        _stream(),
                        _stream(
                            stream_id="301",
                            title="Commentary with the filmmakers",
                            language="French",
                            default=False,
                        ),
                    ]
                )
            ],
        )
        authed_page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
        card = authed_page.locator("#inspLoudness")
        card.scroll_into_view_if_needed()
        assert card.evaluate("el => el.scrollWidth <= el.clientWidth")
        for metric in card.locator("dd").all():
            assert metric.evaluate("el => el.scrollWidth <= el.clientWidth")
        if folder := os.environ.get("INSPECTOR_SCREENSHOT_DIR"):
            Path(folder).mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(Path(folder) / f"loudness-{theme}-{width}.png"))
