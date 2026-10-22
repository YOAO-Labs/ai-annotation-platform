"""Fenced two-stage directory registration with durable, per-file checkpoints."""

from datetime import timedelta
import asyncio
import uuid
import time

from fastapi import HTTPException
from sqlalchemy import BigInteger, cast, func, insert, literal, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetRegistrationEntry as Entry,
    DatasetRegistrationRun as Run,
    DatasetScanEntry,
)
from app.services import async_job as jobs
from app.services import dataset_scans as scans
from app.services import storage_location as locations
from app.services import filesystem_storage as fs
from app.services.async_job_notify import notify_job_terminal
from app.services.dataset_content import key_authority

KIND = "dataset_registration"
LEASE_SECONDS = 60
ACTIVE = {"pending", "running"}
PATH_ERRORS = {
    "filesystem_unavailable",
    "path_unavailable",
    "not_a_directory",
    "permission_denied",
    "symlink_rejected",
    "path_boundary_rejected",
    "path_changed",
    "not_a_regular_file",
}
FILE_ERRORS = {
    "invalid_image",
    "image_format_unsupported",
    "image_pixel_budget_exceeded",
    "content_too_large",
    "source_changed",
    "content_mismatch",
} | PATH_ERRORS


class LostRegistrationLease(Exception):
    pass


async def context(db, run_id):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Registration not found")
    location, source = await scans.source_context(db, run.source_id)
    job_id = await db.scalar(select(Run.async_job_id).where(Run.id == run_id))
    job = (
        await db.scalar(
            select(AsyncJob)
            .where(AsyncJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job_id
        else None
    )
    run = await db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return location, source, run, job


async def validate(db, location, source, run):
    await scans.validate_context(db, location, source, snapshot=run.snapshot)
    actor = await scans.require_actor(db, uuid.UUID(run.snapshot["actor_id"]), source)
    proof = await key_authority(
        db, actor, run.snapshot.get("api_key_id"), "datasets:write"
    )
    if proof != run.snapshot.get("key_proof"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor


async def request_authority(db, actor, api_key_id, proof, source=None):
    await scans.require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})


async def attempt(db, run, source, actor, *, api_key_id=None, key_proof):
    await request_authority(db, actor, api_key_id, key_proof, source)
    run.snapshot = {
        **run.snapshot,
        "actor_id": str(actor.id),
        "api_key_id": str(api_key_id) if api_key_id else None,
        "key_proof": key_proof,
    }
    job = await jobs.create_job(
        db,
        kind=KIND,
        user_id=actor.id,
        celery_task_id=str(uuid.uuid4()),
        payload={
            "registration_id": str(run.id),
            "source_id": str(source.id),
            "dataset_id": str(source.dataset_id),
            "stage": run.phase,
        },
    )
    run.async_job_id = job.id
    run.status = "pending"
    run.error_code = None
    run.completed_at = None
    run.lease_token = None
    run.lease_expires_at = await scans.now(db)
    job.result = dict(run.counts)
    return job


async def start_preflight(db, scan_id, actor, *, api_key_id=None):
    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source, scan, _ = await scans.run_context(db, scan_id)
    await request_authority(db, actor, api_key_id, key_proof, source)
    await scans.validate_context(db, location, source, snapshot=scan.snapshot)
    if not scan.complete or scan.status != "completed":
        raise HTTPException(409, detail={"code": "scan_incomplete"})
    existing = await db.scalar(select(Run).where(Run.scan_run_id == scan_id))
    if existing:
        return existing, False
    if await db.scalar(
        select(Run.id).where(Run.source_id == source.id, Run.status.in_(ACTIVE))
    ):
        raise HTTPException(409, "Another registration is active for this source")
    await locations.require_scan_node(
        db, location, f"worker:{source.config['node_id']}"
    )
    total, size = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        cast(DatasetScanEntry.observed["size"].astext, BigInteger)
                    ),
                    0,
                ),
            ).where(
                DatasetScanEntry.run_id == scan_id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            )
        )
    ).one()
    result = await probe_candidates(
        db, location, source, scan.id, locations.api_node_id()
    )
    await request_authority(db, actor, api_key_id, key_proof, source)
    await locations.store_probe(
        db, location, locations.api_node_id(), result, await scans.now(db)
    )
    if not result["ok"]:
        raise HTTPException(
            409, detail={"code": "api_source_unavailable", "reason": result["code"]}
        )
    run = Run(
        scan_run_id=scan.id,
        source_id=source.id,
        snapshot={
            "source_revision": source.config_revision,
            "location_revision": location.config_revision,
            "node_id": source.config["node_id"],
            "ownership_mode": "reference",
            "cache_policy": "none",
        },
        generation=0,
        phase="preflight",
        counts={
            "indexed_count": total,
            "indexed_bytes": int(size),
            "pending_count": total,
            "ready_count": 0,
            "registered_count": 0,
            "unchanged_count": 0,
            "error_count": 0,
            "existing_count": 0,
            "skipped_count": scan.counts.get("skipped_count", 0),
            "original_copy_bytes": 0,
            "preview_estimated_bytes": 0,
        },
    )
    db.add(run)
    await db.flush()
    await db.execute(
        insert(Entry).from_select(
            [
                "run_id",
                "path_digest",
                "state",
                "item_id",
                "attempts",
                "refresh_unverified",
            ],
            select(
                literal(run.id),
                DatasetScanEntry.path_digest,
                literal("pending"),
                DatasetReference.dataset_item_id,
                literal(0),
                literal(False),
            )
            .outerjoin(
                DatasetReference,
                (DatasetReference.source_id == source.id)
                & (DatasetReference.path_digest == DatasetScanEntry.path_digest),
            )
            .where(
                DatasetScanEntry.run_id == scan_id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            ),
        )
    )
    existing_count = await db.scalar(
        select(func.count())
        .select_from(Entry)
        .where(Entry.run_id == run.id, Entry.item_id.is_not(None))
    )
    run.counts = {
        **run.counts,
        "existing_count": existing_count,
        "estimated_new_count": total - existing_count,
    }
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run, True


