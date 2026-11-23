"""Pure retirement fences and isolated-database recovery without live providers."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.export_output import ExportOutput
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import async_job
from app.services import export_media_retirement as retirement
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import export_outputs, export_storage
from tests import test_export_media_bundle_models as model_tests
from tests.factory import create_project
from tests.test_export_media_storage import Client


def owner(**changes):
    values = model_tests._values()
    values.update(
        lease_token=None,
        fixture_run_id=None,
        lease_expires_at=None,
        published_at=None,
        retired_at=None,
        last_attempted_at=None,
    )
    values.update(changes)
    values["storage_ref"] = physical.make_ref(values["id"], bucket="saved-media")
    return SimpleNamespace(**values)


class PolicyDB:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


async def store_policy(db, actor, row, job, replacement=None):
    project = await create_project(db, owner_id=actor.id)
    job_id = row.job_id
    if job is not None:
        stored = await async_job.create_job(
            db,
            kind=job.kind,
            user_id=actor.id,
            project_id=project.id,
            payload=job.payload,
        )
        stored.status = job.status
        row.job_id = stored.id
    if replacement is not None and replacement.id != row.id:
        project_id = (
            project.id
            if replacement.project_id == row.project_id
            else replacement.project_id
        )
        await database_owner(
            db,
            id=replacement.id,
            project_id=project_id,
            job_id=row.job_id if replacement.job_id == job_id else replacement.job_id,
            input_digest=replacement.input_digest,
            input_snapshot={
                "schema": replacement.input_schema,
                "probe_contract": 1,
                "references": {},
                "project": {"id": str(project_id), "data_type": "video"},
                "tasks": [],
                "media": [],
            },
        )
    row.project_id = project.id
    await database_owner(db, id=row.id, project_id=row.project_id, job_id=row.job_id)


@pytest.mark.parametrize("state", ["building", "ready", "retiring"])
async def test_published_latch_survives_missing_job_until_exact_expiry(state):
    row = owner(state=state, published_at=datetime.now(timezone.utc))
    assert not await retirement.attempt_ended(PolicyDB(), row)
    assert await retirement.attempt_ended(PolicyDB(), row, current=row.expires_at)


@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "cancelled", None]
)
async def test_unpublished_attempt_end_is_explicit_and_never_a_job_age_timeout(
    status, db_session, super_admin
):
    row = owner()
    job = (
        None
        if status is None
        else SimpleNamespace(
            id=row.job_id,
            project_id=row.project_id,
            kind="export",
            status=status,
            payload={},
        )
    )
    expected = status not in {"pending", "running"}
    await store_policy(db_session, super_admin, row, job)
    assert await retirement.attempt_ended(db_session, row) is expected
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await retirement.attempt_ended(db_session, row)


def replacement_claim(key, schema):
    row = owner()
    replacement = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=row.project_id,
        job_id=row.job_id,
        input_digest="b" * 64,
        input_schema=schema,
    )
    proof = {
        "schema": schema,
        "bundle_id": str(replacement.id),
        "input_digest": replacement.input_digest,
    }
    job = SimpleNamespace(
        kind="export",
        status="running",
        project_id=row.project_id,
        payload={key: proof},
    )
    return row, job, replacement


@pytest.mark.parametrize(
    ("key", "schema", "accepted"),
    [
        ("lidar_media", 1, True),
        ("lidar_media", 2, True),
        ("lidar_media", 3, False),
        ("lidar_media", 4, False),
        ("image_media", 1, False),
        ("image_media", 2, False),
        ("image_media", 3, True),
        ("image_media", 4, False),
        ("video_media", 1, False),
        ("video_media", 2, False),
        ("video_media", 3, False),
        ("video_media", 4, True),
    ],
)
async def test_replacement_must_match_its_media_protocol(
    key, schema, accepted, db_session, super_admin
):
    row, job, replacement = replacement_claim(key, schema)
    await store_policy(db_session, super_admin, row, job, replacement)
    assert await retirement.attempt_ended(db_session, row) is accepted


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_project",
        "foreign_job",
        "digest",
        "schema",
        "extra",
        "missing",
        "same",
        "input_schema",
        "both",
    ],
)
async def test_unproven_replacement_does_not_retire_bundle(
    fault, db_session, super_admin
):
    row, job, replacement = replacement_claim("lidar_media", 1)
    proof = job.payload["lidar_media"]
    if fault == "foreign_project":
        replacement.project_id = uuid.uuid4()
    elif fault == "foreign_job":
        replacement.job_id = uuid.uuid4()
    elif fault == "digest":
        proof["input_digest"] = "c" * 64
    elif fault == "schema":
        proof["schema"] = True
    elif fault == "extra":
        proof["lease"] = "renewed"
    elif fault == "same":
        proof["bundle_id"] = str(row.id)
    elif fault == "input_schema":
        replacement.input_schema = 2
    elif fault == "both":
        job.payload["image_media"] = proof
    elif fault == "missing":
        replacement = None
    await store_policy(db_session, super_admin, row, job, replacement)
    assert not await retirement.attempt_ended(db_session, row)


@pytest.mark.parametrize("unknown_close", [False, True])
async def test_delete_lifetime_keeps_permit_through_constructor_and_real_close(
    monkeypatch, unknown_close
):
    row = owner(state="retiring", retired_at=datetime.now(timezone.utc))
    receipt = retirement.snapshot(row)
    client, active, events = Client(), [False], []
    for key in row.inventory["objects"]:
        client.add(key)
    if unknown_close:
        client.close_failure = OSError("unknown close")
    permit = SimpleNamespace(
        id=uuid.uuid4(), location_id=physical.LEGACY_LOCATION_ID, revision=1
    )

    async def acquire(*args, **kwargs):
        active[0] = True
        events.append("acquire")
        return permit

    async def guard(*args, **kwargs):
        assert active[0]
        events.append("guard")

    async def settle(value):
        assert value is permit and client.close_count == 1
        active[0] = False
        events.append("settle")

    def construct():
        assert active[0]
        events.append("construct")
        return client

    monkeypatch.setattr(transfer, "_acquire", acquire)
    monkeypatch.setattr(transfer, "_settle", settle)
    monkeypatch.setattr(retirement, "_guard", guard)
    monkeypatch.setattr(physical, "create_client", construct)
    monkeypatch.setattr(retirement.removal, "recorder", lambda *args: None)
    if unknown_close:
        with pytest.raises(physical.ExportMediaStorageError) as error:
            await retirement._physical_delete(None, receipt)
        assert error.value.storage_operation_unsettled
        assert active[0] and events == ["acquire", "guard", "construct"]
    else:
        await retirement._physical_delete(None, receipt)
        assert not active[0] and events[-2:] == ["guard", "settle"]
    assert all(params["Bucket"] == "saved-media" for _, params in client.calls)


async def test_cancelled_delete_drains_before_closing_client_and_permit(monkeypatch):
    row = owner(state="retiring", retired_at=datetime.now(timezone.utc))
    client, entered, release = Client(), threading.Event(), threading.Event()
    for key in row.inventory["objects"]:
        client.add(key)
    original_delete = client.delete_object

    def blocking(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_delete(**kwargs)

    client.delete_object = blocking
    permit = SimpleNamespace(id=uuid.uuid4())
    settled = AsyncMock()
    monkeypatch.setattr(transfer, "_acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(transfer, "_settle", settled)
    monkeypatch.setattr(retirement, "_guard", AsyncMock())
    monkeypatch.setattr(physical, "create_client", lambda: client)
    monkeypatch.setattr(retirement.removal, "recorder", lambda *args: None)
    pending = asyncio.create_task(
        retirement._physical_delete(None, retirement.snapshot(row))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and client.close_count == 0
        settled.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert client.close_count == 1
    settled.assert_awaited_once_with(permit)


async def test_private_fixture_inventory_requires_owner_before_physical_deletion(
    monkeypatch,
):
    created = datetime.now(timezone.utc)
    row = owner(
        fixture_run_id=uuid.uuid4(),
        created_at=created,
        expires_at=created + timedelta(minutes=15),
    )
    client = Client()
    for key in row.inventory["objects"]:
        client.add(key, b"x" * row.inventory["objects"][key]["size_bytes"])
    db = PolicyDB()
    monkeypatch.setattr(retirement, "lock_owner", AsyncMock(return_value=row))
    monkeypatch.setattr(retirement, "has_operations", AsyncMock(return_value=False))
    session = AsyncMock(return_value=False)
    monkeypatch.setattr(retirement, "fixture_owner_session", session)
    permit = SimpleNamespace(
        id=uuid.uuid4(), location_id=physical.LEGACY_LOCATION_ID, revision=1
    )
    monkeypatch.setattr(transfer, "_acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(transfer, "_settle", AsyncMock())
    monkeypatch.setattr(retirement, "_guard", AsyncMock())
    monkeypatch.setattr(physical, "create_client", lambda: client)
    monkeypatch.setattr(retirement.removal, "recorder", lambda *args: None)

    async def start(_db, identifier, at):
        assert identifier == row.id
        row.state, row.retired_at = "retiring", at
        return at

    async def finish(_db, identifier, _at):
        assert identifier == row.id
        row.state = "deleted"
        return True

    monkeypatch.setattr(retirement.data, "start_media_retirement", start)
    monkeypatch.setattr(retirement.data, "finish_media_retirement", finish)

    assert not await retirement._cleanup_one(db, row.id)
    assert row.state == "building"
    assert all(client.rows[key] for key in row.inventory["objects"])
    assert not client.calls

    session.return_value = True
    assert await retirement._cleanup_one(db, row.id)
    assert row.state == "deleted"
    assert all(not client.rows[key] for key in row.inventory["objects"])
    assert any(action == "delete" for action, _ in client.calls)


@pytest.mark.parametrize("native", [False, True])
async def test_scheduled_fixture_zip_stops_before_physical_io(monkeypatch, native):
    bundle = owner(fixture_run_id=uuid.uuid4())
    output = SimpleNamespace(
        id=bundle.zip_output_id,
        project_id=bundle.project_id,
        job_id=bundle.job_id,
        state="ready",
    )
    db = PolicyDB()
    monkeypatch.setattr(
        export_outputs,
        "_cleanup_locked",
        AsyncMock(return_value=(output, None if native else bundle)),
    )
    monkeypatch.setattr(
        export_outputs,
        "fixture_native_lifetime",
        AsyncMock(return_value=object() if native else None),
    )
    monkeypatch.setattr(
        retirement, "fixture_owner_session", AsyncMock(return_value=False)
    )

    def forbid_delete(*args, **kwargs):
        raise AssertionError("ordinary cleanup reached physical ZIP deletion")

    monkeypatch.setattr(export_storage, "delete", forbid_delete)
    monkeypatch.setattr(retirement, "delete_zip", forbid_delete)
    assert not await export_outputs._cleanup_one(db, output.id, {"removed": 0})
    assert output.state == "ready"


@pytest.mark.parametrize(
    "protection", [None, "legacy", "published", "operation", "running"]
)
async def test_pending_zip_has_its_own_retirement_and_preserves_legacy_pending(
    monkeypatch, protection
):
    bundle = owner()
    output = SimpleNamespace(
        id=bundle.zip_output_id,
        project_id=bundle.project_id,
        job_id=bundle.job_id,
        media_bundle_id=bundle.id,
        media="image",
        format="pointmask",
        file_count=1,
        sha256="a" * 64,
        size_bytes=11,
        state="pending",
        upload_proof=None,
        created_at=bundle.created_at,
        expires_at=bundle.expires_at,
        retired_at=None,
        last_attempted_at=None,
        storage_ref={
            **{
                key: value
                for key, value in bundle.storage_ref.items()
                if key != "key_prefix"
            },
            "object_key": export_storage.key_for(
                "image", bundle.project_id, bundle.job_id, bundle.zip_output_id
            ),
        },
    )
    if protection == "published":
        bundle.published_at = datetime.now(timezone.utc)
    if protection == "legacy":
        output.media_bundle_id = None
    db = PolicyDB()
    remove_output = AsyncMock(return_value=True)
    monkeypatch.setattr(
        export_outputs,
        "_cleanup_locked",
        AsyncMock(return_value=(output, None if protection == "legacy" else bundle)),
    )
    monkeypatch.setattr(
        export_outputs, "fixture_native_lifetime", AsyncMock(return_value=None)
    )

    async def stage(_db, identifier, at):
        assert identifier == output.id
        output.state, output.retired_at = "retired", at
        return 0

    monkeypatch.setattr(export_outputs.data, "start_zip_retirement", stage)
    monkeypatch.setattr(
        export_outputs.data, "zip_retirement_unreferenced", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(export_outputs.data, "remove_retired_zip", remove_output)
    monkeypatch.setattr(
        retirement, "attempt_ended", AsyncMock(return_value=protection != "running")
    )
    monkeypatch.setattr(
        retirement, "has_operations", AsyncMock(return_value=protection == "operation")
    )

    async def delete_zip(db, expected_owner, receipt):
        assert output.state == "retired" and db.committed
        assert expected_owner is bundle and receipt.id == output.id
        assert bundle.state == "building" and bundle.inventory

    remove = AsyncMock(side_effect=delete_zip)
    monkeypatch.setattr(retirement, "delete_zip", remove)
    result = await export_outputs._cleanup_one(db, output.id, {"removed": 0})
    assert result is (protection is None)
    if protection is None:
        remove.assert_awaited_once()
        assert remove_output.await_args.args[:2] == (db, output.id)
        assert bundle.state == "building" and bundle.inventory
    else:
        assert output.state == "pending"
        remove.assert_not_awaited()
        remove_output.assert_not_awaited()


async def database_owner(db, **changes):
    values = model_tests._values(**changes)
    values["storage_ref"] = physical.make_ref(values["id"], bucket="saved-media")
    row = Bundle(**values)
    db.add(row)
    await db.flush()
    return row


def objects(monkeypatch, *rows):
    client = Client()
    for row in rows:
        for key in row.inventory["objects"]:
            client.add(key)
    monkeypatch.setattr(physical, "create_client", lambda: client)
    return client


@pytest.fixture
async def retirement_queue(gc_db):
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    media, write, read, first, later = [
        await database_owner(gc_db, created_at=start + timedelta(minutes=i))
        for i in range(5)
    ]
    for purpose in (
        f"export_media_{media.id.hex}",
        f"export_write_{write.zip_output_id.hex}",
        f"export_read_{read.zip_output_id.hex}",
    ):
        location = uuid.uuid4()
        gc_db.add(StorageMaintenanceGate(location_id=location))
        await gc_db.flush()
        gc_db.add(
            StorageOperation(
                id=uuid.uuid4(),
                location_id=location,
                revision=1,
                purpose=purpose,
                node_id="unknown-close",
                started_at=start - timedelta(days=100),
            )
        )
    await gc_db.commit()
    return (media, write, read), first, later


async def test_database_unknown_operations_do_not_starve_next_candidate_before_limit(
    gc_db, retirement_queue, monkeypatch
):
    protected, first, later = retirement_queue
    client = objects(monkeypatch, *protected, first, later)
    counts = await retirement.cleanup(gc_db, limit=1)
    assert counts == {"deleted": 1, "retained": 0, "failed": 0}
    states = dict((await gc_db.execute(select(Bundle.id, Bundle.state))).all())
    assert states == {
        **{row.id: "building" for row in (*protected, later)},
        first.id: "deleted",
    }
    assert await gc_db.scalar(select(func.count()).select_from(StorageOperation)) == 3
    assert {
        params["Key"] for action, params in client.calls if action == "delete"
    } == set(first.inventory["objects"])


async def test_database_locked_candidate_is_skipped_without_waiting(gc_db, monkeypatch):
    db = gc_db
    first, second = await database_owner(db), await database_owner(db)
    await db.commit()
    objects(monkeypatch, first, second)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as blocker:
        await blocker.scalar(
            select(Bundle).where(Bundle.id == first.id).with_for_update()
        )
        try:
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            assert (await retirement.cleanup(db, limit=1))["deleted"] == 1
            current = await db.get(Bundle, second.id, populate_existing=True)
            assert current.state == "deleted"
            assert (await db.get(Bundle, first.id)).state == "building"
        finally:
            await blocker.rollback()


async def test_database_failed_partial_delete_retries_without_dropping_inventory(
    gc_db, monkeypatch
):
    db = gc_db
    values = model_tests._values()
    values["inventory"]["objects"][
        physical.prefix_for(values["id"]) + str(uuid.uuid4()) + ".bin"
    ] = {
        "sha256": "b" * 64,
        "size_bytes": 11,
    }
    values["object_count"], values["size_bytes"] = 2, 22
    row = await database_owner(db, **values)
    await db.commit()
    client = objects(monkeypatch, row)
    expected_inventory = deepcopy(row.inventory)
    original = client.delete_object
    entered, release = threading.Event(), threading.Event()

    def fail_after_delete(**kwargs):
        entered.set()
        assert release.wait(5)
        original(**kwargs)
        raise OSError("partial delete failed")

    client.delete_object = fail_after_delete
    cleanup = asyncio.create_task(retirement.cleanup(db))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with async_sessionmaker(db.bind)() as observer:
            durable = await observer.get(Bundle, row.id)
            assert durable.state == "retiring" and durable.retired_at is not None
            assert durable.inventory == expected_inventory
            retired_at = durable.retired_at
    finally:
        release.set()
        counts = await cleanup
    assert counts["failed"] == 1
    assert sum(bool(versions) for versions in client.rows.values()) == 1
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 0
    client.delete_object = original
    assert (await retirement.cleanup(db))["deleted"] == 1
    await db.refresh(row)
    assert (row.state, row.retired_at, row.inventory) == (
        "deleted",
        retired_at,
        expected_inventory,
    )
    assert not any(client.rows.values())


async def test_database_unknown_close_keeps_operation_and_retiring_owner(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    await db.commit()
    client = objects(monkeypatch, row)
    client.close_failure = OSError("unknown client close")
    assert (await retirement.cleanup(db))["failed"] == 1
    await db.refresh(row)
    assert row.state == "retiring" and row.inventory
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    client.close_failure = None
    assert await retirement.cleanup(db) == {"deleted": 0, "retained": 0, "failed": 0}


async def test_database_published_delivery_survives_job_and_cache_absence(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.commit()
    output = await model_tests._zip(db, row, created_at=row.created_at)
    row.state, row.published_at, output.state = (
        "ready",
        datetime.now(timezone.utc),
        "ready",
    )
    await db.commit()
    client = objects(monkeypatch, row)
    assert await retirement.cleanup(db) == {"deleted": 0, "retained": 0, "failed": 0}
    monkeypatch.setattr(
        retirement, "now", lambda: row.expires_at + timedelta(seconds=1)
    )
    assert not await retirement._cleanup_one(db, row.id)
    assert not client.calls and (await db.get(Bundle, row.id)).state == "ready"


async def test_database_pending_zip_and_media_retire_independently_legacy_pending_stays(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    endpoint = physical._runtime_endpoint()
    zip_bucket = "saved-export-zip"
    zip_ref = {
        **{key: value for key, value in row.storage_ref.items() if key != "key_prefix"},
        "bucket": zip_bucket,
        "object_key": export_storage.key_for(
            "image", row.project_id, row.job_id, row.zip_output_id
        ),
        "endpoint_url": endpoint,
        "storage_identity": export_storage.managed_storage_identity(
            endpoint, zip_bucket
        ),
    }
    output = await model_tests._zip(
        db, row, created_at=row.created_at, storage_ref=zip_ref
    )
    legacy_created = datetime.now(timezone.utc) - timedelta(days=8)
    legacy = await model_tests._zip(
        db, created_at=legacy_created, expires_at=legacy_created + timedelta(days=7)
    )
    await db.commit()
    deleted = []
    monkeypatch.setattr(
        export_storage, "delete", lambda receipt, **kwargs: deleted.append(receipt.id)
    )
    client = objects(monkeypatch, row)
    assert (await export_outputs.cleanup(db))["outputs_deleted"] == 1
    assert deleted == [output.id]
    assert await db.get(ExportOutput, output.id) is None
    assert (await db.get(Bundle, row.id)).state == "building"
    assert await db.get(ExportOutput, legacy.id) is not None
    assert not client.calls
    assert (await retirement.cleanup(db))["deleted"] == 1
    assert (await db.get(Bundle, row.id, populate_existing=True)).state == "deleted"


async def test_database_zip_cleanup_observes_bundle_reads_at_any_location_before_limit(
    gc_db, retirement_queue, monkeypatch
):
    db = gc_db
    protected, first, later = retirement_queue
    for row in (*protected, first, later):
        ref = {
            key: value for key, value in row.storage_ref.items() if key != "key_prefix"
        }
        ref["object_key"] = export_storage.key_for(
            "image", row.project_id, row.job_id, row.zip_output_id
        )
        await model_tests._zip(db, row, created_at=row.created_at, storage_ref=ref)
    await db.commit()
    deleted = []
    monkeypatch.setattr(
        export_storage, "delete", lambda receipt, **kwargs: deleted.append(receipt.id)
    )
    result = await export_outputs.cleanup(db, limit=1)
    assert result["outputs_deleted"] == 1 and deleted == [first.zip_output_id]
    remaining = dict(
        (await db.execute(select(ExportOutput.id, ExportOutput.state))).all()
    )
    assert remaining == {row.zip_output_id: "pending" for row in (*protected, later)}
    for row in protected:
        assert not await export_outputs._cleanup_one(
            db, row.zip_output_id, {"removed": 0}
        )
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 3
