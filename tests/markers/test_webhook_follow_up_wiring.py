"""A webhook queues its Intro & Credits and loudness jobs when it arrives, with its pin, item ids and delay.

The jobs are queued beside the preview job, not by it: none waits for another, and Previews being off for the file's
library never holds them up. The end-to-end journeys (each sender route, restart during the delay, vendor pins,
Previews off) are in tests/journeys/test_journey_webhook_jobs_per_feature.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import triggers
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web import webhooks as wh

SONARR_PATH = "/data/TV Shows/Show (2023)/Season 02/Show - S02E04.mkv"
SUBMIT_JOBS = "media_preview_generator.markers.triggers.submit_webhook_jobs"
# Previews on for a library holding every path these tests send, so each webhook also opens a preview batch.
_DATA = [{"id": "1", "name": "Data", "remote_paths": ["/data"], "enabled": True}]
SERVERS = [
    {
        "id": "plex-1",
        "type": "plex",
        "name": "Plex",
        "enabled": True,
        "url": "http://p",
        "auth": {"token": "t"},
        "libraries": _DATA,
    },
    {
        "id": "jf-1",
        "type": "jellyfin",
        "name": "Jellyfin",
        "enabled": True,
        "url": "http://j",
        "auth": {"method": "api_key", "api_key": "k"},
        "libraries": _DATA,
    },
]


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import media_preview_generator.web.settings_manager as sm_mod

    sm_mod._settings_manager = None
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sm_mod.get_settings_manager().set("media_servers", SERVERS)
    yield
    sm_mod._settings_manager = None


@pytest.fixture(autouse=True)
def _isolate_jobs(tmp_path):
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    wh._pending_batches.clear()
    yield
    wh._pending_batches.clear()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


def _schedule(source, path, **kwargs):
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch("media_preview_generator.web.webhooks.threading.Timer", MagicMock()),
    ):
        return wh._schedule_webhook_job(source, "Show S02E04", path, early_scan=False, **kwargs)


@pytest.mark.parametrize(
    ("source", "server_id", "pin"),
    [
        ("sonarr", None, None),
        ("radarr", None, None),
        ("sportarr", None, None),
        ("plex", "plex-1", "plex-1"),  # Plex library.new via /api/webhooks/plex, pinned to the resolved server
        ("sonarr", "jf-1", "jf-1"),  # ?server_id= on an Arr webhook
        ("sonarr", "gone-1", None),  # an unknown server id is treated as unpinned, for previews and markers alike
        ("custom", None, None),  # Tdarr / scripts
        ("recently_added", None, None),
    ],
)
def test_a_sender_webhook_queues_its_jobs_on_arrival_with_the_preview_pin(source, server_id, pin):
    with patch(SUBMIT_JOBS, return_value=["ic-1"]) as submit:
        assert _schedule(source, SONARR_PATH, **({"server_id": server_id} if server_id else {})) is True
    submit.assert_called_once_with(paths=[SONARR_PATH], source=source, pin=pin, item_id_hints=None, delay_s=60)
    (batch,) = wh._pending_batches.values()
    assert batch["server_id_pin"] == pin  # the preview job publishes where its Intro & Credits job does


def test_each_file_a_batch_takes_queues_its_own_jobs():
    other = SONARR_PATH.replace("S02E04", "S02E05")
    with patch(SUBMIT_JOBS, return_value=[]) as submit:
        _schedule("sonarr", SONARR_PATH, server_id="jf-1")
        _schedule("sonarr", other, server_id="jf-1")
    assert [c.kwargs["paths"] for c in submit.call_args_list] == [[SONARR_PATH], [other]]
    assert {c.kwargs["pin"] for c in submit.call_args_list} == {"jf-1"}


def test_a_duplicate_sender_webhook_queues_nothing():
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=12),
        patch(SUBMIT_JOBS) as submit,
    ):
        assert wh._schedule_webhook_job("sonarr", "Show", SONARR_PATH, early_scan=False) is False
    submit.assert_not_called()


@pytest.mark.parametrize(
    ("hints", "server_id_filter", "expected_hints", "pin"),
    [
        ({"jf-1": "abc"}, None, {"/data/Movies/Foo.mkv": {"jf-1": "abc"}}, None),
        ({"plex-1": "9"}, None, {"/data/Movies/Foo.mkv": {"plex-1": "9"}}, None),
        (None, None, None, None),
        ({"jf-1": ""}, None, None, None),  # empty ids are dropped before they reach the jobs
        ({"jf-1": "abc"}, "jf-1", {"/data/Movies/Foo.mkv": {"jf-1": "abc"}}, "jf-1"),  # /api/webhooks/server/<id>
    ],
)
def test_a_vendor_webhook_hands_its_item_ids_and_pin_to_its_jobs(hints, server_id_filter, expected_hints, pin):
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch(SUBMIT_JOBS, return_value=[]) as submit,
    ):
        job_id = wh.create_vendor_webhook_job(
            source="jellyfin",
            title="Foo",
            canonical_path="/data/Movies/Foo.mkv",
            item_id_by_server=hints,
            server_id_filter=server_id_filter,
        )
    assert job_id is not None
    submit.assert_called_once_with(
        paths=["/data/Movies/Foo.mkv"], source="jellyfin", pin=pin, item_id_hints=expected_hints, delay_s=60
    )


def test_the_vendor_jobs_use_the_sanitised_source():
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch(SUBMIT_JOBS, return_value=[]) as submit,
    ):
        wh.create_vendor_webhook_job(source="  JellyFin ", canonical_path="/data/Movies/Foo.mkv")
    assert submit.call_args.kwargs["source"] == "jellyfin"


def test_a_duplicate_vendor_webhook_creates_no_job():
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=12),
        patch(SUBMIT_JOBS) as submit,
    ):
        assert wh.create_vendor_webhook_job(source="plex", canonical_path="/data/Movies/Foo.mkv") is None
    submit.assert_not_called()


class TestSubmitWebhookJobs:
    def test_items_carry_the_sender_of_each_file_and_every_job_gets_the_delay(self):
        hints = {"/a.mkv": {"jf-1": "abc"}}
        with (
            patch("media_preview_generator.markers.triggers.submit_follow_ups", return_value=["ic-1"]) as follow_ups,
            patch(
                "media_preview_generator.markers.triggers._submit_loudness_follow_up", return_value=["ld-1"]
            ) as loudness,
        ):
            queued = triggers.submit_webhook_jobs(
                paths=["/a.mkv", "/b.mkv"], source="sonarr", pin="plex-1", item_id_hints=hints, delay_s=45
            )
        assert queued == ["ic-1", "ld-1"]
        follow_ups.assert_called_once_with(
            preview_job_id=None,
            items=[
                ProcessableItem(canonical_path="/a.mkv", server_id="jf-1", item_id_by_server={"jf-1": "abc"}),
                ProcessableItem(canonical_path="/b.mkv", server_id="", item_id_by_server={}),
            ],
            source="sonarr",
            pin="plex-1",
            delay_s=45,
        )
        loudness.assert_called_once_with(None, ["/a.mkv", "/b.mkv"], "sonarr", "plex-1", delay_s=45)

    def test_an_intro_credits_failure_still_queues_loudness(self):
        with (
            patch("media_preview_generator.markers.triggers.submit_follow_ups", side_effect=RuntimeError("boom")),
            patch("media_preview_generator.markers.triggers._submit_loudness_follow_up", return_value=["ld-1"]),
        ):
            assert triggers.submit_webhook_jobs(paths=["/a.mkv"], source="sonarr", pin=None) == ["ld-1"]


def _library_server(sid, *, previews, remote="/data/tv"):
    return {
        "id": sid,
        "type": "jellyfin",
        "name": sid,
        "enabled": True,
        "url": "http://x",
        "auth": {"method": "api_key", "api_key": "k"},
        "libraries": [{"id": "1", "name": "TV", "remote_paths": [remote], "enabled": previews}],
    }


class TestPreviewsWanted:
    PATH = "/data/tv/Show/Season 01/Show - S01E01.mkv"

    @pytest.mark.parametrize(
        ("servers", "pin", "wanted"),
        [
            ([_library_server("a", previews=True)], None, True),
            ([_library_server("a", previews=False)], None, False),
            ([_library_server("a", previews=False), _library_server("b", previews=True)], None, True),
            # Pinned: only the pinned server's libraries count.
            ([_library_server("a", previews=False), _library_server("b", previews=True)], "a", False),
            ([_library_server("a", previews=False), _library_server("b", previews=True)], "b", True),
            # No library holds the file: no preview job.
            ([_library_server("a", previews=True, remote="/other")], None, False),
            ([], None, False),
        ],
        ids=["on", "off", "off-and-on", "pinned-off", "pinned-on", "unknown-library", "no-servers"],
    )
    def test_previews_wanted(self, servers, pin, wanted):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", servers)
        assert wh._previews_wanted(self.PATH, pin) is wanted


class TestWhyNoJob:
    PATH = "/data/tv/Show/Season 01/Show - S01E01.mkv"

    def test_a_library_with_every_feature_off(self):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", [_library_server("a", previews=False)])
        assert wh._why_no_job(self.PATH, None) == "Previews, Intro & Credits and loudness are all off for its library"

    def test_no_library_holds_the_file_and_the_path_mapping_hint_follows(self):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("media_servers", [_library_server("a", previews=True, remote="/media/tv")])
        reason = wh._why_no_job("/data/tv/Show/Season 01/Show - S01E01.mkv", None)
        assert reason.startswith("no library on your servers holds /data/tv/Show/Season 01/Show - S01E01.mkv. ")
        assert "/media/tv" in reason  # the hint names the library folder the sender's path should map to
