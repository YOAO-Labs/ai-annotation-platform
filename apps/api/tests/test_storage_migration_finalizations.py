"""Real PostgreSQL control transactions; native/file proof is supplied by its owner.

Synthetic closed Copy bytes exercise the database protocol, not physical I/O or
the independent materialized-evidence validator's qualification.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_migration_copy import (
    StorageMigrationCopy as Copy,
    StorageMigrationCopyAttempt as Attempt,
)
from app.db.models.storage_migration_finalization import (
    StorageMigrationFinalization as Finalization,
)
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.schemas.storage_migration_inventory import StorageMigrationInventoryRequest
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyGenerationRequest,
)
from app.schemas.storage_migration_window import (
    StorageMigrationWindowRequest,
    StorageMigrationWindowTransition,
)
from app.services import (
    storage_migration_finalizations as service,
    storage_migration_windows as windows,
    storage_migration_copies as copies,
    storage_migration_inventories as inventories,
)
from app.services.storage_migration_inventory import InventoryLimits, write_inventory
from app.workers.storage_migration_inventory import ReportSink
from tests.storage_migration_isolation import (
    db_session as db_session,
    migration_engine as migration_engine,
    super_admin as super_admin,
)
from tests.test_storage_migration_inventories import EmptySource
from tests.test_storage_migration_windows import (
    prepared as prepared,
    reader_world as reader_world,
    copy_worker_world as copy_worker_world,
)


def code(expected, call):
    async def check():
        with pytest.raises(HTTPException) as error:
            await call()
        assert error.value.detail["code"] == expected

    return check()


@pytest.fixture
async def control(db_session, prepared, super_admin):
    actor = super_admin[0]
    value = await windows.open_window(
        db_session, actor, StorageMigrationWindowRequest(parent_copy_id=prepared.id)
    )
    await windows.transition(
        db_session,
        value.window_id,
        actor,
        StorageMigrationWindowTransition(action="stop", expected_revision=1),
    )
    context = await service.preflight(db_session, actor.id, value.window_id, 2)
    claim = dict(
        schema=service.CLAIM_SCHEMA,
        finalization_id=str(uuid.uuid4()),
        window_id=str(value.window_id),
        expected_window_revision=2,
        database_identity=context["database_identity"],
        reservation_nonce=str(uuid.uuid4()),
        native_operation_id=str(uuid.uuid4()),
        config_digest="a" * 64,
        reserved=False,
    )
    native = dict(
        config_digest=claim["config_digest"],
        operation_id=claim["native_operation_id"],
        journal_revision=3,
        barriers_sha256="b" * 64,
        observed_at=inventories.now().isoformat(),
        counts={"buckets": 9, "versions": 0, "delete_markers": 0},
        source={key: context["source"][key] for key in ("endpoint", "purpose_buckets")},
    )
    return SimpleNamespace(
        actor=actor,
        window_id=value.window_id,
        context=context,
        claim=claim,
        native=native,
        parent=prepared,
    )


@pytest.fixture
async def bound(db_session, control):
    control.item = await service.bind(
        db_session, control.actor.id, control.claim, control.native
    )
    return control


async def child(db, world):
    request = StorageMigrationInventoryRequest(
        window_id=world.window_id,
        handling=dict.fromkeys(inventories.PURPOSES, "migrate"),
    )
    inv, job, _ = await inventories.start(db, world.actor, request)
    await inventories.claim(db, job.id, inv.task_id)
    inv.operation_id = uuid.uuid4()
    sink = ReportSink()
    write_inventory(
        None,
        EmptySource(),
        inv.source["purpose_buckets"],
        source={"endpoint": inv.source["endpoint"]},
        observations={
            "migration_window": {
                "window_id": str(inv.window_id),
                "window_revision": inv.window_revision,
                "source_quiescence_proven": False,
            }
        },
        handling=inv.handling,
        limits=InventoryLimits(
            **{k: v for k, v in inv.limits.items() if k != "max_seconds"}
        ),
        sink=sink,
    )
    await inventories.publish(db, job.id, inv.task_id, report=sink.value)
    assert inv.status == "completed"
    baseline, catalog = b"control-only-closed-checkpoint", b"control-only-catalog"
    row = Copy(
        id=uuid.uuid4(),
        parent_copy_id=world.parent.id,
        generation=2,
        parent_evidence_sha256="c" * 64,
        window_id=world.window_id,
        window_revision=world.item.window_revision,
        input_fingerprint=uuid.uuid4().hex * 2,
        source_inventory_job_id=job.id,
        source_report=sink.value,
        source_report_sha256=copies.sha(sink.value),
        source_complete=True,
        source=deepcopy(inv.source),
        target_location_id=world.parent.target_location_id,
        target=deepcopy(world.parent.target),
        handling=deepcopy(world.parent.handling),
        limits=deepcopy(world.parent.limits),
        baseline=baseline,
        baseline_sha256=copies.sha(baseline),
        catalog=catalog,
        catalog_sha256=copies.sha(catalog),
        summary={
            "counts": dict.fromkeys(copies.STATES, 0),
            "copy_subset_complete": True,
        },
        delta_sequence=0,
        delta_bytes=0,
    )
    db.add(row)
    await db.flush()
    attempt_job = await inventories.jobs.create_job(
        db,
        kind=copies.KIND,
        user_id=world.actor.id,
        celery_task_id=str(uuid.uuid4()),
        payload={
            "copy_id": str(row.id),
            "operation": "verify",
            "migration_ready": False,
        },
    )
    stamp = inventories.now()
    attempt_job.status, attempt_job.started_at, attempt_job.completed_at = (
        "completed",
        stamp,
        stamp,
    )
    attempt = Attempt(
        id=uuid.uuid4(),
        copy_id=row.id,
        async_job_id=attempt_job.id,
        task_id=attempt_job.celery_task_id,
        requested_by=world.actor.id,
        authority={"api_key_id": None, "key_proof": None},
        scope={},
        operation="verify",
        status="completed",
        started_at=stamp,
        completed_at=stamp,
        source_operation_id=uuid.uuid4(),
        target_operation_id=uuid.uuid4(),
        result={
            "attempt_subset_complete": None,
            "selected_count": None,
            "selected_verified_count": None,
        },
    )
    db.add(attempt)
    row.last_attempt_id = attempt.id
    await db.flush()
    return row, attempt, job


def evidence(token):
    return {
        "schema": service.EVIDENCE_SCHEMA,
        "evidence_sha256": "d" * 64,
        **{
            key: token[key]
            for key in ("source_report_sha256", "catalog_sha256", "baseline_sha256")
        },
        "counts": {**dict.fromkeys(service.COUNT_FIELDS, 0), "source_buckets": 9},
        "catalog_summary": {
            "complete": True,
            "enumeration_complete": True,
            "resolution_complete": True,
            "counts": {},
        },
    }


async def test_bind_bumps_only_window_revision_and_is_exactly_idempotent(
    db_session, control
):
    window = await db_session.get(Window, control.window_id)
    gate_revision = window.source_gate_revision
    first = await service.bind(
        db_session, control.actor.id, control.claim, control.native
    )
    assert first.state == "bound" and first.revision == 1 and first.window_revision == 3
    assert window.source_gate_revision == gate_revision
    again = await service.bind(
        db_session,
        control.actor.id,
        {**control.claim, "reserved": True},
        {
            **control.native,
            "journal_revision": 99,
            "observed_at": inventories.now().isoformat(),
        },
    )
    assert again == first
    public = (await windows.item(db_session, window)).model_dump(mode="json")
    assert public["finalization"]["state"] == "bound"
    assert public["source_quiescence_proven"] is public["migration_ready"] is False
    serialized = json.dumps(public)
    for hidden in (
        "reservation_nonce",
        "config_digest",
        "database_identity",
        "operation_id",
        control.context["source"]["endpoint"],
    ):
        assert hidden not in serialized


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_identity", "f" * 64),
        ("expected_window_revision", 1),
        ("reserved", True),
    ],
)
async def test_first_bind_rejects_cross_context_before_mutation(
    db_session, control, field, value
):
    expected = {
        "database_identity": "finalization_database_changed",
        "expected_window_revision": "finalization_changed",
        "reserved": "finalization_unknown",
    }[field]
    await code(
        expected,
        lambda: service.bind(
            db_session,
            control.actor.id,
            {**control.claim, field: value},
            control.native,
        ),
    )
    assert (await db_session.get(Window, control.window_id)).revision == 2
    assert (
        await db_session.get(Finalization, uuid.UUID(control.claim["finalization_id"]))
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "00000000-0000-4000-8000-000000000000"),
        ("config_digest", "f" * 64),
        ("barriers_sha256", "invalid"),
        ("counts", {"bad": True}),
    ],
)
async def test_native_identity_and_safe_observation_are_required(
    db_session, control, field, value
):
    await code(
        "finalization_native_changed",
        lambda: service.bind(
            db_session,
            control.actor.id,
            control.claim,
            {**control.native, field: value},
        ),
    )
    assert (await db_session.get(Window, control.window_id)).revision == 2


async def test_authority_is_rechecked_and_pending_intent_can_be_abandoned(
    db_session, control
):
    control.actor.is_active = False
    await db_session.flush()
    await code(
        "finalization_authority_changed",
        lambda: service.bind(
            db_session, control.actor.id, control.claim, control.native
        ),
    )
    control.actor.is_active = True
    await db_session.flush()
    abandoned = await service.begin_invalidation(
        db_session, control.actor.id, control.claim, 0
    )
    assert abandoned.state == "invalidated" and abandoned.invalidated_at is not None
    assert (await db_session.get(Window, control.window_id)).revision == 2


@pytest.mark.parametrize("unsettled", [False, True])
async def test_unreserved_intent_abandon_after_window_close(
    db_session, control, unsettled
):
    closed = await windows.transition(
        db_session,
        control.window_id,
        control.actor,
        StorageMigrationWindowTransition(action="close", expected_revision=2),
    )
    assert closed.state == "closed" and closed.revision > 2
    if unsettled:
        db_session.add(
            StorageOperation(
                id=uuid.uuid4(),
                location_id=inventories.LEGACY_LOCATION_ID,
                revision=closed.source_gate_revision,
                purpose="native-read",
                node_id="test",
            )
        )
        await db_session.flush()
        await code(
            "finalization_unsettled",
            lambda: service.begin_invalidation(
                db_session, control.actor.id, control.claim, 0
            ),
        )
        assert (
            await db_session.get(
                Finalization, uuid.UUID(control.claim["finalization_id"])
            )
            is None
        )
    else:
        item = await service.begin_invalidation(
            db_session, control.actor.id, control.claim, 0
        )
        assert item.state == "invalidated" and item.window_revision == closed.revision
        assert (await db_session.get(Window, control.window_id)).state == "closed"


@pytest.mark.parametrize("kind", ["operation", "inventory", "attempt"])
async def test_bind_rejects_actual_or_queued_responsibility(db_session, control, kind):
    if kind == "operation":
        db_session.add(
            StorageOperation(
                id=uuid.uuid4(),
                location_id=inventories.LEGACY_LOCATION_ID,
                revision=control.context["source_gate_revision"],
                purpose="native-read",
                node_id="test",
            )
        )
    elif kind == "inventory":
        await inventories.start(
            db_session,
            control.actor,
            StorageMigrationInventoryRequest(window_id=control.window_id),
        )
    else:
        attempt = await copies.last(db_session, control.parent)
        attempt.status = "running"
    await db_session.flush()
    await code(
        "finalization_unsettled",
        lambda: service.bind(
            db_session, control.actor.id, control.claim, control.native
        ),
    )


async def test_other_claim_and_native_drift_cannot_replace_reserved_identity(
    db_session, bound
):
    for changed, native, expected in [
        (
            {**bound.claim, "reservation_nonce": str(uuid.uuid4())},
            bound.native,
            "finalization_changed",
        ),
        (
            bound.claim,
            {**bound.native, "barriers_sha256": "f" * 64},
            "finalization_native_changed",
        ),
    ]:
        await code(
            expected, lambda: service.bind(db_session, bound.actor.id, changed, native)
        )


async def test_full_seal_cas_readonly_and_explicit_invalidation(db_session, bound):
    row, attempt, _ = await child(db_session, bound)
    begin = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    assert begin["item"].state == "sealing" and begin["item"].revision == 2
    assert begin["values"]["baseline"] == row.baseline
    assert (await copies.item(db_session, row)).read_only
    await code("copy_read_only", lambda: copies.writable(db_session, row))
    await code(
        "finalization_changed",
        lambda: service.finish_seal(
            db_session,
            bound.actor.id,
            bound.claim,
            1,
            begin["token"],
            evidence(begin["token"]),
            bound.native,
        ),
    )
    again = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 2, bound.native
    )
    assert again["token"] == begin["token"]
    final = await service.finish_seal(
        db_session,
        bound.actor.id,
        bound.claim,
        2,
        begin["token"],
        evidence(begin["token"]),
        {
            **bound.native,
            "journal_revision": 100,
            "observed_at": inventories.now().isoformat(),
        },
    )
    assert final.state == "sealed" and final.revision == 3 and final.sealed_at
    await code(
        "finalization_active",
        lambda: windows.transition(
            db_session,
            bound.window_id,
            bound.actor,
            StorageMigrationWindowTransition(action="close", expected_revision=3),
        ),
    )
    await code(
        "finalization_active",
        lambda: service.require_resumable(db_session, inventories.LEGACY_LOCATION_ID),
    )
    closing = await service.begin_invalidation(
        db_session, bound.actor.id, bound.claim, 3
    )
    assert closing.state == "invalidating"
    await code(
        "finalization_closing",
        lambda: inventories.start(
            db_session,
            bound.actor,
            StorageMigrationInventoryRequest(window_id=bound.window_id),
        ),
    )
    done = await service.finish_invalidation(
        db_session, bound.actor.id, bound.claim, closing.revision
    )
    assert done.state == "invalidated" and done.sealed_at == final.sealed_at
    assert not await service.frozen_copy(db_session, row.id)
    value = await windows.transition(
        db_session,
        bound.window_id,
        bound.actor,
        StorageMigrationWindowTransition(action="close", expected_revision=3),
    )
    assert value.state == "closed"


@pytest.mark.parametrize(
    "change",
    ["run", "subset", "retry_failed", "missing_permit", "cancelled", "unverified"],
)
async def test_latest_full_verify_required(db_session, bound, change):
    row, attempt, _ = await child(db_session, bound)
    if change == "run":
        attempt.operation = "run"
    elif change == "subset":
        attempt.retry_object_ids = ["f" * 64]
    elif change == "retry_failed":
        attempt.retry_failed = True
    elif change == "missing_permit":
        attempt.source_operation_id = None
    elif change == "cancelled":
        attempt.cancel_requested_at = inventories.now()
    else:
        row.summary = {**row.summary, "copy_subset_complete": False}
    await db_session.flush()
    await code(
        "finalization_verify_required",
        lambda: service.begin_seal(
            db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
        ),
    )


async def test_source_config_drift_blocks_seal_but_not_invalidation(db_session, bound):
    row, _, _ = await child(db_session, bound)
    location = await db_session.get(StorageLocation, inventories.LEGACY_LOCATION_ID)
    location.config_revision += 1
    await db_session.flush()
    await code(
        "finalization_changed",
        lambda: service.begin_seal(
            db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
        ),
    )
    closing = await service.begin_invalidation(
        db_session, bound.actor.id, bound.claim, 1
    )
    assert (
        await service.finish_invalidation(
            db_session, bound.actor.id, bound.claim, closing.revision
        )
    ).state == "invalidated"


async def test_unsettled_physical_receipt_blocks_finish_invalidation(db_session, bound):
    closing = await service.begin_invalidation(
        db_session, bound.actor.id, bound.claim, 1
    )
    operation = StorageOperation(
        id=uuid.uuid4(),
        location_id=inventories.LEGACY_LOCATION_ID,
        revision=bound.context["source_gate_revision"],
        purpose="still-reading",
        node_id="test",
    )
    db_session.add(operation)
    await db_session.flush()
    await code(
        "finalization_unsettled",
        lambda: service.finish_invalidation(
            db_session, bound.actor.id, bound.claim, closing.revision
        ),
    )
    await db_session.delete(operation)
    await db_session.flush()
    assert (
        await service.finish_invalidation(
            db_session, bound.actor.id, bound.claim, closing.revision
        )
    ).state == "invalidated"


async def test_finalization_survives_async_job_cleanup(db_session, bound):
    row, attempt, inventory = await child(db_session, bound)
    begin = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    saved_id = inventory.id
    await db_session.execute(
        delete(AsyncJob).where(AsyncJob.id.in_([inventory.id, attempt.async_job_id]))
    )
    await db_session.flush()
    value = await service.status(
        db_session, bound.actor.id, {**bound.claim, "reserved": True}
    )
    assert value.inventory_job_id == saved_id
    final = await service.finish_seal(
        db_session,
        bound.actor.id,
        bound.claim,
        2,
        begin["token"],
        evidence(begin["token"]),
        bound.native,
    )
    assert final.state == "sealed" and final.inventory_job_id == saved_id


async def test_generation_of_frozen_child_is_rejected(db_session, bound):
    row, _, inventory = await child(db_session, bound)
    await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    # The parent path itself must fence, without relying on a fake successor.
    await code(
        "copy_read_only",
        lambda: copies.generation(
            db_session,
            row.id,
            bound.actor,
            StorageMigrationCopyGenerationRequest(inventory_job_id=inventory.id),
        ),
    )


async def test_changed_checkpoint_revokes_proof_in_same_receipt_transaction(
    db_session, bound
):
    row, _, _ = await child(db_session, bound)
    await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    await service.changed_checkpoint(db_session, row.id)
    await db_session.flush()
    value = await service.status(db_session, bound.actor.id, bound.claim)
    assert (
        value.state == "invalidating"
        and value.error_code == "finalization_checkpoint_changed"
        and value.revision == 3
    )


@pytest.mark.parametrize("sealed", [False, True])
async def test_actual_late_delta_is_saved_after_proof_freeze(db_session, bound, sealed):
    row, attempt, _ = await child(db_session, bound)
    begin = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    if sealed:
        await service.finish_seal(
            db_session,
            bound.actor.id,
            bound.claim,
            2,
            begin["token"],
            evidence(begin["token"]),
            bound.native,
        )
    row.summary = {
        "counts": {**dict.fromkeys(copies.STATES, 0), "pending": 1},
        "copy_subset_complete": False,
    }
    await db_session.flush()
    data = {
        "id": "e" * 64,
        "state": "verified",
        "receipt": {"version_id": "late-actual-ack"},
    }
    await copies.save_delta(
        db_session, attempt.async_job_id, attempt.task_id, data, copies.row_digest(data)
    )
    from app.db.models.storage_migration_copy import StorageMigrationCopyDelta

    receipt = await db_session.get(StorageMigrationCopyDelta, (row.id, data["id"]))
    assert receipt.data["receipt"]["version_id"] == "late-actual-ack"
    value = await service.status(db_session, bound.actor.id, bound.claim)
    assert (
        value.state == "invalidating"
        and value.error_code == "finalization_checkpoint_changed"
    )


async def test_finish_rejects_changed_token_and_native_generation(db_session, bound):
    row, _, _ = await child(db_session, bound)
    begin = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    await code(
        "finalization_checkpoint_changed",
        lambda: service.finish_seal(
            db_session,
            bound.actor.id,
            bound.claim,
            2,
            {**begin["token"], "delta_sequence": 30},
            evidence(begin["token"]),
            bound.native,
        ),
    )
    await code(
        "finalization_native_changed",
        lambda: service.finish_seal(
            db_session,
            bound.actor.id,
            bound.claim,
            2,
            begin["token"],
            evidence(begin["token"]),
            {**bound.native, "barriers_sha256": "f" * 64},
        ),
    )


async def test_claim_nonce_format_and_unknown_reserved_abandon_fail_closed(
    db_session, control
):
    await code(
        "finalization_invalid_claim",
        lambda: service.bind(
            db_session,
            control.actor.id,
            {**control.claim, "reservation_nonce": "e" * 64},
            control.native,
        ),
    )
    await code(
        "finalization_unknown",
        lambda: service.begin_invalidation(
            db_session, control.actor.id, {**control.claim, "reserved": True}, 0
        ),
    )


@pytest.mark.parametrize(
    "mismatch",
    ["verify_count", "buckets", "versions", "delete_markers", "missing_native_count"],
)
async def test_seal_cross_checks_native_full_verify_and_evidence_counts(
    db_session, bound, mismatch
):
    row, attempt, _ = await child(db_session, bound)
    if mismatch == "verify_count":
        row.summary = {
            **row.summary,
            "counts": {**row.summary["counts"], "verified": 1},
        }
        await db_session.flush()
    begin = await service.begin_seal(
        db_session, bound.actor.id, bound.claim, row.id, 1, bound.native
    )
    native = deepcopy(bound.native)
    if mismatch != "verify_count":
        key = "versions" if mismatch == "missing_native_count" else mismatch
        if mismatch == "missing_native_count":
            native["counts"].pop(key)
        else:
            native["counts"][key] += 1
    await code(
        "finalization_invalid_evidence",
        lambda: service.finish_seal(
            db_session,
            bound.actor.id,
            bound.claim,
            2,
            begin["token"],
            evidence(begin["token"]),
            native,
        ),
    )


async def test_bind_concurrent_lock_rolls_back_new_responsibility(reader_world):
    """Use existing committed owners; new durable responsibility stays rolled back."""
    import asyncio

    world = reader_world
    async with world.maker() as db:
        context = await service.preflight(
            db, world.actor_id, world.window_id, world.window_revision
        )
        claim = dict(
            schema=service.CLAIM_SCHEMA,
            finalization_id=str(uuid.uuid4()),
            window_id=str(world.window_id),
            expected_window_revision=world.window_revision,
            database_identity=context["database_identity"],
            reservation_nonce=str(uuid.uuid4()),
            native_operation_id=str(uuid.uuid4()),
            config_digest="a" * 64,
            reserved=False,
        )
        native = dict(
            config_digest=claim["config_digest"],
            operation_id=claim["native_operation_id"],
            journal_revision=3,
            barriers_sha256="b" * 64,
            observed_at=inventories.now().isoformat(),
            counts={"buckets": 9, "versions": 0, "delete_markers": 0},
            source={
                key: context["source"][key] for key in ("endpoint", "purpose_buckets")
            },
        )
        await db.rollback()
    async with world.maker() as first, world.maker() as second:
        await service.preflight(
            first, world.actor_id, world.window_id, world.window_revision
        )
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(
                service.bind(second, world.actor_id, claim, native), 2
            )
        assert error.value.detail["code"] in {
            "finalization_busy",
            "storage_maintenance_busy",
        }
        await second.rollback()
        await first.rollback()
        bound = await service.bind(second, world.actor_id, claim, native)
        assert (
            bound.state == "bound"
            and bound.window_revision == world.window_revision + 1
        )
        assert await second.get(Finalization, uuid.UUID(claim["finalization_id"]))
        await second.rollback()
    async with world.maker() as third:
        assert (
            await third.get(Finalization, uuid.UUID(claim["finalization_id"])) is None
        )
        window = await third.get(Window, world.window_id)
        assert window.revision == world.window_revision and window.state == "stopped"
