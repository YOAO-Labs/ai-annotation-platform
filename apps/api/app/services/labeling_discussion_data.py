"""Execute Labeling discussion and event commands in the caller transaction."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.discussion_contract import (
    DiscussionCommand,
    DiscussionData,
    LabelingDiscussionUnavailable,
    LabelingTaskEventsUnavailable,
    TaskEventCommand,
)
from aap_labeling.discussion_data import (
    LabelingDiscussionDataClient,
    LabelingTaskEventDataClient,
)
from app.services.labeling_annotation_data import _native_error_fields

_LOCK_FAILURES = {"55P03", "40P01", "40001"}


async def discussion_command(
    db: AsyncSession,
    request: DiscussionCommand,
    *,
    flush_before: bool = True,
) -> DiscussionData:
    if flush_before:
        await db.flush()

    async def execute(payload: str):
        with db.no_autoflush:
            return await db.scalar(
                text(
                    "SELECT aap_labeling_v1.discussion_command(CAST(:request AS jsonb))"
                ),
                {"request": payload},
            )

    try:
        return await LabelingDiscussionDataClient(execute).command(request)
    except LabelingDiscussionUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "labeling_discussion_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_labeling_discussion_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "labeling_discussion_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in _LOCK_FAILURES:
            reason = (
                "comment_busy"
                if request.operation.endswith("comment")
                else "feedback_busy"
            )
            raise HTTPException(409, detail={"reason": reason}) from exc
        raise


async def task_event_command(
    db: AsyncSession,
    request: TaskEventCommand,
    *,
    flush_before: bool = True,
):
    if flush_before and db.autoflush:
        await db.flush()

    async def execute(payload: str):
        with db.no_autoflush:
            return await db.scalar(
                text(
                    "SELECT aap_labeling_v1.task_event_command(CAST(:request AS jsonb))"
                ),
                {"request": payload},
            )

    try:
        return await LabelingTaskEventDataClient(execute).command(request)
    except LabelingTaskEventsUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "labeling_task_events_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_labeling_task_events_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "task_event_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in _LOCK_FAILURES:
            raise HTTPException(409, detail={"reason": "task_event_busy"}) from exc
        raise
