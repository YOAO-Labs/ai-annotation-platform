"""Physical output verification and real training execution ownership fences."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import training_dataset_originals as originals
from app.services import training_dataset_storage as owners
from app.services import training_datasets as datasets
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.reference_keys import reference_key
from app.services import storage_access
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.workers import training_dataset as worker
from tests.factory import create_project
from tests.training_original_fixtures import original_receipt


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ObservedBody(io.BytesIO):
    def __init__(self, raw, *, fail_close=False):
        super().__init__(raw)
        self.read_sizes, self.returned = [], 0
        self.close_calls, self.fail_close = 0, fail_close

    def read(self, size=-1):
        self.read_sizes.append(size)
        chunk = super().read(min(size, 7) if size >= 0 else 7)
        self.returned += len(chunk)
        return chunk

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise OSError("Unconfirmed storage body close")
        super().close()


def output_provider(monkeypatch, raw, *, declared_size=None, fail_close=False):
    body = ObservedBody(raw, fail_close=fail_close)
    calls = []
    head = {
        "ContentLength": len(raw) if declared_size is None else declared_size,
        "ETag": '"saved-object-etag"',
        "VersionId": "saved-version",
        "Metadata": {"snapshot-digest": "snapshot", "sha256": sha(raw)},
    }

    def head_object(**kwargs):
        calls.append(("head", kwargs))
        return head

    def get_object(**kwargs):
        calls.append(("get", kwargs))
        return {**head, "Body": body}

    monkeypatch.setattr(worker.storage_service.client, "head_object", head_object)
    monkeypatch.setattr(worker.storage_service.client, "get_object", get_object)
    return body, calls, head


def test_verify_output_reads_entire_body_with_saved_etag_and_version(monkeypatch):
    raw = b"all saved package bytes, including the final member" * 5
    body, calls, _head = output_provider(monkeypatch, raw)
    worker.verify_output(
        "saved-bucket",
        "owned/package.zip",
        sha(raw),
        len(raw),
        snapshot_digest="snapshot",
    )
    assert calls == [
        ("head", {"Bucket": "saved-bucket", "Key": "owned/package.zip"}),
        (
            "get",
            {
                "Bucket": "saved-bucket",
                "Key": "owned/package.zip",
                "IfMatch": '"saved-object-etag"',
                "VersionId": "saved-version",
            },
        ),
    ]
    assert body.returned == len(raw) and len(body.read_sizes) > 2
    assert all(0 < size <= len(raw) + 1 for size in body.read_sizes)
    assert body.closed and body.close_calls == 1


@pytest.mark.parametrize(
    "mismatch", ["head-size", "snapshot", "truncated", "oversized", "sha"]
)
def test_verify_output_rejects_actual_size_or_sha_even_with_plausible_metadata(
    monkeypatch, mismatch
):
    expected = b"frozen output bytes"
    raw = {
        "truncated": expected[:-1],
        "oversized": expected + b"!",
        "sha": b"X" + expected[1:],
    }.get(mismatch, expected)
    body, calls, head = output_provider(monkeypatch, raw, declared_size=len(expected))
    head["Metadata"]["sha256"] = sha(expected)
    if mismatch == "head-size":
        head["ContentLength"] += 1
    if mismatch == "snapshot":
        head["Metadata"]["snapshot-digest"] = "rewritten-owner"
    try:
        with pytest.raises(TrainingDatasetError) as error:
            worker.verify_output(
                "saved-bucket",
                "owned/package.zip",
                sha(expected),
                len(expected),
                snapshot_digest="snapshot",
            )
        assert error.value.code == "training_output_mismatch"
        if mismatch in {"head-size", "snapshot"}:
            assert [operation for operation, _ in calls] == ["head"]
            assert body.close_calls == 0
        else:
            assert [operation for operation, _ in calls] == ["head", "get"]
            assert body.closed and body.close_calls == 1
    finally:
        if not body.closed:
            body.close()


def test_verify_output_close_failure_remains_unsettled(monkeypatch):
    raw = b"fully read but physical closure unknown"
    body, calls, _head = output_provider(monkeypatch, raw, fail_close=True)
    try:
        with pytest.raises(OSError, match="Unconfirmed") as error:
            worker.verify_output(
                "saved-bucket", "owned/package.zip", sha(raw), len(raw)
            )
        assert error.value.storage_operation_unsettled is True
        assert body.returned == len(raw) and body.close_calls == 1
        assert not body.closed
        assert [operation for operation, _ in calls] == ["head", "get"]
    finally:
        body.fail_close = False
        body.close()


def bitmap(color):
    data = io.BytesIO()
    with Image.new("RGB", (8, 6), color) as image:
        image.save(data, format="BMP")
    return data.getvalue()


def test_retained_metadata_rewrite_cannot_replace_frozen_original(
    monkeypatch, tmp_path
):
    original, rewritten = bitmap("red"), bitmap("blue")
    assert original != rewritten and len(original) == len(rewritten)
    body, calls, _head = output_provider(monkeypatch, rewritten)
    sample = {
        "task_id": str(uuid.uuid4()),
        "source_key": "private-original/image.bmp",
        "original": original_receipt(original),
        "media": {"sha256": sha(original), "size_bytes": len(original)},
    }

    def unexpected_put(*args, **kwargs):
        pytest.fail("Rewritten retained content must not be published")

    monkeypatch.setattr(worker.storage_service.client, "upload_file", unexpected_put)
    with pytest.raises(TrainingDatasetError) as error:
        worker.materialize_image(
            sample, "owned-version", "snapshot", tmp_path, bucket="saved-bucket"
        )
    assert error.value.code == "source_media_changed"
    assert body.closed and body.returned == len(rewritten)
    assert all(params["Bucket"] == "saved-bucket" for _operation, params in calls)
    assert all(
        params["Key"] == f"owned-version/media/{sample['task_id']}"
        for _operation, params in calls
    )


@pytest.mark.parametrize("recorded", [False, True])
def test_missing_retained_media_never_reads_locator_only_original(
    monkeypatch, tmp_path, recorded
):
    calls = []
    sample = {
        "task_id": str(uuid.uuid4()),
        "source_bucket": "legacy-guessed-bucket",
        "source_key": "private-original/image.png",
        "media": {"etag": "old-frozen-header", "size_bytes": 10},
    }
    if recorded:
        sample["original"] = original_receipt(bitmap("red"))

    def missing(**kwargs):
        calls.append(kwargs)
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

    def forbidden(*args, **kwargs):
        pytest.fail("Missing retained media cannot fall back to locator-only raw I/O")

    monkeypatch.setattr(worker.storage_service.client, "head_object", missing)
    monkeypatch.setattr(worker.storage_service.client, "get_object", forbidden)
    monkeypatch.setattr(worker.storage_service.client, "upload_file", forbidden)
    with pytest.raises(TrainingDatasetError) as error:
        worker.materialize_image(
            sample, "owned-version", "snapshot", tmp_path, bucket="saved-bucket"
        )
    assert error.value.code == (
        "original_read_required" if recorded else "original_input_unrecorded"
    )
    assert calls == [
        {"Bucket": "saved-bucket", "Key": f"owned-version/media/{sample['task_id']}"}
    ]
    assert not list(tmp_path.iterdir())


def saved_owner(project_id, version_id, *, bucket="saved-training-boundary-bucket"):
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    return {
        "schema": 1,
        "purpose": "training-dataset",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": owners.prefix_for(project_id, version_id),
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }


async def test_reference_close_cancellation_preserves_prior_unknown_output_owner(
    monkeypatch, tmp_path
):
    from app.services import training_dataset_sources

    project_id, version_id = uuid.uuid4(), uuid.uuid4()
    version = SimpleNamespace(
        id=version_id,
        project_id=project_id,
        creator_id=uuid.uuid4(),
        storage_ref=saved_owner(project_id, version_id),
        snapshot={},
    )
    sample = {
        "task_id": str(uuid.uuid4()),
        "source_key": reference_key(uuid.uuid4(), "a" * 64),
    }
    closing, release, finished = threading.Event(), threading.Event(), threading.Event()
    source = io.BytesIO(b"verified reference")

    def close():
        source.close()
        closing.set()
        if not release.wait(10):
            raise TimeoutError("test did not release reference close")
        finished.set()

    calls = []

    def materialize(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TrainingDatasetError(
                "reference_read_required", "retained media absent"
            )
        raise storage_access.mark_unsettled(OSError("output close unknown"))

    monkeypatch.setattr(worker, "materialize_image", materialize)
    monkeypatch.setattr(worker, "assert_dataset_authority", AsyncMock())
    monkeypatch.setattr(
        training_dataset_sources,
        "training_reference_snapshot",
        AsyncMock(return_value=(SimpleNamespace(file=source, close=close), {}, {})),
    )
    db = SimpleNamespace(get=AsyncMock(return_value=object()))
    pending = asyncio.create_task(
        worker.materialize_sample(
            db,
            version,
            sample,
            version.storage_ref["prefix"].rstrip("/"),
            "snapshot",
            tmp_path,
            "worker:original-test",
        )
    )
    try:
        assert await asyncio.to_thread(closing.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await pending
        assert failure.value.storage_operation_unsettled is True
        assert finished.is_set() and source.closed and len(calls) == 2
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.fixture
async def execution_world(db_session, super_admin):
    db, (actor, token) = db_session, super_admin
    project = await create_project(db, owner_id=actor.id)
    version_id = uuid.uuid4()
    snapshot = {"samples": [], "classes": [], "source": {"media_type": "image"}}
    job = AsyncJob(
        kind="training_dataset",
        project_id=project.id,
        user_id=actor.id,
        status="running",
        celery_task_id="owned-training-task",
        payload={"training_dataset_id": str(version_id)},
    )
    db.add(job)
    await db.flush()
    version = TrainingDatasetVersion(
        id=version_id,
        project_id=project.id,
        creator_id=actor.id,
        async_job_id=job.id,
        request_id=uuid.uuid4(),
        request_digest=content_digest(snapshot),
        name="Execution boundaries",
        status="running",
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
        storage_ref=saved_owner(project.id, version_id),
    )
    db.add(version)
    await db.flush()
    context = worker.Execution(
        version.id,
        job.id,
        version.attempt_id,
        version.snapshot_digest,
        deepcopy(version.storage_ref),
        originals.Authority(project.id, actor.id, None, None),
        job.celery_task_id,
    )
    return SimpleNamespace(
        db=db,
        actor=actor,
        token=token,
        project=project,
        version=version,
        job=job,
        context=context,
    )


async def test_guard_execution_locks_real_version_before_real_job(
    execution_world, test_engine
):
    world, statements = execution_world, []

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        if "FOR UPDATE" in statement.upper():
            statements.append(statement.lower())

    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    try:
        assert (
            await worker.guard_execution(world.db, world.context, lock=True)
            is world.version
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
    version_index = next(
        i
        for i, statement in enumerate(statements)
        if "training_dataset_versions" in statement
    )
    job_index = next(
        i for i, statement in enumerate(statements) if "async_jobs" in statement
    )
    assert version_index < job_index


@pytest.mark.parametrize(
    "change",
    [
        "snapshot",
        "snapshot-digest",
        "owner",
        "attempt",
        "job-binding",
        "job-owner",
        "job-kind",
        "celery-task",
    ],
)
async def test_guard_execution_rechecks_saved_database_bindings(
    execution_world, change
):
    world = execution_world
    if change == "owner":
        # Persisted ownership is immutable; an execution captured for another
        # owner must still fail against the actual saved database row.
        context = replace(
            world.context,
            owner_ref=saved_owner(
                world.project.id, world.version.id, bucket="other-owner-bucket"
            ),
        )
        with pytest.raises(ContentError) as failure:
            await worker.guard_execution(world.db, context, lock=True)
        assert failure.value.code == "training_input_binding_changed"
        return
    if change == "snapshot":
        model, identifier, values = (
            TrainingDatasetVersion,
            world.version.id,
            {"snapshot": {**world.version.snapshot, "seed": 99}},
        )
    elif change == "snapshot-digest":
        model, identifier, values = (
            TrainingDatasetVersion,
            world.version.id,
            {"snapshot_digest": "f" * 64},
        )
    elif change == "attempt":
        model, identifier, values = (
            TrainingDatasetVersion,
            world.version.id,
            {"attempt_id": uuid.uuid4()},
        )
    elif change == "job-binding":
        model, identifier, values = (
            AsyncJob,
            world.job.id,
            {"payload": {"training_dataset_id": str(uuid.uuid4())}},
        )
    elif change == "job-owner":
        model, identifier, values = AsyncJob, world.job.id, {"user_id": None}
    elif change == "job-kind":
        model, identifier, values = AsyncJob, world.job.id, {"kind": "export"}
    else:
        model, identifier, values = (
            AsyncJob,
            world.job.id,
            {"celery_task_id": "replacement-training-task"},
        )
    cached = world.version if model is TrainingDatasetVersion else world.job
    before = {field: deepcopy(getattr(cached, field)) for field in values}
    await world.db.execute(
        update(model)
        .where(model.id == identifier)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    assert all(getattr(cached, field) == value for field, value in before.items())
    if change in {"attempt", "celery-task"}:
        with pytest.raises(worker.Superseded):
            await worker.guard_execution(world.db, world.context, lock=True)
    else:
        with pytest.raises(ContentError) as error:
            await worker.guard_execution(world.db, world.context, lock=True)
        assert error.value.code == "training_input_binding_changed"


@pytest.mark.parametrize("cancel", ["version", "job", "requested"])
async def test_guard_execution_observes_current_job_or_version_cancellation(
    execution_world, cancel
):
    world = execution_world
    model = TrainingDatasetVersion if cancel == "version" else AsyncJob
    identifier = world.version.id if cancel == "version" else world.job.id
    values = (
        {"payload": {**world.job.payload, "cancel_requested": True}}
        if cancel == "requested"
        else {"status": "cancelled"}
    )
    await world.db.execute(
        update(model)
        .where(model.id == identifier)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    assert world.version.status == "running" and world.job.status == "running"
    with pytest.raises(asyncio.CancelledError):
        await worker.guard_execution(world.db, world.context, lock=True)


@pytest.fixture
async def unresolved_output(execution_world, test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    location_id, operation_id, unrelated_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessions() as db:
        db.add(
            StorageMaintenanceGate(location_id=location_id, state="active", revision=1)
        )
        await db.flush()
        for identifier, version_id in [
            (operation_id, execution_world.version.id),
            (unrelated_id, uuid.uuid4()),
        ]:
            db.add(
                StorageOperation(
                    id=identifier,
                    location_id=location_id,
                    revision=1,
                    purpose=originals.output_purpose(version_id),
                    node_id="worker:training-boundaries@test",
                )
            )
        await db.commit()

    async def release_owned():
        async with sessions() as db:
            await db.execute(
                delete(StorageOperation).where(StorageOperation.id == operation_id)
            )
            await db.commit()

    try:
        yield SimpleNamespace(id=operation_id, release=release_owned)
    finally:
        async with sessions() as db:
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.id.in_([operation_id, unrelated_id])
                )
            )
            await db.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location_id
                )
            )
            await db.commit()


@pytest.mark.parametrize("operation", ["retry", "delete"])
async def test_durable_output_operation_blocks_retry_and_delete(
    execution_world, unresolved_output, httpx_client, monkeypatch, operation
):
    world = execution_world
    world.version.status = world.job.status = "failed"
    await world.db.flush()

    def forbidden_io(*args, **kwargs):
        pytest.fail(
            "An unresolved output operation must stop retry/delete before dispatch or storage I/O"
        )

    monkeypatch.setattr(datasets, "dispatch_dataset", forbidden_io)
    monkeypatch.setattr(worker.storage_service, "delete_prefix", forbidden_io)
    with pytest.raises(TrainingDatasetError) as error:
        await datasets.lock_dataset_execution(world.db, world.version.id)
    assert error.value.code == "execution_stopping" and error.value.status_code == 409
    url = f"/api/v1/projects/{world.project.id}/training-datasets/{world.version.id}"
    response = await (
        httpx_client.post(
            url + "/retry", headers={"Authorization": f"Bearer {world.token}"}
        )
        if operation == "retry"
        else httpx_client.delete(
            url, headers={"Authorization": f"Bearer {world.token}"}
        )
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "execution_stopping"
    assert (
        await world.db.get(
            TrainingDatasetVersion, world.version.id, populate_existing=True
        )
    ).status == "failed"
    assert await world.db.get(StorageOperation, unresolved_output.id) is not None
    await unresolved_output.release()
    # Another version's durable operation remains and must not block this owner.
    await datasets.lock_dataset_execution(world.db, world.version.id)
