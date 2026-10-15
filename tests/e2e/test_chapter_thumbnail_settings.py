"""Real browser/form/API/disk round-trip for the independent chapter opt-in."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_journey_edit_existing_server import _seeded_server


def _server():
    server = _seeded_server()
    server["markers"] = {
        "enabled": False,
        "plex": {
            "agent": {
                "enabled": True,
                "url": "http://127.0.0.1:1",
                "token": "chapter-ui-fixture-key-0123456789",
            }
        },
    }
    return server


@pytest.mark.e2e
@pytest.mark.parametrize("backend_real_app", [{"media_servers": [_server()]}], indirect=True)
def test_chapter_toggle_persists_without_enabling_markers_or_losing_helper_key(backend_real_app, backend_real_page):
    url, config_dir = backend_real_app
    page = backend_real_page
    for previous, desired in [(False, True), (True, False), (False, True)]:
        page.goto(url + "/servers")
        page.locator(".edit-server-btn[data-id='plex-edit-test']").click()
        expect(page.locator("#editServerModal")).to_be_visible()
        page.locator("[data-bs-target='#edit-tab-processing']").click()
        toggle = page.locator("#editPlexChapterThumbnails")
        expect(toggle).to_be_checked(checked=previous)
        toggle.set_checked(desired)
        page.locator("#editServerSave").click()
        expect(page.locator("#editServerModal")).to_be_hidden(timeout=10000)
        saved = json.loads((Path(config_dir) / "settings.json").read_text())["media_servers"][0]
        assert saved["output"]["chapter_thumbnails"] is desired
        assert saved["markers"]["enabled"] is False
        # Migration v24 moved the helper onto a node: the key lives on the node the server points at.
        settings = json.loads((Path(config_dir) / "settings.json").read_text())
        node_id = saved["markers"]["plex"]["agent"]["node_id"]
        (node,) = [n for n in settings["nodes"] if n["id"] == node_id]
        assert node["key"] == "chapter-ui-fixture-key-0123456789"
    page.reload()
    page.locator(".edit-server-btn[data-id='plex-edit-test']").click()
    page.locator("[data-bs-target='#edit-tab-processing']").click()
    expect(page.locator("#editPlexChapterThumbnails")).to_be_checked()
