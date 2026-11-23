"""系统设置 PATCH + 开放注册 toggle 联动验证。"""

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

    res = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert res.status_code == 200
    initial = res.json()

    res = await httpx_client.patch(
        "/api/v1/settings/system",
        headers=headers,
        json={"allow_open_registration": True, "invitation_ttl_days": 30},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["allow_open_registration"] is True
    assert body["invitation_ttl_days"] == 30

    SystemSettingsService.invalidate()
    res = await httpx_client.get("/api/v1/settings/system", headers=headers)
    assert res.json()["allow_open_registration"] is True
    assert res.json()["invitation_ttl_days"] == 30

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
    assert body["smtp"]["password_set"] is True
    assert "password" not in body["smtp"]
    assert body["smtp"]["host"] == "smtp.test.local"
    assert body["smtp"]["configured"] is True

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


@pytest.fixture
async def settings_acl_world(test_engine):
    import os
    import uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.factory import create_user
    from tests.test_workspace_project_authority_locking import _api_url

    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the owned verifier's real ordinary LOGIN")
    verified = _api_url(test_engine)
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"])
    assert (url.host, url.port, url.database, url.username) == (
        verified.host,
        verified.port,
        verified.database,
        "identity_embedded_api_consumer",
    )
    auth_engine = create_async_engine(url, hide_parameters=True)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    api = async_sessionmaker(auth_engine, expire_on_commit=False)
    marker = uuid.uuid4().hex
    shadow = "diagnostics_shadow_" + marker
    actors = []
    try:
        async with owner.begin() as db:
            for index, role in enumerate(
                ("super_admin", "super_admin", "employee", "employee")
            ):
                actors.append(
                    await create_user(
                        db,
                        email=f"diagnostics-{index}-{marker}@example.test",
                        role=role,
                        name=f"Diagnostics actor {index}",
                    )
                )
            await db.execute(text(f"CREATE SCHEMA {shadow}"))
            await db.execute(
                text(
                    f"CREATE TABLE {shadow}.system_settings (key text, value_json jsonb)"
                )
            )
            await db.execute(
                text(
                    f"INSERT INTO {shadow}.system_settings VALUES ('video_chunk_warmup_lookahead', '999999')"
                )
            )
            await db.execute(text(f"CREATE TABLE {shadow}.bug_reports (id uuid)"))
            await db.execute(
                text(f"CREATE SEQUENCE {shadow}.display_seq_bug_reports START 9000000")
            )
            await db.execute(text(f"GRANT USAGE ON SCHEMA {shadow} TO {url.username}"))
        yield owner, api, actors, shadow
    finally:
        await auth_engine.dispose()
        async with owner.begin() as db:
            await db.execute(text(f"DROP SCHEMA IF EXISTS {shadow} CASCADE"))
            if actors:
                ids = [actor.id for actor in actors]
                await db.execute(
                    text(
                        "DELETE FROM yoao_settings_v1.smtp_test_attempts WHERE actor_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": ids},
                )
                await db.execute(
                    text(
                        "DELETE FROM public.users WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": ids},
                )


async def test_live_settings_cas_rechecks_after_another_admin_commits(
    settings_acl_world,
):
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.services.system_settings_service import SettingsVersionConflict

    owner, api, (winner, contender, employee, _), shadow = settings_acl_world
    key = "video_chunk_warmup_lookahead"
    async with owner() as db:
        previous = (
            (
                await db.execute(
                    text(
                        "SELECT key, value_type, value_json::text AS value_json, updated_at, "
                        "updated_by, runtime_revision FROM public.system_settings WHERE key=:key"
                    ),
                    {"key": key},
                )
            )
            .mappings()
            .one_or_none()
        )
    try:
        async with api() as first, api() as second:
            assert (
                await first.scalar(text("SELECT session_user"))
                == "identity_embedded_api_consumer"
            )
            for forbidden in (
                "SELECT value_json FROM public.system_settings",
                "SELECT * FROM yoao_settings_v1.smtp_test_attempts",
                "SELECT yoao_settings_v1.mail_delivery_policy('{}'::jsonb)",
                "SELECT yoao_settings_v1.claim_smtp_test()",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with first.begin_nested():
                        await first.execute(text(forbidden))
                assert denied.value.orig.sqlstate == "42501"
            await first.execute(text(f"SET LOCAL search_path = {shadow}, public"))
            with pytest.raises(HTTPException) as denied:
                await SystemSettingsService.snapshot(first, employee.id)
            assert denied.value.status_code == 403
            before = await SystemSettingsService.snapshot(first, winner.id)
            assert before.values[key] != 999999
            new_value = 0 if before.values[key] != 0 else 1
            await SystemSettingsService.set_many(
                first,
                {key: new_value},
                winner.id,
                expected_version=before.version,
            )
            waiting_pid = await second.scalar(
                text("SELECT pg_catalog.pg_backend_pid()")
            )
            blocker_pid = await first.scalar(text("SELECT pg_catalog.pg_backend_pid()"))
            pending = asyncio.create_task(
                SystemSettingsService.set_many(
                    second,
                    {key: 0 if new_value else 1},
                    contender.id,
                    expected_version=before.version,
                )
            )
            try:
                async with asyncio.timeout(10):
                    while not await first.scalar(
                        text(
                            "SELECT CAST(:blocker AS integer) = ANY(pg_catalog.pg_blocking_pids(CAST(:waiting AS integer)))"
                        ),
                        {"blocker": blocker_pid, "waiting": waiting_pid},
                    ):
                        if pending.done():
                            await pending
                            pytest.fail(
                                "the other edit did not wait on the owner fence"
                            )
                        await asyncio.sleep(0.01)
                    await first.commit()
                    with pytest.raises(SettingsVersionConflict) as conflict:
                        await pending
                    assert conflict.value.current.values[key] == new_value
                    assert conflict.value.current.version != before.version
                    await second.rollback()
                    limits = await SystemSettingsService.get_import_limits_snapshot(
                        second
                    )
                    assert limits["version"] == conflict.value.current.version
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
    finally:
        async with owner.begin() as db:
            await db.execute(
                text("DELETE FROM public.system_settings WHERE key=:key"), {"key": key}
            )
            if previous is not None:
                await db.execute(
                    text(
                        "INSERT INTO public.system_settings "
                        "(key,value_type,value_json,updated_at,updated_by,runtime_revision) VALUES "
                        "(:key,:value_type,CAST(:value_json AS jsonb),:updated_at,:updated_by,:runtime_revision)"
                    ),
                    dict(previous),
                )
        SystemSettingsService.invalidate(key)


@pytest.mark.parametrize("lost_acknowledgement", [False, True])
async def test_live_smtp_attempt_uses_only_dedicated_mail_login(
    settings_acl_world,
    lost_acknowledgement,
):
    import asyncio
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from yoao_identity.mail_postgres import MailSettingsPostgresData
    from yoao_identity_runtime.mail_config_v2 import MailV2WorkerSettings
    from yoao_identity_runtime.mail_worker_v2 import create_app

    from app.services.email import SmtpConfigError, send_test_email
    from tests.identity_recovery_live_helpers import SMTPInbox

    owner, api, (admin, other_admin, employee, _), _ = settings_acl_world
    dsn = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_MAIL_SETTINGS_URL"]).set(
        drivername="postgresql"
    )
    mail = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_MAIL_V2_URL"]).set(
        drivername="postgresql"
    )
    assert dsn.username == "yoao_identity_mail_settings_client"
    assert mail.username == "yoao_identity_mail_v2_client"
    inbox = SMTPInbox(lose_ack=int(lost_acknowledgement))
    try:
        sender = MailSettingsPostgresData(dsn.render_as_string(hide_password=False))
        worker = create_app(
            MailV2WorkerSettings(
                _env_file=None,
                database_url=mail.render_as_string(hide_password=False),
                settings_database_url=dsn.render_as_string(hide_password=False),
                encryption_key=Fernet.generate_key().decode(),
                smtp_host="127.0.0.1",
                smtp_port=inbox.port,
                smtp_password="private-smtp-password",
                smtp_from="sender@example.test",
                poll_interval_seconds=0.1,
            )
        )
        async with api() as db:
            for url in (dsn, mail):
                assert (url.host, url.port, url.database) == (
                    db.bind.url.host,
                    db.bind.url.port,
                    db.bind.url.database,
                )
            assert (
                await db.scalar(
                    text("SELECT yoao_settings_v1.queue_smtp_test(CAST(:id AS uuid))"),
                    {"id": employee.id},
                )
                is None
            )
            async with worker.router.lifespan_context(worker), asyncio.timeout(18):
                if lost_acknowledgement:
                    with pytest.raises(SmtpConfigError, match="transport_failed"):
                        await send_test_email(db, admin.id)
                else:
                    assert await send_test_email(db, admin.id) == {
                        "to": admin.email,
                        "from": "sender@example.test",
                        "host": "127.0.0.1",
                        "port": inbox.port,
                    }
                await inbox.wait(1)
                assert len(inbox.messages) == 1
                message = inbox.messages[0]
                assert message["To"] == admin.email
                assert message["Subject"] == "[YOAO] SMTP 测试邮件"
                assert message.get_content().splitlines() == [
                    "这是一封 YOAO 的 SMTP 测试邮件。",
                    "收到这封邮件，表示本次测试已送达你的邮箱。",
                ]
                assert await sender.claim_smtp_test() is None
                async with owner() as observer:
                    attempt = await observer.scalar(
                        text(
                            "SELECT id FROM yoao_settings_v1.smtp_test_attempts "
                            "WHERE actor_id = CAST(:actor AS uuid)"
                        ),
                        {"actor": admin.id},
                    )
                assert attempt is not None
                assert (
                    await db.scalar(
                        text(
                            "SELECT yoao_settings_v1.read_smtp_test(CAST(:actor AS uuid), CAST(:attempt AS uuid))"
                        ),
                        {"actor": other_admin.id, "attempt": attempt},
                    )
                    is None
                )
    finally:
        inbox.close()
