from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
from io import BytesIO
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
import uuid

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.raster_mask_object import RasterMaskObject
from app.services import raster_object_storage, storage_access
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    build_rle_reference,
    build_rle_gzip_reference,
    canonical_rle_bytes,
    load_coco_rle,
    lock_raster_mask_references,
    store_coco_rle,
    store_coco_rle_gzip,
    validate_mask_geometry_for_task,
)


def memory_mask_storage(*, get_error: Exception | None = None):
    """Boto-shaped bytes, identities and conditional operations for RLE tests."""
    storage = MagicMock()
    storage.bucket = settings.minio_bucket
    storage.client.meta.endpoint_url = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    storage.objects = {}
    storage.open_body = BytesIO

    def head(*, Bucket, Key, **kwargs):
        assert Bucket == storage.bucket
        if Key not in storage.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        raw = storage.objects[Key]
        return {
            "ContentLength": len(raw),
            "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
        }

    def put(*, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        assert Bucket == storage.bucket
        if IfNoneMatch == "*" and Key in storage.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        storage.objects[Key] = bytes(Body)
        return head(Bucket=Bucket, Key=Key)

    def get(*, Bucket, Key, IfMatch=None, **kwargs):
        metadata = head(Bucket=Bucket, Key=Key)
        if IfMatch is not None and IfMatch != metadata["ETag"]:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        return {**metadata, "Body": storage.open_body(storage.objects[Key])}

    storage.client.head_object.side_effect = head
    storage.client.put_object.side_effect = put
    storage.client.get_object.side_effect = get_error or get
    storage.verify_upload.side_effect = lambda key: (
        head(Bucket=storage.bucket, Key=key) if key in storage.objects else None
    )
    return storage


async def mask_owner_for(db, reference, *, state="ready"):
    """Read committed independent ownership through the actual caller engine."""
    async with async_sessionmaker(
        storage_access.engine(db), expire_on_commit=False
    )() as session:
        row = await session.scalar(
            select(RasterMaskObject).where(
                RasterMaskObject.logical_key == reference["object_key"],
                RasterMaskObject.state == state,
            )
        )
        assert row is not None
        return row


async def ready_mask_owner(db, storage, rle, *, compressed=False, encoded_data=None):
    """Preload known historical bytes with a real immutable ready receipt."""
    reference = (build_rle_gzip_reference if compressed else build_rle_reference)(rle)
    data = (
        encoded_data
        if encoded_data is not None
        else gzip.compress(canonical_rle_bytes(rle), mtime=1)
        if compressed
        else canonical_rle_bytes(rle)
    )
    async with async_sessionmaker(
        storage_access.engine(db), expire_on_commit=False
    )() as session:
        row = RasterMaskObject(
            id=uuid.uuid4(),
            **raster_object_storage.content(reference, data),
            state="pending",
            created_at=datetime.now(timezone.utc),
        )
        row.storage_ref = await raster_object_storage.allocate(session, row)
        session.add(row)
        await session.flush()
        key = row.storage_ref["object_key"]
        storage.objects[key] = data
        row.state = "ready"
        row.published_at = row.last_used_at = datetime.now(timezone.utc)
        await session.commit()
    return reference, key


async def test_store_coco_rle_uses_content_addressed_key_and_put_if_absent(db_session):
    storage = memory_mask_storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=storage,
        db=db_session,
    )
    assert reference["encoding"] == "coco_rle_ref"
    assert reference["object_key"].endswith(f"{reference['sha256']}.json")
    assert reference["runs"] == 4
    storage.client.put_object.assert_called_once()

    owner = await mask_owner_for(db_session, reference)
    physical_key = owner.storage_ref["object_key"]
    assert physical_key == f"raster-masks/objects/{owner.id}.json"
    assert physical_key != reference["object_key"]
    assert set(storage.objects) == {physical_key}
    storage.client.put_object.reset_mock()
    await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=storage,
        db=db_session,
    )
    storage.client.put_object.assert_not_called()


