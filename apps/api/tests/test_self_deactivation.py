"""v0.8.1 · 自助注销冷静期 + Celery beat 处理路径验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.services.deactivation_service import DeactivationService


pytestmark = pytest.mark.asyncio


async def test_request_sets_scheduled_7d_out(
    httpx_client, annotator, db_session: AsyncSession
):
    user, token = annotator
    res = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "我不需要这个账号了"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deactivation_requested_at"] is not None
    assert body["deactivation_scheduled_at"] is not None

    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.deactivation_reason == "我不需要这个账号了"
    delta = refreshed.deactivation_scheduled_at - refreshed.deactivation_requested_at
    # 6.5 ~ 7.5 天之间
    assert timedelta(days=6, hours=12) < delta < timedelta(days=7, hours=12)


async def test_double_request_returns_400(httpx_client, annotator):
    _, token = annotator
    headers = {"Authorization": f"Bearer {token}"}
    r1 = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request", headers=headers, json={}
    )
    assert r1.status_code == 200
    r2 = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request", headers=headers, json={}
    )
    assert r2.status_code == 400


async def test_cancel_clears_fields(httpx_client, annotator, db_session: AsyncSession):
    user, token = annotator
    headers = {"Authorization": f"Bearer {token}"}
    await httpx_client.post(
        "/api/v1/auth/me/deactivation-request", headers=headers, json={"reason": "test"}
    )
    res = await httpx_client.delete(
        "/api/v1/auth/me/deactivation-request", headers=headers
    )
    assert res.status_code == 200
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.deactivation_requested_at is None
    assert refreshed.deactivation_scheduled_at is None
    assert refreshed.deactivation_reason is None


async def test_last_super_admin_cannot_self_deactivate(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert res.status_code == 400
    assert "继任者" in res.json()["detail"]


async def test_execute_due_processes_overdue_users(db_session: AsyncSession, annotator):
    """模拟 cron 任务调用 execute_due — overdue 用户被 is_active=False。"""
    user, _ = annotator
    # 直接 SQL 把 scheduled_at 提前到过去
    user.deactivation_requested_at = datetime.now(timezone.utc) - timedelta(days=8)
    user.deactivation_reason = "stale"
    user.deactivation_scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db_session.flush()

    n, pending = await DeactivationService.execute_due(db_session)
    assert n == 1
    assert isinstance(pending, list)

    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.is_active is False
    assert refreshed.deactivation_scheduled_at is None
    assert refreshed.deactivation_requested_at is None

    # audit 行已写
    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "user.deactivation_approve")
                .where(AuditLog.target_id == str(user.id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) >= 1


async def test_execute_due_skips_not_yet_due(db_session: AsyncSession, annotator):
    """scheduled_at 还在未来 → 不处理。"""
    user, _ = annotator
    user.deactivation_requested_at = datetime.now(timezone.utc)
    user.deactivation_scheduled_at = datetime.now(timezone.utc) + timedelta(days=3)
    await db_session.flush()

    n, pending = await DeactivationService.execute_due(db_session)
    assert n == 0
    assert isinstance(pending, list)
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.is_active is True
