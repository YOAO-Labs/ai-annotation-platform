"""Real PostgreSQL all-nine CAS and rollback; native proofs are synthetic.

No provider or producer qualification is claimed by these control-plane tests.
"""

from copy import deepcopy
from importlib import util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset_asset import StorageBinding
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.storage_migration_cutovers import StorageMigrationCutover as Cutover
from app.schemas.storage_maintenance import StorageMaintenanceRequest
from app.schemas.storage_migration_window import StorageMigrationWindowTransition
from app.services import (
    storage_maintenance,
    storage_migration_cutovers as service,
    storage_migration_finalizations as finals,
    storage_migration_read_publications as reads,
    storage_migration_windows as windows,
)
from tests.test_storage_migration_finalizations import (
    bound as bound,
    control as control,
    code,
)
from tests.test_storage_migration_read_publications import publish, sealed as sealed
from tests.test_storage_migration_windows import prepared as prepared


async def review(db, world):
    context = await service.prepare_activation(
        db,
        world.actor.id,
        world.claim,
        world.final.revision,
        world.publication.revision,
        world.native,
    )
    return service.issue_review(
        context,
        deepcopy(context),
        {purpose: {"qualified_probe": True} for purpose in service.PURPOSES},
    )


@pytest.fixture
async def active(db_session, sealed):
    await publish(db_session, sealed)
    sealed.review = await review(db_session, sealed)
    sealed.cutover = await service.activate(
        db_session, sealed.actor.id, sealed.claim, sealed.review, sealed.native
    )
    return sealed


async def vector(db):
    return {
        row.purpose: service.binding_facts(row)
        for row in await db.scalars(
            select(StorageBinding).where(StorageBinding.purpose.in_(service.PURPOSES))
        )
    }


async def drain(db, world):
    world.draining = await service.begin_rollback(
        db, world.actor.id, uuid.UUID(world.cutover["id"]), world.cutover["revision"]
    )
    context = await service.rollback_preflight(
        db, world.actor.id, uuid.UUID(world.cutover["id"]), world.draining["revision"]
    )
    world.b_claim = {
        "schema": service.CLAIM_SCHEMA,
        "cutover_id": world.cutover["id"],
        "expected_cutover_revision": context["revision"],
        "database_identity": context["database_identity"],
        "reservation_nonce": str(uuid.uuid4()),
        "native_operation_id": str(uuid.uuid4()),
        "config_digest": "c" * 64,
        "reserved": False,
    }
    world.b_native = {
        "config_digest": world.b_claim["config_digest"],
        "operation_id": world.b_claim["native_operation_id"],
        "journal_revision": 4,
        "barriers_sha256": "d" * 64,
        "observed_at": world.native["observed_at"],
        "counts": {"buckets": 1, "versions": 3},
        "source": context["target_source"],
    }
    return context


async def bind(db, world):
    await drain(db, world)
    return await service.bind_rollback(
        db,
        world.actor.id,
        uuid.UUID(world.cutover["id"]),
        world.draining["revision"],
        world.b_claim,
        world.b_native,
    )


async def finish_source(db, world, restoring):
    pub = await reads.begin_withdrawal(
        db, world.actor.id, world.claim, world.publication.revision
    )
    await reads.finish_withdrawal(db, world.actor.id, world.claim, pub.revision)
    final = await finals.begin_invalidation(
        db, world.actor.id, world.claim, world.final.revision
    )
    await finals.finish_invalidation(db, world.actor.id, world.claim, final.revision)
    window = await windows.owner(db, world.window_id)
    await windows.transition(
        db,
        world.window_id,
        world.actor,
        StorageMigrationWindowTransition(
            action="close", expected_revision=window.revision
        ),
    )
    return await service.finish_rollback(
        db,
        world.actor.id,
        uuid.UUID(restoring["id"]),
        restoring["revision"],
        world.b_native,
    )


async def test_review_requires_all_probes_and_unchanged_capture(db_session, sealed):
    await publish(db_session, sealed)
    context = await service.prepare_activation(
        db_session,
        sealed.actor.id,
        sealed.claim,
        sealed.final.revision,
        sealed.publication.revision,
        sealed.native,
    )
    with pytest.raises(HTTPException) as error:
        service.issue_review(context, context, {"datasets": {"probe": True}})
    assert error.value.detail["code"] == "cutover_probe_required"
    changed = {**context, "target_gate_revision": context["target_gate_revision"] + 1}
    with pytest.raises(HTTPException):
        service.issue_review(context, changed, {})
    await code(
        "cutover_preview_invalid",
        lambda: service.activate(
            db_session,
            sealed.actor.id,
            sealed.claim,
            {"context": context},
            sealed.native,
        ),
    )
    assert await db_session.scalar(select(Cutover.id)) is None
    assert await vector(db_session) == context["before_bindings"]