async def test_unknown_legacy_logical_bytes_do_not_authorize_a_read(db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [3, 7], "counts": [2, 19]}
    reference = build_rle_reference(rle)
    original = canonical_rle_bytes(rle)
    storage.objects[reference["object_key"]] = original
    with pytest.raises(raster_object_storage.RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_unpinned"
    assert rejected.value.status_code == 409
    storage.client.head_object.assert_not_called()
    storage.client.get_object.assert_not_called()
    assert storage.objects == {reference["object_key"]: original}


async def test_load_coco_rle_verifies_reference_metadata(db_session):
    storage = memory_mask_storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]},
        storage=storage,
        db=db_session,
    )
    body = storage.client.put_object.call_args.kwargs["Body"]
    stream = BytesIO(body)
    stream.close = MagicMock()
    storage.open_body = lambda _raw: stream
    assert (await load_coco_rle(reference, storage=storage, db=db_session))[
        "counts"
    ] == [
        1,
        2,
        2,
        1,
    ]
    stream.close.assert_called_once()


async def test_load_coco_rle_rejects_digest_mismatch(db_session):
    storage = memory_mask_storage()
    reference = await store_coco_rle(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [6]},
        storage=storage,
        db=db_session,
    )
    stream = BytesIO(b'{"encoding":"coco_rle","size":[2,3],"counts":[0,6]}')
    stream.close = MagicMock()
    owner = await mask_owner_for(db_session, reference)
    storage.objects[owner.storage_ref["object_key"]] = stream.getvalue()
    storage.open_body = lambda _raw: stream
    with pytest.raises(raster_object_storage.RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_integrity_mismatch"


@pytest.mark.parametrize("compressed", [False, True])
async def test_store_rejects_corrupt_existing_object_without_overwrite(
    compressed, db_session
):
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
    storage = memory_mask_storage()
    _, key = await ready_mask_owner(db_session, storage, rle, compressed=compressed)
    storage.objects[key] = b"corrupt"
    writer = store_coco_rle_gzip if compressed else store_coco_rle
    with pytest.raises(RasterMaskContractError) as rejected:
        await writer(rle, storage=storage, db=db_session)
    assert rejected.value.detail["reason"] == "raster_object_storage_integrity_mismatch"
    assert storage.objects[key] == b"corrupt"
    storage.client.put_object.assert_not_called()
    storage.client.delete_object.assert_not_called()


async def test_store_requires_conditional_creation_and_full_readback(db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
    await store_coco_rle(rle, storage=storage, db=db_session)
    assert storage.client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    storage.client.get_object.assert_called_once()


async def test_store_reuses_historical_gzip_with_different_mtime(db_session):
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
    storage = memory_mask_storage()
    original = gzip.compress(canonical_rle_bytes(rle), mtime=1)
    expected, key = await ready_mask_owner(
        db_session, storage, rle, compressed=True, encoded_data=original
    )
    reference = await store_coco_rle_gzip(rle, storage=storage, db=db_session)
    assert reference == expected
    assert reference["object_key"] != key
    assert storage.objects[key] == original
    storage.client.put_object.assert_not_called()
    storage.client.get_object.assert_called_once()


async def test_cancelled_store_drains_thread_before_releasing_caller_lock(db_session):
    storage = memory_mask_storage()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    put = storage.client.put_object.side_effect

    def blocked_put(**kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return put(**kwargs)
        finally:
            finished.set()

    storage.client.put_object.side_effect = blocked_put
    lock = asyncio.Lock()

    async def caller():
        async with lock:
            await store_coco_rle(
                {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]},
                storage=storage,
                db=db_session,
            )

    task = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert lock.locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)
    assert not lock.locked()


@pytest.mark.parametrize("correct_winner", [False, True])
async def test_conditional_creation_race_verifies_winner(correct_winner, db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}

    def race(**kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        storage.objects[kwargs["Key"]] = kwargs["Body"] if correct_winner else b"bad"
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

    storage.client.put_object.side_effect = race
    if correct_winner:
        await store_coco_rle(rle, storage=storage, db=db_session)
    else:
        with pytest.raises(RasterMaskContractError) as rejected:
            await store_coco_rle(rle, storage=storage, db=db_session)
        assert (
            rejected.value.detail["reason"]
            == "raster_object_storage_integrity_mismatch"
        )
    storage.client.put_object.assert_called_once()
    storage.client.delete_object.assert_not_called()


@pytest.mark.parametrize(
    "operation,code",
    [
        ("head_object", "AccessDenied"),
        ("put_object", "ConditionalRequestConflict"),
        ("put_object", "AccessDenied"),
    ],
)
async def test_store_does_not_treat_other_provider_errors_as_reusable(
    operation, code, db_session
):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    if operation == "head_object":
        await ready_mask_owner(db_session, storage, rle)
    failure = ClientError({"Error": {"Code": code}}, operation)
    getattr(storage.client, operation).side_effect = failure
    with pytest.raises(RasterMaskContractError) as raised:
        await store_coco_rle(
            rle,
            storage=storage,
            db=db_session,
        )
    expected = (
        "integrity_mismatch" if code == "ConditionalRequestConflict" else "unavailable"
    )
    assert raised.value.detail["reason"] == "raster_object_storage_" + expected
    storage.client.get_object.assert_not_called()
    if operation == "head_object":
        storage.client.put_object.assert_not_called()


async def test_store_rejects_corrupt_bytes_after_successful_put(db_session):
    storage = memory_mask_storage()
    put = storage.client.put_object.side_effect

    def corrupt(**kwargs):
        result = put(**kwargs)
        storage.objects[kwargs["Key"]] = b"X" * len(kwargs["Body"])
        return result

    storage.client.put_object.side_effect = corrupt
    reference = build_rle_reference(
        {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    )
    with pytest.raises(RasterMaskContractError) as rejected:
        await store_coco_rle(
            {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]},
            storage=storage,
            db=db_session,
        )
    assert rejected.value.detail["reason"] == "raster_object_storage_integrity_mismatch"
    pending = await mask_owner_for(db_session, reference, state="pending")
    assert pending.published_at is None
    assert pending.storage_ref["object_key"] in storage.objects
    storage.client.put_object.assert_called_once()
    storage.client.delete_object.assert_not_called()


@pytest.mark.parametrize("version", [None, "null", "version-1"])
async def test_read_pins_real_version_and_rechecks_current_key(version, db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference, _ = await ready_mask_owner(db_session, storage, rle)
    head, get = (
        storage.client.head_object.side_effect,
        storage.client.get_object.side_effect,
    )

    def versioned_head(**kwargs):
        assert "VersionId" not in kwargs
        return {
            **head(**kwargs),
            **({"VersionId": version} if version is not None else {}),
        }

    def versioned_get(**kwargs):
        assert kwargs.get("VersionId") == (version if version != "null" else None)
        assert kwargs["IfMatch"]
        return {
            **get(**kwargs),
            **({"VersionId": version} if version is not None else {}),
        }

    storage.client.head_object.side_effect = versioned_head
    storage.client.get_object.side_effect = versioned_get
    assert await load_coco_rle(reference, storage=storage, db=db_session) == rle
    assert storage.client.head_object.call_count == 2


@pytest.mark.parametrize("stage", ["response", "after_close"])
async def test_read_rejects_identity_changes_and_closes_body(stage, db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference, physical_key = await ready_mask_owner(db_session, storage, rle)
    raw = canonical_rle_bytes(rle)

    class Replaced(BytesIO):
        def close(self):
            if stage == "after_close":
                storage.objects[physical_key] = b"Z" * len(raw)
            super().close()

    stream = Replaced(raw)
    storage.open_body = lambda _raw: stream
    get = storage.client.get_object.side_effect
    if stage == "response":
        storage.client.get_object.side_effect = lambda **kwargs: {
            **get(**kwargs),
            "ETag": '"different"',
        }
    with pytest.raises(raster_object_storage.RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_integrity_mismatch"
    assert stream.closed


@pytest.mark.parametrize(
    "mode", ["short_chunks", "truncated", "overflow", "read_error", "close_error"]
)
async def test_read_bounds_and_real_stream_closure(mode, monkeypatch, db_session):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference, _ = await ready_mask_owner(db_session, storage, rle)
    raw = canonical_rle_bytes(rle)
    monkeypatch.setattr(raster_object_storage, "CHUNK_BYTES", 5)

    class Chunked(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 5
            if mode == "read_error":
                raise OSError("read failed")
            return super().read(min(size, 3) if mode == "short_chunks" else size)

        def close(self):
            super().close()
            if mode == "close_error":
                raise OSError("close acknowledgement unknown")

    content = (
        raw[:-1] if mode == "truncated" else raw + b"X" if mode == "overflow" else raw
    )
    stream = Chunked(content)
    storage.open_body = lambda _raw: stream
    if mode == "short_chunks":
        assert await load_coco_rle(reference, storage=storage, db=db_session) == rle
    else:
        with pytest.raises(raster_object_storage.RasterObjectStorageError) as raised:
            await load_coco_rle(reference, storage=storage, db=db_session)
        expected = (
            "unavailable"
            if mode in ("read_error", "close_error")
            else "integrity_mismatch"
        )
        assert raised.value.code == "raster_object_storage_" + expected
        if mode == "close_error":
            assert raised.value.storage_operation_unsettled
    assert stream.closed


@pytest.mark.parametrize(
    "code,reason",
    [
        ("NoSuchKey", "missing"),
        ("NoSuchVersion", "missing"),
        ("PreconditionFailed", "integrity_mismatch"),
    ],
)
async def test_read_preserves_missing_and_changed_error_contract(
    code, reason, db_session
):
    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference, _ = await ready_mask_owner(db_session, storage, rle)
    storage.client.get_object.side_effect = ClientError(
        {"Error": {"Code": code}}, "GetObject"
    )
    with pytest.raises(raster_object_storage.RasterObjectStorageError) as rejected:
        await load_coco_rle(reference, storage=storage, db=db_session)
    assert rejected.value.code == "raster_object_storage_" + reason
    assert rejected.value.status_code == (404 if reason == "missing" else 409)


@pytest.mark.parametrize(
    "stage,interrupt",
    [
        ("read", "cancel"),
        ("close", "cancel"),
        ("close", "timeout"),
        ("close_unknown", "cancel"),
    ],
)
async def test_interrupted_load_drains_real_stream(
    stage, interrupt, monkeypatch, db_session
):
    from app.observability.metrics import RASTER_MASK_CONTENT_OPERATIONS_TOTAL

    storage = memory_mask_storage()
    rle = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 5]}
    reference, _ = await ready_mask_owner(db_session, storage, rle)
    raw = canonical_rle_bytes(rle)
    entered, release = threading.Event(), threading.Event()
    draining = asyncio.Event()
    finish = storage_access.finish

    async def observed_finish(task):
        draining.set()
        return await finish(task)

    monkeypatch.setattr(storage_access, "finish", observed_finish)
    success_counter = RASTER_MASK_CONTENT_OPERATIONS_TOTAL.labels(
        operation="load", outcome="success"
    )
    success_before = success_counter._value.get()

    def block():
        entered.set()
        assert release.wait(5)

    class Blocked(BytesIO):
        def read(self, size=-1):
            if stage == "read":
                block()
            return super().read(size)

        def close(self):
            if stage.startswith("close"):
                block()
            super().close()
            if stage == "close_unknown":
                raise OSError("close acknowledgement unknown")

    stream = Blocked(raw)
    storage.open_body = lambda _raw: stream
    operation = load_coco_rle(reference, storage=storage, db=db_session)
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if interrupt == "timeout":
            task = asyncio.create_task(asyncio.wait_for(task, 0.05))
        else:
            task.cancel()
        await asyncio.wait_for(draining.wait(), 2)
        if interrupt == "cancel":
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert not stream.closed
    finally:
        release.set()
        with pytest.raises(
            TimeoutError if interrupt == "timeout" else asyncio.CancelledError
        ) as raised:
            await task
        if stage == "close_unknown":
            assert raised.value.storage_operation_unsettled
    assert stream.closed
    assert success_counter._value.get() == success_before


@pytest.mark.asyncio
async def test_reference_locks_are_sorted_deduplicated_and_verified(monkeypatch):
    import app.services.raster_mask_storage as module

    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: True))
    )
    refs = [
        {"object_key": "raster-masks/sha256/b.json", "sha256": "b"},
        {"object_key": "raster-masks/sha256/a.json", "sha256": "a"},
        {"object_key": "raster-masks/sha256/b.json", "sha256": "b"},
    ]
    load = AsyncMock()
    monkeypatch.setattr(module, "load_coco_rle", load)
    bind = AsyncMock()
    monkeypatch.setattr("app.services.raster_mask_objects.bind_publications", bind)

    assert await lock_raster_mask_references(db, {"items": refs}) == [
        "raster-masks/sha256/a.json",
        "raster-masks/sha256/b.json",
    ]
    assert [call.args[1]["key"] for call in db.execute.await_args_list] == [
        "aap:raster-mask:raster-masks/sha256/a.json",
        "aap:raster-mask:raster-masks/sha256/b.json",
    ]
    assert [call.args[0]["sha256"] for call in load.call_args_list] == ["a", "b"]
    bind.assert_awaited_once_with(db, {"items": refs})
    assert all(
        "pg_try_advisory_xact_lock" in str(call.args[0])
        for call in db.execute.await_args_list
    )


async def test_reference_lock_conflict_rejects_before_read_or_binding(monkeypatch):
    import app.services.raster_mask_storage as module

    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: False))
    )
    load, bind = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module, "load_coco_rle", load)
    monkeypatch.setattr("app.services.raster_mask_objects.bind_publications", bind)
    with pytest.raises(RasterMaskContractError) as rejected:
        await lock_raster_mask_references(
            db, {"object_key": "raster-masks/sha256/a.json"}
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["reason"] == "mask_content_busy"
    load.assert_not_awaited()
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_mask_geometry_context_matches_video_size_and_frame_count():
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=1920,
                height=1080,
                metadata_={"video": {"frame_count": 10}},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    geometry = {
        "type": "video_track_mask",
        "keyframes": [{"frame_index": 9, "mask": {"size": [1080, 1920]}}],
        "outside": [{"from": 2, "to": 4}],
    }
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0]["mask"]["size"] = (1080, 1920)
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0]["frame_index"] = 10
    with pytest.raises(ValueError, match="frame_index"):
        await validate_mask_geometry_for_task(db, task, geometry)

    geometry["keyframes"][0] = {"frame_index": 9, "mask": {"size": [720, 1280]}}
    with pytest.raises(ValueError, match="mask size"):
        await validate_mask_geometry_for_task(db, task, geometry)


