"""Controlled NuScenes creation in per-test durable disposable namespaces."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import numpy as np
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.enums import UserRole
from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.task import Task
from app.db.models.user import User
from app.services import auxiliary_originals, managed_original_creation as creation
from app.services import managed_original_reader as reader
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import ensure_legacy_bindings
from scripts.import_nuscenes_scene import (
    _lidar_bin_to_binary_pcd,
    _local_stage,
    import_nuscenes,
)
from tests.test_import_nuscenes_lite import _write_fake_nuscenes
from tests.test_managed_original_creation import Client, migration


@pytest.fixture
async def owned_import(test_engine, monkeypatch, tmp_path):
    schema = "nuscenes_owned_" + uuid.uuid4().hex
    tables = (
        "users",
        "datasets",
        "dataset_items",
        "scenes",
        "projects",
        "project_datasets",
        "tasks",
        "task_dataset_item_links",
        "scene_frame_poses",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "dataset_assets",
        "dataset_asset_locations",
        "managed_original_retirements",
    )
    async with test_engine.begin() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for entity in ("tasks", "scenes"):
            await connection.execute(
                text(f'CREATE SEQUENCE "{schema}".display_seq_{entity}')
            )
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
        await connection.execute(
            text(
                f'ALTER TABLE "{schema}".scene_frame_poses ADD CONSTRAINT '
                "uq_scene_frame_pose UNIQUE (scene_id, frame_index)"
            )
        )
        for table, column in (
            ("dataset_items", "nuscenes_raw_original_id"),
            ("scenes", "nuscenes_map_original_id"),
            ("dataset_asset_locations", "creation_id"),
            ("managed_original_retirements", "creation_id"),
        ):
            await connection.execute(
                text(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN IF EXISTS {column}')
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    client = Client()
    client.meta = SimpleNamespace(
        endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    monkeypatch.setattr(creation, "create_client", lambda **_: client)
    root = tmp_path / "source"
    _write_fake_nuscenes(root, scenes=2, samples_per=2)
    try:
        async with engine.begin() as connection:
            for filename in (
                "0219_managed_original_creations.py",
                "0230_managed_original_task_scope.py",
                "0235_dataset_auxiliary_originals.py",
            ):
                await connection.run_sync(
                    lambda sync, filename=filename: migration(sync, filename).upgrade()
                )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            actor = User(
                id=uuid.uuid4(),
                name="nuscenes-owned",
                email="nuscenes-owned@test.invalid",
                password_hash="unused",
                role=UserRole.SUPER_ADMIN,
                is_active=True,
            )
            db.add(actor)
            await db.commit()
            yield db, actor, client, root
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def run_import(db, actor, root, scenes=None):
    return await import_nuscenes(
        db,
        nuscenes_root=root,
        scene_names=scenes or ["scene-0000", "scene-0001"],
        dataset_name="owned-nuscenes",
        owner_id=actor.id,
    )


async def test_new_import_owns_originals_preserves_counts_and_caller_transaction(
    owned_import,
):
    db, actor, _client, root = owned_import
    result = await run_import(db, actor, root)
    assert result["total_items"] == 10
    # The independent ledgers commit; the caller's Dataset does not.
    async with creation.storage_access.engine(db).connect() as connection:
        assert (
            await connection.scalar(
                select(Dataset.id).where(Dataset.id == result["dataset_id"])
            )
            is None
        )
        assert await connection.scalar(select(DatasetAuxiliaryOriginal.id)) is not None
    await db.commit()
    items = list(await db.scalars(select(DatasetItem)))
    assert len(items) == 10
    assert len(list(await db.scalars(select(DatasetAsset)))) == 10
    assert all(
        row.creation_id for row in await db.scalars(select(DatasetAssetLocation))
    )
    scenes = list(await db.scalars(select(Scene)))
    assert len({scene.nuscenes_map_original_id for scene in scenes}) == 1
    tasks = list(await db.scalars(select(Task)))
    assert len(tasks) == 4
    for task in tasks:
        raw = await auxiliary_originals.resolve_task_raw(db, task.id)
        map_source = await auxiliary_originals.resolve_scene_map(db, task.id)
        assert raw.media_type == "raw_lidar" and map_source.media_type == "map"
        assert raw.content_id != map_source.content_id
        assert raw.item_id == task.dataset_item_id
        assert raw.ownership_facts.kind == "raw_lidar"


async def test_idempotent_import_reuses_all_canonical_receipts(owned_import):
    db, actor, client, root = owned_import
    await run_import(db, actor, root)
    await db.commit()
    before = [
        (row.id, row.file_path, deepcopy(row.metadata_))
        for row in await db.scalars(select(DatasetItem).order_by(DatasetItem.id))
    ]
    created = list(
        await db.scalars(
            select(ManagedOriginalCreation.id).order_by(ManagedOriginalCreation.id)
        )
    )
    result = await run_import(db, actor, root)
    await db.commit()
    after = [
        (row.id, row.file_path, row.metadata_)
        for row in await db.scalars(select(DatasetItem).order_by(DatasetItem.id))
    ]
    assert before == after and result["total_items"] == 0
    assert created == list(
        await db.scalars(
            select(ManagedOriginalCreation.id).order_by(ManagedOriginalCreation.id)
        )
    )
    assert client.closed > len(created)


async def test_late_source_drift_leaves_known_unbound_responsibility(owned_import):
    db, actor, _client, root = owned_import
    # Establish a full prior scene, then alter its local original bytes.
    await run_import(db, actor, root)
    await db.commit()
    path = next((root / "samples/LIDAR_TOP").glob("*.pcd.bin"))
    path.write_bytes(path.read_bytes()[:-4] + b"wrong")
    owners = list(
        await db.scalars(
            select(DatasetAuxiliaryOriginal.id).order_by(DatasetAuxiliaryOriginal.id)
        )
    )
    with pytest.raises(ValueError, match="source drift"):
        await run_import(db, actor, root)
    await db.rollback()
    assert owners == list(
        await db.scalars(
            select(DatasetAuxiliaryOriginal.id).order_by(DatasetAuxiliaryOriginal.id)
        )
    )
    assert len(list(await db.scalars(select(Task)))) == 4


@pytest.mark.parametrize("changed", ["scene", "dataset"])
async def test_other_connection_change_during_physical_read_blocks_final_binding(
    owned_import, monkeypatch, changed
):
    db, actor, _client, root = owned_import
    result = await run_import(db, actor, root)
    await db.commit()
    scene = await db.scalar(select(Scene).order_by(Scene.name))
    identifier = scene.id if changed == "scene" else result["dataset_id"]
    model = Scene if changed == "scene" else Dataset
    field = Scene.source_metadata if changed == "scene" else Dataset.metadata_
    original_run, mutated = creation._Physical.run, False

    async def mutate(self, function, *args, **kwargs):
        nonlocal mutated
        value = await original_run(self, function, *args, **kwargs)
        if function is reader._capture and not mutated:
            mutated = True
            async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
                await other.execute(text("SET LOCAL lock_timeout = '1s'"))
                current = await other.scalar(
                    select(field).where(model.id == identifier)
                )
                await other.execute(
                    update(model)
                    .where(model.id == identifier)
                    .values({field: {**current, "concurrent_marker": "retained"}})
                )
                await other.commit()
        return value

    monkeypatch.setattr(creation._Physical, "run", mutate)
    with pytest.raises(ValueError, match=changed + " changed before final binding"):
        await run_import(db, actor, root)
    assert mutated
    await db.rollback()
    current = await db.scalar(select(field).where(model.id == identifier))
    assert current["concurrent_marker"] == "retained"
    assert len(list(await db.scalars(select(Task)))) == 4


async def test_all_scene_physical_work_precedes_existing_business_write_locks(
    owned_import, monkeypatch
):
    db, actor, _client, root = owned_import
    await run_import(db, actor, root)
    await db.commit()
    identifiers = list(await db.scalars(select(Scene.id)))
    original_run, captures = creation._Physical.run, 0

    async def check(self, function, *args, **kwargs):
        nonlocal captures
        value = await original_run(self, function, *args, **kwargs)
        if function is reader._capture:
            captures += 1
            async with async_sessionmaker(db.bind)() as other:
                await other.execute(text("SET LOCAL lock_timeout = '1s'"))
                await other.execute(
                    select(Scene.id)
                    .where(Scene.id.in_(identifiers))
                    .with_for_update(nowait=True)
                )
                await other.rollback()
        return value

    monkeypatch.setattr(creation._Physical, "run", check)
    await run_import(db, actor, root)
    assert captures >= 2
    await db.rollback()


async def test_sensor_import_registers_auxiliary_but_ego_export_resolver_refuses(
    owned_import,
):
    db, actor, _client, root = owned_import
    await import_nuscenes(
        db,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="owned-nuscenes",
        owner_id=actor.id,
        frame="sensor",
    )
    await db.commit()
    scene = await db.scalar(select(Scene))
    assert scene.source_metadata["frame"] == "sensor"
    assert scene.nuscenes_map_original_id is not None
    task = await db.scalar(select(Task))
    with pytest.raises(ContentError, match="auxiliary_original_contract_changed"):
        await auxiliary_originals.resolve_task_raw(db, task.id)


@pytest.mark.parametrize("changed", ["raw", "map"])
async def test_legacy_proof_drift_is_not_adopted_when_auxiliary_pointer_is_missing(
    owned_import, changed
):
    db, actor, _client, root = owned_import
    await run_import(db, actor, root, ["scene-0000"])
    await db.commit()
    original_scene = await db.scalar(select(Scene))
    original_items = list(await db.scalars(select(DatasetItem)))
    # A separate historical import has metadata receipts but no canonical
    # registration. It cannot borrow the first import's ownership by inference.
    legacy = Dataset(
        id=uuid.uuid4(),
        display_id="DS-NU-legacy-proof",
        name="legacy-proof",
        data_type="point_cloud",
        is_temporal=True,
        created_by=actor.id,
        metadata_={
            "axis_convention": "iso_8855",
            "source": {"storage_layout": "legacy_name"},
        },
    )
    db.add(legacy)
    await db.flush()
    scene = Scene(
        id=uuid.uuid4(),
        display_id="SCN-legacy-proof",
        dataset_id=legacy.id,
        name=original_scene.name,
        source_format="nuscenes",
        source_metadata=deepcopy(original_scene.source_metadata),
        created_by=actor.id,
    )
    db.add(scene)
    await db.flush()
    scene_id, legacy_id = scene.id, legacy.id
    for item in original_items:
        db.add(
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=legacy.id,
                scene_id=scene.id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=item.file_type,
                file_size=item.file_size,
                content_hash=item.content_hash,
                width=item.width,
                height=item.height,
                frame_index=item.frame_index,
                metadata_=deepcopy(item.metadata_),
            )
        )
    await db.commit()
    if changed == "raw":
        path = next((root / "samples/LIDAR_TOP").glob("*.pcd.bin"))
        points = np.frombuffer(path.read_bytes(), dtype="<f4").copy()
        points[3] += 0.125  # XYZ-derived PCD is unchanged; raw proof is not.
        path.write_bytes(points.tobytes())
    else:
        path = root / "maps/map-0.png"
        path.write_bytes(path.read_bytes() + b"changed-original-map")
    with pytest.raises(ValueError, match="source drift"):
        await import_nuscenes(
            db,
            nuscenes_root=root,
            scene_names=["scene-0000"],
            dataset_name="legacy-proof",
            owner_id=actor.id,
        )
    await db.rollback()
    assert (
        await db.scalar(
            select(Scene.nuscenes_map_original_id).where(Scene.id == scene_id)
        )
        is None
    )
    assert all(
        pointer is None
        for pointer in await db.scalars(
            select(DatasetItem.nuscenes_raw_original_id).where(
                DatasetItem.dataset_id == legacy_id
            )
        )
    )


def test_one_bounded_raw_stage_drives_conversion_after_local_path_replacement(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    path = root / "source.pcd.bin"
    raw = np.asarray([[1, 2, 3, 0.5, 9]], dtype="<f4").tobytes()
    path.write_bytes(raw)
    with _local_stage(root, path.name) as stage:
        path.write_bytes(np.asarray([[7, 8, 9, 0.1, 2]], dtype="<f4").tobytes())
        pcd = _lidar_bin_to_binary_pcd(stage.file)
        body = pcd.split(b"DATA binary\n", 1)[1]
        assert np.frombuffer(body, dtype="<f4").tolist() == [1, 2, 3]
        assert stage.sha256 == hashlib.sha256(raw).hexdigest()
        assert stage.size == len(raw)
        stage.file.seek(0)
        assert stage.file.read() == raw


def test_local_stage_rejects_oversize_before_opening_body(tmp_path):
    path = tmp_path / "large.pcd.bin"
    with path.open("wb") as stream:
        stream.truncate(256 * 1024**2 + 1)
    with pytest.raises(ValueError, match="byte budget"):
        with _local_stage(tmp_path, path.name):
            pytest.fail("oversized source must not yield a stage")
