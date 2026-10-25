"""Real MVCC roots and transaction fences for exact-version reclamation."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import managed_original_reclamation as observation
from app.services import managed_original_reclaim_fence as fence
from tests.test_managed_original_reclamation import (
    assertion,
    recorded_statements,
    version,
)
from tests.factory import create_project
from tests.test_s3_references import s3_world as s3_world


def expired():
    return datetime.now(timezone.utc) - timedelta(days=1)


async def stopped_gate(db, location_id, revision=1):
    gate = await db.get(StorageMaintenanceGate, location_id)
    if gate is None:
        gate = StorageMaintenanceGate(
            location_id=location_id, state="stopped", revision=revision
        )
        db.add(gate)
    else:
        gate.state, gate.revision = "stopped", revision
    await db.flush()
    return gate


async def test_group_observation_is_select_only_and_eligible_without_static_gates(
    db_session,
):
    row = await assertion(db_session, retain_until=expired())
    with recorded_statements(db_session) as statements:
        result = await observation.observe(db_session, row.id)
    assert len(statements) == 2
    assert "set_config('statement_timeout', '30s', true)" in statements[0]
    assert "set_config('jit', 'off', true)" in statements[0]
    assert statements[1].lstrip().startswith("WITH")
    assert result["blockers"] == []
    assert all(value == 0 for value in result["roots"].values())
    assert result["creation"] is None
    assert isinstance(result["measured_at"], datetime)
    assert (
        result["fingerprint"]
        == (await observation.observe(db_session, row.id))["fingerprint"]
    )
    summary = await observation.reconciliation_observe(db_session)
    assert summary["counts"]["unreferenced_groups"] == 1


async def test_group_observation_peer_and_declared_hold_are_not_pagination_scoped(
    db_session,
):
    row = await assertion(db_session, retain_until=expired())
    await assertion(db_session, object_key=row.object_key)
    result = await observation.observe(db_session, row.id)
    assert result["group"]["peer_assertions"] == 2
    assert "indefinite_retention" in result["blockers"]
    assert (await observation.reconciliation_observe(db_session))["counts"][
        "physical_groups"
    ] == 1


async def test_current_training_receipt_survives_item_cascade_and_unknown_is_explicit(
    db_session, super_admin
):
    row = await assertion(db_session, retain_until=expired())
    project = await create_project(db_session, owner_id=super_admin[0].id)
    receipt = {
        "schema": 1,
        "owner_kind": "dataset_asset",
        "content_id": str(row.asset_id),
        "location_id": str(row.location_id),
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
    }
    retained = await version(
        db_session,
        project.id,
        [{"original": receipt, "source_key": row.object_key}],
        managed_original_contract=1,
    )
    result = await observation.observe(db_session, row.id)
    assert result["roots"]["training_versions"] == 1
    assert result["unknown"]["unknown_training_versions"] == 0
    retained.snapshot = retained.snapshot | {"managed_original_contract": 2}
    await db_session.flush()
    changed = await observation.observe(db_session, row.id)
    assert changed["unknown"]["unknown_training_versions"] == 1
    assert changed["fingerprint"] != result["fingerprint"]


async def test_shared_fence_is_transactional_and_excludes_gc(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    bucket, key = "fence-bucket", "fence/" + uuid.uuid4().hex
    async with sessions() as publication, sessions() as gc:
        await fence.shared(publication, bucket, key)
        with pytest.raises(HTTPException) as busy:
            await fence.exclusive(gc, bucket, key)
        assert busy.value.detail["code"] == "managed_original_reclaim_busy"
        await publication.rollback()
        await fence.exclusive(gc, bucket, key)
        with pytest.raises(HTTPException):
            await fence.shared(publication, bucket, key)
        await gc.rollback()
        await fence.shared(publication, bucket, key)


async def test_actual_unsettled_operation_blocks_even_without_live_item(db_session):
    row = await assertion(db_session, retain_until=expired())
    await stopped_gate(db_session, row.location_id, 5)
    operation = StorageOperation(
        location_id=row.location_id,
        revision=4,
        purpose="original_binary",
        node_id="owned-test",
    )
    db_session.add(operation)
    await db_session.flush()
    result = await observation.observe(db_session, row.id)
    assert result["roots"]["operations"] == 1 and "operations" in result["blockers"]


async def claim(
    db,
    *,
    state="claimed",
    status="pending",
    physical_settled=True,
    actor_id=None,
    with_job=False,
):
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.managed_original_reclaim import (
        ManagedOriginalReclaimAttempt,
        ManagedOriginalReclaimClaim,
    )
    from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.storage import storage_service

    binding = await ensure_legacy_bindings(db)
    creation = ManagedOriginalCreation(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        location_id=LEGACY_LOCATION_ID,
        storage_identity=managed_storage_identity(
            storage_service.client.meta.endpoint_url, binding.bucket
        ),
        bucket=binding.bucket,
        object_key="reclaim-guard/" + uuid.uuid4().hex,
        logical_path="guard.bin",
        origin="zip",
        max_bytes=42,
        timeout_seconds=60,
        content_type="application/octet-stream",
        authority={},
        configuration={},
        input_fingerprint="a" * 64,
        maintenance_revision=1,
    )
    db.add(creation)
    await db.flush()
    row = await assertion(
        db,
        creation_id=creation.id,
        object_key=creation.object_key,
        retain_until=expired(),
    )
    attempt_id, task_id = uuid.uuid4(), str(uuid.uuid4())
    job = None
    if with_job:
        from app.db.models.async_job import AsyncJob

        job = AsyncJob(
            kind="managed_original_reclaim",
            status="running",
            user_id=actor_id,
            celery_task_id=task_id,
            payload={"reclaim_id": str(attempt_id), "retirement_id": str(row.id)},
        )
        db.add(job)
        await db.flush()
    attempt = ManagedOriginalReclaimAttempt(
        id=attempt_id,
        async_job_id=job.id if job else None,
        retirement_id=row.id,
        creation_id=creation.id,
        requested_by=actor_id or uuid.uuid4(),
        task_id=task_id,
        authority={},
        target={
            "location_id": str(row.location_id),
            "bucket": row.bucket,
            "object_key": row.object_key,
        },
        input_fingerprint="b" * 64,
        version_id="accurate-version",
        expected_sha256=row.sha256,
        expected_size_bytes=row.size_bytes,
        maintenance_revision=1,
        reason="Owned guard regression",
        status=status,
        physical_settled=physical_settled,
        **(
            {"completed_at": datetime.now(timezone.utc), "outcome": "unknown"}
            if status == "unknown"
            else {}
        ),
    )
    db.add(attempt)
    await db.flush()
    owner = ManagedOriginalReclaimClaim(
        bucket=row.bucket,
        object_key=row.object_key,
        storage_identity=row.storage_identity,
        creation_id=creation.id,
        version_id=attempt.version_id,
        current_attempt_id=attempt.id,
        state=state,
    )
    db.add(owner)
    await db.flush()
    return row, attempt, owner


@pytest.mark.parametrize("state", ["claimed", "tombstone", "deleted"])
async def test_permanent_claim_blocks_shared_publication(db_session, state):
    row, _, _ = await claim(db_session, state=state)
    with pytest.raises(HTTPException) as rejected:
        await fence.shared(db_session, row.bucket, row.object_key)
    assert rejected.value.detail["code"] == "managed_original_reclaim_claimed"


async def test_proven_released_claim_allows_publication(db_session):
    row, _, _ = await claim(db_session, state="released")
    await fence.shared(db_session, row.bucket, row.object_key)


@pytest.mark.parametrize(
    "state,status,settled,blocked",
    [
        ("claimed", "pending", True, True),
        ("tombstone", "pending", True, True),
        ("tombstone", "unknown", False, True),
        ("tombstone", "unknown", True, False),
    ],
)
async def test_gate_resume_requires_actual_reclaim_settlement(
    db_session, state, status, settled, blocked
):
    row, _, _ = await claim(
        db_session, state=state, status=status, physical_settled=settled
    )
    if blocked:
        with pytest.raises(HTTPException) as rejected:
            await fence.require_resumable(db_session, row.location_id)
        assert rejected.value.detail["code"] == "managed_original_reclaim_unsettled"
    else:
        await fence.require_resumable(db_session, row.location_id)


async def test_legacy_read_options_checks_claim_after_real_admission(
    db_session, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services import original_sources

    # The owned short session uses the caller's verified disposable engine, not
    # a transport or today's binding. Its result must propagate before I/O.
    marker = AsyncMock(
        side_effect=HTTPException(
            409, detail={"code": "managed_original_reclaim_claimed"}
        )
    )
    monkeypatch.setattr(fence, "protect_source", marker)
    source = SimpleNamespace(
        location_id=uuid.UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7")
    )
    with pytest.raises(HTTPException):
        await original_sources.read_options(db_session, source)
    marker.assert_awaited_once()


async def test_reconciliation_unknown_coverage_survives_empty_retirement_table(
    db_session, super_admin
):
    project = await create_project(db_session, owner_id=super_admin[0].id)
    await version(db_session, project.id, [], schema=99)
    result = await observation.reconciliation_observe(db_session)
    assert result["counts"]["physical_groups"] == 0
    assert result["unknown"]["unknown_training_versions"] == 1


async def test_only_valid_current_attempt_can_exclude_its_exact_operation(
    db_session, super_admin
):
    from app.db.models.async_job import AsyncJob

    row, attempt, _ = await claim(db_session, actor_id=super_admin[0].id, with_job=True)
    before = await observation.observe(db_session, row.id)
    job = await db_session.get(AsyncJob, attempt.async_job_id)
    await stopped_gate(db_session, row.location_id)
    operation = StorageOperation(
        location_id=row.location_id,
        revision=1,
        purpose="managed_original_reclaim",
        node_id="owned-test",
    )
    db_session.add(operation)
    await db_session.flush()
    attempt.operation_id = operation.id
    attempt.status, attempt.physical_settled = "running", False
    await db_session.flush()
    valid = await observation.observe(db_session, row.id, reclaim_attempt_id=attempt.id)
    assert valid["roots"]["operations"] == 0
    assert valid["fingerprint"] == before["fingerprint"]
    job.celery_task_id = str(uuid.uuid4())
    await db_session.flush()
    rejected = await observation.observe(
        db_session, row.id, reclaim_attempt_id=attempt.id
    )
    assert rejected["roots"]["operations"] == 1
    assert rejected["fingerprint"] != before["fingerprint"]


async def test_saved_s3_reference_remains_a_training_root_after_alias_item_deleted(
    db_session, super_admin, s3_world
):
    from sqlalchemy import delete, select
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_source import DatasetScanEntry
    from app.services.dataset_references import register_entry, reference_context
    from app.services.training_dataset_sources import capture_source_lineage
    from tests.test_s3_references import scan

    _, actor, _, _, _ = s3_world
    run = await scan(db_session, s3_world)
    entry = await db_session.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )
    result = await register_entry(
        db_session, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    context = await reference_context(db_session, result["item_id"], actor)
    lineage = await capture_source_lineage(db_session, context)
    row = await assertion(
        db_session,
        bucket=entry.observed["bucket"],
        object_key=entry.observed["key"],
        retain_until=expired(),
    )
    project = await create_project(db_session, owner_id=actor.id)
    sample = {
        "source_key": f"__aap_reference__/{result['item_id']}/{context['sha256']}",
        "asset_reference": {
            "schema": 1,
            "dataset_id": str(s3_world[2].id),
            "dataset_item_id": str(result["item_id"]),
            "asset_id": str(context["asset_id"]),
            "sha256": context["sha256"],
            "size_bytes": context["size"],
        },
        "source_lineage": lineage,
    }
    await version(
        db_session,
        project.id,
        [sample],
        managed_original_contract=1,
        source_lineage_contract={"schema": 1, "scope": "accepted-reference"},
    )
    await db_session.execute(
        delete(DatasetItem).where(DatasetItem.id == result["item_id"])
    )
    observed = await observation.observe(db_session, row.id)
    assert observed["roots"]["reference_aliases"] == 0
    assert observed["roots"]["training_versions"] == 1
    assert observed["unknown"]["unknown_training_versions"] == 0


async def test_saved_s3_observation_guard_rejects_claim_before_physical_read(
    db_session, monkeypatch
):
    from unittest.mock import AsyncMock

    marker = AsyncMock(
        side_effect=HTTPException(
            409, detail={"code": "managed_original_reclaim_claimed"}
        )
    )
    monkeypatch.setattr(fence, "shared", marker)
    with pytest.raises(HTTPException):
        await fence.protect_observed_read(
            db_session,
            {"kind": "s3", "bucket": "saved-bucket", "key": "saved-key"},
            {"kind": "s3", "bucket": "saved-bucket"},
        )
    assert marker.await_args.args[1:] == ("saved-bucket", "saved-key")


async def test_claim_refuses_asset_registration_before_new_replica_exists(
    db_session, super_admin
):
    from app.db.models.dataset_asset import DatasetAsset
    from app.services.dataset_assets import register_item_asset
    from tests.test_managed_original_reclamation import live_item
    from sqlalchemy import func, select

    row, _, _ = await claim(db_session)
    item = await live_item(db_session, super_admin[0], row.object_key)
    with pytest.raises(HTTPException) as rejected:
        await register_item_asset(
            db_session, item, sha256=row.sha256, size_bytes=row.size_bytes
        )
    assert rejected.value.detail["code"] == "managed_original_reclaim_claimed"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetAsset)
            .where(DatasetAsset.dataset_item_id == item.id)
        )
        == 0
    )


async def test_claim_refuses_retention_mutation_without_changing_policy(
    db_session, super_admin
):
    from app.services import managed_original_retention as retention
    from tests.test_managed_original_retention import change, confirmed

    row, _, _ = await claim(db_session)
    payload = change(action="add_hold")
    reviewed = await retention.preview(db_session, row.id, payload, super_admin[0])
    with pytest.raises(HTTPException) as rejected:
        await retention.apply(
            db_session, row.id, confirmed(payload, reviewed), super_admin[0]
        )
    assert rejected.value.detail["code"] == "managed_original_reclaim_claimed"
    assert row.policy_revision == 1


async def test_unknown_reclaim_operation_blocks_resume_without_attempt_owner(
    db_session,
):
    from app.services.dataset_assets import LEGACY_LOCATION_ID

    await stopped_gate(db_session, LEGACY_LOCATION_ID)
    db_session.add(
        StorageOperation(
            location_id=LEGACY_LOCATION_ID,
            revision=1,
            purpose="managed_original_reclaim",
            node_id="unknown-native",
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException):
        await fence.require_resumable(db_session, LEGACY_LOCATION_ID)


async def test_reconciliation_aggregates_in_sql_with_bounded_return(db_session):
    import json

    row = await assertion(db_session, retain_until=expired())
    for _ in range(3):
        await assertion(db_session, object_key=row.object_key, retain_until=expired())
    with recorded_statements(db_session) as statements:
        result = await observation.reconciliation_observe(db_session)
    assert len(statements) == 2
    assert "set_config('statement_timeout', '30s', true)" in statements[0]
    assert "set_config('jit', 'off', true)" in statements[0]
    assert "SELECT to_jsonb(g)" not in statements[1]
    assert result["counts"]["total_assertions"] == 4
    assert (
        result["counts"]["physical_groups"]
        == result["counts"]["unreferenced_groups"]
        == 1
    )
    assert set(result) == {"measured_at", "counts", "unknown"}
    assert len(json.dumps(result, default=str)) < 1024


@pytest.mark.parametrize("lineage", [False, True])
async def test_unresolvable_retained_reference_is_unknown_instead_of_zero_roots(
    db_session, super_admin, lineage
):
    row = await assertion(db_session, retain_until=expired())
    project = await create_project(db_session, owner_id=super_admin[0].id)
    item_id = uuid.uuid4()
    sample = {
        "source_key": f"__aap_reference__/{item_id}/" + "a" * 64,
        "asset_reference": {
            "schema": 1,
            "dataset_id": str(uuid.uuid4()),
            "dataset_item_id": str(item_id),
            "asset_id": str(uuid.uuid4()),
            "sha256": "a" * 64,
            "size_bytes": 42,
        },
    }
    fields = {}
    if lineage:
        sample["source_lineage"] = {
            "source_id": str(uuid.uuid4()),
            "location_id": str(uuid.uuid4()),
            "scan_run_id": str(uuid.uuid4()),
            "path_digest": "b" * 64,
            "source_config_revision": 1,
            "location_config_revision": 1,
            "accepted_revision_id": None,
            "previous_revision_id": None,
            "revision_number": None,
            "accepted_at": None,
            "scan_completed_at": None,
            "content_verified_at": None,
        }
        fields["source_lineage_contract"] = {"schema": 1, "scope": "accepted-reference"}
    await version(db_session, project.id, [sample], **fields)
    result = await observation.observe(db_session, row.id)
    assert result["unknown"]["unknown_training_versions"] == 1
    assert "unknown_training_versions" in result["blockers"]


async def test_decision_original_receipt_protects_all_retirement_peers(
    db_session, super_admin
):
    from app.db.models.active_learning import (
        ActiveLearningRound,
        ActiveLearningDecision,
    )
    from tests.sample_evaluation_fixtures import Objects, seed

    row = await assertion(db_session, retain_until=expired())
    peer = await assertion(
        db_session, object_key=row.object_key, retain_until=expired()
    )
    actor = super_admin[0]
    world = await seed(db_session, actor, Objects())
    round = ActiveLearningRound(
        project_id=world.project.id,
        creator_id=actor.id,
        model_version_id=world.model.id,
        deployment_id=world.deployment.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Retained original decision",
        status="failed",
        spec={"schema": 1, "original_contract": 1, "pool": []},
        spec_digest="b" * 64,
    )
    db_session.add(round)
    await db_session.flush()
    snapshot = {
        "source_key": "unrelated-logical-key",
        "dataset_item_id": str(uuid.uuid4()),
        "original": {
            "schema": 1,
            "owner_kind": "dataset_asset",
            "content_id": str(peer.asset_id),
            "location_id": str(peer.location_id),
            "sha256": peer.sha256,
            "size_bytes": peer.size_bytes,
        },
    }
    db_session.add(
        ActiveLearningDecision(
            round_id=round.id,
            actor_id=actor.id,
            request_id=uuid.uuid4(),
            request_digest="c" * 64,
            action="reject",
            snapshot={"source_snapshots": [snapshot]},
            snapshot_digest="d" * 64,
        )
    )
    await db_session.flush()
    result = await observation.observe(db_session, row.id)
    assert result["roots"]["active_learning_roots"] == 1
    assert result["unknown"]["unknown_active_learning_rounds"] == 0
    db_session.add(
        ActiveLearningDecision(
            round_id=round.id,
            actor_id=actor.id,
            request_id=uuid.uuid4(),
            request_digest="e" * 64,
            action="reject",
            snapshot={"source_snapshots": [snapshot | {"source_lineage": {}}]},
            snapshot_digest="f" * 64,
        )
    )
    await db_session.flush()
    unknown = await observation.observe(db_session, row.id)
    assert unknown["unknown"]["unknown_active_learning_rounds"] == 1


async def test_actual_operation_on_another_retirement_peer_location_protects_group(
    db_session,
):
    from app.db.models.dataset_asset import StorageLocation

    row = await assertion(db_session, retain_until=expired())
    location = StorageLocation(
        id=uuid.uuid4(), name="peer-" + uuid.uuid4().hex, kind="filesystem", config={}
    )
    db_session.add(location)
    await db_session.flush()
    await assertion(
        db_session,
        object_key=row.object_key,
        location_id=location.id,
        retain_until=expired(),
    )
    db_session.add(
        StorageMaintenanceGate(location_id=location.id, state="active", revision=1)
    )
    await db_session.flush()
    db_session.add(
        StorageOperation(
            location_id=location.id,
            revision=1,
            purpose="old_alias_read",
            node_id="owned-test",
        )
    )
    await db_session.flush()
    observed = await observation.observe(db_session, row.id)
    assert observed["roots"]["operations"] == 1
    summary = await observation.reconciliation_observe(db_session)
    assert summary["counts"]["total_assertions"] == 2
    assert (
        summary["counts"]["physical_groups"]
        == summary["counts"]["protected_groups"]
        == 1
    )
