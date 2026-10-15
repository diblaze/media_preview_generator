"""E2E: the Nodes page (list, Add node, node dialog) and the Plex server's "Plex database" selector.

Every /api/nodes* route is mocked with ``page.route``, so these run against the page alone. Screenshots go to
``MPG_UX_EVIDENCE_DIR`` when it is set (else the test's tmp dir).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from .conftest import accept_app_confirm, expect_modal_shown, watch_modal_shown
from .test_info_icons import _AUDIT_JS, MORE
from .test_intro_credits_server_tab import (
    _mock_server_page,
    _open_tab,
    _plex_ready_details,
    _plex_server,
    _save_and_read_put,
    _status,
)

APP_VERSION = "5.2.0"
CHANGED_ELSEWHERE = "This node was changed in another tab. Reload the page, then make your change again."
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _capture(locator: Locator, filename: str, tmp_path: Path) -> None:
    evidence = Path(os.environ.get("MPG_UX_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    locator.screenshot(path=str(evidence / filename), animations="disabled")


def _fulfill(route: Route, body: object, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _node(node_id: str, name: str, state: str = "ready", **overrides) -> dict:
    node = {
        "id": node_id,
        "name": name,
        "url": f"http://{name}.lan:9494",
        "enabled": True,
        "state": state,
        "reason": "",
        "version": APP_VERSION,
        "roles": ["worker"],
        "legacy": False,
        "platform": {"os": "linux", "arch": "x86_64", "python": "3.12"},
        "gpus": [],
        "cpu": {"cores": 8, "load1": 0.4, "ram_total": 32e9, "ram_available": 20e9},
        "busy": 0,
        "last_seen": time.time() - 3,
        "path_mappings": [],
        "used_by": {"groups": [], "servers": []},
        "edit_tag": f"{node_id}-1",
    }
    node.update(overrides)
    return node


def _next_tag(tag: str) -> str:
    """The tag the app gives a node after a save changed it (``n-storage-1`` -> ``n-storage-2``)."""
    stem, _, n = tag.rpartition("-")
    return f"{stem}-{int(n) + 1}"


def _four_nodes() -> list[dict]:
    """The mockup's four: a GPU worker, a Windows Plex database node, an access problem and an offline worker."""
    return [
        _node(
            "n-storage",
            "storage",
            gpus=[{"type": "NVIDIA", "device": "cuda:0", "name": "Quadro P5000", "status": "ok"}],
            cpu={"cores": 24, "load1": 3.1, "ram_total": 64e9, "ram_available": 40e9},
            busy=2,
            path_mappings=[{"app_prefix": "/data/Movies", "node_prefix": "/mnt/nas/Movies"}],
            used_by={"groups": [{"id": "g1", "name": "GPU box"}], "servers": []},
        ),
        _node(
            "n-pc",
            "gaming-pc",
            url="http://host.docker.internal:9494",
            roles=["plex_db"],
            platform={"os": "windows", "arch": "AMD64", "python": "3.12"},
            reason="Plex has it open · local disk",
            cpu={},
            used_by={"groups": [], "servers": [{"id": "plex-1", "name": "Home"}]},
        ),
        _node("n-nuc", "nuc", "access_problem", reason="TV Shows not found at /media/tv"),
        _node(
            "n-old",
            "old-laptop",
            "offline",
            url="http://192.168.1.40:9494",
            reason="since 14:02 · next try in 4 min",
            gpus=[{"type": "NVIDIA", "device": "cuda:0", "name": "GTX 1650", "status": "ok"}],
            last_seen=time.time() - 600,
        ),
    ]


class NodesApi:
    """Routes every /api/nodes* call to canned answers and records what the page sent."""

    def __init__(self, page: Page, nodes: list[dict]) -> None:
        self.nodes = nodes
        self.calls: list[tuple[str, str, object]] = []
        self.probe_answer: dict = {"ok": True, "info": _probe_info()}
        self.create_answer: tuple[int, dict] | None = None
        self.test_answers: list[dict] = []
        self.health_answer: dict = {"overall_ok": True, "sections": []}
        self.log_answer: dict = {"lines": [_log("14:01:02", "node started"), _log("14:01:03", "1 GPU found")]}
        self.delete_answer: tuple[int, dict] = (200, {"ok": True})
        self.rescan_answer: tuple[int, dict] | None = None
        self.suggest_answer: dict = {"path_mappings": [], "media_setup": {"servers": 1, "libraries": 1}}
        # Check now: how many list reads after it still say the nodes are being checked; the next shows what changed.
        self.check_reads = 0
        self.after_check: dict[str, dict] = {}
        self._checking = 0
        page.route("**/api/nodes**", self._handle)

    def bodies(self, method: str, suffix: str) -> list:
        return [body for m, path, body in self.calls if m == method and path.endswith(suffix)]

    def _list(self, *, checking: bool) -> dict:
        return {"nodes": [{**node, "checking": checking} for node in self.nodes], "app_version": APP_VERSION}

    def _find(self, node_id: str) -> dict:
        return next(n for n in self.nodes if n["id"] == node_id)

    def _handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split("/api", 1)[1].split("?", 1)[0]
        body = request.post_data_json if request.post_data else None
        # Recorded before answering: tests wait for the response, so the body is there when they look.
        self.calls.append((request.method, path, body))
        parts = path.strip("/").split("/")
        if parts == ["nodes"] and request.method == "GET":
            if self._checking:
                self._checking -= 1
                if not self._checking:
                    for node in self.nodes:
                        node.update(self.after_check.get(node["id"], {}))
                return _fulfill(route, self._list(checking=bool(self._checking)))
            return _fulfill(route, self._list(checking=False))
        if parts == ["nodes", "check"]:
            self._checking = self.check_reads + 1
            return _fulfill(route, self._list(checking=True), 202)
        if parts == ["nodes", "probe"]:
            return _fulfill(route, self.probe_answer)
        if parts == ["nodes"] and request.method == "POST":
            if self.create_answer:
                status, answer = self.create_answer
                return _fulfill(route, answer, status)
            info = self.probe_answer.get("info") or {}
            row = _node("n-new", body["name"], roles=list((info.get("node") or {}).get("roles") or ["worker"]))
            row["url"] = body["url"]
            row["path_mappings"] = body["path_mappings"]
            self.nodes.append(row)
            return _fulfill(route, row)
        node = self._find(parts[1])
        if len(parts) == 2 and request.method == "PUT":
            # As the app does: a save from a dialog opened before the node last changed is refused.
            if "edit_tag" in body and body["edit_tag"] != node["edit_tag"]:
                return _fulfill(route, {"error": CHANGED_ELSEWHERE}, 409)
            node.update({k: v for k, v in body.items() if k not in ("key", "edit_tag")})
            node["edit_tag"] = _next_tag(node["edit_tag"])
            return _fulfill(route, node)
        if len(parts) == 2 and request.method == "DELETE":
            status, answer = self.delete_answer
            if status == 200:
                self.nodes.remove(node)
            return _fulfill(route, answer, status)
        action = parts[2]
        if action == "check":
            return _fulfill(route, node)
        if action == "rescan-gpus":
            if self.rescan_answer:
                status, answer = self.rescan_answer
                return _fulfill(route, answer, status)
            node["gpus"] = [{"type": "NVIDIA", "device": "cuda:0", "name": "RTX 4070", "status": "ok"}]
            return _fulfill(route, node)
        if action == "test-access":
            return _fulfill(route, self.test_answers.pop(0) if self.test_answers else {"libraries": []})
        if action == "suggested-mappings":
            return _fulfill(route, self.suggest_answer)
        if action == "health":
            return _fulfill(route, self.health_answer)
        if action == "log":
            return _fulfill(route, self.log_answer)
        return _fulfill(route, {"error": "unexpected"}, 404)


def _log(time_of_day: str, message: str, level: str = "INFO") -> dict:
    return {"time": time_of_day, "level": level, "message": message}


def _probe_info(roles: list[str] | None = None, version: str = APP_VERSION, version_match: bool = True) -> dict:
    """The probe's ``info`` as ``POST /api/nodes/probe`` answers it for a worker node."""
    return {
        "node": {"id": "uid-1", "name": "storage", "version": version, "roles": roles or ["worker"]},
        "legacy": False,
        "version_match": version_match,
        "already_added": None,
        "suggested_path_mappings": [{"app_prefix": "/data/TV Shows", "node_prefix": "/mnt/nas/tv"}],
        "plex_db": None,
        "platform": {"os": "linux", "arch": "x86_64", "python": "3.12"},
        "worker": {
            "gpus": [{"type": "NVIDIA", "device": "GPU0", "name": "Quadro P5000", "status": "ok"}],
            "cpu": {"cores": 24, "load1": 0.5, "ram_total": 64e9, "ram_available": 50e9},
            "temp": {"path": "/tmp", "free_bytes": 41e9},
            "notes": [{"title": "Credit text", "chip": "CPU on this GPU", "message": "Vulkan reports no hardware GPU"}],
        },
    }


