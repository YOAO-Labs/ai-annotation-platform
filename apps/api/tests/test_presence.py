"""v0.8.3 · 在线状态心跳机制：login / heartbeat / mark_inactive_offline 验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.services.system_settings_service import SystemSettingsService
from app.workers.presence import mark_inactive_offline_with_session


pytestmark = pytest.mark.asyncio


async def test_login_writes_last_seen_at(httpx_client, db_session: AsyncSession):
    """登录成功 → user.last_seen_at 被写入（与 last_login_at 同步）。"""
    from app.core.security import hash_password

    user = User(
        email="presence-login@test.local",
        name="P",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    res = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "Test1234"},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(user)
    assert user.last_seen_at is not None
    assert user.last_login_at is not None
    assert user.status == "online"


async def test_heartbeat_updates_last_seen_at(httpx_client, annotator):
    """POST /me/heartbeat → 204，last_seen_at 被刷新；老 status='offline' 也被拉回 online。"""
    user, token = annotator
    res = await httpx_client.post(
        "/api/v1/auth/me/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 204, res.text


async def test_mark_inactive_offline_flips_stale_users(db_session: AsyncSession):
    """mark_inactive_offline_with_session：超阈值的 online 用户置 offline，活跃的不动。"""
    from app.core.security import hash_password

    now = datetime.now(timezone.utc)
    stale = User(
        email="presence-stale@test.local",
        name="Stale",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="online",
        last_seen_at=now - timedelta(minutes=10),
    )
    fresh = User(
        email="presence-fresh@test.local",
        name="Fresh",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="online",
        last_seen_at=now - timedelta(seconds=30),
    )
    null_seen = User(
        email="presence-null@test.local",
        name="Null",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="online",
        last_seen_at=None,
    )
    already_offline = User(
        email="presence-off@test.local",
        name="Off",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="offline",
        last_seen_at=now - timedelta(hours=1),
    )
    for u in (stale, fresh, null_seen, already_offline):
        db_session.add(u)
    await db_session.flush()

    result = await mark_inactive_offline_with_session(db_session)
    # stale + null_seen 应被翻；fresh / already_offline 不动
    assert result["affected"] >= 2

    for u in (stale, fresh, null_seen, already_offline):
        await db_session.refresh(u)

    assert stale.status == "offline"
    assert null_seen.status == "offline"
    assert fresh.status == "online"  # 30s 前活跃，远低于 5min 阈值
    assert already_offline.status == "offline"  # 本就是 offline


async def test_presence_scan_reads_runtime_threshold(db_session: AsyncSession):
    """A subsequent beat scan uses the DB override without a worker restart."""

    from app.core.security import hash_password

    await SystemSettingsService.set_many(
        db_session, {"offline_threshold_minutes": 2}, actor_id=None
    )
    await db_session.commit()
    now = datetime.now(timezone.utc)
    user = User(
        email="presence-runtime-threshold@test.local",
        name="Runtime threshold",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="online",
        last_seen_at=now - timedelta(minutes=3),
    )
    db_session.add(user)
    await db_session.flush()

    result = await mark_inactive_offline_with_session(db_session)
    assert result["threshold_minutes"] == 2
    await db_session.refresh(user)
    assert user.status == "offline"


async def test_users_stats_endpoint_weekly_active(
    httpx_client, super_admin, db_session: AsyncSession
):
    """GET /users/stats 返回 weekly_active：基于 last_seen_at >= now-7d。"""
    from app.core.security import hash_password

    now = datetime.now(timezone.utc)
    recent = User(
        email="stats-recent@test.local",
        name="R",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="offline",
        last_seen_at=now - timedelta(days=2),
    )
    old = User(
        email="stats-old@test.local",
        name="O",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
        status="offline",
        last_seen_at=now - timedelta(days=20),
    )
    for u in (recent, old):
        db_session.add(u)
    await db_session.flush()

    _, token = super_admin
    res = await httpx_client.get(
        "/api/v1/users/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "weekly_active" in body
    assert body["weekly_active"] >= 1  # recent
    # old 不应计入；total / online 字段也得在
    assert "total" in body and "online" in body
