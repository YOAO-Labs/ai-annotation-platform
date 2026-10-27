"""Fixed membership, current authority, fenced checkpoints and human decisions."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, false, func, select

from app.db.enums import UserRole
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_source import DatasetReference, DatasetSource
from app.db.models.dataset_quality import (
    DatasetQualityRun as Run,
    DatasetQualityResult as Result,
)
from app.services import async_job as jobs
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services import dataset_source_access as access
from app.services.async_job_notify import notify_job_terminal
from app.services.audit import AuditService
from app.services.dataset_content import key_authority
from app.schemas.dataset_quality import DatasetQualityResultOut

ACTIVE = {"pending", "running"}
LEASE_SECONDS = 120
MAX_MEMBERS = 10000


def now():
    return datetime.now(timezone.utc)


def scope_clause(actor):
    if not actor.is_active or actor.role not in access.MANAGERS:
        return false()
    visible_sources = select(DatasetSource.id).where(access.source_scope_clause(actor))
    hidden = (
        select(1)
        .where(
            Result.run_id == Run.id,
            Result.source_id.is_not(None),
            Result.source_id.not_in(visible_sources),
        )
        .exists()
    )
    return and_(
        Run.dataset_id.in_(
            select(Dataset.id).where(
                True
                if actor.role == UserRole.SUPER_ADMIN
                else Dataset.created_by == actor.id
            )
        ),
        ~hidden,
    )


def job_scope_clause(actor):
    return and_(
        AsyncJob.kind == "dataset_quality",
        select(1)
        .where(
            scope_clause(actor),
            cast(Run.id, String) == AsyncJob.payload["run_id"].astext,
        )
        .exists(),
    )


async def require_run(db, dataset_id, run_id, actor, *, lock=False):
    actor = await access.assert_dataset_manage(db, dataset_id, actor)
    stmt = select(Run).where(
        Run.id == run_id, Run.dataset_id == dataset_id, scope_clause(actor)
    )
    if lock:
        stmt = stmt.with_for_update()
    run = await db.scalar(stmt.execution_options(populate_existing=True))
    if run is None:
        raise HTTPException(404, "Quality run not found")
    return run


async def authority(db, run):
    actor = await access.require_manager(db, uuid.UUID(run.authority["actor_id"]))
    await require_run(db, run.dataset_id, run.id, actor)
    if (
        await key_authority(db, actor, run.authority["api_key_id"], "datasets:write")
        != run.authority["key_proof"]
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await require_write(db, run.dataset_id, snapshot=run.authority)
    await maintenance_authority(db, run, lock=True)
    return actor


async def maintenance_authority(db, run, *, lock=False):
    location_ids = set(
        await db.scalars(
            select(DatasetSource.location_id)
            .join(Result, Result.source_id == DatasetSource.id)
            .where(Result.run_id == run.id)
            .distinct()
        )
    )
    captured = run.authority.get("storage_maintenance", {})
    locations = sorted(location_ids | {uuid.UUID(key) for key in captured}, key=str)
    if lock and locations:
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id.in_(locations))
            .order_by(StorageLocation.id)
            .with_for_update(read=True, nowait=True)
        )
    # Old queued runs belong to epoch 1, not whatever epoch is current on retry.
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


async def attach_job(db, run, actor, key_id):
    dataset = await require_write(db, run.dataset_id, snapshot=run.authority)
    await maintenance_authority(db, run, lock=True)
    run.authority = dict(
        storage_maintenance=run.authority.get("storage_maintenance", {}),
        dataset_lifecycle_revision=dataset.lifecycle_revision,
        actor_id=str(actor.id),
        api_key_id=str(key_id) if key_id else None,
        key_proof=await key_authority(db, actor, key_id, "datasets:write"),
    )
    task_id = str(uuid.uuid4())
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
        celery_task_id=task_id,
    )
    run.async_job_id = job.id
    run.status, run.error_code, run.completed_at = "pending", None, None
    run.generation += 1
    run.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
    await db.flush()
    return task_id


async def start(db, dataset_id, actor, config, key_id=None):
    actor = await access.assert_dataset_manage(db, dataset_id, actor)
    dataset = await require_write(db, dataset_id)
    if await db.scalar(
        select(Run.id).where(Run.dataset_id == dataset_id, Run.status.in_(ACTIVE))
    ):
        raise HTTPException(409, "A quality run is already active")
    members = (
        await db.execute(
            select(DatasetItem, DatasetAsset, DatasetReference.source_id)
            .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
            .outerjoin(
                DatasetReference, DatasetReference.dataset_item_id == DatasetItem.id
            )
            .where(DatasetItem.dataset_id == dataset_id)
            .order_by(DatasetItem.id)
            .limit(MAX_MEMBERS + 1)
        )
    ).all()
    if len(members) > MAX_MEMBERS:
        raise HTTPException(422, "Quality runs support at most 10000 fixed members")
    maintenance = {}
    for source_id in sorted({s for _, _, s in members if s}, key=str):
        source = await access.require_source(db, source_id, actor)
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == source.location_id)
            .with_for_update(read=True, nowait=True)
        )
        maintenance[str(source.location_id)] = await storage_access.check(
            db,
            source.location_id,
            lock=True,
        )
    run = Run(
        dataset_id=dataset_id,
        authority={
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "storage_maintenance": maintenance,
        },
        config=config.model_dump(),
        total=len(members),
        processed=0,
        generation=0,
        summary={},
        rule_version="image-quality-1",
    )
    db.add(run)
    await db.flush()
    for item, asset, source_id in members:
        trusted = asset is not None and asset.verification_state == "verified"
        db.add(
            Result(
                run_id=run.id,
                item_id=item.id,
                file_name=item.file_name,
                source_id=source_id,
                asset_id=asset.id if asset else None,
                sha256=asset.checksum_value if trusted else None,
                size_bytes=asset.size_bytes if trusted else None,
                reason_code="media_not_supported"
                if item.file_type != "image"
                else "content_unverified"
                if not trusted
                else None,
            )
        )
    await attach_job(db, run, actor, key_id)
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
    if await db.scalar(
        select(Run.id).where(
            Run.dataset_id == dataset_id, Run.id != run.id, Run.status.in_(ACTIVE)
        )
    ):
        raise HTTPException(409, "Another quality run is active")
    if run.status in ACTIVE and run.async_job_id:
        await jobs.mark_failed(db, run.async_job_id, error="lease_expired")
        await notify_job_terminal(db, job_id=run.async_job_id)
    await attach_job(db, run, actor, key_id)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality.resume",
        target_type="dataset_quality",
        target_id=run.id,
    )
    return run


async def finish(db, run, status, error=None):
    if status == "completed":
        await require_write(db, run.dataset_id, snapshot=run.authority)
        await maintenance_authority(db, run, lock=True)
    run.status, run.error_code, run.completed_at, run.lease_until = (
        status,
        error,
        now(),
        None,
    )
    run.generation += 1
    run.summary = await summary(db, run.id)
    if run.async_job_id:
        result = {"run_id": str(run.id), **run.summary}
        if status == "completed":
            await jobs.mark_complete(db, run.async_job_id, result=result)
        elif status == "cancelled":
            await jobs.mark_cancelled(db, run.async_job_id, result=result)
        else:
            await jobs.mark_failed(
                db, run.async_job_id, error=error or "worker_failed", result=result
            )
        await notify_job_terminal(db, job_id=run.async_job_id)


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


def duplicate_groups(run_id):
    return (
        select(
            Result.sha256.label("sha"),
            Result.size_bytes.label("size"),
            func.count().label("n"),
        )
        .where(Result.run_id == run_id, Result.bytes_verified.is_(True))
        .group_by(Result.sha256, Result.size_bytes)
        .having(func.count() > 1)
        .subquery()
    )


async def summary(db, run_id):
    counts = dict(
        (
            await db.execute(
                select(Result.state, func.count())
                .where(Result.run_id == run_id)
                .group_by(Result.state)
            )
        ).all()
    )
    groups = duplicate_groups(run_id)
    duplicate_count, members = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(groups.c.n), 0)).select_from(
                groups
            )
        )
    ).one()
    issues = await db.scalar(
        select(func.count())
        .select_from(Result)
        .where(Result.run_id == run_id, Result.findings != [])
    )
    return {
        "processed_count": counts.get("checked", 0) + counts.get("skipped", 0),
        "checked_count": counts.get("checked", 0),
        "skipped_count": counts.get("skipped", 0),
        "issue_count": issues,
        "duplicate_group_count": duplicate_count,
        "duplicate_member_count": int(members),
    }


def result_query(run_id, *, finding=None, review=None, sha256=None):
    """One filter contract for browsing and materializing a review scope."""
    groups = duplicate_groups(run_id)
    stmt = (
        select(
            Result,
            func.coalesce(groups.c.n, 0),
            DatasetItem.id,
            DatasetAsset.id,
            DatasetAsset.checksum_value,
        )
        .outerjoin(
            groups,
            and_(
                Result.bytes_verified.is_(True),
                Result.sha256 == groups.c.sha,
                Result.size_bytes == groups.c.size,
            ),
        )
        .outerjoin(DatasetItem, DatasetItem.id == Result.item_id)
        .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .where(Result.run_id == run_id)
    )
    if finding == "exact_duplicate":
        stmt = stmt.where(groups.c.n > 1)
    elif finding == "skipped":
        stmt = stmt.where(Result.state == "skipped")
    elif finding:
        stmt = stmt.where(Result.findings.contains([finding]))
    if review:
        stmt = stmt.where(Result.review_state == review)
    if sha256:
        stmt = stmt.where(Result.bytes_verified.is_(True), Result.sha256 == sha256)
    return stmt


async def result_page(
    db, run, *, limit, offset, finding=None, review=None, sha256=None
):
    stmt = result_query(run.id, finding=finding, review=review, sha256=sha256)
    total = await db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    values = []
    for row, count, item_id, asset_id, sha in (
        await db.execute(
            stmt.order_by(Result.file_name, Result.id).limit(limit).offset(offset)
        )
    ).all():
        value = {
            name: getattr(row, name)
            for name in DatasetQualityResultOut.model_fields
            if name not in {"duplicate_count", "stale"}
        }
        values.append(
            DatasetQualityResultOut(
                **value,
                duplicate_count=count,
                stale=item_id is None or asset_id != row.asset_id or sha != row.sha256,
            )
        )
    return {"items": values, "total": total}


async def review_result(db, run, result_id, actor, payload, *, batch_id=None):
    row = await db.scalar(
        select(Result)
        .where(Result.run_id == run.id, Result.id == result_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "Quality result not found")
    await require_write(db, run.dataset_id)
    if row.state == "pending":
        raise HTTPException(409, "Result is not available yet")
    if row.review_revision != payload.expected_revision:
        raise HTTPException(409, "Review changed; reload the result before deciding")
    if not payload.note.strip():
        raise HTTPException(422, "A review reason is required")
    old_state = row.review_state
    row.review_state, row.review_note = payload.state, payload.note.strip()
    row.review_revision += 1
    row.reviewed_by, row.reviewed_at = actor.id, now()
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality.review",
        target_type="dataset_quality_result",
        target_id=row.id,
        detail={
            "run_id": str(run.id),
            "item_id": str(row.item_id),
            "asset_id": str(row.asset_id) if row.asset_id else None,
            "sha256": row.sha256,
            "rule_version": run.rule_version,
            "config": run.config,
            "before": old_state,
            "state": row.review_state,
            "note": row.review_note,
            "revision": row.review_revision,
            **({"batch_id": str(batch_id)} if batch_id else {}),
        },
    )
    return {"review_revision": row.review_revision}


async def fenced(db, run_id, job_id, task_id, generation):
    run = await db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        run is None
        or job is None
        or run.async_job_id != job_id
        or job.celery_task_id != task_id
        or run.generation != generation
        or run.status not in ACTIVE
        or job.status not in ACTIVE
        or not run.lease_until
        or run.lease_until <= now()
    ):
        return None
    return run


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    run = await db.scalar(
        select(Run)
        .where(Run.async_job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        run is None
        or job is None
        or job.celery_task_id != task_id
        or run.status not in ACTIVE
        or job.status not in ACTIVE
    ):
        return
    await finish(
        db,
        run,
        "cancelled" if cancelled else "failed",
        "cancelled" if cancelled else "worker_failed",
    )
