"""Durable periodic wakeups; existing domain runs own all file work."""

import uuid

from fastapi import HTTPException

from app.db.models.dataset_source import DatasetRegistrationRun as Registration
from app.services import async_job as jobs
from app.services import dataset_registration as registrations
from app.services import dataset_registration_data as data
from app.services import dataset_scan_data
from app.services import dataset_scans as scans
from app.services import dataset_source_access as access
from app.services import storage_access, storage_location as locations
from app.services.dataset_content import key_authority
from app.services.filesystem_storage import FilesystemStorageError
from app.services.site_agents import SiteAgentError
from app.services.storage_registry_data import get_probe

BLOCKING = {
    "access_revoked",
    "api_key_authority_revoked",
    "configuration_changed",
    "root_identity_changed",
    "root_not_configured",
    "sftp_root_not_qualified",
    "sftp_host_key_mismatch",
    "source_unavailable",
    "location_unavailable",
    "run_authority_changed",
    "dataset_archived",
    "dataset_lifecycle_changed",
    "storage_maintenance",
    "storage_maintenance_changed",
    "storage_maintenance_busy",
    "storage_operation_unsettled",
    "agent_revoked",
    "agent_unpaired",
    "agent_unauthorized",
    "credential_expired",
    "credential_revision_changed",
    "agent_protocol_incompatible",
    "agent_identity_mismatch",
    "agent_root_revision_changed",
    "root_revision_changed",
    "agent_location_changed",
}


class LostWakeup(Exception):
    pass


async def context(db, source_id):
    location, source = await scans.source_context(db, source_id)
    return location, source, await data.read_schedule(db, source_id, lock=True)


async def configure(db, source_id, payload, actor, key_id=None):
    location, source, row = await context(db, source_id)
    await access.require_source(db, source.id, actor)
    proof = await key_authority(db, actor, key_id, "datasets:write")
    dataset = await scans.require_dataset_write(db, source)
    if (row.revision if row else 0) != payload.expected_revision:
        raise HTTPException(409, detail={"code": "schedule_revision_changed"})
    stamp = await scans.now(db)
    anchor = maintenance_revision = None
    if payload.enabled:
        await key_authority(db, actor, key_id, "datasets:read")
        await scans.validate_write_context(db, location, source)
        anchor = await locations.node_anchor(
            db, location, f"worker:{source.config['node_id']}"
        )
        if anchor is None:
            raise HTTPException(409, detail={"code": "worker_anchor_unverified"})
        maintenance_revision = await storage_access.check(db, location.id, lock=True)
    code = await data.configure_sync(
        db,
        source_id,
        payload,
        actor.id,
        key_id,
        proof,
        anchor,
        maintenance_revision,
        dataset.lifecycle_revision,
        stamp,
    )
    if code:
        raise HTTPException(
            403 if code == "access_revoked" else 409, detail={"code": code}
        )
    return await data.read_schedule(db, source_id)


async def continue_cycle(db, source_id, payload, actor, key_id=None):
    location, source, row = await context(db, source_id)
    await access.require_source(db, source.id, actor)
    await key_authority(db, actor, key_id, "datasets:write")
    if row is not None:
        await scans.validate_write_context(db, location, source, snapshot=row.snapshot)
    else:
        await scans.require_dataset_write(db, source)
    code = await data.continue_sync(db, source_id, payload, await scans.now(db))
    if code:
        raise HTTPException(409, detail={"code": code})
    return await data.read_schedule(db, source_id)


async def authority(db, location, source, row):
    actor = await scans.require_actor(db, row.actor_id, source)
    for scope in ("datasets:read", "datasets:write"):
        if await key_authority(db, actor, row.api_key_id, scope) != row.key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await scans.validate_write_context(db, location, source, snapshot=row.snapshot)
    return actor


async def require_run_authority(db, run, row):
    job = (
        await jobs.get_job(db, run.async_job_id, populate_existing=True)
        if run.async_job_id
        else None
    )
    snapshot = run.snapshot
    if (
        (job is not None and job.user_id != row.actor_id)
        or (job is None and run.status in scans.ACTIVE)
        or snapshot.get("sync_actor_id") != str(row.actor_id)
        or (
            isinstance(run, Registration)
            and snapshot.get("actor_id") != str(row.actor_id)
        )
        or snapshot.get("sync_source_id") != str(row.source_id)
        or snapshot.get("api_key_id")
        != (str(row.api_key_id) if row.api_key_id else None)
        or snapshot.get("key_proof") != row.key_proof
        or snapshot.get("dataset_lifecycle_revision", 1)
        != row.snapshot.get("dataset_lifecycle_revision", 1)
        or snapshot.get("maintenance_revision", 1)
        != row.snapshot.get("maintenance_revision", 1)
    ):
        raise HTTPException(409, detail={"code": "run_authority_changed"})


