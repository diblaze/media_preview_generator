"""Bell card for an NVIDIA GPU passed into the container without its driver (issue #419)."""

from __future__ import annotations

import pytest

from media_preview_generator.gpu import detect
from media_preview_generator.web import notifications as notif_mod
from media_preview_generator.web.notifications import (
    NVIDIA_DRIVER_MISSING_ID,
    _build_nvidia_driver_missing_notification,
    build_active_notifications,
    reset_session,
)

VULKAN_CARD = {"id": "vulkan_software_fallback", "severity": "warning", "title": "t", "body_html": "b"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    reset_session()
    for name in (
        "_build_timezone_misconfigured_notification",
        "_build_schema_migration_notification",
        "_build_deprecated_image_notification",
        "_build_unhealthy_media_mounts_notification",
        "_build_library_health_not_showing_notification",
        "_build_library_health_check_failed_notification",
    ):
        monkeypatch.setattr(notif_mod, name, lambda: None)
    for name in ("_build_credits_on_cpu_notifications", "_build_gpu_keeps_failing_notifications"):
        monkeypatch.setattr(notif_mod, name, lambda: [])
    monkeypatch.setattr(notif_mod, "_build_node_notifications", lambda: [])
    monkeypatch.setattr(notif_mod, "_build_vulkan_software_fallback_notification", lambda: dict(VULKAN_CARD))
    yield
    reset_session()


def _container(monkeypatch, tmp_path, *, control_node: bool, smi: str | None) -> None:
    nvidiactl = tmp_path / "nvidiactl"
    if control_node:
        nvidiactl.touch()
    monkeypatch.setattr(detect, "_NVIDIA_CONTROL_NODE", str(nvidiactl))
    monkeypatch.setattr(detect.shutil, "which", lambda name: smi if name == "nvidia-smi" else None)


@pytest.mark.parametrize(
    ("control_node", "smi"),
    [
        pytest.param(True, "/usr/bin/nvidia-smi", id="runtime-in-use"),
        pytest.param(False, None, id="no-nvidia-gpu"),
    ],
)
def test_no_card_unless_an_nvidia_device_came_without_its_driver(monkeypatch, tmp_path, control_node, smi):
    _container(monkeypatch, tmp_path, control_node=control_node, smi=smi)

    assert _build_nvidia_driver_missing_notification() is None
    assert [n["id"] for n in build_active_notifications()] == ["vulkan_software_fallback"]


def test_the_card_says_what_is_wrong_and_how_to_fix_it(monkeypatch, tmp_path):
    """The reporter passed /dev/nvidia* under devices: without runtime: nvidia and saw only "no GPU"."""
    _container(monkeypatch, tmp_path, control_node=True, smi=None)

    card = _build_nvidia_driver_missing_notification()

    assert card is not None
    assert card["id"] == NVIDIA_DRIVER_MISSING_ID
    assert card["title"] == "NVIDIA GPU passed in without its driver"
    assert "no nvidia-smi" in card["body_html"]
    assert "Start the container with the NVIDIA runtime (Docker Compose: runtime: nvidia;" in card["body_html"]
    assert "NVIDIA_DRIVER_CAPABILITIES=all" in card["body_html"]
    assert [n["id"] for n in build_active_notifications()] == [NVIDIA_DRIVER_MISSING_ID, "vulkan_software_fallback"]
