"""Trusted operator transactions; provider work belongs outside these locks."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.storage_migration_cutovers import StorageMigrationCutover as Cutover
from app.db.models.storage_migration_finalization import (
    StorageMigrationFinalization as Finalization,
)
from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication as Publication,
)
from app.services import (
    managed_s3_target,
    storage_access,
    storage_migration_finalizations as finals,
    storage_migration_read_publications as reads,
    storage_migration_windows as windows,
)
from app.services.storage_migration_inventory import PURPOSES

AUDIENCE = "storage-migration-cutover"
CLAIM_SCHEMA = "aap.storage-migration-rollback-fence-claim/v1"
ROLLING = ("rollback_draining", "rollback_bound", "rollback_restoring")
CLAIM_FIELDS = {
    "schema",
    "cutover_id",
    "expected_cutover_revision",
    "database_identity",
    "reservation_nonce",
    "native_operation_id",
    "config_digest",
    "reserved",
}
digest = finals.digest


def reject(code="cutover_changed", status=409):
    finals.reject(code, status)


def canonical_claim(claim):
    return {**deepcopy(claim), "reserved": False}


def binding_facts(row):
    return {
        "location_id": str(row.location_id),
        "bucket": row.bucket,
        "generation": row.generation,
    }


def rollback_bindings(row):
    return {
        purpose: {**facts, "generation": facts["generation"] + 2}
        for purpose, facts in row.before_bindings.items()
    }


def item(row):
    context = row.review["context"]
    return {
        "id": str(row.id),
        "state": row.state,
        "revision": row.revision,
        "window_id": str(row.window_id),
        "target_location_id": str(row.target_location_id),
        "target_gate_revision": row.target_gate_revision,
        "review": deepcopy(row.review),
        "before_bindings": deepcopy(row.before_bindings),
        "after_bindings": deepcopy(row.after_bindings),
        "rollback_claim": deepcopy(row.rollback_claim),
        "source_release_operation_id": str(row.source_release_operation_id)
        if row.source_release_operation_id
        else None,
        **{
            name: deepcopy(context[name])
            for name in ("source_claim", "database_identity", "target_source")
        },
    }


async def _owner(db, cutover_id, *, lock=False, required=True):
    query = select(Cutover).where(Cutover.id == cutover_id)
    if lock:
        query = query.with_for_update(nowait=True)
    try:
        row = await db.scalar(query.execution_options(populate_existing=True))
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("cutover_busy")
        raise
    if row is None and required:
        reject("cutover_unknown", 404)
    return row


async def _positions(db, ids):
    initial = list(
        await db.scalars(select(StorageLocation).where(StorageLocation.id.in_(ids)))
    )
    if len(initial) != len(ids):
        reject()
    links = {row.id: row.connection_id for row in initial}
    connection_ids = sorted({value for value in links.values() if value is not None})
    try:
        if connection_ids:
            await db.execute(
                select(StorageConnection.id)
                .where(StorageConnection.id.in_(connection_ids))
                .order_by(StorageConnection.id)
                .with_for_update(read=True, nowait=True)
            )
        rows = list(
            await db.scalars(
                select(StorageLocation)
                .where(StorageLocation.id.in_(ids))
                .order_by(StorageLocation.id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
        )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("cutover_busy")
        raise
    if {row.id: row.connection_id for row in rows} != links:
        reject()


async def _bindings(db, *, exclusive=False):
    try:
        rows = list(
            await db.scalars(
                select(StorageBinding)
                .where(StorageBinding.purpose.in_(PURPOSES))
                .order_by(StorageBinding.purpose)
                .with_for_update(read=not exclusive, nowait=True)
                .execution_options(populate_existing=True)
            )
        )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("cutover_busy")
        raise
    if {row.purpose for row in rows} != PURPOSES:
        reject("cutover_bindings_unproven")
    return rows


def _vector(rows):
    return {row.purpose: binding_facts(row) for row in rows}


def _cas(row, expected_revision):
    if type(expected_revision) is not int or row.revision != expected_revision:
        reject()


def _replay_revision(row, expected_revision, floor):
    if (
        type(expected_revision) is not int
        or not floor <= expected_revision <= row.revision
    ):
        reject()


async def prepare_activation(
    db,
    actor_id,
    a_claim,
    expected_finalization_revision,
    expected_publication_revision,
    native_a,
):
    a_claim = await finals._claim(db, actor_id, a_claim)
    initial = await db.get(
        Publication, uuid.UUID(a_claim["finalization_id"]), populate_existing=True
    )
    if initial is None:
        reject("read_publication_unknown")
    actor = await finals._actor(db, actor_id)
    await _positions(
        db, sorted({initial.source_location_id, initial.target_location_id})
    )
    bindings = await _bindings(db, exclusive=True)
    final, _, native, _, publication = await reads._prepared(
        db, actor.id, a_claim, expected_finalization_revision, native_a
    )
    if (
        publication is None
        or publication.state != "active"
        or publication.revision != expected_publication_revision
    ):
        reject("read_publication_changed")
    before = _vector(bindings)
    if any(
        facts["location_id"] != str(publication.source_location_id)
        or facts["bucket"] != publication.source["purpose_buckets"][purpose]
        or type(facts["generation"]) is not int
        or not 1 <= facts["generation"] <= 2147483645
        for purpose, facts in before.items()
    ):
        reject("cutover_bindings_unproven")
    location = await db.get(
        StorageLocation, publication.target_location_id, populate_existing=True
    )
    targets = {
        purpose: (
            await managed_s3_target.target_for(db, location, purpose=purpose)
        ).facts
        for purpose in sorted(PURPOSES)
    }
    if any(
        target["endpoint_url"] != publication.target["endpoint_url"]
        or target["bucket"] != publication.target["bucket"]
        for target in targets.values()
    ):
        reject()
    target_gate_revision = await storage_access.check(
        db, publication.target_location_id, lock=True
    )
    window = await windows.owner(db, publication.window_id)
    return {
        "id": str(publication.id),
        "actor_id": str(actor.id),
        "window_id": str(publication.window_id),
        "source_claim": canonical_claim(final.claim),
        "database_identity": a_claim["database_identity"],
        "publication_revision": publication.revision,
        "mapping_sha256": publication.mapping_sha256,
        "finalization_revision": final.revision,
        "window_revision": window.revision,
        "source_location_id": str(publication.source_location_id),
        "target_location_id": str(publication.target_location_id),
        "source_gate_revision": window.source_gate_revision,
        "target_gate_revision": target_gate_revision,
        "target_source": {
            "endpoint": publication.target["endpoint_url"],
            "purpose_buckets": dict.fromkeys(
                sorted(PURPOSES), publication.target["bucket"]
            ),
        },
        "native_source": {
            key: deepcopy(native[key])
            for key in (
                "config_digest",
                "operation_id",
                "barriers_sha256",
                "source",
                "counts",
            )
        },
        "targets": targets,
        "before_bindings": before,
        "after_bindings": {
            purpose: {
                "location_id": str(publication.target_location_id),
                "bucket": targets[purpose]["bucket"],
                "generation": facts["generation"] + 1,
            }
            for purpose, facts in before.items()
        },
    }


preview = prepare_activation


def issue_review(before_context, after_context, capabilities):
    if before_context != after_context:
        reject("cutover_preview_changed")
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != PURPOSES
        or any(
            not isinstance(value, dict) or not value for value in capabilities.values()
        )
    ):
        reject("cutover_probe_required")
    measured = datetime.now(timezone.utc)
    expires = measured + timedelta(minutes=5)
    review = {
        "context": deepcopy(after_context),
        "capabilities": deepcopy(capabilities),
        "measured_at": measured.isoformat(),
        "expires_at": expires.isoformat(),
    }
    review["impact_token"] = jwt.encode(
        {"aud": AUDIENCE, "review": digest(review), "exp": expires},
        settings.secret_key,
        algorithm="HS256",
    )
    return review


def _review(review, *, expired=False):
    try:
        if set(review) != {
            "context",
            "capabilities",
            "measured_at",
            "expires_at",
            "impact_token",
        }:
            raise ValueError
        token = jwt.decode(
            review["impact_token"],
            settings.secret_key,
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={"verify_exp": not expired},
        )
        if (
            token["review"]
            != digest(
                {key: value for key, value in review.items() if key != "impact_token"}
            )
            or set(review["capabilities"]) != PURPOSES
            or any(
                not isinstance(value, dict) or not value
                for value in review["capabilities"].values()
            )
        ):
            raise ValueError
        return review["context"]
    except (JWTError, ValueError, TypeError, KeyError):
        reject("cutover_preview_invalid")


async def activate(db, actor_id, a_claim, review, native_a):
    a_claim = await finals._claim(db, actor_id, a_claim)
    actor = await finals._actor(db, actor_id)
    context = _review(review, expired=True)
    if (
        context["actor_id"] != str(actor.id)
        or canonical_claim(a_claim) != context["source_claim"]
    ):
        reject("cutover_preview_invalid")
    existing = await _owner(db, uuid.UUID(context["id"]), required=False)
    if existing is not None:
        existing, bindings, window, final, pub, source, _ = await _control(
            db, actor_id, existing.id
        )
        if existing.review_sha256 != digest(review):
            reject()
        _expect_vector(
            bindings,
            existing.after_bindings
            if existing.state in ("active", "rollback_draining")
            else rollback_bindings(existing),
        )
        if existing.state == "active":
            _source_held(existing, window, final, pub, source)
            native = finals._native(
                native_a,
                a_claim,
                context["native_source"]["source"],
                context["native_source"],
            )
            if native["counts"] != context["native_source"]["counts"]:
                reject("finalization_native_changed")
        return item(existing)
    _review(review)
    current = await prepare_activation(
        db,
        actor.id,
        a_claim,
        context["finalization_revision"],
        context["publication_revision"],
        native_a,
    )
    if current != context:
        reject("cutover_preview_changed")
    bindings = await _bindings(db, exclusive=True)
    row = Cutover(
        id=uuid.UUID(context["id"]),
        window_id=uuid.UUID(context["window_id"]),
        source_location_id=uuid.UUID(context["source_location_id"]),
        target_location_id=uuid.UUID(context["target_location_id"]),
        state="active",
        revision=1,
        review=deepcopy(review),
        review_sha256=digest(review),
        before_bindings=deepcopy(context["before_bindings"]),
        after_bindings=deepcopy(context["after_bindings"]),
        target_gate_revision=context["target_gate_revision"],
        created_by=actor.id,
    )
    for binding in bindings:
        facts = row.after_bindings[binding.purpose]
        binding.location_id, binding.bucket, binding.generation = (
            uuid.UUID(facts["location_id"]),
            facts["bucket"],
            facts["generation"],
        )
    db.add(row)
    await db.flush()
    return item(row)


async def _control(db, actor_id, cutover_id, *, exclusive_bindings=False):
    await finals._actor(db, actor_id)
    initial = await _owner(db, cutover_id)
    await _positions(
        db, sorted({initial.source_location_id, initial.target_location_id})
    )
    bindings = await _bindings(db, exclusive=exclusive_bindings)
    window = await windows.owner(db, initial.window_id, lock=True, exclusive=True)
    # Match the established Window control order, source Gate before target Gate.
    gates = {}
    for location_id in (initial.source_location_id, initial.target_location_id):
        gate = await finals._lock(
            db, select(Gate).where(Gate.location_id == location_id)
        )
        if gate is None:
            reject()
        gates[location_id] = gate
    final = await finals._lock(
        db, select(Finalization).where(Finalization.id == initial.id)
    )
    publication = await reads._owner(db, initial.id, lock=True)
    row = await _owner(db, cutover_id, lock=True)
    from app.services import storage_migration_copy_target as targets
    from app.services import storage_migration_inventories as inventories

    identity, _, _, _ = await targets.resolve(db, row.target_location_id)
    if publication is None or final is None or identity != publication.target:
        reject()
    if await inventories.source_scope(db) != publication.source:
        reject()
    if row.review["context"]["database_identity"] != await finals.identity(db):
        reject("cutover_database_changed")
    return (
        row,
        bindings,
        window,
        final,
        publication,
        gates[row.source_location_id],
        gates[row.target_location_id],
    )


def _expect_vector(bindings, expected):
    if _vector(bindings) != expected:
        reject("cutover_bindings_changed")


def _source_held(row, window, final, publication, gate):
    context = row.review["context"]
    if (
        window.state != "stopped"
        or gate.state != "stopped"
        or gate.revision != context["source_gate_revision"]
        or final.state != "sealed"
        or final.revision != context["finalization_revision"]
        or publication.state != "active"
        or publication.revision != context["publication_revision"]
    ):
        reject()


async def _change(db, row, state):
    row.state, row.revision = state, row.revision + 1
    row.updated_at = await db.scalar(select(func.statement_timestamp()))
    if state == "rolled_back":
        row.rolled_back_at = row.updated_at
    await db.flush()


def _gate_change(gate, state, actor_id, stamp):
    gate.state, gate.revision = state, gate.revision + 1
    gate.changed_by, gate.changed_at = actor_id, stamp


async def begin_rollback(db, actor_id, cutover_id, expected_revision):
    row, bindings, window, final, pub, source, target = await _control(
        db, actor_id, cutover_id
    )
    if row.state != "active":
        if row.state in (*ROLLING, "rolled_back"):
            _replay_revision(row, expected_revision, 1)
            _expect_vector(
                bindings,
                row.after_bindings
                if row.state == "rollback_draining"
                else rollback_bindings(row),
            )
            return item(row)
        reject()
    _cas(row, expected_revision)
    _expect_vector(bindings, row.after_bindings)
    _source_held(row, window, final, pub, source)
    if target.state != "active":
        reject()
    _gate_change(
        target,
        "draining",
        actor_id,
        await db.scalar(select(func.statement_timestamp())),
    )
    row.target_gate_revision = target.revision
    await _change(db, row, "rollback_draining")
    return item(row)


async def _drained(db, target_location_id):
    if await db.scalar(
        select(StorageOperation.id)
        .where(StorageOperation.location_id == target_location_id)
        .limit(1)
    ):
        reject("cutover_unsettled")


async def rollback_preflight(db, actor_id, cutover_id, expected_revision):
    row, bindings, window, final, pub, source, target = await _control(
        db, actor_id, cutover_id
    )
    _cas(row, expected_revision)
    if row.state != "rollback_draining":
        reject()
    _expect_vector(bindings, row.after_bindings)
    _source_held(row, window, final, pub, source)
    if not (
        (target.state == "draining" and target.revision == row.target_gate_revision)
        or (
            target.state == "stopped"
            and target.revision == row.target_gate_revision + 1
        )
    ):
        reject()
    await _drained(db, row.target_location_id)
    return {
        "id": str(row.id),
        "revision": row.revision,
        "database_identity": row.review["context"]["database_identity"],
        "target_source": deepcopy(row.review["context"]["target_source"]),
        "target_gate_revision": target.revision,
    }


def validate_rollback_claim(claim):
    if (
        not isinstance(claim, dict)
        or set(claim) != CLAIM_FIELDS
        or claim["schema"] != CLAIM_SCHEMA
        or not all(
            finals._uuid(claim[key])
            for key in ("cutover_id", "reservation_nonce", "native_operation_id")
        )
        or not all(
            finals._sha(claim[key]) for key in ("database_identity", "config_digest")
        )
        or type(claim["expected_cutover_revision"]) is not int
        or claim["expected_cutover_revision"] < 1
        or type(claim["reserved"]) is not bool
    ):
        reject("cutover_invalid_claim")
    return canonical_claim(claim)


async def bind_rollback(db, actor_id, cutover_id, expected_revision, b_claim, native_b):
    claim = validate_rollback_claim(b_claim)
    row, bindings, window, final, pub, source, target = await _control(
        db, actor_id, cutover_id, exclusive_bindings=True
    )
    if (
        claim["cutover_id"] != str(row.id)
        or claim["database_identity"] != row.review["context"]["database_identity"]
    ):
        reject("cutover_invalid_claim")
    native = finals._native(
        native_b, claim, row.review["context"]["target_source"], row.rollback_native
    )
    if (
        row.rollback_native is not None
        and native["counts"] != row.rollback_native["counts"]
    ):
        reject("finalization_native_changed")
    if row.state in ("rollback_bound", "rollback_restoring", "rolled_back"):
        _replay_revision(row, expected_revision, claim["expected_cutover_revision"])
        if row.rollback_claim != claim:
            reject()
        _expect_vector(bindings, rollback_bindings(row))
        return item(row)
    await rollback_preflight(db, actor_id, cutover_id, expected_revision)
    if claim["expected_cutover_revision"] != row.revision:
        reject()
    if target.state == "draining":
        _gate_change(
            target,
            "stopped",
            actor_id,
            await db.scalar(select(func.statement_timestamp())),
        )
    row.target_gate_revision = target.revision
    row.rollback_claim, row.rollback_native = claim, native
    expected = rollback_bindings(row)
    for binding in bindings:
        facts = expected[binding.purpose]
        binding.location_id, binding.bucket, binding.generation = (
            uuid.UUID(facts["location_id"]),
            facts["bucket"],
            facts["generation"],
        )
    await _change(db, row, "rollback_bound")
    return item(row)


async def begin_restoration(
    db, actor_id, cutover_id, expected_revision, source_release_operation_id
):
    try:
        release_id = uuid.UUID(str(source_release_operation_id))
    except (TypeError, ValueError, AttributeError):
        reject("cutover_release_invalid")
    row, bindings, window, final, pub, source, target = await _control(
        db, actor_id, cutover_id
    )
    if str(release_id) == row.review["context"]["source_claim"]["native_operation_id"]:
        reject("cutover_release_invalid")
    _expect_vector(bindings, rollback_bindings(row))
    if row.state in ("rollback_restoring", "rolled_back"):
        _replay_revision(
            row, expected_revision, row.rollback_claim["expected_cutover_revision"] + 1
        )
        if row.source_release_operation_id != release_id:
            reject()
        return item(row)
    _cas(row, expected_revision)
    if (
        row.state != "rollback_bound"
        or target.state != "stopped"
        or target.revision != row.target_gate_revision
    ):
        reject()
    _source_held(row, window, final, pub, source)
    row.source_release_operation_id = release_id
    await _change(db, row, "rollback_restoring")
    return item(row)


async def finish_rollback(db, actor_id, cutover_id, expected_revision, native_b):
    row, bindings, window, final, pub, source, target = await _control(
        db, actor_id, cutover_id
    )
    _expect_vector(bindings, rollback_bindings(row))
    if row.rollback_claim is None:
        reject()
    native = finals._native(
        native_b,
        row.rollback_claim,
        row.review["context"]["target_source"],
        row.rollback_native,
    )
    if native["counts"] != row.rollback_native["counts"]:
        reject("finalization_native_changed")
    if row.state == "rolled_back":
        _replay_revision(
            row, expected_revision, row.rollback_claim["expected_cutover_revision"] + 2
        )
        return item(row)
    _cas(row, expected_revision)
    if (
        row.state != "rollback_restoring"
        or window.state != "closed"
        or source.state != "active"
        or final.state != "invalidated"
        or pub.state != "withdrawn"
        or target.state != "stopped"
        or target.revision != row.target_gate_revision
    ):
        reject()
    await _drained(db, row.target_location_id)
    from app.services.managed_original_reclaim_fence import require_resumable

    await require_resumable(db, row.target_location_id)
    _gate_change(
        target, "active", actor_id, await db.scalar(select(func.statement_timestamp()))
    )
    row.target_gate_revision = target.revision
    await _change(db, row, "rolled_back")
    return item(row)


async def status(db, actor_id, cutover_id):
    await finals._actor(db, actor_id)
    row = await _owner(db, cutover_id)
    if row.review["context"]["database_identity"] != await finals.identity(db):
        reject("cutover_database_changed")
    return item(row)


async def require_target_resumable(db, location_id):
    if await db.scalar(
        select(Cutover.id)
        .where(Cutover.target_location_id == location_id, Cutover.state.in_(ROLLING))
        .limit(1)
    ):
        reject("cutover_active")


async def require_finalization_invalidatable(db, finalization_id):
    row = await _owner(db, finalization_id, lock=True, required=False)
    if row is not None and row.state not in ("rollback_restoring", "rolled_back"):
        reject("cutover_active")
