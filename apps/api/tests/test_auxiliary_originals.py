"""Real auxiliary responsibility and narrow pure contract regressions."""

import asyncio
from datetime import datetime, timezone
import io
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Scene
from app.db.models.dataset_auxiliary_original import (
    DatasetAuxiliaryOriginal as Original,
)
from app.services import auxiliary_originals as service
from app.services import managed_original_creation as creation
from app.services.content_snapshot import ContentError
from tests.test_managed_original_creation import (
    client as client,
    creation_db as creation_db,
    migration,
)


def map_source():
    return {
        "token": "map",
        "filename": "maps/a.png",
        "category": "semantic_prior",
        "log_tokens": ["log"],
    }


def raw_source():
    return {
        "sample": {"token": "sample", "scene_token": "scene"},
        "sample_data": {
            "token": "data",
            "sample_token": "sample",
            "calibrated_sensor_token": "calibrated",
            "ego_pose_token": "pose",
            "timestamp": 1,
            "is_key_frame": True,
            "filename": "samples/LIDAR_TOP/a.pcd.bin",
        },
        "calibrated_sensor": {"token": "calibrated", "sensor_token": "sensor"},
        "sensor": {"token": "sensor", "modality": "lidar"},
        "ego_pose": {"token": "pose", "timestamp": 1},
    }


@pytest.fixture
async def auxiliary_db(creation_db):
    db = creation_db
    connection = await db.connection()
    await connection.execute(
        text("CREATE TABLE scenes (LIKE public.scenes INCLUDING ALL)")
    )
    for table, column in (
        ("dataset_items", "nuscenes_raw_original_id"),
        ("scenes", "nuscenes_map_original_id"),
    ):
        await connection.execute(
            text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
        )
    await connection.run_sync(
        lambda sync: migration(sync, "0235_dataset_auxiliary_originals.py").upgrade()
    )
    # The historical scope setup precedes saved current-owner addresses.
    await connection.execute(
        text("ALTER TABLE dataset_asset_locations DROP COLUMN storage_ref")
    )
    await connection.run_sync(
        lambda sync: migration(sync, "0251_dataset_original_targets.py").upgrade()
    )
    await db.commit()
    return db


async def saved(db, *, kind="map", dataset_id=None, data=b"map"):
    source = map_source() if kind == "map" else raw_source()
    allocation = await creation.allocate_auxiliary(
        db,
        dataset_id=dataset_id or uuid.uuid4(),
        kind=kind,
        source_contract=source,
        logical_path="aux/original",
        max_bytes=100,
        authority={"policy": "owned-test"},
    )
    created = await creation.create_stream(
        db, allocation, io.BytesIO(data), content_type=allocation["content_type"]
    )
    owner = await service.publish(
        db, allocation, created, kind=kind, source_contract=source
    )
    return owner, allocation, created


async def test_independent_verified_owner_survives_business_rollback(
    auxiliary_db, client
):
    owner, allocation, created = await saved(auxiliary_db)
    await auxiliary_db.rollback()
    assert (await auxiliary_db.get(Original, owner.id)).sha256 == created.sha256
    assert allocation["owner_kind"] == "dataset_auxiliary"
    assert allocation["object_key"] == service.key_for(owner.dataset_id, owner.id)
    assert allocation["staging_key"] is None
    again = await service.publish(
        auxiliary_db, allocation, created, kind="map", source_contract=map_source()
    )
    assert again.id == owner.id
    assert (
        service.snapshot(owner).source_contract
        == service.snapshot(again).source_contract
    )


@pytest.mark.parametrize("field", service.FACTS)
async def test_saved_identity_cannot_change_or_be_deleted(auxiliary_db, client, field):
    owner, _, _ = await saved(auxiliary_db)
    value = getattr(owner, field)
    replacement = (
        uuid.uuid4()
        if isinstance(value, uuid.UUID)
        else value + 1
        if type(value) is int
        else {"changed": True}
        if isinstance(value, dict)
        else datetime(2000, 1, 1, tzinfo=timezone.utc)
        if isinstance(value, datetime)
        else "changed"
    )
    with pytest.raises(IntegrityError):
        async with auxiliary_db.begin_nested():
            await auxiliary_db.execute(
                update(Original)
                .where(Original.id == owner.id)
                .values(**{field: replacement})
            )
    with pytest.raises(IntegrityError):
        async with auxiliary_db.begin_nested():
            await auxiliary_db.execute(delete(Original).where(Original.id == owner.id))


async def test_creation_kind_and_contract_are_paired_at_database_boundary(
    auxiliary_db, client
):
    owner, _, _ = await saved(auxiliary_db)
    values = {name: getattr(owner, name) for name in service.FACTS}
    values["kind"] = "raw_lidar"
    with pytest.raises(IntegrityError, match="matching verified creation"):
        async with auxiliary_db.begin_nested():
            await auxiliary_db.execute(Original.__table__.insert().values(**values))


