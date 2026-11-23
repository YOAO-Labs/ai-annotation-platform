"""Fixed Audit publication ledger operations on the caller's transaction."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_archive_output import AuditArchiveOutput


async def has_ready_output(db: AsyncSession, year: int, month: int) -> bool:
    return await db.scalar(
        text("SELECT yoao_audit_v1.archive_output_has_ready(:year, :month)"),
        {"year": year, "month": month},
    )


async def lock_ready_output(db: AsyncSession, year: int, month: int):
    result = await db.execute(
        text("SELECT * FROM yoao_audit_v1.archive_output_ready_month(:year, :month)"),
        {"year": year, "month": month},
    )
    return result.one_or_none()


async def lock_pending_output(db: AsyncSession, identifier):
    result = await db.execute(
        text(
            "SELECT * FROM yoao_audit_v1.archive_output_pending(CAST(:id AS pg_catalog.uuid))"
        ),
        {"id": identifier},
    )
    return result.one_or_none()


async def list_ready_outputs(db: AsyncSession):
    return await db.execute(
        text(
            "SELECT * FROM yoao_audit_v1.archive_output_ready_list() "
            "ORDER BY year DESC, month DESC"
        )
    )


async def output_history(db: AsyncSession) -> dict:
    result = await db.execute(
        text("SELECT * FROM yoao_audit_v1.archive_output_history()")
    )
    return dict(result.mappings().one())


async def save_candidate(db: AsyncSession, row: AuditArchiveOutput) -> None:
    await db.scalar(
        text(
            "SELECT yoao_audit_v1.archive_output_save_candidate("
            "CAST(:id AS pg_catalog.uuid), CAST(:job_id AS pg_catalog.uuid), "
            "CAST(:month AS pg_catalog.date), CAST(:parent_oid AS pg_catalog.int8), "
            "CAST(:source_oid AS pg_catalog.int8), CAST(:row_count AS pg_catalog.int8), "
            "CAST(:source_size AS pg_catalog.int8), CAST(:source_sha AS pg_catalog.text), "
            "CAST(:size AS pg_catalog.int8), CAST(:sha AS pg_catalog.text), "
            "CAST(:storage_ref AS pg_catalog.jsonb), CAST(:created_at AS pg_catalog.timestamptz))"
        ),
        {
            "id": row.id,
            "job_id": row.job_id,
            "month": row.month_start,
            "parent_oid": row.parent_oid,
            "source_oid": row.source_oid,
            "row_count": row.row_count,
            "source_size": row.source_size_bytes,
            "source_sha": row.source_sha256,
            "size": row.size_bytes,
            "sha": row.sha256,
            "storage_ref": json.dumps(
                row.storage_ref, ensure_ascii=False, allow_nan=False
            ),
            "created_at": row.created_at,
        },
    )
