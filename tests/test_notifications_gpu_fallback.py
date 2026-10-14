"""Bell card for a GPU whose last files all ran on the CPU (jobs.gpu_fallback)."""

from __future__ import annotations

import pytest

from media_preview_generator.jobs.gpu_fallback import get_gpu_fallback_tracker, notification_id
from media_preview_generator.web import notifications as notif_mod
from media_preview_generator.web.notifications import (
    _build_gpu_keeps_failing_notifications,
    build_active_notifications,
    dismiss_session,
    is_session_only_dismissal,
    reset_session,
)

KEY = "/dev/dri/renderD128"


@pytest.fixture(autouse=True)
def _fresh():
    get_gpu_fallback_tracker().reset()
    reset_session()
    yield
    get_gpu_fallback_tracker().reset()
    reset_session()


def _flag(key: str = KEY, name: str = "Intel UHD Graphics 770", reason: str = "hevc not supported", n: int = 5):
    for _ in range(n):
        get_gpu_fallback_tracker().record(key, name, fell_back=True, reason=reason)


class TestGpuKeepsFailingNotification:
    def test_absent_while_no_gpu_is_flagged(self):
        _flag(n=4)
        assert _build_gpu_keeps_failing_notifications() == []

    def test_one_card_per_flagged_gpu_with_the_approved_wording(self):
        _flag()
        (card,) = _build_gpu_keeps_failing_notifications()
        assert card == {
            "id": "gpu_keeps_failing__dev_dri_renderD128",
            "severity": "warning",
            "title": "GPU keeps failing: files are running on the CPU",
            "body_html": (
                "<p class='mb-0'>Intel UHD Graphics 770 couldn't process the last 5 files, so they ran on the CPU "
                "(slower). Last reason: hevc not supported. Check the GPU driver in Settings → GPU.</p>"
            ),
            "dismissable": True,
            "permanent_dismissable": False,
            "source": "gpu_fallback_streak",
            "device": KEY,
        }

    def test_two_flagged_gpus_give_two_cards_in_key_order(self):
        _flag("/dev/dri/renderD129", "GPU B")
        _flag("/dev/dri/renderD128", "GPU A")
        cards = _build_gpu_keeps_failing_notifications()
        assert [c["id"] for c in cards] == [
            notification_id("/dev/dri/renderD128"),
            notification_id("/dev/dri/renderD129"),
        ]
        assert cards[0]["body_html"].startswith("<p class='mb-0'>GPU A couldn't")

    def test_the_reason_and_name_are_escaped_and_secrets_masked(self):
        _flag(name="GPU <b>A</b>", reason="GET http://plex:32400/?X-Plex-Token=secret123 <failed>")
        (card,) = _build_gpu_keeps_failing_notifications()
        assert "secret123" not in card["body_html"]
        assert "<b>" not in card["body_html"]
        assert "&lt;failed&gt;" in card["body_html"]

    def test_clears_once_the_gpu_finishes_a_file_without_falling_back(self):
        _flag()
        get_gpu_fallback_tracker().record(KEY, "GPU A", fell_back=False, reason=None)
        assert _build_gpu_keeps_failing_notifications() == []

    def test_it_is_in_the_active_list_and_hidden_only_for_the_session(self, monkeypatch):
        for name in (
            "_build_vulkan_software_fallback_notification",
            "_build_timezone_misconfigured_notification",
            "_build_schema_migration_notification",
            "_build_deprecated_image_notification",
            "_build_unhealthy_media_mounts_notification",
        ):
            monkeypatch.setattr(notif_mod, name, lambda: None)
        _flag()
        card_id = notification_id(KEY)
        assert [n["id"] for n in build_active_notifications()] == [card_id]
        # A stored permanent dismissal is ignored: the problem comes and goes, like an unmounted media path.
        assert [n["id"] for n in build_active_notifications(dismissed_permanent=[card_id])] == [card_id]
        dismiss_session(card_id)
        assert build_active_notifications() == []

    def test_a_recovered_gpu_that_fails_again_shows_its_card_again(self, monkeypatch):
        for name in (
            "_build_vulkan_software_fallback_notification",
            "_build_timezone_misconfigured_notification",
            "_build_schema_migration_notification",
            "_build_deprecated_image_notification",
            "_build_unhealthy_media_mounts_notification",
        ):
            monkeypatch.setattr(notif_mod, name, lambda: None)
        _flag()
        dismiss_session(notification_id(KEY))
        get_gpu_fallback_tracker().record(KEY, "GPU A", fell_back=False, reason=None)
        _flag()
        assert [n["id"] for n in build_active_notifications()] == [notification_id(KEY)]

    def test_a_node_gpu_card_names_the_node_and_links_to_it_not_this_apps_gpu_settings(self, monkeypatch):
        from media_preview_generator.nodes import registry as reg

        class _Registry:
            def view(self, node_id):
                assert node_id == "n1"
                return reg.NodeView("n1", "Storage <box>", "http://n", True, reg.STATE_READY, "", "1", ("worker",),
                                    (), 4, False, None)  # fmt: skip

        monkeypatch.setattr(reg, "_registry", _Registry())
        key = f"node:n1:{KEY}"
        _flag(key, name="Storage <box> Intel Arc A380")
        (card,) = _build_gpu_keeps_failing_notifications()
        assert card["id"] == notification_id(key) and card["device"] == key
        assert card["body_html"] == (
            "<p class='mb-0'>Storage &lt;box&gt; Intel Arc A380 couldn't process the last 5 files, so they ran on the "
            "CPU (slower). Last reason: hevc not supported. Check the GPU driver on Storage &lt;box&gt;, then Rescan "
            "GPUs on its Nodes page.</p>"
        )
        assert "Settings → GPU" not in card["body_html"]
        assert card["action"] == {"href": "/nodes#node-n1", "label": "Open node"}

    def test_a_node_gpu_card_still_shows_when_the_node_is_gone(self, monkeypatch):
        from media_preview_generator.nodes import registry as reg

        class _Registry:
            def view(self, node_id):
                return None

        monkeypatch.setattr(reg, "_registry", _Registry())
        _flag(f"node:n1:{KEY}", name="NAS Intel Arc A380")
        (card,) = _build_gpu_keeps_failing_notifications()
        assert "Check the GPU driver on the node, then Rescan GPUs on its Nodes page." in card["body_html"]
        assert card["action"] == {"href": "/nodes#node-n1", "label": "Open node"}

    def test_a_local_gpu_card_has_no_node_link(self):
        _flag()
        (card,) = _build_gpu_keeps_failing_notifications()
        assert "action" not in card

    def test_session_only_dismissal_covers_gpu_cards_and_the_media_mount_card(self):
        assert is_session_only_dismissal(notification_id(KEY)) is True
        assert is_session_only_dismissal(notif_mod.MEDIA_MOUNT_UNHEALTHY_ID) is True
        assert is_session_only_dismissal(notif_mod.VULKAN_SOFTWARE_FALLBACK_ID) is False


