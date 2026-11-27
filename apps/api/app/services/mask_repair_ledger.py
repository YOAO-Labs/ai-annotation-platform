"""Read canonical repair-batch rows through the labeling owner."""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.quality_data_contract import (
    QUALITY_DATA_PROTOCOL,
    ReadMaskRepairBatchById,
    ReadMaskRepairBatchByJob,
    ReadMaskRepairBatchForSource,
)

from app.db.models.mask_repair_batch import MaskRepairBatch
from app.services.labeling_quality_data import (
    model_from_quality_row,
    quality_data_command,
)


def canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


async def load_repair_batch(
    db: AsyncSession, *, batch_id: uuid.UUID, lock: bool = False
) -> MaskRepairBatch | None:
    row = await quality_data_command(
        db,
        ReadMaskRepairBatchById(
            schema=QUALITY_DATA_PROTOCOL,
            operation="read_batch_by_id",
            batch_id=batch_id,
            lock=lock,
        ),
    )
    return (
        model_from_quality_row(MaskRepairBatch, row) if isinstance(row, dict) else None
    )


async def load_repair_batch_source(
    db: AsyncSession, *, batch_id: uuid.UUID, lock: bool = False
) -> MaskRepairBatch | None:
    row = await quality_data_command(
        db,
        ReadMaskRepairBatchForSource(
            schema=QUALITY_DATA_PROTOCOL,
            operation="read_batch_for_source",
            batch_id=batch_id,
            lock=lock,
        ),
    )
    return (
        model_from_quality_row(MaskRepairBatch, row) if isinstance(row, dict) else None
    )


async def load_repair_batch_for_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    rollback: bool = False,
    lock: bool = False,
) -> MaskRepairBatch | None:
    row = await quality_data_command(
        db,
        ReadMaskRepairBatchByJob(
            schema=QUALITY_DATA_PROTOCOL,
            operation="read_batch_by_job",
            job_id=job_id,
            terminal_kind="mask_repair_rollback" if rollback else "mask_repair",
            lock=lock,
        ),
    )
    return (
        model_from_quality_row(MaskRepairBatch, row) if isinstance(row, dict) else None
    )
