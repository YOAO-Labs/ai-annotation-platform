"""Borrow the API transaction for owned Mask edit persistence."""

from __future__ import annotations

import json
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.ai_mask_accept_contract import (
    AiMaskAcceptCommand,
    AiMaskAcceptUnavailable,
)
from aap_labeling.ai_mask_accept_data import AiMaskAcceptDataClient
from aap_labeling.mask_edit_contract import MaskEditCommand, MaskEditUnavailable
from aap_labeling.mask_edit_data import MaskEditDataClient
from aap_labeling.mask_revision_contract import (
    MaskRevisionCommand,
    MaskRevisionUnavailable,
)
from aap_labeling.mask_revision_data import MaskRevisionDataClient
from app.db.models.task import Task
from app.services.labeling_annotation_data import _native_error_fields


class MaskOwnerCommandError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def mask_task_fence(task: Task, actor_id: uuid.UUID) -> dict[str, object]:
    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "actor_id": actor_id,
        "expected_batch_id": task.batch_id,
        "expected_status": task.status,
        "expected_review_round_id": task.review_round_id,
    }


async def _command(db: AsyncSession, sql: str, client_type, request, unavailable_type):
    if db.autoflush:
        await db.flush()

    async def execute(payload: str):
        return await db.scalar(text(sql), {"request": payload})

    try:
        return await client_type(execute).command(request)
    except unavailable_type as exc:
        raise HTTPException(503, detail={"reason": "mask_owner_unavailable"}) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message in {
            "aap_mask_edit_error",
            "aap_mask_revision_error",
            "aap_ai_mask_accept_error",
        }:
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = 409, {"reason": "mask_write_conflict"}
            raise MaskOwnerCommandError(status_code, response_detail) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise MaskOwnerCommandError(409, {"reason": "task_busy"}) from exc
        raise


async def mask_edit_command(db: AsyncSession, request: MaskEditCommand):
    return await _command(
        db,
        "SELECT aap_labeling_v1.mask_edit_command(CAST(:request AS jsonb))",
        MaskEditDataClient,
        request,
        MaskEditUnavailable,
    )


async def mask_revision_command(db: AsyncSession, request: MaskRevisionCommand):
    return await _command(
        db,
        "SELECT aap_labeling_v1.mask_revision_command(CAST(:request AS jsonb))",
        MaskRevisionDataClient,
        request,
        MaskRevisionUnavailable,
    )


async def ai_mask_accept_command(db: AsyncSession, request: AiMaskAcceptCommand):
    return await _command(
        db,
        "SELECT aap_labeling_v1.ai_mask_accept_command(CAST(:request AS jsonb))",
        AiMaskAcceptDataClient,
        request,
        AiMaskAcceptUnavailable,
    )
