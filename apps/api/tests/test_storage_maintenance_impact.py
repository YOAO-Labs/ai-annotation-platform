"""Stored maintenance dependencies remain bounded, honest and read-only."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import delete, event, func, select, update

from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageLocation,
)
from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSource,
    DatasetSourcePreview,
    DatasetSourceSyncSchedule,
)
from app.db.models.original_cache import OriginalCacheEntry, OriginalCacheReadLease
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.storage_connection import StorageConnection
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.schemas.dataset_source import SourceLifecycleChange
from app.services import (
    content_snapshot,
    dataset_source_lifecycle,
    dataset_source_revisions,
    filesystem_storage,
    reference_storage,
    storage_maintenance_impact as maintenance,
)
from app.services.dataset_references import reference_context
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest
from app.services.training_dataset_sources import capture_source_lineage
from tests.factory import create_api_key, create_project, create_task
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_source_revisions import candidate


def path(location):
    return f"/api/v1/storage-locations/{location.id}/maintenance-impact"


async def location(db, *, kind="filesystem", connection_id=None):
    row = StorageLocation(
        id=uuid.uuid4(),
        name=f"maintenance-{uuid.uuid4().hex}",
        kind=kind,
        connection_id=connection_id,
        config={
            "root_handle": "offline-private-root",
            "relative_path": "private-source-directory",
            "scope": "restricted",
            "use_user_ids": [],
        },
    )
    db.add(row)
    await db.flush()
    return row


async def dataset(db, actor):
    row = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:12]}",
        name="Maintenance scope",
        created_by=actor.id,
    )
    db.add(row)
    await db.flush()
    return row


async def source(db, where, *, dataset_id=None, **fields):
    row = DatasetSource(
        location_id=where.id,
        dataset_id=dataset_id,
        name="Retained maintenance source",
        config={"relative_path": "private-subdirectory"},
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def read(client, where, actor):
    response = await client.get(path(where), headers=headers(actor))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["location_id"] == str(where.id)
    assert result["config_revision"] == where.config_revision
    assert datetime.fromisoformat(result["measured_at"]).utcoffset() is not None
    assert result["scope"] == "stored_dependencies"
    assert result["frozen_coverage"] == "recorded_location_lineage_only"
    assert result["input_coverage"] == "current_task_links_only"
    assert result["execution_permission"] is False
    assert all(type(value) is int and value >= 0 for value in result["counts"].values())
    assert "private-source-directory" not in response.text
    assert "offline-private-root" not in response.text
    return result


def forbid_source_io(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Maintenance impact must not access source or object bytes")

    for name in (
        "location_directory",
        "capture_anchor",
        "probe_directory",
        "scan_directory",
    ):
        monkeypatch.setattr(filesystem_storage, name, unexpected)
    monkeypatch.setattr(reference_storage, "configuration", unexpected)
    monkeypatch.setattr(reference_storage, "content_file", unexpected)
    monkeypatch.setattr(content_snapshot, "capture", unexpected)
    for name in (
        "head_object",
        "list_objects_v2",
        "get_object",
        "put_object",
        "delete_object",
    ):
        monkeypatch.setattr(storage_service.client, name, unexpected)


@contextmanager
def statements(db):
    collected = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if (
            not statement.lstrip()
            .upper()
            .startswith(("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT "))
        ):
            collected.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield collected
    finally:
        event.remove(engine, "before_cursor_execute", record)


def after_aggregate(monkeypatch, db, mutation):
    """Inject a change after real SQL, before the endpoint's second authority check."""
    original_dependencies, execute = maintenance.dependencies, db.execute
    calls = []

    def marked(location_id):
        return original_dependencies(location_id).execution_options(
            maintenance_test_checkpoint=True
        )

    async def changed(statement, *args, **kwargs):
        result = await execute(statement, *args, **kwargs)
        if statement.get_execution_options().get("maintenance_test_checkpoint"):
            calls.append(True)
            await mutation(execute)
        return result

    monkeypatch.setattr(maintenance, "dependencies", marked)
    monkeypatch.setattr(db, "execute", changed)
    return calls


async def key_headers(db, actor, scopes):
    key, plaintext = await create_api_key(db, actor.id, "maintenance read", scopes)
    await db.commit()
    return key, {"Authorization": "Bearer " + plaintext}


