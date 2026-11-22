"""AdminDashboardStats.registration_by_day 聚合验证。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog


pytestmark = pytest.mark.asyncio


async def test_registration_by_day_aggregates_invite_vs_open(
    httpx_client, super_admin, db_session: AsyncSession
):
    actor, token = super_admin

    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            AuditLog(
                action="user.register",
                actor_id=actor.id,
                target_type="user",
                target_id=str(actor.id),
                detail_json={"invitation_id": "inv-1"},
                created_at=now,
            ),
            AuditLog(
                action="user.register",
                actor_id=actor.id,
                target_type="user",
                target_id=str(actor.id),
                detail_json={"invitation_id": "inv-2"},
                created_at=now,
            ),
            AuditLog(
                action="user.register",
                actor_id=actor.id,
                target_type="user",
                target_id=str(actor.id),
                detail_json={"invitation_id": "inv-3"},
                created_at=now,
            ),
            AuditLog(
                action="user.register",
                actor_id=actor.id,
                target_type="user",
                target_id=str(actor.id),
                detail_json={"method": "open_registration"},
                created_at=now,
            ),
            AuditLog(
                action="user.register",
                actor_id=actor.id,
                target_type="user",
                target_id=str(actor.id),
                detail_json={"method": "open_registration"},
                created_at=now,
            ),
        ]
    )
    await db_session.flush()

    res = await httpx_client.get(
        "/api/v1/dashboard/admin",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    series = body.get("registration_by_day", [])
    assert len(series) == 30
    today_iso = now.date().isoformat()
    today_row = next((r for r in series if r["date"] == today_iso), None)
    assert today_row is not None
    assert today_row["invite_count"] == 3
    assert today_row["open_count"] == 2
