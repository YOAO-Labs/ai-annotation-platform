"""Capture the format-specific immutable input contract before enqueue."""

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.models.async_job import AsyncJob


async def _record_dispatch(db, job_id, values):
    # The API can return after another delivery has already claimed the Job.
    # Never replace that winner's identity or terminal state. A held worker
    # lock also proves that this backfill must not wait and overwrite it.
    unclaimed = (
        select(AsyncJob.id)
        .where(
            AsyncJob.id == job_id,
            AsyncJob.kind == "export",
            AsyncJob.status == "pending",
            AsyncJob.celery_task_id.is_(None),
        )
        .with_for_update(skip_locked=True)
        .cte("unclaimed_export_dispatch")
    )
    result = await db.scalar(
        update(AsyncJob)
        .where(AsyncJob.id.in_(select(unclaimed.c.id)))
        .values(**values)
        .returning(AsyncJob.id)
        .execution_options(synchronize_session=False)
    )
    return result is not None


async def record_dispatch(db, job_id, task_id):
    return await _record_dispatch(db, job_id, {"celery_task_id": task_id})


async def fail_dispatch(db, job_id):
    return await _record_dispatch(
        db,
        job_id,
        {
            "status": "failed",
            "completed_at": datetime.now(timezone.utc),
            "error_message": "export_dispatch_failed",
        },
    )


async def capture_job(db, job, project, *, batch_id, targets, opts, task_ids=None):
    if project.data_type == "image":
        from app.services.exporting.image_jobs import capture_job as capture_image

        return await capture_image(
            db,
            job,
            project,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
    if project.data_type == "video":
        from app.services.exporting.video_jobs import capture_job as capture_video

        return await capture_video(
            db,
            job,
            project,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
    from app.services.exporting.lidar_jobs import capture_job as capture_lidar

    return await capture_lidar(
        db, job, project, batch_id=batch_id, targets=targets, opts=opts
    )
