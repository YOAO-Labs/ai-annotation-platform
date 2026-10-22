"""Transactional and cross-consumer invariants for runtime settings."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.db.models.system_setting import SystemSetting
from app.services.email import _load_smtp_config
from app.services.system_settings_service import SystemSettingsService


@pytest.mark.parametrize("nested_commit", [False, True])
async def test_rolled_back_override_never_escapes_through_process_cache(
    db_session, nested_commit
):
    key = "task_create_sync_threshold"
    baseline = await SystemSettingsService.get(db_session, key)
    changed = 0 if baseline != 0 else 1
    SystemSettingsService.invalidate()
    await SystemSettingsService.set_many(db_session, {key: changed}, None)
    if nested_commit:
        async with db_session.begin_nested():
            pass
    assert await SystemSettingsService.get(db_session, key) == changed
    await db_session.rollback()
    assert await SystemSettingsService.get(db_session, key) == baseline


async def test_explicit_empty_password_remains_an_override(db_session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "smtp_password", "")
    await SystemSettingsService.set_many(db_session, {"smtp_password": ""}, None)
    row = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == "smtp_password")
    )
    assert row is not None and row.value_json == ""
    monkeypatch.setattr(settings, "smtp_password", "deployment-test-secret")
    assert await SystemSettingsService.get(db_session, "smtp_password") == ""
    await SystemSettingsService.reset_many(db_session, ["smtp_password"], None)
    assert (
        await SystemSettingsService.get(db_session, "smtp_password")
        == "deployment-test-secret"
    )


async def test_smtp_uses_one_fresh_saved_cohort_despite_partial_cache(db_session):
    await SystemSettingsService.set_many(
        db_session,
        {
            "smtp_host": "old.test",
            "smtp_port": 1025,
            "smtp_from": "old@example.test",
            "smtp_password": "old-secret",
        },
        None,
    )
    await db_session.commit()
    assert await SystemSettingsService.get(db_session, "smtp_host") == "old.test"
    for key, value in {
        "smtp_host": "new.test",
        "smtp_from": "new@example.test",
        "smtp_password": "new-secret",
    }.items():
        await db_session.execute(
            update(SystemSetting)
            .where(SystemSetting.key == key)
            .values(value_json=value)
            .execution_options(synchronize_session=False)
        )
    await db_session.commit()
    config = await _load_smtp_config(db_session)
    assert config["smtp_host"] == "new.test"
    assert config["smtp_from"] == "new@example.test"
    assert config["smtp_password"] == "new-secret"


@pytest.mark.parametrize("registration_default", [None, False, True])
async def test_import_snapshot_identifies_same_version_as_management(
    db_session, registration_default
):
    await SystemSettingsService.set_many(
        db_session, {"offline_threshold_minutes": 6}, None
    )
    management = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=registration_default
    )
    budget = await SystemSettingsService.get_import_limits_snapshot(
        db_session, registration_default=registration_default
    )
    assert budget["version"] == management.version


async def test_deployment_changes_invalidate_an_old_management_version(
    db_session, monkeypatch
):
    from app.config import settings
    from app.services.system_settings_service import SettingsVersionConflict

    before = await SystemSettingsService.snapshot(db_session, bypass_cache=True)
    monkeypatch.setattr(
        settings, "task_create_sync_threshold", settings.task_create_sync_threshold + 1
    )
    with pytest.raises(SettingsVersionConflict):
        await SystemSettingsService.set_many(
            db_session,
            {"video_chunk_warmup_lookahead": 0},
            None,
            expected_version=before.version,
        )


async def test_registration_default_participates_in_version_and_cas(db_session):
    from app.services.system_settings_service import SettingsVersionConflict

    before = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=False
    )
    after = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=True
    )

    assert before.values["allow_open_registration"] is False
    assert after.values["allow_open_registration"] is True
    assert before.version != after.version
    with pytest.raises(SettingsVersionConflict):
        await SystemSettingsService.set_many(
            db_session,
            {"allow_open_registration": False},
            None,
            expected_version=before.version,
            registration_default=True,
        )
    assert await db_session.scalar(select(SystemSetting)) is None


async def test_registration_override_wins_and_reset_uses_owner_default(db_session):
    await SystemSettingsService.set_many(
        db_session,
        {"allow_open_registration": False},
        None,
        registration_default=False,
    )

    overridden = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=True
    )
    assert overridden.values["allow_open_registration"] is False
    assert (
        overridden.version
        != (
            await SystemSettingsService.snapshot(
                db_session, bypass_cache=True, registration_default=False
            )
        ).version
    )
    assert overridden.rows["allow_open_registration"].value_json is False

    changes = await SystemSettingsService.reset_many(
        db_session,
        ["allow_open_registration"],
        registration_default=True,
    )
    reset = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=True
    )
    assert changes == {"allow_open_registration": (False, True)}
    assert reset.values["allow_open_registration"] is True
    assert reset.rows.get("allow_open_registration") is None


async def test_corrupt_registration_override_can_be_repaired_to_owner_default(
    db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "allow_open_registration", True)
    db_session.add(
        SystemSetting(
            key="allow_open_registration",
            value_type="bool",
            value_json="corrupt-bool-row",
        )
    )
    await db_session.flush()
    fallback = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=False
    )
    assert fallback.values["allow_open_registration"] is False

    changes = await SystemSettingsService.set_many(
        db_session,
        {"allow_open_registration": False},
        None,
        registration_default=False,
    )
    stored_value = await db_session.scalar(
        select(SystemSetting.value_json).where(
            SystemSetting.key == "allow_open_registration"
        )
    )
    repaired = await SystemSettingsService.snapshot(
        db_session, bypass_cache=True, registration_default=False
    )
    assert changes == {"allow_open_registration": (False, False)}
    assert stored_value is False
    assert repaired.values["allow_open_registration"] is False
