"""Borrow the API transaction for Annotation prediction writes."""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.annotation_prediction_contract import (
    ANNOTATION_PREDICTION_PROTOCOL,
    AnnotationPredictionCommand,
    AnnotationPredictionUnavailable,
)
from aap_labeling.annotation_prediction_data import AnnotationPredictionDataClient
from app.services.labeling_annotation_data import _native_error_fields
from app.db.models.task import Task


def annotation_prediction_context(task: Task, actor_id: uuid.UUID) -> dict[str, object]:
    return {
        "schema": ANNOTATION_PREDICTION_PROTOCOL,
        "task_id": task.id,
        "project_id": task.project_id,
        "actor_id": actor_id,
        "expected_batch_id": task.batch_id,
        "expected_status": task.status,
        "expected_review_round_id": task.review_round_id,
    }


async def annotation_prediction_command(
    db: AsyncSession, request: AnnotationPredictionCommand
):
    if db.autoflush:
        await db.flush()

    async def execute(payload: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.annotation_prediction_command(CAST(:request AS jsonb))"
            ),
            {"request": payload},
        )

    try:
        return await AnnotationPredictionDataClient(execute).command(request)
    except AnnotationPredictionUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "annotation_prediction_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_annotation_prediction_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "annotation_prediction_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise HTTPException(409, detail={"reason": "task_busy"}) from exc
        raise