async def finish(db, run, job, status, code=None):
    run.status, run.error_code = status, code
    run.lease_token = None
    run.lease_expires_at = None
    run.completed_at = await scans.now(db)
    if job is not None:
        job.status = status
        job.progress_pct = 100 if status == "completed" else job.progress_pct
        job.error_message = code
        job.completed_at = run.completed_at
        job.result = {
            **run.counts,
            "registration_id": str(run.id),
            "phase": run.phase,
            "error_code": code,
        }
        await notify_job_terminal(db, job_id=job.id)


async def fenced(db, run_id, task_id, fence):
    location, source, run, job = await context(db, run_id)
    if (
        not job
        or job.celery_task_id != task_id
        or run.status != "running"
        or job.status != "running"
        or (run.generation, run.lease_token) != fence
        or not run.lease_expires_at
        or run.lease_expires_at <= await scans.now(db)
    ):
        raise LostRegistrationLease()
    await validate(db, location, source, run)
    return location, source, run, job


async def claim(db, run_id, task_id, node_id):
    location, source, run, job = await context(db, run_id)
    if (
        not job
        or job.celery_task_id != task_id
        or run.status != "pending"
        or job.status != "pending"
    ):
        return None
    await validate(db, location, source, run)
    if node_id != run.snapshot["node_id"]:
        raise fs.FilesystemStorageError("wrong_execution_node")
    node = f"worker:{node_id}"
    result = await probe_candidates(db, location, source, run.scan_run_id, node)
    await validate(db, location, source, run)
    await locations.store_probe(db, location, node, result, await scans.now(db))
    if not result["ok"]:
        raise fs.FilesystemStorageError("worker_source_unavailable")
    run.generation += 1
    run.lease_token = uuid.uuid4()
    run.lease_expires_at = await scans.now(db) + timedelta(seconds=LEASE_SECONDS)
    run.status = job.status = "running"
    run.error_code = None
    job.started_at = job.started_at or await scans.now(db)
    return run.generation, run.lease_token


def transition(run, entry, state, *, error_code=None, item_id=None):
    counts = dict(run.counts)
    old = f"{entry.state}_count"
    new = f"{state}_count"
    counts[old] -= 1
    counts[new] += 1
    run.counts = counts
    entry.state, entry.error_code = state, error_code
    if item_id is not None:
        entry.item_id = item_id
    entry.attempts += 1


async def next_entry(db, run_id, task_id, fence):
    _, _, run, job = await fenced(db, run_id, task_id, fence)
    state = "pending" if run.phase == "preflight" else "ready"
    entry = await db.scalar(
        select(Entry)
        .where(Entry.run_id == run.id, Entry.state == state)
        .order_by(Entry.path_digest)
        .limit(1)
    )
    if entry is None:
        await finish(db, run, job, "completed")
        return None
    run.lease_expires_at = await scans.now(db) + timedelta(seconds=LEASE_SECONDS)
    return {
        "path_digest": entry.path_digest,
        "scan_run_id": run.scan_run_id,
        "phase": run.phase,
        "refresh_unverified": entry.refresh_unverified,
        "actor_id": uuid.UUID(run.snapshot["actor_id"]),
        "api_key_id": run.snapshot.get("api_key_id"),
        "node_id": f"worker:{run.snapshot['node_id']}",
    }


