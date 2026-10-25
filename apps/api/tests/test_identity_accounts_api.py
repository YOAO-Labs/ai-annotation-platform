"""Public account behavior through the Identity command owner."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.security import hash_password, verify_password
from app.db.models.audit_log import AuditLog
from app.schemas.me import PasswordChange
from app.schemas.user import UserOut


async def test_profile_read_and_name_changes_keep_public_shape_and_noop_audit(
    httpx_client, annotator, db_session
):
    user, token = annotator
    headers = {"Authorization": f"Bearer {token}"}
    before = await httpx_client.get("/api/v1/auth/me", headers=headers)
    assert before.status_code == 200
    assert set(before.json()) == set(UserOut.model_fields)
    assert "password_hash" not in before.json()
    changed = await httpx_client.patch(
        "/api/v1/auth/me", json={"name": "  New name\u3000"}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "New name"
    noop = await httpx_client.patch(
        "/api/v1/auth/me", json={"name": "New name"}, headers=headers
    )
    assert noop.status_code == 200
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.actor_id == user.id, AuditLog.action == "user.profile_update")
    )
    assert count == 1
    invalid = await httpx_client.patch(
        "/api/v1/auth/me", json={"name": "\t \u3000"}, headers=headers
    )
    assert invalid.status_code == 400 and invalid.json()["detail"] == "姓名不能为空"
    assert (await httpx_client.get("/api/v1/auth/me", headers=headers)).json()[
        "name"
    ] == "New name"


async def test_password_change_clears_admin_marker_and_keeps_current_session(
    httpx_client, annotator, db_session
):
    user, token = annotator
    user.password_hash = hash_password("OldPassword123")
    user.password_admin_reset_at = datetime.now(UTC)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    wrong = await httpx_client.post(
        "/api/v1/auth/me/password",
        json={"old_password": "WrongPassword123", "new_password": "NewPassword123"},
        headers=headers,
    )
    assert wrong.status_code == 400 and wrong.json()["detail"] == "原密码不正确"
    same = await httpx_client.post(
        "/api/v1/auth/me/password",
        json={"old_password": "OldPassword123", "new_password": "OldPassword123"},
        headers=headers,
    )
    assert same.status_code == 400 and same.json()["detail"] == "新密码不能与原密码相同"
    changed = await httpx_client.post(
        "/api/v1/auth/me/password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword123"},
        headers=headers,
    )
    assert changed.status_code == 204 and changed.content == b""
    await db_session.refresh(user)
    assert verify_password("NewPassword123", user.password_hash)
    assert user.password_admin_reset_at is None and user.credential_generation == 0
    assert (
        await httpx_client.get("/api/v1/auth/me", headers=headers)
    ).status_code == 200
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.actor_id == user.id, AuditLog.action == "user.password_change")
    )
    assert count == 1


async def test_scoped_api_key_retains_existing_own_account_access(
    httpx_client, annotator
):
    _, token = annotator
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        json={"name": "Account fixture", "scopes": ["datasets:read"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201, created.text
    headers = {"Authorization": f"Bearer {created.json()['plaintext']}"}
    assert (
        await httpx_client.get("/api/v1/auth/me", headers=headers)
    ).status_code == 200
    changed = await httpx_client.patch(
        "/api/v1/auth/me", json={"name": "Scoped self"}, headers=headers
    )
    assert changed.status_code == 200 and changed.json()["name"] == "Scoped self"
    assert (
        await httpx_client.post("/api/v1/auth/me/heartbeat", headers=headers)
    ).status_code == 204


@pytest.mark.parametrize(
    "payload",
    [
        {"old_password": "OldSecretFixture123", "new_password": "weak-secret-fixture"},
        {"old_password": "OldSecretFixture" * 20, "new_password": "ValidSecret123"},
        {
            "old_password": "OldSecretFixture123",
            "new_password": {"nested": "NestedSecretFixture"},
        },
    ],
)
async def test_password_validation_never_echoes_input_or_context(
    httpx_client, annotator, payload
):
    _, token = annotator
    response = await httpx_client.post(
        "/api/v1/auth/me/password",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert (
        "SecretFixture" not in response.text
        and "weak-secret-fixture" not in response.text
    )
    assert all(
        set(error) == {"type", "loc", "msg"} for error in response.json()["detail"]
    )


def test_password_change_repr_hides_credentials():
    value = PasswordChange(
        old_password="OldSecretFixture123", new_password="NewSecretFixture123"
    )
    assert "SecretFixture" not in repr(value)
