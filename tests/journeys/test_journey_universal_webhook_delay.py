"""Universal webhook delays retain vendor routing through real job dispatch."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.web import jobs
from media_preview_generator.web import webhooks as wh
from media_preview_generator.web.app import _requeue_interrupted_on_startup
from media_preview_generator.web.routes import job_runner

from . import test_journey_webhook_delay as delay_tests

pytestmark = pytest.mark.journey
app = delay_tests.app
_reset_singletons = delay_tests._reset_singletons
delay_flow = delay_tests.delay_flow
_HEADERS = {"X-Auth-Token": "test-token-12345678"}
_PATH = "/data/movie.mkv"
_PROVIDERS = ("plex", "emby", "jellyfin", "sonarr", "radarr", "path")
_SERVER_IDS = {"plex": "plex-1", "emby": "emby-1", "jellyfin": "jelly-1"}


def _payload(provider: str, *, ignored: bool = False) -> dict:
    if provider == "plex":
        return {
            "event": "media.play" if ignored else "library.new",
            "Metadata": {"ratingKey": "42", "type": "movie", "title": "Movie"},
        }
    if provider == "emby":
        return {
            "Event": "playback.start" if ignored else "library.new",
            "Item": {"Id": "42"},
            "Server": {"Id": "emby-1"},
        }
    if provider == "jellyfin":
        return {
            "NotificationType": "PlaybackStart" if ignored else "ItemAdded",
            "ItemId": "42",
            "ItemType": "Movie",
            "ServerId": "jelly-1",
        }
    if provider in ("sonarr", "radarr"):
        payload = delay_tests._payload(provider, _PATH)
        if ignored:
            payload["eventType"] = "Test"
        return payload
    return {"path": _PATH, **({"eventType": "Test"} if ignored else {})}


def _url(provider: str, scoped: bool, query: str = "") -> str:
    suffix = "server/" + _SERVER_IDS.get(provider, "plex-1") if scoped else "incoming"
    return f"/api/webhooks/{suffix}{query}"


def _post(flow, provider: str, scoped: bool, query: str = "", *, ignored: bool = False, headers=None):
    payload = _payload(provider, ignored=ignored)
    kwargs = {"data": {"payload": json.dumps(payload)}} if provider == "plex" else {"json": payload}
    return flow.client.post(_url(provider, scoped, query), headers=headers or _HEADERS, **kwargs)


@pytest.fixture
def vendor_flow(delay_flow, monkeypatch):
    servers = delay_flow.settings.get("media_servers")
    delay_flow.settings.set(
        "media_servers",
        [
            *servers,
            {
                "id": "jelly-1",
                "type": "jellyfin",
                "name": "Jellyfin",
                "enabled": True,
                "url": "http://jellyfin:8096",
                "auth": {"method": "api_key", "api_key": "key"},
                "libraries": [{"id": "3", "name": "Movies", "enabled": True}],
                "output": {"adapter": "jellyfin_trickplay"},
            },
        ],
    )
    for server_type in (PlexServer, EmbyServer, JellyfinServer):
        monkeypatch.setattr(server_type, "resolve_item_to_remote_paths", lambda self, item_id: [(item_id, _PATH)])
    return delay_flow


def _assert_dispatch(flow, provider: str, scoped: bool, *, regenerate: bool = False, path: str = _PATH) -> None:
    config = flow.run.call_args.args[0]
    server_id = _SERVER_IDS.get(provider, "plex-1")
    assert config.webhook_paths == [path]
    assert config.webhook_source == provider
    assert config.server_id_filter == (server_id if scoped else None)
    assert config.regenerate_thumbnails is regenerate
    expected_hints = {path: {server_id: "42"}} if provider in _SERVER_IDS else {}
    assert (config.webhook_item_id_hints or {}) == expected_hints


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("scoped", [False, True], ids=["incoming", "server"])
@pytest.mark.parametrize("query,expected", [("", 300), ("?delay=1", 1), ("?delay=3600", 3600)])
def test_all_router_payloads_wait_then_forward_real_config(vendor_flow, provider, scoped, query, expected):
    flow = vendor_flow
    response = _post(flow, provider, scoped, query)
    assert response.status_code == 202
    result = response.get_json()
    assert result["status"] == "queued"
    assert result["kind"] == provider
    assert result["canonical_path"] == _PATH
    job = jobs.get_job_manager().get_job(result["job_id"])
    assert job.status == jobs.JobStatus.PENDING
    assert job.config["webhook_paths"] == [_PATH]
    assert job.config["webhook_debounce_pending"] is True
    assert datetime.fromisoformat(job.config["webhook_fire_at"]) == flow.clock.now + timedelta(seconds=expected)
    assert flow.timers[-1].interval == expected
    assert flow.settings.get("webhook_delay") == 300
    flow.run.assert_not_called()
    job_runner._start_job_async(job.id, dict(job.config))
    flow.run.assert_not_called()
    flow.clock.now += timedelta(seconds=expected)
    flow.timers[-1].fire()
    assert flow.run.call_count == 1
    _assert_dispatch(flow, provider, scoped)
    assert job.status == jobs.JobStatus.COMPLETED


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("scoped", [False, True])
def test_validation_precedes_vendor_resolution_and_test_events(vendor_flow, provider, scoped):
    flow = vendor_flow
    for query in (
        "delay=3601",
        "delay=0",
        "delay=",
        "delay=1&delay=2",
        "delay=-1",
        "delay=1.5",
        "delay=%2B1",
        "delay=%201",
    ):
        for ignored in (False, True):
            response = _post(flow, provider, scoped, "?" + query, ignored=ignored)
            assert response.status_code == 400, (provider, query, response.get_json())
            assert "delay" in response.get_json()["error"]
    assert not jobs.get_job_manager().get_all_jobs()
    assert not flow.timers
    flow.run.assert_not_called()


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("scoped", [False, True])
def test_test_and_ignored_events_never_create_processing_jobs(vendor_flow, provider, scoped):
    flow = vendor_flow
    response = _post(flow, provider, scoped, "?delay=3600", ignored=True)
    assert response.status_code in (200, 202)
    assert not jobs.get_job_manager().get_all_jobs()
    assert not flow.timers
    flow.run.assert_not_called()


@pytest.mark.parametrize("scoped", [False, True])
def test_auth_is_required_before_delay_validation(vendor_flow, scoped):
    response = _post(vendor_flow, "plex", scoped, "?delay=3601", headers={"X-Auth-Token": "wrong"})
    assert response.status_code == 401
    assert not jobs.get_job_manager().get_all_jobs()
    assert not vendor_flow.timers


@pytest.mark.parametrize("provider", ["plex", "emby", "jellyfin"])
@pytest.mark.parametrize("scoped", [False, True])
def test_multi_version_jobs_wait_independently_and_preserve_hints_and_regeneration(
    vendor_flow, monkeypatch, provider, scoped
):
    flow = vendor_flow
    server_class = {"plex": PlexServer, "emby": EmbyServer, "jellyfin": JellyfinServer}[provider]
    paths = ["/data/movie-1080p.mkv", "/data/movie-2160p.mkv"]
    monkeypatch.setattr(
        server_class, "resolve_item_to_remote_paths", lambda self, item_id: [(item_id, p) for p in paths]
    )
    response = _post(flow, provider, scoped, "?delay=30&regenerate=true")
    assert response.status_code == 202
    body = response.get_json()
    assert body["status"] == "queued" and body["version_count"] == 2
    assert len({row["job_id"] for row in body["jobs"]}) == 2
    assert [row["canonical_path"] for row in body["jobs"]] == paths
    assert len(flow.timers) == 2
    flow.run.assert_not_called()
    flow.clock.now += timedelta(seconds=30)
    for index, timer in enumerate(list(flow.timers)):
        timer.fire()
        assert flow.run.call_count == index + 1
        _assert_dispatch(flow, provider, scoped, regenerate=True, path=paths[index])


@pytest.mark.parametrize("provider", ["plex", "emby", "jellyfin"])
def test_restart_recovers_vendor_wait_and_all_dispatch_fields(vendor_flow, tmp_path, monkeypatch, provider):
    flow = vendor_flow
    response = _post(flow, provider, True, "?delay=30&regenerate=true")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    original_config = dict(jobs.get_job_manager().get_job(job_id).config)
    wh.reset_webhook_debounce()
    flow.clock.now += timedelta(seconds=10)
    reloaded = jobs.JobManager(config_dir=str(tmp_path / "config"))
    monkeypatch.setattr(jobs, "_job_manager", reloaded)
    try:
        assert reloaded.get_job(job_id).config == original_config
        _requeue_interrupted_on_startup(str(tmp_path / "config"))
        flow.run.assert_not_called()
        assert flow.timers[-1].interval == 20
        flow.clock.now += timedelta(seconds=20)
        flow.timers[-1].fire()
        assert flow.run.call_count == 1
        _assert_dispatch(flow, provider, True, regenerate=True)
    finally:
        if reloaded._retention_timer:
            reloaded._retention_timer.cancel()


@pytest.mark.parametrize("action", ["cancel", "delete", "fire_now"])
def test_vendor_timer_cannot_resurrect_or_duplicate_finished_job(vendor_flow, action):
    flow = vendor_flow
    response = _post(flow, "plex", True, "?delay=30&regenerate=true")
    job_id = response.get_json()["job_id"]
    manager = jobs.get_job_manager()
    timer = flow.timers[-1]
    if action == "cancel":
        manager.cancel_job(job_id)
    elif action == "delete":
        assert manager.delete_job(job_id)
    else:
        response = flow.client.post(f"/api/jobs/{job_id}/fire-webhook-now", headers=_HEADERS)
        assert response.status_code == 202
        assert flow.run.call_count == 1
        _assert_dispatch(flow, "plex", True, regenerate=True)
    flow.clock.now += timedelta(seconds=30)
    timer.fire()
    assert flow.run.call_count == (1 if action == "fire_now" else 0)


def test_duplicate_native_event_keeps_deadline_and_does_not_start_markers_early(vendor_flow):
    from unittest.mock import patch

    flow = vendor_flow
    with patch("media_preview_generator.markers.triggers.submit_pending_follow_up", return_value=[]) as follow_up:
        first = _post(flow, "jellyfin", True, "?delay=30").get_json()
        job = jobs.get_job_manager().get_job(first["job_id"])
        deadline = job.config["webhook_fire_at"]
        timer = flow.timers[-1]
        flow.clock.now += timedelta(seconds=10)
        duplicate = _post(flow, "jellyfin", True, "?delay=3600")
        assert duplicate.status_code == 202
        assert duplicate.get_json()["status"] == "ignored_duplicate"
        assert job.config["webhook_fire_at"] == deadline
        assert flow.timers == [timer]
        follow_up.assert_not_called()
        flow.run.assert_not_called()
        flow.clock.now += timedelta(seconds=20)
        timer.fire()
        _assert_dispatch(flow, "jellyfin", True)
        follow_up.assert_called_once()
        assert follow_up.call_args.args[0] == job.id
        overrides = follow_up.call_args.args[1]
        assert overrides["server_id"] == "jelly-1"
        assert overrides["webhook_paths"] == [_PATH]
        assert overrides["webhook_item_id_hints"] == {_PATH: {"jelly-1": "42"}}
        assert overrides["source"] == "jellyfin"


def test_paused_vendor_deadline_finalizes_then_resume_uses_current_config(vendor_flow):
    flow = vendor_flow
    response = _post(flow, "emby", True, "?delay=30&regenerate=true")
    job = jobs.get_job_manager().get_job(response.get_json()["job_id"])
    stale_config = dict(job.config)
    flow.settings.processing_paused = True
    flow.clock.now += timedelta(seconds=30)
    flow.timers[-1].fire()
    flow.run.assert_not_called()
    assert job.status == jobs.JobStatus.PENDING
    assert not job.config.get("webhook_debounce_pending")
    flow.settings.processing_paused = False
    job_runner._start_job_async(job.id, stale_config)
    assert flow.run.call_count == 1
    _assert_dispatch(flow, "emby", True, regenerate=True)


def test_manual_vendor_reprocess_removes_delay_mode_but_preserves_routing(vendor_flow):
    flow = vendor_flow
    response = _post(flow, "plex", True, "?delay=3600&regenerate=true")
    manager = jobs.get_job_manager()
    original = manager.get_job(response.get_json()["job_id"])
    manager.cancel_job(original.id)
    replay = flow.client.post(f"/api/jobs/{original.id}/reprocess", headers=_HEADERS)
    assert replay.status_code == 201
    assert flow.run.call_count == 1
    _assert_dispatch(flow, "plex", True, regenerate=True)
    job = manager.get_job(replay.get_json()["id"])
    assert not {"webhook_delay_mode", "webhook_debounce_pending", "webhook_fire_at"}.intersection(job.config)


def test_vendor_retry_wait_is_independent_of_initial_delay(vendor_flow):
    from unittest.mock import patch

    flow = vendor_flow
    manager = jobs.get_job_manager()
    run_count = 0

    def process(config, selected_gpus, **kwargs):
        nonlocal run_count
        run_count += 1
        job = manager.get_job(kwargs["job_id"])
        parent_id = job.config.get("parent_job_id") or job.id
        outcome = "skipped_file_not_found" if run_count == 1 else "generated"
        manager.record_file_result(parent_id, _PATH, outcome, "", "worker")
        return {"outcome": {outcome: 1}}

    flow.run.side_effect = process
    response = _post(flow, "plex", True, "?delay=3600")
    original = manager.get_job(response.get_json()["job_id"])
    flow.clock.now += timedelta(seconds=3600)
    with patch("time.sleep"), patch.object(flow.settings, "get", wraps=flow.settings.get) as get:
        flow.timers[-1].fire()
    assert flow.run.call_count == 2
    initial_config = flow.run.call_args_list[0].args[0]
    assert initial_config.webhook_source == "plex"
    for call in flow.run.call_args_list:
        config = call.args[0]
        assert config.webhook_paths == [_PATH]
        assert config.webhook_source == "plex"
        assert config.server_id_filter == "plex-1"
        assert config.webhook_item_id_hints == {_PATH: {"plex-1": "42"}}
    retry = next(job for job in manager.get_all_jobs() if job.config.get("parent_job_id") == original.id)
    assert retry.config["parent_job_id"] == original.id
    assert retry.config["retry_delay"] > 0
    assert retry.config["scheduled_at"]
    assert not {"webhook_delay_mode", "webhook_debounce_pending", "webhook_fire_at"}.intersection(retry.config)
    assert len(flow.timers) == 1
    assert not any(call.args[0] == "webhook_delay" for call in get.call_args_list)
