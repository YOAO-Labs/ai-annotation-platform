"""Capture the format-specific immutable input contract before enqueue."""

from app.services.exporting import image_jobs, lidar_jobs, video_jobs


async def create_export_job(
    db, project, *, actor_id, payload, batch_id, targets, opts, task_ids=None
):
    if project.data_type == "image":
        return await image_jobs.create_scoped_job(
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
        return await video_jobs.capture_job(
            db,
            job,
            project,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
    return await lidar_jobs.capture_job(
        db, job, project, batch_id=batch_id, targets=targets, opts=opts
    )
