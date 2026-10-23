"""Retirement assertions stay bounded, conservative and non-executing."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event, update

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.db.models.managed_original_retention_hold import (
    ManagedOriginalRetentionHold,
)
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import managed_original_reclamation as planner
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from tests.factory import create_project, create_task
from tests.test_dataset_content import headers

PATH = "/api/v1/storage/managed-original-reclamation"


@pytest.fixture
def retirement_admin(super_admin):
    return super_admin[0]


async def assertion(db, **fields):
    binding = await ensure_legacy_bindings(db)
    measured = datetime.now(timezone.utc) - timedelta(days=1)
    values = {
        "id": uuid.uuid4(),
        "assertion_digest": uuid.uuid4().hex + uuid.uuid4().hex,
        "dataset_id": uuid.uuid4(),
        "dataset_item_id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
        "location_id": LEGACY_LOCATION_ID,
        "bucket": binding.bucket,
        "object_key": f"private-retired-original/{uuid.uuid4()}.jpg",
        "role": "managed_original",
        "location_config_revision": 1,
        "binding_generation": binding.generation,
        "storage_identity": managed_storage_identity(
            storage_service.client.meta.endpoint_url, binding.bucket
        ),
        "identity_state": "verified_binding",
        "asset_verification_state": "verified",
        "sha256": "a" * 64,
        "size_bytes": 42,
        "reference_state": "present",
        "verified_at": measured,
        "dataset_lifecycle_revision": 1,
        "retired_at": measured,
        "policy_revision": 1,
        "retain_until": None,
    }
    row = ManagedOriginalRetirement(**(values | fields))
    db.add(row)
    await db.flush()
    return row


async def declared_hold(db, anchor, actor, *, kind="backup", released=False):
    row = ManagedOriginalRetentionHold(
        id=uuid.uuid4(),
        retirement_id=anchor.id,
        kind=kind,
        reference_code=f"declared-{uuid.uuid4().hex}",
        reason="Declared requirement; no recovery proof claimed",
        created_by=actor.id,
        revision=2 if released else 1,
        released_at=datetime.now(timezone.utc) if released else None,
        released_by=actor.id if released else None,
        release_reason="Requirement explicitly released" if released else None,
    )
    db.add(row)
    await db.flush()
    return row


async def release_declared_hold(db, hold, actor):
    hold.released_at = datetime.now(timezone.utc)
    hold.released_by = actor.id
    hold.release_reason = "Requirement explicitly released"
    hold.revision = 2
    await db.flush()


async def version(db, project_id, samples, *, status="ready", **fields):
    snapshot = {"schema": 1, "samples": samples, "classes": []} | fields
    row = TrainingDatasetVersion(
        project_id=project_id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Retained source evidence",
        status=status,
        snapshot=snapshot,
        snapshot_digest="b" * 64,
        manifest={},
    )
    db.add(row)
    await db.flush()
    return row


async def live_item(db, actor, key, *, archived=False):
    dataset = Dataset(
        display_id=f"retired-{uuid.uuid4().hex[:12]}",
        name="Retained root",
        created_by=actor.id,
        archived_at=datetime.now(timezone.utc) if archived else None,
        archived_by=actor.id if archived else None,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="root.jpg",
        file_path=key,
        file_type="image",
    )
    db.add(item)
    await db.flush()
    return item


@contextmanager
def recorded_statements(db):
    collected = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        collected.append(statement)

    event.listen(db.sync_session.bind, "before_cursor_execute", record)
    try:
        yield collected
    finally:
        event.remove(db.sync_session.bind, "before_cursor_execute", record)


def forbid_object_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Reclamation preview must not perform object I/O")

    for name in (
        "head_object",
        "list_objects_v2",
        "get_object",
        "put_object",
        "delete_object",
        "delete_objects",
    ):
        monkeypatch.setattr(storage_service.client, name, forbidden)
    monkeypatch.setattr(storage_service, "delete_prefix", forbidden)


async def test_global_preview_is_read_only_bounded_and_reports_assertion_units(
    db_session, retirement_admin, monkeypatch
):
    db = db_session
    first = await assertion(db)
    # Repeated assertions are intentionally not described as physical objects.
    await assertion(db, object_key=first.object_key, size_bytes=None)
    forbid_object_io(monkeypatch)
    with recorded_statements(db) as statements:
        result = await planner.preview(db, retirement_admin, limit=1)
    assert all(s.lstrip().upper().startswith(("SELECT", "WITH")) for s in statements)
    assert len(statements) == 4  # Actor, complete observation, actor, configuration.
    assert result.unit == "retirement_assertion"
    assert result.counts.total_assertions == result.counts.retained_assertions == 2
    assert result.counts.known_asserted_bytes == 42
    assert result.counts.unknown_size_assertions == 1
    assert result.reclaimable_bytes is None and result.execution_permission is False
    assert len(result.items) == 1 and result.has_more
    assert result.coverage.backup_holds == result.coverage.migration_holds == "unknown"
    assert result.coverage.unregistered_originals == "not_recorded"
    assert result.coverage.declared_requirements == "registered_retirement_peers_only"
    encoded = result.model_dump_json()
    for private in (
        first.object_key,
        first.bucket,
        storage_service.client.meta.endpoint_url,
    ):
        assert private not in encoded
    other = await planner.preview(db, retirement_admin, limit=1, offset=1)
    assert other.counts == result.counts and other.items[0].id != result.items[0].id
    assert not other.has_more
    empty = await planner.preview(db, retirement_admin, offset=10)
    assert empty.items == [] and empty.counts.total_assertions == 2


@pytest.mark.parametrize(
    "peer_identity",
    [
        {},
        {"storage_identity": "f" * 64},
        {
            "identity_state": "unknown",
            "storage_identity": None,
            "identity_error_code": "historical_identity_unknown",
        },
    ],
)
async def test_any_indefinite_peer_protects_complete_group_across_identity_gaps(
    db_session, retirement_admin, peer_identity
):
    db = db_session
    elapsed = datetime.now(timezone.utc) - timedelta(seconds=1)
    first = await assertion(db, retain_until=elapsed, policy_revision=2)
    peer = await assertion(db, object_key=first.object_key, **peer_identity)
    result = await planner.preview(db, retirement_admin, limit=1)
    assert result.counts.total_assertions == result.counts.retained_assertions == 2
    assert result.counts.policy_candidate_assertions == 0
    row = result.items[0]
    assert row.id == first.id and row.policy_revision == 2
    assert row.retain_until == elapsed
    assert row.indefinite_retention and row.effective_retain_until is None
    assert row.peer_assertions == 2 and not row.policy_candidate
    assert "related_indefinite_retention" in row.blockers
    assert "indefinite_retention" not in row.blockers
    second = (await planner.preview(db, retirement_admin, offset=1)).items[0]
    assert second.id == peer.id and "indefinite_retention" in second.blockers


async def test_effective_policy_uses_latest_finite_peer_deadline_and_exact_locator(
    db_session, retirement_admin
):
    db = db_session
    checked = datetime.now(timezone.utc)
    first = await assertion(db, retain_until=checked - timedelta(days=1))
    earlier = await assertion(
        db, object_key=first.object_key, retain_until=checked + timedelta(days=1)
    )
    latest = await assertion(
        db, object_key=first.object_key, retain_until=checked + timedelta(days=2)
    )
    independent = await assertion(
        db,
        object_key=first.object_key + "-neighbor",
        retain_until=checked - timedelta(days=1),
    )
    other_bucket = await assertion(
        db,
        bucket=first.bucket + "-neighbor",
        object_key=first.object_key,
        retain_until=checked - timedelta(days=1),
    )
    result = await planner.preview(db, retirement_admin)
    rows = {row.id: row for row in result.items}
    for owner in (first, earlier, latest):
        row = rows[owner.id]
        assert row.peer_assertions == 3 and not row.indefinite_retention
        assert row.effective_retain_until == latest.retain_until
        assert row.status == "retained" and not row.policy_candidate
    assert "related_retention_window" in rows[first.id].blockers
    assert "retention_window" not in rows[first.id].blockers
    assert "related_retention_window" in rows[earlier.id].blockers
    assert "retention_window" in rows[earlier.id].blockers
    assert "related_retention_window" not in rows[latest.id].blockers
    assert (
        rows[independent.id].peer_assertions
        == rows[other_bucket.id].peer_assertions
        == 1
    )
    assert rows[independent.id].policy_candidate
    assert not rows[other_bucket.id].policy_candidate  # Identity is not current.
    assert result.counts.retained_assertions == 3
    assert result.counts.blocked_assertions == 2
    assert result.counts.policy_candidate_assertions == 1


async def test_declared_holds_protect_peers_and_new_assertions_until_each_is_released(
    db_session, retirement_admin
):
    db = db_session
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    first = await assertion(db, retain_until=expired)
    second = await assertion(db, object_key=first.object_key, retain_until=expired)
    backup = await declared_hold(db, first, retirement_admin)
    other_backup = await declared_hold(db, second, retirement_admin)
    migration = await declared_hold(db, first, retirement_admin, kind="migration")
    await declared_hold(db, second, retirement_admin, released=True)
    added = await assertion(
        db,
        object_key=first.object_key,
        retain_until=expired,
        storage_identity="f" * 64,
    )
    result = await planner.preview(db, retirement_admin)
    assert result.counts.protected_assertions == 3
    assert result.counts.retained_assertions == result.counts.blocked_assertions == 0
    assert result.counts.policy_candidate_assertions == 0
    for row in result.items:
        assert row.peer_assertions == 3
        assert row.declared_backup_holds == 2 and row.declared_migration_holds == 1
        assert row.status == "protected" and not row.policy_candidate
        assert row.retain_until == row.effective_retain_until == expired
        assert not row.indefinite_retention
        assert "declared_backup_hold" in row.blockers
        assert "declared_migration_hold" in row.blockers
        assert all(value == 0 for value in row.roots.model_dump().values())
    assert {row.id for row in result.items} == {first.id, second.id, added.id}
    await release_declared_hold(db, backup, retirement_admin)
    result = await planner.preview(db, retirement_admin)
    assert all(row.declared_backup_holds == 1 for row in result.items)
    assert result.counts.protected_assertions == 3
    await release_declared_hold(db, migration, retirement_admin)
    result = await planner.preview(db, retirement_admin)
    assert all(row.declared_migration_holds == 0 for row in result.items)
    assert all("declared_migration_hold" not in row.blockers for row in result.items)
    assert result.counts.protected_assertions == 3
    await release_declared_hold(db, other_backup, retirement_admin)
    result = await planner.preview(db, retirement_admin)
    assert result.counts.blocked_assertions == 3
    assert result.counts.policy_candidate_assertions == 2
    assert all(
        row.declared_backup_holds == row.declared_migration_holds == 0
        for row in result.items
    )
    assert all(row.policy_candidate for row in result.items if row.id != added.id)
    assert not next(row for row in result.items if row.id == added.id).policy_candidate
    assert result.coverage.backup_holds == result.coverage.migration_holds == "unknown"


async def test_group_hold_status_precedes_indefinite_policy_and_released_history_is_inactive(
    db_session, retirement_admin
):
    db = db_session
    first = await assertion(db)
    await declared_hold(db, first, retirement_admin, kind="migration", released=True)
    before = await planner.preview(db, retirement_admin)
    assert before.items[0].status == "retained"
    assert before.items[0].declared_migration_holds == 0
    await declared_hold(db, first, retirement_admin)
    after = await planner.preview(db, retirement_admin)
    assert after.items[0].status == "protected"
    assert after.items[0].indefinite_retention
    assert (
        after.counts.protected_assertions == 1 and after.counts.retained_assertions == 0
    )


async def test_full_group_counts_before_maximum_page_remain_select_only_and_bounded(
    db_session, retirement_admin, monkeypatch
):
    db = db_session
    first = await assertion(db)
    shared = {
        column.key: getattr(first, column.key)
        for column in ManagedOriginalRetirement.__table__.columns
        if column.key not in {"id", "assertion_digest", "asset_id", "dataset_item_id"}
    }
    db.add_all(
        [
            ManagedOriginalRetirement(
                **shared,
                id=uuid.uuid4(),
                assertion_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                asset_id=uuid.uuid4(),
                dataset_item_id=uuid.uuid4(),
            )
            for _ in range(101)
        ]
    )
    await db.flush()
    await declared_hold(db, first, retirement_admin)
    forbid_object_io(monkeypatch)
    with recorded_statements(db) as statements:
        result = await planner.preview(db, retirement_admin, limit=100)
    assert len(statements) == 4
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH"))
        for statement in statements
    )
    assert result.counts.total_assertions == result.counts.protected_assertions == 102
    assert result.counts.known_asserted_bytes == 102 * first.size_bytes
    assert result.counts.policy_candidate_assertions == 0
    assert len(result.items) == 100 and result.has_more
    assert all(
        row.peer_assertions == 102 and row.declared_backup_holds == 1
        for row in result.items
    )
    last = await planner.preview(db, retirement_admin, limit=100, offset=100)
    assert len(last.items) == 2 and not last.has_more
    assert result.counts == last.counts
    assert not {row.id for row in result.items}.intersection(
        row.id for row in last.items
    )


async def test_past_retention_is_only_a_policy_candidate_and_cannot_authorize_deletion(
    db_session, retirement_admin
):
    await assertion(
        db_session, retain_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    result = await planner.preview(db_session, retirement_admin)
    item = result.items[0]
    assert item.identity_state == "current" and item.policy_candidate
    assert item.status == "blocked" and result.counts.blocked_assertions == 1
    assert set(item.blockers) == {
        "exclusive_ownership_unproven",
        "backup_holds_unknown",
        "migration_holds_unknown",
        "object_presence_not_checked",
    }


@pytest.mark.parametrize(
    ("fields", "blocker"),
    [
        (
            {
                "identity_state": "unknown",
                "storage_identity": None,
                "identity_error_code": "historical_identity_unknown",
            },
            "storage_identity_unknown",
        ),
        ({"storage_identity": "f" * 64}, "storage_identity_mismatch"),
        (
            {"asset_verification_state": "unverified", "sha256": None},
            "content_identity_unverified",
        ),
        ({"size_bytes": None}, "content_identity_unverified"),
        ({"reference_state": "unverified", "verified_at": None}, "locator_unverified"),
    ],
)
async def test_identity_and_content_gaps_never_become_policy_candidates(
    db_session, retirement_admin, fields, blocker
):
    await assertion(
        db_session,
        retain_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        **fields,
    )
    item = (await planner.preview(db_session, retirement_admin)).items[0]
    assert not item.policy_candidate and blocker in item.blockers


async def test_archived_legacy_and_linked_roots_protect_another_assertion_same_locator(
    db_session, retirement_admin
):
    db = db_session
    retired = await assertion(db)
    item = await live_item(db, retirement_admin, retired.object_key, archived=True)
    project = await create_project(db, owner_id=retirement_admin.id)
    task = await create_task(db, project_id=project.id)
    task.file_path, task.dataset_item_id = retired.object_key, item.id
    db.add(
        TaskDatasetItemLink(
            task_id=task.id, dataset_item_id=item.id, role="primary_lidar"
        )
    )
    asset = DatasetAsset(id=uuid.uuid4(), dataset_item_id=item.id, media_type="image")
    db.add(asset)
    await db.flush()
    db.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=retired.bucket,
            object_key=retired.object_key,
            role="original_cache",
        )
    )
    await db.flush()
    result = await planner.preview(db, retirement_admin)
    row = result.items[0]
    assert row.status == "protected" and not row.policy_candidate
    assert (
        row.roots.asset_locations
        == row.roots.dataset_items
        == row.roots.tasks
        == row.roots.task_dataset_links
        == 1
    )
    assert "indefinite_retention" in row.blockers
    assert (
        result.counts.protected_assertions == 1
        and result.counts.retained_assertions == 0
    )


@pytest.mark.parametrize(
    "status", ["pending", "running", "ready", "failed", "cancelled"]
)
async def test_every_retained_training_status_protects_exact_source_locator(
    db_session, retirement_admin, status
):
    db = db_session
    retired = await assertion(db)
    project = await create_project(db, owner_id=retirement_admin.id)
    await version(
        db,
        project.id,
        [{"source_bucket": retired.bucket, "source_key": retired.object_key}],
        status=status,
    )
    row = (await planner.preview(db, retirement_admin)).items[0]
    assert row.roots.training_versions == 1 and row.status == "protected"
    assert "retained_training_version" in row.blockers


@pytest.mark.parametrize(
    "fields",
    [
        {"samples": None},
        {"samples": {}},
        {"samples": [None]},
        {"samples": [{"source_bucket": 123, "source_key": "unknown"}]},
        {"samples": [{"source_bucket": "unknown", "source_key": {}}]},
        {"samples": [{"asset_reference": "bad"}]},
        {"samples": [{"asset_reference": {"asset_id": "not-a-uuid"}}]},
        {"schema": 999},
        {"task": None},
        {"format": None},
        {"source_lineage_contract": None},
        {"asset_source_contract": {"schema": 999}},
    ],
)
async def test_unknown_training_source_shapes_block_without_cast_errors(
    db_session, retirement_admin, fields
):
    db = db_session
    await assertion(db, retain_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    project = await create_project(db, owner_id=retirement_admin.id)
    samples = fields.get("samples", [])
    await version(
        db,
        project.id,
        samples,
        **{key: value for key, value in fields.items() if key != "samples"},
    )
    result = await planner.preview(db, retirement_admin)
    assert result.counts.unknown_training_versions == 1
    assert not result.items[0].policy_candidate
    assert "unsupported_training_snapshot" in result.items[0].blockers


async def test_reference_identity_protects_retired_peer_assertion_same_locator(
    db_session, retirement_admin
):
    db = db_session
    retired = await assertion(db)
    peer = await assertion(db, object_key=retired.object_key)
    project = await create_project(db, owner_id=retirement_admin.id)
    sample = {
        "source_key": f"__aap_reference__/{peer.dataset_item_id}/{peer.sha256}",
        "asset_reference": {
            "schema": 1,
            "dataset_id": str(peer.dataset_id),
            "dataset_item_id": str(peer.dataset_item_id),
            "asset_id": str(peer.asset_id),
            "sha256": peer.sha256,
            "size_bytes": peer.size_bytes,
        },
    }
    await version(db, project.id, [sample])
    result = await planner.preview(db, retirement_admin)
    assert result.counts.unknown_training_versions == 0
    assert all(row.roots.training_versions == 1 for row in result.items)


@pytest.mark.parametrize(
    "changed",
    [
        None,
        {"source_config_revision": "1"},
        {"accepted_revision_id": {}},
        {"revision_number": -1},
        {"content_verified_at": "2026-10-24TgarbageZ"},
    ],
)
async def test_malformed_declared_reference_lineage_is_an_unknown_blocker(
    db_session, retirement_admin, changed
):
    db = db_session
    retired = await assertion(
        db, retain_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    project = await create_project(db, owner_id=retirement_admin.id)
    sample = {
        "source_key": f"__aap_reference__/{retired.dataset_item_id}/{retired.sha256}",
        "asset_reference": {
            "schema": 1,
            "dataset_id": str(retired.dataset_id),
            "dataset_item_id": str(retired.dataset_item_id),
            "asset_id": str(retired.asset_id),
            "sha256": retired.sha256,
            "size_bytes": retired.size_bytes,
        },
    }
    if changed is not None:
        sample["source_lineage"] = {
            "source_id": str(uuid.uuid4()),
            "location_id": str(LEGACY_LOCATION_ID),
            "scan_run_id": str(uuid.uuid4()),
            "path_digest": "a" * 64,
            "source_config_revision": 1,
            "location_config_revision": 1,
            "accepted_revision_id": None,
            "previous_revision_id": None,
            "revision_number": None,
            "accepted_at": None,
            "scan_completed_at": None,
            "content_verified_at": None,
        } | changed
    await version(
        db,
        project.id,
        [sample],
        source_lineage_contract={"schema": 1, "scope": "accepted-reference"},
    )
    result = await planner.preview(db, retirement_admin)
    assert result.counts.unknown_training_versions == 1
    assert "unsupported_training_snapshot" in result.items[0].blockers
    assert not result.items[0].policy_candidate


async def test_legacy_task_other_bucket_is_ambiguous_instead_of_unused(
    db_session, retirement_admin
):
    db = db_session
    retired = await assertion(
        db, retain_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    project = await create_project(db, owner_id=retirement_admin.id)
    task = await create_task(db, project_id=project.id)
    task.file_path = retired.object_key
    await db.flush()
    row = (await planner.preview(db, retirement_admin)).items[0]
    if storage_service.bucket == storage_service.datasets_bucket:
        assert row.roots.tasks == 1
    else:
        assert row.roots.tasks == 0 and row.roots.ambiguous_legacy_tasks == 1
        assert "legacy_task_bucket_unknown" in row.blockers
    assert not row.policy_candidate


async def test_active_learning_failed_history_retains_source_snapshot_and_unknowns(
    db_session, retirement_admin
):
    from app.db.models.active_learning import ActiveLearningRound
    from tests.sample_evaluation_fixtures import Objects, seed

    db = db_session
    retired = await assertion(
        db, retain_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    world = await seed(db, retirement_admin, Objects())
    history = ActiveLearningRound(
        project_id=world.project.id,
        creator_id=retirement_admin.id,
        model_version_id=world.model.id,
        deployment_id=world.deployment.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Historical source root",
        status="failed",
        spec={
            "schema": 1,
            "pool": [
                {
                    "snapshot": {
                        "source_bucket": retired.bucket,
                        "source_key": retired.object_key,
                    }
                }
            ],
        },
        spec_digest="b" * 64,
    )
    db.add(history)
    await db.flush()
    row = (await planner.preview(db, retirement_admin)).items[0]
    assert row.status == "protected" and row.roots.active_learning_roots == 1
    history.spec = {"schema": 1, "pool": [None]}
    await db.flush()
    result = await planner.preview(db, retirement_admin)
    assert result.counts.unknown_active_learning_rounds == 1
    assert "unsupported_active_learning_snapshot" in result.items[0].blockers
    assert not result.items[0].policy_candidate


async def test_api_requires_current_superadmin_and_star_key_scope(
    db_session, retirement_admin, super_admin, project_admin, httpx_client
):
    from app.services import api_key_service

    await assertion(db_session)
    denied = await httpx_client.get(PATH, headers=headers(project_admin))
    assert denied.status_code == 403
    created, raw = await api_key_service.create_key(
        db_session, retirement_admin, name="read-only", scopes=["datasets:read"]
    )
    response = await httpx_client.get(PATH, headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403
    ok = await httpx_client.get(PATH, headers=headers(super_admin))
    assert ok.status_code == 200 and ok.json()["execution_permission"] is False
    assert (
        await httpx_client.get(PATH + "?limit=101", headers=headers(super_admin))
    ).status_code == 422
    assert created.id


async def test_role_revocation_after_observation_rejects_result(
    db_session, retirement_admin, monkeypatch
):
    await assertion(db_session)
    original = planner.require_admin
    calls = 0

    async def revoked(db, actor_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(403, "Revoked")
        return await original(db, actor_id)

    monkeypatch.setattr(planner, "require_admin", revoked)
    with pytest.raises(HTTPException) as error:
        await planner.preview(db_session, retirement_admin)
    assert error.value.status_code == 403


async def test_configuration_change_after_observation_is_not_returned_as_current(
    db_session, retirement_admin, monkeypatch
):
    from app.db.models.dataset_asset import StorageBinding

    await assertion(db_session)
    original = planner.require_admin
    calls = 0

    async def changed(db, actor_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "datasets")
                .values(generation=2)
            )
        return await original(db, actor_id)

    monkeypatch.setattr(planner, "require_admin", changed)
    with pytest.raises(HTTPException) as error:
        await planner.preview(db_session, retirement_admin)
    assert error.value.status_code == 409
