"""Worker groups with node devices: the grouped device picker, node chips and zero-capacity reasons."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_dashboard_defaults
from .test_worker_groups import _complete_setup, group_api, settings_page  # noqa: F401

pytestmark = pytest.mark.e2e

NODES = [
    {
        "id": "storage",
        "name": "storage",
        "state": "ready",
        "reason": "",
        "devices": [{"device": "cuda:0", "name": "Quadro P5000", "type": "nvidia", "status": "ok"}],
        "cpu_cores": 24,
    },
    {
        "id": "nuc",
        "name": "nuc",
        "state": "access_problem",
        "reason": "fix access first",
        "devices": [],
        "cpu_cores": 8,
    },
]


def offline_node_group(api: dict) -> None:
    state = api["state"]
    state["nodes"] = NODES + [
        {
            "id": "laptop",
            "name": "old-laptop",
            "state": "offline",
            "reason": "node offline",
            "devices": [{"device": "cuda:0", "name": "GTX 1650", "type": "nvidia", "status": "ok"}],
            "cpu_cores": 4,
        }
    ]
    state["groups"][1]["members"].append(
        {
            "id": "m-laptop",
            "resource": "gpu",
            "device": "cuda:0",
            "count": 1,
            "job_types": ["previews"],
            "node": "laptop",
        }
    )
    state["capacity"]["groups"][1]["members"].append(
        {
            "id": "m-laptop",
            "resource": "gpu",
            "device": "cuda:0",
            "node": "laptop",
            "node_name": "old-laptop",
            "reason": "node offline",
            "desired": 1,
            "target": 0,
            "available": 0,
            "busy": 0,
            "finishing": 0,
            "state": "node_unavailable",
        }
    )


def test_picker_lists_this_app_first_then_each_node_with_its_state(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    group_api["state"]["nodes"] = NODES
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    picker = page.locator('[data-member="m-cpu"] [data-pick]')
    labels = picker.locator("optgroup").evaluate_all("groups => groups.map(g => g.label)")
    assert labels == ["Main", "storage · Ready", "nuc · Access problem"]
    storage = picker.locator('optgroup[label="storage · Ready"] option')
    expect(storage).to_have_text(["storage · Quadro P5000", "storage · CPU (24 cores)"])
    picker.select_option("node:storage:gpu:cuda:0")
    row = page.locator('[data-member="m-cpu"]')
    expect(row.locator(".node-chip")).to_have_text("storage")
    expect(row.locator("[data-node-zero]")).to_have_count(0)
    expect(row).to_contain_text("Loudness removed — it runs on CPU only.")
    page.locator('[data-member="m-cpu"] [data-kind="previews"]').click()
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    member = group_api["writes"][-1][2]["groups"][0]["members"][0]
    assert member == {
        "id": "m-cpu",
        "resource": "gpu",
        "device": "cuda:0",
        "count": 2,
        "job_types": ["previews"],
        "node": "storage",
    }


def test_member_on_a_node_that_cannot_work_shows_why_it_has_zero(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    group_api["state"]["nodes"] = NODES
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("node:nuc:cpu")
    row = page.locator('[data-member="m-cpu"]')
    expect(row.locator(".node-chip")).to_have_text("nuc")
    # A few words on the chip (the same the server gives the member), the node's own reason as its tooltip.
    expect(row.locator("[data-node-zero]")).to_have_text("fix media access first · 0 now")
    expect(row.locator("[data-node-zero]")).to_have_attribute("data-bs-title", "fix access first")
    # Red, as on the Nodes page: a node with an access problem gets no work at all.
    expect(row.locator("[data-node-zero]")).to_have_class("node-zero bad")
    expect(row).to_contain_text("nuc: fix media access first · 0 workers now.")
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("cpu")
    expect(row.locator(".node-chip")).to_have_count(0)
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    assert "node" not in group_api["writes"][-1][2]["groups"][0]["members"][0]


def test_picker_without_nodes_stays_a_flat_list(authed_page: Page, app_url: str, group_api: dict) -> None:  # noqa: F811
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="cpu-night"]').click()
    picker = authed_page.locator('[data-member="m-cpu"] [data-pick]')
    expect(picker.locator("optgroup")).to_have_count(0)
    expect(picker.locator("option")).to_have_text(["CPU", "NVIDIA card"])


def test_settings_row_and_editor_flag_an_offline_node_member(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    settings_page(authed_page, app_url)
    page = authed_page
    chip = page.locator('#workerGroupRows [data-group-id="gpu-video"] .mchip').nth(1)
    expect(chip).to_contain_text("old-laptop · GTX 1650")
    expect(chip).to_have_class("mchip is-missing")
    page.locator('[data-edit="gpu-video"]').click()
    zero = page.locator('[data-member="m-laptop"] [data-node-zero]')
    expect(zero).to_have_text("node offline · 0 now")
    expect(zero).to_have_class("node-zero bad")
    expect(page.locator('[data-member="m-laptop"] [data-pick]')).to_have_value("node:laptop:gpu:cuda:0")


@pytest.mark.parametrize(
    ("state", "cause", "reason", "short"),
    [
        ("offline", "restarting", "old-laptop is restarting; it gets no files until it is back.", "node restarting"),
        ("offline", "rate_limited", "old-laptop asks this app to wait.", "node asks to wait"),
        # Its reason and Setup Health say "Update this app"; the chip said "node needs an update".
        ("update_needed", "app_behind", "Update this app to 9.9.10.", "app needs an update"),
        ("update_needed", "", "Update the node to 9.9.9.", "node needs an update"),
    ],
    ids=["restarting", "asks_to_wait", "app_behind", "node_behind"],
)
def test_editor_says_why_a_node_member_has_no_workers_when_nothing_on_the_node_is_wrong(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
    state: str,
    cause: str,
    reason: str,
    short: str,
) -> None:
    """Its chip said "node offline · 0 now" while its tooltip, Setup Health and the bell said it is restarting."""
    offline_node_group(group_api)
    laptop = next(node for node in group_api["state"]["nodes"] if node["id"] == "laptop")
    laptop.update(state=state, cause=cause, reason=reason)
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="gpu-video"]').click()
    expect(authed_page.locator('[data-member="m-laptop"] [data-node-zero]')).to_have_text(f"{short} · 0 now")


def test_dashboard_shows_node_chip_zero_reason_and_offline_worker_card(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": 4,
        "worker_type": "GPU",
        "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
        "group_name": "Video",
        "group_id": "gpu-video",
        "member_id": "m-laptop",
        "retiring": True,
        "status": "processing",
        "current_title": "The Bear · S02E06",
        "ffmpeg_started": True,
        "progress_percent": 40,
        "node": {"id": "laptop", "name": "old-laptop"},
        "node_state": "offline",
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    block = authed_page.locator('[data-member-block="gpu-video:m-laptop"]')
    expect(block.locator(".mem-h .node-chip")).to_have_text("old-laptop")
    expect(block.locator("[data-node-zero]")).to_have_text("node offline · 0 now")
    expect(block).to_contain_text("old-laptop · GTX 1650: node offline. Its jobs wait; other devices keep working.")
    card = authed_page.locator('[data-worker-key="GPU_4"]')
    expect(card.locator("[data-node-chip]")).to_have_text("old-laptop")
    expect(card.locator("[data-status-badge]")).to_have_text("Node offline")
    expect(card.locator("[data-status-badge]")).to_have_class("pill bad")
    local = authed_page.locator('[data-member-block="gpu-video:m-gpu"] .mem-h')
    expect(local.locator(".node-chip")).to_have_text("Main")


def test_an_offline_nodes_line_doesnt_say_other_devices_keep_working_when_none_do(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    """Every other group off: nothing else runs, so "other devices keep working" wasn't true."""
    offline_node_group(group_api)
    state = group_api["state"]
    state["groups"][0]["enabled"] = False
    state["capacity"]["groups"][0]["members"][0].update(target=0, busy=0)
    state["groups"][1]["members"] = [m for m in state["groups"][1]["members"] if m["id"] != "m-gpu"]
    video = state["capacity"]["groups"][1]
    video["members"] = [m for m in video["members"] if m["id"] != "m-gpu"]
    mock_dashboard_defaults(authed_page)
    authed_page.goto(app_url + "/")
    block = authed_page.locator('[data-member-block="gpu-video:m-laptop"]')
    expect(block.locator(".msg")).to_have_text("old-laptop · GTX 1650: node offline. Its jobs wait. Open old-laptop")
    chip = block.locator("[data-node-zero]")
    expect(chip).to_have_attribute("data-bs-title", "Its jobs wait for old-laptop.")


