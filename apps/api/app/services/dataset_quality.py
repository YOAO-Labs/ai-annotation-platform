"""Fixed membership, current authority, fenced checkpoints and human decisions."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException

from app.schemas.dataset_quality import DatasetQualityResultOut
from app.services import jobs_lifecycle_data as jobs
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services import dataset_source_access as access
from app.services.async_job_notify import notify_job_terminal
from app.services.audit import AuditService
from app.services.dataset_content import key_authority
from app.services.dataset_quality_data import QualityData

ACTIVE = {"pending", "running"}
LEASE_SECONDS = 120
MAX_MEMBERS = 10000


def now():
    return datetime.now(timezone.utc)


async def list_runs(db, dataset_id, actor, *, limit, offset):
    actor = await access.assert_dataset_manage(db, dataset_id, actor)
    return await QualityData(db).run_page(
        dataset_id, actor.id, limit=limit, offset=offset
    )


async def require_run(db, dataset_id, run_id, actor, *, lock=False):
    actor = await access.assert_dataset_manage(db, dataset_id, actor)
    run = await QualityData(db).run(dataset_id, run_id, actor.id, lock=lock)
    if run is None:
        raise HTTPException(404, "Quality run not found")
    return run


async def authority(db, run):
    actor = await access.require_manager(db, uuid.UUID(run.authority["actor_id"]))
    await require_run(db, run.dataset_id, run.id, actor)
    proof = await key_authority(
        db, actor, run.authority["api_key_id"], "datasets:write"
    )
    if not await QualityData(db).run_key_matches(run, proof):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await require_write(db, run.dataset_id, snapshot=run.authority)
    await maintenance_authority(db, run, lock=True)
    return actor


async def maintenance_authority(db, run, *, lock=False, captured=None):
    data = QualityData(db)
    location_ids = {
        member.location_id
        for member in await data.maintenance_members(run.id)
        if member.location_id is not None
    }
    if captured is None:
        captured = run.authority.get("storage_maintenance", {})
    locations = sorted(location_ids | {uuid.UUID(key) for key in captured}, key=str)
    if lock and locations:
        await data.lock_locations(run.id, locations)
    for location_id in locations:
        await storage_access.check(
            db,
            location_id,
            expected_revision=captured.get(str(location_id), 1),
            lock=lock,
        )


async def require_write(db, dataset_id, *, snapshot=None):
    """Keep historical evidence readable while fencing new business writes."""
    from app.services.dataset_lifecycle import require_writable

    expected = (
        snapshot.get("dataset_lifecycle_revision", 1) if snapshot is not None else None
    )
    return await require_writable(db, dataset_id, expected_revision=expected, lock=True)


def failure_code(exc):
    if isinstance(exc, ContentError) and exc.code.startswith("storage_"):
        return exc.code
    if isinstance(exc, HTTPException):
        if exc.status_code in {401, 403, 404}:
            return "access_revoked"
        if isinstance(exc.detail, dict) and exc.detail.get("code") in {
            "dataset_archived",
            "dataset_lifecycle_changed",
            "dataset_busy",
        }:
            return exc.detail["code"]
    return "worker_failed"


async def attach_job(db, run, actor, key_id, *, maintenance=None):
    dataset = await require_write(db, run.dataset_id, snapshot=run.authority)
    if maintenance is None:
        maintenance = run.authority.get("storage_maintenance", {})
    await maintenance_authority(db, run, lock=True, captured=maintenance)
    proof = await key_authority(db, actor, key_id, "datasets:write")
    job = await jobs.create_job(
        db,
        kind="dataset_quality",
        user_id=actor.id,
        payload={
            "dataset_id": str(run.dataset_id),
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "run_id": str(run.id),
            "rule_version": run.rule_version,
            "total": run.total,
        },
        celery_task_id=str(uuid.uuid4()),
    )
    updated = await QualityData(db).attach_run(
        run,
        job.id,
        actor.id,
        key_id,
        proof,
        dataset.lifecycle_revision,
        maintenance,
        now() + timedelta(seconds=LEASE_SECONDS),
    )
    if updated is None:
        raise HTTPException(409, "Quality attempt changed")
    return updated


async def start(db, dataset_id, actor, config, key_id=None):
    actor = await access.assert_dataset_manage(db, dataset_id, actor)
    dataset = await require_write(db, dataset_id)
    data = QualityData(db)
    run = await data.begin_run(
        dataset_id, actor.id, config.model_dump(), dataset.lifecycle_revision
    )
    if run == "dataset_not_found":
        raise HTTPException(404, "Dataset not found")
    if run == "active":
        raise HTTPException(409, "A quality run is already active")
    if run == "too_many_members":
        raise HTTPException(422, "Quality runs support at most 10000 fixed members")
    maintenance = {}
    for member in await data.maintenance_members(run.id):
        source = await access.require_source(db, member.source_id, actor)
        await data.lock_locations(run.id, [source.location_id])
        maintenance[str(source.location_id)] = await storage_access.check(
            db, source.location_id, lock=True
        )
    run = await attach_job(db, run, actor, key_id, maintenance=maintenance)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality.start",
        target_type="dataset_quality",
        target_id=run.id,
        detail={
            "dataset_id": str(dataset_id),
            "total": run.total,
            "config": run.config,
        },
    )
    return run


async def resume(db, dataset_id, run_id, actor, key_id=None):
    run = await require_run(db, dataset_id, run_id, actor, lock=True)
    await require_write(db, dataset_id, snapshot=run.authority)
    if run.status == "completed" or (
        run.status in ACTIVE and run.lease_until and run.lease_until > now()
    ):
        raise HTTPException(409, "Run is completed or still leased")
    if await QualityData(db).other_active_run(run):
        raise HTTPException(409, "Another quality run is active")
    if run.status in ACTIVE and run.async_job_id:
        await jobs.mark_failed(db, run.async_job_id, error="lease_expired")
        await notify_job_terminal(db, job_id=run.async_job_id)
    run = await attach_job(db, run, actor, key_id)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality.resume",
        target_type="dataset_quality",
        target_id=run.id,
    )
    return run


async def finish(db, run, status, error=None, *, task_id=None):
    if status == "completed":
        await require_write(db, run.dataset_id, snapshot=run.authority)
        await maintenance_authority(db, run, lock=True)
    updated = await QualityData(db).transition_run(
        run, status, now(), task_id=task_id, error=error
    )
    if updated and updated.async_job_id:
        await finish_job(
            db,
            updated.async_job_id,
            status,
            error,
            {"run_id": str(run.id), **updated.summary},
        )
    return updated


async def finish_job(db, job_id, status, error, result):
    if status == "completed":
        await jobs.mark_complete(db, job_id, result=result)
    elif status == "cancelled":
        await jobs.mark_cancelled(db, job_id, result=result)
    else:
        await jobs.mark_failed(
            db, job_id, error=error or "worker_failed", result=result
        )
    await notify_job_terminal(db, job_id=job_id)


async def cancel_job(db, job, actor):
    run = await require_run(
        db,
        uuid.UUID(job.payload["dataset_id"]),
        uuid.UUID(job.payload["run_id"]),
        actor,
        lock=True,
    )
    if run.async_job_id != job.id or run.status not in ACTIVE:
        raise HTTPException(409, "Quality attempt has ended")
    await finish(db, run, "cancelled", "cancelled")


async def result_page(
    db, run, actor, *, limit, offset, finding=None, review=None, sha256=None
):
    page = await QualityData(db).result_page(
        run.id,
        actor.id,
        limit=limit,
        offset=offset,
        finding=finding,
        review=review,
        sha=sha256,
    )
    return {
        "items": [DatasetQualityResultOut.model_validate(row) for row in page["items"]],
        "total": page["total"],
    }


async def review_result(
    db, run, result_id, actor, payload, *, batch_id=None, locked=None
):
    data = QualityData(db)
    row = locked if locked is not None else await data.lock_result(run.id, result_id)
    if row is None:
        raise HTTPException(404, "Quality result not found")
    if locked is None:
        await require_write(db, run.dataset_id)
    if row["state"] == "pending":
        raise HTTPException(409, "Result is not available yet")
    if row["review_revision"] != payload.expected_revision:
        raise HTTPException(409, "Review changed; reload the result before deciding")
    note = payload.note.strip()
    if not note:
        raise HTTPException(422, "A review reason is required")
    revision = await data.apply_review(
        run.id,
        result_id,
        actor.id,
        payload.expected_revision,
        payload.state,
        note,
        now(),
    )
    if revision is None:
        raise HTTPException(409, "Review changed; reload the result before deciding")
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality.review",
        target_type="dataset_quality_result",
        target_id=result_id,
        detail={
            "run_id": str(run.id),
            "item_id": row["item_id"],
            "asset_id": row["asset_id"],
            "sha256": row["sha256"],
            "rule_version": run.rule_version,
            "config": run.config,
            "before": row["review_state"],
            "state": payload.state,
            "note": note,
            "revision": revision,
            **({"batch_id": str(batch_id)} if batch_id else {}),
        },
    )
    return {"review_revision": revision}


async def fenced(db, run_id, job_id, task_id, generation):
    return await QualityData(db).run_attempt(
        "worker",
        now(),
        run_id=run_id,
        job_id=job_id,
        task_id=task_id,
        generation=generation,
    )


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    run = await QualityData(db).run_attempt(
        "signal", now(), job_id=job_id, task_id=task_id
    )
    if run is not None:
        await finish(
            db,
            run,
            "cancelled" if cancelled else "failed",
            "cancelled" if cancelled else "worker_failed",
            task_id=task_id,
        )


async def reconcile_terminal(db, job_id, *, cancelled):
    await QualityData(db).reconcile_run(job_id, cancelled=cancelled, now=now())
