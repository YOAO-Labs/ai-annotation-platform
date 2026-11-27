"""Fixed scope previews, current authority and atomic review checkpoints."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from app.schemas.dataset_quality import DatasetQualityReview
from app.services import jobs_lifecycle_data as jobs, dataset_quality as quality
from app.services import dataset_source_access as access
from app.services.audit import AuditService
from app.services.dataset_content import key_authority
from app.services.dataset_quality_data import QualityData

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
    value = {key: row[key] for key in EVIDENCE_FIELDS}
    if value["observed_at"] is not None:
        observed = datetime.fromisoformat(value["observed_at"]).astimezone(timezone.utc)
        value["observed_at"] = str(observed)
    return value


async def list_batches(db, dataset_id, run_id, actor, *, limit, offset):
    await quality.require_run(db, dataset_id, run_id, actor)
    return await QualityData(db).batch_page(
        run_id, actor.id, limit=limit, offset=offset
    )


async def member_page(db, batch, actor, *, limit, offset):
    page = await QualityData(db).member_page(
        batch, actor.id, limit=limit, offset=offset
    )
    if page is None:
        raise HTTPException(404, "Review batch not found")
    return page


async def require_batch(db, dataset_id, run_id, batch_id, actor, *, lock=False):
    await quality.require_run(db, dataset_id, run_id, actor)
    batch = await QualityData(db).batch(run_id, batch_id, actor.id, lock=lock)
    if batch is None:
        raise HTTPException(404, "Review batch not found")
    return batch


async def preview(db, dataset_id, run_id, actor, payload):
    run = await quality.require_run(db, dataset_id, run_id, actor, lock=True)
    dataset = await quality.require_write(db, dataset_id)
    if run.status in ACTIVE:
        raise HTTPException(409, "Wait until quality detection has stopped")
    data = QualityData(db)
    if not await data.prepare_preview(run_id, actor.id, now()):
        raise HTTPException(409, "Too many unconfirmed previews; wait for expiry")
    filters = (
        payload.query.model_dump(exclude_none=True)
        if payload.scope == "filtered"
        else {}
    )
    rows = await data.preview_rows(
        run.id,
        actor.id,
        **filters,
        results=payload.result_ids if payload.scope == "selected" else None,
    )
    if payload.scope == "selected" and len(rows) != len(payload.result_ids):
        raise HTTPException(404, "Selected quality result not found")
    if not rows or len(rows) > quality.MAX_MEMBERS:
        raise HTTPException(422, "Review scopes require 1 to 10000 results")
    members = [
        {
            "result_id": row["id"],
            "snapshot": {
                **evidence(row),
                "duplicate_count": row["duplicate_count"],
                "historical": row["historical"],
            },
        }
        for row in rows
    ]
    selection = {"scope": payload.scope, "query": filters}
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
                "members": members,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    created_at = now()
    batch = await data.create_preview(
        run_id,
        actor.id,
        dataset.lifecycle_revision,
        selection,
        digest,
        payload.state,
        note,
        created_at,
        created_at + timedelta(minutes=10),
        members,
    )
    if batch is None:
        raise HTTPException(404, "Quality run not found")
    return batch


async def authority(db, batch):
    actor = await access.require_manager(db, uuid.UUID(batch.authority["actor_id"]))
    run = await quality.require_run(db, batch.dataset_id, batch.quality_run_id, actor)
    proof = await key_authority(
        db, actor, batch.authority["api_key_id"], "datasets:write"
    )
    if not await QualityData(db).batch_key_matches(batch, proof):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    return actor, run


async def attach_job(db, batch, run, actor, key_id):
    dataset = await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    proof = await key_authority(db, actor, key_id, "datasets:write")
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
    updated = await QualityData(db).attach_batch(
        batch,
        job.id,
        actor.id,
        key_id,
        proof,
        dataset.lifecycle_revision,
        now() + timedelta(seconds=LEASE_SECONDS),
    )
    if updated is None:
        raise HTTPException(409, "Review attempt changed")
    return updated


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
        return batch, False
    if batch.expires_at <= now():
        raise HTTPException(409, "Review preview expired; create a new preview")
    if not batch.actionable_count:
        raise HTTPException(409, "This scope has no reviewable results")
    run = await quality.require_run(db, dataset_id, run_id, actor)
    batch = await attach_job(db, batch, run, actor, key_id)
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
    batch = await attach_job(db, batch, run, actor, key_id)
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_quality_review.resume",
        target_type="dataset_quality_review",
        target_id=batch.id,
    )
    return batch


async def finish(db, batch, status, error=None, *, task_id=None):
    if status == "completed":
        await quality.require_write(db, batch.dataset_id, snapshot=batch.authority)
    updated = await QualityData(db).transition_batch(
        batch, status, now(), task_id=task_id, error=error
    )
    if updated and updated.async_job_id:
        await quality.finish_job(
            db,
            updated.async_job_id,
            status,
            error,
            {"batch_id": str(batch.id), **updated.summary},
        )
    return updated


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
    return await QualityData(db).batch_attempt(
        "worker",
        now(),
        batch_id=batch_id,
        job_id=job_id,
        task_id=task_id,
        generation=generation,
    )


async def process_member(db, batch, actor, run, task_id):
    data = QualityData(db)
    member = await data.next_member(batch.id)
    if member is None:
        return None
    row = await data.lock_result(run.id, member.result_id)
    await quality.require_write(db, run.dataset_id, snapshot=batch.authority)
    expected = {key: member.snapshot[key] for key in EVIDENCE_FIELDS}
    revision = None
    if (
        row is not None
        and evidence(row) == expected
        and batch.proof == {"rule_version": run.rule_version, "config": run.config}
    ):
        value = await quality.review_result(
            db,
            run,
            member.result_id,
            actor,
            DatasetQualityReview(
                state=batch.decision,
                note=batch.note,
                expected_revision=expected["review_revision"],
            ),
            batch_id=batch.id,
            locked=row,
        )
        revision = value["review_revision"]
    recorded_at = now()
    processed = await data.checkpoint(
        batch,
        task_id,
        member.id,
        revision,
        recorded_at,
        recorded_at + timedelta(seconds=LEASE_SECONDS),
    )
    return processed if processed is not None else False


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    batch = await QualityData(db).batch_attempt(
        "signal", now(), job_id=job_id, task_id=task_id
    )
    if batch is not None:
        await finish(
            db,
            batch,
            "cancelled" if cancelled else "failed",
            "cancelled" if cancelled else "worker_failed",
            task_id=task_id,
        )


async def reconcile_terminal(db, job_id, *, cancelled):
    await QualityData(db).reconcile_batch(job_id, cancelled=cancelled, now=now())
