"""Bind Labeling QC and repair commands to the caller's transaction."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.quality_data_contract import (
    QualityDataCommand,
    QualityDataUnavailable,
)
from aap_labeling.quality_data_data import QualityDataClient
from app.services.labeling_annotation_data import _native_error_fields

_FUNCTIONS = {
    "mask_qc_command": text(
        "SELECT aap_labeling_v1.mask_qc_command(CAST(:request AS jsonb))"
    ),
    "mask_repair_command": text(
        "SELECT aap_labeling_v1.mask_repair_command(CAST(:request AS jsonb))"
    ),
    "raster_mask_retention_query": text(
        "SELECT aap_labeling_v1.raster_mask_retention_query(CAST(:request AS jsonb))"
    ),
}


async def quality_data_command(
    db: AsyncSession,
    request: QualityDataCommand,
    *,
    flush_before: bool = True,
) -> object:
    """Execute one closed owner operation without leaving the current unit of work."""
    if flush_before and db.autoflush:
        await db.flush()

    async def execute(function: str, payload: str) -> object:
        statement = _FUNCTIONS.get(function)
        if statement is None:
            raise QualityDataUnavailable()
        return await db.scalar(statement, {"request": payload})

    try:
        return await QualityDataClient(execute).command(request)
    except QualityDataUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "labeling_quality_data_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_labeling_quality_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "labeling_quality_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise HTTPException(409, detail={"reason": "task_busy"}) from exc
        raise


def model_from_quality_row(model_type, row: dict[str, object]):
    """Build a detached SQLAlchemy-compatible record from owner JSON data."""
    values: dict[str, object] = {}
    for column in model_type.__table__.columns:
        value = row.get(column.name)
        if value is None:
            values[column.name] = None
            continue
        python_type = column.type.python_type
        if python_type is uuid.UUID:
            value = uuid.UUID(str(value))
        elif python_type is datetime and isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif python_type is list:
            item_type = getattr(column.type, "item_type", None)
            if getattr(item_type, "python_type", None) is uuid.UUID:
                value = [uuid.UUID(str(item)) for item in value]
        values[column.name] = value
    return model_type(**values)
