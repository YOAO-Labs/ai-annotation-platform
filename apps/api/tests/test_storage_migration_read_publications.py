"""Real PostgreSQL publication control, with synthetic sealed Copy fixtures.

These tests exercise database ownership/CAS, exact locator selection and durable
withdrawal; they do not claim provider I/O or independent evidence qualification.
"""

from copy import deepcopy
import hashlib
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select, update

from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset import DatasetItem
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.storage_migration_copy import StorageMigrationCopyDelta
from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication as Publication,
    StorageMigrationReadRoute as Route,
)
from app.services import (
    storage_migration_copy as core,
    storage_migration_copies as copies,
    storage_migration_finalizations as finalizations,
    storage_migration_read_publications as service,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.content_snapshot import ContentError
from tests.test_storage_migration_finalizations import (
    bound as bound,
    child,
    code,
    control as control,
    evidence,
)
from tests.test_storage_migration_windows import prepared as prepared


def entries(world, *, count=2):
    values = []
    for number in range(count):
        source = {
            "endpoint_url": world.copy.source["endpoint"],
            "bucket": world.copy.source["purpose_buckets"]["datasets"],
            "key": "original/同一对象.png",
            "version_id": f"source-{number}",
        }
        values.append(
            {
                "source_object_id": core.digest(source),
                "source_endpoint_url": source["endpoint_url"],
                "source_bucket": source["bucket"],
                "source_object_key": source["key"],
                "source_version_id": source["version_id"],
                "sha256": "e" * 64,
                "size_bytes": 4,
                "source_etag": '"source-etag"',
                "is_latest": number == 1,
                "target_object_key": world.copy.target["key_prefix"] + f"plan/{number}",
                "target_version_id": f"target-{number}",
                "target_etag": f'"target-{number}"',
                "target_content_sha256": "f" * 64,
                "creation_nonce": f"nonce-{number}",
                "creation_plan_uuid": str(uuid.uuid4()),
            }
        )
    return sorted(values, key=lambda row: row["source_object_id"])


def mapping(world, rows=None):
    rows = deepcopy(world.entries if rows is None else rows)
    checksum = hashlib.sha256()
    for row in rows:
        checksum.update(core.canonical(row))
    return {
        "schema": service.SCHEMA,
        "evidence": deepcopy(world.evidence),
        "entries": rows,
        "mapping_sha256": checksum.hexdigest(),
    }


@pytest.fixture
async def sealed(db_session, bound):
    bound.copy, bound.attempt, _ = await child(db_session, bound)
    bound.entries = entries(bound)
    bound.copy.summary = {
        "counts": {**dict.fromkeys(copies.STATES, 0), "verified": len(bound.entries)},
        "copy_subset_complete": True,
    }
    begin = await finalizations.begin_seal(
        db_session, bound.actor.id, bound.claim, bound.copy.id, 1, bound.native
    )
    bound.evidence = evidence(begin["token"])
    bound.evidence["counts"].update(copied_objects=2, verified_target_bytes=8)
    bound.final = await finalizations.finish_seal(
        db_session,
        bound.actor.id,
        bound.claim,
        2,
        begin["token"],
        bound.evidence,
        bound.native,
    )
    return bound


async def publish(db, world):
    world.preflight = await service.preflight_publish(
        db, world.actor.id, world.claim, world.final.revision, world.native
    )
    world.publication = await service.publish(
        db,
        world.actor.id,
        world.claim,
        world.final.revision,
        world.preflight["token"],
        mapping(world),
        world.native,
    )
    return world.publication


async def lookup(db, world, **changes):
    entry = world.entries[0]
    fields = dict(
        source_location_id=LEGACY_LOCATION_ID,
        endpoint_url=entry["source_endpoint_url"],
        bucket=entry["source_bucket"],
        object_key=entry["source_object_key"],
        sha256=entry["sha256"],
        size_bytes=entry["size_bytes"],
    )
    return await service.lookup(db, **{**fields, **changes})


async def test_publish_preflight_cas_idempotency_and_exact_locator(db_session, sealed):
    value = await publish(db_session, sealed)
    assert value.publication_id == sealed.final.finalization_id
    assert value.state == "active" and value.revision == 1 and value.route_count == 2
    assert sealed.preflight["item"].window_revision == sealed.final.window_revision
    assert sealed.preflight["evidence"] == sealed.evidence
    assert await publish(db_session, sealed) == value
    assert len(list(await db_session.scalars(select(Route)))) == 2
    route = await lookup(db_session, sealed)
    assert (
        route.source_version_id == "source-1" and route.target_version_id == "target-1"
    )
    assert route.target_etag == '"target-1"'
    assert route.purpose == service.purpose(value.publication_id)
    assert route.source_location_id != route.target_location_id
    assert route.maintenance_revision == 1
    await service.recheck(db_session, route)
    assert callable((await service.target_for_route(db_session, route)).client)
    for changes in (
        {"endpoint_url": "http://127.0.0.1:9999"},
        {"bucket": "other"},
        {"object_key": "original/other.png"},
        {"sha256": "c" * 64},
        {"size_bytes": 5},
        {"source_location_id": uuid.uuid4()},
    ):
        assert await lookup(db_session, sealed, **changes) is None
    assert (await service.status(db_session, sealed.actor.id, sealed.claim)) == value


async def test_same_content_versions_use_stable_source_id_when_no_latest(
    db_session, sealed
):
    for row in sealed.entries:
        row["is_latest"] = False
    await publish(db_session, sealed)
    route = await lookup(db_session, sealed, lock=True)
    assert route.source_object_id == sealed.entries[0]["source_object_id"]


@pytest.mark.parametrize(
    "operation", ["lookup", "locked_lookup", "recheck", "locked_recheck", "target"]
)
async def test_route_snapshots_preserve_unflushed_caller_work(
    db_session, sealed, operation
):
    await publish(db_session, sealed)
    route = await lookup(db_session, sealed)
    pending = DatasetItem(
        id=uuid.uuid4(), dataset_id=uuid.uuid4(), file_name="not-flushed"
    )
    db_session.add(pending)
    try:
        if operation in {"lookup", "locked_lookup"}:
            assert await lookup(
                db_session, sealed, lock=operation == "locked_lookup", size_bytes=None
            )
        elif operation in {"recheck", "locked_recheck"}:
            await service.recheck(db_session, route, lock=operation == "locked_recheck")
        else:
            assert await service.target_for_route(db_session, route)
        assert pending in db_session.new
    finally:
        db_session.expunge(pending)


async def test_saved_exact_version_selects_nonlatest_even_without_saved_size(
    db_session, sealed
):
    await publish(db_session, sealed)
    route = await lookup(
        db_session, sealed, source_version_id="source-0", size_bytes=None
    )
    assert route.source_version_id == "source-0" and route.is_latest is False
    assert route.target_version_id == "target-0" and route.size_bytes == 4
    assert (
        await lookup(
            db_session, sealed, source_version_id="source-missing", size_bytes=None
        )
        is None
    )
    assert (
        await lookup(
            db_session,
            sealed,
            source_version_id="source-0",
            sha256="0" * 64,
            size_bytes=None,
        )
        is None
    )


@pytest.mark.parametrize("selector", [service.ANY_VERSION, None, "null"])
async def test_null_slot_and_absent_version_are_distinct_mapping_identities(
    db_session, sealed, selector
):
    for number, entry in enumerate(sealed.entries):
        entry["source_version_id"] = None if number == 0 else "null"
        entry["is_latest"] = number == 1
        entry["source_object_id"] = core.digest(
            {
                "endpoint_url": entry["source_endpoint_url"],
                "bucket": entry["source_bucket"],
                "key": entry["source_object_key"],
                "version_id": entry["source_version_id"],
            }
        )
    sealed.entries.sort(key=lambda entry: entry["source_object_id"])
    await publish(db_session, sealed)
    route = await lookup(
        db_session, sealed, source_version_id=selector, size_bytes=None
    )
    expected = "null" if selector is service.ANY_VERSION else selector
    assert route.source_version_id == expected
    assert route.is_latest is (expected == "null")


@pytest.mark.parametrize(
    "change",
    [
        "authority",
        "claim",
        "native",
        "revision",
        "token",
        "mapping",
        "evidence",
        "partial",
        "target_version",
        "source_id",
        "source_bucket",
        "source_endpoint",
        "target_prefix",
        "extra_field",
    ],
)
async def test_publish_rejects_changed_context_and_closed_mapping(
    db_session, sealed, annotator, change
):
    prepared = await service.preflight_publish(
        db_session, sealed.actor.id, sealed.claim, 3, sealed.native
    )
    actor, claim, native, revision, token, proof = (
        sealed.actor.id,
        sealed.claim,
        sealed.native,
        3,
        prepared["token"],
        mapping(sealed),
    )
    expected = "read_publication_invalid_mapping"
    if change == "authority":
        actor, expected = annotator[0].id, "finalization_authority_changed"
    elif change == "claim":
        claim, expected = (
            {**claim, "reservation_nonce": str(uuid.uuid4())},
            "finalization_changed",
        )
    elif change == "native":
        native, expected = (
            {**native, "barriers_sha256": "0" * 64},
            "finalization_native_changed",
        )
    elif change == "revision":
        revision, expected = 2, "finalization_changed"
    elif change == "token":
        token, expected = {**token, "target_revisions": {}}, "read_publication_changed"
    elif change == "mapping":
        proof["mapping_sha256"] = "0" * 64
    elif change == "evidence":
        proof["evidence"] = {**proof["evidence"], "evidence_sha256": "0" * 64}
    elif change == "partial":
        proof = mapping(sealed, sealed.entries[:1])
    else:
        key, value = {
            "target_version": ("target_version_id", "null"),
            "source_id": ("source_object_id", "0" * 64),
            "source_bucket": ("source_bucket", "unregistered"),
            "source_endpoint": ("source_endpoint_url", "http://127.0.0.1:9999"),
            "target_prefix": ("target_object_key", "outside/prefix"),
            "extra_field": ("unexpected", True),
        }[change]
        proof["entries"][0][key] = value
    await code(
        expected,
        lambda: service.publish(
            db_session, actor, claim, revision, token, proof, native
        ),
    )
    assert await db_session.scalar(select(Publication.id)) is None
    assert await db_session.scalar(select(Route.publication_id)) is None


@pytest.mark.parametrize(
    "change",
    ["source_disabled", "target_disabled", "physical", "gate"],
)
async def test_lookup_and_recheck_reject_disabled_drift_or_stopped_target(
    db_session, sealed, change
):
    await publish(db_session, sealed)
    route = await lookup(db_session, sealed)
    source = await db_session.get(StorageLocation, LEGACY_LOCATION_ID)
    target = await db_session.get(StorageLocation, route.target_location_id)
    connection = await db_session.get(StorageConnection, target.connection_id)
    if change == "source_disabled":
        source.enabled = False
    elif change == "target_disabled":
        target.enabled = False
    elif change == "physical":
        connection.config = {**connection.config, "bucket": "changed-target"}
    else:
        await db_session.execute(
            update(StorageMaintenanceGate)
            .where(StorageMaintenanceGate.location_id == target.id)
            .values(state="stopped", revision=2)
        )
    await db_session.flush()
    for call in (
        lambda: lookup(db_session, sealed),
        lambda: service.recheck(db_session, route),
    ):
        with pytest.raises((HTTPException, ContentError)):
            await call()


async def test_current_credentials_rotate_without_rewriting_publication_identity(
    db_session, sealed
):
    await publish(db_session, sealed)
    before = await lookup(db_session, sealed)
    target = await db_session.get(StorageLocation, before.target_location_id)
    connection = await db_session.get(StorageConnection, target.connection_id)
    connection.revision += 1
    connection.secret_enc = b"rotated-private-secret"
    target.config_revision += 1
    await db_session.flush()
    with pytest.raises(ContentError, match="read_publication_changed"):
        await service.recheck(db_session, before)
    current = await lookup(db_session, sealed)
    assert current.target_connection_revision == before.target_connection_revision + 1
    assert current.target_location_revision == before.target_location_revision + 1
    assert current.target_endpoint_url == before.target_endpoint_url
    assert current.target_version_id == before.target_version_id
    await service.recheck(db_session, current)
    access = await service.target_for_route(db_session, current)
    assert access.secret_enc == b"rotated-private-secret"


async def test_withdrawal_keeps_native_claim_and_waits_only_own_actual_permits(
    db_session, sealed
):
    value = await publish(db_session, sealed)
    route = await lookup(db_session, sealed)
    own = StorageOperation(
        location_id=route.target_location_id,
        revision=1,
        purpose=route.purpose,
        node_id="test",
    )
    other = StorageOperation(
        location_id=route.target_location_id,
        revision=1,
        purpose="migration_read_" + uuid.uuid4().hex,
        node_id="test",
    )
    db_session.add_all([own, other])
    await db_session.flush()
    await code(
        "read_publication_active",
        lambda: finalizations.begin_invalidation(
            db_session, sealed.actor.id, sealed.claim, 3
        ),
    )
    await code(
        "read_publication_changed",
        lambda: service.begin_withdrawal(db_session, sealed.actor.id, sealed.claim, 99),
    )
    value = await service.begin_withdrawal(
        db_session, sealed.actor.id, sealed.claim, value.revision
    )
    assert value.state == "withdrawing" and value.revision == 2
    assert await lookup(db_session, sealed) is None
    with pytest.raises(ContentError, match="read_publication_changed"):
        await service.recheck(db_session, route)
    await code(
        "read_publication_unsettled",
        lambda: service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 2),
    )
    await db_session.delete(own)
    await db_session.flush()
    value = await service.finish_withdrawal(
        db_session, sealed.actor.id, sealed.claim, 2
    )
    assert value.state == "withdrawn" and value.revision == 3
    assert (
        await service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 3)
        == value
    )
    assert await db_session.get(StorageOperation, other.id)
    assert len(list(await db_session.scalars(select(Route)))) == 2
    assert not await service.frozen_copy(db_session, sealed.copy.id)
    assert (await copies.item(db_session, sealed.copy)).read_only
    final = await finalizations.status(db_session, sealed.actor.id, sealed.claim)
    assert final.state == "sealed" and final.revision == 3
    await code(
        "read_publication_withdrawn",
        lambda: service.preflight_publish(
            db_session, sealed.actor.id, sealed.claim, 3, sealed.native
        ),
    )
    final = await finalizations.begin_invalidation(
        db_session, sealed.actor.id, sealed.claim, 3
    )
    final = await finalizations.finish_invalidation(
        db_session, sealed.actor.id, sealed.claim, final.revision
    )
    assert final.state == "invalidated"
    assert (
        await db_session.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
    ).state == "stopped"


