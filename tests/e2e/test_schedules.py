"""E2E tests for the Schedules UI on /automation.

Phase E regression: the create-schedule and update-schedule endpoints used
to reject any schedule pinned to a non-Plex server with a 400. After the
multi-server completion every vendor's processor implements
scan_recently_added + list_canonical_paths, so non-Plex schedules now
save and run.

These tests exercise the UI end-to-end against mocked /api/schedules and
/api/servers responses to confirm the previously-blocked options now go
through cleanly.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status, mock_servers_list
from .conftest import expect_modal_shown, watch_modal_shown


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _seed_servers_for_schedule_modal(page: Page) -> None:
    """Common setup for the schedule modal: dashboard defaults + server list +
    /api/schedules with one Jellyfin schedule and one Plex schedule already in
    place so the table renders with vendor badges."""
    mock_dashboard_defaults(page)
    servers = [
        {
            "id": "plex-1",
            "name": "Home Plex",
            "type": "plex",
            "enabled": True,
            "status": "connected",
            "url": "http://plex.local:32400",
        },
        {
            "id": "jf-1",
            "name": "Home Jellyfin",
            "type": "jellyfin",
            "enabled": True,
            "status": "connected",
            "url": "http://jf.local:8096",
        },
        {
            "id": "emby-1",
            "name": "Home Emby",
            "type": "emby",
            "enabled": True,
            "status": "connected",
            "url": "http://emby.local:8096",
        },
    ]
    mock_media_servers_status(page, servers=servers)
    mock_servers_list(page, servers=servers)
    # Libraries endpoint: one library per server, grouped by server in the dialog.
    page.route(
        "**/api/libraries**",
        lambda r: _fulfill_json(
            r,
            {
                "libraries": [
                    {
                        "id": "1",
                        "name": "TV Shows",
                        "type": "show",
                        "server_id": "plex-1",
                        "server_name": "Home Plex",
                        "server_type": "plex",
                    },
                    {
                        "id": "lib-1",
                        "name": "Movies",
                        "type": "movie",
                        "server_id": "jf-1",
                        "server_name": "Home Jellyfin",
                        "server_type": "jellyfin",
                    },
                    {
                        "id": "lib-2",
                        "name": "Films",
                        "type": "movie",
                        "server_id": "emby-1",
                        "server_name": "Home Emby",
                        "server_type": "emby",
                    },
                ]
            },
        ),
    )
    # Schedules list (empty initially so the table renders without
    # cluttering up the assertions).
    page.route(
        "**/api/schedules",
        lambda r: _fulfill_json(r, {"schedules": []}) if r.request.method == "GET" else r.continue_(),
    )


def _tick_library(page: Page, server_id: str) -> None:
    """Untick All Libraries (which enables the rows), then tick that server's library."""
    expect(page.locator(f'.schedule-library-checkbox[data-server-id="{server_id}"]')).to_be_attached(timeout=3000)
    page.locator("#scheduleLibraryAll").uncheck()
    page.locator(f'.schedule-library-checkbox[data-server-id="{server_id}"]').check()