async def record(
    db, run_id, task_id, fence, path_digest, state, *, error_code=None, result=None
):
    _, _, run, job = await fenced(db, run_id, task_id, fence)
    entry = await db.get(Entry, (run.id, path_digest), populate_existing=True)
    expected = "pending" if run.phase == "preflight" else "ready"
    if entry is None or entry.state != expected:
        raise LostRegistrationLease()
    transition(
        run,
        entry,
        state,
        error_code=error_code,
        item_id=result["item_id"] if result else None,
    )
    run.lease_expires_at = await scans.now(db) + timedelta(seconds=LEASE_SECONDS)
    job.result = dict(run.counts)
    total = run.counts["indexed_count"]
    remaining = (
        run.counts["pending_count"]
        if run.phase == "preflight"
        else run.counts["ready_count"]
    )
    job.progress_pct = int(100 * (total - remaining) / total) if total else 100


async def other_active(db, run):
    if await db.scalar(
        select(Run.id).where(
            Run.source_id == run.source_id, Run.id != run.id, Run.status.in_(ACTIVE)
        )
    ):
        raise HTTPException(409, "Another registration is active for this source")


async def probe_candidates(db, location, source, scan_id, node):
    root = await locations.perform_probe(db, location, node, None)
    if not root["ok"]:
        return root
    paths = list(
        await db.scalars(
            select(DatasetScanEntry.relative_path)
            .where(
                DatasetScanEntry.run_id == scan_id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            )
            .order_by(DatasetScanEntry.path_digest)
            .limit(8)
        )
    )
    for path in paths:
        probe = await locations.perform_probe(
            db,
            location,
            node,
            "/".join(p for p in [source.config["relative_path"], path] if p),
        )
        if probe["ok"]:
            return probe
    return root


def check_source_directory(config, path, anchor):
    with fs.location_directory(config, path, expected_anchor=anchor):
        return


async def ensure_source_available(db, location, source, node):
    config, relative = dict(location.config), source.config["relative_path"]
    anchor = await locations.node_anchor(db, location, node)
    await db.commit()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(check_source_directory, config, relative, anchor),
            timeout=3,
        )
    except (TimeoutError, fs.FilesystemStorageError):
        raise fs.FilesystemStorageError("source_unavailable") from None


async def confirm(db, run_id, actor, *, api_key_id=None):
    from app.services.dataset_content import scan_snapshot
    from app.services.content_snapshot import ContentError

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    preflight_version = None
    source = None

    async def authorize_request(current_db):
        await request_authority(current_db, actor, api_key_id, key_proof, source)

    async def authorize(current_db):
        await authorize_request(current_db)
        if preflight_version is not None:
            _, _, current, _ = await context(current_db, run_id)
            if (
                (current.async_job_id, current.generation) != preflight_version
                or current.phase != "preflight"
                or current.status != "completed"
            ):
                raise HTTPException(409, "Preflight changed; refresh before confirming")

    deadline = time.monotonic() + 10
    for _ in range(32):
        location, source, run, _ = await context(db, run_id)
        await authorize(db)
        await scans.validate_context(db, location, source, snapshot=run.snapshot)
        await other_active(db, run)
        if run.phase == "registering":
            return run, False
        if run.status != "completed":
            raise HTTPException(
                409, "Complete the preflight before confirming registration"
            )
        if preflight_version is None:
            preflight_version = (run.async_job_id, run.generation)
        sample = await db.scalar(
            select(Entry)
            .where(Entry.run_id == run.id, Entry.state == "ready")
            .order_by(Entry.path_digest)
            .limit(1)
        )
        if sample is None:
            if run.counts["error_count"]:
                raise HTTPException(409, detail={"code": "no_ready_images"})
            break
        observed = await db.get(DatasetScanEntry, (run.scan_run_id, sample.path_digest))
        scan_id, digest, sha = (
            run.scan_run_id,
            sample.path_digest,
            observed.verified_content["sha256"],
        )
        proof = await locations.perform_probe(
            db, location, locations.api_node_id(), None
        )
        await authorize(db)
        await locations.store_probe(
            db, location, locations.api_node_id(), proof, await scans.now(db)
        )
        if not proof["ok"]:
            raise HTTPException(
                409, detail={"code": "api_source_unavailable", "reason": proof["code"]}
            )
        await db.commit()
        try:
            snapshot = await scan_snapshot(
                db,
                scan_id,
                digest,
                actor,
                expected_sha256=sha,
                api_key_id=api_key_id,
                verify=True,
                before_commit=authorize_request,
                node_id=locations.api_node_id(),
            )
            snapshot.close()
        except (ContentError, fs.FilesystemStorageError) as exc:
            await db.rollback()
            if exc.code not in FILE_ERRORS:
                raise
            location, source, run, _ = await context(db, run_id)
            await authorize(db)
            if exc.code in PATH_ERRORS:
                await ensure_source_available(
                    db, location, source, locations.api_node_id()
                )
                _, _, run, _ = await context(db, run_id)
                await authorize(db)
            if run.phase != "preflight" or run.status != "completed":
                raise HTTPException(
                    409, "Preflight changed; refresh before confirming"
                ) from None
            sample = await db.get(Entry, (run.id, digest), populate_existing=True)
            if sample.state == "ready":
                transition(run, sample, "error", error_code=f"api_{exc.code}"[:60])
            await db.commit()
            if time.monotonic() >= deadline:
                raise HTTPException(
                    409, detail={"code": "api_verification_incomplete"}
                ) from None
            continue
        break
    else:
        raise HTTPException(409, detail={"code": "api_verification_incomplete"})
    location, source, run, _ = await context(db, run_id)
    await authorize(db)
    await scans.validate_context(db, location, source, snapshot=run.snapshot)
    await other_active(db, run)
    if run.phase != "preflight" or run.status != "completed":
        raise HTTPException(409, "Preflight changed; refresh before confirming")
    run.phase = "registering"
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run, True


