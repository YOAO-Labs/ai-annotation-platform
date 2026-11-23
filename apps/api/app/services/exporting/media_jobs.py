"""Capture the format-specific immutable input contract before enqueue."""


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