def _library(name: str, app_root: str, ok: bool, node_root: str, **extra) -> dict:
    files = [
        {
            "path": f"{app_root}/file{i}.mkv",
            "ok": ok,
            "kind": None if ok else "missing",
            "message": "",
            "node_path": f"{node_root}/file{i}.mkv",
        }
        for i in range(3)
    ]
    return {"server": "Home", "library": name, "app_root": app_root, "checked": 3, "ok": ok, "files": files, **extra}


def _open_nodes(page: Page, app_url: str, nodes: list[dict], path: str = "/nodes") -> NodesApi:
    api = NodesApi(page, nodes)
    page.goto(f"{app_url}{path}")
    expect(page.locator("#nodeList")).not_to_contain_text("Loading nodes", timeout=10000)
    return api


def _open_add(page: Page) -> Locator:
    watch_modal_shown(page, "addNodeModal")
    page.locator("#nodesAddBtn").click()
    expect_modal_shown(page, "addNodeModal")
    return page.locator("#addNodeModal")


def _open_node(page: Page, node_id: str) -> Locator:
    watch_modal_shown(page, "nodeModal")
    page.locator(f"#node-{node_id} .nd-open").click()
    expect_modal_shown(page, "nodeModal")
    return page.locator("#nodeModal")


def _node_tab(page: Page, tab: str) -> Locator:
    page.locator(f'#nodeModal .sm-rail [data-tab="{tab}"]').click()
    pane = page.locator(f"#node-tab-{tab}")
    expect(pane).to_be_visible()
    return pane


