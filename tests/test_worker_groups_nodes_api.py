"""The Workers API with node members: saving, node rows for the picker, member rows and idle worker names."""

from __future__ import annotations

import pytest

from media_preview_generator.nodes.registry import STATE_OFFLINE, set_registry
from media_preview_generator.web.settings_manager import get_settings_manager

from .test_settings_save_keeps_pause import TOKEN, _reset_singletons, app  # noqa: F401
from .test_worker_group_nodes import NODE_GPU, FakeRegistry, group, member

LOCAL_GPU = {"device": "cuda:0", "name": "NVIDIA RTX 4090", "type": "nvidia", "status": "ok"}


@pytest.fixture
def registry():
    fake = FakeRegistry()
    set_registry(fake)
    yield fake
    set_registry(None)


@pytest.fixture
def client(app, monkeypatch):  # noqa: F811 — pytest injects the imported shared fixture.
    monkeypatch.setattr("media_preview_generator.web.routes._helpers._ensure_gpu_cache", lambda: [LOCAL_GPU])
    monkeypatch.setattr("media_preview_generator.web.routes.api_jobs._ensure_gpu_cache", lambda: [LOCAL_GPU])
    with app.app_context():
        get_settings_manager().set("nodes", [{"id": "storage", "name": "storage"}])
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_TOKEN"] = TOKEN
    return client


def put(client, groups):
    state = client.get("/api/worker-groups").get_json()
    return client.put("/api/worker-groups", json={"groups": groups, "revision": state["revision"]})


def mixed_group():
    return group(
        "night",
        member("ngpu", "gpu", "cuda:0", count=2, node="storage"),
        member("ncpu", count=1, node="storage"),
        member("lgpu", "gpu", "cuda:0", count=1),
    )


