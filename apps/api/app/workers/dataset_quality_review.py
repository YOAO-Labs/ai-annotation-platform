"""Resumable review chunks; no source-file I/O or dynamic query expansion."""

import asyncio
from datetime import timedelta
import logging
import time
import uuid

from fastapi import HTTPException

from app.services import jobs_lifecycle_data as jobs
from app.services import dataset_quality_review as svc
from app.services.dataset_quality_data import QualityData
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
    job = await jobs.get_job(db, batch.async_job_id)
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
    processed = await svc.process_member(db, batch, actor, run, task_id)
    if processed is None:
        await svc.finish(db, batch, "completed", task_id=task_id)
        await db.commit()
        return False
    if processed is False:
        await db.rollback()
        return False
    await svc.authority(db, batch)
    await jobs.update_progress(
        db,
        job_id,
        int(processed * 100 / max(1, batch.total)),
        extra_payload={"processed_count": processed},
    )
    await db.commit()
    return True


async def execute(db, batch_id, task_id):
    data = QualityData(db)
    batch = await data.batch_attempt(
        "claim", svc.now(), batch_id=batch_id, task_id=task_id
    )
    if batch is None:
        await db.rollback()
        return
    try:
        await svc.authority(db, batch)
    except HTTPException as exc:
        await svc.finish(db, batch, "failed", svc.failure_code(exc), task_id=task_id)
        await db.commit()
        return
    generation, job_id = batch.generation, batch.async_job_id
    claimed_at = svc.now()
    if (
        await data.transition_batch(
            batch,
            "claim",
            claimed_at,
            task_id=task_id,
            lease_until=claimed_at + timedelta(seconds=svc.LEASE_SECONDS),
        )
        is None
    ):
        await db.rollback()
        return
    await jobs.mark_running(db, job_id)
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
            await _redispatch(db, data, batch, task_id)
    except Exception as exc:
        await db.rollback()
        batch = await svc.fenced(db, batch_id, job_id, task_id, generation)
        if batch:
            await svc.finish(
                db, batch, "failed", svc.failure_code(exc), task_id=task_id
            )
            await db.commit()
        log.warning(
            "Quality review stopped batch=%s exception=%s", batch_id, type(exc).__name__
        )


async def _redispatch(db, data, batch, task_id, *, recovered=False):
    queued_at = svc.now()
    updated = await data.transition_batch(
        batch,
        "redispatch",
        queued_at,
        task_id=task_id,
        lease_until=queued_at + timedelta(seconds=svc.LEASE_SECONDS)
        if recovered
        else None,
    )
    if updated is None:
        await db.rollback()
        return
    job = await jobs.get_job(db, batch.async_job_id, populate_existing=True)
    jobs.bind_execution_id(job, str(uuid.uuid4()))
    await dispatch(db, updated)


async def recover(db):
    data = QualityData(db)
    for batch_id in await data.expired_batches(svc.now()):
        batch = await data.batch_attempt("recover", svc.now(), batch_id=batch_id)
        if batch is None:
            continue
        job = await jobs.get_job(db, batch.async_job_id, populate_existing=True)
        if job is None or job.status not in svc.ACTIVE:
            await svc.finish(db, batch, "failed", "worker_failed")
            await db.commit()
            continue
        try:
            await svc.authority(db, batch)
        except HTTPException as exc:
            await svc.finish(
                db, batch, "failed", svc.failure_code(exc), task_id=job.celery_task_id
            )
            await db.commit()
            continue
        await _redispatch(db, data, batch, job.celery_task_id, recovered=True)


@celery_app.task(bind=True, acks_late=True, soft_time_limit=150, time_limit=180)
def review_quality(self, batch_id):
    asyncio.run(_session(lambda db: execute(db, uuid.UUID(batch_id), self.request.id)))


@celery_app.task
def recover_quality_reviews():
    asyncio.run(_session(recover))