def _open_new_schedule(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/automation#schedules")
    page.wait_for_load_state("domcontentloaded")
    page.locator('button:has-text("Add Schedule")').first.click()
    expect(page.locator("#newScheduleForm")).to_be_visible(timeout=2000)


@pytest.mark.e2e
class TestScheduleNonPlex:
    def test_save_recently_added_schedule_against_jellyfin_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for recently_added. Now it saves cleanly."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-1", "name": "Recent JF", "server_id": "jf-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        # Click "Add Schedule" — opens the modal.
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        # Fill the form: name + Recently added + the Jellyfin library (the server is inferred from it).
        authed_page.locator("#scheduleName").fill("Recent JF")
        authed_page.locator("#scheduleFilesRecent").check()
        _tick_library(authed_page, "jf-1")
        # Submit — the form's primary button.
        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        # Wait for POST to fire so the assertion below isn't racing it.
        with authed_page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/schedules"), timeout=5000
        ) as req_info:
            save_btn.click()
        req_info.value  # noqa: B018 — the response landed, so the mock recorded the body

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "jf-1", body
        cfg = body.get("config") or {}
        assert cfg == {
            "job_kinds": ["previews"],
            "job_type": "recently_added",
            "lookback_hours": 1,
            "libraries": [{"server_id": "jf-1", "library_id": "lib-1"}],
        }, body

    def test_save_full_library_schedule_against_emby_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for full_library too."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-2", "name": "Nightly Emby", "server_id": "emby-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        authed_page.locator("#scheduleName").fill("Nightly Emby")
        # Whole library is the default; the server is inferred from the ticked library.
        _tick_library(authed_page, "emby-1")

        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        with authed_page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/schedules"), timeout=5000
        ) as req_info:
            save_btn.click()
        req_info.value  # noqa: B018 — the response landed, so the mock recorded the body

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "emby-1", body
        cfg = body.get("config") or {}
        assert cfg["job_kinds"] == ["previews"], body
        assert cfg["job_type"] == "full_library", body
        assert cfg["sort_by"] == "default", body


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("config", "expected_order"),
    [
        ({}, "inherit"),
        ({"sort_by": None}, "default"),
        ({"sort_by": ""}, "default"),
        ({"sort_by": "default"}, "default"),
        ({"sort_by": "newest"}, "newest"),
        ({"sort_by": "oldest"}, "oldest"),
        ({"sort_by": "random"}, "random"),
    ],
)
def test_edit_schedule_preserves_processing_order(
    authed_page: Page, app_url: str, config: dict, expected_order: str
) -> None:
    """Older schedules with no override keep inheriting the configured order."""
    _seed_servers_for_schedule_modal(authed_page)
    authed_page.route(
        "**/api/schedules",
        lambda route: _fulfill_json(
            route,
            {
                "schedules": [
                    {
                        "id": "order",
                        "name": "Order",
                        "config": {"job_kinds": ["previews"], "job_type": "full_library", **config},
                        "cron_expression": "0 3 * * *",
                    }
                ]
            },
        ),
    )
    authed_page.goto(f"{app_url}/automation#schedules")
    authed_page.wait_for_function("schedules.some(s => s.id === 'order')")
    authed_page.evaluate("showEditScheduleModal('order')")
    expect(authed_page.locator("#scheduleSortBy")).to_have_value(expected_order)
    authed_page.route("**/api/schedules/order", lambda route: _fulfill_json(route, {"id": "order"}))
    with authed_page.expect_request("**/api/schedules/order") as saved:
        authed_page.locator("#scheduleSubmitBtn").click()
    saved_config = saved.value.post_data_json["config"]
    if expected_order == "inherit":
        assert "sort_by" not in saved_config
    else:
        assert saved_config["sort_by"] == expected_order


