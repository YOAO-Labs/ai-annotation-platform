"""Fenced, metadata-only directory inventories with durable directory checkpoints."""

from __future__ import annotations

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm.attributes import set_committed_value
from yoao_jobs.lifecycle import JobsLifecycleManager

from app.services import (
    jobs_lifecycle_data as jobs,
    dataset_scan_data as data,
    dataset_source_access as access,
    filesystem_storage as fs,
    reference_storage,
    source_selection as selection,
    storage_access,
    storage_job_progress as progress,
    storage_location as locations,
)
from app.services.async_job_notify import notify_job_terminal
from app.services.assets_dataset_data import dataset_metadata
from app.services.jobs_lifecycle_data import JobsLifecyclePostgresData
from app.services.storage_registry_data import get_location, get_probe

KIND = "dataset_scan"
ACTIVE = {"pending", "running"}


class LostScanLease(Exception):
    pass


def path_digest(path):
    return hashlib.sha256(path.encode()).hexdigest()


async def source_out(db, source, *, location=None, agent_status=...):
    from app.services import agent_availability
    from app.services.directory_policy import effective_cache_policy
    from app.services.dataset_source_protection import declaration

    location = location or await get_location(db, source.location_id)
    if agent_status is ...:
        agent_status = (
            (await agent_availability.for_locations(db, [location.id])).get(location.id)
            if location and location.kind == "agent"
            else None
        )
    return (
        {
            key: getattr(source, key)
            for key in (
                "id",
                "dataset_id",
                "location_id",
                "name",
                "config_revision",
                "enabled",
                "ownership_mode",
                "cache_policy",
                "policy_revision",
                "unregistered_at",
            )
        }
        | source.config
        | {
            "mode": "inventory",
            "external_protection": declaration(source),
            "agent_status": agent_status,
            "effective_cache_policy": effective_cache_policy(source, location)
            if location
            else "none",
            "location_cache_policy": location.config.get("cache_policy", "preview")
            if location
            else "none",
        }
    )


async def now(db):
    return await db.scalar(text("SELECT pg_catalog.clock_timestamp()"))


async def require_actor(db, actor_id, source=None):
    actor = await access.require_manager(db, actor_id)
    if source is not None:
        await access.require_source(db, source.id, actor)
    return actor


async def source_context(db, source_id):
    source = await data.lock_source(db, source_id)
    if source is None:
        raise HTTPException(404, "Dataset source not found")
    return await get_location(db, source.location_id), source


async def run_context(db, run_id):
    run = await data.execution_run(db, run_id, lock=False)
    if run is None:
        raise HTTPException(404, "Dataset scan not found")
    location, source = await source_context(db, run.source_id)
    run = await data.execution_run(db, run_id, lock=True)
    if run is None:
        raise HTTPException(404, "Dataset scan not found")
    job = (
        await jobs.get_job(db, run.async_job_id, populate_existing=True)
        if run.async_job_id
        else None
    )
    return location, source, run, job


async def validate_context(
    db, location, source, *, snapshot=None, check_maintenance_snapshot=True
):
    if (
        not source.enabled
        or source.unregistered_at is not None
        or source.dataset_id is None
        or await dataset_metadata(db, source.dataset_id) is None
    ):
        raise fs.FilesystemStorageError("source_unavailable")
    if (
        location is None
        or not location.enabled
        or location.kind not in {"filesystem", "s3", "sftp", "agent"}
    ):
        raise fs.FilesystemStorageError("location_unavailable")
    await storage_access.check(
        db,
        location.id,
        expected_revision=(
            snapshot.get("maintenance_revision", 1)
            if snapshot is not None and check_maintenance_snapshot
            else None
        ),
        lock=True,
    )
    if location.kind in {"s3", "sftp", "agent"}:
        configured = await reference_storage.configuration(db, location)
        if (
            snapshot
            and "anchor" in snapshot
            and configured.anchor != snapshot["anchor"]
        ):
            raise fs.FilesystemStorageError("configuration_changed")
    if snapshot and (
        snapshot["source_revision"] != source.config_revision
        or snapshot["location_revision"] != location.config_revision
    ):
        raise fs.FilesystemStorageError("configuration_changed")


