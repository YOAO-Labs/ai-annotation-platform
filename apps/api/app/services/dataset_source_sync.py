"""Durable periodic wakeups; existing domain runs own all file work."""

from datetime import timedelta
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.dataset_source import (
    DatasetSourceSyncSchedule as Schedule,
    DatasetScanRun as Scan,
    DatasetRegistrationRun as Registration,
    DatasetSourceChange as Change,
)
from app.db.models.dataset_asset import StorageLocationProbe
from app.db.models.async_job import AsyncJob
from app.services import dataset_scans as scans, dataset_registration as registrations
from app.services import storage_location as locations, storage_access
from app.services import dataset_source_access as access
from app.services.dataset_content import key_authority
from app.services.filesystem_storage import FilesystemStorageError
from app.services.site_agents import SiteAgentError

CHECK_SECONDS = 60
DISPATCH_SECONDS = 90
BATCH_SIZE = 100
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
    row = await db.scalar(
        select(Schedule)
        .where(Schedule.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return location, source, row


async def configure(db, source_id, payload, actor, key_id=None):
    location, source, row = await context(db, source_id)
    await access.require_source(db, source.id, actor)
    proof = await key_authority(db, actor, key_id, "datasets:write")
    dataset = await scans.require_dataset_write(db, source)
    if (row.revision if row else 0) != payload.expected_revision:
        raise HTTPException(409, detail={"code": "schedule_revision_changed"})
    if row is None:
        row = Schedule(
            source_id=source.id,
            actor_id=actor.id,
            revision=0,
            enabled=False,
            state="idle",
            snapshot={},
        )
        db.add(row)
    stamp = await scans.now(db)
    if payload.enabled:
        await key_authority(db, actor, key_id, "datasets:read")
        await scans.validate_write_context(db, location, source)
        anchor = await locations.node_anchor(
            db, location, f"worker:{source.config['node_id']}"
        )
        if anchor is None:
            raise HTTPException(409, detail={"code": "worker_anchor_unverified"})
        delegation_changed = (row.actor_id, row.api_key_id, row.key_proof) != (
            actor.id,
            key_id,
            proof,
        )
        snapshot = {
            "source_revision": source.config_revision,
            "location_revision": location.config_revision,
            "maintenance_revision": await storage_access.check(
                db, location.id, lock=True
            ),
            "dataset_lifecycle_revision": dataset.lifecycle_revision,
            "anchor": anchor,
        }
        lifecycle_changed = (
            row.snapshot.get("dataset_lifecycle_revision", 1)
            != dataset.lifecycle_revision
            or row.snapshot.get("maintenance_revision", 1)
            != snapshot["maintenance_revision"]
        )
        if lifecycle_changed:
            # A renewed delegation cannot adopt observations or jobs from before
            # an archive/restore or maintenance cycle. Old attempts keep their fence.
            row.scan_id = row.registration_id = None
            row.state = "idle"
        if delegation_changed or row.snapshot != snapshot or row.state == "blocked":
            scan = await db.get(Scan, row.scan_id) if row.scan_id else None
            registration = (
                await db.get(Registration, row.registration_id)
                if row.registration_id
                else None
            )
            if (
                registration
                and registration.phase == "registering"
                and registration.status in scans.ACTIVE
            ):
                row.state = "registering"
            elif (
                registration
                and registration.phase == "preflight"
                and registration.status == "completed"
            ):
                row.state = "awaiting_review"
            elif (
                delegation_changed
                and row.state != "blocked"
                and any(
                    run and run.status in scans.ACTIVE for run in (scan, registration)
                )
            ):
                raise HTTPException(
                    409, detail={"code": "schedule_cycle_authority_pinned"}
                )
            else:
                row.state = "idle"
        row.snapshot = snapshot
        row.actor_id, row.api_key_id, row.key_proof = actor.id, key_id, proof
        if row.state == "awaiting_review":
            row.next_run_at = None
        elif not row.enabled or row.next_run_at is None:
            row.next_run_at = stamp
        row.last_error = None
    row.revision += 1
    row.enabled, row.interval_minutes = payload.enabled, payload.interval_minutes
    row.new_file_policy, row.timezone = payload.new_file_policy, payload.timezone
    row.dispatch_token = row.dispatch_expires_at = None
    row.next_check_at = stamp
    await db.flush()
    return row


async def continue_cycle(db, source_id, payload, actor, key_id=None):
    location, source, row = await context(db, source_id)
    await access.require_source(db, source.id, actor)
    await key_authority(db, actor, key_id, "datasets:write")
    if row is not None:
        await scans.validate_write_context(db, location, source, snapshot=row.snapshot)
    else:
        await scans.require_dataset_write(db, source)
    if (
        row is None
        or row.revision != payload.expected_revision
        or row.scan_id != payload.scan_id
    ):
        raise HTTPException(409, detail={"code": "schedule_revision_changed"})
    run = await db.get(Registration, row.registration_id)
    if (
        not row.enabled
        or row.state != "awaiting_review"
        or run is None
        or run.phase != "preflight"
        or run.status != "completed"
    ):
        raise HTTPException(409, detail={"code": "schedule_review_changed"})
    row.revision += 1
    finish_cycle(row, await scans.now(db))
    row.dispatch_token = row.dispatch_expires_at = None
    await db.flush()
    return row


def finish_cycle(row, stamp, error=None):
    row.state, row.last_error = "idle", error
    row.next_run_at = stamp + timedelta(minutes=row.interval_minutes)
    row.next_check_at = row.next_run_at


async def authority(db, location, source, row):
    actor = await scans.require_actor(db, row.actor_id, source)
    for scope in ("datasets:read", "datasets:write"):
        if await key_authority(db, actor, row.api_key_id, scope) != row.key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    await scans.validate_write_context(db, location, source, snapshot=row.snapshot)
    return actor


async def require_run_authority(db, run, row):
    job = (
        await db.get(AsyncJob, run.async_job_id, populate_existing=True)
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
    stamp = await scans.now(db)
    if (
        row is None
        or not row.enabled
        or row.next_check_at > stamp
        or (row.dispatch_expires_at and row.dispatch_expires_at > stamp)
    ):
        return None
    row.dispatch_token = uuid.uuid4()
    row.dispatch_expires_at = stamp + timedelta(seconds=DISPATCH_SECONDS)
    row.next_check_at = row.dispatch_expires_at
    return (row.source_id, row.revision, row.dispatch_token, source.config["node_id"])


async def due_ids(db):
    return list(
        await db.scalars(
            select(Schedule.source_id)
            .where(
                Schedule.enabled.is_(True),
                Schedule.next_check_at <= await scans.now(db),
            )
            .order_by(Schedule.next_check_at, Schedule.source_id)
            .limit(BATCH_SIZE)
        )
    )


async def record_failure(db, source_id, revision, token, code):
    _, _, row = await context(db, source_id)
    if row is None or row.revision != revision or row.dispatch_token != token:
        return
    row.dispatch_token = row.dispatch_expires_at = None
    row.last_checked_at, row.last_error = await scans.now(db), code[:60]
    row.next_check_at = row.last_checked_at + timedelta(minutes=row.interval_minutes)
    if code in BLOCKING:
        row.enabled, row.state = False, "blocked"


async def action(db, source, row):
    operation = await choose_action(db, source, row)
    if operation:
        other_scan = await db.scalar(
            select(Scan.id)
            .where(
                Scan.source_id == source.id,
                Scan.id != row.scan_id if row.scan_id else True,
                Scan.status.in_(scans.ACTIVE),
            )
            .limit(1)
        )
        other_registration = await db.scalar(
            select(Registration.id)
            .where(
                Registration.source_id == source.id,
                Registration.id != row.registration_id if row.registration_id else True,
                Registration.status.in_(scans.ACTIVE),
            )
            .limit(1)
        )
        if other_scan or other_registration:
            row.last_error = "source_busy"
            return None
    return operation


async def choose_action(db, source, row):
    stamp = await scans.now(db)
    if row.state == "idle":
        if row.next_run_at and row.next_run_at > stamp:
            row.next_check_at = row.next_run_at
            return None
        busy = await db.scalar(
            select(Scan.id)
            .where(Scan.source_id == source.id, Scan.status.in_(scans.ACTIVE))
            .limit(1)
        )
        busy = busy or await db.scalar(
            select(Registration.id)
            .where(
                Registration.source_id == source.id,
                Registration.status.in_(scans.ACTIVE),
            )
            .limit(1)
        )
        if busy:
            row.last_error = "source_busy"
            return None
        return "start_scan"
    if row.state == "scanning":
        run = await db.get(Scan, row.scan_id, populate_existing=True)
        await require_run_authority(db, run, row)
        if run.status == "completed" and run.complete:
            manual = await db.scalar(
                select(Registration).where(Registration.scan_run_id == run.id)
            )
            if manual is not None:
                if manual.status in {"failed", "cancelled"} or (
                    manual.phase == "registering" and manual.status == "completed"
                ):
                    finish_cycle(row, stamp)
                else:
                    row.last_error = "source_busy"
                return None
            return "start_preflight"
        if run.status in {"cancelled", "failed"}:
            finish_cycle(row, stamp, run.error_code or f"scan_{run.status}")
        elif run.status == "pending":
            return "dispatch_scan"
        elif run.lease_expires_at and run.lease_expires_at <= stamp:
            return "resume_scan"
    else:
        run = await db.get(Registration, row.registration_id, populate_existing=True)
        # Only an explicitly automatic run under this exact delegation is ours
        # to recover. Human confirmation remains observation-only.
        if run.phase == "registering":
            if run.status in {"completed", "cancelled", "failed"}:
                finish_cycle(row, stamp, run.error_code)
            else:
                row.state = "registering"
                if run.snapshot.get("sync_auto_ingestion") and owns_run(run, row):
                    await require_run_authority(db, run, row)
                    if run.status == "pending":
                        return "dispatch_registration"
                    if run.lease_expires_at and run.lease_expires_at <= stamp:
                        return "resume_registration"
            return None
        if row.state == "awaiting_review" and run.status == "completed":
            return None
        await require_run_authority(db, run, row)
        if run.status in {"cancelled", "failed"}:
            finish_cycle(row, stamp, run.error_code or f"preflight_{run.status}")
        elif run.status == "completed":
            if run.phase == "registering":
                finish_cycle(row, stamp)
            elif row.state != "awaiting_review":
                if await pure_additions(db, run, row):
                    return "confirm_additions"
                attention = await db.scalar(
                    select(func.count())
                    .select_from(Change)
                    .where(Change.run_id == run.id, Change.kind != "unchanged")
                )
                if attention:
                    row.state, row.next_run_at = "awaiting_review", None
                else:
                    finish_cycle(row, stamp)
        elif run.status == "pending":
            return "dispatch_registration"
        elif run.lease_expires_at and run.lease_expires_at <= stamp:
            return "resume_registration"
    return None


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
    kinds = set(
        await db.scalars(select(Change.kind).where(Change.run_id == run.id).distinct())
    )
    return "added" in kinds and kinds <= {"added", "unchanged"}


async def process(db, source_id, revision, token, node):
    location, source, row, actor = await fenced(db, source_id, revision, token, node)
    stamp = await scans.now(db)
    row.next_check_at = stamp + timedelta(seconds=CHECK_SECONDS)
    operation = await action(db, source, row)
    # Idle/review observations are committed before any broker publication.
    if operation:
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
        stored = await db.get(
            StorageLocationProbe,
            (location.id, probe_node),
            populate_existing=True,
        )
        if not stored.result["ok"]:
            raise FilesystemStorageError(stored.result["code"])
        anchor = await locations.node_anchor(db, location, f"worker:{node}")
        if anchor != row.snapshot["anchor"]:
            raise FilesystemStorageError("root_identity_changed")
        await locations.require_scan_node(db, location, f"worker:{node}")
        # Source serialization rechecks manual conflicts after probe I/O.
        operation = await action(db, source, row)
        if operation == "start_scan":
            run, created = await scans.start_scan(
                db, source.id, actor, api_key_id=row.api_key_id
            )
            if not created:
                raise LostWakeup()
            run.snapshot = {
                **run.snapshot,
                "sync_source_id": str(source.id),
                "sync_actor_id": str(row.actor_id),
                "sync_new_file_policy": row.new_file_policy,
            }
            row.scan_id, row.registration_id = run.id, None
            row.state, row.last_started_at = "scanning", await scans.now(db)
        elif operation == "resume_scan":
            run = await scans.resume_scan(
                db, row.scan_id, actor, api_key_id=row.api_key_id
            )
        elif operation == "start_preflight":
            scan = await db.get(Scan, row.scan_id)
            pinned_policy = scan.snapshot.get("sync_new_file_policy", "manual")
            run, created = await registrations.start_preflight(
                db, row.scan_id, actor, api_key_id=row.api_key_id, worker_node=node
            )
            if not created:
                raise LostWakeup()
            run.snapshot = {
                **run.snapshot,
                "sync_source_id": str(source.id),
                "sync_actor_id": str(row.actor_id),
                "sync_new_file_policy": pinned_policy,
            }
            row.registration_id, row.state = run.id, "preflighting"
        elif operation == "confirm_additions":

            async def guard(current_db):
                _, _, current, _ = await fenced(
                    current_db, source_id, revision, token, node
                )
                current_run = await current_db.get(
                    Registration, current.registration_id, populate_existing=True
                )
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
            run.snapshot = {**run.snapshot, "sync_auto_ingestion": True}
            row.state = "registering"
        elif operation == "resume_registration":
            run = await registrations.resume(
                db, row.registration_id, actor, api_key_id=row.api_key_id
            )
        if operation:
            # Nested domain entry points capture current credentials. Reject a
            # concurrent key rotation instead of silently renewing the delegation.
            await authority(db, location, source, row)
            await db.flush()
            current_run = await db.get(
                Scan
                if "scan" in operation and operation != "start_preflight"
                else Registration,
                row.scan_id if "scan" in operation else row.registration_id,
                populate_existing=True,
            )
            await require_run_authority(db, current_run, row)
            row.last_error = None
        row.next_check_at = await scans.now(db) + timedelta(seconds=CHECK_SECONDS)
    row.dispatch_token = row.dispatch_expires_at = None
    row.last_checked_at = await scans.now(db)
    scan_id, registration_id = row.scan_id, row.registration_id
    await db.commit()
    # Persistent pointers close every commit-before-publish gap; publication is replayable.
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
