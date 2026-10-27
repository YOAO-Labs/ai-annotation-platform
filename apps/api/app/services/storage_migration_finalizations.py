"""Trusted local operator control; no endpoint imports physical proof or runs Docker.

Callers own commit/rollback. Native inspection and checkpoint materialization must
run outside these short database transactions while holding the private Journal.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import undefer

from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.storage_migration_copy import (
    StorageMigrationCopy as Copy,
    StorageMigrationCopyAttempt as Attempt,
)
from app.db.models.storage_migration_finalization import (
    StorageMigrationFinalization as Finalization,
)
from app.db.models.storage_migration_inventory import (
    StorageMigrationInventory as Inventory,
)
from app.db.models.user import User
from app.schemas.storage_migration_window import StorageMigrationFinalizationItem
from app.services import (
    storage_access,
    storage_migration_copies as copies,
    storage_migration_inventories as inventories,
    storage_migration_windows as windows,
)

CLAIM_SCHEMA = "aap.storage-migration-fence-claim/v1"
EVIDENCE_SCHEMA = "aap.storage-migration-final-evidence/v1"
CLAIM_FIELDS = {
    "schema",
    "finalization_id",
    "window_id",
    "expected_window_revision",
    "database_identity",
    "reservation_nonce",
    "native_operation_id",
    "config_digest",
    "reserved",
}
COUNT_FIELDS = {
    "current_objects",
    "current_delete_markers",
    "copied_objects",
    "retained_objects",
    "rebuild_objects",
    "verified_target_bytes",
    "source_buckets",
    "source_verified_bytes",
    "mutable_null_objects",
    "unpinned_objects",
    "over_object_limit",
    "exact_catalog_matches",
    "previous_only_objects",
    "history_records",
}
ACTIVE = ("bound", "sealing", "sealed", "invalidating")


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _counts(value):
    return (
        isinstance(value, dict)
        and len(value) <= 64
        and all(
            isinstance(key, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
            and type(number) is int
            and 0 <= number < 2**63
            for key, number in value.items()
        )
    )


def validate_claim(claim):
    if not (
        isinstance(claim, dict)
        and set(claim) == CLAIM_FIELDS
        and claim.get("schema") == CLAIM_SCHEMA
        and all(
            _uuid(claim.get(key))
            for key in (
                "finalization_id",
                "window_id",
                "reservation_nonce",
                "native_operation_id",
            )
        )
        and all(_sha(claim.get(key)) for key in ("database_identity", "config_digest"))
        and type(claim.get("expected_window_revision")) is int
        and claim["expected_window_revision"] >= 1
        and type(claim.get("reserved")) is bool
    ):
        reject("finalization_invalid_claim")
    return deepcopy(claim)


async def identity(db):
    value = (
        (
            await db.execute(
                text(
                    "SELECT current_database() AS database, (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS oid, inet_server_addr()::text AS address, inet_server_port() AS port"
                )
            )
        )
        .mappings()
        .one()
    )
    return digest(dict(value))


async def _actor(db, actor_id):
    actor = await db.get(User, actor_id, populate_existing=True)
    if actor is None:
        reject("finalization_authority_changed", 403)
    try:
        actor, _ = await windows.authority(db, actor, None, lock=True)
    except HTTPException as exc:
        if exc.status_code == 403:
            reject("finalization_authority_changed", 403)
        raise
    return actor


async def _lock(db, query):
    try:
        return await db.scalar(
            query.with_for_update(nowait=True).execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("finalization_busy")
        raise


async def _window(db, window_id, *, context=False):
    initial = await windows.owner(db, window_id)
    if context:
        ids = sorted({initial.source_location_id, initial.target_location_id})
        connections = select(StorageLocation.connection_id).where(
            StorageLocation.id.in_(ids), StorageLocation.connection_id.is_not(None)
        )
        try:
            await db.execute(
                select(StorageConnection.id)
                .where(StorageConnection.id.in_(connections))
                .order_by(StorageConnection.id)
                .with_for_update(read=True, nowait=True)
            )
            await db.execute(
                select(StorageLocation.id)
                .where(StorageLocation.id.in_(ids))
                .order_by(StorageLocation.id)
                .with_for_update(read=True, nowait=True)
            )
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("finalization_busy")
            raise
    row = await windows.owner(db, window_id, lock=True, exclusive=True)
    if context:
        current = await inventories.source_scope(db)
        if (
            row.state != "stopped"
            or current != row.source
            or current["location_revision"] != row.source_config_revision
        ):
            reject("finalization_changed")
        await windows.gate(db, row, exclusive=True)
        from app.services import storage_migration_copy_target as targets

        target, _, _, _ = await targets.resolve(db, row.target_location_id)
        if target != row.target:
            reject("finalization_changed")
        await inventories.gate_revision(db, row.target_location_id, lock=True)
    return row


async def _copy(db, copy_id, *, private=False):
    query = select(Copy).where(Copy.id == copy_id)
    if private:
        query = query.options(
            undefer(Copy.source_report), undefer(Copy.catalog), undefer(Copy.baseline)
        )
    row = await _lock(db, query)
    if row is None:
        reject("finalization_copy_changed")
    return row


async def _settled(db, window):
    relevant = select(Copy.id).where(
        (Copy.window_id == window.id) | (Copy.id == window.parent_copy_id)
    )
    attempts = list(
        await db.scalars(select(Attempt).where(Attempt.copy_id.in_(relevant)))
    )
    if any(row.status in inventories.ACTIVE for row in attempts) or await db.scalar(
        select(Inventory.async_job_id)
        .where(
            Inventory.window_id == window.id, Inventory.status.in_(inventories.ACTIVE)
        )
        .limit(1)
    ):
        reject("finalization_unsettled")
    operation_ids = [
        value
        for row in attempts
        for value in (row.source_operation_id, row.target_operation_id)
        if value
    ]
    if await db.scalar(
        select(StorageOperation.id)
        .where(
            (StorageOperation.location_id == window.source_location_id)
            | StorageOperation.id.in_(operation_ids)
        )
        .limit(1)
    ):
        reject("finalization_unsettled")


def _native(native, claim, source, previous=None):
    keys = {
        "config_digest",
        "operation_id",
        "journal_revision",
        "barriers_sha256",
        "observed_at",
        "counts",
        "source",
    }
    try:
        stamp = datetime.fromisoformat(native["observed_at"].replace("Z", "+00:00"))
        valid = (
            isinstance(native, dict)
            and set(native) == keys
            and native["config_digest"] == claim["config_digest"]
            and native["operation_id"] == claim["native_operation_id"]
            and type(native["journal_revision"]) is int
            and native["journal_revision"] >= 1
            and _sha(native["barriers_sha256"])
            and stamp.utcoffset() is not None
            and _counts(native["counts"])
            and windows.physical_source(native["source"])
            == windows.physical_source(source)
            and len(json.dumps(native).encode()) <= 65536
        )
    except (ValueError, TypeError, KeyError, AttributeError, HTTPException):
        valid = False
    if not valid:
        reject("finalization_native_changed")
    # Journal claim writes legitimately advance its revision, and a live status
    # has a new timestamp. Physical operation/config/barriers/source stay exact.
    if previous and any(
        native[key] != previous[key]
        for key in ("config_digest", "operation_id", "barriers_sha256", "source")
    ):
        reject("finalization_native_changed")
    return deepcopy(native)


def _same_claim(row, claim):
    if {k: v for k, v in row.claim.items() if k != "reserved"} != {
        k: v for k, v in claim.items() if k != "reserved"
    }:
        reject("finalization_changed")


async def _claim(db, actor_id, claim):
    claim = validate_claim(claim)
    await _actor(db, actor_id)
    if await identity(db) != claim["database_identity"]:
        reject("finalization_database_changed")
    return claim


def _cas(row, revision):
    if type(revision) is not int or revision != row.revision:
        reject("finalization_changed")


async def _change(db, row, state, **fields):
    row.state, row.revision = state, row.revision + 1
    row.updated_at = await db.scalar(select(func.statement_timestamp()))
    for key, value in fields.items():
        setattr(row, key, value)
    await db.flush()


def item(row):
    return StorageMigrationFinalizationItem(
        finalization_id=row.id,
        revision=row.revision,
        state=row.state,
        window_revision=row.window_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        native_observed_at=row.native.get("observed_at"),
        sealed_at=row.sealed_at,
        invalidated_at=row.invalidated_at,
        inventory_job_id=row.inventory_job_id,
        copy_id=row.copy_id,
        counts=(row.evidence or {}).get("counts"),
        error_code=row.error_code,
    )


async def public_for_window(db, window_id):
    row = await db.scalar(
        select(Finalization)
        .where(Finalization.window_id == window_id)
        .order_by(Finalization.created_at.desc(), Finalization.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return item(row) if row else None


async def require_window_open(db, window_id, *, closing=False):
    states = ACTIVE if closing else ("invalidating",)
    if await db.scalar(
        select(Finalization.id)
        .where(Finalization.window_id == window_id, Finalization.state.in_(states))
        .limit(1)
    ):
        reject("finalization_active" if closing else "finalization_closing")


async def require_resumable(db, location_id):
    if await db.scalar(
        select(Finalization.id)
        .where(
            Finalization.source_location_id == location_id,
            Finalization.state.in_(ACTIVE),
        )
        .limit(1)
    ):
        reject("finalization_active")


async def frozen_copy(db, copy_id):
    from app.services import storage_migration_read_publications as publications

    if await publications.frozen_copy(db, copy_id):
        return True
    return (
        await db.scalar(
            select(Finalization.id)
            .where(
                Finalization.copy_id == copy_id,
                Finalization.state.in_(("sealing", "sealed")),
            )
            .limit(1)
        )
        is not None
    )


async def preflight(db, actor_id, window_id, expected_revision):
    await _actor(db, actor_id)
    window = await _window(db, window_id, context=True)
    if type(expected_revision) is not int or window.revision != expected_revision:
        reject("finalization_changed")
    if await db.scalar(
        select(Finalization.id)
        .where(Finalization.window_id == window.id, Finalization.state.in_(ACTIVE))
        .limit(1)
    ):
        reject("finalization_active")
    parent = await _copy(db, window.parent_copy_id)
    if (
        parent.baseline_sha256 is None
        or parent.source != window.source
        or parent.target != window.target
    ):
        # Historical cosmetic source revisions remain valid, as in B6.
        if (
            parent.baseline_sha256 is None
            or windows.physical_source(parent.source)
            != windows.physical_source(window.source)
            or parent.target != window.target
        ):
            reject("finalization_changed")
    await copies.writable(db, parent)
    await _settled(db, window)
    return {
        "database_identity": await identity(db),
        "window_id": str(window.id),
        "window_revision": window.revision,
        "source_gate_revision": window.source_gate_revision,
        "source_config_revision": window.source_config_revision,
        "parent_copy_id": str(window.parent_copy_id),
        "target_location_id": str(window.target_location_id),
        "source": deepcopy(window.source),
        "target": deepcopy(window.target),
    }


async def _existing(db, claim, *, lock=True):
    query = select(Finalization).where(
        Finalization.id == uuid.UUID(claim["finalization_id"])
    )
    row = (
        await _lock(db, query)
        if lock
        else await db.scalar(query.execution_options(populate_existing=True))
    )
    if row is not None:
        _same_claim(row, claim)
    return row


def _new(window, actor_id, claim, native, *, state="bound"):
    return Finalization(
        id=uuid.UUID(claim["finalization_id"]),
        window_id=window.id,
        state=state,
        revision=1,
        window_revision=window.revision,
        source_location_id=window.source_location_id,
        source=deepcopy(window.source),
        source_gate_revision=window.source_gate_revision,
        source_config_revision=window.source_config_revision,
        parent_copy_id=window.parent_copy_id,
        target_location_id=window.target_location_id,
        target=deepcopy(window.target),
        claim=deepcopy(claim),
        native=native,
        created_by=actor_id,
    )


def _bound(row, window):
    if (
        window.revision != row.window_revision
        or window.source != row.source
        or window.source_gate_revision != row.source_gate_revision
        or window.source_config_revision != row.source_config_revision
        or window.parent_copy_id != row.parent_copy_id
        or window.target_location_id != row.target_location_id
        or window.target != row.target
    ):
        reject("finalization_changed")


async def bind(db, actor_id, claim, native):
    claim = await _claim(db, actor_id, claim)
    window = await _window(db, uuid.UUID(claim["window_id"]), context=True)
    row = await _existing(db, claim)
    if row is not None:
        _bound(row, window)
        _native(native, claim, window.source, row.native)
        if row.state not in ("bound", "sealing", "sealed"):
            reject("finalization_closing")
        return item(row)
    if claim["reserved"]:
        reject("finalization_unknown")
    await preflight(db, actor_id, window.id, claim["expected_window_revision"])
    native = _native(native, claim, window.source)
    window.revision += 1
    window.updated_at = await db.scalar(select(func.statement_timestamp()))
    row = _new(window, actor_id, claim, native)
    db.add(row)
    await db.flush()
    return item(row)


def _token(copy, attempt, window):
    return {
        "schema": "aap.storage-migration-final-token/v1",
        "copy_id": str(copy.id),
        "source_report_sha256": copy.source_report_sha256,
        "catalog_sha256": copy.catalog_sha256,
        "baseline_sha256": copy.baseline_sha256,
        "delta_sequence": copy.delta_sequence,
        "delta_bytes": copy.delta_bytes,
        "last_attempt": {
            key: str(getattr(attempt, key))
            if getattr(attempt, key) is not None
            else None
            for key in (
                "id",
                "task_id",
                "operation",
                "status",
                "completed_at",
                "source_operation_id",
                "target_operation_id",
            )
        },
        "attempt_result": deepcopy(attempt.result),
        "metadata": {
            "window_id": str(window.id),
            "window_revision": window.revision,
            "source": copy.source,
            "target": copy.target,
            "parent_copy_id": str(copy.parent_copy_id),
            "parent_evidence_sha256": copy.parent_evidence_sha256,
            "generation": copy.generation,
            "handling": copy.handling,
            "limits": copy.limits,
            "source_complete": copy.source_complete,
            "summary": copy.summary,
        },
    }


async def _verified(db, row, window, copy):
    if (
        copy.parent_copy_id != row.parent_copy_id
        or copy.window_id != window.id
        or copy.window_revision != row.window_revision
        or copy.source != row.source
        or copy.target_location_id != row.target_location_id
        or copy.target != row.target
        or not copy.source_complete
        or not copy.baseline_sha256
        or not copy.catalog_sha256
        or not all(
            isinstance(getattr(copy, field), bytes)
            for field in ("source_report", "catalog", "baseline")
        )
    ):
        reject("finalization_copy_changed")
    try:
        header = json.loads(copy.source_report.split(b"\n", 1)[0])
        bound = header["observations"]["migration_window"]
    except (ValueError, TypeError, KeyError):
        reject("finalization_copy_changed")
    if bound != {
        "window_id": str(window.id),
        "window_revision": row.window_revision,
        "source_quiescence_proven": False,
    }:
        reject("finalization_copy_changed")
    attempt = await _lock(db, select(Attempt).where(Attempt.id == copy.last_attempt_id))
    if (
        attempt is None
        or attempt.copy_id != copy.id
        or attempt.operation != "verify"
        or attempt.status != "completed"
        or attempt.completed_at is None
        or attempt.cancel_requested_at is not None
        or attempt.error_code is not None
        or attempt.retry_failed
        or attempt.retry_object_ids is not None
        or attempt.source_operation_id is None
        or attempt.target_operation_id is None
    ):
        reject("finalization_verify_required")
    summary = copy.summary if isinstance(copy.summary, dict) else {}
    counts = summary.get("counts")
    if (
        summary.get("copy_subset_complete") is not True
        or not isinstance(counts, dict)
        or set(counts) != set(copies.STATES)
        or any(type(value) is not int or value < 0 for value in counts.values())
        or any(
            counts[state]
            for state in copies.STATES
            if state not in {"verified", "skipped"}
        )
    ):
        reject("finalization_verify_required")
    await _settled(db, window)
    return attempt


async def begin_seal(db, actor_id, claim, copy_id, expected_revision, native):
    claim = await _claim(db, actor_id, claim)
    window = await _window(db, uuid.UUID(claim["window_id"]), context=True)
    copy = await _copy(db, copy_id, private=True)
    row = await _existing(db, claim)
    if row is None:
        reject("finalization_unknown")
    _cas(row, expected_revision)
    _bound(row, window)
    native = _native(native, claim, window.source, row.native)
    if row.state not in ("bound", "sealing", "sealed") or row.copy_id not in (
        None,
        copy.id,
    ):
        reject("finalization_changed")
    attempt = await _verified(db, row, window, copy)
    values = await copies.input_values(db, copy)
    token = _token(copy, attempt, window)
    if row.token is not None and row.token != token:
        reject("finalization_checkpoint_changed")
    if row.state == "bound":
        await _change(
            db,
            row,
            "sealing",
            copy_id=copy.id,
            inventory_job_id=copy.source_inventory_job_id,
            token=deepcopy(token),
            native=native,
        )
    return {"item": item(row), "values": values, "token": token}


async def finish_seal(db, actor_id, claim, expected_revision, token, evidence, native):
    claim = await _claim(db, actor_id, claim)
    window = await _window(db, uuid.UUID(claim["window_id"]), context=True)
    initial = await _existing(db, claim, lock=False)
    if initial is None or initial.copy_id is None:
        reject("finalization_unknown")
    copy = await _copy(db, initial.copy_id, private=True)
    row = await _existing(db, claim)
    _cas(row, expected_revision)
    _bound(row, window)
    native = _native(native, claim, window.source, row.native)
    if row.state not in ("sealing", "sealed"):
        reject("finalization_changed")
    attempt = await _verified(db, row, window, copy)
    if token != row.token or token != _token(copy, attempt, window):
        reject("finalization_checkpoint_changed")
    keys = {
        "schema",
        "evidence_sha256",
        "source_report_sha256",
        "catalog_sha256",
        "baseline_sha256",
        "counts",
        "catalog_summary",
    }
    if not (
        isinstance(evidence, dict)
        and set(evidence) == keys
        and evidence.get("schema") == EVIDENCE_SCHEMA
        and _sha(evidence.get("evidence_sha256"))
        and _counts(evidence.get("counts"))
        and set(evidence["counts"]) == COUNT_FIELDS
        and isinstance(evidence.get("catalog_summary"), dict)
        and len(json.dumps(evidence).encode()) <= 65536
        and all(
            evidence.get(key) == token.get(key)
            for key in ("source_report_sha256", "catalog_sha256", "baseline_sha256")
        )
    ):
        reject("finalization_invalid_evidence")
    counts = evidence["counts"]
    if copy.summary["counts"]["verified"] != counts["copied_objects"] or any(
        native["counts"].get(actual) != counts[expected]
        for actual, expected in (
            ("buckets", "source_buckets"),
            ("versions", "current_objects"),
            ("delete_markers", "current_delete_markers"),
        )
    ):
        reject("finalization_invalid_evidence")
    if row.state == "sealed":
        if row.evidence != evidence:
            reject("finalization_changed")
        return item(row)
    await _change(
        db,
        row,
        "sealed",
        evidence=deepcopy(evidence),
        native=native,
        sealed_at=await db.scalar(select(func.statement_timestamp())),
    )
    return item(row)


async def begin_invalidation(db, actor_id, claim, expected_revision):
    claim = await _claim(db, actor_id, claim)
    window = await _window(db, uuid.UUID(claim["window_id"]))
    row = await _existing(db, claim)
    from app.services.storage_migration_cutovers import (
        require_finalization_invalidatable,
    )

    await require_finalization_invalidatable(db, uuid.UUID(claim["finalization_id"]))
    from app.services.storage_migration_read_publications import require_unpublished

    await require_unpublished(db, uuid.UUID(claim["finalization_id"]))
    if row is None:
        if claim["reserved"]:
            reject("finalization_unknown")
        if type(expected_revision) is not int or expected_revision != 0:
            reject("finalization_changed")
        # No database reservation ever existed for this intent. The original
        # window may have closed after a rolled-back bind; lock its current Gate
        # and drain real work without requiring the old stopped revision.
        gate = await _lock(
            db,
            select(StorageMaintenanceGate).where(
                StorageMaintenanceGate.location_id == window.source_location_id
            ),
        )
        if gate is None:
            reject("finalization_changed")
        await _settled(db, window)
        row = _new(window, actor_id, claim, {}, state="invalidated")
        row.invalidated_at = await db.scalar(select(func.statement_timestamp()))
        db.add(row)
        await db.flush()
        return item(row)
    _cas(row, expected_revision)
    if row.state not in ("invalidating", "invalidated"):
        await _change(db, row, "invalidating")
    return item(row)


async def finish_invalidation(db, actor_id, claim, expected_revision):
    claim = await _claim(db, actor_id, claim)
    window = await _window(db, uuid.UUID(claim["window_id"]))
    await windows.gate(db, window, exclusive=True)
    row = await _existing(db, claim)
    if row is None:
        reject("finalization_unknown")
    from app.services.storage_migration_cutovers import (
        require_finalization_invalidatable,
    )

    await require_finalization_invalidatable(db, row.id)
    from app.services.storage_migration_read_publications import require_unpublished

    await require_unpublished(db, row.id)
    _cas(row, expected_revision)
    if row.state == "invalidated":
        return item(row)
    if row.state != "invalidating":
        reject("finalization_changed")
    await _settled(db, window)
    await _change(
        db,
        row,
        "invalidated",
        invalidated_at=await db.scalar(select(func.statement_timestamp())),
    )
    return item(row)


async def status(db, actor_id, claim):
    claim = await _claim(db, actor_id, claim)
    row = await _existing(db, claim, lock=False)
    if row is None:
        reject("finalization_unknown")
    return item(row)


async def changed_checkpoint(db, copy_id):
    # save_delta already holds the Copy lock. Control acquires Copy before
    # Finalization NOWAIT and never spans native work, so this one short UPDATE
    # may wait without a reverse-lock cycle. Receipt and revocation commit
    # together; a NOWAIT error here must never discard a real physical ACK.
    await db.execute(
        update(Finalization)
        .where(
            Finalization.copy_id == copy_id,
            Finalization.state.in_(("sealing", "sealed")),
        )
        .values(
            state="invalidating",
            revision=Finalization.revision + 1,
            updated_at=func.statement_timestamp(),
            error_code="finalization_checkpoint_changed",
        )
    )
    from app.services import storage_migration_read_publications as publications

    # Copy -> Finalization -> Publication matches control lock order. Both
    # eligibility changes and the actual ACK become visible in one commit.
    await publications.changed_checkpoint(db, copy_id)
