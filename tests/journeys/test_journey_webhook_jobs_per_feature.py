"""A webhook creates the jobs its file needs when it arrives: previews, Intro & Credits, loudness, each only where that
feature is on for the file's library, and none waits for another.

Before, every webhook made a preview job and the Intro & Credits job hung off it: with Previews off for the library
(#423) the preview job blocked Intro & Credits until a worker allowed previews, then claimed nothing and retried five
times; a Plex webhook for a library that's off in the app (#422) made a preview job that retried "file not found".

Real app, real JobManager (jobs.db), real webhook handlers and restart revival, real Intro & Credits and loudness job
creation. Only the preview work (``run_processing``) and the Intro & Credits and loudness runs are stubbed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS, JOB_KIND_PREVIEWS
from media_preview_generator.markers.job_runner import WEBHOOK_FILES
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey

EPISODE = "/data/tv/Show (2020)/Season 01/Show (2020) - S01E01.mkv"
EPISODE_2 = "/data/tv/Show (2020)/Season 01/Show (2020) - S01E02.mkv"
MOVIE_4K = "/data/4k/Casino Royale (2006)/Casino Royale (2006).mkv"
UNKNOWN = "/elsewhere/tv/Show (2020)/Season 01/Show (2020) - S01E01.mkv"
TOKEN = "test-token-12345678"


def _server(sid, stype, *, previews=True, markers=True, loudness=False):
    entry = {
        "id": sid,
        "type": stype,
        "name": sid.upper(),
        "enabled": True,
        "url": f"http://{sid}:8096",
        "auth": {"method": "api_key", "api_key": "k"} if stype != "plex" else {"token": "tok"},
        "libraries": [
            {"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": previews, "kind": "show"},
            {"id": "2", "name": "4K", "remote_paths": ["/data/4k"], "enabled": False, "kind": "movie"},
        ],
        "markers": {"enabled": markers, "library_ids": ["1"] if markers else None},
        "loudness": {"enabled": loudness, "library_ids": None},
    }
    if stype == "plex":
        entry["markers"]["plex"] = {"db_write_confirmed_at": "2026-10-01T00:00:00+00:00"}
    return entry


@pytest.fixture(autouse=True)
def _reset_singletons():
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.webhooks as wh_mod

    def reset():
        reset_settings_manager()
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        wh_mod._recent_dispatches.clear()
        wh_mod._pending_batches.clear()
        wh_mod._pending_timers.clear()

    reset()
    yield
    reset()


@pytest.fixture()
def make_app(tmp_path, monkeypatch):
    def build(servers):
        from media_preview_generator.web.app import create_app

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(
            json.dumps({"setup_complete": True, "webhook_enabled": True, "webhook_delay": 60, "media_servers": servers})
        )
        (config_dir / "auth.json").write_text(json.dumps({"token": TOKEN}))
        monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
        monkeypatch.setenv("CONFIG_DIR", str(config_dir))
        reset_settings_manager()
        flask_app = create_app(config_dir=str(config_dir))
        flask_app.config_dir = str(config_dir)
        return flask_app

    return build


@pytest.fixture()
def previews():
    """Stub the preview work; record what each run was asked to do."""
    runs: list[dict] = []

    def fake_run_processing(config, selected_gpus, **kwargs):
        runs.append({"job_id": kwargs.get("job_id"), "webhook_paths": list(config.webhook_paths or [])})
        return {"outcome": {"generated": len(config.webhook_paths or [])}}

    with patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing):
        yield runs


@pytest.fixture(autouse=True)
def feature_runs():
    """Intro & Credits and loudness jobs are created and started for real; their runs are out of scope here."""
    with (
        patch("media_preview_generator.markers.job_runner.run_intro_credits_job") as markers,
        patch("media_preview_generator.loudness.job.run_loudness_job") as loudness,
    ):
        yield markers, loudness


def _jobs(kind):
    from media_preview_generator.web.jobs import get_job_manager

    return [j for j in get_job_manager().get_all_jobs() if j.kind == kind]


def _sonarr(path, server_id=None):
    from media_preview_generator.web import webhooks as wh

    with patch.object(wh.threading, "Timer", MagicMock()):
        return wh._schedule_webhook_job("sonarr", "Show S01E01", path, server_id=server_id, early_scan=False)


def _starts_in(job) -> float:
    due = datetime.fromisoformat(job.config["retry_not_before"])
    return (due - datetime.now(UTC)).total_seconds()


class TestPreviewsOffIntroCreditsOn:
    """#423: Plex with Previews off on every library and Intro & Credits on for TV."""

    @pytest.fixture()
    def app(self, make_app):
        return make_app([_server("plex-1", "plex", previews=False)])

    def test_a_custom_webhook_makes_only_the_intro_credits_job(self, app, previews):
        client = app.test_client()
        with patch("media_preview_generator.web.webhooks.threading.Timer", MagicMock()):
            response = client.post("/api/webhooks/custom", json={"file_path": EPISODE}, headers={"X-Auth-Token": TOKEN})
        assert response.status_code == 202
        assert _jobs(JOB_KIND_PREVIEWS) == []
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        assert markers.config["file_paths"] == [EPISODE]
        assert markers.config["follows_job_id"] is None  # waits for no other job
        assert 50 < _starts_in(markers) <= 60  # the webhook delay, so Plex can add the file first
        assert previews == []

    @pytest.mark.parametrize("source", ["sonarr", "radarr", "plex", "recently_added"])
    def test_every_sender_route_makes_only_the_intro_credits_job(self, app, source):
        from media_preview_generator.web import webhooks as wh

        with app.app_context(), patch.object(wh.threading, "Timer", MagicMock()):
            assert wh._schedule_webhook_job(source, "Show S01E01", EPISODE, early_scan=False) is True
        assert _jobs(JOB_KIND_PREVIEWS) == []
        assert len(_jobs(JOB_KIND_INTRO_CREDITS)) == 1
        assert wh._pending_batches == {}

    def test_a_vendor_webhook_makes_only_the_intro_credits_job(self, app):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            job_id = wh.create_vendor_webhook_job(
                source="plex", canonical_path=EPISODE, item_id_by_server={"plex-1": "9"}, server_id="plex-1"
            )
        assert _jobs(JOB_KIND_PREVIEWS) == []
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        assert job_id == markers.id
        assert markers.config["webhook_item_id_hints"] == {EPISODE: {"plex-1": "9"}}

    def test_episodes_of_a_season_share_one_intro_credits_job(self, app):
        with app.app_context():
            assert _sonarr(EPISODE)
            assert _sonarr(EPISODE_2)
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        assert markers.config["file_paths"] == [EPISODE, EPISODE_2]
        assert markers.config[WEBHOOK_FILES] is True

    def test_a_repeat_of_the_same_webhook_is_a_duplicate(self, app):
        with app.app_context():
            assert _sonarr(EPISODE)
            assert _sonarr(EPISODE) is False
        assert len(_jobs(JOB_KIND_INTRO_CREDITS)) == 1

    def test_the_intro_credits_job_survives_a_restart_during_the_delay(self, app):
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web import app as app_mod
        from media_preview_generator.web.jobs import JobStatus

        with app.app_context():
            assert _sonarr(EPISODE)
            with jobs_mod._job_lock:
                jobs_mod._job_manager = None
            jobs_mod.get_job_manager(app.config_dir)
            app_mod._requeue_interrupted_on_startup(app.config_dir)
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        assert markers.status is JobStatus.PENDING
        assert _starts_in(markers) > 0  # still waits out what's left of the delay, not the whole delay again