async def test_empty_offline_location_is_read_only_and_query_count_is_fixed(
    db_session, super_admin, httpx_client, monkeypatch
):
    db = db_session
    where = await location(db)
    where.enabled = False
    await db.commit()
    forbid_source_io(monkeypatch)
    with statements(db) as initial_queries:
        empty = await read(httpx_client, where, super_admin)
    assert empty["counts"] and not any(empty["counts"].values())
    assert all(
        sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in initial_queries
    )
    # The result must not grow or issue one query per retained source/page.
    for index in range(45):
        await source(
            db,
            where,
            enabled=index % 2 == 0 and index % 3 != 0,
            unregistered_at=datetime.now(timezone.utc) if index % 3 == 0 else None,
        )
    await db.commit()
    with statements(db) as populated_queries:
        populated = await read(httpx_client, where, super_admin)
    assert populated["counts"]["sources"] == 45
    assert populated["counts"]["enabled_sources"] == 15
    assert populated["counts"]["unregistered_sources"] == 15
    assert len(populated_queries) == len(initial_queries) <= 8
    assert all(
        sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in populated_queries
    )
    assert sum("jsonb_array_elements" in sql for sql in populated_queries) == 1
    assert set(populated) == set(empty)
    assert set(populated["counts"]) == set(empty["counts"])


@pytest.mark.parametrize("kind", ["s3", "sftp"])
async def test_external_connection_is_supported_but_platform_storage_is_not(
    db_session, super_admin, httpx_client, monkeypatch, kind
):
    connection = StorageConnection(
        name="Disconnected private S3",
        kind=kind,
        scope="owner",
        created_by=super_admin[0].id,
        config={"endpoint": "http://offline.invalid:9000", "bucket": "private-bucket"},
        secret_enc=b"unavailable-encrypted-credentials",
    )
    db_session.add(connection)
    await db_session.flush()
    external = await location(db_session, kind=kind, connection_id=connection.id)
    external.enabled = False
    platform = await location(db_session, kind="s3")
    await db_session.commit()
    forbid_source_io(monkeypatch)
    result = await read(httpx_client, external, super_admin)
    assert not any(result["counts"].values())
    assert "offline.invalid" not in str(result) and "credentials" not in str(result)
    response = await httpx_client.get(path(platform), headers=headers(super_admin))
    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]["code"] == "platform_maintenance_impact_unsupported"
    )
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{uuid.uuid4()}/maintenance-impact",
        headers=headers(super_admin),
    )
    assert response.status_code == 404


async def test_real_accepted_history_and_current_relationships_are_deduplicated(
    db_session, reference_world, super_admin, httpx_client, monkeypatch
):
    db = db_session
    registered, project, old_task, world = reference_world
    where, retained = world[4:6]
    # Keep a genuinely accepted older item/task, then accept changed bytes.
    world, registration, change, payload = await candidate(db, world)
    accepted = await dataset_source_revisions.accept(
        db, registration.id, change["path_digest"], payload, world[2]
    )
    assert accepted["revision_number"] == 2
    assert accepted["item_id"] != registered["item_id"]
    tasks = list(await db.scalars(select(Task).where(Task.project_id == project.id)))
    assert len(tasks) == 2
    for task in tasks:
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=super_admin[0].id,
                class_name="car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            )
        )
    # An item reached by revision, immutable reference and two locators is one item.
    old_asset = await db.scalar(
        select(DatasetAsset).where(
            DatasetAsset.dataset_item_id == old_task.dataset_item_id
        )
    )
    for bucket in ("replica-a", "replica-b"):
        db.add(
            DatasetAssetLocation(
                asset_id=old_asset.id,
                location_id=where.id,
                bucket=bucket,
                object_key="private/replica.png",
            )
        )
    managed_dataset = await dataset(db, super_admin[0])
    managed_item = DatasetItem(
        dataset_id=managed_dataset.id,
        file_name="managed.png",
        file_path="private/managed.png",
    )
    db.add(managed_item)
    await db.flush()
    managed_asset = DatasetAsset(
        id=uuid.uuid4(), dataset_item_id=managed_item.id, media_type="image"
    )
    db.add(managed_asset)
    await db.flush()
    db.add(
        DatasetAssetLocation(
            asset_id=managed_asset.id,
            location_id=where.id,
            bucket="managed",
            object_key="private/managed.png",
        )
    )
    without_tasks = await create_project(db, owner_id=super_admin[0].id)
    db.add_all(
        [
            ProjectDataset(project_id=without_tasks.id, dataset_id=world[3].id),
            ProjectDataset(project_id=without_tasks.id, dataset_id=managed_dataset.id),
        ]
    )
    # A current task is sufficient even without a ProjectDataset association.
    task_only_project = await create_project(db, owner_id=super_admin[0].id)
    task_only = await create_task(db, project_id=task_only_project.id)
    task_only.dataset_item_id = old_task.dataset_item_id
    task_only.file_path = old_task.file_path
    for index in range(22):
        await source(db, where, dataset_id=world[3].id, enabled=index != 0)
    deleted_dataset = await dataset(db, super_admin[0])
    orphan = await source(
        db,
        where,
        dataset_id=deleted_dataset.id,
        enabled=False,
        unregistered_at=datetime.now(timezone.utc),
    )
    await db.execute(delete(Dataset).where(Dataset.id == deleted_dataset.id))
    await db.refresh(orphan)
    assert orphan.dataset_id is None
    elsewhere = await location(db)
    await source(db, elsewhere, dataset_id=world[3].id)
    impact = await dataset_source_lifecycle.impact(db, retained, world[2])
    await dataset_source_lifecycle.change(
        db,
        retained.id,
        SourceLifecycleChange(
            action="unregister",
            expected_revision=retained.config_revision,
            impact_proof=impact["proof"],
        ),
        world[2],
    )
    await db.commit()
    forbid_source_io(monkeypatch)
    with statements(db) as queries:
        counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["sources"] == 24
    assert counts["enabled_sources"] == 21
    assert counts["unregistered_sources"] == 2
    assert counts["items"] == 3 and counts["reference_items"] == 2
    assert counts["datasets"] == 2
    assert counts["projects"] == 3 and counts["tasks"] == 3
    assert counts["annotations"] == 2
    assert len(queries) <= 8
    assert all(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in queries)