async def fenced(db, source_id, revision, token, node):
    location, source, row = await context(db, source_id)
    if (
        row is None
        or not row.enabled
        or row.revision != revision
        or row.dispatch_token != token
        or row.dispatch_expires_at is None
        or row.dispatch_expires_at <= await scans.now(db)
    ):
        raise LostWakeup()
    if node != source.config["node_id"]:
        raise LostWakeup()
    actor = await authority(db, location, source, row)
    return location, source, row, actor


async def claim_due(db, source_id):
    _, source, row = await context(db, source_id)
    if row is None:
        return None
    token = uuid.uuid4()
    if not await data.claim_sync(db, source_id, token, await scans.now(db)):
        return None
    await data.read_schedule(db, source_id)
    return source_id, row.revision, token, source.config["node_id"]


async def due_ids(db):
    return await data.due_sources(db, await scans.now(db))


async def record_failure(db, source_id, revision, token, code):
    await context(db, source_id)
    await data.fail_sync(
        db, source_id, revision, token, code, code in BLOCKING, await scans.now(db)
    )
    await data.read_schedule(db, source_id)


async def action(db, source, row):
    operation, outcome, error = await choose_action(db, source, row)
    if operation and await source_busy(
        db, source.id, scan_id=row.scan_id, registration_id=row.registration_id
    ):
        return None, "busy", None
    return operation, outcome, error


async def source_busy(db, source_id, *, scan_id=None, registration_id=None):
    return (
        await dataset_scan_data.active_scan_id(db, source_id, except_run_id=scan_id)
        is not None
        or await data.active_run_id(db, source_id, except_id=registration_id)
        is not None
    )


async def choose_action(db, source, row):
    stamp = await scans.now(db)
    if row.state == "idle":
        if row.next_run_at and row.next_run_at > stamp:
            return None, "not_due", None
        if await source_busy(db, source.id):
            return None, "busy", None
        return "start_scan", "started_scan", None
    if row.state == "scanning":
        run = await dataset_scan_data.execution_run(db, row.scan_id)
        await require_run_authority(db, run, row)
        if run.status == "completed" and run.complete:
            manual = await data.for_scan(db, run.id)
            if manual is not None:
                if manual.status in {"failed", "cancelled"} or (
                    manual.phase == "registering" and manual.status == "completed"
                ):
                    return None, "finished_cycle", None
                return None, "busy", None
            return "start_preflight", "started_preflight", None
        if run.status in {"cancelled", "failed"}:
            return None, "finished_cycle", run.error_code or f"scan_{run.status}"
        if run.status == "pending":
            return "dispatch_scan", "continued_run", None
        if run.lease_expires_at and run.lease_expires_at <= stamp:
            return "resume_scan", "continued_run", None
        return None, "unchanged", None

    run = await data.read_run(db, row.registration_id)
    if run.phase == "registering":
        if run.status in {"completed", "cancelled", "failed"}:
            return None, "finished_cycle", run.error_code
        if run.snapshot.get("sync_auto_ingestion") and owns_run(run, row):
            await require_run_authority(db, run, row)
            if run.status == "pending":
                return "dispatch_registration", "continued_run", None
            if run.lease_expires_at and run.lease_expires_at <= stamp:
                return "resume_registration", "continued_run", None
        return None, "observe_registration", None
    if row.state == "awaiting_review" and run.status == "completed":
        return None, "unchanged", None
    await require_run_authority(db, run, row)
    if run.status in {"cancelled", "failed"}:
        return None, "finished_cycle", run.error_code or f"preflight_{run.status}"
    if run.status == "completed":
        if await pure_additions(db, run, row):
            return "confirm_additions", "confirmed_additions", None
        if await data.change_kinds(db, run.id) - {"unchanged"}:
            return None, "awaiting_review", None
        return None, "finished_cycle", None
    if run.status == "pending":
        return "dispatch_registration", "continued_run", None
    if run.lease_expires_at and run.lease_expires_at <= stamp:
        return "resume_registration", "continued_run", None
    return None, "unchanged", None


