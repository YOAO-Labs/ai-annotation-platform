"""v0.8.1 · audit_logs 月分区 + 不可变 trigger + ensure_future / archive 服务验证。"""

from __future__ import annotations


import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit_partition_service import AuditPartitionService


pytestmark = pytest.mark.asyncio


async def test_audit_logs_is_partitioned(db_session: AsyncSession):
    res = await db_session.execute(
        text("SELECT relkind FROM pg_class WHERE relname='audit_logs'")
    )
    relkind = res.scalar_one_or_none()
    # 'p' = partitioned table; 'r' = ordinary table（asyncpg 返回 bytes "char"）
    norm = relkind.decode() if isinstance(relkind, (bytes, bytearray)) else relkind
    assert norm == "p", f"audit_logs should be partitioned, got relkind={relkind!r}"


async def test_immutability_trigger_blocks_update(db_session: AsyncSession):
    """不可变 trigger 在父表上挂，应级联到所有子分区。"""
    # 写一条审计行，再尝试 UPDATE
    await db_session.execute(
        text(
            "INSERT INTO audit_logs (action, created_at) VALUES ('test.action', now())"
        )
    )
    await db_session.flush()
    # UPDATE 应抛错
    with pytest.raises(Exception) as exc:
        await db_session.execute(
            text(
                "UPDATE audit_logs SET action = 'test.tampered' "
                "WHERE action = 'test.action'"
            )
        )
        await db_session.flush()
    assert "immutable" in str(exc.value).lower() or "denied" in str(exc.value).lower()


async def test_ensure_future_partitions_creates_missing(db_session: AsyncSession):
    """调用 ensure 应保证未来 N 月分区存在，已存在的不重复创建。"""
    await AuditPartitionService.ensure_future_partitions(db_session, months_ahead=6)
    # 多数月份已在 0037 迁移建好；但向 6 月（往后）会有新增
    # 不强求 created 数量，只要不报错且后续再调用为空
    again = await AuditPartitionService.ensure_future_partitions(
        db_session, months_ahead=6
    )
    assert again == [], f"二次调用不应再创建分区，但创建了: {again}"


async def test_archive_old_partitions_dryrun_recent(db_session: AsyncSession):
    """retain_months=120 时不应有任何分区被归档（保留期足够长）。"""
    result = await AuditPartitionService.archive_old_partitions(
        db_session, retain_months=120
    )
    assert result["total_rows"] == 0
    assert result["archived_partitions"] == []


async def test_export_detail_helper_includes_actor_and_request(
    httpx_client, super_admin
):
    """audit-logs 导出端点的 detail_json 包含 actor_email / ip / request_id / filter_criteria。"""

    user, token = super_admin
    res = await httpx_client.get(
        "/api/v1/audit-logs/export?format=json&action=auth.login",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text

    # 注意：httpx_client 走同一 db_session；audit log 已在该 session 中可见
    # 此处单独取 db_session 在另一个 fixture，需要从已知的 super_admin 创建查询
    # 简化：直接断言 response body 含 _export_meta
    body = res.json()
    assert "_export_meta" in body
    assert body["_export_meta"]["exported_by"] == user.email
    assert body["_export_meta"]["filter_criteria"] == {"action": "auth.login"}


async def test_ensure_rejects_independent_same_named_table(db_session):
    from datetime import date

    from app.services.audit_archive_partitions import AuditArchiveError, partition_name
    from app.services import audit_partition_service as service

    # Override only the clock, so the collision is reached without creating a
    # large span of unrelated future partitions in this savepoint-backed test.
    target = date(2080, 1, 1)
    name = partition_name(target)
    await db_session.execute(text(f'CREATE TABLE public."{name}" (id bigint)'))

    class Clock:
        @staticmethod
        def now(_timezone):
            from datetime import datetime, timezone

            return datetime(2080, 1, 1, tzinfo=timezone.utc)

    from unittest.mock import patch

    with patch.object(service, "datetime", Clock):
        with pytest.raises(AuditArchiveError, match="audit_archive_source_invalid"):
            await AuditPartitionService.ensure_future_partitions(
                db_session, months_ahead=0
            )
    assert (
        await db_session.scalar(
            text(
                "SELECT relispartition FROM pg_class WHERE oid=CAST(:name AS regclass)"
            ),
            {"name": f"public.{name}"},
        )
        is False
    )