@pytest.mark.e2e
class TestScheduleLibrariesReuseStartJobPicker:
    def test_libraries_group_by_server_and_the_server_dropdown_is_gone(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _open_new_schedule(authed_page, app_url)

        expect(authed_page.locator("#scheduleLibraryList .ov-lg")).to_have_count(3, timeout=3000)
        expect(authed_page.locator("#scheduleLibraryList")).to_contain_text("Home Jellyfin")
        expect(authed_page.locator("#scheduleServer")).to_have_count(0)
        expect(authed_page.locator("#scheduleScopeBadge")).to_contain_text("all servers")

    def test_scope_badge_names_the_server_when_every_tick_is_on_one(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _open_new_schedule(authed_page, app_url)
        _tick_library(authed_page, "emby-1")

        expect(authed_page.locator("#scheduleScopeBadge")).to_contain_text("Home Emby")
        authed_page.locator('.schedule-library-checkbox[data-server-id="jf-1"]').check()
        expect(authed_page.locator("#scheduleScopeBadge")).to_contain_text("2 servers")

    def test_unticking_every_library_blocks_the_save_with_an_inline_error(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        _open_new_schedule(authed_page, app_url)
        authed_page.locator("#scheduleName").fill("No libs")
        expect(authed_page.locator('.schedule-library-checkbox[data-server-id="jf-1"]')).to_be_attached(timeout=3000)
        authed_page.locator("#scheduleLibraryAll").uncheck()

        authed_page.locator("#scheduleSubmitBtn").click()

        expect(authed_page.locator("#scheduleLibrariesError")).to_be_visible()
        assert captured == []


_INTRO_SCHEDULE = {
    "id": "sch-ic",
    "name": "Weekly Intro & Credits",
    "enabled": True,
    "trigger_type": "cron",
    "trigger_value": "0 3 * * 6",
    "library_id": None,
    "library_ids": [],
    "library_name": "All Libraries",
    "server_id": None,
    "priority": None,
    "stop_time": "",
    # What normalize_schedule_config makes of a legacy {"job_type": "intro_credits"} schedule.
    "config": {"job_kinds": ["intro_credits"], "job_type": "full_library"},
    "next_run": None,
}

_ALL_KINDS = ["previews", "intro_credits", "loudness"]


def _capture_schedule_writes(page: Page, schedules: list[dict]) -> list[tuple[str, dict]]:
    """GET /api/schedules serves ``schedules``; POST and PUT bodies are captured as ``(method, body)``."""
    captured: list[tuple[str, dict]] = []

    def handler(route: Route) -> None:
        method = route.request.method
        if method in ("POST", "PUT"):
            captured.append((method, route.request.post_data_json or {}))
            _fulfill_json(route, {"id": "sch-ic", "enabled": True}, status=201 if method == "POST" else 200)
        else:
            _fulfill_json(route, {"schedules": schedules})

    page.route("**/api/schedules", handler)
    page.route("**/api/schedules/*", handler)
    return captured


def _save_schedule(page: Page, path: str, method: str = "POST") -> None:
    """Click Save and wait for the write's RESPONSE: waiting for the request would let a caller's assertion run
    before ``_capture_schedule_writes`` recorded the body (it flakes under ``-n 8``)."""
    with page.expect_response(lambda r: r.request.method == method and r.url.endswith(path), timeout=5000):
        page.locator("#scheduleSubmitBtn").click()


@pytest.mark.e2e
class TestScheduleMultiKind:
    def test_dialog_starts_on_previews_whole_library_with_default_priority(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _open_new_schedule(authed_page, app_url)

        expect(authed_page.locator("#scheduleKindPreviews")).to_be_checked()
        expect(authed_page.locator("#scheduleKindMarkers")).not_to_be_checked()
        expect(authed_page.locator("#scheduleKindLoudness")).not_to_be_checked()
        expect(authed_page.locator("#scheduleFilesAll")).to_be_checked()
        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_hidden()
        expect(authed_page.locator("#schedulePriority")).to_have_value("")
        expect(authed_page.locator("#newScheduleModal .ov-hint", has_text="run Low")).to_be_visible()
        expect(authed_page.locator("#scheduleSummary")).to_contain_text("Whole library")
        # Info icons on the new controls use the app's tooltip pattern.
        for selector in ("#scheduleFilesGroup .info-icon", "#scheduleKindMarkersInfo", "#scheduleKindLoudnessInfo"):
            expect(authed_page.locator(selector)).to_have_attribute("data-bs-toggle", "tooltip")

    def test_ticking_all_three_kinds_saves_them_in_canonical_order_with_one_priority(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        _open_new_schedule(authed_page, app_url)
        authed_page.locator("#scheduleName").fill("Nightly: everything")
        authed_page.locator("#scheduleKindLoudness").check()
        authed_page.locator("#scheduleKindMarkers").check()
        authed_page.locator("#schedulePriority").evaluate(
            "el => { el.value = '1'; el.dispatchEvent(new Event('change', {bubbles: true})); }"
        )

        expect(authed_page.locator("#scheduleSummary .ov-task")).to_have_count(3)
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_visible()
        _save_schedule(authed_page, "/api/schedules")

        assert [method for method, _ in captured] == ["POST"]
        body = captured[0][1]
        assert body["config"] == {
            "job_kinds": _ALL_KINDS,
            "job_type": "full_library",
            "sort_by": "default",
            "added_filter": "all",
        }
        assert body["priority"] == 1
        assert (body["library_ids"], body["server_id"]) == ([], "")
        assert "libraries" not in body["config"]

    def test_without_previews_the_order_is_hidden_the_filters_stay_and_no_sort_is_saved(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        _open_new_schedule(authed_page, app_url)
        authed_page.locator("#scheduleName").fill("Markers nightly")
        authed_page.locator("#scheduleKindMarkers").check()
        authed_page.locator("#scheduleKindPreviews").uncheck()

        expect(authed_page.locator("#scheduleSortByGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleScanFilters")).to_be_visible()
        _save_schedule(authed_page, "/api/schedules")

        assert captured[0][1]["config"] == {
            "job_kinds": ["intro_credits"],
            "job_type": "full_library",
            "added_filter": "all",
        }

    def test_the_last_ticked_kind_cannot_be_unticked(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _open_new_schedule(authed_page, app_url)

        authed_page.locator("#scheduleKindPreviews").click()

        expect(authed_page.locator("#scheduleKindPreviews")).to_be_checked()

    def test_recently_added_without_previews_saves_a_list_only_schedule(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        _open_new_schedule(authed_page, app_url)
        authed_page.locator("#scheduleName").fill("New arrivals: markers")
        authed_page.locator("#scheduleKindMarkers").check()
        authed_page.locator("#scheduleKindPreviews").uncheck()
        authed_page.locator("#scheduleFilesRecent").check()

        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_visible()
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_hidden()
        expect(authed_page.locator("#scheduleScanFilters")).to_be_hidden()
        expect(authed_page.locator("#scheduleFilesHint")).to_contain_text("No previews are made")
        # Recently added nudges an untouched trigger to every 15 minutes.
        expect(authed_page.locator("#scheduleTypeInterval")).to_be_checked()
        expect(authed_page.locator("#scheduleIntervalValue")).to_have_value("15")
        authed_page.locator("#scheduleLookback").select_option("6")
        _save_schedule(authed_page, "/api/schedules")

        body = captured[0][1]
        assert body["config"] == {"job_kinds": ["intro_credits"], "job_type": "recently_added", "lookback_hours": 6}
        assert body["interval_minutes"] == 15

    def test_recently_added_with_previews_saves_every_ticked_kind_and_no_filters(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [])
        _open_new_schedule(authed_page, app_url)
        authed_page.locator("#scheduleName").fill("New arrivals")
        authed_page.locator("#scheduleKindMarkers").check()
        authed_page.locator("#scheduleFilesRecent").check()

        expect(authed_page.locator("#scheduleFilesHint")).to_contain_text("makes previews")
        _save_schedule(authed_page, "/api/schedules")

        assert captured[0][1]["config"] == {
            "job_kinds": ["previews", "intro_credits"],
            "job_type": "recently_added",
            "lookback_hours": 1,
        }

    def test_check_servers_is_no_longer_offered(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        _open_new_schedule(authed_page, app_url)

        expect(authed_page.locator("#scheduleMarkersCheckServers")).to_have_count(0)
        expect(authed_page.locator("#scheduleCheckServersNote")).to_have_count(0)


@pytest.mark.e2e
class TestScheduleListAndEditRoundTrip:
    def test_legacy_intro_credits_schedule_lists_its_chip_and_round_trips_through_edit(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        captured = _capture_schedule_writes(authed_page, [_INTRO_SCHEDULE])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits")
        expect(row.locator(".schedule-kind-badge")).to_have_text(["Intro & Credits"], timeout=3000)
        expect(row.locator(".schedule-files-label")).to_have_text("Whole library")
        assert "Low" in (row.locator(".priority-badge").get_attribute("title") or "")

        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        expect(authed_page.locator("#scheduleKindMarkers")).to_be_checked()
        expect(authed_page.locator("#scheduleKindPreviews")).not_to_be_checked()
        expect(authed_page.locator("#scheduleFilesAll")).to_be_checked()
        expect(authed_page.locator("#scheduleSortByGroup")).to_be_hidden()
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        assert [method for method, _ in captured] == ["PUT"]
        assert captured[0][1]["config"] == {
            "job_kinds": ["intro_credits"],
            "job_type": "full_library",
            "added_filter": "all",
        }
        assert captured[0][1]["priority"] is None

    def test_legacy_recently_added_schedule_shows_every_kind_and_keeps_its_lookback(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        # What normalize_schedule_config makes of a legacy {"job_type": "recently_added", "lookback_hours": 6}.
        schedule = {
            **_INTRO_SCHEDULE,
            "name": "New arrivals",
            "config": {"job_kinds": _ALL_KINDS, "job_type": "recently_added", "lookback_hours": 6},
        }
        captured = _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="New arrivals")
        expect(row.locator(".schedule-kind-badge")).to_have_text(
            ["Previews", "Intro & Credits", "Plex loudness"], timeout=3000
        )
        expect(row.locator(".schedule-files-label")).to_have_text("Recently added, last 6 hours")

        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)
        for kind_id in ("scheduleKindPreviews", "scheduleKindMarkers", "scheduleKindLoudness"):
            expect(authed_page.locator(f"#{kind_id}")).to_be_checked()
        expect(authed_page.locator("#scheduleFilesRecent")).to_be_checked()
        expect(authed_page.locator("#scheduleLookback")).to_have_value("6")
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        assert captured[0][1]["config"] == {
            "job_kinds": _ALL_KINDS,
            "job_type": "recently_added",
            "lookback_hours": 6,
        }

    def test_edit_preselects_the_saved_libraries_and_keeps_the_server_pin(
        self, authed_page: Page, app_url: str
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        schedule = {
            **_INTRO_SCHEDULE,
            "library_id": "lib-2",
            "library_ids": ["lib-2"],
            "library_name": "Films",
            "server_id": "emby-1",
        }
        captured = _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits").locator(
            'button[aria-label="Edit schedule"]'
        ).click()

        expect(authed_page.locator('.schedule-library-checkbox[data-server-id="emby-1"]')).to_be_checked(timeout=3000)
        expect(authed_page.locator("#scheduleLibraryAll")).not_to_be_checked()
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        body = captured[0][1]
        assert (body["library_ids"], body["library_id"], body["server_id"]) == (["lib-2"], "lib-2", "emby-1")
        assert body["config"]["libraries"] == [{"server_id": "emby-1", "library_id": "lib-2"}]

    @staticmethod
    def _open_pinned_edit(authed_page: Page, app_url: str, **extra) -> list[tuple[str, dict]]:
        _seed_servers_for_schedule_modal(authed_page)
        schedule = {
            **_INTRO_SCHEDULE,
            "library_id": "lib-2",
            "library_ids": ["lib-2"],
            "library_name": "Films",
            "server_id": "emby-1",
            **extra,
        }
        captured = _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits").locator(
            'button[aria-label="Edit schedule"]'
        ).click()
        expect(authed_page.locator('.schedule-library-checkbox[data-server-id="emby-1"]')).to_be_checked(timeout=3000)
        return captured

    def test_editing_a_pinned_schedule_to_all_libraries_clears_the_server_pin(
        self, authed_page: Page, app_url: str
    ) -> None:
        captured = self._open_pinned_edit(authed_page, app_url)
        authed_page.locator("#scheduleLibraryAll").check()
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        body = captured[0][1]
        assert body["server_id"] == ""
        assert body["library_ids"] == []
        assert "libraries" not in body["config"]

    def test_editing_a_pinned_schedule_to_libraries_on_two_servers_clears_the_server_pin(
        self, authed_page: Page, app_url: str
    ) -> None:
        captured = self._open_pinned_edit(authed_page, app_url)
        authed_page.locator('.schedule-library-checkbox[data-server-id="plex-1"]').check()
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        body = captured[0][1]
        assert body["server_id"] == ""
        assert sorted(body["library_ids"]) == ["1", "lib-2"]
        assert body["config"]["libraries"] == [
            {"server_id": "plex-1", "library_id": "1"},
            {"server_id": "emby-1", "library_id": "lib-2"},
        ]

    def test_edit_preselects_picks_on_two_servers_from_the_saved_pairs(self, authed_page: Page, app_url: str) -> None:
        pairs = [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "emby-1", "library_id": "lib-2"}]
        captured = self._open_pinned_edit(
            authed_page,
            app_url,
            library_ids=["1", "lib-2"],
            library_id=None,
            server_id=None,
            config={"job_kinds": ["loudness"], "job_type": "full_library", "libraries": pairs},
        )
        expect(authed_page.locator('.schedule-library-checkbox[data-server-id="plex-1"]')).to_be_checked()
        expect(authed_page.locator('.schedule-library-checkbox[data-server-id="jf-1"]')).not_to_be_checked()
        _save_schedule(authed_page, "/api/schedules/sch-ic", method="PUT")

        body = captured[0][1]
        assert (body["server_id"], body["config"]["libraries"]) == ("", pairs)

    def test_a_new_schedule_after_editing_starts_from_the_defaults(self, authed_page: Page, app_url: str) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        schedule = {**_INTRO_SCHEDULE, "config": {"job_kinds": _ALL_KINDS, "job_type": "recently_added"}}
        _capture_schedule_writes(authed_page, [schedule])
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        row = authed_page.locator("#scheduleList tr", has_text="Weekly Intro & Credits")
        # Bootstrap ignores a close click while the modal is still opening (its dialog slides in after the fade).
        watch_modal_shown(authed_page, "newScheduleModal")
        row.locator('button[aria-label="Edit schedule"]').click()
        expect(authed_page.locator("#scheduleFilesRecent")).to_be_checked(timeout=2000)
        expect_modal_shown(authed_page, "newScheduleModal")
        authed_page.locator("#newScheduleModal .btn-close").click()
        expect(authed_page.locator("#newScheduleModal")).to_be_hidden(timeout=3000)

        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        expect(authed_page.locator("#scheduleKindPreviews")).to_be_checked()
        expect(authed_page.locator("#scheduleKindMarkers")).not_to_be_checked()
        expect(authed_page.locator("#scheduleKindLoudness")).not_to_be_checked()
        expect(authed_page.locator("#scheduleFilesAll")).to_be_checked()
        expect(authed_page.locator("#scheduleLookbackGroup")).to_be_hidden()


@pytest.mark.e2e
class TestScheduleListRowLayout:
    """Each row keeps the job chips under the name, which files under the library, and the schedule in words."""

    def test_row_puts_files_under_library_and_reads_schedule_and_next_run_in_words(
        self, authed_page: Page, app_url: str, tmp_path
    ) -> None:
        _seed_servers_for_schedule_modal(authed_page)
        nightly = {
            **_INTRO_SCHEDULE,
            "id": "sch-tv",
            "name": "TV Daily",
            "trigger_value": "0 2 * * 0,1,2,3,4,5,6",
            "library_name": "TV Shows",
            "stop_time": "07:00",
            "config": {"job_kinds": _ALL_KINDS, "job_type": "full_library"},
            "next_run": "2099-01-01T02:00:00",
        }
        recent = {
            **_INTRO_SCHEDULE,
            "id": "sch-new",
            "name": "New arrivals",
            "trigger_type": "interval",
            "trigger_value": "15",
            "config": {"job_kinds": ["previews", "intro_credits"], "job_type": "recently_added", "lookback_hours": 1},
        }
        _capture_schedule_writes(authed_page, [nightly, recent])
        authed_page.set_viewport_size({"width": 1400, "height": 900})
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")

        row = authed_page.locator("#scheduleList tr", has_text="TV Daily")
        cells = row.locator("td")
        expect(cells.nth(0).locator(".sched-kinds .schedule-kind-badge")).to_have_count(3, timeout=3000)
        expect(cells.nth(0).locator(".schedule-files-label")).to_have_count(0)
        expect(cells.nth(1).locator(".schedule-files-label")).to_have_text("Whole library")
        expect(cells.nth(2).locator(".sched-main")).to_have_text("Daily at 02:00")
        expect(cells.nth(2).locator(".sched-sub")).to_have_text("Stops at 07:00")
        expect(cells.nth(4).locator(".next")).to_contain_text("Jan")
        expect(cells.nth(4)).not_to_contain_text("2099-01-01")

        recent_row = authed_page.locator("#scheduleList tr", has_text="New arrivals")
        expect(recent_row.locator("td").nth(1).locator(".schedule-files-label")).to_have_text(
            "Recently added, last 1 hour"
        )
        expect(recent_row.locator("td").nth(2).locator(".sched-main")).to_have_text("Every 15 minutes")
        authed_page.locator("#scheduleList").screenshot(path=str(tmp_path / "schedule_rows.png"))
