"""Task scope exercises the existing conditional writer and independent ledger."""

import asyncio
import hashlib
from types import SimpleNamespace
import threading
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import DatasetItem
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation
from app.services import managed_original_creation as service
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service
from tests.test_managed_original_creation import Client, stage_client
from tests.test_migration_0230_managed_original_task_scope import (
    allocation,
    legacy_creation_db as _legacy_creation_db,
    task_creation_db as _task_creation_db,
)

legacy_creation_db = _legacy_creation_db


@pytest.fixture
async def task_creation_db(legacy_creation_db):
    db = await _task_creation_db.__wrapped__(legacy_creation_db)
    async with service.storage_access.engine(db).begin() as connection:
        await connection.run_sync(StorageUsageMutation.__table__.create)
    return db


@pytest.fixture
def client(monkeypatch):
    value = Client()
    monkeypatch.setattr(service, "create_client", lambda **_: value)
    stage_client(monkeypatch, value)
    return value


async def operations(db):
    async with service.storage_access.engine(db).connect() as connection:
        return list(await connection.scalars(select(StorageOperation.id)))


def stage(client, row, data=b"original"):
    client.store(
        row["staging_bucket"],
        row["staging_key"],
        data,
        content_type=row["content_type"],
    )


def final_writes(client, row):
    return [
        params
        for operation, params in client.calls
        if operation in {"complete", "put"}
        and params["Bucket"] == row["bucket"]
        and params["Key"] == row["object_key"]
    ]


async def test_allocation_is_independent_of_business_rows_and_rollback(
    task_creation_db,
):
    db = task_creation_db
    pending = DatasetItem(
        dataset_id=uuid.uuid4(),
        file_name="pending",
        file_path="pending",
        file_type="image",
    )
    db.add(pending)
    await db.flush()
    item_id = pending.id
    project_id, task_id, requested_by = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = await allocation(
        db, project_id=project_id, task_id=task_id, requested_by=requested_by
    )
    await db.rollback()
    assert await db.get(DatasetItem, item_id) is None
    saved = await service._read(db, row["id"])
    assert saved["owner_kind"] == "task" and saved["dataset_id"] is None
    assert (saved["project_id"], saved["task_id"], saved["requested_by"]) == (
        project_id,
        task_id,
        requested_by,
    )
    assert saved["status"] == "allocated"
    assert saved["bucket"] == storage_service.bucket
    assert saved["staging_bucket"] == storage_service.import_bucket
    assert (
        saved["object_key"] == f"__aap_task_originals__/{task_id}/{row['id']}/payload"
    )
    assert saved["staging_key"] == f"{service.STAGING_PREFIX}/{row['id']}/payload"
    second = await allocation(db, project_id=project_id, task_id=task_id)
    assert second["object_key"] != row["object_key"]
    assert second["staging_key"] != row["staging_key"]


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("image/jpeg", b"opaque image bytes"),
        ("video/mp4", b"opaque video bytes"),
        ("application/vnd.pointcloud", b"opaque point cloud bytes"),
        ("application/pdf", b"%PDF-general file"),
        ("application/octet-stream", b"\x00\xffbinary"),
        ("application/octet-stream", b""),
    ],
)
async def test_generic_bytes_are_sealed_verified_and_staging_replay_cannot_change_them(
    task_creation_db, client, content_type, data
):
    db = task_creation_db
    row = await allocation(db, content_type=content_type, max_bytes=len(data))
    client.versioned = True
    stage(client, row, data)
    result = await service.complete_direct(db, row)
    saved = await service._read(db, row["id"])
    assert saved["status"] == "verified"
    assert result.creation_id == row["id"]
    assert result.sha256 == saved["sha256"] == hashlib.sha256(data).hexdigest()
    assert result.size_bytes == saved["size_bytes"] == len(data)
    assert result.etag == saved["receipt"]["etag"]
    assert result.version_id == saved["receipt"]["version_id"]
    assert saved["verification"]["sha256"] == result.sha256
    assert saved["source"]["sha256"] == result.sha256
    assert client.objects[row["bucket"], row["object_key"]]["data"] == data
    assert all(body.closed for body in client.bodies)
    assert not await operations(db)
    calls = len(client.calls)
    stage(client, row, b"replayed different bytes")
    repeated = await service.complete_direct(db, row)
    assert repeated.creation_id == result.creation_id
    assert repeated.sha256 == result.sha256
    assert len(client.calls) == calls and len(final_writes(client, row)) == 1
    assert client.objects[row["bucket"], row["object_key"]]["data"] == data
    assert not [operation for operation, _ in client.calls if operation == "delete"]


