"""Resumable review chunks; no source-file I/O or dynamic query expansion."""

import asyncio
from datetime import timedelta
import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_quality_review import DatasetQualityReviewBatch as Batch
from app.services import dataset_quality_review as svc
from app.services.async_job import mark_running, update_progress
from app.workers.celery_app import celery_app
from app.workers.dataset_quality import _session

log = logging.getLogger(__name__)


def _publish(batch_id, task_id):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        review_quality.apply_async(
            args=[str(batch_id)],
            task_id=task_id,
            queue="default",
            connection=connection,
            retry=False,
        )


async def dispatch(db, batch):
    job = await db.get(AsyncJob, batch.async_job_id)
    batch_id, task_id = batch.id, job.celery_task_id
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(_publish, batch_id, task_id), 3)
    except Exception:
        log.warning("Quality review dispatch unavailable batch=%s", batch_id)


async def process_one(db, batch_id, job_id, task_id, generation):
    batch = await svc.fenced(db, batch_id, job_id, task_id, generation)
    if batch is None:
        await db.rollback()
        return False
    actor, run = await svc.authority(db, batch)
    if not await svc.process_member(db, batch, actor, run):
        await svc.finish(db, batch, "completed")
        await db.commit()
        return False
    await svc.authority(db, batch)
    batch.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    await update_progress(
        db,
        job_id,
        int(batch.processed * 100 / max(1, batch.total)),
        extra_payload={"processed_count": batch.processed},
    )
    await db.commit()
    return True


async def execute(db, batch_id, task_id):
    batch = await db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None or batch.status != "pending":
        await db.rollback()
        return
    job = await db.get(AsyncJob, batch.async_job_id, populate_existing=True)
    if job is None or job.celery_task_id != task_id or job.status not in svc.ACTIVE:
        await db.rollback()
        return
    try:
        await svc.authority(db, batch)
    except HTTPException as exc:
        await svc.finish(db, batch, "failed", svc.failure_code(exc))
        await db.commit()
        return
    generation, job_id = batch.generation, job.id
    batch.status = "running"
    batch.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    await mark_running(db, job_id)
    await db.commit()
    try:
        started = time.monotonic()
        for _ in range(25):
            if not await process_one(db, batch_id, job_id, task_id, generation):
                return
            if time.monotonic() - started > 60:
                break
        batch = await svc.fenced(db, batch_id, job_id, task_id, generation)
        if batch:
            await svc.authority(db, batch)
            job = await db.get(AsyncJob, job_id)
            batch.status = "pending"
            batch.generation += 1
            job.celery_task_id = str(uuid.uuid4())
            await dispatch(db, batch)
    except Exception as exc:
        await db.rollback()
        batch = await svc.fenced(db, batch_id, job_id, task_id, generation)
        if batch:
            await svc.finish(db, batch, "failed", svc.failure_code(exc))
            await db.commit()
        log.warning(
            "Quality review stopped batch=%s exception=%s", batch_id, type(exc).__name__
        )


async def recover(db):
    ids = list(
        await db.scalars(
            select(Batch.id)
            .where(Batch.status.in_(svc.ACTIVE), Batch.lease_until <= svc.now())
            .limit(50)
        )
    )
    for batch_id in ids:
        batch = await db.scalar(
            select(Batch)
            .where(
                Batch.id == batch_id,
                Batch.status.in_(svc.ACTIVE),
                Batch.lease_until <= svc.now(),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if batch is None:
            continue
        job = await db.get(AsyncJob, batch.async_job_id, populate_existing=True)
        if job is None or job.status not in svc.ACTIVE:
            await svc.finish(db, batch, "failed", "worker_failed")
            await db.commit()
            continue
        try:
            await svc.authority(db, batch)
        except HTTPException as exc:
            await svc.finish(db, batch, "failed", svc.failure_code(exc))
            await db.commit()
            continue
        batch.status = "pending"
        batch.generation += 1
        batch.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
        job.celery_task_id = str(uuid.uuid4())
        await dispatch(db, batch)


@celery_app.task(bind=True, acks_late=True, soft_time_limit=150, time_limit=180)
def review_quality(self, batch_id):
    asyncio.run(_session(lambda db: execute(db, uuid.UUID(batch_id), self.request.id)))


@celery_app.task
def recover_quality_reviews():
    asyncio.run(_session(recover))
