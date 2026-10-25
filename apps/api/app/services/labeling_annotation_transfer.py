"""Bind closed Annotation transfer commands to the caller's transaction."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_FUNCTION,
    AnnotationTransferCommand,
    AnnotationTransferUnavailable,
)
from aap_labeling.annotation_transfer_data import AnnotationTransferDataClient
from app.services.labeling_annotation_data import _native_error_fields


class AnnotationTransferRejected(RuntimeError):
    def __init__(self, *, status_code: int, reason: str, detail: object = None):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail


async def annotation_transfer_command(
    db: AsyncSession, request: AnnotationTransferCommand
):
    if db.autoflush:
        await db.flush()

    async def execute(payload: str):
        return await db.scalar(
            text(
                f"SELECT {ANNOTATION_TRANSFER_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))"
            ),
            {"request": payload},
        )

    try:
        return await AnnotationTransferDataClient(execute).command(request)
    except AnnotationTransferUnavailable as exc:
        raise AnnotationTransferRejected(
            status_code=503, reason="annotation_transfer_unavailable"
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_annotation_transfer_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                reason = value["reason"]
                response_detail = value.get("detail")
                if not isinstance(reason, str):
                    raise ValueError("invalid transfer rejection")
            except (KeyError, TypeError, ValueError):
                status_code, reason, response_detail = (
                    409,
                    "annotation_transfer_conflict",
                    None,
                )
            raise AnnotationTransferRejected(
                status_code=status_code, reason=reason, detail=response_detail
            ) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise AnnotationTransferRejected(
                status_code=409, reason="task_busy"
            ) from exc
        raise
