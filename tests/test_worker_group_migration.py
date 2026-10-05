"""Upgrade preserves capacity, overnight meaning and intended global holds."""

from media_preview_generator.quiet_hours import quiet_hours_weekly_mask
from media_preview_generator.upgrade import _migrate_to_v21
from media_preview_generator.web.settings_manager import SettingsManager


def test_zero_cpu_and_disabled_gpu_stay_disabled(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "name": "GPU", "workers": 2, "enabled": False, "ffmpeg_threads": 3}],
        }
    )
    _migrate_to_v21(settings)
    assert settings.cpu_threads == settings.gpu_threads == 0
    assert len(settings.worker_groups) == 1
    assert settings.worker_groups[0]["count"] == 2
    assert settings.worker_groups[0]["enabled"] is False
    assert settings.gpu_config[0]["ffmpeg_threads"] == 3
    assert "loudness" not in settings.worker_groups[0]["job_types"]


def test_explicit_empty_groups_survive_a_repeated_migration(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"cpu_threads": 12, "worker_groups": [], "worker_groups_revision": 4})
    _migrate_to_v21(settings)
    _migrate_to_v21(settings)
    assert settings.worker_groups == []
    assert settings.worker_groups_revision == 4


def test_ambiguous_existing_pause_remains_manual(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"processing_paused": True, "cpu_threads": 2})
    _migrate_to_v21(settings)
    assert settings.processing_pause_reasons == ["manual"]
    assert settings.get("processing_pause_preserved") is True
    settings.set_processing_pause_reason("quiet_hours", False)
    assert settings.processing_paused


def test_proven_no_worker_pause_becomes_resource_wait(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"processing_paused": True, "processing_auto_paused": True, "cpu_threads": 0, "gpu_config": []})
    _migrate_to_v21(settings)
    assert not settings.processing_paused
    assert settings.worker_groups == []
    assert not settings.processing_auto_paused


def test_legacy_overnight_week_intervals_are_preserved(tmp_path):
    settings = SettingsManager(str(tmp_path))
    legacy = {"enabled": True, "windows": [{"days": ["mon"], "start": "23:00", "end": "07:00"}]}
    before = quiet_hours_weekly_mask(legacy)
    settings.set("quiet_hours", legacy)
    _migrate_to_v21(settings)
    migrated = settings.get("quiet_hours")
    assert migrated["day_basis"] == "start"
    assert quiet_hours_weekly_mask(migrated) == before
    assert len(migrated["windows"]) == 2


def test_migration_does_not_enable_new_cpu_capacity(tmp_path):
    settings = SettingsManager(str(tmp_path))
    settings.update({"cpu_threads": 0, "gpu_config": [{"device": "cuda:0", "workers": 4, "enabled": True}]})
    _migrate_to_v21(settings)
    assert settings.gpu_threads == 4
    assert settings.cpu_threads == 0
    assert all(g["resource"] == "gpu" and g["availability"]["mode"] == "always" for g in settings.worker_groups)


def test_full_upgrade_chain_preserves_pause_without_auto_provenance(tmp_path):
    from media_preview_generator.upgrade import _migrate_schema

    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "_schema_version": 18,
            "processing_paused": True,
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "enabled": False, "workers": 0}],
        }
    )
    _migrate_schema(settings)
    assert settings.processing_pause_reasons == ["manual"]
    assert settings.processing_paused
    assert settings.get("processing_pause_preserved") is True
    assert settings.cpu_threads == settings.gpu_threads == 0


def test_full_upgrade_chain_clears_only_preexisting_valid_auto_pause(tmp_path):
    from media_preview_generator.upgrade import _migrate_schema

    settings = SettingsManager(str(tmp_path))
    settings.update(
        {
            "_schema_version": 18,
            "processing_paused": True,
            "processing_auto_paused": True,
            "cpu_threads": 0,
            "gpu_config": [{"device": "cuda:0", "enabled": False, "workers": 0}],
        }
    )
    _migrate_schema(settings)
    assert settings.processing_pause_reasons == []
    assert not settings.processing_paused
    assert settings.cpu_threads == settings.gpu_threads == 0
