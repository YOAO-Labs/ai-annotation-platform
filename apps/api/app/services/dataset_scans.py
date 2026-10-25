"""Fenced, metadata-only directory inventories with durable directory checkpoints."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetSource, DatasetScanEntry, DatasetScanRun
from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.services import async_job as jobs
from app.services import filesystem_storage as fs
from app.services import storage_location as locations
from app.services import (
    reference_storage,
    source_selection as selection,
    storage_access,
)
from app.services import dataset_source_access as access
from app.services.async_job_notify import notify_job_terminal

KIND = "dataset_scan"
LEASE_SECONDS = 60
BUDGET = {"max_entries": 50000, "max_directories": 5000}
ACTIVE = {"pending", "running"}


class LostScanLease(Exception):
    pass


def path_digest(path):
    return hashlib.sha256(path.encode()).hexdigest()


async def source_out(db, source, *, location=None):
    from app.services.directory_policy import effective_cache_policy
    from app.services.dataset_source_protection import declaration

    location = location or await db.get(
        StorageLocation, source.location_id, populate_existing=True
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
            "effective_cache_policy": effective_cache_policy(source, location)
            if location
            else "none",
            "location_cache_policy": location.config.get("cache_policy", "preview")
            if location
            else "none",
        }
    )


async def now(db):
    return await db.scalar(select(func.clock_timestamp()))


async def require_actor(db, actor_id, source=None):
    actor = await access.require_manager(db, actor_id)
    if source is not None:
        await access.require_source(db, source.id, actor)
    return actor


async def source_context(db, source_id):
    source = await db.get(DatasetSource, source_id)
    if source is None:
        raise HTTPException(404, "Dataset source not found")
    location = await db.scalar(
        select(StorageLocation)
        .where(StorageLocation.id == source.location_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    source = await db.scalar(
        select(DatasetSource)
        .where(DatasetSource.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return location, source


async def run_context(db, run_id):
    run = await db.get(DatasetScanRun, run_id)
    if run is None:
        raise HTTPException(404, "Dataset scan not found")
    location, source = await source_context(db, run.source_id)
    # Waiting for Source may have allowed a resume to replace the attempt.
    # Read the association again after serialization, before locking Job.
    job_id = await db.scalar(
        select(DatasetScanRun.async_job_id).where(DatasetScanRun.id == run_id)
    )
    # Retention deletes Job before its FK clears Run.async_job_id. Match that
    # order after Source serialization, including resumes of old terminal jobs.
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
        select(DatasetScanRun)
        .where(DatasetScanRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return location, source, run, job


async def validate_context(
    db, location, source, *, snapshot=None, check_maintenance_snapshot=True
):
    if (
        not source.enabled
        or source.unregistered_at is not None
        or source.dataset_id is None
        or await db.get(Dataset, source.dataset_id, populate_existing=True) is None
    ):
        raise fs.FilesystemStorageError("source_unavailable")
    if (
        location is None
        or not location.enabled
        or location.kind not in {"filesystem", "s3", "sftp"}
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
    if location.kind in {"s3", "sftp"}:
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
    if location.kind in {"filesystem", "sftp"}:
        fs.relative_path(payload.relative_path)
        if location.kind == "sftp":
            await reference_storage.configuration(db, location)
    else:
        from app.services.sources.s3_reference import directory_prefix

        directory_prefix("", payload.relative_path)
    if any(
        not pattern or len(pattern) > 1000 or "\x00" in pattern
        for pattern in [*payload.include_globs, *payload.exclude_globs]
    ):
        raise HTTPException(422, "Invalid source filter")
    source = DatasetSource(
        dataset_id=payload.dataset_id,
        location_id=location.id,
        name=payload.name,
        ownership_mode=payload.ownership_mode,
        cache_policy=payload.cache_policy,
        config=payload.model_dump(
            include={
                "relative_path",
                "node_id",
                "recursive",
                "include_globs",
                "exclude_globs",
            }
        ),
    )
    db.add(source)
    await db.flush()
    return source


async def update_source(db, source_id, payload, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    _, source = await source_context(db, source_id)
    await require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await require_dataset_write(db, source)
    if source.config_revision != payload.expected_revision:
        raise HTTPException(409, "Source configuration revision changed")
    if source.unregistered_at is not None:
        raise HTTPException(409, "Source has been unregistered")
    if payload.enabled is not None and payload.enabled != source.enabled:
        raise HTTPException(409, detail={"code": "lifecycle_impact_required"})
    if await db.scalar(
        select(DatasetScanRun.id)
        .where(DatasetScanRun.source_id == source.id, DatasetScanRun.status.in_(ACTIVE))
        .limit(1)
    ):
        raise HTTPException(409, "Cancel the active scan before editing its source")
    from app.db.models.dataset_source import DatasetRegistrationRun

    if await db.scalar(
        select(DatasetRegistrationRun.id).where(
            DatasetRegistrationRun.source_id == source.id,
            DatasetRegistrationRun.status.in_(ACTIVE),
        )
    ):
        raise HTTPException(
            409, "Cancel the active registration before editing its source"
        )
    if payload.name is not None:
        source.name = payload.name
    if payload.enabled is not None:
        source.enabled = payload.enabled
    if payload.cache_policy is not None and payload.cache_policy != source.cache_policy:
        source.cache_policy = payload.cache_policy
        source.policy_revision += 1
    source.config_revision += 1
    await db.flush()
    return source


async def new_job(db, run, source, actor, *, api_key_id=None, key_proof=None):
    from app.services.dataset_content import key_authority

    await require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    run.snapshot = {
        **run.snapshot,
        "api_key_id": str(api_key_id) if api_key_id else None,
        "key_proof": key_proof,
    }
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
    run.async_job_id = job.id
    run.status = "pending"
    run.error_code = None
    run.complete = False
    run.completed_at = None
    run.lease_token = None
    run.lease_expires_at = None
    return job


async def start_scan(db, source_id, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source = await source_context(db, source_id)
    await require_actor(db, actor.id, source)
    dataset = await validate_write_context(db, location, source)
    existing = await db.scalar(
        select(DatasetScanRun).where(
            DatasetScanRun.source_id == source.id, DatasetScanRun.status.in_(ACTIVE)
        )
    )
    if existing:
        await validate_write_context(db, location, source, snapshot=existing.snapshot)
        return existing, False
    node = f"worker:{source.config['node_id']}"
    await locations.require_scan_node(db, location, node)
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise HTTPException(409, "Worker root identity is not verified")
    probe = await db.get(
        StorageLocationProbe, (location.id, node), populate_existing=True
    )
    run = DatasetScanRun(
        source_id=source.id,
        snapshot={
            "source_revision": source.config_revision,
            "location_revision": location.config_revision,
            "maintenance_revision": await storage_access.check(
                db, location.id, lock=True
            ),
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "anchor": anchor,
            "scan_host_id": probe.result.get("host_id"),
            "config": dict(source.config),
            "budget": dict(BUDGET),
        },
    )
    db.add(run)
    await db.flush()
    db.add(
        DatasetScanEntry(
            run_id=run.id,
            path_digest=path_digest(""),
            relative_path="",
            kind="directory",
            state="pending",
            observed={},
        )
    )
    await new_job(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    await db.flush()
    return run, True


async def finish(db, run, job, status, code=None):
    run.status = status
    run.error_code = code
    run.complete = status == "completed"
    run.completed_at = await now(db)
    run.lease_token = None
    run.lease_expires_at = None
    if job is not None:
        job.status = status
        job.progress_pct = 100 if run.complete else 0
        job.completed_at = run.completed_at
        job.error_message = code
        job.result = {
            **run.counts,
            "complete": run.complete,
            "error_code": code,
            "run_id": str(run.id),
        }
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
    other = await db.scalar(
        select(DatasetScanRun.id).where(
            DatasetScanRun.source_id == source.id,
            DatasetScanRun.id != run.id,
            DatasetScanRun.status.in_(ACTIVE),
        )
    )
    if other:
        raise HTTPException(409, "Another scan is active for this source")
    await locations.require_scan_node(
        db, location, f"worker:{source.config['node_id']}"
    )
    if job and job.status in ACTIVE:
        await finish(db, run, job, "failed", "attempt_superseded")
    await new_job(db, run, source, actor, api_key_id=api_key_id, key_proof=key_proof)
    await db.flush()
    return run


async def cancel_job(db, job_id, actor):
    await require_actor(db, actor.id)
    run_id = await db.scalar(
        select(DatasetScanRun.id).where(DatasetScanRun.async_job_id == job_id)
    )
    if run_id is None:
        raise HTTPException(409, "Scan attempt has been superseded")
    _, source, run, job = await run_context(db, run_id)
    await require_actor(db, actor.id, source)
    if run.async_job_id != job_id or run.status not in ACTIVE:
        raise HTTPException(409, "Scan attempt is no longer active")
    await finish(db, run, job, "cancelled", "cancelled_by_user")


async def signal_interrupted(db, job, *, cancelled):
    run_id = await db.scalar(
        select(DatasetScanRun.id).where(DatasetScanRun.async_job_id == job.id)
    )
    if run_id is None:
        return
    _, _, run, current = await run_context(db, run_id)
    if (
        current is None
        or current.id != job.id
        or current.celery_task_id != job.celery_task_id
        or run.status not in ACTIVE
    ):
        return
    await finish(
        db, run, current, "cancelled" if cancelled else "failed", "worker_interrupted"
    )


async def claim(db, run_id, task_id, node_id):
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
    except (fs.FilesystemStorageError, HTTPException) as exc:
        await finish(
            db,
            run,
            job,
            "failed",
            failure_code(exc, "node_not_verified"),
        )
        return None
    source.generation += 1
    run.snapshot = {**run.snapshot, "scan_host_id": fs.host_identity()}
    run.generation = source.generation
    run.lease_token = uuid.uuid4()
    run.lease_expires_at = await now(db) + timedelta(seconds=LEASE_SECONDS)
    run.status = "running"
    run.phase = "resume_validation"
    job.status = "running"
    job.started_at = await now(db)
    return (run.generation, run.lease_token)


def failure_code(exc, fallback):
    from app.services.content_snapshot import ContentError

    if getattr(exc, "storage_operation_unsettled", False):
        return "storage_operation_unsettled"
    if isinstance(exc, (fs.FilesystemStorageError, ContentError)):
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
        return work
    base = select(DatasetScanEntry).where(
        DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "directory"
    )
    if run.phase in {"resume_validation", "validating"}:
        query = base.where(
            DatasetScanEntry.state == "captured",
            DatasetScanEntry.validated_generation != run.generation,
        )
    else:
        query = base.where(DatasetScanEntry.state == "pending")
    entry = await db.scalar(query.order_by(DatasetScanEntry.path_digest).limit(1))
    if entry is None:
        if run.phase == "validating":
            await finish(db, run, job, "completed")
            return None
        if run.phase == "resume_validation":
            run.phase = "enumerating"
        else:
            run.phase = "validating"
            await db.execute(
                update(DatasetScanEntry)
                .where(
                    DatasetScanEntry.run_id == run.id,
                    DatasetScanEntry.kind == "directory",
                )
                .values(validated_generation=0)
            )
        return {"transition": True}
    run.lease_expires_at = await now(db) + timedelta(seconds=LEASE_SECONDS)
    job.payload = {**job.payload, "stage": run.phase}
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

    work["_fault_observation"] = await scan_fault_observation(
        db, location, source, run, job, work
    )
    return work


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

        return await apply_page(db, source, run, job, work, manifest)
    entry = await db.get(
        DatasetScanEntry, (run.id, work["path_digest"]), populate_existing=True
    )
    if run.phase != work["phase"] or entry is None:
        raise LostScanLease()
    if work["phase"] != "enumerating":
        if entry.directory_digest != manifest["digest"]:
            raise fs.FilesystemStorageError("directory_changed")
        entry.validated_generation = run.generation
        return
    if entry.state != "pending":
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
                "run_id": run.id,
                "path_digest": path_digest(path),
                "relative_path": path,
                "kind": item["kind"],
                "state": state,
                "observed": {
                    key: value for key, value in item.items() if key != "name"
                },
            }
        )
    count = await db.scalar(
        select(func.count())
        .select_from(DatasetScanEntry)
        .where(DatasetScanEntry.run_id == run.id)
    )
    directories = await db.scalar(
        select(func.count())
        .select_from(DatasetScanEntry)
        .where(DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "directory")
    )
    if (
        count - 1 + len(rows) > run.snapshot["budget"]["max_entries"]
        or directories + sum(item["kind"] == "directory" for item in rows)
        > run.snapshot["budget"]["max_directories"]
    ):
        raise fs.FilesystemStorageError("scan_budget_exceeded")
    if not entry.relative_path:
        if (
            source.root_identity is not None
            and source.root_identity != manifest["identity"]
        ):
            raise fs.FilesystemStorageError("directory_identity_changed")
        source.root_identity = manifest["identity"]
    for offset in range(0, len(rows), 200):
        await db.execute(
            insert(DatasetScanEntry)
            .values(rows[offset : offset + 200])
            .on_conflict_do_nothing()
        )
    entry.state = "captured"
    entry.observed = {"identity": manifest["identity"]}
    entry.directory_digest = manifest["digest"]
    await db.flush()
    grouped = (
        await db.execute(
            select(DatasetScanEntry.kind, DatasetScanEntry.state, func.count())
            .where(
                DatasetScanEntry.run_id == run.id, DatasetScanEntry.relative_path != ""
            )
            .group_by(DatasetScanEntry.kind, DatasetScanEntry.state)
        )
    ).all()
    run.counts = {
        "processed_count": sum(n for _, _, n in grouped),
        "file_count": sum(
            n for kind, state, n in grouped if kind == "file" and state == "observed"
        ),
        "directory_count": sum(n for kind, _, n in grouped if kind == "directory"),
        "skipped_count": sum(n for _, state, n in grouped if state == "skipped"),
    }
    run.lease_expires_at = await now(db) + timedelta(seconds=LEASE_SECONDS)
    job.result = {**run.counts, "complete": False}


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
