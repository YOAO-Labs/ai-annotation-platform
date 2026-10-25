"""One exact worker, durable per-directory commits, no source-file mutations."""

import asyncio
import uuid

from celery.utils.nodenames import worker_direct
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset_source import DatasetScanRun, DatasetSource
from app.db.models.async_job import AsyncJob
from app.services import dataset_scans as svc
from app.services import filesystem_storage as fs, storage_access
from app.workers.celery_app import celery_app


async def dispatch(db, run_id):
    run = await db.get(DatasetScanRun, run_id)
    source = await db.get(DatasetSource, run.source_id)
    job = await db.get(AsyncJob, run.async_job_id)
    task_id, node = job.celery_task_id, source.config["node_id"]
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_publish, run_id, task_id, node), timeout=3
        )
    except Exception:
        await db.rollback()
        # An uncertain publish may already have reached the worker. The same
        # task id remains fenced; a future resume supersedes it atomically.
        _, _, current, current_job = await svc.run_context(db, run_id)
        if (
            current_job
            and current_job.celery_task_id == task_id
            and current.status == "pending"
        ):
            await svc.finish(db, current, current_job, "failed", "dispatch_unavailable")
        await db.commit()
    await db.refresh(run)


def _publish(run_id, task_id, node):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        run_dataset_scan.apply_async(
            args=[str(run_id)],
            task_id=task_id,
            queue=worker_direct(node),
            connection=connection,
            retry=False,
            expires=300,
        )


@celery_app.task(
    bind=True,
    name="app.workers.dataset_scan.run_dataset_scan",
    acks_late=True,
    soft_time_limit=240,
    time_limit=300,
)
def run_dataset_scan(self, run_id):
    asyncio.run(execute_scan(uuid.UUID(run_id), self.request.id, self.request.hostname))


async def execute_scan(run_id, task_id, node_id):
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            fence = await svc.claim(db, run_id, task_id, node_id)
            await db.commit()
            if fence is None:
                return
            try:
                while True:
                    work = await svc.next_directory(db, run_id, task_id, fence)
                    await db.commit()
                    if work is None:
                        return
                    if work.get("transition"):
                        continue
                    if work.get("kind") == "s3":
                        manifest = await storage_access.run(
                            db,
                            work["location_id"],
                            "scan",
                            work["access"].list_page,
                            work["path"],
                            work["cursor"],
                            expected_revision=work["maintenance_revision"],
                            node_id=node_id,
                        )
                    else:
                        scan_directory = (
                            work["config"].scan_directory
                            if work.get("kind") == "sftp"
                            else lambda *args, **kwargs: fs.scan_directory(
                                work["config"], *args, **kwargs
                            )
                        )
                        manifest = await storage_access.run(
                            db,
                            work["location_id"],
                            "scan",
                            scan_directory,
                            work["path"],
                            expected_anchor=work["anchor"],
                            expected_identity=work["identity"],
                            expected_revision=work["maintenance_revision"],
                            node_id=node_id,
                            timeout=5,
                        )
                    await svc.apply_directory(
                        db, run_id, task_id, fence, work, manifest
                    )
                    await db.commit()
            except svc.LostScanLease:
                await db.rollback()
            except Exception as exc:
                await db.rollback()
                code = svc.failure_code(
                    exc,
                    "filesystem_timeout"
                    if isinstance(exc, TimeoutError)
                    else "scan_interrupted",
                )
                await svc.fail_attempt(db, run_id, task_id, fence, code, failure=exc)
                await db.commit()
    except HTTPException:
        pass  # Deleted/retired contracts cannot be revived by a queued message.
    finally:
        await engine.dispose()