async def test_map_sharing_and_business_delete_retain_owner(auxiliary_db, client):
    owner, _, _ = await saved(auxiliary_db)
    scenes = []
    for index in range(2):
        metadata = {
            "frame": "ego",
            "nuscenes_export": {
                "scene": {"token": str(index), "log_token": "log"},
                "log": {"token": "log"},
                "map": map_source(),
                "map_storage_key": owner.object_key,
                "map_file_size": owner.size_bytes,
                "map_sha256": owner.sha256,
            },
        }
        scene = Scene(
            id=uuid.uuid4(),
            display_id="aux-" + str(index),
            dataset_id=owner.dataset_id,
            name="scene-" + str(index),
            source_format="nuscenes",
            source_metadata=metadata,
        )
        auxiliary_db.add(scene)
        await auxiliary_db.flush()
        await service.bind_map(auxiliary_db, scene, owner)
        scenes.append(scene)
    await auxiliary_db.commit()
    await auxiliary_db.delete(scenes[0])
    await auxiliary_db.commit()
    assert (await auxiliary_db.get(Original, owner.id)).id == scenes[
        1
    ].nuscenes_map_original_id
    with pytest.raises(IntegrityError, match="contract mismatch"):
        async with auxiliary_db.begin_nested():
            wrong = Scene(
                id=uuid.uuid4(),
                display_id="aux-wrong",
                dataset_id=uuid.uuid4(),
                name="wrong",
                source_metadata=scenes[1].source_metadata,
                nuscenes_map_original_id=owner.id,
            )
            auxiliary_db.add(wrong)
            await auxiliary_db.flush()


def test_contract_does_not_accept_metadata_locators_as_authority():
    with pytest.raises(ContentError, match="contract_invalid"):
        service.contract("map", {"source_storage_key": "some/key", "sha256": "a" * 64})
    with pytest.raises(ContentError, match="source_budget_exceeded"):
        service.contract("map", map_source() | {"huge": "x" * 65536})
    with pytest.raises(ContentError, match="contract_invalid"):
        service.contract("raw_lidar", raw_source() | {"source_storage_key": "some/key"})


@pytest.mark.asyncio
async def test_physical_baseexception_and_repeated_cancel_drain_before_source_close():
    class Stop(BaseException):
        pass

    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    source = io.BytesIO(b"bytes")
    physical = creation._Physical()

    def native():
        started.set()
        release.wait(5)
        assert not source.closed
        stopped.set()
        return SimpleNamespace(receipt="complete")

    task = asyncio.create_task(physical.run(native))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not stopped.is_set()
    release.set()
    result = await task
    assert result.receipt == "complete" and stopped.is_set()
    assert isinstance(physical.interrupted, asyncio.CancelledError)
    source.close()

    async def fail():
        raise Stop()

    with pytest.raises(Stop):
        await creation._drain(asyncio.create_task(fail()))


@pytest.mark.asyncio
async def test_successful_future_still_latches_external_baseexception(monkeypatch):
    class Stop(BaseException):
        pass

    original = asyncio.shield

    async def interrupt_after_done(future):
        await original(future)
        raise Stop()

    monkeypatch.setattr(asyncio, "shield", interrupt_after_done)
    physical = creation._Physical()
    assert await physical.run(lambda: "late success") == "late success"
    assert isinstance(physical.interrupted, Stop)


async def test_interrupted_late_unknown_keeps_durable_permit(
    auxiliary_db, client, monkeypatch
):
    from app.services import managed_original_writer as writer
    from app.db.models.storage_maintenance import StorageOperation

    entered, release = threading.Event(), threading.Event()
    allocation = await creation.allocate_auxiliary(
        auxiliary_db,
        dataset_id=uuid.uuid4(),
        kind="map",
        source_contract=map_source(),
        logical_path="map/original",
        max_bytes=100,
        authority={"policy": "owned-test"},
    )

    def unknown(*args, **kwargs):
        entered.set()
        release.wait(5)
        raise writer.WriterError("creation_complete_unknown", outcome="unknown")

    monkeypatch.setattr(writer, "prepare_stream", unknown)
    operation = asyncio.create_task(
        creation._execute(auxiliary_db, allocation, io.BytesIO(b"bytes"))
    )
    await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    release.set()
    with pytest.raises(ContentError) as failure:
        await operation
    assert failure.value.storage_operation_unsettled is True
    row = await creation._read(auxiliary_db, allocation["id"])
    assert row["status"] == "unknown"
    async with service.storage_access.engine(auxiliary_db).connect() as connection:
        assert (
            await connection.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.id == row["operation_id"]
                )
            )
            is not None
        )
