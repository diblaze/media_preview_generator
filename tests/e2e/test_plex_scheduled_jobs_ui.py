"""E2E: Plex jobs set to "As a scheduled task" hold new previews until Plex's maintenance window (#413).

Covers the Setup Health row (built by the real backend builder so the test tracks the copy), the Servers card
pill, the "waiting" note on Library health, and the Review & fix dialog sentence. Plex itself is mocked with
``page.route``; Library health reads a seeded store.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator

import pytest
from playwright.sync_api import Page, Route, expect

from media_preview_generator.library_health.models import (
    Cell,
    CellState,
    Feature,
    LibraryResult,
    ServerResult,
    TodoFile,
)
from media_preview_generator.library_health.store import HealthStore
from media_preview_generator.servers.plex_scheduled_jobs import HOLD_PREVIEWS_CHECK_ID, readiness_check

from ._mocks import mock_server_connection_probe
from .conftest import expect_modal_shown, watch_modal_shown

SERVER_ID = "plex-1"
LIBRARY_HEALTH_SERVER_ID = "e2e-plex"


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _fulfill(route: Route, body: object, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _plex_server() -> dict:
    return {
        "id": SERVER_ID,
        "type": "plex",
        "name": "Plex Lab",
        "enabled": True,
        "url": "http://plex.invalid:32400",
        "auth": {"method": "token"},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": [{"id": "1", "name": "Movies", "kind": "movie", "enabled": True, "remote_paths": []}],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {"adapter": "plex_bundle", "plex_config_folder": "/tmp"},
        "markers": {"enabled": False, "library_ids": None},
    }


def _readiness_payload() -> dict:
    check = readiness_check(
        {"GenerateIntroMarkerBehavior": "scheduled", "ButlerStartHour": 2, "ButlerEndHour": 5},
        {"1": {"enableIntroMarkerGeneration": True}},
        ["1"],
        set(),
        can_write=True,
    )
    assert check is not None and check["id"] == HOLD_PREVIEWS_CHECK_ID
    section = {
        "id": "library_settings",
        "title": "Library settings",
        "docs_anchor": "library-settings",
        "ok": False,
        "severity": "recommended",
        "checks": [check],
    }
    return {"vendor": "plex", "overall_ok": False, "sections": [section]}


def _mock_servers(page: Page) -> dict:
    """Route the Servers page for one Plex server; returns the captured plex-scheduled-jobs POSTs."""
    server = _plex_server()
    captured: dict = {"posts": []}

    def single(route: Route) -> None:
        _fulfill(route, server) if route.request.method == "GET" else route.continue_()

    def listing(route: Route) -> None:
        _fulfill(route, {"servers": [server]}) if route.request.method == "GET" else route.continue_()

    def jobs(route: Route) -> None:
        captured["posts"].append(route.request.method)
        _fulfill(route, {"ok": True})

    mock_server_connection_probe(page, ok=True, server_name=server["name"])
    page.route("**/api/servers", listing)
    page.route(f"**/api/servers/{SERVER_ID}", single)
    page.route(f"**/api/servers/{SERVER_ID}/plex-scheduled-jobs", jobs)
    page.route("**/api/servers/*/previews-readiness", lambda route: _fulfill(route, _readiness_payload()))
    return captured


@pytest.mark.e2e
class TestSetupHealthRow:
    def test_row_is_recommended_and_fix_posts_after_confirm(self, authed_page: Page, app_url: str) -> None:
        captured = _mock_servers(authed_page)
        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        edit_btn = authed_page.locator(f".edit-server-btn[data-id='{SERVER_ID}']")
        edit_btn.wait_for(state="visible", timeout=10000)
        watch_modal_shown(authed_page, "editServerModal")
        edit_btn.click()
        expect_modal_shown(authed_page, "editServerModal")
        authed_page.locator('#editServerModal [data-bs-target="#edit-tab-health"]').click()
        expect(authed_page.locator("#edit-tab-health")).to_be_visible(timeout=5000)

        recommended = authed_page.locator("#editReadinessBody details[data-tier='recommended']")
        expect(recommended).to_contain_text("New previews only show in Plex after its maintenance window", timeout=5000)
        expect(authed_page.locator("#editReadinessBody details[data-tier='critical']")).to_have_count(0)
        recommended.evaluate("el => { el.open = true; }")
        button = recommended.get_by_role("button", name="Run them when media is added")
        expect(button).to_be_visible()
        assert captured["posts"] == []

        button.click()
        modal = authed_page.locator("#readinessConfirmModal")
        expect(modal).to_contain_text("Intro markers")
        modal.get_by_role("button", name="Confirm", exact=True).click()

        expect(authed_page.locator(".toast", has_text="Applied")).to_be_visible()
        assert captured["posts"] == ["POST"]


@pytest.mark.e2e
class TestServerCardPill:
    def test_card_shows_amber_to_fix_pill(self, authed_page: Page, app_url: str) -> None:
        _mock_servers(authed_page)
        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Plex Lab", timeout=5000)

        glyph = authed_page.locator(".server-readiness-glyph").first
        expect(glyph).to_be_visible(timeout=5000)
        glyph_class = glyph.get_attribute("class") or ""
        assert "pill" in glyph_class and "warn" in glyph_class, glyph_class
        expect(glyph).to_have_text("1 to fix")


def _health_result(waiting: int, not_showing: int) -> ServerResult:
    movies = LibraryResult(
        library_id="1",
        name="Movies",
        kind="movie",
        total=20,
        cells={
            Feature.PREVIEWS: Cell(
                CellState.COUNTED, total=20, done=20 - waiting - not_showing, waiting=waiting, not_showing=not_showing
            ),
            Feature.LOUDNESS: Cell(CellState.COUNTED, total=20, done=20),
            Feature.INTRO: Cell(CellState.NOT_APPLICABLE, reason="Not used for films"),
            Feature.CREDITS: Cell(CellState.OFF, reason="Off for this library"),
        },
    )
    todo = [
        TodoFile(Feature.PREVIEWS, f"/media/Movies/Shown {i}/Shown {i}.mkv", f"Shown {i}", f"{i}", True, "1")
        for i in range(1, not_showing + 1)
    ]
    return ServerResult(
        server_id=LIBRARY_HEALTH_SERVER_ID,
        name="Plex",
        type="plex",
        libraries=[movies],
        todo=todo,
        access="Reads Plex's database directly",
        checked_at=time.time() - 120,
        duration_s=12,
    )


@pytest.fixture
def health_store(tmp_path_factory) -> Generator[HealthStore, None, None]:
    config_dir = next(tmp_path_factory.getbasetemp().glob("config_main*"))
    store = HealthStore(str(config_dir / "library_health.db"))
    yield store
    store.remove_missing_servers(set())


@pytest.mark.e2e
class TestLibraryHealthWaiting:
    NOTE = "12 show in Plex after its maintenance window"

    def test_waiting_previews_show_a_note_and_no_amber_button(
        self, authed_page: Page, app_url: str, health_store: HealthStore
    ) -> None:
        health_store.replace_server(_health_result(waiting=12, not_showing=0))
        authed_page.goto(f"{app_url}/library-health")

        card = authed_page.locator(".lh-server")
        expect(card.locator(".lh-note", has_text=self.NOTE)).to_be_visible()
        expect(card.get_by_role("button", name="not showing in Plex")).to_have_count(0)
        expect(authed_page.locator(".lh-attn")).to_have_count(0)

    def test_not_showing_previews_still_get_the_amber_button(
        self, authed_page: Page, app_url: str, health_store: HealthStore
    ) -> None:
        health_store.replace_server(_health_result(waiting=0, not_showing=3))
        authed_page.goto(f"{app_url}/library-health")

        card = authed_page.locator(".lh-server")
        expect(card.get_by_role("button", name="3 not showing in Plex")).to_be_visible()
        expect(card).not_to_contain_text("maintenance window")

    def test_review_and_fix_body_mentions_scheduled_jobs(
        self, authed_page: Page, app_url: str, health_store: HealthStore
    ) -> None:
        health_store.replace_server(_health_result(waiting=0, not_showing=3))
        authed_page.goto(f"{app_url}/library-health")
        authed_page.get_by_role("button", name="Review & fix").click()

        dialog = authed_page.locator("#lhFixModal")
        expect(dialog).to_be_visible()
        expect(dialog.locator("#lhFixBody")).to_contain_text(
            'Plex will also start any of its jobs set to "As a scheduled task"'
        )
