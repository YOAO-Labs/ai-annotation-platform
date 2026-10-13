"""v0.8.1 · POST /users/{id}/admin-reset-password 角色等级 / 复用密码 / 审计 detail 验证。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.db.models.audit_log import AuditLog
from app.db.models.user import User


pytestmark = pytest.mark.asyncio


async def test_super_admin_resets_annotator(
    httpx_client, super_admin, annotator, db_session: AsyncSession
):
    _, token = super_admin
    target_user, _ = annotator

    res = await httpx_client.post(
        f"/api/v1/users/{target_user.id}/admin-reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["target_email"] == target_user.email
    temp = body["temp_password"]
    assert len(temp) == 16

    # 临时密码应能登录（密码哈希正确）
    refreshed = await db_session.get(User, target_user.id)
    await db_session.refresh(refreshed)
    assert verify_password(temp, refreshed.password_hash)
    assert refreshed.password_admin_reset_at is not None


async def test_cannot_reset_self(httpx_client, super_admin):
    actor, token = super_admin
    res = await httpx_client.post(
        f"/api/v1/users/{actor.id}/admin-reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400


async def test_cannot_reset_same_or_higher_level(
    httpx_client, project_admin, super_admin
):
    """project_admin 不能重置 super_admin。"""
    _, pa_token = project_admin
    sa_user, _ = super_admin
    res = await httpx_client.post(
        f"/api/v1/users/{sa_user.id}/admin-reset-password",
        headers={"Authorization": f"Bearer {pa_token}"},
    )
    assert res.status_code == 403


async def test_audit_detail_excludes_password(
    httpx_client, super_admin, annotator, db_session: AsyncSession
):
    _, token = super_admin
    target_user, _ = annotator

    res = await httpx_client.post(
        f"/api/v1/users/{target_user.id}/admin-reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    temp = res.json()["temp_password"]

    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "user.password_admin_reset")
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    assert rows
    detail = rows[0].detail_json or {}
    assert detail.get("target_email") == target_user.email
    assert detail.get("target_role") == "employee"
    assert temp not in str(detail)


async def test_self_change_password_clears_admin_reset(
    httpx_client, super_admin, annotator, db_session: AsyncSession
):
    """管理员重置 → 用户自助改密 → password_admin_reset_at 清空。"""
    _, sa_token = super_admin
    target_user, _ = annotator

    # 1. 管理员先重置（拿到临时密码）
    res = await httpx_client.post(
        f"/api/v1/users/{target_user.id}/admin-reset-password",
        headers={"Authorization": f"Bearer {sa_token}"},
    )
    temp = res.json()["temp_password"]

    # 2. 用户用临时密码登录拿 token，再自助改密
    from app.core.security import create_access_token

    target_token = create_access_token(
        subject=str(target_user.id), role=target_user.role
    )

    res = await httpx_client.post(
        "/api/v1/auth/me/password",
        headers={"Authorization": f"Bearer {target_token}"},
        json={"old_password": temp, "new_password": "NewStrong1!Pass"},
    )
    assert res.status_code == 204, res.text

    refreshed = await db_session.get(User, target_user.id)
    await db_session.refresh(refreshed)
    assert refreshed.password_admin_reset_at is None
