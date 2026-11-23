"""Fenced two-stage directory registration with durable, per-file checkpoints."""

import time
import uuid

from fastapi import HTTPException

from app.db.models.async_job import AsyncJob
from app.services import async_job as jobs
from app.services import dataset_registration_data as data
from app.services import dataset_scans as scans
from app.services import dataset_source_changes as changes
from app.services import filesystem_storage as fs
from app.services import reference_storage, storage_access
from app.services import storage_location as locations
from app.services.async_job_notify import notify_job_terminal
from app.services.dataset_content import key_authority
from app.services.jobs_lifecycle_data import job_lock_statement

KIND = "dataset_registration"
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
    binding = await data.binding(db, run_id)
    if binding is None:
        raise HTTPException(404, "Registration not found")
    location, source = await scans.source_context(db, binding.source_id)
    binding = await data.binding(db, run_id)
    job = await lock_job(db, binding.job_id)
    run = await data.read_run(db, run_id, lock=True)
    return location, source, run, job


async def lock_job(db, job_id):
    if job_id is None:
        return None
    return await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )


async def validate(db, location, source, run):
    await scans.validate_write_context(db, location, source, snapshot=run.snapshot)
    if source.policy_revision != run.snapshot.get("policy_revision", 1):
        raise fs.FilesystemStorageError("configuration_changed")
    actor = await scans.require_actor(db, uuid.UUID(run.snapshot["actor_id"]), source)
    proof = await key_authority(
        db, actor, run.snapshot.get("api_key_id"), "datasets:write"
    )
    if proof != run.snapshot.get("key_proof"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor


async def request_authority(db, actor, api_key_id, proof, source=None):
    actor = await scans.require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor


async def attempt(db, run, source, actor, *, api_key_id=None, key_proof):
    actor = await request_authority(db, actor, api_key_id, key_proof, source)
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
    if not await data.open_attempt(db, run, job.id, actor.id, api_key_id, key_proof):
        raise HTTPException(409, detail={"code": "registration_attempt_changed"})
    await data.read_run(db, run.id)
    await jobs.replace_result(db, job.id, result=dict(run.counts))
    return job


async def start_preflight(db, scan_id, actor, *, api_key_id=None, worker_node=None):
    from app.services.directory_policy import effective_cache_policy

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source, scan, _ = await scans.run_context(db, scan_id)
    actor = await request_authority(db, actor, api_key_id, key_proof, source)
    await scans.validate_write_context(db, location, source, snapshot=scan.snapshot)
    if not scan.complete or scan.status != "completed":
        raise HTTPException(409, detail={"code": "scan_incomplete"})
    existing = await data.for_scan(db, scan_id)
    if existing:
        return existing, False
    if await data.active_run_id(db, source.id):
        raise HTTPException(409, "Another registration is active for this source")
    await locations.require_scan_node(
        db, location, f"worker:{source.config['node_id']}"
    )
    if worker_node is not None:
        if worker_node != source.config["node_id"]:
            raise HTTPException(409, detail={"code": "wrong_execution_node"})
        await key_authority(db, actor, api_key_id, "datasets:read")
    else:

        async def probe_authority():
            current_location, current_source, current_scan, _ = await scans.run_context(
                db, scan_id
            )
            await request_authority(db, actor, api_key_id, key_proof, current_source)
            await scans.validate_write_context(
                db, current_location, current_source, snapshot=current_scan.snapshot
            )

        result = await probe_candidates(
            db,
            location,
            source,
            scan.id,
            locations.api_node_id(),
            guard=probe_authority,
        )
        location, source, scan, _ = await scans.run_context(db, scan_id)
        await request_authority(db, actor, api_key_id, key_proof, source)
        await scans.validate_write_context(db, location, source, snapshot=scan.snapshot)
        await locations.store_probe(
            db, location, locations.api_node_id(), result, await scans.now(db)
        )
        if not result["ok"]:
            raise HTTPException(
                409, detail={"code": "api_source_unavailable", "reason": result["code"]}
            )
    existing = await data.for_scan(db, scan_id)
    if existing:
        return existing, False
    if await data.active_run_id(db, source.id):
        raise HTTPException(409, "Another registration is active for this source")
    run_id = uuid.uuid4()
    code = await data.create_preflight(
        db, run_id, scan_id, actor.id, effective_cache_policy(source, location)
    )
    if code:
        raise HTTPException(
            403 if code == "access_revoked" else 409, detail={"code": code}
        )
    run = await data.read_run(db, run_id)
    await changes.initialize(db, run, scan.snapshot["config"], location.kind)
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run, True


async def finish(db, run, job, status, code=None):
    if not await data.finish(db, run, job, status, code):
        return
    await data.read_run(db, run.id)
    if job is not None:
        await lock_job(db, job.id)
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

    async def probe_authority():
        current_location, current_source, current_run, current_job = await context(
            db, run_id
        )
        if (
            not current_job
            or current_job.celery_task_id != task_id
            or current_run.status != "pending"
            or current_job.status != "pending"
        ):
            raise LostRegistrationLease()
        await validate(db, current_location, current_source, current_run)

    result = await probe_candidates(
        db, location, source, run.scan_run_id, node, guard=probe_authority
    )
    location, source, run, job = await context(db, run_id)
    if (
        not job
        or job.celery_task_id != task_id
        or job.status != "pending"
        or run.status != "pending"
    ):
        raise LostRegistrationLease()
    await validate(db, location, source, run)
    await locations.store_probe(db, location, node, result, await scans.now(db))
    if not result["ok"]:
        raise fs.FilesystemStorageError("worker_source_unavailable")
    token = uuid.uuid4()
    if not await data.claim(db, run, job, node_id, token):
        raise LostRegistrationLease()
    await lock_job(db, job.id)
    await data.read_run(db, run.id)
    return run.generation, token


async def consume_relocation(db, run_id, digest, item_id, revision_id):
    if not await data.consume_relocation(db, run_id, digest, item_id, revision_id):
        raise HTTPException(409, detail={"code": "relocation_target_unavailable"})
    await data.read_run(db, run_id)


async def next_entry(db, run_id, task_id, fence):
    _, _, run, job = await fenced(db, run_id, task_id, fence)
    entry = await data.next_entry(db, run, job, fence)
    if entry is not None and entry.path_digest is None:
        raise LostRegistrationLease()
    if entry is None:
        await finish(db, run, job, "completed")
        return None
    await data.read_run(db, run.id)
    return {
        "path_digest": entry.path_digest,
        "scan_run_id": run.scan_run_id,
        "job_id": job.id,
        "registration_id": run.id,
        "phase": run.phase,
        "refresh_unverified": entry.refresh_unverified,
        "actor_id": uuid.UUID(run.snapshot["actor_id"]),
        "api_key_id": run.snapshot.get("api_key_id"),
        "node_id": f"worker:{run.snapshot['node_id']}",
        "cache_policy": run.cache_policy,
    }


async def record(
    db,
    run_id,
    task_id,
    fence,
    path_digest,
    state,
    *,
    error_code=None,
    result=None,
    preview_estimated_bytes=None,
    preview_error=None,
    original_size=0,
):
    _, _, run, job = await fenced(db, run_id, task_id, fence)
    recorded = await data.record_entry(
        db,
        run,
        job,
        fence,
        path_digest,
        state,
        error=error_code,
        result=result,
        preview_estimate=preview_estimated_bytes,
        preview_error=preview_error,
        original_size=original_size,
    )
    if recorded is None:
        raise LostRegistrationLease()
    if recorded.rejected:
        raise HTTPException(409, detail={"code": recorded.rejected})
    await data.read_run(db, run.id)
    total = run.counts["indexed_count"]
    remaining = run.counts[
        "pending_count" if run.phase == "preflight" else "ready_count"
    ]
    await jobs.update_progress(
        db,
        job.id,
        int(100 * (total - remaining) / total) if total else 100,
        result=dict(run.counts),
    )
    from app.services import storage_job_progress as progress

    await progress.record(
        db,
        job,
        task_id=task_id,
        stage="preflight" if run.phase == "preflight" else "registering",
        unit="entries",
        completed=total - remaining,
        marker=(
            f"{run.id}:{run.generation}:{run.phase}:"
            f"{path_digest}:{recorded.attempts}:{recorded.state}"
        ),
    )


async def other_active(db, run):
    if await data.active_run_id(db, run.source_id, except_id=run.id):
        raise HTTPException(409, "Another registration is active for this source")


async def probe_candidates(db, location, source, scan_id, node, *, guard=None):
    async def settled(result):
        if location.kind == "agent":
            if guard is None:
                raise LostRegistrationLease()
            await guard()
            await locations.store_probe(db, location, node, result, await scans.now(db))
            await db.commit()
            result = {
                key: value
                for key, value in result.items()
                if key != "_agent_command_id"
            }
        return result

    root = await locations.perform_probe(db, location, node, None)
    root = await settled(root)
    if not root["ok"]:
        return root
    for path in await data.probe_paths(db, scan_id):
        probe = await locations.perform_probe(
            db,
            location,
            node,
            reference_storage.sample_path(
                location, source.config["relative_path"], path
            ),
        )
        probe = await settled(probe)
        if location.kind == "s3" and probe["code"] == "s3_conditions_not_supported":
            return probe
        if probe["ok"]:
            return probe
    return root


def check_source_directory(config, path, anchor):
    from app.services.source_readers.s3 import S3Access
    from app.services.source_readers.sftp import SftpAccess

    if isinstance(config, S3Access):
        config.list_page(path)
        return
    if isinstance(config, SftpAccess):
        config.scan_directory(path, expected_anchor=anchor)
        return
    with fs.location_directory(config, path, expected_anchor=anchor):
        return


async def ensure_source_available(db, location, source, node):
    config, relative = (
        await reference_storage.configuration(db, location),
        source.config["relative_path"],
    )
    anchor = await locations.node_anchor(db, location, node)
    revision = await storage_access.check(db, location.id, lock=True)
    location_id = location.id
    await db.commit()
    if location.kind == "agent":
        from app.services import agent_transport

        receipt = await agent_transport.exchange(
            db,
            location,
            "scan",
            {
                "path": relative,
                "expected_anchor": anchor,
                "expected_identity": source.root_identity,
            },
            fence={"source_id": str(source.id)},
            purpose="registration_directory",
            expected_revision=revision,
            node_id=node,
        )
        await agent_transport.acknowledge(db, receipt, accepted=True)
        return
    try:
        await storage_access.run(
            db,
            location_id,
            "registration_directory",
            check_source_directory,
            config,
            relative,
            anchor,
            expected_revision=revision,
            node_id=node,
            timeout=3,
        )
    except (TimeoutError, fs.FilesystemStorageError):
        raise fs.FilesystemStorageError("source_unavailable") from None


async def confirm(
    db,
    run_id,
    actor,
    *,
    api_key_id=None,
    scope="all_safe",
    worker_node=None,
    before_commit=None,
):
    from app.services.dataset_content import scan_snapshot
    from app.services.content_snapshot import ContentError

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    preflight_version = None
    source = None

    async def authorize_request(current_db):
        nonlocal actor
        actor = await request_authority(
            current_db, actor, api_key_id, key_proof, source
        )
        if source is not None:
            await scans.require_dataset_write(current_db, source, snapshot=run.snapshot)
        if before_commit is not None:
            await before_commit(current_db)

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
        if worker_node is not None and worker_node != source.config["node_id"]:
            raise HTTPException(409, detail={"code": "wrong_worker_node"})
        node_id = (
            f"worker:{worker_node}"
            if worker_node is not None
            else locations.api_node_id()
        )
        await authorize(db)
        await scans.validate_write_context(db, location, source, snapshot=run.snapshot)
        await other_active(db, run)
        if run.phase == "registering":
            if run.registration_scope != scope:
                raise HTTPException(409, detail={"code": "registration_scope_changed"})
            return run, False
        if run.status != "completed":
            raise HTTPException(
                409, "Complete the preflight before confirming registration"
            )
        if preflight_version is None:
            preflight_version = (run.async_job_id, run.generation)
        if scope == "additions_only" and not run.comparison_complete:
            raise HTTPException(409, detail={"code": "comparison_incomplete"})
        sample = await data.confirmation_sample(db, run.id, scope)
        if sample is None:
            if scope == "additions_only":
                raise HTTPException(409, detail={"code": "no_added_images"})
            if run.counts["error_count"]:
                raise HTTPException(409, detail={"code": "no_ready_images"})
            break
        scan_id, digest, sha = run.scan_run_id, sample.path_digest, sample.sha256
        proof = await locations.perform_probe(db, location, node_id, None)
        await authorize(db)
        await locations.store_probe(db, location, node_id, proof, await scans.now(db))
        if not proof["ok"]:
            raise HTTPException(
                409,
                detail={
                    "code": "source_unavailable"
                    if worker_node
                    else "api_source_unavailable",
                    "reason": proof["code"],
                },
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
                before_io=authorize_request,
                node_id=node_id,
            )
            snapshot.close()
        except (ContentError, fs.FilesystemStorageError) as exc:
            await db.rollback()
            if exc.code not in FILE_ERRORS:
                raise
            location, source, run, _ = await context(db, run_id)
            await authorize(db)
            if exc.code in PATH_ERRORS:
                await ensure_source_available(db, location, source, node_id)
                _, _, run, _ = await context(db, run_id)
                await authorize(db)
            if run.phase != "preflight" or run.status != "completed":
                raise HTTPException(
                    409, "Preflight changed; refresh before confirming"
                ) from None
            await data.fail_confirmation_sample(
                db,
                run.id,
                preflight_version,
                digest,
                f"{'worker' if worker_node else 'api'}_{exc.code}"[:60],
            )
            await data.read_run(db, run.id)
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
    await scans.validate_write_context(db, location, source, snapshot=run.snapshot)
    await other_active(db, run)
    if not await data.confirm(db, run.id, preflight_version, scope):
        raise HTTPException(409, "Preflight changed; refresh before confirming")
    await data.read_run(db, run.id)
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run, True


async def resume(db, run_id, actor, *, api_key_id=None, path_digest=None):
    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source, run, old_job = await context(db, run_id)
    actor = await request_authority(db, actor, api_key_id, key_proof, source)
    await scans.validate_write_context(db, location, source, snapshot=run.snapshot)
    await other_active(db, run)
    if (
        run.status == "running"
        and run.lease_expires_at
        and run.lease_expires_at > await scans.now(db)
    ):
        raise HTTPException(409, "Registration worker still holds a current lease")
    if path_digest is not None:
        code = await data.retry_entry(db, run, path_digest)
        if code == "entry_not_failed":
            raise HTTPException(409, "Only a failed entry can be retried")
        if code == "active_lease":
            raise HTTPException(409, "Registration worker still holds a current lease")
        if code:
            raise HTTPException(409, detail={"code": code})
        await data.read_run(db, run.id)
    elif run.status == "completed":
        raise HTTPException(409, "Use the per-entry retry action for completed work")
    if old_job and old_job.status in ACTIVE:
        await finish(db, run, old_job, "failed", "attempt_superseded")
    await attempt(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    return run


async def cancel_job(db, job_id, actor):
    await scans.require_actor(db, actor.id)
    run_id = await data.run_for_job(db, job_id)
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


async def signal_interrupted(db, job: AsyncJob, *, cancelled):
    observed_id, observed_task_id = job.id, job.celery_task_id
    run_id = await data.run_for_job(db, observed_id)
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
    if not await data.requeue(
        db, run, job, fence, str(uuid.uuid4()), delay=delay, error=code
    ):
        raise LostRegistrationLease()
    await lock_job(db, job.id)
    await data.read_run(db, run.id)
