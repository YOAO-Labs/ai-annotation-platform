"""Original lifecycle facts and commands borrow the caller's transaction."""

import json
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import attributes


def original_json(value):
    return (
        json.dumps(value, default=str, allow_nan=False) if value is not None else None
    )


async def _fact(db, statement, parameters):
    row = (await db.execute(statement, parameters)).mappings().one_or_none()
    return SimpleNamespace(**row) if row is not None else None


async def retirement_anchor(db, retirement_id, *, lock=False):
    return (
        await db.execute(
            text("SELECT * FROM yoao_assets_v1.original_retirement_anchor(:id, :lock)"),
            {"id": retirement_id, "lock": lock},
        )
    ).one_or_none()


async def reclaim_attempt(db, attempt_id, *, lock=False):
    return await _fact(
        db,
        text("SELECT * FROM yoao_assets_v1.reclaim_attempt_facts(:id, :lock)"),
        {"id": attempt_id, "lock": lock},
    )


async def reclaim_attempt_for_job(db, job_id):
    return await _fact(
        db,
        text("SELECT * FROM yoao_assets_v1.reclaim_attempt_for_job(:job)"),
        {"job": job_id},
    )


async def reclaim_claim(db, bucket, object_key, *, lock=False):
    return await _fact(
        db,
        text("SELECT * FROM yoao_assets_v1.reclaim_claim_facts(:bucket, :key, :lock)"),
        {"bucket": bucket, "key": object_key, "lock": lock},
    )


async def reclaim_reconciliation(db, *, lock=False, initialize=False):
    return await _fact(
        db,
        text(
            "SELECT * FROM yoao_assets_v1.original_reconciliation_facts(:lock, :initialize)"
        ),
        {"lock": lock, "initialize": initialize},
    )


async def advance_reconciliation(
    db, row, status, *, at, error=None, result=None, job_result=None
):
    return await db.scalar(
        text("""SELECT yoao_assets_v1.advance_original_reconciliation(
            :job, :task, :previous, :status, :error, CAST(:result AS jsonb), :at, CAST(:job_result AS jsonb))"""),
        {
            "job": row.async_job_id,
            "task": row.task_id,
            "previous": row.status,
            "status": status,
            "error": error,
            "result": original_json(result),
            "at": at,
            "job_result": original_json(job_result),
        },
    )


async def verification_facts(db, verification_id, *, lock=False):
    return await _fact(
        db,
        text("SELECT * FROM yoao_assets_v1.original_verification_facts(:id, :lock)"),
        {"id": verification_id, "lock": lock},
    )


async def verification_for_job(db, job_id):
    return await _fact(
        db,
        text("SELECT * FROM yoao_assets_v1.original_verification_for_job(:job)"),
        {"job": job_id},
    )


async def active_verification(db, retirement_id, *, lock=False):
    return await _fact(
        db,
        text(
            "SELECT * FROM yoao_assets_v1.active_original_verification(:retirement, :lock)"
        ),
        {"retirement": retirement_id, "lock": lock},
    )


def committed_job(job, **fields):
    for field, value in fields.items():
        attributes.set_committed_value(job, field, value)
