"""在线状态心跳机制：login / heartbeat / mark_inactive_offline 验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.services.system_settings_service import SystemSettingsService
from app.workers.presence import mark_inactive_offline_with_session


pytestmark = pytest.mark.asyncio


async def _presence(db, user_id):
    return (
        await db.execute(
            text(
                "SELECT status, last_seen_at FROM identity_account_presence WHERE user_id=:id"
            ),
            {"id": user_id},
        )
    ).one()


async def _set_presence(db, user_id, status, last_seen_at):
    await db.execute(
        text(
            "UPDATE identity_account_presence SET status=:status, last_seen_at=:seen "
            "WHERE user_id=:id"
        ),
        {"id": user_id, "status": status, "seen": last_seen_at},
    )


async def test_login_writes_last_seen_at(httpx_client, db_session: AsyncSession):
    """登录成功后，presence.last_seen_at 与账号 last_login_at 均有值。"""
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
    status, last_seen_at = await _presence(db_session, user.id)
    assert last_seen_at is not None
    assert user.last_login_at is not None
    assert status == "online"


async def test_heartbeat_updates_last_seen_at(httpx_client, annotator):
    """POST /me/heartbeat → 204，last_seen_at 被刷新；老 status='offline' 也被拉回 online。"""
    user, token = annotator
    res = await httpx_client.post(
        "/api/v1/auth/me/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 204, res.text
    profile = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["status"] == "online"
    assert profile.json()["last_seen_at"] is not None


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
    )
    fresh = User(
        email="presence-fresh@test.local",
        name="Fresh",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    null_seen = User(
        email="presence-null@test.local",
        name="Null",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    already_offline = User(
        email="presence-off@test.local",
        name="Off",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    for u in (stale, fresh, null_seen, already_offline):
        db_session.add(u)
    await db_session.flush()
    for user, status, seen in (
        (stale, "online", now - timedelta(minutes=10)),
        (fresh, "online", now - timedelta(seconds=30)),
        (null_seen, "online", None),
        (already_offline, "offline", now - timedelta(hours=1)),
    ):
        await _set_presence(db_session, user.id, status, seen)

    result = await mark_inactive_offline_with_session(db_session)
    assert result["affected"] >= 2

    assert (await _presence(db_session, stale.id)).status == "offline"
    assert (await _presence(db_session, null_seen.id)).status == "offline"
    assert (await _presence(db_session, fresh.id)).status == "online"
    assert (await _presence(db_session, already_offline.id)).status == "offline"


async def test_presence_scan_reads_runtime_threshold(
    db_session: AsyncSession, super_admin
):
    """A subsequent beat scan uses the DB override without a worker restart."""

    from app.core.security import hash_password

    await SystemSettingsService.set_many(
        db_session, {"offline_threshold_minutes": 2}, actor_id=super_admin[0].id
    )
    await db_session.commit()
    now = datetime.now(timezone.utc)
    user = User(
        email="presence-runtime-threshold@test.local",
        name="Runtime threshold",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await _set_presence(db_session, user.id, "online", now - timedelta(minutes=3))

    result = await mark_inactive_offline_with_session(db_session)
    assert result["threshold_minutes"] == 2
    assert (await _presence(db_session, user.id)).status == "offline"


async def test_identity_command_bulk_offline_is_set_based_and_transactional(
    db_session: AsyncSession,
):
    """NULL and strict cutoff selection roll back with the caller's transaction."""
    from uuid import uuid4

    from app.core.security import hash_password
    from app.services.identity_account_data import IdentityPresenceCommandData

    fixture_tag = uuid4().hex
    password_hash = hash_password("Test1234")
    cutoff = datetime(2026, 10, 17, 11, 55, tzinfo=timezone.utc)
    states = {
        "stale": ("online", cutoff - timedelta(seconds=1)),
        "null": ("online", None),
        "fresh": ("online", cutoff + timedelta(seconds=1)),
        "equal": ("online", cutoff),
        "offline": ("offline", cutoff - timedelta(days=1)),
    }
    user_ids = {}
    for label in states:
        user_id = uuid4()
        user_ids[label] = user_id
        db_session.add(
            User(
                id=user_id,
                email=f"presence-owner-{fixture_tag}-{label}@test.local",
                name=label,
                password_hash=password_hash,
                role="employee",
                is_active=True,
            )
        )
    await db_session.flush()
    for label, (status, last_seen_at) in states.items():
        await _set_presence(db_session, user_ids[label], status, last_seen_at)
    fixture_ids = tuple(user_ids.values())

    async def read_statuses():
        return dict(
            (
                await db_session.execute(
                    text(
                        "SELECT user_id, status FROM identity_account_presence WHERE user_id = ANY(:ids)"
                    ),
                    {"ids": list(fixture_ids)},
                )
            ).all()
        )

    initial_statuses = {
        user_ids[label]: status for label, (status, _) in states.items()
    }
    savepoint = await db_session.begin_nested()
    try:
        await IdentityPresenceCommandData(db_session).mark_inactive_accounts_offline(
            cutoff
        )
        assert await read_statuses() == {
            user_ids["stale"]: "offline",
            user_ids["null"]: "offline",
            user_ids["fresh"]: "online",
            user_ids["equal"]: "online",
            user_ids["offline"]: "offline",
        }
    finally:
        await savepoint.rollback()
    assert await read_statuses() == initial_statuses


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
    )
    old = User(
        email="stats-old@test.local",
        name="O",
        password_hash=hash_password("Test1234"),
        role="employee",
        is_active=True,
    )
    for u in (recent, old):
        db_session.add(u)
    await db_session.flush()
    await _set_presence(db_session, recent.id, "offline", now - timedelta(days=2))
    await _set_presence(db_session, old.id, "offline", now - timedelta(days=20))

    _, token = super_admin
    res = await httpx_client.get(
        "/api/v1/users/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "weekly_active" in body
    assert body["weekly_active"] >= 1
    assert "total" in body and "online" in body
