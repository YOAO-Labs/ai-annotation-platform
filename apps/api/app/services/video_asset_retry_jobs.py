"""Project saved video-output facts into the existing private job detail."""

import uuid

from sqlalchemy import select

from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal

KIND = AsyncJobKind.VIDEO_ASSET_RETRY.value
ACTIVE = ("pending", "running")


def _active(output_id):
    return select(AsyncJob).where(
        AsyncJob.project_id.is_(None),
        AsyncJob.kind == KIND,
        AsyncJob.status.in_(ACTIVE),
        AsyncJob.payload["output_id"].astext == str(output_id),
    )


async def observe(db, output, user_id: uuid.UUID, asset_type: str):
    """Called while prepare holds the Item lock; never reuse another actor's job."""
    job = await db.scalar(
        _active(output.id).where(
            AsyncJob.user_id == user_id,
            AsyncJob.payload["asset_type"].astext == asset_type,
        )
    )
    if job is None:
        job = await jobs.create_job(
            db,
            kind=KIND,
            user_id=user_id,
            payload={
                "output_id": str(output.id),
                "dataset_item_id": str(output.dataset_item_id),
                "asset_type": asset_type,
            },
        )
    await _project(db, job, output)
    return job


async def dispatch_pending(db, job_id):
    """A broker exception does not prove that delivery or execution failed."""
    job = await jobs.lock_job(db, job_id)
    if job is not None and job.status == "pending":
        job.result = {"stage": "dispatch_pending"}


def _failure(output, asset_type):
    if output.state != "ready":
        # Public errors are codes, never raw provider/decoder exception strings.
        return (
            "video_asset_retry_expired"
            if output.error_code == "lease_expired"
            else "video_asset_retry_failed"
        )
    if asset_type in {"probe", "poster", "frame_timetable"}:
        video = (output.result or {}).get("video") or {}
        if video.get(f"{asset_type}_error"):
            return f"video_asset_retry_{asset_type}_failed"
        if asset_type == "poster" and not video.get("poster_frame_path"):
            return "video_asset_retry_poster_failed"
        if asset_type == "frame_timetable" and not (output.result or {}).get("frames"):
            return "video_asset_retry_frame_timetable_failed"
    return None


async def _project(db, job, output):
    if job.status not in ACTIVE:
        return
    if output.state == "building":
        if output.lease_token is not None:
            await jobs.mark_running(db, job.id)
            job.result = {"stage": "processing"}
        else:
            job.result = {"stage": "queued"}
        return
    error = _failure(output, job.payload["asset_type"])
    result = {
        "asset_type": job.payload["asset_type"],
        "success_count": 0 if error else 1,
        "failed_count": 1 if error else 0,
    }
    if error:
        await jobs.mark_failed(db, job.id, error=error, result=result)
    else:
        await jobs.mark_complete(db, job.id, result=result)
    await notify_job_terminal(db, job_id=job.id)


async def sync_output(db, output):
    """Called after locking the Output, in its existing transition transaction."""
    rows = await db.scalars(
        _active(output.id)
        .order_by(AsyncJob.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for job in rows:
        await _project(db, job, output)