@pytest.mark.parametrize(
    ("node_state", "text", "tone"),
    [
        ("update_needed", "Node update needed", "bad"),
        ("no_ffmpeg", "Node FFmpeg missing", "bad"),
        ("temp_problem", "Node temp folder problem", "bad"),
        ("disabled", "Node turned off", "warn"),
    ],
)
def test_a_worker_cards_node_state_has_the_nodes_pages_name_and_colour(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
    node_state: str,
    text: str,
    tone: str,
) -> None:
    """The dashboard showed these amber (red on the Nodes page) and a turned-off node as "Node disabled"."""
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": 4,
        "worker_type": "GPU",
        "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
        "group_name": "Video",
        "group_id": "gpu-video",
        "member_id": "m-laptop",
        "status": "idle",
        "node": {"id": "laptop", "name": "old-laptop"},
        "node_state": node_state,
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    badge = authed_page.locator('[data-worker-key="GPU_4"] [data-status-badge]')
    expect(badge).to_have_text(text)
    expect(badge).to_have_class(f"pill {tone}")


@pytest.mark.parametrize(
    ("state", "cause", "text"),
    [
        ("offline", "restarting", "Node restarting"),
        ("offline", "rate_limited", "Node asks to wait"),
        ("offline", "unreachable", "Node offline"),
        ("offline", "", "Node offline"),
        ("update_needed", "app_behind", "App needs an update"),
        ("update_needed", "", "Node update needed"),
    ],
)
def test_a_worker_card_says_why_its_node_gives_it_no_work(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
    state: str,
    cause: str,
    text: str,
) -> None:
    """A restarting node's card read "Node offline" while the bell and Workers said it is restarting."""
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": 4,
        "worker_type": "GPU",
        "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
        "group_name": "Video",
        "group_id": "gpu-video",
        "member_id": "m-laptop",
        "status": "idle",
        "node": {"id": "laptop", "name": "old-laptop"},
        "node_state": state,
        "node_cause": cause,
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    expect(authed_page.locator('[data-worker-key="GPU_4"] [data-status-badge]')).to_have_text(text)