async def test_active_runs_survive_job_retention_and_schedules_use_enabled_state(
    db_session, super_admin, httpx_client, monkeypatch
):
    db = db_session
    where, elsewhere = await location(db), await location(db)
    for index, (target, status) in enumerate(
        [
            (where, "pending"),
            (where, "running"),
            (where, "completed"),
            (elsewhere, "running"),
        ]
    ):
        retained = await source(db, target)
        job = AsyncJob(
            kind="dataset_scan", status="failed" if index < 2 else "running", payload={}
        )
        db.add(job)
        await db.flush()
        scan = DatasetScanRun(
            source_id=retained.id, async_job_id=job.id, snapshot={}, status=status
        )
        db.add(scan)
        await db.flush()
        completed_scan = DatasetScanRun(
            source_id=retained.id, snapshot={}, status="completed", complete=True
        )
        db.add(completed_scan)
        await db.flush()
        registration = DatasetRegistrationRun(
            scan_run_id=completed_scan.id,
            source_id=retained.id,
            snapshot={},
            status=status,
        )
        db.add(registration)
        db.add(
            DatasetSourceSyncSchedule(
                source_id=retained.id,
                actor_id=super_admin[0].id,
                enabled=index != 2,
                state="blocked" if index == 0 else "idle",
            )
        )
        if index == 1:
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
            await db.refresh(scan)
            assert scan.async_job_id is None
    await db.commit()
    forbid_source_io(monkeypatch)
    counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["active_scans"] == 2
    assert counts["active_registrations"] == 2
    assert counts["enabled_schedules"] == 2
    assert counts["items"] == counts["tasks"] == 0


def cache_entry(scope, physical, now, *, state="ready", size=10):
    return OriginalCacheEntry(
        id=uuid.uuid4(),
        scope_location_id=scope,
        source_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        sha256="a" * 64,
        size_bytes=size,
        source_revision=1,
        policy_revision=1,
        location_revision=1,
        location_id=physical,
        bucket="platform-original-cache",
        binding_generation=1,
        storage_identity="b" * 64,
        state=state,
        uncertain_write=state == "uploading",
        last_accessed_at=now,
        expires_at=now + timedelta(days=1),
        writer_until=now - timedelta(days=1),
        retain_until=now,
        next_check_at=now,
    )


def input_lease(task_id, now, *, state="active", expired=False):
    return PredictionInputLease(
        contract_version=1,
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        owner_kind="batch_predict",
        task_id=task_id,
        bucket="platform-inputs",
        storage_identity="c" * 64,
        state=state,
        expires_at=now + timedelta(days=-1 if expired else 1),
        retain_until=now,
        next_cleanup_at=now,
    )


