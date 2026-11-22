"""审计日志不可变 trigger 测试覆盖

PG trigger `deny_audit_log_mutation` 阻断 audit_logs 的 UPDATE/DELETE；
GDPR 数据清除路径用 `SET LOCAL "app.allow_audit_update" = 'true'` 在事务内豁免；
audit_logs 按月分区，父表上的 trigger 自动级联到子分区。

四条 case：
  ① UPDATE 无豁免 → 抛 RAISE
  ② DELETE 无豁免 → 抛 RAISE
  ③ SET LOCAL 豁免后 UPDATE/DELETE 成功；事务结束后豁免不泄漏
  ④ COPY (pg_restore 路径) 走 BEFORE ROW trigger 不触发 → 不被阻断
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.asyncio


async def _insert_row(db: AsyncSession, action: str = "test.immutability") -> int:
    """写一行 audit_log 并返回 id。"""
    res = await db.execute(
        text(
            "INSERT INTO audit_logs (action, created_at) "
            "VALUES (:a, now()) RETURNING id"
        ),
        {"a": action},
    )
    rid = res.scalar_one()
    await db.flush()
    return rid


async def test_update_without_exemption_is_blocked(db_session: AsyncSession):
    """普通 UPDATE 应被 trigger 拒绝，错误信息含 immutable / denied。"""
    await _insert_row(db_session, action="test.update.blocked")
    with pytest.raises(Exception) as exc:
        await db_session.execute(
            text(
                "UPDATE audit_logs SET action='test.tampered' "
                "WHERE action='test.update.blocked'"
            )
        )
        await db_session.flush()
    msg = str(exc.value).lower()
    assert "immutable" in msg or "denied" in msg, msg


async def test_delete_without_exemption_is_blocked(db_session: AsyncSession):
    """普通 DELETE 应被 trigger 拒绝。"""
    await _insert_row(db_session, action="test.delete.blocked")
    with pytest.raises(Exception) as exc:
        await db_session.execute(
            text("DELETE FROM audit_logs WHERE action='test.delete.blocked'")
        )
        await db_session.flush()
    msg = str(exc.value).lower()
    assert "immutable" in msg or "denied" in msg, msg


async def test_set_local_exemption_allows_update_and_delete(
    db_session: AsyncSession,
):
    """SET LOCAL 豁免后 UPDATE / DELETE 成功；事务结束自动失效。"""
    rid = await _insert_row(db_session, action="test.exempt")

    await db_session.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
    res = await db_session.execute(
        text("UPDATE audit_logs SET action='test.exempt.modified' WHERE id=:id"),
        {"id": rid},
    )
    assert res.rowcount == 1

    res2 = await db_session.execute(
        text("DELETE FROM audit_logs WHERE id=:id"), {"id": rid}
    )
    assert res2.rowcount == 1


async def test_set_local_exemption_does_not_leak_across_savepoint(
    db_session: AsyncSession,
):
    """SET LOCAL 豁免不应跨 SAVEPOINT 泄漏到下一个嵌套事务。"""
    rid = await _insert_row(db_session, action="test.leak")

    sp = await db_session.begin_nested()
    try:
        await db_session.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
        await db_session.execute(
            text("UPDATE audit_logs SET action='test.leak.tmp' WHERE id=:id"),
            {"id": rid},
        )
    finally:
        await sp.rollback()

    with pytest.raises(Exception) as exc:
        await db_session.execute(
            text("UPDATE audit_logs SET action='test.leak.tampered' WHERE id=:id"),
            {"id": rid},
        )
        await db_session.flush()
    assert "immutable" in str(exc.value).lower() or "denied" in str(exc.value).lower()


async def test_copy_bypasses_row_trigger(db_session: AsyncSession):
    """pg_restore 路径走 COPY，不触发 BEFORE ROW trigger，应允许写入。

    用 INSERT INTO ... SELECT 不行（仍走 trigger）；这里用真正的 COPY 命令。
    asyncpg 的 raw_connection 暴露 copy_records_to_table，但 SQLAlchemy 异步层
    需用 connection.connection 拿到底层 asyncpg 连接。
    """
    raw = await db_session.connection()
    asyncpg_conn = await raw.get_raw_connection()
    pg_conn = asyncpg_conn.driver_connection

    from datetime import datetime, timezone

    rows = [
        ("test.copy.row", datetime.now(timezone.utc)),
    ]
    await pg_conn.copy_records_to_table(
        "audit_logs",
        records=rows,
        columns=("action", "created_at"),
    )

    res = await db_session.execute(
        text("SELECT count(*) FROM audit_logs WHERE action='test.copy.row'")
    )
    assert res.scalar_one() == 1
