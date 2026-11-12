"""Capture the format-specific immutable input contract before enqueue."""

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


async def create_export_job(
    db, project, *, actor_id, payload, batch_id, targets, opts, task_ids=None
):
    if project.data_type == "image":
        from app.services.exporting.image_jobs import create_scoped_job

        return await create_scoped_job(
            db,
            project,
            actor_id=actor_id,
            payload=payload,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
    from app.services import async_job

    job = await async_job.create_job(
        db, kind="export", user_id=actor_id, project_id=project.id, payload=payload
    )
    receipt = await _capture_non_image_job(
        db,
        job,
        project,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
    )
    return job, receipt


async def _capture_non_image_job(
    db, job, project, *, batch_id, targets, opts, task_ids=None
):
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