@pytest.mark.parametrize("existing", [b"original", b"foreign bytes"])
async def test_collision_preserves_foreign_bytes_and_task_responsibility(
    task_creation_db, client, existing
):
    db = task_creation_db
    row = await allocation(db)
    stage(client, row)
    client.store(row["bucket"], row["object_key"], existing)
    with pytest.raises(ContentError, match="creation_write_conflict"):
        await service.complete_direct(db, row)
    saved = await service._read(db, row["id"])
    assert saved["status"] == "conflict" and saved["receipt"] is None
    assert saved["task_id"] == row["task_id"] and saved["dataset_id"] is None
    assert client.objects[row["bucket"], row["object_key"]]["data"] == existing
    calls = len(client.calls)
    with pytest.raises(ContentError, match="managed_original_creation_terminal"):
        await service.complete_direct(db, row)
    assert len(client.calls) == calls and not await operations(db)
    assert not [operation for operation, _ in client.calls if operation == "delete"]


async def test_task_final_readback_rejects_same_size_corruption_without_deleting(
    task_creation_db, client
):
    db = task_creation_db
    row = await allocation(db)
    stage(client, row)
    complete = client.complete_multipart_upload

    def corrupt(**params):
        response = complete(**params)
        client.store(
            params["Bucket"], params["Key"], b"corrupt!", etag=response["ETag"]
        )
        return response

    client.complete_multipart_upload = corrupt
    with pytest.raises(ContentError, match="creation_content_mismatch"):
        await service.complete_direct(db, row)
    saved = await service._read(db, row["id"])
    assert saved["status"] == "failed" and saved["receipt"]
    assert saved["verification"] is None
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"corrupt!"
    assert not await operations(db)
    assert not [operation for operation, _ in client.calls if operation == "delete"]


async def test_task_staging_budget_is_enforced_before_final_creation(
    task_creation_db, client
):
    db = task_creation_db
    row = await allocation(db, max_bytes=0)
    stage(client, row, b"over budget")
    with pytest.raises(ContentError) as caught:
        await service.complete_direct(db, row)
    assert caught.value.status == 413
    assert caught.value.code == "creation_byte_budget_exceeded"
    assert (await service._read(db, row["id"]))["status"] == "failed"
    assert not final_writes(client, row)
    assert (row["bucket"], row["object_key"]) not in client.objects
    assert not await operations(db)


async def test_task_lookup_cannot_cross_scope_or_task_and_dataset_protocol_stays_intact(
    task_creation_db, client
):
    db = task_creation_db
    row = await allocation(db)
    assert (
        await service.for_task(db, task_id=row["task_id"], object_key=row["object_key"])
    ) == row
    assert (
        await service.for_task(db, task_id=uuid.uuid4(), object_key=row["object_key"])
        is None
    )
    assert (
        await service.for_task(db, task_id=row["task_id"], object_key="old/final")
        is None
    )
    assert (
        await service.for_key(db, dataset_id=None, object_key=row["object_key"]) is None
    )
    dataset = await service.allocate(
        db,
        dataset_id=row["task_id"],
        logical_path="a.bin",
        origin="direct",
        staging=True,
        max_bytes=10,
    )
    assert dataset["owner_kind"] == "dataset"
    assert dataset["project_id"] is dataset["task_id"] is None
    assert dataset["bucket"] == storage_service.datasets_bucket
    assert (
        await service.for_key(
            db, dataset_id=dataset["dataset_id"], object_key=dataset["object_key"]
        )
    ) == dataset
    assert (
        await service.for_task(
            db, task_id=dataset["dataset_id"], object_key=dataset["object_key"]
        )
        is None
    )
    stage(client, dataset)
    assert (await service.complete_direct(db, dataset)).size_bytes == len(b"original")
    stage(client, row)
    await service.complete_direct(db, row)
    with pytest.raises(ContentError, match="managed_original_creation_unverified"):
        await service.attach(db, SimpleNamespace(), row["id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_kind", "dataset"),
        ("project_id", uuid.uuid4()),
        ("task_id", uuid.uuid4()),
        ("dataset_id", uuid.uuid4()),
    ],
)
async def test_untrusted_scope_snapshot_is_rejected_before_io(
    task_creation_db, client, field, value
):
    row = await allocation(task_creation_db)
    stage(client, row)
    with pytest.raises(ContentError, match="managed_original_creation_not_found"):
        await service.complete_direct(task_creation_db, row | {field: value})
    without = dict(row)
    without.pop(field)
    with pytest.raises(ContentError, match="managed_original_creation_not_found"):
        await service.complete_direct(task_creation_db, without)
    assert not client.calls
    assert (await service._read(task_creation_db, row["id"]))["status"] == "allocated"