CREDITS_DEVICE = {
    "key": KEY,
    "reason": "the GPU got 2 of 31 test pictures wrong (it found no text in both)",
    "driver_suspect": True,
    "selftest": {},
}


@pytest.fixture
def credits_devices(monkeypatch):
    from media_preview_generator.markers.credits import textdet_helper

    devices: list[dict] = []
    monkeypatch.setattr(textdet_helper, "credits_on_cpu_devices", lambda: list(devices))
    return devices


class TestCreditsOnCpuNotification:
    def test_absent_while_no_device_failed_its_self_test(self, credits_devices):
        assert notif_mod._build_credits_on_cpu_notifications() == []

    def test_a_driver_suspect_card_has_the_hint_and_the_gpus_friendly_name(self, credits_devices):
        get_gpu_fallback_tracker().record(KEY, "Intel UHD Graphics 770", fell_back=False, reason=None)
        credits_devices.append(CREDITS_DEVICE)
        (card,) = notif_mod._build_credits_on_cpu_notifications()
        assert card == {
            "id": "credits_on_cpu__dev_dri_renderD128",
            "severity": "warning",
            "title": "Credits are being read on the CPU on Intel UHD Graphics 770",
            "body_html": (
                "<p class='mb-1'>In its accuracy check, the GPU got 2 of 31 test pictures wrong (it found no text in "
                "both). A GPU that reads text wrong could put credits markers in the wrong place, so credits on "
                "this GPU are read on the CPU until the app restarts. That's slower, but the markers are just as "
                "accurate. Previews still use the GPU.</p>"
                "<p class='mb-0 small text-muted'>This usually points to a GPU driver problem. Check the host's "
                'kernel log for GPU hangs (<code>dmesg | grep -iE "i915|amdgpu|GPU HANG"</code>), then restart the '
                "app.</p>"
            ),
            "dismissable": True,
            "permanent_dismissable": False,
            "source": "credits_text_selftest",
            "device": KEY,
        }

    def test_without_a_driver_suspicion_there_is_no_hint_and_the_key_names_the_gpu(self, credits_devices):
        credits_devices.append({**CREDITS_DEVICE, "driver_suspect": False, "reason": "the GPU got 3 of 31 <wrong>"})
        (card,) = notif_mod._build_credits_on_cpu_notifications()
        assert card["title"] == f"Credits are being read on the CPU on {KEY}"
        assert card["body_html"].startswith("<p class='mb-0'>In its accuracy check, the GPU got 3 of 31 &lt;wrong&gt;.")
        assert "driver problem" not in card["body_html"]

    def test_the_card_is_in_the_active_list_and_hidden_only_for_the_session(self, credits_devices):
        credits_devices.append(CREDITS_DEVICE)
        card_id = notif_mod._build_credits_on_cpu_notifications()[0]["id"]
        assert is_session_only_dismissal(card_id)
        assert card_id in [n["id"] for n in build_active_notifications()]
        # A stored permanent dismissal is ignored, as for the GPU streak card.
        assert card_id in [n["id"] for n in build_active_notifications(dismissed_permanent=[card_id])]
        dismiss_session(card_id)
        assert card_id not in [n["id"] for n in build_active_notifications()]

    def test_the_streak_card_for_the_same_gpu_is_suppressed_but_another_gpu_still_shows(self, credits_devices):
        credits_devices.append(CREDITS_DEVICE)
        credits = "Credits read on the CPU: this GPU failed its accuracy check. Previews still use the GPU."
        _flag(KEY, "GPU A", reason=credits)
        _flag("/dev/dri/renderD129", "GPU B", reason=credits)
        assert [c["device"] for c in _build_gpu_keeps_failing_notifications()] == ["/dev/dri/renderD129"]

    def test_a_preview_streak_on_the_same_gpu_still_shows_its_card(self, credits_devices):
        # The credits card says previews still use the GPU; a streak last fed by a preview decode failing says
        # otherwise, so its own card must not be hidden.
        credits_devices.append(CREDITS_DEVICE)
        _flag(KEY, "GPU A", reason="Credits read on the CPU: this GPU failed its accuracy check.", n=4)
        _flag(KEY, "GPU A", reason="hevc not supported", n=1)
        assert [c["device"] for c in _build_gpu_keeps_failing_notifications()] == [KEY]

    def test_a_failing_read_gives_no_cards(self, monkeypatch):
        from media_preview_generator.markers.credits import textdet_helper

        def boom():
            raise RuntimeError("no pool")

        monkeypatch.setattr(textdet_helper, "credits_on_cpu_devices", boom)
        assert notif_mod._build_credits_on_cpu_notifications() == []
