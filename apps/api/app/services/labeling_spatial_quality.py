"""Labeling-owned fixed data operations for spatial quality runs and evidence."""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.spatial_quality_contract import (
    SPATIAL_QUALITY_FUNCTION,
    SpatialQualityUnavailable,
    decode_spatial_quality_reply,
    spatial_quality_request,
    spatial_quality_request_json,
)


_QUALITY_STEP = text(
    f"SELECT {SPATIAL_QUALITY_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))"
)


async def spatial_quality_data(
    db: AsyncSession, action: str, **values: object
) -> dict[str, object]:
    request = spatial_quality_request(action, **values)
    if db.autoflush:
        await db.flush()
    try:
        payload = await db.scalar(
            _QUALITY_STEP, {"request": spatial_quality_request_json(request)}
        )
        return decode_spatial_quality_reply(request, payload)
    except SpatialQualityUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"reason": "spatial_quality_data_unavailable"},
        ) from None


def quality_row(model: type[Any], value: object) -> Any:
    """Materialize the existing response shape from a validated owner projection."""
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=503,
            detail={"reason": "spatial_quality_data_unavailable"},
        )
    row = dict(value)
    for column in model.__mapper__.columns:
        name = column.key
        item = row.get(name)
        if item is None:
            continue
        try:
            python_type = column.type.python_type
        except (NotImplementedError, AttributeError):
            continue
        if python_type is uuid.UUID and isinstance(item, str):
            row[name] = uuid.UUID(item)
        elif python_type is datetime and isinstance(item, str):
            row[name] = datetime.fromisoformat(item.replace("Z", "+00:00"))
        elif python_type is list and isinstance(item, list):
            if name == "related_annotation_ids":
                row[name] = [
                    uuid.UUID(value) if isinstance(value, str) else value
                    for value in item
                ]
    return model(**row)


def quality_record(value: object) -> SimpleNamespace:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=503,
            detail={"reason": "spatial_quality_data_unavailable"},
        )
    row = dict(value)
    for name, item in row.items():
        if isinstance(item, str) and (
            name == "id" or name.endswith("_id") or name in {"dataset_item_id"}
        ):
            try:
                row[name] = uuid.UUID(item)
            except ValueError:
                pass
        elif name.endswith("_at") and isinstance(item, str):
            row[name] = datetime.fromisoformat(item.replace("Z", "+00:00"))
    return SimpleNamespace(**row)


def quality_records(values: object) -> list[SimpleNamespace]:
    if not isinstance(values, list):
        raise HTTPException(
            status_code=503,
            detail={"reason": "spatial_quality_data_unavailable"},
        )
    return [quality_record(value) for value in values]


def quality_rows(model: type[Any], values: object) -> list[Any]:
    if not isinstance(values, list):
        raise HTTPException(
            status_code=503,
            detail={"reason": "spatial_quality_data_unavailable"},
        )
    return [quality_row(model, value) for value in values]


__all__ = [
    "quality_record",
    "quality_records",
    "quality_row",
    "quality_rows",
    "spatial_quality_data",
]