async def test_task_lookup_preserves_saved_bucket_after_current_bucket_change(
    task_creation_db, client, monkeypatch
):
    row = await allocation(task_creation_db)
    stage(client, row)
    monkeypatch.setattr(storage_service, "bucket", "another-current-bucket")
    assert (
        await service.for_task(
            task_creation_db, task_id=row["task_id"], object_key=row["object_key"]
        )
        == row
    )
    assert not client.calls
    assert (await service._read(task_creation_db, row["id"]))["bucket"] == row["bucket"]


async def test_verified_task_evidence_survives_business_rollback_and_scope_stays_fixed(
    task_creation_db, client
):
    db = task_creation_db
    row = await allocation(db)
    stage(client, row)
    await service.complete_direct(db, row)
    await db.rollback()
    saved = await service._read(db, row["id"])
    assert saved["status"] == "verified" and saved["receipt"] and saved["verification"]
    for field in ("owner_kind", "project_id", "task_id"):
        with pytest.raises(IntegrityError, match="immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(Creation)
                    .where(Creation.id == row["id"])
                    .values({field: None})
                )
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"
    assert not [operation for operation, _ in client.calls if operation == "delete"]


@pytest.mark.parametrize("failure", ["lost_ack", "close"])
async def test_unknown_task_write_keeps_exact_permit_and_never_retries(
    task_creation_db, client, failure
):
    db = task_creation_db
    row = await allocation(db)
    stage(client, row)
    if failure == "lost_ack":
        original = client.complete_multipart_upload

        def lost(**params):
            original(**params)
            raise TimeoutError("private acknowledgement detail")

        client.complete_multipart_upload = lost
    else:
        client.close_failure = True
    with pytest.raises(ContentError) as caught:
        await service.complete_direct(db, row)
    assert caught.value.storage_operation_unsettled and caught.value.status == 503
    assert "private" not in str(caught.value)
    saved = await service._read(db, row["id"])
    assert saved["status"] == "unknown"
    assert saved["operation_id"] in await operations(db)
    async with service.storage_access.engine(db).connect() as connection:
        assert (
            await connection.scalar(
                select(StorageOperation.purpose).where(
                    StorageOperation.id == saved["operation_id"]
                )
            )
            == f"task_original_write_{row['id'].hex}"
        )
    assert saved["task_id"] == row["task_id"]
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"
    calls = len(client.calls)
    with pytest.raises(ContentError, match="managed_original_creation_terminal"):
        await service.complete_direct(db, row)
    assert len(client.calls) == calls


@pytest.mark.parametrize("versioned", [False, True])
async def test_cancelled_task_completion_drains_saved_source_stream_and_preserves_ledger(
    task_creation_db, client, versioned
):
    db = task_creation_db
    row = await allocation(db)
    client.versioned = versioned
    stage(client, row)
    client.versioned = False
    entered, release = threading.Event(), threading.Event()

    def hold(operation, params):
        if operation == "part":
            entered.set()
            assert release.wait(5)

    client.hook = hold
    task = asyncio.create_task(service.complete_direct(db, row))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        saved = await service._read(db, row["id"])
        assert saved["status"] == "preparing"
        assert saved["operation_id"] in await operations(db)
        with pytest.raises(ContentError, match="managed_original_creation_active"):
            await service.complete_direct(db, row)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        try:
            await service.storage_access.finish(task)
        except asyncio.CancelledError:
            pass
    assert task.cancelled()
    assert (await service._read(db, row["id"]))["status"] == "verified"
    assert len(final_writes(client, row)) == 1 and not await operations(db)
