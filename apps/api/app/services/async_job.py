"""Jobs policy for keeping idempotent video tracker summaries."""

from __future__ import annotations

import uuid
from typing import Protocol

from app.db.models.async_job import AsyncJob, AsyncJobKind


class VideoTrackerSummaryData(Protocol):
    async def insert_video_summary(
        self,
        *,
        id: uuid.UUID,
        kind: str,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        payload: dict,
        celery_task_id: str | None,
    ) -> None: ...

    async def get_job(
        self, job_id: uuid.UUID, *, populate_existing: bool = False
    ) -> AsyncJob | None: ...


async def ensure_video_tracker_summary(
    data: VideoTrackerSummaryData,
    *,
    tracker_job_id: uuid.UUID,
    kind: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    payload: dict,
    celery_task_id: str | None,
) -> AsyncJob:
    """Retain one best-effort index row across tracker worker redeliveries.

    The canonical tracker UUID is also its summary UUID in the separate index.
    Reuse never resets lifecycle or progress and never supplies execution rights.
    """
    if kind not in {
        AsyncJobKind.VIDEO_TRACKER.value,
        AsyncJobKind.VIDEO_CORRECTION.value,
    } or (payload.get("video_tracker_job_id") != str(tracker_job_id)):
        raise ValueError("invalid video tracker summary")
    payload = {**payload, "video_execution_protocol": "aap-video-execution-data/v1"}
    await data.insert_video_summary(
        id=tracker_job_id,
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload,
        celery_task_id=celery_task_id,
    )
    summary = await data.get_job(tracker_job_id, populate_existing=True)
    if summary is None or (
        summary.kind != kind
        or summary.user_id != user_id
        or summary.project_id != project_id
        or (summary.payload or {}).get("video_tracker_job_id") != str(tracker_job_id)
        or (summary.payload or {}).get("video_execution_protocol")
        != "aap-video-execution-data/v1"
    ):
        raise ValueError("video tracker summary identity conflict")
    return summary
