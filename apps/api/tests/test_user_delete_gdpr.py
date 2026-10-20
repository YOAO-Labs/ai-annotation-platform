"""v0.6.6 · GDPR 脱敏：删除用户后，audit_logs 历史行 actor_email / actor_role 抹除。

保留 actor_id（FK 仍指向软删用户；用户行硬删时 ON DELETE SET NULL 兜底）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.audit_log import AuditLog
from app.db.models.user import User


@pytest.mark.asyncio
async def test_delete_user_redacts_audit_email(httpx_client, super_admin, db_session):
    _, sa_token = super_admin

    uid = uuid.uuid4()
    target = User(
        id=uid,
        email="gdpr-target@test.local",
        name="ToRedact",
        password_hash="$2b$12$" + "0" * 53,
        role="employee",
        is_active=True,
    )
    db_session.add(target)
    await db_session.flush()

    # 预先植入 3 条 audit_logs，由该用户作为 actor 发起
    for i in range(3):
        db_session.add(
            AuditLog(
                actor_id=uid,
                actor_email="gdpr-target@test.local",
                actor_role="annotator",
                action=f"test.dummy.{i}",
                target_type="task",
                target_id=str(uuid.uuid4()),
            )
        )
    await db_session.flush()

    # 执行删除
    r = await httpx_client.delete(
        f"/api/v1/users/{uid}",
        headers={"Authorization": f"Bearer {sa_token}"},
    )
    assert r.status_code == 200, r.text

    # 历史 audit_logs 行 actor_email / actor_role 应已抹除
    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.actor_id == uid)
                .where(AuditLog.action.like("test.dummy.%"))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    for row in rows:
        assert row.actor_email is None, "脱敏后 actor_email 应为 NULL"
        assert row.actor_role is None, "脱敏后 actor_role 应为 NULL"
        assert row.actor_id == uid, "actor_id 必须保留以维持关联"

    # USER_DELETE 那一行的 detail 应含 redacted_audit_rows >= 3
    delete_audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "user.delete")
            .where(AuditLog.target_id == str(uid))
        )
    ).scalar_one_or_none()
    assert delete_audit is not None
    assert (delete_audit.detail_json or {}).get("redacted_audit_rows", 0) == 3