class TestLibraryOffInTheApp:
    """#422: a Plex webhook for a library that's off in the app (Previews, Intro & Credits and loudness all off)."""

    @pytest.fixture()
    def app(self, make_app):
        return make_app([_server("plex-1", "plex", markers=False)])

    def test_a_plex_webhook_makes_no_job_at_all(self, app):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            assert wh._schedule_webhook_job("plex", "Casino Royale", MOVIE_4K, server_id="plex-1") is True
        assert _jobs(JOB_KIND_PREVIEWS) == []
        assert _jobs(JOB_KIND_INTRO_CREDITS) == []
        assert _jobs(JOB_KIND_LOUDNESS) == []
        assert wh._pending_batches == {}

    def test_a_vendor_plex_webhook_with_an_item_id_makes_no_job_at_all(self, app):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            job_id = wh.create_vendor_webhook_job(
                source="plex", canonical_path=MOVIE_4K, item_id_by_server={"plex-1": "77"}, server_id="plex-1"
            )
        assert job_id == ""
        assert _jobs(JOB_KIND_PREVIEWS) == []
        assert _jobs(JOB_KIND_INTRO_CREDITS) == []
        assert _jobs(JOB_KIND_LOUDNESS) == []
        assert wh._pending_batches == {}

    def test_the_incoming_route_says_the_file_was_ignored(self, app):
        from media_preview_generator.web.webhook_router import _create_webhook_job

        with app.app_context():
            result = _create_webhook_job(MOVIE_4K, {"plex-1": "77"}, kind="plex")
        assert result["status"] == "ignored"
        assert _jobs(JOB_KIND_PREVIEWS) == []