async def test_cache_orphans_preview_destinations_and_current_input_links(
    db_session, reference_world, super_admin, httpx_client, monkeypatch
):
    db = db_session
    registered, _, task, world = reference_world
    where, retained = world[4:6]
    platform = await location(db, kind="s3")
    elsewhere = await location(db)
    now = await db.scalar(select(func.statement_timestamp()))
    entries = [
        cache_entry(where.id, platform.id, now, state=state, size=size)
        for state, size in [("ready", 10), ("uploading", 20), ("deleting", 30)]
    ]
    # Physical destination and a surviving source ID cannot change quota ownership.
    foreign = cache_entry(elsewhere.id, where.id, now, size=400)
    foreign.source_id = retained.id
    db.add_all([*entries, foreign])
    await db.flush()
    for entry, days in [
        (entries[0], 1),
        (entries[0], 2),
        (entries[1], -1),
        (foreign, 1),
    ]:
        db.add(
            OriginalCacheReadLease(
                id=uuid.uuid4(),
                entry_id=entry.id,
                expires_at=now + timedelta(days=days),
            )
        )
    db.add_all(
        [
            input_lease(task.id, now),
            input_lease(task.id, now, expired=True),
            input_lease(task.id, now, state="cleanup_pending", expired=True),
            input_lease(uuid.uuid4(), now),
            input_lease(uuid.uuid4(), now, state="cleanup_pending"),
        ]
    )
    asset = await db.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == task.dataset_item_id)
    )
    preview = DatasetSourcePreview(
        source_id=retained.id,
        path_digest=world[7].path_digest,
        asset_id=asset.id,
        source_sha256=registered["sha256"],
        spec="stored-preview-only",
        policy_revision=1,
        state="ready",
        location_id=platform.id,
        bucket="platform-preview",
        object_key="missing-is-still-a-record.webp",
        sha256="d" * 64,
        size_bytes=123,
    )
    db.add(preview)
    await db.commit()
    forbid_source_io(monkeypatch)
    with statements(db) as queries:
        counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["cache_entries"] == 3 and counts["cache_bytes"] == 60
    assert counts["active_cache_read_leases"] == 2
    # Expiry is cleanup evidence; this view reports the stored input state only.
    assert counts["active_input_leases"] == 2 and counts["cleanup_input_leases"] == 1
    assert counts["ready_previews"] == 1 and counts["preview_bytes"] == 123
    assert counts["failed_previews"] == 0
    assert all(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in queries)
    preview.state = "failed"
    preview.size_bytes = 999
    await db.commit()
    counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["ready_previews"] == counts["preview_bytes"] == 0
    assert counts["failed_previews"] == 1
    foreign_counts = (await read(httpx_client, elsewhere, super_admin))["counts"]
    assert foreign_counts["cache_entries"] == 1 and foreign_counts["cache_bytes"] == 400
    assert foreign_counts["active_cache_read_leases"] == 1
    assert (
        foreign_counts["active_input_leases"] == foreign_counts["failed_previews"] == 0
    )


async def frozen_version(db, project_id, snapshot, status):
    digest = content_digest(snapshot)
    row = TrainingDatasetVersion(
        project_id=project_id,
        request_id=uuid.uuid4(),
        request_digest=digest,
        name="Retained location lineage",
        status=status,
        snapshot=snapshot,
        snapshot_digest=digest,
        manifest={},
    )
    db.add(row)
    await db.flush()
    return row


async def test_frozen_versions_use_recorded_lineage_once_and_keep_historical_coverage_explicit(
    db_session, reference_world, super_admin, httpx_client, monkeypatch
):
    db = db_session
    _, project, task, world = reference_world
    where = world[4]
    context = await reference_context(
        db, task.dataset_item_id, world[2], task_id=task.id
    )
    lineage = await capture_source_lineage(db, context)
    assert lineage["accepted_revision_id"] and lineage["location_id"] == str(where.id)
    identity = {
        "schema": 1,
        "dataset_id": str(world[3].id),
        "dataset_item_id": str(task.dataset_item_id),
        "asset_id": str(context["asset_id"]),
        "sha256": context["sha256"],
        "size_bytes": context["size"],
    }
    sample = {
        "task_id": str(task.id),
        "asset_reference": identity,
        "source_lineage": lineage,
    }
    for status in ("ready", "pending", "running", "failed", "cancelled"):
        await frozen_version(
            db,
            project.id,
            {"classes": [], "samples": [deepcopy(sample), deepcopy(sample)]},
            status,
        )
    # A ready version owned by another project still protects the location.
    independent = await create_project(db, owner_id=super_admin[0].id)
    await frozen_version(db, independent.id, {"samples": [sample]}, "ready")
    for samples in (
        None,
        "historical-non-array",
        [],
        [{"task_id": str(task.id), "asset_reference": identity}],
        [{"location_id": str(where.id)}],
        [{"source_lineage": None}],
        [{"source_lineage": {**lineage, "location_id": str(uuid.uuid4())}}],
    ):
        await frozen_version(db, project.id, {"samples": samples}, "ready")
    await frozen_version(db, project.id, {}, "running")
    # Current source state no longer establishes these frozen associations.
    world[5].enabled = False
    world[5].dataset_id = None
    await db.commit()
    forbid_source_io(monkeypatch)
    counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["ready_lineage_versions"] == 2
    assert counts["unfinished_lineage_versions"] == 2


