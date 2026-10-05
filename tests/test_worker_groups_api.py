"""Actual policy API persistence, concurrency guards and legacy compatibility."""

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager

from .test_settings_save_keeps_pause import TOKEN, _reset_singletons, app  # noqa: F401
from .test_worker_group_policy import cpu_group


@pytest.fixture
def client(app):  # noqa: F811 — pytest injects the imported shared fixture.
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_TOKEN"] = TOKEN
    return client


def save(client, groups):
    state = client.get("/api/worker-groups").get_json()
    response = client.put("/api/worker-groups", json={"groups": groups, "revision": state["revision"]})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_policy_survives_reload_and_stale_edit_cannot_undo_scaling(client):
    state = save(client, [cpu_group()])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": 1})
    assert response.status_code == 200
    assert response.get_json()["groups"][0]["count"] == 3
    stale = client.put("/api/worker-groups", json={"groups": state["groups"], "revision": state["revision"]})
    assert stale.status_code == 409
    assert client.get("/api/worker-groups").get_json()["groups"][0]["count"] == 3


def test_last_group_disable_keeps_manual_and_quiet_holds_independent(client):
    save(client, [cpu_group(count=1)])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": -1}).get_json()
    assert response["groups"][0]["count"] == 1
    assert response["groups"][0]["enabled"] is False
    assert response["processing_paused"] is False
    client.post("/api/processing/pause")
    enabled = client.post("/api/worker-groups/cpu-a/scale", json={"enabled": True}).get_json()
    assert enabled["processing_paused"] is True
    assert enabled["capacity"]["groups"][0]["available"] == 0
    settings = get_settings_manager()
    settings.set_processing_pause_reason("quiet_hours", True)
    resumed = client.post("/api/processing/resume").get_json()
    assert resumed == {"paused": True, "reasons": ["quiet_hours"]}


def test_quick_scale_uses_aggregate_validation_and_preserves_revision_on_failure(client):
    state = save(client, [cpu_group(count=16), cpu_group(id="cpu-b", count=16)])
    response = client.post("/api/worker-groups/cpu-a/scale", json={"delta": 1})
    assert response.status_code == 400
    current = client.get("/api/worker-groups").get_json()
    assert current["revision"] == state["revision"]
    assert current["capacity"]["peak"]["cpu"] == 32


def test_gpu_cannot_accept_loudness_and_bad_policy_is_not_saved(client):
    state = save(client, [cpu_group()])
    response = client.put(
        "/api/worker-groups",
        json={"revision": state["revision"], "groups": [cpu_group(resource="gpu", device="cuda:0")]},
    )
    assert response.status_code == 400
    assert "requires CPU" in response.get_json()["error"]
    assert client.get("/api/worker-groups").get_json()["groups"] == state["groups"]


def test_legacy_count_updates_one_group_but_refuses_ambiguous_resource(client):
    save(client, [cpu_group()])
    response = client.post("/api/settings", json={"cpu_threads": 4})
    assert response.status_code == 200
    assert get_settings_manager().worker_groups[0]["count"] == 4
    assert client.post("/api/settings", json={"cpu_threads": -1}).status_code == 400
    assert client.post("/api/settings", json={"cpu_threads": 33}).status_code == 400
    save(client, [cpu_group(), cpu_group(id="cpu-b")])
    assert client.post("/api/settings", json={"cpu_threads": 1}).status_code == 400
    assert client.post("/api/workers/add", json={"worker_type": "CPU", "count": 1}).status_code == 409


def test_gpu_tuning_does_not_change_group_counts_or_permissions(client):
    group = cpu_group(resource="gpu", device="cuda:0", job_types=["previews"])
    save(client, [group])
    response = client.post("/api/settings", json={"gpu_config": [{"device": "cuda:0", "ffmpeg_threads": 4}]})
    assert response.status_code == 200
    settings = get_settings_manager()
    assert settings.worker_groups == [group]
    assert settings.gpu_config[0]["ffmpeg_threads"] == 4


def test_intentional_empty_groups_do_not_fall_back_to_legacy_counts(client):
    response = save(client, [])
    assert response["groups"] == []
    assert response["capacity"]["current"] == {"cpu": 0, "gpu": 0}
    assert response["processing_paused"] is False
    settings = get_settings_manager()
    assert settings.cpu_threads == settings.gpu_threads == 0


def test_configuration_warning_accounts_for_global_quiet_hours(client):
    group = cpu_group(
        job_types=["previews"],
        availability={"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]},
    )
    save(client, [group])
    get_settings_manager().set(
        "quiet_hours",
        {
            "enabled": True,
            "day_basis": "start",
            "windows": [{"days": ["mon"], "start": "23:00", "end": "07:00"}],
        },
    )
    payload = client.get("/api/worker-groups").get_json()
    assert any(w["code"] == "no_eligible_workers" and w["job_type"] == "previews" for w in payload["warnings"])
