"""v0.8.1 · 系统设置 PATCH + 开放注册 toggle 联动验证。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_settings_service import SystemSettingsService


pytestmark = pytest.mark.asyncio


async def test_get_requires_super_admin(httpx_client, project_admin):
    _, token = project_admin
    res = await httpx_client.get(
        "/api/v1/settings/system",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


async def test_patch_round_trip(httpx_client, super_admin, db_session: AsyncSession):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    # 初始 GET
    res = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert res.status_code == 200
    initial = res.json()

    # PATCH allow_open_registration=True + invitation_ttl_days=30
    res = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"allow_open_registration": True, "invitation_ttl_days": 30},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allow_open_registration"] is True
    assert body["invitation_ttl_days"] == 30

    # 再 GET 应保持
    SystemSettingsService.invalidate()
    res = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert res.json()["allow_open_registration"] is True
    assert res.json()["invitation_ttl_days"] == 30

    # 还原（避免污染其它 test）
    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "allow_open_registration": initial["allow_open_registration"],
            "invitation_ttl_days": initial["invitation_ttl_days"],
        },
    )


async def test_patch_invalid_ttl_returns_422(httpx_client, super_admin):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    res = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"invitation_ttl_days": 999},
    )
    # pydantic Field(ge=1, le=90) 会拦截
    assert res.status_code == 422


async def test_smtp_password_masked_in_response(
    httpx_client, super_admin, db_session: AsyncSession
):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    res = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "smtp_host": "smtp.test.local",
            "smtp_port": 587,
            "smtp_user": "u@test",
            "smtp_password": "secret-p@ss",
            "smtp_from": "noreply@test.local",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # password_set true，response 不直接回密码
    assert body["smtp"]["password_set"] is True
    assert "password" not in body["smtp"]
    assert body["smtp"]["host"] == "smtp.test.local"
    assert body["smtp"]["configured"] is True

    # 清理
    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "smtp_host": "",
            "smtp_port": None,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
        },
    )


async def test_open_register_status_reflects_db_override(
    httpx_client, super_admin, db_session
):
    """PATCH allow_open_registration 后 /auth/registration-status 返回新值
    （证明 DB override 正确接管 env 默认）。"""
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"allow_open_registration": True},
    )
    SystemSettingsService.invalidate()

    res = await httpx_client.get("/api/v1/auth/registration-status")
    assert res.status_code == 200
    assert res.json()["open_registration_enabled"] is True

    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"allow_open_registration": False},
    )
    SystemSettingsService.invalidate()
    res = await httpx_client.get("/api/v1/auth/registration-status")
    assert res.json()["open_registration_enabled"] is False


async def test_audit_log_excludes_smtp_password_value(
    httpx_client, super_admin, db_session: AsyncSession
):
    """审计 detail 中 smtp_password 仅记录 changed=True，不含明文。"""
    from sqlalchemy import select
    from app.db.models.audit_log import AuditLog

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"smtp_password": "highly-secret-string"},
    )

    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "system.settings_update")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "expected at least one audit row"
    detail = rows[0].detail_json or {}
    assert "smtp_password" in detail
    assert detail["smtp_password"] == {"changed": True}
    assert "highly-secret-string" not in str(detail)

    # 清理
    await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"smtp_password": ""},
    )


async def test_runtime_knobs_metadata_zero_and_reset_contract(
    httpx_client, super_admin
):
    """The six operational knobs retain zero/false and expose reset metadata."""

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    initial = (
        await httpx_client.get("/api/v1/settings/system", headers=headers)
    ).json()

    assert initial["version"].startswith("v1-")
    for key in (
        "max_invitations_per_day",
        "offline_threshold_minutes",
        "dataset_import_max_files",
        "dataset_import_max_total_bytes",
        "task_create_sync_threshold",
        "video_chunk_warmup_lookahead",
    ):
        assert key in initial["metadata"]
        assert {
            "source",
            "deployment_default",
            "updated_at",
            "updated_by",
            "value_type",
            "unit",
            "effect",
            "min_value",
            "max_value",
            "in_range",
        } <= set(initial["metadata"][key])

    updated = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "max_invitations_per_day": 1,
            "offline_threshold_minutes": 2,
            "dataset_import_max_files": 1,
            "dataset_import_max_total_bytes": 1,
            "task_create_sync_threshold": 0,
            "video_chunk_warmup_lookahead": 0,
            "expected_version": initial["version"],
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["max_invitations_per_day"] == 1
    assert body["offline_threshold_minutes"] == 2
    assert body["dataset_import_max_files"] == 1
    assert body["dataset_import_max_total_bytes"] == 1
    assert body["task_create_sync_threshold"] == 0
    assert body["video_chunk_warmup_lookahead"] == 0
    assert body["metadata"]["task_create_sync_threshold"]["source"] == "override"

    stale = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"task_create_sync_threshold": 1, "expected_version": initial["version"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["settings"]["version"] == body["version"]

    reset = await httpx_client.post(
        "/api/v1/settings/system/reset",
        headers=headers,
        json={
            "keys": [
                "max_invitations_per_day",
                "offline_threshold_minutes",
                "dataset_import_max_files",
                "dataset_import_max_total_bytes",
                "task_create_sync_threshold",
                "video_chunk_warmup_lookahead",
            ],
            "expected_version": body["version"],
        },
    )
    assert reset.status_code == 200, reset.text
    reset_body = reset.json()
    for key in (
        "max_invitations_per_day",
        "offline_threshold_minutes",
        "dataset_import_max_files",
        "dataset_import_max_total_bytes",
        "task_create_sync_threshold",
        "video_chunk_warmup_lookahead",
    ):
        assert reset_body["metadata"][key]["source"] == "deployment"


async def test_system_settings_patch_rejects_coercion_and_unknown_keys(
    httpx_client, super_admin
):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    bool_text = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"allow_open_registration": "false"},
    )
    assert bool_text.status_code == 422

    unknown = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"not_a_setting": 1},
    )
    assert unknown.status_code == 422


async def test_system_settings_get_uses_recovery_deployment_default(
    httpx_client, app_module, super_admin, monkeypatch
):
    from app.api.v1.recovery_commands import get_registration_default
    from app.config import settings

    monkeypatch.setattr(settings, "allow_open_registration", True)

    async def recovery_default():
        return False

    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_registration_default,
        recovery_default,
    )
    _, token = super_admin
    response = await httpx_client.get(
        "/api/v1/settings/system",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allow_open_registration"] is False
    assert body["metadata"]["allow_open_registration"]["source"] == "deployment"
    assert body["metadata"]["allow_open_registration"]["deployment_default"] is False


async def test_registration_default_change_causes_cas_conflict_with_matching_readback(
    httpx_client, db_session, app_module, super_admin, monkeypatch
):
    from app.api.v1.recovery_commands import get_registration_default
    from app.config import settings
    from app.db.models.system_setting import SystemSetting
    from sqlalchemy import select

    monkeypatch.setattr(settings, "allow_open_registration", True)
    defaults = iter([False, True, True])
    resolved = []

    async def recovery_default():
        value = next(defaults)
        resolved.append(value)
        return value

    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_registration_default,
        recovery_default,
    )
    _, token = super_admin
    # Both rejected requests roll back their session. Keep the pre-operation
    # actor fixture outside those request transactions.
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    initial = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["allow_open_registration"] is False

    response = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "allow_open_registration": False,
            "expected_version": initial.json()["version"],
        },
    )

    assert response.status_code == 409, response.text
    details = response.json()["detail"]
    settings_readback = details["settings"]
    assert settings_readback["allow_open_registration"] is True
    assert (
        settings_readback["metadata"]["allow_open_registration"]["deployment_default"]
        is True
    )
    assert details["version"] == settings_readback["version"]

    reset = await httpx_client.post(
        "/api/v1/settings/system/reset",
        headers=headers,
        json={
            "keys": ["allow_open_registration"],
            "expected_version": initial.json()["version"],
        },
    )
    assert reset.status_code == 409, reset.text
    reset_details = reset.json()["detail"]
    assert reset_details["settings"]["allow_open_registration"] is True
    assert (
        reset_details["settings"]["metadata"]["allow_open_registration"][
            "deployment_default"
        ]
        is True
    )
    assert reset_details["version"] == reset_details["settings"]["version"]
    assert resolved == [False, True, True]
    row = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == "allow_open_registration")
    )
    assert row is None


@pytest.mark.parametrize("method", ["get", "patch", "reset"])
async def test_unavailable_owner_default_rejects_settings_before_writes(
    httpx_client, db_session, app_module, super_admin, monkeypatch, method
):
    from sqlalchemy import func, select
    from app.api.v1.recovery_commands import get_recovery_commands
    from app.db.models.audit_log import AuditLog
    from app.db.models.system_setting import SystemSetting
    from app.services.identity_contracts import IdentityGatewayUnavailable
    from app.services.system_settings_service import SystemSettingsService

    actor, token = super_admin
    await SystemSettingsService.set_many(
        db_session, {"allow_open_registration": True}, actor.id
    )
    await db_session.commit()
    before = await db_session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == "system.settings_update"
        )
    )

    class UnavailableOwner:
        async def registration_status(self, *, deployment_default=False):
            assert deployment_default
            raise IdentityGatewayUnavailable()

    monkeypatch.setitem(
        app_module.dependency_overrides, get_recovery_commands, UnavailableOwner
    )
    headers = {"Authorization": f"Bearer {token}"}
    if method == "get":
        response = await httpx_client.get("/api/v1/settings/system", headers=headers)
    elif method == "patch":
        response = await httpx_client.patch(
            "/api/v1/settings/system",
            headers=headers,
            json={"allow_open_registration": False},
        )
    else:
        response = await httpx_client.post(
            "/api/v1/settings/system/reset",
            headers=headers,
            json={"keys": ["allow_open_registration"]},
        )
    assert response.status_code == 503
    row = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == "allow_open_registration")
    )
    assert row.value_json is True
    assert (
        await db_session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "system.settings_update"
            )
        )
        == before
    )


async def test_registration_reset_uses_recovery_default_and_override_still_wins(
    httpx_client, db_session, app_module, super_admin, monkeypatch
):
    from app.api.v1.recovery_commands import get_registration_default
    from app.config import settings
    from app.db.models.system_setting import SystemSetting
    from sqlalchemy import select

    monkeypatch.setattr(settings, "allow_open_registration", False)

    async def recovery_default():
        return True

    monkeypatch.setitem(
        app_module.dependency_overrides,
        get_registration_default,
        recovery_default,
    )
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    initial = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["allow_open_registration"] is True

    patched = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={
            "allow_open_registration": False,
            "expected_version": initial.json()["version"],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["allow_open_registration"] is False
    assert patched.json()["metadata"]["allow_open_registration"]["source"] == "override"
    assert (
        patched.json()["metadata"]["allow_open_registration"]["deployment_default"]
        is True
    )

    reset = await httpx_client.post(
        "/api/v1/settings/system/reset",
        headers=headers,
        json={
            "keys": ["allow_open_registration"],
            "expected_version": patched.json()["version"],
        },
    )
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["allow_open_registration"] is True
    assert body["metadata"]["allow_open_registration"]["source"] == "deployment"
    row = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == "allow_open_registration")
    )
    assert row is None