async def test_real_late_ack_persists_while_routes_revoke_and_hold_responsibility(
    db_session, sealed
):
    await publish(db_session, sealed)
    sealed.copy.summary = {
        "counts": {**dict.fromkeys(copies.STATES, 0), "pending": 1},
        "copy_subset_complete": False,
    }
    await db_session.flush()
    data = {
        "id": "b" * 64,
        "state": "verified",
        "receipt": {"version_id": "late-actual-provider-ack"},
    }
    await copies.save_delta(
        db_session,
        sealed.attempt.async_job_id,
        sealed.attempt.task_id,
        data,
        copies.row_digest(data),
    )
    receipt = await db_session.get(
        StorageMigrationCopyDelta, (sealed.copy.id, data["id"])
    )
    assert receipt.data["receipt"]["version_id"] == "late-actual-provider-ack"
    publication = await service.status(db_session, sealed.actor.id, sealed.claim)
    assert publication.state == "withdrawing" and publication.revision == 2
    assert await lookup(db_session, sealed) is None
    final = await finalizations.status(db_session, sealed.actor.id, sealed.claim)
    assert final.state == "invalidating"
    await code(
        "read_publication_active",
        lambda: finalizations.finish_invalidation(
            db_session, sealed.actor.id, sealed.claim, final.revision
        ),
    )
    await service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 2)
    final = await finalizations.finish_invalidation(
        db_session, sealed.actor.id, sealed.claim, final.revision
    )
    assert final.state == "invalidated"


async def test_withdrawn_routes_retain_history_without_freezing_new_copy_work(
    db_session, sealed
):
    await publish(db_session, sealed)
    await service.begin_withdrawal(db_session, sealed.actor.id, sealed.claim, 1)
    await service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 2)
    await finalizations.begin_invalidation(db_session, sealed.actor.id, sealed.claim, 3)
    await finalizations.finish_invalidation(
        db_session, sealed.actor.id, sealed.claim, 4
    )
    await copies.writable(db_session, sealed.copy)
    assert len(list(await db_session.scalars(select(Route)))) == 2


async def test_status_and_withdraw_recheck_current_authority(
    db_session, sealed, annotator
):
    await publish(db_session, sealed)
    for call in (
        lambda: service.status(db_session, annotator[0].id, sealed.claim),
        lambda: service.begin_withdrawal(db_session, annotator[0].id, sealed.claim, 1),
        lambda: service.finish_withdrawal(db_session, annotator[0].id, sealed.claim, 1),
    ):
        await code("finalization_authority_changed", call)