@pytest.mark.e2e
class TestNodeList:
    def test_each_state_gets_its_chip_colour_and_button(self, authed_page: Page, app_url: str) -> None:
        # Red is a node that gets no work (Setup Health's Must fix); every problem state's button says Fix.
        states = {
            "ready": ("✓ Ready", "ok", "Open"),
            "update_needed": ("Update needed", "bad", "Fix"),
            "access_problem": ("Access problem", "bad", "Fix"),
            "gpu_lost": ("GPU lost", "warn", "Fix"),
            "offline": ("Offline", "bad", "Fix"),
            "key_refused": ("Key refused", "bad", "Fix"),
            "no_ffmpeg": ("FFmpeg missing", "bad", "Fix"),
            "temp_problem": ("Temp folder problem", "bad", "Fix"),
            # It gets no work either, and its Fix opens Settings, where its Remove button is.
            "duplicate": ("Added twice", "bad", "Fix"),
            "connecting": ("Connecting…", "off", "Open"),
            "disabled": ("Turned off", "off", "Open"),
        }
        nodes = [_node(f"n-{i}", f"node{i}", state, reason=f"why {i}") for i, state in enumerate(states)]
        _open_nodes(authed_page, app_url, nodes)
        duplicate = list(states).index("duplicate")
        expect(authed_page.locator(f"#node-n-{duplicate} .nd-open")).to_have_attribute("data-tab", "settings")

        for i, (chip, tone, button) in enumerate(states.values()):
            row = authed_page.locator(f"#node-n-{i}")
            expect(row).to_have_attribute("data-tone", tone)
            pill = row.locator(".nd-status .pill:not(.nd-to-fix)")
            expect(pill).to_have_text(chip)
            if tone == "off":
                expect(pill).to_have_class("pill")
            else:
                expect(pill).to_have_class(f"pill {tone}")
            if chip == "Turned off":
                expect(row.locator(".nd-status")).not_to_contain_text(f"why {i}")
            else:
                expect(row.locator(".nd-status")).to_contain_text(f"why {i}")
            expect(row.locator(".nd-open")).to_have_text(button)

    def test_a_turned_off_node_says_so_once(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(
            authed_page,
            app_url,
            [_node("n-off", "spare", "disabled", enabled=False, reason="Turned off on the Nodes page")],
        )
        api.health_answer = {
            "overall_ok": True,
            "sections": [
                {
                    "id": "node_connection",
                    "title": "Connection",
                    "checks": [
                        {
                            "id": "node_disabled",
                            "label": "Turned off — no work is sent",
                            "ok": False,
                            "severity": "info",
                            "informational": True,
                        }
                    ],
                }
            ],
        }
        status = authed_page.locator("#node-n-off .nd-status")
        expect(status.locator(".pill")).to_have_text("Turned off")
        expect(status).not_to_contain_text("Turned off on the Nodes page")

        _open_node(authed_page, "n-off")
        pane = _node_tab(authed_page, "health")
        expect(pane.locator(".nd-note")).to_have_text("Turned off — no work is sent.")
        expect(pane.locator(".readiness-bucket")).to_have_count(0)
        expect(authed_page.locator("#nodeHealthSummary")).to_have_text("")

    def test_a_ready_card_says_how_many_setup_health_rows_to_fix(self, authed_page: Page, app_url: str) -> None:
        nodes = [
            _node("n-gpu", "gpu-box", to_fix={"must_fix": 1, "recommended": 2}),
            _node("n-new", "new-box", to_fix={"must_fix": 0, "recommended": 1}),
            _node("n-ok", "ok-box", to_fix={"must_fix": 0, "recommended": 0}),
            _node("n-off", "off-box", "offline", to_fix={"must_fix": 1, "recommended": 0}),
        ]
        _open_nodes(authed_page, app_url, nodes)

        gpu = authed_page.locator("#node-n-gpu")
        expect(gpu.locator(".nd-to-fix")).to_have_text("1 to fix")
        expect(gpu.locator(".nd-to-fix")).to_have_class("pill bad nd-to-fix")
        expect(gpu.locator(".nd-open")).to_have_text("Fix")
        expect(gpu.locator(".nd-open")).to_have_attribute("data-tab", "health")
        new = authed_page.locator("#node-n-new")
        expect(new.locator(".nd-to-fix")).to_have_text("1 to fix")
        expect(new.locator(".nd-to-fix")).to_have_class("pill warn nd-to-fix")
        expect(new.locator(".nd-open")).to_have_text("Open")
        expect(new.locator(".nd-open")).to_have_attribute("data-tab", "health")
        expect(authed_page.locator("#node-n-ok .nd-to-fix")).to_have_count(0)
        expect(authed_page.locator("#node-n-ok .nd-open")).to_have_attribute("data-tab", "overview")
        # The state chip already says what is wrong with a node that isn't ready.
        expect(authed_page.locator("#node-n-off .nd-to-fix")).to_have_count(0)

        watch_modal_shown(authed_page, "nodeModal")
        gpu.locator(".nd-open").click()
        expect_modal_shown(authed_page, "nodeModal")
        expect(authed_page.locator("#node-tab-health")).to_be_visible()

    def test_rows_show_roles_hardware_busy_and_who_uses_them(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())

        storage = authed_page.locator("#node-n-storage")
        expect(storage.locator(".nd-url")).to_have_text("http://storage.lan:9494")
        expect(storage.locator(".nd-chips")).to_contain_text("Worker")
        expect(storage.locator(".nd-chips")).to_contain_text("Quadro P5000")
        expect(storage.locator(".nd-chips")).to_contain_text("24 CPU")
        expect(storage.locator(".nd-chips")).to_contain_text("2 files running")
        expect(storage.locator(".nd-chips")).to_contain_text('used by worker group "GPU box"')
        expect(storage.locator(".nd-status")).to_contain_text(f"v{APP_VERSION} · seen")

        pc = authed_page.locator("#node-n-pc")
        expect(pc.locator(".nd-chips")).to_contain_text("Plex database")
        expect(pc.locator(".nd-chips")).to_contain_text("Windows")
        expect(pc.locator(".nd-chips")).to_contain_text('used by Plex server "Home"')
        expect(pc.locator(".nd-chips")).not_to_contain_text("Worker")
        expect(pc.locator(".nd-ico i")).to_have_class("bi bi-windows")

    def test_a_node_that_isnt_answering_shows_when_it_was_seen_not_its_old_hardware_or_idle(
        self, authed_page: Page, app_url: str
    ) -> None:
        """An offline card said "idle" beside the GPU and cores of its last answer, as if it were up and waiting."""
        _open_nodes(authed_page, app_url, _four_nodes())

        old = authed_page.locator("#node-n-old")
        expect(old.locator(".nd-status")).to_contain_text("seen 10 min ago")
        expect(old.locator(".nd-chips")).to_have_text("Worker")
        nuc = authed_page.locator("#node-n-nuc .nd-chips")
        expect(nuc).to_contain_text("8 CPU")
        expect(nuc).to_contain_text("idle")

        _open_node(authed_page, "n-old")
        overview = authed_page.locator("#nodeOverview")
        expect(overview).to_contain_text("Last answered10 min ago")
        expect(overview).not_to_contain_text("idle")
        expect(overview).not_to_contain_text("load")
        expect(overview).not_to_contain_text("free of")

    def test_empty_list_explains_nodes_and_links_the_docs(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, [])
        empty = authed_page.locator(".nd-empty")
        expect(empty).to_contain_text("No nodes yet")
        expect(empty).to_contain_text("lend its GPU or CPU")
        expect(empty.locator("a")).to_have_attribute(
            "href", "https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#remote-nodes"
        )
        # Nothing to check yet: the button is hidden, not greyed out.
        expect(authed_page.locator("#nodesCheckNow")).to_be_hidden()
        watch_modal_shown(authed_page, "addNodeModal")
        empty.locator(".nd-add").click()
        expect_modal_shown(authed_page, "addNodeModal")

    def test_check_now_checks_every_node_in_one_request_and_says_checking_until_done(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.check_reads = 2
        api.after_check = {"n-old": {"state": "ready", "reason": ""}}
        button = authed_page.locator("#nodesCheckNow")

        button.click()

        expect(button).to_have_text("Checking…")
        expect(button).to_be_disabled()
        expect(authed_page.locator("#node-n-old .nd-status .pill:not(.nd-to-fix)")).not_to_have_text("✓ Ready")
        expect(button).to_have_text("Check now", timeout=10000)
        expect(button).to_be_enabled()
        expect(authed_page.locator("#node-n-old .nd-status .pill:not(.nd-to-fix)")).to_have_text("✓ Ready")
        assert [path for method, path, _ in api.calls if method == "POST"] == ["/nodes/check"]

    def test_list_is_polled(self, authed_page: Page, app_url: str) -> None:
        authed_page.clock.install()
        nodes = _four_nodes()
        _open_nodes(authed_page, app_url, nodes)
        nodes[3]["state"] = "ready"
        nodes[3]["reason"] = ""
        authed_page.clock.run_for(10_500)
        expect(authed_page.locator("#node-n-old .nd-status .pill:not(.nd-to-fix)")).to_have_text("✓ Ready")

    def test_the_url_hash_opens_that_node(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes(), path="/nodes#node-n-nuc")
        expect(authed_page.locator("#nodeModal")).to_be_visible()
        expect(authed_page.locator("#nodeModalName")).to_have_text("nuc")
        expect(authed_page.locator("#nodeModalState")).to_have_text("Access problem")

    def test_a_hash_with_a_tab_opens_that_tab(self, authed_page: Page, app_url: str) -> None:
        # The bell's "Fix key" links here.
        _open_nodes(authed_page, app_url, _four_nodes(), path="/nodes#node-n-storage/settings")
        expect(authed_page.locator("#node-tab-settings")).to_be_visible()
        expect(authed_page.locator("#nodeModalName")).to_have_text("storage")

    def test_the_bells_test_access_link_opens_media_and_runs_the_test(self, authed_page: Page, app_url: str) -> None:
        api = NodesApi(authed_page, _four_nodes())
        with authed_page.expect_response(lambda r: r.url.endswith("/test-access")):
            authed_page.goto(f"{app_url}/nodes#node-n-nuc/media/test")
        expect(authed_page.locator("#node-tab-media")).to_be_visible()
        assert [path for method, path, _ in api.calls if method == "POST"] == ["/nodes/n-nuc/test-access"]

    def test_fix_opens_the_tab_with_the_fix(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-nuc")
        expect(authed_page.locator("#node-tab-media")).to_be_visible()
        assert authed_page.evaluate("() => window.location.hash") == "#node-n-nuc"

    def test_settings_menu_opens_the_page(self, authed_page: Page, app_url: str) -> None:
        NodesApi(authed_page, [])
        authed_page.goto(f"{app_url}/")
        authed_page.locator("#navSettingsDropdown").hover()
        authed_page.locator("#navSettingsMenu .dropdown-item", has_text="Nodes").click()
        authed_page.wait_for_url(f"{app_url}/nodes")
        expect(authed_page.locator("#navSettingsDropdown")).to_have_class("nav-link dropdown-toggle active")
        expect(authed_page.locator('#navSettingsMenu .dropdown-item[href="/nodes"]')).to_have_attribute(
            "aria-current", "page"
        )

    def test_phone_width_fits(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(PHONE)
        _open_nodes(authed_page, app_url, _four_nodes())
        assert authed_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _open_node(authed_page, "n-storage")
        assert authed_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _node_tab(authed_page, "media")
        expect(authed_page.locator("#nodeMediaEditor .nd-app")).to_be_visible()

    def test_on_a_phone_every_tab_shows_and_each_path_field_is_labelled(self, authed_page: Page, app_url: str) -> None:
        authed_page.set_viewport_size(PHONE)
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        width = authed_page.viewport_size["width"]
        for tab in ("overview", "media", "health", "log", "settings"):
            box = authed_page.locator(f'#nodeModal .sm-rail [data-tab="{tab}"]').bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, tab
        _node_tab(authed_page, "media")
        row = authed_page.locator("#nodeMediaEditor .nd-map-row").first
        expect(row.locator(".nd-mini-app")).to_have_text("Path on this app")
        expect(row.locator(".nd-mini-app")).to_be_visible()
        expect(row.locator(".nd-mini-node")).to_have_text("Path on the node")
        expect(row.locator(".nd-mini-node")).to_be_visible()
        # On a wide screen the column headers say it once instead.
        authed_page.set_viewport_size({"width": 1440, "height": 900})
        expect(row.locator(".nd-mini-app")).to_be_hidden()

    def test_info_icons_follow_the_app_rule(self, authed_page: Page, app_url: str) -> None:
        nodes = _four_nodes()
        nodes[1]["legacy"] = True
        _open_nodes(authed_page, app_url, nodes)
        _open_add(authed_page)
        icons = authed_page.evaluate(_AUDIT_JS)
        assert len(icons) >= 8
        for icon in icons:
            assert icon["hint"], icon
            if icon["hasDetail"]:
                assert icon["hint"].endswith(f" {MORE}") and icon["pointerClass"], icon
            else:
                assert "click for" not in icon["hint"].lower() and not icon["pointerClass"], icon


@pytest.mark.e2e
class TestAddNode:
    def test_the_dialog_opens_with_the_cursor_in_the_address(self, authed_page: Page, app_url: str) -> None:
        """The name is optional (the node's own is used), so typing starts at the address."""
        _open_nodes(authed_page, app_url, _four_nodes()[:1])
        _open_add(authed_page)
        expect(authed_page.locator("#addNodeUrl")).to_be_focused()

    def test_connect_save_test_access_use_suggestion_and_save(
        self, authed_page: Page, app_url: str, tmp_path: Path
    ) -> None:
        page = authed_page
        api = _open_nodes(page, app_url, _four_nodes()[:1])
        modal = _open_add(page)
        expect(page.locator("#addNodeStepLabel")).to_have_text("Step 1 of 2")
        page.locator("#addNodeName").fill("nas")
        page.locator("#addNodeUrl").fill("http://nas.lan:9494")
        page.locator("#addNodeKey").fill("secret-key")
        expect(page.locator("#addNodeUrl")).to_have_attribute("placeholder", "http://host.lan:9494")
        expect(page.locator("#addNodeKey")).to_have_attribute("type", "password")

        page.locator("#addNodeConnect").click()

        probe = page.locator("#addNodeProbe")
        expect(probe).to_contain_text("✓ Connected")
        expect(probe).to_contain_text(f"version {APP_VERSION} · same as this app")
        expect(probe).to_contain_text("GPU0 · Quadro P5000 · test decode ✓")
        expect(probe).to_contain_text("24 CPU cores")
        expect(probe).to_contain_text("41 GB free temp")
        expect(probe).to_contain_text("CPU on this GPU")
        expect(probe).to_contain_text("Vulkan reports no hardware GPU")
        assert api.bodies("POST", "/nodes/probe") == [{"url": "http://nas.lan:9494", "key": "secret-key"}]
        assert not api.bodies("POST", "/nodes"), "Connect must not save"

        next_btn = page.locator("#addNodeNext")
        expect(next_btn).to_have_text("Next: where it sees your media →")
        next_btn.click()

        expect(page.locator("#addNodeStep2")).to_be_visible()
        assert api.bodies("POST", "/nodes") == [
            {"name": "nas", "url": "http://nas.lan:9494", "key": "secret-key", "path_mappings": []}
        ]
        expect(page.locator("#addNodeStepLabel")).to_have_text("Step 2 of 2")
        expect(modal.locator(".nd-map-head h3")).to_have_text("Where nas sees your media")
        expect(page.locator("#addNodeCancel")).to_have_text("Finish later")
        # Pre-filled from the probe's suggested rows.
        editor = page.locator("#addNodeMappings")
        expect(editor.locator(".nd-app").first).to_have_value("/data/TV Shows")
        expect(editor.locator(".nd-node").first).to_have_value("/mnt/nas/tv")
        editor.locator(".nd-add-row").click()
        editor.locator(".nd-app").nth(1).fill("/data/Movies")
        editor.locator(".nd-node").nth(1).fill("/mnt/nas/Movies")

        api.test_answers = [
            {
                "libraries": [
                    _library("Movies", "/data/Movies", True, "/mnt/nas/Movies"),
                    _library(
                        "TV Shows",
                        "/data/TV Shows",
                        False,
                        "/mnt/nas/tv",
                        nearby={"/mnt/nas": ["Movies", "TV Shows", "downloads"]},
                        suggestion={"from": "/mnt/nas/tv", "to": "/mnt/nas/TV Shows"},
                    ),
                ]
            },
            {
                "libraries": [
                    _library("Movies", "/data/Movies", True, "/mnt/nas/Movies"),
                    _library("TV Shows", "/data/TV Shows", True, "/mnt/nas/TV Shows"),
                ]
            },
        ]
        editor.locator(".nd-test").click()

        rows = editor.locator(".nd-map-row")
        expect(rows.nth(0).locator(".nd-chip-slot")).to_have_text("✗ TV Shows 0/3")
        expect(rows.nth(1).locator(".nd-chip-slot")).to_have_text("✓ Movies 3/3")
        card = editor.locator('.nd-fail[data-library="TV Shows"]')
        expect(card).to_contain_text("TV Shows: not found on the node.")
        expect(card).to_contain_text("Tried /mnt/nas/tv/file0.mkv")
        expect(card).to_contain_text("Folders the node does see under /mnt/nas:")
        expect(card.locator(".pill")).to_have_text(["Movies", "TV Shows", "downloads"])
        assert api.bodies("POST", "/nodes/n-new/test-access") == [
            {
                "path_mappings": [
                    {"app_prefix": "/data/TV Shows", "node_prefix": "/mnt/nas/tv"},
                    {"app_prefix": "/data/Movies", "node_prefix": "/mnt/nas/Movies"},
                ]
            }
        ]
        _capture(modal.locator(".modal-content"), "nodes-add-step2-failed-library.png", tmp_path)

        with page.expect_response(lambda r: r.url.endswith("/test-access")):
            card.locator(".nd-use").click()

        expect(editor.locator(".nd-node").first).to_have_value("/mnt/nas/TV Shows")
        expect(rows.nth(0).locator(".nd-chip-slot")).to_have_text("✓ TV Shows 3/3")
        expect(editor.locator(".nd-map-results")).to_contain_text("The node sees every library, same files.")

        page.locator("#addNodeFinish").click()
        expect(modal).to_be_hidden(timeout=5000)
        assert api.bodies("PUT", "/nodes/n-new") == [
            {
                "path_mappings": [
                    {"app_prefix": "/data/TV Shows", "node_prefix": "/mnt/nas/TV Shows"},
                    {"app_prefix": "/data/Movies", "node_prefix": "/mnt/nas/Movies"},
                ],
                "edit_tag": "n-new-1",
            }
        ]
        expect(page.locator("#node-n-new")).to_be_visible()

    def _to_step_2(self, page: Page) -> None:
        page.locator("#addNodeUrl").fill("http://nas.lan:9494")
        page.locator("#addNodeKey").fill("secret-key")
        page.locator("#addNodeConnect").click()
        expect(page.locator("#addNodeProbe")).to_contain_text("Connected")
        page.locator("#addNodeNext").click()
        expect(page.locator("#addNodeStep2")).to_be_visible()

    def test_finish_later_keeps_the_rows_shown_in_step_2(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        api = _open_nodes(page, app_url, [])
        _open_add(page)
        self._to_step_2(page)
        expect(page.locator("#addNodeMappings .nd-app").first).to_have_value("/data/TV Shows")

        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/nodes/n-new")):
            page.locator("#addNodeCancel").click()

        expect(page.locator("#addNodeModal")).to_be_hidden(timeout=5000)
        assert api.bodies("PUT", "/nodes/n-new") == [
            {"path_mappings": [{"app_prefix": "/data/TV Shows", "node_prefix": "/mnt/nas/tv"}], "edit_tag": "n-new-1"}
        ]

    def test_step_2_with_servers_but_no_libraries_says_to_refresh_them(self, authed_page: Page, app_url: str) -> None:
        page = authed_page
        api = _open_nodes(page, app_url, [])
        api.probe_answer["info"]["suggested_path_mappings"] = []
        api.probe_answer["info"]["media_setup"] = {"servers": 1, "libraries": 0}
        _open_add(page)
        self._to_step_2(page)

        note = page.locator("#addNodeMappings .nd-map-note")
        expect(note).to_contain_text("Your media servers have no libraries loaded yet.")
        expect(note.get_by_role("link", name="Refresh libraries on Media Servers")).to_have_attribute(
            "href", "/servers"
        )
        expect(page.locator("#addNodeMappings")).not_to_contain_text("Add a media server")

    def test_plex_database_node_skips_the_media_step(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        modal = _open_add(authed_page)
        api.probe_answer = {
            "ok": True,
            "info": {
                **_probe_info(roles=["plex_db"]),
                "worker": None,
                "version_match": None,
                "suggested_path_mappings": [],
            },
        }
        api.probe_answer["info"]["plex_db"] = {"config_dir": "C:\\Plex", "db_present": True}
        authed_page.locator("#addNodeUrl").fill("http://pc:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeProbe")).to_contain_text("✓ Database found")
        # The node's own name fills an empty Name field.
        expect(authed_page.locator("#addNodeName")).to_have_value("storage")
        expect(authed_page.locator("#addNodeStepLabel")).to_have_text("")
        expect(authed_page.locator("#addNodeNext")).to_have_text("Add node")

        authed_page.locator("#addNodeNext").click()

        expect(modal).to_be_hidden(timeout=5000)
        assert api.bodies("POST", "/nodes")[0]["name"] == "storage"
        assert not [c for c in api.calls if c[1].endswith("/test-access")]

    def test_version_mismatch_warns_update_needed(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        api.probe_answer = {"ok": True, "info": _probe_info(version="5.1.0", version_match=False)}
        authed_page.locator("#addNodeUrl").fill("http://nas:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        probe = authed_page.locator("#addNodeProbe")
        expect(probe.locator(".pill.warn").first).to_have_text("Update needed")
        expect(probe).to_contain_text(f"version 5.1.0; this app is {APP_VERSION}")

    @pytest.mark.parametrize(
        ("kind", "text"),
        [
            (
                "auth",
                "Key refused. Copy the key again from the node's log, or use the NODE_KEY (or AGENT_TOKEN) you set.",
            ),
            ("unreachable", "Nothing answered at this address. Check the address and port"),
            ("protocol", "not as a node this app can use. Update the node"),
            ("request", "didn't accept the request"),
        ],
    )
    def test_probe_errors_are_plain_words(self, authed_page: Page, app_url: str, kind: str, text: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        api.probe_answer = {"ok": False, "error": {"kind": kind, "message": "raw detail"}}
        authed_page.locator("#addNodeUrl").fill("http://nas:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeError")).to_contain_text(text)
        expect(authed_page.locator("#addNodeNext")).to_be_hidden()

    @pytest.mark.parametrize(
        ("url", "kind", "reason", "text"),
        [
            ("http://nas:9494", "protocol", "this_app", "This address is this app itself, not a node."),
            ("http://nas:8080", "protocol", "other_app", "This address is another copy of this app, not a node."),
            ("http://nas:80", "protocol", "web_page", "This address answers with a web page, not a node."),
            ("https://nas:9494", "unreachable", "tls", "It doesn't answer over https."),
            ("https://nas:9494", "unreachable", "timeout", "No answer over https in time."),
            ("http://nass.lan:9494", "unreachable", "dns", "Can't find nass.lan."),
            ("http://nas:9495", "unreachable", "refused", "Nothing is listening on that port."),
        ],
        ids=["this-app", "other-app", "web-page", "https", "https-timeout", "dns", "refused"],
    )
    def test_probe_errors_say_what_to_check(
        self, authed_page: Page, app_url: str, url: str, kind: str, reason: str, text: str
    ) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        api.probe_answer = {"ok": False, "error": {"kind": kind, "message": "raw detail", "reason": reason}}
        authed_page.locator("#addNodeUrl").fill(url)
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeError")).to_contain_text(text)
        expect(authed_page.locator("#addNodeError")).not_to_contain_text("Update the node")

    def test_a_node_on_another_protocol_names_both_versions(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        message = f"This node runs version 9.10.0 and this app runs {APP_VERSION}, and they don't speak the same node"
        api.probe_answer = {"ok": False, "error": {"kind": "protocol", "message": message, "node_version": "9.10.0"}}
        authed_page.locator("#addNodeUrl").fill("http://nas:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeError")).to_contain_text(message)

    def test_missing_fields_are_flagged_inline(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeUrl")).to_have_class("form-control font-monospace is-invalid")
        expect(authed_page.locator("#addNodeError")).to_contain_text("Enter the node's address")
        assert not api.bodies("POST", "/nodes/probe")

    def test_already_added_node_is_named(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        api.create_answer = (409, {"error": "already added", "node_id": "n-storage", "name": "storage"})
        authed_page.locator("#addNodeUrl").fill("http://nas:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        authed_page.locator("#addNodeNext").click()
        expect(authed_page.locator("#addNodeError")).to_contain_text('This node is already added as "storage"')
        expect(authed_page.locator("#addNodeStep1")).to_be_visible()

    def test_probe_of_a_node_already_added_says_so_before_saving(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [])
        _open_add(authed_page)
        api.probe_answer = {"ok": True, "info": {**_probe_info(), "already_added": {"id": "n-1", "name": "storage"}}}
        authed_page.locator("#addNodeUrl").fill("http://nas:9494")
        authed_page.locator("#addNodeKey").fill("k")
        authed_page.locator("#addNodeConnect").click()
        expect(authed_page.locator("#addNodeError")).to_contain_text('This node is already added as "storage"')
        expect(authed_page.locator("#addNodeNext")).to_be_hidden()
        assert not api.bodies("POST", "/nodes")


@pytest.mark.e2e
class TestNodeDialog:
    def test_overview_shows_hardware_and_rescans_gpus(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        overview = authed_page.locator("#nodeOverview")
        expect(overview).to_contain_text("✓ Ready")
        expect(overview).to_contain_text("same as this app")
        expect(overview).to_contain_text("24 cores · load 3.1")
        expect(overview).to_contain_text("40 GB free of 64 GB")
        expect(overview).to_contain_text("2 files running")
        expect(authed_page.locator("#nodeGpuList")).to_contain_text("Quadro P5000")
        expect(authed_page.locator("#nodeGpuList")).to_contain_text("✓ Working")

        authed_page.locator("#nodeRescanGpus").click()

        expect(authed_page.locator("#nodeRescanResult")).to_have_text("Found 1 working GPU")
        expect(authed_page.locator("#nodeGpuList")).to_contain_text("RTX 4070")
        assert api.bodies("POST", "/nodes/n-storage/rescan-gpus") == [None]

    def test_rescan_of_a_node_that_does_not_answer_says_so_in_red(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.rescan_answer = (502, {"error": "Couldn't ask storage: http://storage.lan:9494 didn't answer"})
        _open_node(authed_page, "n-storage")
        authed_page.locator("#nodeRescanGpus").click()
        result = authed_page.locator("#nodeRescanResult")
        expect(result).to_have_text("Couldn't ask storage: http://storage.lan:9494 didn't answer")
        expect(result).to_have_class("sm-tres bad")

    def test_rescan_does_not_count_a_gpu_that_failed_its_test(self, authed_page: Page, app_url: str) -> None:
        nodes = _four_nodes()
        api = _open_nodes(authed_page, app_url, nodes)
        failed = {"type": "NVIDIA", "device": "cuda:0", "name": "Quadro P5000", "status": "failed"}
        api.rescan_answer = (200, {**nodes[0], "gpus": [failed]})
        _open_node(authed_page, "n-storage")
        authed_page.locator("#nodeRescanGpus").click()
        result = authed_page.locator("#nodeRescanResult")
        expect(result).to_have_text("No working GPU found · 1 not working")
        expect(result).to_have_class("sm-tres warn")

    def test_plex_database_node_has_no_media_tab(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-pc")
        expect(authed_page.locator('#nodeModal [data-tab="media"]')).to_be_hidden()
        expect(authed_page.locator("#nodeGpuSection")).to_be_hidden()

    def test_media_tab_saves_mappings(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        expect(pane.locator(".nd-node").first).to_have_value("/mnt/nas/Movies")
        pane.locator(".nd-node").first.fill("/mnt/media/Movies")
        expect(authed_page.locator("#nodeMediaResult")).to_have_text("Not saved yet")
        authed_page.locator("#nodeSaveMappings").click()
        expect(authed_page.locator("#nodeMediaResult")).to_have_text("Saved")
        assert api.bodies("PUT", "/nodes/n-storage") == [
            {
                "path_mappings": [{"app_prefix": "/data/Movies", "node_prefix": "/mnt/media/Movies"}],
                "edit_tag": "n-storage-1",
            }
        ]

    def test_a_node_with_no_rows_gets_the_library_folders_suggested(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, [_node("n-nas", "nas")])
        api.suggest_answer = {
            "path_mappings": [{"app_prefix": "/data/Movies", "node_prefix": "/data/Movies"}],
            "media_setup": {"servers": 1, "libraries": 1},
        }
        _open_node(authed_page, "n-nas")
        pane = _node_tab(authed_page, "media")

        expect(pane.locator(".nd-app").first).to_have_value("/data/Movies")
        expect(pane.locator(".nd-node").first).to_have_value("/data/Movies")
        expect(authed_page.locator("#nodeMediaResult")).to_have_text("Suggested from your libraries. Not saved yet")
        # Nothing typed, so closing loses nothing and doesn't ask.
        authed_page.keyboard.press("Escape")
        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)
        assert not api.bodies("PUT", "/nodes/n-nas")

    def test_test_access_with_servers_but_no_libraries_says_to_refresh_them(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.test_answers = [{"libraries": [], "media_setup": {"servers": 2, "libraries": 0}}]
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        pane.locator(".nd-test").click()

        note = pane.locator(".nd-map-note")
        expect(note).to_contain_text("No libraries to test yet. Your media servers have no libraries loaded yet.")
        expect(note.get_by_role("link", name="Refresh libraries on Media Servers")).to_have_attribute(
            "href", "/servers"
        )

    def test_closing_the_dialog_gives_focus_back_to_the_nodes_button_even_after_a_poll(
        self, authed_page: Page, app_url: str
    ) -> None:
        """Focus went to the page itself, and a poll replaced the button while the dialog was open."""
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        api.nodes[0] = {**api.nodes[0], "busy": 1}
        authed_page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(authed_page.locator("#node-n-storage .nd-chips")).to_contain_text("1 file running")

        authed_page.keyboard.press("Escape")

        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#node-n-storage .nd-open")).to_be_focused()

    def test_a_poll_keeps_rows_that_didnt_change_and_the_focus_of_one_that_did(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        authed_page.evaluate("document.querySelector('#node-n-old')._kept = true")
        authed_page.locator("#node-n-storage .nd-open").focus()
        api.nodes[0] = {**api.nodes[0], "busy": 1}
        api.nodes[2] = {**api.nodes[2], "reason": "Movies not found at /media/movies"}
        api.nodes.append(_node("n-new", "new-box"))

        authed_page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

        expect(authed_page.locator("#node-n-storage .nd-chips")).to_contain_text("1 file running")
        expect(authed_page.locator("#node-n-nuc .nd-status")).to_contain_text("Movies not found at /media/movies")
        expect(authed_page.locator("#node-n-new")).to_be_visible()
        expect(authed_page.locator("#node-n-storage .nd-open")).to_be_focused()
        # Seen 10 minutes ago both times: nothing on its row changed, so it is the same element.
        assert authed_page.evaluate("document.querySelector('#node-n-old')._kept") is True
        ids = authed_page.locator("#nodeList > .nd-row").evaluate_all("rows => rows.map((r) => r.dataset.id)")
        assert ids == ["n-storage", "n-pc", "n-nuc", "n-old", "n-new"]

    def test_closing_with_unsaved_mappings_asks_first(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        pane.locator(".nd-node").first.fill("/mnt/media/Movies")
        _node_tab(authed_page, "overview")

        authed_page.keyboard.press("Escape")
        foot = authed_page.locator("#nodeDiscardFoot")
        expect(foot).to_be_visible()
        expect(foot).to_contain_text("Discard the unsaved mappings?")
        expect(authed_page.locator("#node-tab-media")).to_be_visible()
        expect(authed_page.locator("#nodeModal")).to_be_visible()

        authed_page.locator("#nodeKeepEditing").click()
        expect(foot).to_be_hidden()
        expect(pane.locator(".nd-node").first).to_have_value("/mnt/media/Movies")

        authed_page.locator("#nodeModal .btn-close").click()
        authed_page.locator("#nodeDiscardConfirm").click()
        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)

    def test_focus_stays_in_the_dialog_after_a_button_finishes(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        authed_page.locator("#nodeRescanGpus").click()
        expect(authed_page.locator("#nodeRescanResult")).to_have_text("Found 1 working GPU")
        assert authed_page.evaluate("() => !!document.activeElement.closest('#nodeModal')")
        authed_page.keyboard.press("Escape")
        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)

    def test_closing_with_unsaved_settings_asks_first(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeSetName").fill("storage-2")
        authed_page.keyboard.press("Escape")
        expect(authed_page.locator("#nodeDiscardFoot")).to_contain_text("Discard the unsaved settings?")
        expect(authed_page.locator("#nodeModal")).to_be_visible()

    def test_saved_mappings_close_without_asking_and_are_tested(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        pane.locator(".nd-node").first.fill("/mnt/media/Movies")
        with authed_page.expect_response(lambda r: r.url.endswith("/test-access")):
            authed_page.locator("#nodeSaveMappings").click()
        assert api.bodies("POST", "/nodes/n-storage/test-access") == [
            {"path_mappings": [{"app_prefix": "/data/Movies", "node_prefix": "/mnt/media/Movies"}]}
        ]
        authed_page.keyboard.press("Escape")
        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#nodeDiscardFoot")).to_be_hidden()

    def test_use_this_edits_the_row_of_the_library_it_was_suggested_for(self, authed_page: Page, app_url: str) -> None:
        # Both libraries pointed at the node's media root: the natural mistake, and the one a real node can list.
        node = _node(
            "n-storage",
            "storage",
            path_mappings=[
                {"app_prefix": "/data/TV", "node_prefix": "/mnt/media"},
                {"app_prefix": "/data/Movies", "node_prefix": "/mnt/media"},
            ],
        )
        api = _open_nodes(authed_page, app_url, [node])
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        listing = {"/mnt/media": ["Movies", "TV"]}
        api.test_answers = [
            {
                "libraries": [
                    _library("TV", "/data/TV", False, "/mnt/media", nearby=listing,
                             suggestion={"from": "/mnt/media", "to": "/mnt/media/TV"}),
                    _library("Movies", "/data/Movies", False, "/mnt/media", nearby=listing,
                             suggestion={"from": "/mnt/media", "to": "/mnt/media/Movies"}),
                ]
            },
            {"libraries": []},
        ]  # fmt: skip
        pane.locator(".nd-test").click()
        card = pane.locator('.nd-fail[data-library="Movies"]')
        expect(card).to_be_visible()

        with authed_page.expect_response(lambda r: r.url.endswith("/test-access")):
            card.locator(".nd-use").click()

        expect(pane.locator(".nd-node").nth(1)).to_have_value("/mnt/media/Movies")
        expect(pane.locator(".nd-node").nth(0)).to_have_value("/mnt/media")

    def test_a_row_for_the_whole_disk_gets_its_library_chips(self, authed_page: Page, app_url: str) -> None:
        node = _node("n-storage", "storage", path_mappings=[{"app_prefix": "/", "node_prefix": "/mnt/app"}])
        api = _open_nodes(authed_page, app_url, [node])
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        api.test_answers = [{"libraries": [_library("Movies", "/data/Movies", True, "/mnt/app/data/Movies")]}]

        pane.locator(".nd-test").click()

        expect(pane.locator(".nd-map-row").nth(0).locator(".nd-chip-slot")).to_have_text("✓ Movies 3/3")
        expect(pane.locator(".nd-map-results .nd-lib-chips")).to_have_count(0)

    def test_libraries_of_the_same_name_on_two_servers_name_their_server(self, authed_page: Page, app_url: str) -> None:
        """Every Plex has its "Films": three chips reading "Films 0/3" can't be told apart."""
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        api.test_answers = [
            {
                "libraries": [
                    _library("Films", "/data/Movies", False, "/mnt/nas/Movies"),
                    {**_library("Films", "/data/Movies2", False, "/mnt/nas/Movies2"), "server": "Lab"},
                    _library("TV Shows", "/data/TV", True, "/mnt/nas/TV"),
                ]
            }
        ]

        pane.locator(".nd-test").click()

        results = pane.locator(".nd-map")
        expect(pane.locator(".nd-fail").first).to_contain_text("Films (Home):")
        expect(pane.locator(".nd-fail").nth(1)).to_contain_text("Films (Lab):")
        expect(results).to_contain_text("✗ Films (Home) 0/3")
        expect(results).to_contain_text("✗ Films (Lab) 0/3")
        # A name only one server uses stays as it is.
        expect(results).to_contain_text("✓ TV Shows 3/3")
        expect(results).not_to_contain_text("TV Shows (Home)")

    def test_an_empty_library_is_not_a_failure(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        empty = {"server": "Home", "library": "Kids", "app_root": "/data/Movies/Kids", "checked": 0, "ok": False,
                 "files": [], "error": "No video files found near the top of /data/Movies/Kids", "empty": True}  # fmt: skip
        api.test_answers = [{"libraries": [_library("Movies", "/data/Movies", True, "/mnt/nas/Movies"), empty]}]

        pane.locator(".nd-test").click()

        expect(pane.locator(".nd-chip-slot").first).to_contain_text("Kids: no files to test")
        expect(pane.locator(".nd-map-results")).to_contain_text("The node sees every library, same files.")
        expect(pane.locator(".nd-fail")).to_have_count(0)

    def test_a_library_the_node_doesnt_mount_says_so(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        reason = (
            "storage doesn't have this library's folder. Mount it on the node, or leave this library to other workers."
        )
        api.test_answers = [
            {
                "libraries": [
                    _library("Movies", "/data/Movies", True, "/mnt/nas/Movies"),
                    _library("Open Films", "/data/Movies/Open", False, "/mnt/nas/Movies/Open", error=reason),
                ]
            }
        ]

        pane.locator(".nd-test").click()

        card = pane.locator('.nd-fail[data-library="Open Films"]')
        expect(card).to_contain_text(f"Open Films: {reason}")
        expect(card.locator(".nd-use")).to_have_count(0)

    def test_libraries_this_app_cant_open_share_one_neutral_note(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        pane = _node_tab(authed_page, "media")
        skipped = [
            {"server": "Home", "library": f"Synth {i}", "app_root": f"/media/synth{i}", "checked": 0, "ok": False,
             "files": [], "error": f"This app can't open /media/synth{i}"}
            for i in range(3)
        ]  # fmt: skip
        # A second folder of the same library is one more result, not one more library.
        skipped.append(
            {**skipped[0], "app_root": "/media/synth0-old", "error": "This app can't open /media/synth0-old"}
        )
        api.test_answers = [
            {"libraries": [_library("Movies", "/data/Movies", False, "/mnt/nas/Movies"), *skipped]},
        ]
        pane.locator(".nd-test").click()

        results = pane.locator(".nd-map-results")
        expect(results.locator(".nd-fail")).to_have_count(1)
        expect(results.locator(".nd-fail")).to_contain_text("Movies")
        note = results.locator(".nd-skipped")
        expect(note).to_have_count(1)
        expect(note.locator("summary")).to_have_text("3 libraries skipped: this app can't open them.")
        expect(note.locator("li")).to_have_count(3)
        expect(note.locator("a")).to_have_attribute("href", "/servers")
        expect(results).not_to_contain_text("Synth 0: no files to test")

    def test_setup_health_shows_an_informational_failure_as_a_note_not_a_bucket(
        self, authed_page: Page, app_url: str
    ) -> None:
        api = NodesApi(authed_page, _four_nodes())
        note = {
            "id": "node_access_skipped",
            "label": "7 libraries skipped: this app can't open them",
            "ok": False,
            "severity": "info",
            "informational": True,
            "reason": "Home · Synth",
            "help_url": "/servers",
            "help_label": "Fix on Media Servers",
        }
        passing = {"id": "lib_movies", "label": "Movies visible", "ok": True, "severity": "critical"}
        api.health_answer = {"overall_ok": True, "sections": [{"id": "a", "title": "Media", "checks": [passing, note]}]}
        authed_page.goto(f"{app_url}/nodes")
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "health")
        body = authed_page.locator("#nodeHealthBody")
        expect(body.locator(".nd-note")).to_have_text(
            "7 libraries skipped: this app can't open them. Fix on Media Servers"
        )
        expect(body.locator('.readiness-bucket[data-tier="recommended"]')).to_have_count(0)
        expect(authed_page.locator("#nodeHealthSummary")).to_have_text("✓ All good")

    def test_a_node_that_needs_an_update_links_the_steps(self, authed_page: Page, app_url: str) -> None:
        docs = "https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md#updating-a-node"
        api = _open_nodes(authed_page, app_url, [_node("n-old", "old", "update_needed")])
        api.health_answer = {
            "overall_ok": False,
            "sections": [
                {
                    "id": "node_connection",
                    "title": "Connection",
                    "checks": [
                        {
                            "id": "node_version",
                            "label": "The node needs an update",
                            "ok": False,
                            "severity": "critical",
                            "fix_where": "node",
                            "docs_url": docs,
                            "help_label": "How to update a node",
                        }
                    ],
                }
            ],
        }
        _open_node(authed_page, "n-old")
        pane = _node_tab(authed_page, "health")
        link = pane.get_by_role("link", name="How to update a node")
        expect(link).to_have_attribute("href", docs)
        expect(link).to_have_attribute("target", "_blank")
        expect(authed_page.locator("#node-n-old .nd-status .pill:not(.nd-to-fix)").first).to_have_class("pill bad")

    def test_setup_health_uses_the_three_buckets(self, authed_page: Page, app_url: str) -> None:
        api = NodesApi(authed_page, _four_nodes())
        api.health_answer = {
            "overall_ok": False,
            "sections": [
                {
                    "id": "access",
                    "title": "Libraries",
                    "checks": [
                        {
                            "id": "lib_tv",
                            "label": "TV Shows isn't visible on this node",
                            "ok": False,
                            "severity": "critical",
                            "tooltip": "0 of 3 files found at the mapped path.",
                            "current": "/data/TV Shows → /media/tv",
                            "recommended": "/data/TV Shows → /media/TV Shows",
                            "fix_tab": "media",
                        },
                        {"id": "lib_movies", "label": "Movies visible, same files", "ok": True, "severity": "critical"},
                    ],
                },
                {
                    "id": "use",
                    "title": "Use",
                    "checks": [
                        {
                            "id": "no_group",
                            "label": "Not in any worker group yet",
                            "ok": False,
                            "severity": "recommended",
                            "reason": "Its 8 CPU cores sit idle until a group uses them.",
                            "help_url": "/settings#section-workers",
                            "help_label": "Open Workers",
                        },
                        {"id": "hidden", "label": "Internal detail", "ok": True, "severity": "info"},
                    ],
                },
            ],
        }
        authed_page.goto(f"{app_url}/nodes")
        expect(authed_page.locator("#node-n-nuc")).to_be_visible()
        _open_node(authed_page, "n-nuc")
        _node_tab(authed_page, "health")

        expect(authed_page.locator("#nodeHealthTitle")).to_have_text("nuc · Setup Health")
        expect(authed_page.locator("#nodeHealthSummary .pill")).to_have_text(["1 must fix", "1 recommended"])
        expect(authed_page.locator("#nodeHealthTabBadge")).to_have_text("1")
        must = authed_page.locator('.readiness-bucket[data-tier="critical"]')
        expect(must.locator("summary")).to_contain_text("Must fix")
        expect(must).to_contain_text("TV Shows isn't visible on this node")
        expect(must.locator(".readiness-current")).to_contain_text("/data/TV Shows → /media/tv")
        expect(must.locator(".readiness-recommended")).to_contain_text("/data/TV Shows → /media/TV Shows")
        recommended = authed_page.locator('.readiness-bucket[data-tier="recommended"]')
        expect(recommended).to_contain_text("Its 8 CPU cores sit idle")
        expect(recommended.locator("a", has_text="Open Workers")).to_have_attribute("href", "/settings#section-workers")
        good = authed_page.locator('.readiness-bucket[data-tier="ok"]')
        expect(good).to_contain_text("Movies visible, same files")
        expect(authed_page.locator("#nodeHealthBody")).not_to_contain_text("Internal detail")

        must.locator(".nd-health-fix").click()
        expect(authed_page.locator("#node-tab-media")).to_be_visible()

    @pytest.mark.parametrize("width", [390, 1440])
    def test_a_long_reason_shows_in_full_with_its_fix(self, authed_page: Page, app_url: str, width: int) -> None:
        """The reason was cut to one line, which hid the sentence that says what to do."""
        authed_page.set_viewport_size({"width": width, "height": 900})
        api = NodesApi(authed_page, _four_nodes())
        reason = (
            "The node can't write to Plex's database folder /config/Library/Application Support/Plex Media Server. "
            "Give the user the node runs as write access to that folder, then run this check again."
        )
        row = {"id": "w", "label": "Can write the database", "ok": False, "severity": "critical", "reason": reason}
        api.health_answer = {"overall_ok": False, "sections": [{"id": "db", "title": "Plex database", "checks": [row]}]}
        authed_page.goto(f"{app_url}/nodes")
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "health")
        shown = authed_page.locator("#nodeHealthBody .rd-reason")
        expect(shown).to_have_text(reason)
        cut = shown.evaluate("el => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1")
        assert cut is False
        assert shown.evaluate("el => el.getBoundingClientRect().height") > 20  # Wrapped onto more than one line.

    def test_log_tab_shows_the_node_log(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "log")
        expect(authed_page.locator("#nodeLog")).to_have_text("14:01:02 INFO node started\n14:01:03 INFO 1 GPU found")
        api.log_answer = {"lines": [_log("14:05:00", "later")]}
        authed_page.locator("#nodeLogRefresh").click()
        expect(authed_page.locator("#nodeLog")).to_have_text("14:05:00 INFO later")
        assert any("/log" in path for _, path, _ in api.calls)
        # The API's ISO times (already in this app's zone) read as the app's own log reads its times.
        api.log_answer = {"lines": [_log("2026-10-15T20:00:00+11:00", "zoned"), _log("2026-10-15T09:00:01Z", "utc")]}
        authed_page.locator("#nodeLogRefresh").click()
        expect(authed_page.locator("#nodeLog")).to_have_text(
            "2026-10-15 20:00:00 INFO zoned\n2026-10-15 09:00:01 INFO utc"
        )

    def test_settings_save_keeps_the_masked_key(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        expect(authed_page.locator("#nodeSetKey")).to_have_value("****")
        authed_page.locator("#nodeSetName").fill("storage-2")
        authed_page.locator("label[for='nodeSetEnabled']").click()
        authed_page.locator("#nodeSettingsSave").click()
        expect(authed_page.locator("#nodeSettingsResult")).to_have_text("Saved")
        assert api.bodies("PUT", "/nodes/n-storage") == [
            {
                "name": "storage-2",
                "url": "http://storage.lan:9494",
                "key": "****",
                "enabled": False,
                "edit_tag": "n-storage-1",
            }
        ]
        expect(authed_page.locator("#nodeModalName")).to_have_text("storage-2")
        expect(authed_page.locator("#node-n-storage .nd-name")).to_have_text("storage-2")

    def test_a_save_over_a_change_made_in_another_tab_is_refused_and_says_so(
        self, authed_page: Page, app_url: str
    ) -> None:
        page = authed_page
        page.clock.install()
        api = _open_nodes(page, app_url, _four_nodes())
        _open_node(page, "n-storage")
        _node_tab(page, "settings")
        # Another tab renames the node; this tab's next poll reads the new row while the dialog is still open.
        api._find("n-storage").update({"name": "storage-renamed", "edit_tag": "n-storage-2"})
        page.clock.run_for(10_500)
        expect(page.locator("#node-n-storage .nd-name")).to_have_text("storage-renamed")

        page.locator("label[for='nodeSetEnabled']").click()
        page.locator("#nodeSettingsSave").click()

        expect(page.locator("#nodeSettingsResult")).to_have_text(CHANGED_ELSEWHERE)
        assert [body["edit_tag"] for body in api.bodies("PUT", "/nodes/n-storage")] == ["n-storage-1"]
        assert (api._find("n-storage")["name"], api._find("n-storage")["enabled"]) == ("storage-renamed", True)

    def test_a_settings_save_then_a_mappings_save_in_one_dialog_both_go_through(
        self, authed_page: Page, app_url: str
    ) -> None:
        page = authed_page
        api = _open_nodes(page, app_url, _four_nodes())
        _open_node(page, "n-storage")
        _node_tab(page, "settings")
        page.locator("#nodeSetName").fill("storage-2")
        page.locator("#nodeSettingsSave").click()
        expect(page.locator("#nodeSettingsResult")).to_have_text("Saved")

        pane = _node_tab(page, "media")
        pane.locator(".nd-node").first.fill("/mnt/media/Movies")
        page.locator("#nodeSaveMappings").click()

        expect(page.locator("#nodeMediaResult")).to_have_text("Saved")
        assert [body["edit_tag"] for body in api.bodies("PUT", "/nodes/n-storage")] == ["n-storage-1", "n-storage-2"]

    def test_a_new_key_the_node_refuses_is_not_saved(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.probe_answer = {"ok": False, "error": {"kind": "auth", "message": "The node refused this key."}}
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeSetKey").fill("wrong-key")
        authed_page.locator("#nodeSettingsSave").click()

        result = authed_page.locator("#nodeSettingsResult")
        expect(result).to_contain_text("Key refused.")
        expect(result).to_contain_text("Nothing was saved.")
        expect(result).to_have_class("sm-tres bad")
        expect(authed_page.locator("#nodeSetKey")).to_have_class(re.compile(r"\bis-invalid\b"))
        assert api.bodies("POST", "/nodes/probe") == [
            {"id": "n-storage", "url": "http://storage.lan:9494", "key": "wrong-key"}
        ]
        assert not api.bodies("PUT", "/nodes/n-storage")

    def test_a_new_key_the_node_accepts_is_saved(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeSetKey").fill("right-key")
        authed_page.locator("#nodeSettingsSave").click()
        expect(authed_page.locator("#nodeSettingsResult")).to_have_text("Saved")
        assert [b["key"] for b in api.bodies("PUT", "/nodes/n-storage")] == ["right-key"]

    def test_a_node_that_does_not_answer_still_saves_and_says_so(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.probe_answer = {
            "ok": False,
            "error": {"kind": "unreachable", "message": "down", "reason": "refused"},
        }
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeSetUrl").fill("http://storage2.lan:9494")
        authed_page.locator("#nodeSetKey").fill("right-key")
        authed_page.locator("#nodeSettingsSave").click()
        result = authed_page.locator("#nodeSettingsResult")
        expect(result).to_contain_text("Saved. ")
        expect(result).to_have_class("sm-tres warn")
        assert len(api.bodies("PUT", "/nodes/n-storage")) == 1

    def test_send_work_switch_and_its_label_have_room(self, authed_page: Page, app_url: str) -> None:
        _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        switch = authed_page.locator("#nodeSetEnabled").bounding_box()
        label = authed_page.locator("label[for='nodeSetEnabled']").bounding_box()
        assert label["x"] - (switch["x"] + switch["width"]) >= 6

    def test_remove_of_a_node_in_use_says_by_what_before_asking(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-storage")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeRemove").click()
        expect(authed_page.locator("#nodeRemoveResult")).to_have_text(
            'Still in use by worker group "GPU box". Take it out of those first.'
        )
        expect(authed_page.locator("#appConfirmModal")).to_be_hidden()
        assert not [c for c in api.calls if c[0] == "DELETE"]

    def test_remove_refused_while_in_use_names_the_users(self, authed_page: Page, app_url: str) -> None:
        # The list said nothing used it; the server, which knows better, refuses.
        api = _open_nodes(authed_page, app_url, _four_nodes())
        api.delete_answer = (
            409,
            {
                "error": "in use",
                "used_by": [
                    {"kind": "group", "id": "g1", "name": "GPU box"},
                    {"kind": "server", "id": "plex-1", "name": "Home"},
                ],
            },
        )
        _open_node(authed_page, "n-nuc")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeRemove").click()
        accept_app_confirm(authed_page)
        expect(authed_page.locator("#nodeRemoveResult")).to_have_text(
            'Still in use by worker group "GPU box", Plex server "Home". Take it out of those first.'
        )
        expect(authed_page.locator("#nodeModal")).to_be_visible()

    def test_remove_drops_the_row(self, authed_page: Page, app_url: str) -> None:
        api = _open_nodes(authed_page, app_url, _four_nodes())
        _open_node(authed_page, "n-old")
        _node_tab(authed_page, "settings")
        authed_page.locator("#nodeRemove").click()
        accept_app_confirm(authed_page)
        expect(authed_page.locator("#nodeModal")).to_be_hidden(timeout=5000)
        expect(authed_page.locator("#node-n-old")).to_have_count(0)
        assert [c[1] for c in api.calls if c[0] == "DELETE"] == ["/nodes/n-old"]


@pytest.mark.e2e
class TestPlexDatabaseSelector:
    def _open(
        self, page: Page, app_url: str, agent: dict | None, details: dict | None = None, nodes: list | None = None
    ) -> dict:
        server = _plex_server()
        if agent is not None:
            server["markers"]["plex"]["agent"] = agent
        captured = _mock_server_page(page, server, _status(server, "ready", "", details or _plex_ready_details()))
        NodesApi(page, nodes or _four_nodes())
        _open_tab(page, app_url, server, tab="general")
        # The node list has answered once the gaming-pc entry is there.
        expect(page.locator("#markersPlexDatabase option[value='n-pc']")).to_be_attached(timeout=5000)
        return captured

    def test_lists_only_plex_database_nodes(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, None)
        # storage, nuc and old-laptop are worker-only nodes: not a route to Plex's database.
        expect(authed_page.locator("#markersPlexDatabase option")).to_have_text(
            ["Open it from this app", "Through a Plex database node: gaming-pc", "Add a Plex database node…"]
        )
        expect(authed_page.locator("#markersPlexDatabase")).to_have_value("")
        expect(authed_page.locator("#markersAgentState")).to_be_hidden()

    def test_choosing_a_node_saves_enabled_and_node_id(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, None)
        authed_page.locator("#markersPlexDatabase").select_option("n-pc")
        expect(authed_page.locator("#markersAgentState")).to_contain_text("Save to use gaming-pc")
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["markers"]["plex"]["agent"] == {"enabled": True, "node_id": "n-pc"}

    def test_opening_from_this_app_saves_it_off(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, {"enabled": True, "node_id": "n-pc"})
        authed_page.locator("#markersPlexDatabase").select_option("")
        expect(authed_page.locator("#markersAgentState")).to_be_hidden()
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["markers"]["plex"]["agent"] == {"enabled": False, "node_id": None}

    def test_untouched_server_without_a_route_posts_no_agent(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, None)
        body = _save_and_read_put(authed_page, "plex-1")
        assert "agent" not in body["markers"]["plex"]

    def test_status_line_names_the_node_and_its_version(self, authed_page: Page, app_url: str) -> None:
        details = {**_plex_ready_details(), "agent": {"state": "connected", "version": APP_VERSION, "url": "x"}}
        self._open(authed_page, app_url, {"enabled": True, "node_id": "n-pc"}, details)
        expect(authed_page.locator("#markersPlexDatabase")).to_have_value("n-pc")
        state = authed_page.locator("#markersAgentState")
        expect(state).to_contain_text("✓ Connected")
        expect(state).to_contain_text(f"gaming-pc · v{APP_VERSION}")
        expect(state.locator("#markersAgentCheckBtn")).to_be_visible()

    @pytest.mark.parametrize(
        ("agent_state", "badge"),
        [("unreachable", "Can't reach it"), ("rejected", "Key refused"), ("incompatible", "Update needed")],
    )
    def test_status_line_keeps_the_problem_badges(
        self, authed_page: Page, app_url: str, agent_state: str, badge: str
    ) -> None:
        details = {**_plex_ready_details(), "agent": {"state": agent_state}}
        self._open(authed_page, app_url, {"enabled": True, "node_id": "n-pc"}, details)
        expect(authed_page.locator("#markersAgentState")).to_contain_text(badge)

    def test_a_node_no_longer_listed_stays_selected(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, {"enabled": True, "node_id": "n-gone"})
        select = authed_page.locator("#markersPlexDatabase")
        expect(select).to_have_value("n-gone")
        expect(select.locator("option[value='n-gone']")).to_contain_text("not on the Nodes page any more")
        body = _save_and_read_put(authed_page, "plex-1")
        assert body["markers"]["plex"]["agent"] == {"enabled": True, "node_id": "n-gone"}

    def test_a_saved_node_that_hasnt_answered_yet_keeps_its_name(self, authed_page: Page, app_url: str) -> None:
        # After a restart a node reports no roles until its first check answers, so the role filter misses it.
        waking = _node("n-wake", "plex-box", "connecting", roles=[], gpus=[], cpu={})
        self._open(authed_page, app_url, {"enabled": True, "node_id": "n-wake"}, nodes=[*_four_nodes(), waking])
        select = authed_page.locator("#markersPlexDatabase")
        expect(select).to_have_value("n-wake")
        expect(select.locator("option[value='n-wake']")).to_have_text("Through a Plex database node: plex-box")

    def test_add_a_node_opens_the_nodes_page_and_keeps_the_choice(self, authed_page: Page, app_url: str) -> None:
        self._open(authed_page, app_url, None)
        with authed_page.context.expect_page() as opened:
            authed_page.locator("#markersPlexDatabase").select_option("__add_node__")
        new_page = opened.value
        new_page.wait_for_load_state("domcontentloaded")
        assert new_page.url.endswith("/nodes#add")
        new_page.close()
        expect(authed_page.locator("#markersPlexDatabase")).to_have_value("")

    def test_setup_health_link_lands_on_the_selector(self, authed_page: Page, app_url: str) -> None:
        server = _plex_server()
        _mock_server_page(authed_page, server, _status(server, "ready", "", _plex_ready_details()))
        NodesApi(authed_page, _four_nodes())
        check = {
            "id": "chapters_helper",
            "label": "Chapter thumbnails need Plex's database",
            "ok": False,
            "severity": "recommended",
            "reason": "Plex runs on another machine.",
            "actions": {},
            "note": {"text": "Pick a Plex database node on the Connection tab.", "configure_plex_helper": True},
        }
        section = {"id": "chapters", "title": "Chapters", "checks": [check]}
        envelope = {"vendor": "plex", "overall_ok": False, "sections": [section]}
        authed_page.route("**/api/servers/*/previews-readiness", lambda route: _fulfill(route, envelope))
        _open_tab(authed_page, app_url, server, tab="health")

        authed_page.locator(".chapter-helper-link").click()

        expect(authed_page.locator("#edit-tab-general")).to_be_visible()
        expect(authed_page.locator("#markersPlexDatabase")).to_be_in_viewport()


@pytest.mark.e2e
def test_nodes_page_screenshot(authed_page: Page, app_url: str, tmp_path: Path) -> None:
    authed_page.set_viewport_size({"width": 1440, "height": 900})
    _open_nodes(authed_page, app_url, _four_nodes())
    expect(authed_page.locator(".nd-row")).to_have_count(4)
    _capture(authed_page.locator(".nodes-page"), "nodes-page.png", tmp_path)