async def resume(db, run_id, actor, *, api_key_id=None, path_digest=None):
    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source, run, old_job = await context(db, run_id)
    await request_authority(db, actor, api_key_id, key_proof, source)
    await scans.validate_context(db, location, source, snapshot=run.snapshot)
    await other_active(db, run)
    if (
        run.status == "running"
        and run.lease_expires_at
        and run.lease_expires_at > await scans.now(db)
    ):
        raise HTTPException(409, "Registration worker still holds a current lease")
    if path_digest is not None:
        entry = await db.get(Entry, (run.id, path_digest))
        if entry is None or entry.state != "error":
            raise HTTPException(409, "Only a failed entry can be retried")
        scan_entry = await db.get(DatasetScanEntry, (run.scan_run_id, path_digest))
        state = (
            "pending"
            if not scan_entry.verified_content or run.phase == "preflight"
            else "ready"
        )
        transition(run, entry, state)
        entry.refresh_unverified = not bool(scan_entry.verified_content)
        if state == "pending":
            run.phase = "preflight"
    elif run.status == "completed":
        raise HTTPException(409, "Use the per-entry retry action for completed work")
    if old_job and old_job.status in ACTIVE:
        await finish(db, run, old_job, "failed", "attempt_superseded")
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run


async def cancel_job(db, job_id, actor):
    await scans.require_actor(db, actor.id)
    run_id = await db.scalar(select(Run.id).where(Run.async_job_id == job_id))
    if not run_id:
        raise HTTPException(409, "Registration attempt has been superseded")
    _, source, run, job = await context(db, run_id)
    await scans.require_actor(db, actor.id, source)
    if run.async_job_id != job_id or run.status not in ACTIVE:
        raise HTTPException(409, "Registration attempt is no longer active")
    await finish(db, run, job, "cancelled", "cancelled_by_user")


async def fail_attempt(db, run_id, task_id, fence, code):
    _, _, run, job = await context(db, run_id)
    if (
        job
        and job.celery_task_id == task_id
        and (
            (fence is None and run.status == "pending")
            or (
                fence is not None
                and run.status == "running"
                and (run.generation, run.lease_token) == fence
            )
        )
    ):
        await finish(db, run, job, "failed", code)


async def signal_interrupted(db, job, *, cancelled):
    observed_id, observed_task_id = job.id, job.celery_task_id
    run_id = await db.scalar(select(Run.id).where(Run.async_job_id == observed_id))
    if run_id:
        _, _, run, current = await context(db, run_id)
        if (
            current
            and current.id == observed_id
            and current.celery_task_id == observed_task_id
            and run.status in ACTIVE
        ):
            await finish(
                db,
                run,
                current,
                "cancelled" if cancelled else "failed",
                "worker_interrupted",
            )


async def yield_attempt(db, run_id, task_id, fence, *, delay=0, code=None):
    _, _, run, job = await fenced(db, run_id, task_id, fence)
    job.celery_task_id = str(uuid.uuid4())
    run.status = job.status = "pending"
    run.lease_token = None
    run.error_code = code
    run.lease_expires_at = await scans.now(db) + timedelta(seconds=delay)
