"""Fixed scope previews, current authority and atomic review checkpoints."""

from datetime import timedelta
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, delete, func, or_, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_quality import (
    DatasetQualityRun as Run,
    DatasetQualityResult as Result,
)
from app.db.models.dataset_quality_review import (
    DatasetQualityReviewBatch as Batch,
    DatasetQualityReviewMember as Member,
)
from app.schemas.dataset_quality import DatasetQualityReview
from app.services import async_job as jobs, dataset_quality as quality
from app.services import dataset_source_access as access
from app.services.async_job_notify import notify_job_terminal
from app.services.audit import AuditService
from app.services.dataset_content import key_authority

ACTIVE = quality.ACTIVE
LEASE_SECONDS = quality.LEASE_SECONDS
now = quality.now
failure_code = quality.failure_code
EVIDENCE_FIELDS = (
    "item_id",
    "asset_id",
    "source_id",
    "file_name",
    "sha256",
    "size_bytes",
    "state",
    "reason_code",
    "bytes_verified",
    "findings",
    "width",
    "height",
    "observed_at",
    "review_revision",
)


def evidence(row):
    # JSON conversion is stable for UUIDs/timestamps and never includes locators.
    return json.loads(
        json.dumps({k: getattr(row, k) for k in EVIDENCE_FIELDS}, default=str)
    )


def visible_clause(actor):
    return or_(Batch.status != "preview", Batch.created_by == actor.id)


def job_scope_clause(actor):
    return and_(
        AsyncJob.kind == "dataset_quality_review",
        select(1)
        .select_from(Batch)
        .join(Run, Run.id == Batch.quality_run_id)
        .where(
            quality.scope_clause(actor),
            cast(Batch.id, String) == AsyncJob.payload["batch_id"].astext,
        )
        .exists(),
    )


async def require_batch(db, dataset_id, run_id, batch_id, actor, *, lock=False):
    await quality.require_run(db, dataset_id, run_id, actor)
    stmt = select(Batch).where(
        Batch.id == batch_id, Batch.quality_run_id == run_id, visible_clause(actor)
    )
    if lock:
        stmt = stmt.with_for_update()
    batch = await db.scalar(stmt.execution_options(populate_existing=True))
    if batch is None:
        raise HTTPException(404, "Review batch not found")
    return batch