async def test_finalization_without_cutover_keeps_existing_invalidation(
    db_session, sealed
):
    pub = await publish(db_session, sealed)
    withdrawing = await reads.begin_withdrawal(
        db_session, sealed.actor.id, sealed.claim, pub.revision
    )
    await reads.finish_withdrawal(
        db_session, sealed.actor.id, sealed.claim, withdrawing.revision
    )
    final = await finals.begin_invalidation(
        db_session, sealed.actor.id, sealed.claim, sealed.final.revision
    )
    done = await finals.finish_invalidation(
        db_session, sealed.actor.id, sealed.claim, final.revision
    )
    assert (
        done.state == "invalidated"
        and await db_session.scalar(select(Cutover.id)) is None
    )


async def test_atomic_all_nine_owner_and_exact_replay(db_session, active):
    assert await vector(db_session) == active.cutover["after_bindings"]
    assert set(active.cutover["before_bindings"]) == service.PURPOSES
    assert active.cutover["id"] == str(active.publication.publication_id)
    assert json.loads(json.dumps(active.cutover)) == active.cutover
    again = await service.activate(
        db_session,
        active.actor.id,
        {**active.claim, "reserved": True},
        active.review,
        {**active.native, "journal_revision": 100},
    )
    assert again == active.cutover
    assert await vector(db_session) == active.cutover["after_bindings"]
    assert len(list(await db_session.scalars(select(Cutover)))) == 1


@pytest.mark.parametrize("phase", ["active", "rollback_draining", "rollback_bound"])
async def test_replay_checks_current_vector_and_never_adopts_drift(
    db_session, active, phase
):
    if phase == "rollback_draining":
        await drain(db_session, active)
    elif phase == "rollback_bound":
        await bind(db_session, active)
    changed = await db_session.get(StorageBinding, "annotations")
    changed.generation += 1
    await db_session.flush()
    before = await vector(db_session)
    await code(
        "cutover_bindings_changed",
        lambda: service.activate(
            db_session, active.actor.id, active.claim, active.review, active.native
        ),
    )
    await code(
        "cutover_bindings_changed",
        lambda: service.begin_rollback(
            db_session, active.actor.id, uuid.UUID(active.cutover["id"]), 1
        ),
    )
    assert await vector(db_session) == before


@pytest.mark.parametrize("purpose", sorted(service.PURPOSES))
async def test_any_changed_binding_rejects_whole_activation(
    db_session, sealed, purpose
):
    await publish(db_session, sealed)
    signed = await review(db_session, sealed)
    binding = await db_session.get(StorageBinding, purpose)
    binding.generation += 1
    await db_session.flush()
    expected = await vector(db_session)
    await code(
        "cutover_preview_changed",
        lambda: service.activate(
            db_session, sealed.actor.id, sealed.claim, signed, sealed.native
        ),
    )
    assert await vector(db_session) == expected
    assert await db_session.scalar(select(Cutover.id)) is None


async def test_signed_review_cannot_be_modified_or_reused_by_other_actor(
    db_session, active, project_admin
):
    changed = deepcopy(active.review)
    changed["capabilities"]["datasets"] = {"changed": True}
    await code(
        "cutover_preview_invalid",
        lambda: service.activate(
            db_session, active.actor.id, active.claim, changed, active.native
        ),
    )
    await code(
        "finalization_authority_changed",
        lambda: service.status(
            db_session, project_admin[0].id, uuid.UUID(active.cutover["id"])
        ),
    )


async def test_rollback_waits_real_operation_and_blocks_generic_resume(
    db_session, active
):
    await drain(db_session, active)
    target_id = uuid.UUID(active.cutover["target_location_id"])
    operation = StorageOperation(
        location_id=target_id,
        revision=active.draining["target_gate_revision"],
        purpose="owned_write",
        node_id="test",
    )
    db_session.add(operation)
    await db_session.flush()
    await code(
        "cutover_unsettled",
        lambda: service.bind_rollback(
            db_session,
            active.actor.id,
            uuid.UUID(active.cutover["id"]),
            active.draining["revision"],
            active.b_claim,
            active.b_native,
        ),
    )
    assert await vector(db_session) == active.cutover["after_bindings"]
    await code(
        "cutover_active",
        lambda: storage_maintenance.review(
            db_session,
            target_id,
            StorageMaintenanceRequest(
                operation="resume",
                expected_revision=active.draining["target_gate_revision"],
            ),
            active.actor,
            None,
        ),
    )
    await code(
        "cutover_active",
        lambda: finals.begin_invalidation(
            db_session, active.actor.id, active.claim, active.final.revision
        ),
    )
    await db_session.delete(operation)
    await db_session.flush()
    result = await service.bind_rollback(
        db_session,
        active.actor.id,
        uuid.UUID(active.cutover["id"]),
        active.draining["revision"],
        active.b_claim,
        active.b_native,
    )
    assert result["state"] == "rollback_bound"


