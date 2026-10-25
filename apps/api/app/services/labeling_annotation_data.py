"""Bind Labeling Annotation commands to an API transaction."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.annotation_write_contract import (
    AnnotationWriteCommand,
    AnnotationWriteUnavailable,
)
from aap_labeling.annotation_write_data import AnnotationWriteDataClient


def _native_error_fields(error: BaseException):
    pending = [error]
    seen: set[int] = set()
    fallback = (None, None, None)
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        message = getattr(current, "message", None) or getattr(
            diagnostic, "message_primary", None
        )
        detail = getattr(current, "detail", None) or getattr(
            diagnostic, "message_detail", None
        )
        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if message == "aap_annotation_write_error":
            return message, detail, sqlstate
        if sqlstate in {"55P03", "40P01", "40001"}:
            return message, detail, sqlstate
        if message is not None or sqlstate is not None:
            fallback = (message, detail, sqlstate)
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return fallback


async def annotation_write_command(db: AsyncSession, request: AnnotationWriteCommand):
    async def execute(request_json: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.annotation_write_command(CAST(:request AS jsonb))"
            ),
            {"request": request_json},
        )

    try:
        return await AnnotationWriteDataClient(execute).command(request)
    except AnnotationWriteUnavailable as exc:
        raise HTTPException(
            status_code=503, detail={"reason": "annotation_write_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_annotation_write_error":
            try:
                payload = json.loads(detail or "{}")
                status_code = int(payload.get("status_code", 409))
                response_detail = payload.get("detail", "Annotation write rejected")
            except (TypeError, ValueError):
                status_code, response_detail = 409, "Annotation write rejected"
            raise HTTPException(
                status_code=status_code, detail=response_detail
            ) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "task_busy"}
            ) from exc
        raise