class TestPreviewsOn:
    @pytest.fixture()
    def app(self, make_app, tmp_path):
        plex_cfg = tmp_path / "plex_cfg"
        (plex_cfg / "Media" / "localhost").mkdir(parents=True)
        plex = _server("plex-1", "plex", loudness=True)
        plex["output"] = {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)}
        return make_app([plex, _server("jf-1", "jellyfin")])

    def test_previews_intro_credits_and_loudness_are_queued_together_and_none_waits(self, app, previews):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            assert _sonarr(EPISODE)
            (preview,) = _jobs(JOB_KIND_PREVIEWS)
            (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
            (loudness,) = _jobs(JOB_KIND_LOUDNESS)
            assert markers.config["follows_job_id"] is None
            assert loudness.config["follows_job_id"] is None
            assert 50 < _starts_in(loudness) <= 60
            (key,) = wh._pending_batches
            wh._execute_webhook_job(key)
        assert previews == [{"job_id": preview.id, "webhook_paths": [EPISODE]}]
        assert len(_jobs(JOB_KIND_INTRO_CREDITS)) == 1  # the preview run queues no second one
        assert len(_jobs(JOB_KIND_LOUDNESS)) == 1

    @pytest.mark.parametrize(
        ("hints", "server_id_filter", "expected_pin"),
        [
            ({"jf-1": "abc"}, None, "jf-1"),  # a non-Plex originator publishes to itself
            ({"plex-1": "9"}, None, None),  # a Plex originator fans out
            ({"jf-1": "abc"}, "plex-1", "plex-1"),  # the explicit pin wins
        ],
        ids=["jellyfin", "plex", "explicit-pin"],
    )
    def test_a_vendor_webhooks_intro_credits_job_publishes_where_its_previews_do(
        self, app, hints, server_id_filter, expected_pin
    ):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            preview_id = wh.create_vendor_webhook_job(
                source="jellyfin",
                canonical_path=EPISODE,
                item_id_by_server=hints,
                server_id=next(iter(hints)),
                server_id_filter=server_id_filter,
            )
        (preview,) = _jobs(JOB_KIND_PREVIEWS)
        assert preview.id == preview_id
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        assert markers.config.get("server_id") == expected_pin

    def test_a_file_under_no_known_library_still_gets_a_preview_job(self, app):
        # Its retries cover a library the server is still adding; its log points at a wrong path mapping.
        with app.app_context():
            assert _sonarr(UNKNOWN)
        assert len(_jobs(JOB_KIND_PREVIEWS)) == 1
        assert _jobs(JOB_KIND_INTRO_CREDITS) == []


class TestABatchStaysOneJobOfEachKind:
    """Files a sender reports one at a time join the waiting job of each kind, as they'd share a webhook batch."""

    MOVIE_A = "/data/tv/Film A (2020)/Film A (2020).mkv"
    MOVIE_B = "/data/tv/Film B (2021)/Film B (2021).mkv"

    @pytest.fixture()
    def app(self, make_app):
        return make_app([_server("plex-1", "plex", previews=False, loudness=True)])

    def test_a_season_pack_makes_one_intro_credits_and_one_loudness_job(self, app):
        with app.app_context():
            assert _sonarr(EPISODE)
            assert _sonarr(EPISODE_2)
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        (loudness,) = _jobs(JOB_KIND_LOUDNESS)
        assert markers.config["file_paths"] == [EPISODE, EPISODE_2]
        assert loudness.config["file_paths"] == [EPISODE, EPISODE_2]
        assert loudness.library_name == "Plex loudness · 2 files"

    def test_movies_from_one_sender_share_their_jobs(self, app):
        from media_preview_generator.web import webhooks as wh

        with app.app_context(), patch.object(wh.threading, "Timer", MagicMock()):
            assert wh._schedule_webhook_job("radarr", "Film A", self.MOVIE_A, early_scan=False)
            assert wh._schedule_webhook_job("radarr", "Film B", self.MOVIE_B, early_scan=False)
        (markers,) = _jobs(JOB_KIND_INTRO_CREDITS)
        (loudness,) = _jobs(JOB_KIND_LOUDNESS)
        assert markers.config["file_paths"] == [self.MOVIE_A, self.MOVIE_B]
        assert loudness.config["file_paths"] == [self.MOVIE_A, self.MOVIE_B]

    def test_another_sender_or_pin_gets_its_own_jobs(self, app):
        from media_preview_generator.web import webhooks as wh

        with app.app_context(), patch.object(wh.threading, "Timer", MagicMock()):
            assert wh._schedule_webhook_job("radarr", "Film A", self.MOVIE_A, early_scan=False)
            assert wh._schedule_webhook_job("custom", "Film B", self.MOVIE_B, early_scan=False)
            film_c = self.MOVIE_B.replace("Film B (2021)", "Film C (2022)")
            assert wh._schedule_webhook_job("radarr", "Film C", film_c, server_id="plex-1", early_scan=False)
        assert len(_jobs(JOB_KIND_LOUDNESS)) == 3
        assert len(_jobs(JOB_KIND_INTRO_CREDITS)) == 3

    def test_a_loudness_job_that_has_sealed_its_files_takes_no_more(self, app):
        from media_preview_generator.loudness.job import _seal_files
        from media_preview_generator.web.jobs import get_job_manager

        with app.app_context():
            assert _sonarr(EPISODE)
            (loudness,) = _jobs(JOB_KIND_LOUDNESS)
            sealed = _seal_files(get_job_manager(), loudness.id, dict(loudness.config))
            assert sealed["file_paths"] == [EPISODE]
            assert _sonarr(EPISODE_2)
        assert sorted(len(j.config["file_paths"]) for j in _jobs(JOB_KIND_LOUDNESS)) == [1, 1]
