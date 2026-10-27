"""Bounded quality chunks with durable membership and stale-worker fencing."""

import asyncio
from datetime import timedelta
import logging
import time
import uuid

from celery.utils.nodenames import worker_direct
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset_source import DatasetSource
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_quality import (
    DatasetQualityRun as Run,
    DatasetQualityResult as Result,
)
from app.services import dataset_quality as svc, dataset_content as content
from app.services.content_snapshot import ContentError
from app.services.async_job import mark_running, update_progress
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


async def next_node(db, run_id):
    row = (
        await db.execute(
            select(Result.reason_code, DatasetSource.config)
            .outerjoin(DatasetSource, DatasetSource.id == Result.source_id)
            .where(Result.run_id == run_id, Result.state == "pending")
            .order_by(Result.id)
            .limit(1)
        )
    ).first()
    return row[1].get("node_id") if row and row[0] is None and row[1] else None


def _publish(run_id, task_id, node):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        detect_quality.apply_async(
            args=[str(run_id)],
            task_id=task_id,
            queue=worker_direct(node) if node else "media",
            connection=connection,
            retry=False,
        )


async def dispatch(db, run):
    job = await db.get(AsyncJob, run.async_job_id)
    task_id, run_id = job.celery_task_id, run.id
    node = await next_node(db, run_id)
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(_publish, run_id, task_id, node), 3)
    except Exception:
        log.warning("Quality dispatch unavailable run=%s", run_id)
        # The persisted lease is the outbox. Beat retries it after expiry.


async def process_one(db, run_id, job_id, task_id, generation, node_id):
    run = await svc.fenced(db, run_id, job_id, task_id, generation)
    if run is None:
        await db.rollback()
        return False
    actor = await svc.authority(db, run)
    row = await db.scalar(
        select(Result)
        .where(Result.run_id == run_id, Result.state == "pending")
        .order_by(Result.id)
        .limit(1)
    )
    if row is None:
        await svc.finish(db, run, "completed")
        await db.commit()
        return False
    node = await next_node(db, run_id)
    if node and node_id != f"worker:{node}":
        await db.commit()
        return "yield"
    row_id, item_id, pinned = (
        row.id,
        row.item_id,
        (row.asset_id, row.sha256, row.size_bytes),
    )
    reason, snapshot = row.reason_code, None
    key_id, config = run.authority["api_key_id"], dict(run.config)
    await db.commit()  # No run/authority locks cross storage I/O.
    try:
        if reason is None:
            identity = await content.quality_identity(
                db, item_id, actor, node_id=node_id
            )
            if identity[:3] != pinned:
                raise ContentError("content_binding_changed")
            await svc.maintenance_authority(db, run)
            snapshot = await content.quality_snapshot(
                db, item_id, actor, identity, api_key_id=key_id, node_id=node_id
            )
    except Exception as exc:
        if isinstance(exc, ContentError) and exc.code.startswith("storage_"):
            raise
        error = content.content_http_error(exc)
        if error.status_code in {401, 403, 404}:
            raise HTTPException(403, detail={"code": "access_revoked"}) from None
        reason = (
            error.detail.get("code", "content_source_unavailable")
            if isinstance(error.detail, dict)
            else "content_source_unavailable"
        )
    try:
        run = await svc.fenced(db, run_id, job_id, task_id, generation)
        if run is None:
            await db.rollback()
            return False
        await svc.authority(db, run)
        row = await db.get(Result, row_id, populate_existing=True)
        row.observed_at, row.reason_code = svc.now(), reason
        row.state = "skipped" if reason else "checked"
        if snapshot is not None:
            row.bytes_verified = True
            row.reason_code = snapshot.inspection_error
            if snapshot.inspection_error == "invalid_image":
                row.findings = ["decode_failed"]
            elif snapshot.inspection_error:
                row.state = "skipped"
            else:
                row.width, row.height = snapshot.width, snapshot.height
                row.findings = (
                    []
                    if (
                        config["min_width"] <= row.width <= config["max_width"]
                        and config["min_height"] <= row.height <= config["max_height"]
                    )
                    else ["dimensions_out_of_range"]
                )
        run.processed += 1
        run.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
        await update_progress(
            db,
            job_id,
            int(run.processed * 100 / max(1, run.total)),
            extra_payload={"processed_count": run.processed},
        )
        await db.commit()
        return True
    finally:
        if snapshot is not None:
            snapshot.close()


async def execute(db, run_id, task_id, node_id):
    run = await db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.status != "pending":
        await db.rollback()
        return
    job = await db.get(AsyncJob, run.async_job_id, populate_existing=True)
    if job is None or job.celery_task_id != task_id or job.status not in svc.ACTIVE:
        await db.rollback()
        return
    try:
        await svc.authority(db, run)
    except (HTTPException, ContentError) as exc:
        await svc.finish(db, run, "failed", svc.failure_code(exc))
        await db.commit()
        return
    generation, job_id = run.generation, job.id
    run.status = "running"
    run.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
    await mark_running(db, job_id)
    await db.commit()
    try:
        started = time.monotonic()
        for _ in range(25):
            processed = await process_one(
                db, run_id, job_id, task_id, generation, node_id
            )
            if not processed:
                return
            if processed == "yield":
                break
            if time.monotonic() - started > 60:
                break
        run = await svc.fenced(db, run_id, job_id, task_id, generation)
        if run:
            await svc.authority(db, run)
            job = await db.get(AsyncJob, job_id)
            run.status = "pending"
            run.generation += 1
            job.celery_task_id = str(uuid.uuid4())
            await dispatch(db, run)
    except Exception as exc:
        await db.rollback()
        run = await svc.fenced(db, run_id, job_id, task_id, generation)
        if run:
            await svc.finish(db, run, "failed", svc.failure_code(exc))
            await db.commit()
        log.warning(
            "Quality attempt stopped run=%s exception=%s", run_id, type(exc).__name__
        )


async def recover(db):
    ids = list(
        await db.scalars(
            select(Run.id)
            .where(Run.status.in_(svc.ACTIVE), Run.lease_until <= svc.now())
            .limit(50)
        )
    )
    for run_id in ids:
        run = await db.scalar(
            select(Run)
            .where(
                Run.id == run_id,
                Run.status.in_(svc.ACTIVE),
                Run.lease_until <= svc.now(),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if run is None:
            continue
        job = await db.get(AsyncJob, run.async_job_id, populate_existing=True)
        if job is None or job.status not in svc.ACTIVE:
            await svc.finish(db, run, "failed", "worker_failed")
            await db.commit()
            continue
        try:
            await svc.authority(db, run)
        except (HTTPException, ContentError) as exc:
            await svc.finish(db, run, "failed", svc.failure_code(exc))
            await db.commit()
            continue
        run.status = "pending"
        run.generation += 1
        run.lease_until = svc.now() + timedelta(seconds=svc.LEASE_SECONDS)
        job.celery_task_id = str(uuid.uuid4())
        await dispatch(db, run)


async def _session(work):
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await work(db)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, acks_late=True, soft_time_limit=150, time_limit=180)
def detect_quality(self, run_id):
    asyncio.run(
        _session(
            lambda db: execute(
                db,
                uuid.UUID(run_id),
                self.request.id,
                f"worker:{self.request.hostname}",
            )
        )
    )


@celery_app.task
def recover_quality():
    asyncio.run(_session(recover))