async def preview(db, dataset_id, run_id, actor, payload):
    run = await quality.require_run(db, dataset_id, run_id, actor, lock=True)
    dataset = await quality.require_write(db, dataset_id)
    if run.status in ACTIVE:
        raise HTTPException(409, "Wait until quality detection has stopped")
    await db.execute(
        delete(Batch).where(
            Batch.quality_run_id == run_id,
            Batch.created_by == actor.id,
            Batch.status == "preview",
            Batch.expires_at <= now(),
        )
    )
    count = await db.scalar(
        select(func.count())
        .select_from(Batch)
        .where(
            Batch.quality_run_id == run_id,
            Batch.created_by == actor.id,
            Batch.status == "preview",
        )
    )
    if count >= 20:
        raise HTTPException(409, "Too many unconfirmed previews; wait for expiry")
    filters = payload.query.model_dump(exclude_none=True)
    stmt = quality.result_query(
        run.id, **(filters if payload.scope == "filtered" else {})
    )
    if payload.scope == "selected":
        stmt = stmt.where(Result.id.in_(payload.result_ids))
    rows = (
        await db.execute(
            stmt.order_by(Result.id)
            .limit(quality.MAX_MEMBERS + 1)
            .execution_options(populate_existing=True)
        )
    ).all()
    if payload.scope == "selected" and len(rows) != len(payload.result_ids):
        raise HTTPException(404, "Selected quality result not found")
    if not rows or len(rows) > quality.MAX_MEMBERS:
        raise HTTPException(422, "Review scopes require 1 to 10000 results")
    members, historical = [], 0
    for row, duplicates, item_id, asset_id, sha in rows:
        stale = item_id is None or asset_id != row.asset_id or sha != row.sha256
        historical += int(stale)
        members.append(
            Member(
                result_id=row.id,
                snapshot={
                    **evidence(row),
                    "duplicate_count": duplicates,
                    "historical": stale,
                },
                state="blocked" if row.state == "pending" else "pending",
                reason_code="result_pending" if row.state == "pending" else None,
                processed_at=now() if row.state == "pending" else None,
            )
        )
    selection = {
        "scope": payload.scope,
        "query": filters if payload.scope == "filtered" else {},
    }
    proof = {"rule_version": run.rule_version, "config": run.config}
    note = payload.note.strip()
    digest = hashlib.sha256(
        json.dumps(
            {
                "run_id": str(run_id),
                "selection": selection,
                "proof": proof,
                "decision": payload.state,
                "note": note,
                "members": [
                    {"result_id": str(m.result_id), "snapshot": m.snapshot}
                    for m in members
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    blocked = sum(m.state == "blocked" for m in members)
    batch = Batch(
        quality_run_id=run_id,
        authority={"dataset_lifecycle_revision": dataset.lifecycle_revision},
        created_by=actor.id,
        selection=selection,
        proof=proof,
        scope_digest=digest,
        decision=payload.state,
        note=note,
        total=len(members),
        actionable_count=len(members) - blocked,
        historical_count=historical,
        processed=blocked,
        summary={
            "applied_count": 0,
            "conflict_count": 0,
            "blocked_count": blocked,
            "processed_count": blocked,
            "partial": bool(blocked),
        },
        expires_at=now() + timedelta(minutes=10),
    )
    db.add(batch)
    await db.flush()
    for member in members:
        member.batch_id = batch.id
        db.add(member)
    await db.flush()
    return batch


async def authority(db, batch):
    actor = await access.require_manager(db, uuid.UUID(batch.authority["actor_id"]))
    run = await db.get(Run, batch.quality_run_id, populate_existing=True)
    if run is None:
        raise HTTPException(404, "Quality run not found")
    await quality.require_run(db, run.dataset_id, run.id, actor)
    if (
        await key_authority(db, actor, batch.authority["api_key_id"], "datasets:write")
        != batch.authority["key_proof"]
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    return actor, run


async def attach_job(db, batch, run, actor, key_id):
    dataset = await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    batch.authority = {
        "dataset_lifecycle_revision": dataset.lifecycle_revision,
        "actor_id": str(actor.id),
        "api_key_id": str(key_id) if key_id else None,
        "key_proof": await key_authority(db, actor, key_id, "datasets:write"),
    }
    job = await jobs.create_job(
        db,
        kind="dataset_quality_review",
        user_id=actor.id,
        payload={
            "dataset_id": str(run.dataset_id),
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "run_id": str(run.id),
            "batch_id": str(batch.id),
            "decision": batch.decision,
            "total": batch.total,
        },
        celery_task_id=str(uuid.uuid4()),
    )
    batch.async_job_id = job.id
    batch.status, batch.error_code, batch.completed_at = "pending", None, None
    batch.generation += 1
    batch.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
    await db.flush()


async def execute_preview(
    db, dataset_id, run_id, batch_id, actor, payload, key_id=None
):
    batch = await require_batch(db, dataset_id, run_id, batch_id, actor, lock=True)
    await quality.require_write(db, dataset_id, snapshot=batch.authority)
    if batch.created_by != actor.id:
        raise HTTPException(403, "Only the preview creator can confirm this scope")
    if batch.scope_digest != payload.scope_digest:
        raise HTTPException(409, "Review scope digest does not match")
    if batch.status != "preview":
        return batch, False  # A repeated confirmation never creates another attempt.
    if batch.expires_at <= now():
        raise HTTPException(409, "Review preview expired; create a new preview")
    if not batch.actionable_count:
        raise HTTPException(409, "This scope has no reviewable results")
    run = await quality.require_run(db, dataset_id, run_id, actor)
    await attach_job(db, batch, run, actor, key_id)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality_review.execute",
        target_type="dataset_quality_review",
        target_id=batch.id,
        detail={
            "run_id": str(run_id),
            "scope_digest": batch.scope_digest,
            "total": batch.total,
            "decision": batch.decision,
        },
    )
    return batch, True


async def resume(db, dataset_id, run_id, batch_id, actor, key_id=None):
    batch = await require_batch(db, dataset_id, run_id, batch_id, actor, lock=True)
    await quality.require_write(db, dataset_id, snapshot=batch.authority)
    if batch.status not in {"failed", "cancelled"} or batch.processed >= batch.total:
        raise HTTPException(
            409, "Only interrupted batches with pending members can resume"
        )
    run = await quality.require_run(db, dataset_id, run_id, actor)
    await attach_job(db, batch, run, actor, key_id)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality_review.resume",
        target_type="dataset_quality_review",
        target_id=batch.id,
    )
    return batch


async def summarize(db, batch):
    counts = dict(
        (
            await db.execute(
                select(Member.state, func.count())
                .where(Member.batch_id == batch.id)
                .group_by(Member.state)
            )
        ).all()
    )
    batch.processed = sum(n for state, n in counts.items() if state != "pending")
    batch.summary = {
        "applied_count": counts.get("applied", 0),
        "conflict_count": counts.get("conflict", 0),
        "blocked_count": counts.get("blocked", 0),
        "processed_count": batch.processed,
        "partial": bool(counts.get("conflict", 0) or counts.get("blocked", 0)),
    }


async def finish(db, batch, status, error=None):
    if status == "completed":
        run = await db.get(Run, batch.quality_run_id, populate_existing=True)
        if run is None:
            raise HTTPException(404, "Quality run not found")
        await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    await summarize(db, batch)
    batch.status = (
        "partial" if status == "completed" and batch.summary["partial"] else status
    )
    batch.error_code, batch.completed_at, batch.lease_until = error, now(), None
    batch.generation += 1
    if batch.async_job_id:
        result = {"batch_id": str(batch.id), **batch.summary}
        if status == "completed":
            await jobs.mark_complete(db, batch.async_job_id, result=result)
        elif status == "cancelled":
            await jobs.mark_cancelled(db, batch.async_job_id, result=result)
        else:
            await jobs.mark_failed(
                db, batch.async_job_id, error=error or "worker_failed", result=result
            )
        await notify_job_terminal(db, job_id=batch.async_job_id)


async def cancel_job(db, job, actor):
    batch = await require_batch(
        db,
        uuid.UUID(job.payload["dataset_id"]),
        uuid.UUID(job.payload["run_id"]),
        uuid.UUID(job.payload["batch_id"]),
        actor,
        lock=True,
    )
    if batch.async_job_id != job.id or batch.status not in ACTIVE:
        raise HTTPException(409, "Review attempt has ended")
    await finish(db, batch, "cancelled", "cancelled")


async def fenced(db, batch_id, job_id, task_id, generation):
    batch = await db.scalar(
        select(Batch)
        .where(Batch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        batch is None
        or job is None
        or batch.async_job_id != job_id
        or job.celery_task_id != task_id
        or batch.generation != generation
        or batch.status not in ACTIVE
        or job.status not in ACTIVE
        or not batch.lease_until
        or batch.lease_until <= now()
    ):
        return None
    return batch


async def process_member(db, batch, actor, run):
    member = await db.scalar(
        select(Member)
        .where(Member.batch_id == batch.id, Member.state == "pending")
        .order_by(Member.id)
        .limit(1)
    )
    if member is None:
        return False
    row = await db.scalar(
        select(Result)
        .where(Result.id == member.result_id, Result.run_id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    expected = {k: member.snapshot[k] for k in EVIDENCE_FIELDS}
    if (
        row is None
        or evidence(row) != expected
        or batch.proof != {"rule_version": run.rule_version, "config": run.config}
    ):
        member.state, member.reason_code = "conflict", "evidence_or_review_changed"
    else:
        value = await quality.review_result(
            db,
            run,
            row.id,
            actor,
            DatasetQualityReview(
                state=batch.decision,
                note=batch.note,
                expected_revision=member.snapshot["review_revision"],
            ),
            batch_id=batch.id,
        )
        member.state = "applied"
        member.applied_revision = value["review_revision"]
    member.processed_at = now()
    # Decision, audit and checkpoint are committed together by the worker.
    await db.flush()
    await summarize(db, batch)
    return True


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    batch = await db.scalar(
        select(Batch)
        .where(Batch.async_job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        batch is None
        or job is None
        or job.celery_task_id != task_id
        or batch.status not in ACTIVE
        or job.status not in ACTIVE
    ):
        return
    await finish(
        db,
        batch,
        "cancelled" if cancelled else "failed",
        "cancelled" if cancelled else "worker_failed",
    )