LONG_REASON = (
    "old-laptop's temp folder can't be used, so it gets no files: no space left on device: /ntmp. Free up space "
    "there, or set NODE_TEMP_DIR on the node to a folder it can write to."
)


def _temp_problem_node(api: dict) -> None:
    offline_node_group(api)
    laptop = next(node for node in api["state"]["nodes"] if node["id"] == "laptop")
    laptop.update(state="temp_problem", reason=LONG_REASON)
    row = next(m for m in api["state"]["capacity"]["groups"][1]["members"] if m["id"] == "m-laptop")
    row["reason"] = "node's temp folder can't be used"


@pytest.mark.parametrize("width", [390, 1440])
def test_the_editors_zero_chip_is_short_stays_in_its_row_and_has_the_full_reason_as_its_tooltip(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
    width: int,
) -> None:
    """The chip carried the node's whole sentence on one line and ran up to 774 px past its card."""
    _temp_problem_node(group_api)
    authed_page.set_viewport_size({"width": width, "height": 844})
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="gpu-video"]').click()
    row = authed_page.locator('[data-member="m-laptop"]')
    zero = row.locator("[data-node-zero]")
    expect(zero).to_have_text("node's temp folder can't be used · 0 now")
    expect(zero).to_have_attribute("data-bs-title", LONG_REASON)
    chip, card = zero.bounding_box(), row.bounding_box()
    assert chip["x"] + chip["width"] <= card["x"] + card["width"] + 1, (chip, card)
    assert authed_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_on_a_phone_the_dashboards_zero_chip_doesnt_cover_the_device_name(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    """At 390 px "node's temp folder can't be used · 0 now" sat on top of the node chip and the GPU's name."""
    _temp_problem_node(group_api)
    mock_dashboard_defaults(authed_page)
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(app_url + "/")
    head = authed_page.locator('[data-member-block="gpu-video:m-laptop"] .mem-h')
    chip = head.locator("[data-node-zero]")
    expect(chip).to_have_text("node's temp folder can't be used · 0 now")
    expect(chip).to_have_attribute(
        "data-bs-title", re.compile(r"^old-laptop's temp folder can't be used.*Its jobs wait")
    )
    overlaps = head.evaluate(
        """head => {
            const chip = head.querySelector('[data-node-zero]').getBoundingClientRect();
            return Array.from(head.querySelector('.devname').children).map(c => c.getBoundingClientRect())
                .filter(p => p.width && p.right > chip.left + 1 && p.left < chip.right && p.bottom > chip.top
                    && p.top < chip.bottom).length;
        }"""
    )
    assert overlaps == 0
    assert authed_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_week_legend_names_a_node_device_apart_from_this_apps(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    settings_page(authed_page, app_url)
    legend = authed_page.locator("#workerWeekLegend")
    expect(legend).to_contain_text("old-laptop · ")
    expect(legend.locator("span", has_text="old-laptop · ")).to_have_count(1)


def test_the_dashboard_says_a_node_problem_once_and_links_the_node(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    group_api["state"]["warnings"] = [
        {
            "code": "node_unavailable",
            "group_id": "gpu-video",
            "member_id": "m-laptop",
            "message": "Video: old-laptop · GTX 1650 has no workers now (node offline). Its jobs wait; other devices "
            "keep working.",
        },
        {"code": "loudness_without_cpu", "message": "Loudness needs a CPU group."},
    ]
    mock_dashboard_defaults(authed_page)
    authed_page.goto(app_url + "/")
    block = authed_page.locator('[data-member-block="gpu-video:m-laptop"]')
    expect(block).to_contain_text("old-laptop · GTX 1650: node offline.")
    expect(block.get_by_role("link", name="Open old-laptop")).to_have_attribute("href", "/nodes#node-laptop")
    expect(authed_page.locator("#workerGroupLiveWarnings")).to_have_text("Loudness needs a CPU group.")
    expect(authed_page.get_by_text("has no workers now")).to_have_count(0)


def test_a_member_switched_from_a_gpu_back_to_a_cpu_runs_loudness_again(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    group_api["state"]["nodes"] = NODES
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    picker = page.locator('[data-member="m-cpu"] [data-pick]')
    picker.select_option("node:storage:gpu:cuda:0")
    expect(page.locator('[data-member="m-cpu"]')).to_contain_text("Loudness removed — it runs on CPU only.")
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("node:storage:cpu")
    row = page.locator('[data-member="m-cpu"]')
    expect(row).to_contain_text("Loudness added, as the group runs it.")
    expect(row.locator('[data-kind="loudness"]')).to_have_attribute("aria-pressed", "true")


LOCAL_WORKER = {
    "worker_id": 1,
    "worker_type": "GPU",
    "worker_name": "GPU Worker 1 (NVIDIA card)",
    "group_name": "Video",
    "group_id": "gpu-video",
    "member_id": "m-gpu",
    "status": "processing",
    "current_title": "Severance · S02E01",
    "ffmpeg_started": True,
    "progress_percent": 30,
}
NODE_WORKER = {
    "worker_id": 4,
    "worker_type": "GPU",
    "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
    "group_name": "Video",
    "group_id": "gpu-video",
    "member_id": "m-laptop",
    "status": "processing",
    "current_title": "The Bear · S02E06",
    "ffmpeg_started": True,
    "progress_percent": 60,
    "node": {"id": "laptop", "name": "old-laptop"},
    "node_state": "ready",
}


def test_with_a_worker_on_a_node_this_apps_worker_cards_say_this_app(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    workers = [LOCAL_WORKER, NODE_WORKER]
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": workers}))
    authed_page.goto(app_url + "/")
    local = authed_page.locator('[data-worker-key="GPU_1"] [data-node-chip]')
    expect(local).to_have_text("Main")
    expect(local).not_to_have_attribute("hidden", "")
    expect(local).to_have_attribute("title", "Runs on this app's machine")
    remote = authed_page.locator('[data-worker-key="GPU_4"] [data-node-chip]')
    expect(remote).to_have_text("old-laptop")
    expect(remote).to_have_attribute("title", "Runs on node old-laptop")
    # The compact table hides the card's own chip; the device row above the cards is where it shows.
    expect(authed_page.locator("#workerGroupLiveRows")).to_have_class(re.compile(r"\bwg-dense\b"))
    head = authed_page.locator('[data-member-block="gpu-video:m-gpu"] .mem-h .node-chip')
    expect(head).to_be_visible()
    expect(head).to_have_text("Main")
    expect(head).to_have_attribute("title", "Runs on this app's machine")
    expect(head.locator("i")).to_have_class("bi bi-house-door")
    expect(authed_page.locator('[data-member-block="gpu-video:m-laptop"] .mem-h .node-chip')).to_have_text("old-laptop")


def test_without_a_worker_on_a_node_worker_cards_have_no_machine_chip(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    mock_dashboard_defaults(authed_page)
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [LOCAL_WORKER]}))
    authed_page.goto(app_url + "/")
    card = authed_page.locator('[data-worker-key="GPU_1"]')
    expect(card).to_be_visible()
    expect(card.locator("[data-node-chip]")).to_have_attribute("hidden", "")
    expect(card.locator("[data-node-chip]")).to_have_text("")
    expect(card.locator("[data-node-chip]")).to_have_attribute("title", "")
    head = authed_page.locator('[data-member-block="gpu-video:m-gpu"] .mem-h')
    expect(head).to_be_visible()
    expect(head.locator(".node-chip")).to_have_count(0)
    expect(authed_page.get_by_text("Main", exact=True)).to_have_count(0)