async def test_normal_stopped_gate_and_bound_claim_replay(db_session, active):
    await drain(db_session, active)
    target = await db_session.get(Gate, uuid.UUID(active.cutover["target_location_id"]))
    target.state, target.revision = "stopped", target.revision + 1
    await db_session.flush()
    first = await service.bind_rollback(
        db_session,
        active.actor.id,
        uuid.UUID(active.cutover["id"]),
        active.draining["revision"],
        active.b_claim,
        active.b_native,
    )
    second = await service.bind_rollback(
        db_session,
        active.actor.id,
        uuid.UUID(active.cutover["id"]),
        first["revision"],
        {**active.b_claim, "reserved": True},
        active.b_native,
    )
    assert first == second and first["revision"] == 3
    assert await vector(db_session) == service.rollback_bindings(
        await db_session.get(Cutover, uuid.UUID(first["id"]))
    )
    await code(
        "cutover_active",
        lambda: finals.begin_invalidation(
            db_session, active.actor.id, active.claim, active.final.revision
        ),
    )


@pytest.mark.parametrize("change", ["source", "operation", "cutover", "epoch"])
async def test_b_claim_cannot_borrow_a_or_another_cutover(db_session, active, change):
    await drain(db_session, active)
    claim, native = deepcopy(active.b_claim), deepcopy(active.b_native)
    expected = "finalization_native_changed"
    if change == "source":
        native["source"] = deepcopy(active.native["source"])
    elif change == "operation":
        native["operation_id"] = str(uuid.uuid4())
    elif change == "cutover":
        claim["cutover_id"] = str(uuid.uuid4())
        expected = "cutover_invalid_claim"
    else:
        claim["expected_cutover_revision"] += 1
        expected = "cutover_changed"
    await code(
        expected,
        lambda: service.bind_rollback(
            db_session,
            active.actor.id,
            uuid.UUID(active.cutover["id"]),
            active.draining["revision"],
            claim,
            native,
        ),
    )
    assert await vector(db_session) == active.cutover["after_bindings"]


async def test_full_restore_keeps_b_native_claim_and_history(db_session, active):
    result = await bind(db_session, active)
    identifier, release = uuid.UUID(result["id"]), uuid.uuid4()
    restoring = await service.begin_restoration(
        db_session, active.actor.id, identifier, result["revision"], release
    )
    assert restoring["source_release_operation_id"] == str(release)
    await code(
        "cutover_changed",
        lambda: service.begin_restoration(
            db_session, active.actor.id, identifier, restoring["revision"], uuid.uuid4()
        ),
    )
    await code(
        "cutover_changed",
        lambda: service.finish_rollback(
            db_session,
            active.actor.id,
            identifier,
            restoring["revision"],
            active.b_native,
        ),
    )
    done = await finish_source(db_session, active, restoring)
    assert done["state"] == "rolled_back" and done["revision"] == 5
    assert done["rollback_claim"] == active.b_claim
    assert done["before_bindings"] == active.cutover["before_bindings"]
    assert done["after_bindings"] == active.cutover["after_bindings"]
    assert (
        await db_session.get(Gate, uuid.UUID(done["target_location_id"]))
    ).state == "active"
    assert (
        await service.finish_rollback(
            db_session, active.actor.id, identifier, 4, active.b_native
        )
        == done
    )
    assert (
        await service.begin_restoration(
            db_session, active.actor.id, identifier, 3, release
        )
        == done
    )
    assert (await service.status(db_session, active.actor.id, identifier))[
        "source_claim"
    ]["reserved"] is False


@pytest.mark.parametrize(
    "change", ["delete", "vector", "review", "generation", "transition"]
)
async def test_database_retains_group_identity_and_order(db_session, active, change):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            statement = delete(Cutover).where(
                Cutover.id == uuid.UUID(active.cutover["id"])
            )
            if change != "delete":
                values = {"revision": 2}
                if change == "vector":
                    values["before_bindings"] = {}
                elif change == "review":
                    values["review"] = {}
                elif change == "generation":
                    facts = deepcopy(active.cutover["after_bindings"])
                    facts["datasets"]["generation"] += 1
                    values["after_bindings"] = facts
                else:
                    values["state"] = "rolled_back"
                statement = (
                    update(Cutover)
                    .where(Cutover.id == uuid.UUID(active.cutover["id"]))
                    .values(**values)
                )
            await db_session.execute(statement)


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0264_storage_migration_cutovers.py"
    )
    spec = util.spec_from_file_location("cutover_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_empty_schema_roundtrip(db_session):
    connection = await db_session.connection()
    await connection.run_sync(lambda conn: migration(conn).downgrade())
    await connection.run_sync(lambda conn: migration(conn).upgrade())


@pytest.mark.parametrize("rolled_back", [False, True])
async def test_nonempty_schema_downgrade_is_refused(db_session, active, rolled_back):
    if rolled_back:
        bound = await bind(db_session, active)
        restoring = await service.begin_restoration(
            db_session,
            active.actor.id,
            uuid.UUID(bound["id"]),
            bound["revision"],
            uuid.uuid4(),
        )
        done = await finish_source(db_session, active, restoring)
        assert done["state"] == "rolled_back" and done["rollback_claim"] is not None
    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="retain native responsibility"):
        await connection.run_sync(lambda conn: migration(conn).downgrade())