async def require_dataset_write(db, source, *, snapshot=None, lock=True):
    """Fence source writes without making retained task/scan content unreadable."""
    from app.services.dataset_lifecycle import require_writable

    if source.dataset_id is None:
        raise HTTPException(409, detail={"code": "source_unavailable"})
    expected = (
        snapshot.get("dataset_lifecycle_revision", 1) if snapshot is not None else None
    )
    return await require_writable(
        db, source.dataset_id, expected_revision=expected, lock=lock
    )


async def validate_write_context(db, location, source, *, snapshot=None):
    dataset = await require_dataset_write(db, source, snapshot=snapshot)
    await validate_context(db, location, source, snapshot=snapshot)
    return dataset


async def create_source(db, payload, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    actor = await access.assert_dataset_manage(db, payload.dataset_id, actor)
    location = await locations.get_visible(db, payload.location_id, actor, lock=True)
    actor = await access.assert_dataset_manage(db, payload.dataset_id, actor)
    location = await locations.get_visible(db, payload.location_id, actor)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    locations.require_reference_backend(location)
    from app.services.dataset_lifecycle import require_writable

    await require_writable(db, payload.dataset_id, lock=True)
    if location.kind in {"filesystem", "sftp", "agent"}:
        fs.relative_path(payload.relative_path)
        if location.kind in {"sftp", "agent"}:
            await reference_storage.configuration(db, location)
    else:
        from app.services.source_readers.s3 import directory_prefix

        directory_prefix("", payload.relative_path)
    if any(
        not pattern or len(pattern) > 1000 or "\x00" in pattern
        for pattern in [*payload.include_globs, *payload.exclude_globs]
    ):
        raise HTTPException(422, "Invalid source filter")
    source_id = await data.create_source(
        db,
        actor.id,
        payload.dataset_id,
        location.id,
        payload.name,
        payload.ownership_mode,
        payload.cache_policy,
        payload.model_dump(
            include={
                "relative_path",
                "node_id",
                "recursive",
                "include_globs",
                "exclude_globs",
            }
        ),
    )
    if source_id is None:
        raise HTTPException(404, "Dataset source not found")
    return await data.lock_source(db, source_id)


async def update_source(db, source_id, payload, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    _, source = await source_context(db, source_id)
    await require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await require_dataset_write(db, source)
    result = await data.edit_source(db, actor.id, source.id, payload)
    if result == "not_found":
        raise HTTPException(404, "Dataset source not found")
    if result == "lifecycle_impact_required":
        raise HTTPException(409, detail={"code": result})
    if result != "updated":
        raise HTTPException(
            409,
            {
                "revision_changed": "Source configuration revision changed",
                "unregistered": "Source has been unregistered",
                "scan_active": "Cancel the active scan before editing its source",
                "registration_active": "Cancel the active registration before editing its source",
            }[result],
        )
    return await data.lock_source(db, source_id)


async def new_job(db, run, source, actor, *, api_key_id=None, key_proof=None):
    from app.services.dataset_content import key_authority

    await require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if api_key_id:
        api_key_id = uuid.UUID(str(api_key_id))
    task_id = str(uuid.uuid4())
    job = await jobs.create_job(
        db,
        kind=KIND,
        user_id=actor.id,
        celery_task_id=task_id,
        payload={
            "run_id": str(run.id),
            "source_id": str(source.id),
            "dataset_id": str(source.dataset_id),
            "stage": "queued",
            "progress_mode": "indeterminate",
        },
    )
    if not await data.bind_attempt(db, run, job, api_key_id, key_proof):
        raise HTTPException(409, "Scan attempt has been superseded")
    return await data.execution_run(db, run.id)


async def start_scan(db, source_id, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source = await source_context(db, source_id)
    await require_actor(db, actor.id, source)
    dataset = await validate_write_context(db, location, source)
    existing_id = await data.active_scan_id(db, source.id)
    if existing_id:
        existing = await data.execution_run(db, existing_id)
        await validate_write_context(db, location, source, snapshot=existing.snapshot)
        return existing, False
    node = f"worker:{source.config['node_id']}"
    await locations.require_scan_node(db, location, node)
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise HTTPException(409, "Worker root identity is not verified")
    probe = await get_probe(db, location.id, locations.probe_node_id(location, node))
    run_id = await data.start(
        db,
        actor.id,
        source.id,
        {
            "location_revision": location.config_revision,
            "maintenance_revision": await storage_access.check(
                db, location.id, lock=True
            ),
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "anchor": anchor,
            "scan_host_id": probe.result.get("host_id"),
        },
    )
    if run_id is None:
        raise HTTPException(404, "Dataset source not found")
    run = await data.execution_run(db, run_id)
    run = await new_job(
        db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof
    )
    return run, True


async def finish(db, run, job, status, code=None):
    if not await data.settle(db, run, job, status, code):
        raise LostScanLease()
    await data.execution_run(db, run.id)
    if job is not None:
        await jobs.get_job(db, job.id, populate_existing=True)
        await notify_job_terminal(db, job_id=job.id)


async def resume_scan(db, run_id, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source, run, job = await run_context(db, run_id)
    await require_actor(db, actor.id, source)
    await validate_write_context(db, location, source, snapshot=run.snapshot)
    if run.status == "completed":
        raise HTTPException(409, "Completed scans cannot be resumed")
    if (
        run.status == "running"
        and run.lease_expires_at
        and run.lease_expires_at > await now(db)
    ):
        raise HTTPException(409, "Scan worker still holds a current lease")
    if await data.active_scan_id(db, source.id, except_run_id=run.id):
        raise HTTPException(409, "Another scan is active for this source")
    await locations.require_scan_node(
        db, location, f"worker:{source.config['node_id']}"
    )
    if job and job.status in ACTIVE:
        await finish(db, run, job, "failed", "attempt_superseded")
    return await new_job(
        db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof
    )


async def cancel_job(db, job_id, actor):
    await require_actor(db, actor.id)
    run_id = await data.run_id_for_job(db, job_id)
    if run_id is None:
        raise HTTPException(409, "Scan attempt has been superseded")
    _, source, run, job = await run_context(db, run_id)
    await require_actor(db, actor.id, source)
    if run.async_job_id != job_id or run.status not in ACTIVE:
        raise HTTPException(409, "Scan attempt is no longer active")
    await finish(db, run, job, "cancelled", "cancelled_by_user")


async def signal_interrupted(db, job, *, cancelled):
    job_id, task_id = job.id, job.celery_task_id
    run_id = await data.run_id_for_job(db, job_id)
    if run_id is None:
        return
    _, _, run, current = await run_context(db, run_id)
    if (
        current is None
        or current.id != job_id
        or current.celery_task_id != task_id
        or run.status not in ACTIVE
    ):
        return
    await finish(
        db, run, current, "cancelled" if cancelled else "failed", "worker_interrupted"
    )


async def claim(db, run_id, task_id, node_id):
    from app.services.content_snapshot import ContentError
    from app.services.site_agents import SiteAgentError

    location, source, run, job = await run_context(db, run_id)
    if (
        job is None
        or job.celery_task_id != task_id
        or run.status != "pending"
        or job.status != "pending"
    ):
        return None
    try:
        actor = await require_actor(db, job.user_id, source)
        from app.services.dataset_content import key_authority

        if await key_authority(
            db, actor, run.snapshot.get("api_key_id"), "datasets:write"
        ) != run.snapshot.get("key_proof"):
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await validate_write_context(db, location, source, snapshot=run.snapshot)
        if node_id != source.config["node_id"]:
            raise fs.FilesystemStorageError("wrong_execution_node")
        await locations.require_scan_node(db, location, f"worker:{node_id}")
    except (
        fs.FilesystemStorageError,
        HTTPException,
        ContentError,
        SiteAgentError,
    ) as exc:
        await finish(
            db,
            run,
            job,
            "failed",
            failure_code(exc, "node_not_verified"),
        )
        return None
    if location.kind == "agent":
        probe = await get_probe(
            db, location.id, locations.probe_node_id(location, f"worker:{node_id}")
        )
        host_id = probe.result.get("host_id") if probe is not None else None
    else:
        host_id = fs.host_identity()
    started_at = await data.claim(db, run, job, node_id, host_id)
    if started_at is None:
        return None
    run = await data.execution_run(db, run.id)
    set_committed_value(source, "generation", run.generation)
    JobsLifecycleManager(
        JobsLifecyclePostgresData(db), clock=lambda: started_at
    ).mark_running(job)
    return (run.generation, run.lease_token)


def failure_code(exc, fallback):
    from app.services.content_snapshot import ContentError
    from app.services.site_agents import SiteAgentError

    if getattr(exc, "storage_operation_unsettled", False):
        return "storage_operation_unsettled"
    if isinstance(exc, (fs.FilesystemStorageError, ContentError, SiteAgentError)):
        return exc.code
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return exc.detail["code"]
        if exc.status_code in {403, 404}:
            return "access_revoked"
    return fallback


async def fenced_context(db, run_id, task_id, fence):
    location, source, run, job = await run_context(db, run_id)
    if (
        job is None
        or job.celery_task_id != task_id
        or run.status != "running"
        or job.status != "running"
        or (run.generation, run.lease_token) != fence
        or source.generation != run.generation
        or run.lease_expires_at is None
        or run.lease_expires_at <= await now(db)
    ):
        raise LostScanLease()
    actor = await require_actor(db, job.user_id, source)
    await validate_write_context(db, location, source, snapshot=run.snapshot)
    from app.services.dataset_content import key_authority

    if await key_authority(
        db, actor, run.snapshot.get("api_key_id"), "datasets:write"
    ) != run.snapshot.get("key_proof"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return location, source, run, job


async def next_directory(db, run_id, task_id, fence):
    location, source, run, job = await fenced_context(db, run_id, task_id, fence)
    if location.kind == "s3":
        from app.services.s3_dataset_scan import next_page

        work = await next_page(db, location, source, run, job)
        if work is not None:
            work["_fault_observation"] = await scan_fault_observation(
                db, location, source, run, job, work
            )
            work["source_io"] = source_scan_proof(run, job, pages=work["pages"])
        return work
    entry = await data.next_directory(db, run, job)
    if entry.outcome == "stale":
        raise LostScanLease()
    if entry.outcome == "done":
        await finish(db, run, job, "completed")
        return None
    run = await data.execution_run(db, run.id)
    if entry.outcome == "transition":
        return {"transition": True}
    await jobs.update_payload(db, job.id, {"stage": run.phase})
    path = "/".join(
        part for part in [source.config["relative_path"], entry.relative_path] if part
    )
    fs.relative_path(path)
    work = {
        "location_id": location.id,
        "maintenance_revision": run.snapshot.get("maintenance_revision", 1),
        "path_digest": entry.path_digest,
        "relative_path": entry.relative_path,
        "phase": run.phase,
        "path": path,
        "kind": location.kind,
        "config": await reference_storage.configuration(db, location),
        "anchor": run.snapshot["anchor"],
        "identity": entry.observed.get("identity")
        or (source.root_identity if not entry.relative_path else None),
    }
    if location.kind == "agent":
        work["agent_fence"] = {
            "source_id": str(source.id),
            "run_id": str(run.id),
            "job_id": str(job.id),
            "task_id": task_id,
            "generation": run.generation,
            "lease_token": str(run.lease_token),
            "user_id": str(job.user_id),
        }

    if location.kind == "sftp":
        work["source_io"] = source_scan_proof(run, job, path_digest=entry.path_digest)

    work["_fault_observation"] = await scan_fault_observation(
        db, location, source, run, job, work
    )
    return work


def source_scan_proof(run, job, **member):
    return {
        "actor_id": job.user_id,
        "key_id": run.snapshot.get("api_key_id"),
        "key_proof": run.snapshot.get("key_proof"),
        "request": {
            "run_id": str(run.id),
            "job_id": str(job.id),
            "task_id": job.celery_task_id,
            "generation": run.generation,
            "lease_token": str(run.lease_token),
            **member,
        },
    }


async def scan_fault_observation(db, location, source, run, job, work):
    from app.services import storage_fault_events

    return await storage_fault_events.prepare(
        db,
        location=location,
        source=source,
        operation="scan",
        actor_id=job.user_id,
        node_id=source.config["node_id"]
        if source.config["node_id"].startswith("worker:")
        else "worker:" + source.config["node_id"],
        anchor=run.snapshot["anchor"],
        maintenance_revision=run.snapshot.get("maintenance_revision", 1),
        path_digest=work.get("path_digest"),
        target=[work["path"], work.get("cursor")],
        expected=work.get("identity"),
        run_id=run.id,
        job_id=job.id,
    )


async def apply_directory(db, run_id, task_id, fence, work, manifest):
    _, source, run, job = await fenced_context(db, run_id, task_id, fence)
    if work.get("kind") == "s3":
        from app.services.s3_dataset_scan import apply_page

        return await apply_page(db, run, job, work, manifest)
    if run.phase != work["phase"]:
        raise LostScanLease()
    if work["phase"] != "enumerating":
        result = await data.validate_directory(db, run, job, work, manifest)
        require_capture(result.outcome)
        if result.progress_count is not None:
            await directory_progress(db, run, job, task_id, work, result.progress_count)
        return
    entry = await data.read_directory(db, run, job, work["path_digest"])
    if entry is None or entry.state != "pending":
        raise LostScanLease()
    config = run.snapshot["config"]
    rows = []
    for item in manifest["entries"]:
        path = "/".join(part for part in [entry.relative_path, item["name"]] if part)
        fs.relative_path(
            "/".join(part for part in [config["relative_path"], path] if part)
        )
        selected = item["kind"] == "file" and selection.selected_file(
            config, path, "filesystem"
        )
        state = (
            "pending"
            if item["kind"] == "directory" and selection.descend(config, path)
            else "observed"
            if selected
            else "skipped"
        )
        rows.append(
            {
                "path_digest": path_digest(path),
                "relative_path": path,
                "kind": item["kind"],
                "state": state,
                "observed": {
                    key: value for key, value in item.items() if key != "name"
                },
            }
        )
    result = await data.capture_directory(db, run, job, work, manifest, rows)
    require_capture(result.outcome)
    if not entry.relative_path:
        set_committed_value(source, "root_identity", manifest["identity"])
    await data.execution_run(db, run.id)
    await jobs.replace_result(db, job.id, result={**result.counts, "complete": False})
    await directory_progress(db, run, job, task_id, work, result.progress_count)


def require_capture(outcome):
    if outcome == "stale":
        raise LostScanLease()
    if outcome != "accepted":
        raise fs.FilesystemStorageError(outcome)


async def directory_progress(db, run, job, task_id, work, completed):
    await db.flush()
    validating = run.phase != "enumerating"
    await progress.record(
        db,
        job,
        task_id=task_id,
        stage="validating" if validating else "scanning",
        unit="directories",
        completed=completed,
        marker=f"{run.id}:{run.generation}:{run.phase}:{work['path_digest']}",
    )


async def fail_attempt(
    db, run_id, task_id, fence, code, *, failure=None, observation=None
):
    try:
        location, source, run, job = await run_context(db, run_id)
        if (
            job
            and job.celery_task_id == task_id
            and run.status == "running"
            and (run.generation, run.lease_token) == fence
        ):
            permitted = True
            try:
                await fenced_context(db, run_id, task_id, fence)
            except (HTTPException, fs.FilesystemStorageError, LostScanLease):
                permitted = False
            await finish(db, run, job, "failed", code)
            if failure is not None and permitted:
                from app.services import storage_fault_events

                await storage_fault_events.report(
                    db,
                    failure,
                    operation="scan",
                    observation=observation,
                    actor_id=job.user_id,
                    location_id=location.id,
                    source_id=source.id,
                    dataset_id=source.dataset_id,
                    run_id=run.id,
                    job_id=job.id,
                )
    except HTTPException:
        pass