@pytest.mark.parametrize(
    "principal", ["project_admin", "annotator", "reviewer", "inactive", "demoted"]
)
async def test_only_current_superadmin_can_observe_dependencies(
    db_session,
    super_admin,
    project_admin,
    annotator,
    reviewer,
    httpx_client,
    monkeypatch,
    principal,
):
    where = await location(db_session)
    actor = {
        "project_admin": project_admin,
        "annotator": annotator,
        "reviewer": reviewer,
    }.get(principal, super_admin)
    if principal == "inactive":
        super_admin[0].is_active = False
    elif principal == "demoted":
        super_admin[0].role = "project_admin"
    await db_session.commit()
    monkeypatch.setattr(
        maintenance,
        "dependencies",
        lambda *_: pytest.fail("Unauthorized dependency aggregation"),
    )
    response = await httpx_client.get(path(where), headers=headers(actor))
    assert response.status_code in {401, 403}, response.text
    assert "counts" not in response.json()


@pytest.mark.parametrize(
    "scopes,allowed",
    [
        (["datasets:read"], True),
        (["*"], True),
        (["datasets:write"], False),
        ([], False),
    ],
)
async def test_api_key_requires_read_scope(
    db_session, super_admin, httpx_client, monkeypatch, scopes, allowed
):
    where = await location(db_session)
    _, auth = await key_headers(db_session, super_admin[0], scopes)
    if not allowed:
        monkeypatch.setattr(
            maintenance,
            "dependencies",
            lambda *_: pytest.fail("Missing read scope must fail before aggregation"),
        )
    response = await httpx_client.get(path(where), headers=auth)
    assert response.status_code == (200 if allowed else 403), response.text
    if allowed:
        assert not any(response.json()["counts"].values())
    else:
        assert "counts" not in response.json()


@pytest.mark.parametrize("change", ["revoke", "rotate", "scope", "expire", "owner"])
async def test_key_authority_changes_after_aggregate_hide_the_result(
    db_session, super_admin, project_admin, httpx_client, monkeypatch, change
):
    db = db_session
    where = await location(db)
    key, auth = await key_headers(db, super_admin[0], ["datasets:read"])

    async def mutation(execute):
        values = {
            "revoke": {"revoked_at": datetime.now(timezone.utc)},
            "rotate": {"key_hash": "changed-authority-proof"},
            "scope": {"scopes": []},
            "expire": {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)},
            "owner": {"user_id": project_admin[0].id},
        }[change]
        await execute(update(ApiKey).where(ApiKey.id == key.id).values(**values))

    calls = after_aggregate(monkeypatch, db, mutation)
    response = await httpx_client.get(path(where), headers=auth)
    assert calls == [True]
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "api_key_authority_revoked"
    assert "counts" not in response.json()


@pytest.mark.parametrize(
    "change", ["project_admin", "employee", "inactive", "revision", "delete"]
)
async def test_post_aggregation_actor_and_location_rechecks_reject_stale_results(
    db_session, super_admin, httpx_client, monkeypatch, change
):
    db = db_session
    where = await location(db)
    await db.commit()

    async def mutation(execute):
        if change in {"project_admin", "employee"}:
            await execute(
                update(User).where(User.id == super_admin[0].id).values(role=change)
            )
        elif change == "inactive":
            await execute(
                update(User).where(User.id == super_admin[0].id).values(is_active=False)
            )
        elif change == "revision":
            await execute(
                update(StorageLocation)
                .where(StorageLocation.id == where.id)
                .values(config_revision=StorageLocation.config_revision + 1)
            )
        else:
            await execute(delete(StorageLocation).where(StorageLocation.id == where.id))

    calls = after_aggregate(monkeypatch, db, mutation)
    response = await httpx_client.get(path(where), headers=headers(super_admin))
    assert calls == [True]
    assert "counts" not in response.json()
    if change == "revision":
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "storage_location_revision_changed"
    else:
        assert response.status_code == (404 if change == "delete" else 403), (
            response.text
        )
