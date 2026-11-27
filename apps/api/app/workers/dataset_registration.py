"""Bounded registration chunks; the durable ledger is also the dispatch outbox."""

import asyncio
import time
import uuid

from celery.utils.nodenames import worker_direct
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import dataset_registration as svc
from app.services import dataset_registration_data as data
from app.services import dataset_scans as scans
from app.services import filesystem_storage as fs
from app.services.content_snapshot import ContentError
from app.services.dataset_content import scan_snapshot
from app.application.dataset_reference_registration import register_entry
from app.services.directory_policy import permits_preview
from app.services.identity_read_relations import read_account_profile
from app.workers.celery_app import celery_app

CHUNK_ENTRIES = 25
CHUNK_SECONDS = 90


def _publish(run_id, task_id, node):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        run_dataset_registration.apply_async(
            args=[str(run_id)],
            task_id=task_id,
            queue=worker_direct(node),
            connection=connection,
            retry=False,
        )


async def dispatch(db, run_id):
    _, _, run, job = await svc.context(db, run_id)
    if run.status != "pending" or not job or not await data.arm_dispatch(db, run, job):
        await db.commit()
        return
    task_id, node = job.celery_task_id, run.snapshot["node_id"]
    await data.read_run(db, run.id)
    await db.commit()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_publish, run_id, task_id, node), timeout=3
        )
    except Exception:
        await db.rollback()
        _, _, current, current_job = await svc.context(db, run_id)
        if (
            current.status == "pending"
            and current_job
            and current_job.celery_task_id == task_id
        ):
            await data.fail_dispatch(db, current, current_job)
            await data.read_run(db, run_id)
        await db.commit()


def error_code(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return "storage_operation_unsettled"
    if isinstance(exc, (ContentError, fs.FilesystemStorageError)):
        return exc.code
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        return str(exc.detail.get("code", "registration_denied"))[:60]
    return "registration_interrupted"


async def process_one(db, run_id, task_id, fence, work):
    actor = await read_account_profile(db, work["actor_id"])
    if actor is None:
        raise HTTPException(403, detail={"code": "access_revoked"})
    digest = work["path_digest"]

    async def guard(session):
        await svc.fenced(session, run_id, task_id, fence)

    if work["phase"] == "preflight":
        snapshot = await scan_snapshot(
            db,
            work["scan_run_id"],
            digest,
            actor,
            verify=True,
            node_id=work["node_id"],
            api_key_id=work["api_key_id"],
            before_commit=guard,
            refresh_unverified=work["refresh_unverified"],
            event_job_id=work["job_id"],
            event_registration_id=work["registration_id"],
        )
        try:
            estimated, preview_error = 0, None
            if permits_preview(work["cache_policy"]):
                try:
                    from app.services.directory_previews import encode

                    estimated = len(await encode(snapshot))
                except Exception:
                    estimated, preview_error = None, "preview_estimate_failed"
            await svc.record(
                db,
                run_id,
                task_id,
                fence,
                digest,
                "ready",
                preview_estimated_bytes=estimated,
                preview_error=preview_error,
                original_size=snapshot.size,
            )
            await db.commit()
        finally:
            snapshot.close()
    else:

        async def registered(session, result):
            await svc.record(
                session,
                run_id,
                task_id,
                fence,
                digest,
                "registered" if result["created"] else "unchanged",
                result=result,
            )

        await register_entry(
            db,
            work["scan_run_id"],
            digest,
            actor,
            node_id=work["node_id"],
            api_key_id=work["api_key_id"],
            before_apply=guard,
            on_result=registered,
            event_job_id=work["job_id"],
            event_registration_id=work["registration_id"],
        )


@celery_app.task(
    bind=True,
    name="app.workers.dataset_registration.run_dataset_registration",
    acks_late=True,
    soft_time_limit=150,
    time_limit=180,
)
def run_dataset_registration(self, run_id):
    asyncio.run(
        execute_registration(uuid.UUID(run_id), self.request.id, self.request.hostname)
    )


async def execute_registration(run_id, task_id, node_id):
    engine = create_async_engine(settings.database_url)
    fence = None
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                fence = await svc.claim(db, run_id, task_id, node_id)
                await db.commit()
                if fence is None:
                    return
                started = time.monotonic()
                for _ in range(CHUNK_ENTRIES):
                    work = await svc.next_entry(db, run_id, task_id, fence)
                    await db.commit()
                    if work is None:
                        return
                    try:
                        await process_one(db, run_id, task_id, fence, work)
                    except svc.LostRegistrationLease:
                        raise
                    except Exception as exc:
                        await db.rollback()
                        code = error_code(exc)
                        if code in {"dataset_busy", "snapshot_capacity_exceeded"}:
                            await svc.yield_attempt(
                                db, run_id, task_id, fence, delay=5, code=code
                            )
                            await db.commit()
                            return
                        if code in {
                            "access_revoked",
                            "api_key_authority_revoked",
                            "configuration_changed",
                            "storage_maintenance",
                            "storage_maintenance_changed",
                            "storage_maintenance_busy",
                            "storage_operation_unsettled",
                            "source_unavailable",
                            "location_unavailable",
                            "root_identity_changed",
                            "root_not_configured",
                            "safe_open_unsupported",
                            "directory_identity_changed",
                            "registration_interrupted",
                            "content_source_unavailable",
                            "connector_host_denied",
                            "connection_unavailable",
                            "s3_redirect_denied",
                            "s3_conditions_not_supported",
                            "s3_invalid_response",
                            "s3_invalid_observation",
                            "sftp_root_not_qualified",
                            "sftp_host_key_mismatch",
                            "sftp_invalid_attributes",
                        }:
                            raise
                        if code in svc.PATH_ERRORS:
                            location, source, current, _ = await svc.fenced(
                                db, run_id, task_id, fence
                            )
                            await svc.ensure_source_available(
                                db,
                                location,
                                source,
                                work["node_id"],
                                actor_id=work["actor_id"],
                                api_key_id=work["api_key_id"],
                                key_proof=current.snapshot.get("key_proof"),
                            )
                        await svc.record(
                            db,
                            run_id,
                            task_id,
                            fence,
                            work["path_digest"],
                            "error",
                            error_code=code,
                        )
                        await db.commit()
                    if time.monotonic() - started >= CHUNK_SECONDS:
                        break
                await svc.yield_attempt(db, run_id, task_id, fence)
                await db.commit()
                await dispatch(db, run_id)
            except svc.LostRegistrationLease:
                await db.rollback()
            except Exception as exc:
                await db.rollback()
                await svc.fail_attempt(db, run_id, task_id, fence, error_code(exc))
                await db.commit()
    except HTTPException:
        pass
    finally:
        await engine.dispose()


async def recover_pending():
    engine = create_async_engine(settings.database_url)
    recovered = 0
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            candidates = await data.due_runs(db, await scans.now(db))
            await db.commit()
            for run_id in candidates:
                _, _, run, job = await svc.context(db, run_id)
                if (
                    run.status not in svc.ACTIVE
                    or not run.lease_expires_at
                    or run.lease_expires_at > await scans.now(db)
                    or not job
                ):
                    await db.commit()
                    continue
                if run.status == "running" and not await data.requeue(
                    db,
                    run,
                    job,
                    (run.generation, run.lease_token),
                    str(uuid.uuid4()),
                    recover=True,
                ):
                    await db.commit()
                    continue
                await db.commit()
                await dispatch(db, run_id)
                recovered += 1
    finally:
        await engine.dispose()
    return recovered


@celery_app.task(name="app.workers.dataset_registration.recover_registrations")
def recover_registrations():
    return asyncio.run(recover_pending())
