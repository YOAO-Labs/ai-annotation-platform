"""AUTH-only API acceptance for suspension, reactivation and credential retirement."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_identity.recovery_crypto import mail_encryption_id, mail_key_from_secret
from aap_identity.security import decode_access_token
from app.db.models.user import User
from tests.factory import create_api_key, create_user
from tests.identity_recovery_helpers import seed_challenge
from tests.test_identity_admission_live import (
    ROOT,
    _docker,
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_suspension_live import (
    _api_key_state,
    _cleanup_suspensions,
    _lifecycle_state,
    _password_reset_state,
    _user_token,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_SUSPENSION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity suspension verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


def _node_public_check(mode, response, target_id):
    peer = ROOT / "apps/api/tests/fixtures/identity_suspensions_peer.mjs"
    result = subprocess.run(
        ["node", str(peer), "--validate-public"],
        input=json.dumps(
            {
                "mode": mode,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body": response.text,
                "target_user_id": str(target_id),
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Public suspension response failed independent validation"
    )
    return json.loads(result.stdout)


async def _receipts(engine, actor_id, target_id):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation_id,operation,target_generation,target_is_active,"
                "result,audit_id FROM public.identity_suspension_operation_receipts "
                "WHERE actor_id=:actor AND target_user_id=:target "
                "ORDER BY created_at,operation_id"
            ),
            {"actor": actor_id, "target": target_id},
        )
        return [dict(row._mapping) for row in rows]


async def _audit_event(maker, actor_id, target_id, action):
    async with maker() as db:
        rows = await db.execute(
            text(
                "SELECT id,action,target_type,target_id,status_code,detail_json "
                "FROM public.audit_logs WHERE actor_id=:actor AND target_id=:target "
                "AND action=:action ORDER BY id"
            ),
            {"actor": actor_id, "target": str(target_id), "action": action},
        )
        return [dict(row._mapping) for row in rows]


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_suspension_api_bindings_and_public_credential_lifecycle(
    test_engine, tmp_path, binding, record_property
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    recovery_key = mail_key_from_secret(secret)
    assert mail_encryption_id(recovery_key)
    suffix = uuid.uuid4().hex
    user_ids = [actor_id]
    jtis = []
    identity = api = None
    api_key_values = []
    jwt_values = []
    raw_reset_tokens = []
    reset_passwords = []
    private_reasons = []
    async with maker.begin() as db:
        reservation_target = await create_user(
            db,
            "super_admin",
            f"suspension-api-reservation-{suffix}@example.test",
            "Accepted reservation target",
        )
        employee_target = await create_user(
            db,
            "employee",
            f"suspension-api-employee-{suffix}@example.test",
            "Employee target",
        )
        reservation_key, reservation_key_text = await create_api_key(
            db, reservation_target.id, "Old suspension target key", ["*"]
        )
        _, manager_key_text = await create_api_key(
            db, actor_id, "Suspension API manager key", ["*"]
        )
        old_reset_token = await seed_challenge(
            db, reservation_target, token=f"suspension-api-old-reset-{suffix}"
        )
        employee_reset_token = await seed_challenge(
            db, employee_target, token=f"suspension-api-employee-reset-{suffix}"
        )
    user_ids.extend([reservation_target.id, employee_target.id])
    api_key_values.extend([reservation_key_text, manager_key_text])
    raw_reset_tokens.extend([old_reset_token, employee_reset_token])
    reservation_token = _user_token(reservation_target, secret)
    manager_token = _token(actor_id, secret)
    jwt_values.extend([reservation_token, manager_token])
    jtis.append(uuid.UUID(decode_access_token(reservation_token, secret=secret)["jti"]))
    configuration = {}
    if binding == "remote_container":
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=True,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=False,
            enable_suspension=True,
            enable_deactivation=True,
            enable_presence=False,
            recovery_mail_key=recovery_key,
        )
        identity_origin = identity.origin
    else:
        identity_origin = ""
        configuration = {
            "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
            "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
            "IDENTITY_SUSPENSION_DATABASE_URL": _dsn("SUSPENSION", test_engine),
            "IDENTITY_DEACTIVATION_DATABASE_URL": _dsn("DEACTIVATION", test_engine),
            "IDENTITY_RECOVERY_DATABASE_URL": _dsn("RECOVERY", test_engine),
            "IDENTITY_RECOVERY_MAIL_KEY": recovery_key,
        }
    try:
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )
        manager_headers = {"Authorization": f"Bearer {manager_token}"}
        manager_key_headers = {"Authorization": f"Bearer {manager_key_text}"}
        reservation_headers = {"Authorization": f"Bearer {reservation_token}"}
        async with httpx.AsyncClient(
            base_url=api.origin,
            trust_env=False,
            timeout=120,
        ) as client:
            manager_me = await client.get("/api/v1/auth/me", headers=manager_headers)
            assert manager_me.status_code == 200, manager_me.text
            manager_key_me = await client.get(
                "/api/v1/auth/me", headers=manager_key_headers
            )
            assert manager_key_me.status_code == 200, manager_key_me.text
            reservation_me = await client.get(
                "/api/v1/auth/me", headers=reservation_headers
            )
            assert reservation_me.status_code == 200, reservation_me.text
            old_key_me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {reservation_key_text}"},
            )
            assert old_key_me.status_code == 200, old_key_me.text

            reservation_reason = f"accepted reservation {suffix}"
            private_reasons.append(reservation_reason)
            accepted_reservation = await client.post(
                "/api/v1/auth/me/deactivation-request",
                headers=reservation_headers,
                json={"reason": reservation_reason},
            )
            assert accepted_reservation.status_code == 200, accepted_reservation.text
            assert accepted_reservation.json()["deactivation_scheduled_at"] is not None
            reserved_before = await _lifecycle_state(maker, reservation_target.id)

            if binding == "dedicated_dsn":
                before_misprivilege = await _lifecycle_state(maker, employee_target.id)
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT SELECT ON public.users TO identity_suspension_client"
                        )
                    )
                try:
                    misprivileged = await client.post(
                        f"/api/v1/users/{employee_target.id}/deactivate",
                        headers=manager_headers,
                    )
                    assert misprivileged.status_code == 503, misprivileged.text
                    assert secret not in misprivileged.text
                    assert control not in misprivileged.text
                    assert (
                        await _lifecycle_state(maker, employee_target.id)
                        == before_misprivilege
                    )
                    assert (
                        await _receipts(test_engine, actor_id, employee_target.id) == []
                    )
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users "
                                "FROM identity_suspension_client"
                            )
                        )

            assert (
                await client.post(
                    "/api/v1/users/not-a-uuid/deactivate", headers=manager_headers
                )
            ).status_code == 404
            assert (
                await client.post(
                    "/api/v1/users/not-a-uuid/reactivate",
                    content=b"null",
                    headers={
                        **manager_headers,
                        "content-type": "application/json",
                    },
                )
            ).status_code == 422

            target_suspend = await client.post(
                f"/api/v1/users/{reservation_target.id}/deactivate",
                headers=manager_headers,
            )
            assert target_suspend.status_code == 200, target_suspend.text
            suspended_user = _node_public_check(
                "suspend", target_suspend, reservation_target.id
            )
            assert suspended_user["disabled_kind"] == "suspended"
            assert suspended_user["disabled_reason"] == "管理员停用账号"
            assert suspended_user["deactivation_requested_at"] == reserved_before[
                "deactivation_requested_at"
            ].isoformat().replace("+00:00", "Z")
            assert suspended_user["deactivation_scheduled_at"] == reserved_before[
                "deactivation_scheduled_at"
            ].isoformat().replace("+00:00", "Z")
            assert (await _lifecycle_state(maker, reservation_target.id))[
                "credential_generation"
            ] == 1
            assert await _api_key_state(maker, reservation_key.id) is not None
            assert await _password_reset_state(maker, old_reset_token) is not None
            assert (
                await client.get("/api/v1/auth/me", headers=reservation_headers)
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {reservation_key_text}"},
                )
            ).status_code == 401

            old_reset = await client.post(
                "/api/v1/auth/reset-password",
                json={"token": old_reset_token, "new_password": "NeverUseOldToken1"},
            )
            reset_passwords.append("NeverUseOldToken1")
            assert old_reset.status_code == 400
            assert old_reset.json() == {"detail": "重置链接无效或已过期"}

            null_body_reactivation = await client.post(
                f"/api/v1/users/{reservation_target.id}/reactivate",
                content=b"null",
                headers={
                    **manager_key_headers,
                    "content-type": "application/json",
                },
            )
            assert null_body_reactivation.status_code == 200, (
                null_body_reactivation.text
            )
            reactivated_user = _node_public_check(
                "reactivate", null_body_reactivation, reservation_target.id
            )
            assert reactivated_user["disabled_kind"] is None
            assert reactivated_user["disabled_reason"] is None
            assert reactivated_user["status"] == "offline"
            assert reactivated_user["deactivation_requested_at"] == reserved_before[
                "deactivation_requested_at"
            ].isoformat().replace("+00:00", "Z")
            assert reactivated_user["deactivation_scheduled_at"] == reserved_before[
                "deactivation_scheduled_at"
            ].isoformat().replace("+00:00", "Z")
            assert (await _lifecycle_state(maker, reservation_target.id))[
                "credential_generation"
            ] == 2
            assert await _api_key_state(maker, reservation_key.id) is not None
            assert await _password_reset_state(maker, old_reset_token) is not None
            assert (
                await client.get("/api/v1/auth/me", headers=reservation_headers)
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {reservation_key_text}"},
                )
            ).status_code == 401
            old_reset_after_reactivation = await client.post(
                "/api/v1/auth/reset-password",
                json={"token": old_reset_token, "new_password": "NeverUseOldToken1"},
            )
            assert old_reset_after_reactivation.status_code == 400
            assert old_reset_after_reactivation.json() == {
                "detail": "重置链接无效或已过期"
            }

            async with maker.begin() as db:
                new_key, new_key_text = await create_api_key(
                    db, reservation_target.id, "New key after reactivation", ["*"]
                )
                new_reset_token = await seed_challenge(
                    db,
                    await db.get(User, reservation_target.id),
                    token=f"suspension-api-new-reset-{suffix}",
                )
            api_key_values.append(new_key_text)
            raw_reset_tokens.append(new_reset_token)
            new_key_me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {new_key_text}"},
            )
            assert new_key_me.status_code == 200, new_key_me.text
            assert await _api_key_state(maker, new_key.id) is None
            assert await _password_reset_state(maker, new_reset_token) is None

            new_reset = await client.post(
                "/api/v1/auth/reset-password",
                json={"token": new_reset_token, "new_password": "NewValidPassword1"},
            )
            reset_passwords.append("NewValidPassword1")
            assert new_reset.status_code == 200, new_reset.text
            assert await _password_reset_state(maker, new_reset_token) is not None

            employee_suspend = await client.post(
                f"/api/v1/users/{employee_target.id}/deactivate",
                headers=manager_headers,
            )
            assert employee_suspend.status_code == 200, employee_suspend.text
            _node_public_check("suspend", employee_suspend, employee_target.id)
            employee_reset = await client.post(
                "/api/v1/auth/reset-password",
                json={
                    "token": employee_reset_token,
                    "new_password": "NeverUseEmployeeToken1",
                },
            )
            reset_passwords.append("NeverUseEmployeeToken1")
            assert employee_reset.status_code == 400
            assert employee_reset.json() == {"detail": "重置链接无效或已过期"}
            reactivation_reason = f"\u0000\u2003 private reason {suffix} \u3000"
            private_reasons.append(reactivation_reason)
            employee_reactivate = await client.post(
                f"/api/v1/users/{employee_target.id}/reactivate",
                headers=manager_headers,
                json={"reason": reactivation_reason, "ignored_legacy_field": "ignored"},
            )
            assert employee_reactivate.status_code == 200, employee_reactivate.text
            _node_public_check("reactivate", employee_reactivate, employee_target.id)
            assert reactivation_reason not in employee_reactivate.text

            reservation_receipts = await _receipts(
                test_engine, actor_id, reservation_target.id
            )
            employee_receipts = await _receipts(
                test_engine, actor_id, employee_target.id
            )
            assert [item["operation"] for item in reservation_receipts] == [
                "suspend_account",
                "reactivate_account",
            ]
            assert [item["operation"] for item in employee_receipts] == [
                "suspend_account",
                "reactivate_account",
            ]
            reservation_suspension_audits = await _audit_event(
                maker, actor_id, reservation_target.id, "user.deactivate"
            )
            reservation_reactivation_audits = await _audit_event(
                maker, actor_id, reservation_target.id, "user.reactivate"
            )
            employee_reactivation_audits = await _audit_event(
                maker, actor_id, employee_target.id, "user.reactivate"
            )
            assert len(reservation_suspension_audits) == 1
            assert reservation_suspension_audits[0]["detail_json"] == {
                "email": reservation_target.email
            }
            assert len(reservation_reactivation_audits) == 1
            assert reservation_reactivation_audits[0]["detail_json"] == {
                "reason_present": False
            }
            assert len(employee_reactivation_audits) == 1
            assert employee_reactivation_audits[0]["detail_json"] == {
                "reason_present": True
            }
            assert reactivation_reason not in json.dumps(
                employee_reactivation_audits[0]["detail_json"], ensure_ascii=False
            )

        record_property("identity_suspension_api_binding", binding)
    finally:
        secrets_to_check = [
            secret,
            control,
            recovery_key,
            *api_key_values,
            *jwt_values,
            *raw_reset_tokens,
            *reset_passwords,
            *private_reasons,
        ]
        if identity and hasattr(identity, "container_id") and identity.container_id:
            container_logs = _docker("logs", identity.container_id)
            assert all(value not in container_logs for value in secrets_to_check)
        if api:
            api.stop()
        api_log = tmp_path / "api.log"
        if api_log.exists():
            contents = api_log.read_text(encoding="utf-8", errors="replace")
            assert all(value not in contents for value in secrets_to_check)
        if identity:
            identity.stop()
        await _cleanup_suspensions(maker, user_ids=user_ids, jtis=jtis)
