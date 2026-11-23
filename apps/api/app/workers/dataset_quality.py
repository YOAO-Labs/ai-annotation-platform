"""Bounded quality chunks with durable membership and stale-worker fencing."""

import asyncio
from datetime import timedelta
import logging
import time
import uuid

from celery.utils.nodenames import worker_direct
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import async_job as jobs
from app.services import dataset_content as content, dataset_quality as svc
from app.services.content_snapshot import ContentError
from app.services.dataset_quality_data import QualityData
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


async def next_node(db, run_id):
    row = await QualityData(db).next_result(run_id)
    return row.node_id if row is not None else None


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
    job = await jobs.get_job(db, run.async_job_id)
    task_id, run_id = job.celery_task_id, run.id
    node = await next_node(db, run_id)
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(_publish, run_id, task_id, node), 3)
    except Exception:
        log.warning("Quality dispatch unavailable run=%s", run_id)


async def process_one(db, run_id, job_id, task_id, generation, node_id):
    run = await svc.fenced(db, run_id, job_id, task_id, generation)
    if run is None:
        await db.rollback()
        return False
    actor = await svc.authority(db, run)
    data = QualityData(db)
    row = await data.next_result(run_id)
    if row is None:
        await svc.finish(db, run, "completed", task_id=task_id)
        await db.commit()
        return False
    node = row.node_id
    if node and node_id != f"worker:{node}":
        await db.commit()
        return "yield"
    row_id, item_id, pinned = (
        row.id,
        row.item_id,
        (row.asset_id, row.sha256, row.size_bytes),
    )
    reason, snapshot = row.reason_code, None
    key_id, config = run.authority["api_key_id"], run.config
    await db.commit()
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
        state = "skipped" if reason else "checked"
        findings, width, height = [], None, None
        if snapshot is not None:
            reason = snapshot.inspection_error
            if reason == "invalid_image":
                findings = ["decode_failed"]
            elif reason:
                state = "skipped"
            else:
                width, height = snapshot.width, snapshot.height
                if not (
                    config["min_width"] <= width <= config["max_width"]
                    and config["min_height"] <= height <= config["max_height"]
                ):
                    findings = ["dimensions_out_of_range"]
        observed_at = svc.now()
        processed = await data.record_result(
            run,
            task_id,
            row_id,
            observed_at,
            state=state,
            reason=reason,
            bytes_verified=snapshot is not None,
            findings=findings,
            width=width,
            height=height,
            lease_until=observed_at + timedelta(seconds=svc.LEASE_SECONDS),
        )
        if processed is None:
            await db.rollback()
            return False
        await jobs.update_progress(
            db,
            job_id,
            int(processed * 100 / max(1, run.total)),
            extra_payload={"processed_count": processed},
        )
        await db.commit()
        return True
    finally:
        if snapshot is not None:
            snapshot.close()


async def execute(db, run_id, task_id, node_id):
    data = QualityData(db)
    run = await data.run_attempt("claim", svc.now(), run_id=run_id, task_id=task_id)
    if run is None:
        await db.rollback()
        return
    try:
        await svc.authority(db, run)
    except (HTTPException, ContentError) as exc:
        await svc.finish(db, run, "failed", svc.failure_code(exc), task_id=task_id)
        await db.commit()
        return
    generation, job_id = run.generation, run.async_job_id
    claimed_at = svc.now()
    if (
        await data.transition_run(
            run,
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
            await _redispatch(db, data, run, task_id)
    except Exception as exc:
        await db.rollback()
        run = await svc.fenced(db, run_id, job_id, task_id, generation)
        if run:
            await svc.finish(db, run, "failed", svc.failure_code(exc), task_id=task_id)
            await db.commit()
        log.warning(
            "Quality attempt stopped run=%s exception=%s", run_id, type(exc).__name__
        )


async def _redispatch(db, data, run, task_id, *, recovered=False):
    queued_at = svc.now()
    updated = await data.transition_run(
        run,
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
    job = await jobs.get_job(db, run.async_job_id, populate_existing=True)
    jobs.bind_execution_id(job, str(uuid.uuid4()))
    await dispatch(db, updated)


async def recover(db):
    data = QualityData(db)
    for run_id in await data.expired_runs(svc.now()):
        run = await data.run_attempt("recover", svc.now(), run_id=run_id)
        if run is None:
            continue
        job = await jobs.get_job(db, run.async_job_id, populate_existing=True)
        if job is None or job.status not in svc.ACTIVE:
            await svc.finish(db, run, "failed", "worker_failed")
            await db.commit()
            continue
        try:
            await svc.authority(db, run)
        except (HTTPException, ContentError) as exc:
            await svc.finish(
                db, run, "failed", svc.failure_code(exc), task_id=job.celery_task_id
            )
            await db.commit()
            continue
        await _redispatch(db, data, run, job.celery_task_id, recovered=True)


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