def owns_run(run, row):
    snapshot = run.snapshot
    return (
        snapshot.get("sync_actor_id") == str(row.actor_id)
        and snapshot.get("api_key_id")
        == (str(row.api_key_id) if row.api_key_id else None)
        and snapshot.get("key_proof") == row.key_proof
        and snapshot.get("sync_source_id") == str(row.source_id)
        and snapshot.get("dataset_lifecycle_revision", 1)
        == row.snapshot.get("dataset_lifecycle_revision", 1)
        and snapshot.get("maintenance_revision", 1)
        == row.snapshot.get("maintenance_revision", 1)
    )


async def pure_additions(db, run, row):
    if (
        row.new_file_policy != "pure_additions"
        or run.snapshot.get("sync_new_file_policy") != "pure_additions"
        or not run.comparison_complete
        or run.counts.get("error_count", 0)
    ):
        return False
    kinds = await data.change_kinds(db, run.id)
    return "added" in kinds and kinds <= {"added", "unchanged"}


async def process(db, source_id, revision, token, node):
    location, source, row, actor = await fenced(db, source_id, revision, token, node)
    operation, outcome, error = await action(db, source, row)
    if operation:
        if not await data.settle_sync(db, row, "probing", await scans.now(db)):
            raise LostWakeup()
        await db.commit()
        try:
            result = await locations.perform_probe(
                db,
                location,
                f"worker:{node}",
                None,
                maintenance_revision=row.snapshot.get("maintenance_revision", 1),
            )
        except SiteAgentError as exc:
            failure = FilesystemStorageError(exc.code)
            if getattr(exc, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(failure)
            raise failure from None
        location, source, row, actor = await fenced(
            db, source_id, revision, token, node
        )
        probe_node = locations.probe_node_id(location, f"worker:{node}")
        await locations.store_probe(
            db, location, probe_node, result, await scans.now(db)
        )
        stored = await get_probe(db, location.id, probe_node)
        if not stored.result["ok"]:
            raise FilesystemStorageError(stored.result["code"])
        anchor = await locations.node_anchor(db, location, f"worker:{node}")
        if anchor != row.snapshot["anchor"]:
            raise FilesystemStorageError("root_identity_changed")
        await locations.require_scan_node(db, location, f"worker:{node}")
        operation, outcome, error = await action(db, source, row)

    run_id = None
    if operation == "start_scan":
        run, created = await scans.start_scan(
            db, source.id, actor, api_key_id=row.api_key_id
        )
        if not created:
            raise LostWakeup()
        run_id = run.id
    elif operation == "resume_scan":
        await scans.resume_scan(db, row.scan_id, actor, api_key_id=row.api_key_id)
    elif operation == "start_preflight":
        run, created = await registrations.start_preflight(
            db, row.scan_id, actor, api_key_id=row.api_key_id, worker_node=node
        )
        if not created:
            raise LostWakeup()
        run_id = run.id
    elif operation == "confirm_additions":

        async def guard(current_db):
            _, _, current, _ = await fenced(
                current_db, source_id, revision, token, node
            )
            current_run = await data.read_run(current_db, current.registration_id)
            await require_run_authority(current_db, current_run, current)
            if not await pure_additions(current_db, current_run, current):
                raise LostWakeup()

        run, created = await registrations.confirm(
            db,
            row.registration_id,
            actor,
            api_key_id=row.api_key_id,
            scope="additions_only",
            worker_node=node,
            before_commit=guard,
        )
        if not created:
            raise LostWakeup()
        run_id = run.id
    elif operation == "resume_registration":
        await registrations.resume(
            db, row.registration_id, actor, api_key_id=row.api_key_id
        )

    if operation:
        await authority(db, location, source, row)
    if not await data.settle_sync(
        db, row, outcome, await scans.now(db), run_id=run_id, error=error
    ):
        raise LostWakeup()
    row = await data.read_schedule(db, source_id)
    scan_id, registration_id = row.scan_id, row.registration_id
    if operation:
        current_run = (
            await dataset_scan_data.execution_run(db, scan_id)
            if operation in {"start_scan", "resume_scan", "dispatch_scan"}
            else await data.read_run(db, registration_id)
        )
        await require_run_authority(db, current_run, row)
    await db.commit()
    if operation in {"start_scan", "resume_scan", "dispatch_scan"}:
        from app.workers.dataset_scan import dispatch

        await dispatch(db, scan_id)
    elif operation in {
        "start_preflight",
        "resume_registration",
        "dispatch_registration",
        "confirm_additions",
    }:
        from app.workers.dataset_registration import dispatch

        await dispatch(db, registration_id)