class TestSave:
    def test_member_on_an_added_node_is_saved_with_its_node(self, client, registry):
        registry.put("storage")
        response = put(client, [mixed_group()])
        assert response.status_code == 200, response.get_json()
        members = response.get_json()["groups"][0]["members"]
        assert [m.get("node") for m in members] == ["storage", "storage", None]

    def test_member_on_an_unknown_node_is_refused(self, client, registry):
        response = put(client, [group("g", member("a", node="laptop"))])
        assert response.status_code == 400
        assert "node laptop is not added" in response.get_json()["error"]

    def test_member_on_a_plex_database_node_is_refused(self, client, registry):
        """Applying "plex-db · CPU" used to say "Worker groups saved", then the row turned "doesn't run workers"."""
        registry.put("storage", name="plex-db", roles=("plex_db",), gpus=())
        response = put(client, [group("g", member("a", node="storage"))])
        assert response.status_code == 400
        assert response.get_json()["error"] == (
            "plex-db doesn't run workers: it is a Plex database node. Pick another device."
        )
        assert not any(saved["id"] == "g" for saved in get_settings_manager().worker_groups)

    def test_member_already_saved_on_a_node_that_turned_out_not_to_run_workers_keeps_saving(self, client, registry):
        registry.put("storage")
        assert put(client, [group("g", member("a", node="storage"))]).status_code == 200
        registry.put("storage", roles=("plex_db",), gpus=())
        response = put(client, [group("g", member("a", node="storage")), group("h", member("b"))])
        assert response.status_code == 200, response.get_json()

    def test_member_on_a_node_not_checked_yet_is_saved(self, client, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline", roles=())
        assert put(client, [group("g", member("a", node="storage"))]).status_code == 200

    def test_legacy_cpu_count_control_ignores_node_cpu_members(self, client, registry):
        registry.put("storage")
        assert put(client, [group("g", member("ncpu", count=3, node="storage"))]).status_code == 200
        assert client.post("/api/settings", json={"cpu_threads": 4}).status_code == 200
        groups = get_settings_manager().worker_groups
        node_member = next(m for g in groups for m in g["members"] if m.get("node"))
        assert node_member["count"] == 3


class TestPayload:
    def test_nodes_list_gives_the_picker_state_devices_and_cores_without_secrets(self, client, registry):
        registry.put("storage", name="Storage box")
        registry.put("nuc", state=STATE_OFFLINE, reason="node offline", gpus=())
        nodes = client.get("/api/worker-groups").get_json()["nodes"]
        assert nodes == [
            {
                "id": "storage",
                "name": "Storage box",
                "state": "ready",
                "reason": "",
                "roles": ["worker"],
                "devices": [NODE_GPU],
                "cpu_cores": 24,
            },
            {
                "id": "nuc",
                "name": "nuc",
                "state": "offline",
                "reason": "node offline",
                "roles": ["worker"],
                "devices": [],
                "cpu_cores": 24,
            },
        ]

    def test_nodes_list_says_which_node_doesnt_run_workers(self, client, registry):
        """The picker offered a Plex database node's "CPU": the rows didn't say what the node does."""
        registry.put("storage", roles=("plex_db",), gpus=())
        nodes = client.get("/api/worker-groups").get_json()["nodes"]
        assert [(n["id"], n["roles"]) for n in nodes] == [("storage", ["plex_db"])]

    def test_node_members_show_node_name_state_and_reason(self, client, registry):
        registry.put("storage")
        assert put(client, [mixed_group()]).status_code == 200
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        payload = client.get("/api/worker-groups").get_json()
        rows = {row["id"]: row for row in payload["capacity"]["groups"][0]["members"]}
        assert {k: rows["ngpu"][k] for k in ("node", "node_name", "state", "reason", "target")} == {
            "node": "storage",
            "node_name": "storage",
            "state": "node_unavailable",
            "reason": "node offline",
            "target": 0,
        }
        assert (rows["lgpu"]["node"], rows["lgpu"]["state"], rows["lgpu"]["reason"]) == (None, "active", "")
        codes = [(w["code"], w.get("member_id")) for w in payload["warnings"]]
        assert ("node_unavailable", "ngpu") in codes and ("node_unavailable", "ncpu") in codes
        message = next(w["message"] for w in payload["warnings"] if w.get("member_id") == "ngpu")
        assert (
            message
            == f"night: storage · {NODE_GPU['name']} has no workers now (node offline). Its jobs wait; other devices "
            "keep working."
        )

    def test_group_of_only_offline_node_members_reads_node_unavailable(self, client, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        assert put(client, [group("g", member("ncpu", node="storage"))]).status_code == 200
        row = client.get("/api/worker-groups").get_json()["capacity"]["groups"][0]
        assert row["state"] == "node_unavailable"

    def test_current_and_peak_count_only_this_apps_workers(self, client, registry):
        registry.put("storage")
        assert put(client, [mixed_group()]).status_code == 200
        capacity = client.get("/api/worker-groups").get_json()["capacity"]
        assert capacity["current"] == {"cpu": 0, "gpu": 1}
        assert capacity["peak"] == {"cpu": 0, "gpu": 1}
        # Node members are counted apart, so Settings can say "... here, 2 GPU + 1 CPU on nodes".
        assert capacity["nodes_current"] == {"cpu": 1, "gpu": 2}
        assert capacity["nodes_peak"] == {"cpu": 1, "gpu": 2}

    def test_node_members_on_an_offline_node_count_in_the_peak_but_not_now(self, client, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        assert put(client, [mixed_group()]).status_code == 200
        capacity = client.get("/api/worker-groups").get_json()["capacity"]
        assert capacity["nodes_current"] == {"cpu": 0, "gpu": 0}
        assert capacity["nodes_peak"] == {"cpu": 1, "gpu": 2}

    def test_registry_failure_leaves_an_empty_nodes_list(self, client, registry, monkeypatch):
        def broken():
            raise RuntimeError("down")

        monkeypatch.setattr(registry, "views", broken)
        assert client.get("/api/worker-groups").get_json()["nodes"] == []


class TestIdleWorkers:
    def test_idle_list_names_node_workers_after_their_node(self, client, registry, app):  # noqa: F811
        registry.put("storage", name="Storage box")
        assert put(client, [mixed_group()]).status_code == 200
        from media_preview_generator.web.routes.api_jobs import _build_idle_workers_from_config

        with app.app_context():
            rows = _build_idle_workers_from_config()
        names = [(row["member_id"], row["worker_name"], row["node"], row["node_state"]) for row in rows]
        assert names == [
            ("ngpu", "Storage box · GPU Worker 1 (Quadro P5000)", {"id": "storage", "name": "Storage box"}, "ready"),
            ("ngpu", "Storage box · GPU Worker 2 (Quadro P5000)", {"id": "storage", "name": "Storage box"}, "ready"),
            ("ncpu", "Storage box · CPU Worker 1", {"id": "storage", "name": "Storage box"}, "ready"),
            ("lgpu", "night 1", None, None),
        ]
        assert rows[0]["group_resource"] == "node:storage:gpu:cuda:0"

    def test_idle_list_leaves_out_members_on_an_offline_node(self, client, registry, app):  # noqa: F811
        registry.put("storage")
        assert put(client, [mixed_group()]).status_code == 200
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        from media_preview_generator.web.routes.api_jobs import _build_idle_workers_from_config

        with app.app_context():
            rows = _build_idle_workers_from_config()
        assert [row["member_id"] for row in rows] == ["lgpu"]