@pytest.mark.asyncio
async def test_single_frame_video_mask_matches_video_size_and_frame_count():
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=1920,
                height=1080,
                metadata_={"video": {"frame_count": 10}},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    geometry = {
        "type": "video_mask",
        "frame_index": 9,
        "mask": {"size": [1080, 1920]},
    }
    await validate_mask_geometry_for_task(db, task, geometry)

    geometry["frame_index"] = 10
    with pytest.raises(ValueError, match="frame_index"):
        await validate_mask_geometry_for_task(db, task, geometry)

    geometry["frame_index"] = 9
    geometry["mask"]["size"] = [720, 1280]
    with pytest.raises(ValueError, match="mask size"):
        await validate_mask_geometry_for_task(db, task, geometry)


@pytest.mark.asyncio
async def test_mask_geometry_context_separates_image_8k_from_video_4k_limit():
    image_db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="image",
                width=8192,
                height=8192,
                metadata_={},
            )
        )
    )
    task = SimpleNamespace(dataset_item_id="item-1")
    await validate_mask_geometry_for_task(
        image_db,
        task,
        {"type": "raster_mask", "mask": {"size": [8192, 8192]}},
    )

    video_db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                file_type="video",
                width=4097,
                height=1,
                metadata_={"video": {"frame_count": 1}},
            )
        )
    )
    with pytest.raises(RasterMaskContractError) as captured:
        await validate_mask_geometry_for_task(
            video_db,
            task,
            {
                "type": "video_track_mask",
                "keyframes": [{"frame_index": 0, "mask": {"size": [1, 4097]}}],
            },
        )

    assert captured.value.detail["reason"] == "video_mask_dimensions_exceeded"
