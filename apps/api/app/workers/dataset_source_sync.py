"""Beat wakes exact nodes; the database owns cadence and progress."""

import asyncio
import uuid

from fastapi import HTTPException
from celery.utils.nodenames import worker_direct

from app.services import dataset_source_sync as svc
from app.services.filesystem_storage import FilesystemStorageError
from app.services.content_snapshot import ContentError
from app.workers._db import task_session
from app.workers.celery_app import celery_app


def error_code(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return "storage_operation_unsettled"
    if isinstance(exc, (FilesystemStorageError, ContentError)):
        return exc.code
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict):
            return str(exc.detail.get("code", "sync_interrupted"))[:60]
        if exc.status_code in {403, 404}:
            return "access_revoked"
    return "sync_interrupted"


def _publish(source_id, revision, token, node):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        advance_source_sync.apply_async(
            args=[str(source_id), revision, str(token)],
            task_id=str(token),
            queue=worker_direct(node),
            connection=connection,
            retry=False,
            expires=90,
        )


async def tick(db):
    ids = await svc.due_ids(db)
    await db.commit()
    dispatched = 0
    for source_id in ids:
        try:
            work = await svc.claim_due(db, source_id)
            await db.commit()
            if not work:
                continue
            try:
                await asyncio.wait_for(asyncio.to_thread(_publish, *work), timeout=3)
                dispatched += 1
            except Exception:
                await svc.record_failure(db, *work[:3], "dispatch_unavailable")
                await db.commit()
        except Exception:
            await db.rollback()
    return {"dispatched": dispatched}


@celery_app.task(
    name="app.workers.dataset_source_sync.tick_source_syncs",
    soft_time_limit=90,
    time_limit=100,
)
def tick_source_syncs():
    return asyncio.run(run_tick())


async def run_tick():
    async with task_session() as db:
        return await tick(db)


@celery_app.task(
    bind=True,
    name="app.workers.dataset_source_sync.advance_source_sync",
    acks_late=True,
    soft_time_limit=60,
    time_limit=80,
)
def advance_source_sync(self, source_id, revision, token):
    return asyncio.run(
        execute(uuid.UUID(source_id), revision, uuid.UUID(token), self.request.hostname)
    )


async def execute(source_id, revision, token, node):
    async with task_session() as db:
        try:
            await svc.process(db, source_id, revision, token, node)
        except svc.LostWakeup:
            await db.rollback()
        except Exception as exc:
            await db.rollback()
            await svc.record_failure(db, source_id, revision, token, error_code(exc))
            await db.commit()
