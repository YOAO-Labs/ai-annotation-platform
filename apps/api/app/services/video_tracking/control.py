"""Bind human video-tracker control to one typed Labeling Native command."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.video_control_contract import (
    VideoControlCommand,
    VideoControlUnavailable,
)
from aap_labeling.video_control_data import VideoControlDataClient
from app.services.labeling_annotation_data import _native_error_fields


class VideoControlRejected(RuntimeError):
    def __init__(self, status_code: int, detail: object):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


async def video_control_command(db: AsyncSession, request: VideoControlCommand):
    if db.autoflush:
        await db.flush()

    async def execute(payload: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.video_control_command(CAST(:request AS jsonb))"
            ),
            {"request": payload},
        )

    try:
        return await VideoControlDataClient(execute).command(request)
    except VideoControlUnavailable as exc:
        raise VideoControlRejected(
            503, {"reason": "video_control_unavailable", "retryable": True}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_video_control_error":
            try:
                payload = json.loads(detail or "{}")
                status_code = int(payload["status_code"])
                response_detail = payload["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "video_control_conflict"},
                )
            raise VideoControlRejected(status_code, response_detail) from exc
        if sqlstate in {"55P03", "40P01", "40001"}:
            raise VideoControlRejected(409, {"reason": "task_busy"}) from exc
        raise


__all__ = ["VideoControlRejected", "video_control_command"]
