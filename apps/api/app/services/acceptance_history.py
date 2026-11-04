"""Acceptance receipts and owner-backed, version-fenced history restores."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    AcceptanceHistory,
    FindOperation,
)
from app.services.annotation_slice_contracts import SLICE_RESTORE_TTL
from app.services.labeling_mask_edit import (
    MaskOwnerCommandError,
    mask_edit_command,
    mask_task_fence,
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def ledger_key(key: str) -> str:
    return "accept:" + hashlib.sha256(key.encode()).hexdigest()


def validate_key(key: str) -> str:
    if not key or len(key) > 128 or key != key.strip():
        raise HTTPException(400, detail={"reason": "invalid_idempotency_key"})
    return key


async def find(db: AsyncSession, task_id: uuid.UUID, actor_id: uuid.UUID, key: str):
    return await mask_edit_command(
        db,
        FindOperation(
            schema=MASK_EDIT_PROTOCOL,
            operation="find_operation",
            task_id=task_id,
            actor_id=actor_id,
            idempotency_key=ledger_key(validate_key(key)),
        ),
    )


async def replay(
    db: AsyncSession,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
    request_digest: str,
):
    receipt = await find(db, task_id, actor_id, key)
    if not receipt.found:
        return None
    if receipt.kind != "accept_prediction" or receipt.request_digest != request_digest:
        raise HTTPException(409, detail={"reason": "idempotency_conflict"})
    if (
        receipt.created_at is None
        or receipt.created_at + SLICE_RESTORE_TTL <= datetime.now(timezone.utc)
    ):
        raise HTTPException(410, detail={"reason": "restore_expired"})
    return receipt.response_json


async def command(
    db: AsyncSession, task, actor_id: uuid.UUID, action: str, payload: dict[str, Any]
):
    try:
        response = await mask_edit_command(
            db,
            AcceptanceHistory(
                schema=MASK_EDIT_PROTOCOL,
                operation="acceptance_history",
                **mask_task_fence(task, actor_id),
                action=action,
                payload=payload,
            ),
        )
    except MaskOwnerCommandError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    return response.history


async def snapshot(
    db: AsyncSession, task, actor_id: uuid.UUID, annotation_id: uuid.UUID, version: int
):
    result = await command(
        db,
        task,
        actor_id,
        "snapshot",
        {
            "annotation_id": str(annotation_id),
            "expected_version": version,
        },
    )
    return result["snapshot"]


async def record(
    db: AsyncSession,
    task,
    actor_id: uuid.UUID,
    *,
    key: str,
    request_digest: str,
    prediction_id: uuid.UUID,
    annotations: list,
    video_segment_id: uuid.UUID | None,
    response: dict,
    before: dict | None = None,
):
    await command(
        db,
        task,
        actor_id,
        "record",
        {
            "acceptance_key": validate_key(key),
            "request_digest": request_digest,
            "prediction_id": str(prediction_id),
            "annotation_ids": [str(annotation.id) for annotation in annotations],
            "video_segment_id": str(video_segment_id) if video_segment_id else None,
            "before": before or {},
            "response": response,
        },
    )
