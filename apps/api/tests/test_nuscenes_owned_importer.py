"""Controlled NuScenes creation in per-test durable disposable namespaces."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import numpy as np
import pytest
from sqlalchemy import select, text, update

from app.db.enums import UserRole
from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.task import Task
from app.db.models.user import User
from app.services import auxiliary_originals, managed_original_creation as creation
from app.services.assets_operator import AssetsOperatorGateway
from app.services.content_snapshot import ContentError
from scripts.import_nuscenes_scene import (
    _lidar_bin_to_binary_pcd,
    _resolve_source_path,
    _safe_source_path,
    import_nuscenes,
)
from tests.test_import_nuscenes_lite import _write_fake_nuscenes
from tests.test_managed_original_creation import Client


@pytest.fixture(autouse=True)
async def _restore_owned_originals(test_engine):
    """Restore fixture-owned independent rows after the caller transaction ends.

    Autouse fixtures are prepared before ``db_session`` and therefore torn down
    after it, so the caller's savepoint/outer transaction (and its row locks) is
    already gone when the guarded restore runs.
    """
    actors: list[uuid.UUID] = []
    yield actors
    if not actors:
        return
    from sqlalchemy import bindparam

    actors_in = bindparam("actors", expanding=True)
    async with test_engine.begin() as cleanup:
        database = await cleanup.scalar(text("SELECT current_database()"))
        if not isinstance(database, str) or not database.endswith("_test"):
            raise RuntimeError(
                "refusing to restore owned original state outside a disposable test database"
            )
        await cleanup.execute(
            text(
                "DELETE FROM storage_operations WHERE id IN ("
                " SELECT operation_id FROM managed_original_creations"
                " WHERE requested_by IN :actors AND operation_id IS NOT NULL)"
            ).bindparams(actors_in),
            {"actors": actors},
        )
        await cleanup.execute(
            text(
                "DELETE FROM dataset_auxiliary_originals WHERE creation_id IN ("
                " SELECT id FROM managed_original_creations"
                " WHERE requested_by IN :actors)"
            ).bindparams(actors_in),
            {"actors": actors},
        )
        await cleanup.execute(
            text(
                "ALTER TABLE managed_original_creations "
                "DISABLE TRIGGER trg_managed_original_creation_history"
            )
        )
        try:
            await cleanup.execute(
                text(
                    "DELETE FROM managed_original_creations"
                    " WHERE requested_by IN :actors"
                ).bindparams(actors_in),
                {"actors": actors},
            )
        finally:
            await cleanup.execute(
                text(
                    "ALTER TABLE managed_original_creations "
                    "ENABLE TRIGGER trg_managed_original_creation_history"
                )
            )


@pytest.fixture
async def owned_import(db_session, _restore_owned_originals, monkeypatch, tmp_path):
    from app.services.storage import storage_service

    client = Client()
    client.meta = SimpleNamespace(
        endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    monkeypatch.setattr(storage_service, "client", client)
    root = tmp_path / "source"
    _write_fake_nuscenes(root, scenes=2, samples_per=2)
    suffix = uuid.uuid4().hex[:10]
    actor = User(
        id=uuid.uuid4(),
        name="nuscenes-owned-" + suffix,
        email=f"nuscenes-owned-{suffix}@test.invalid",
        password_hash="unused",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )
    db_session.add(actor)
    await db_session.flush()
    # Owned imports expire the session repeatedly; keep the fixture actor's
    # facts available without a lazy load outside greenlet context.
    db_session.expunge(actor)
    _restore_owned_originals.append(actor.id)
    yield db_session, actor, client, root


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
    # Business rows (Dataset and its auxiliary owners) stay in the caller
    # transaction; only independent physical creation responsibility commits.
    async with creation.storage_access.engine(db).connect() as connection:
        assert (
            await connection.scalar(
                select(Dataset.id).where(Dataset.id == result["dataset_id"])
            )
            is None
        )
        assert (
            await connection.scalar(
                select(DatasetAuxiliaryOriginal.id).where(
                    DatasetAuxiliaryOriginal.dataset_id == result["dataset_id"]
                )
            )
            is None
        )
        assert (
            await connection.scalar(
                select(ManagedOriginalCreation.id).where(
                    ManagedOriginalCreation.requested_by == actor.id,
                    ManagedOriginalCreation.status == "verified",
                )
            )
            is not None
        )
    await db.commit()
    assert (
        await db.scalar(
            select(DatasetAuxiliaryOriginal.id).where(
                DatasetAuxiliaryOriginal.dataset_id == result["dataset_id"]
            )
        )
        is not None
    )
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
    keys_before = set(client.objects)
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
    assert set(client.objects) == keys_before


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
    with pytest.raises(ValueError):
        await run_import(db, actor, root)
    await db.rollback()
    assert owners == list(
        await db.scalars(
            select(DatasetAuxiliaryOriginal.id).order_by(DatasetAuxiliaryOriginal.id)
        )
    )
    assert len(list(await db.scalars(select(Task)))) == 4


@pytest.mark.parametrize("changed", ["scene", "dataset"])
async def test_business_change_during_replay_blocks_final_binding(
    owned_import, monkeypatch, changed
):
    db, actor, _client, root = owned_import
    result = await run_import(db, actor, root)
    await db.commit()
    scene = await db.scalar(select(Scene).order_by(Scene.name))
    identifier = scene.id if changed == "scene" else result["dataset_id"]
    model = Scene if changed == "scene" else Dataset
    field = Scene.source_metadata if changed == "scene" else Dataset.metadata_
    original_run, mutated = AssetsOperatorGateway.ensure_auxiliary_original, False

    async def mutate(self, **kwargs):
        nonlocal mutated
        value = await original_run(self, **kwargs)
        if not mutated:
            mutated = True
            current = await db.scalar(select(field).where(model.id == identifier))
            assert current is not None
            await db.execute(
                update(model)
                .where(model.id == identifier)
                .values({field: {**current, "concurrent_marker": "drifted"}})
            )
        return value

    monkeypatch.setattr(AssetsOperatorGateway, "ensure_auxiliary_original", mutate)
    with pytest.raises(ValueError):
        await run_import(db, actor, root)
    assert mutated
    await db.rollback()
    current = await db.scalar(select(field).where(model.id == identifier))
    assert current is not None and "concurrent_marker" not in current
    assert len(list(await db.scalars(select(Task)))) == 4


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
    with pytest.raises(ValueError):
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


def test_local_source_paths_stay_inside_the_root_and_reject_unsafe_names(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    (root / "source.pcd.bin").write_bytes(b"point cloud")
    assert (
        _resolve_source_path(root, "source.pcd.bin")
        == (root / "source.pcd.bin").resolve()
    )

    for name in (
        "../escape.bin",
        "/absolute.bin",
        "nested/../../escape.bin",
        "back\\slash.bin",
        "drive:C.bin",
        "nul\0byte.bin",
        "./relative.bin",
        "trailing/",
    ):
        with pytest.raises(ValueError):
            _safe_source_path(name)

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "link.bin").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        _resolve_source_path(root, "link.bin")
    with pytest.raises((FileNotFoundError, ValueError)):
        _resolve_source_path(root, "missing.bin")


def test_lidar_bin_conversion_reads_one_resolved_local_source(tmp_path):
    path = tmp_path / "source.pcd.bin"
    raw = np.asarray([[1, 2, 3, 0.5, 9]], dtype="<f4").tobytes()
    path.write_bytes(raw)
    source = _resolve_source_path(tmp_path, path.name)

    pcd = _lidar_bin_to_binary_pcd(source)

    body = pcd.split(b"DATA binary\n", 1)[1]
    assert np.frombuffer(body, dtype="<f4").tolist() == [1, 2, 3]
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == hashlib.sha256(raw).hexdigest()
    )
    assert source.stat().st_size == len(raw)


async def test_unknown_physical_write_retains_independent_creation_and_admission(
    owned_import, monkeypatch
):
    db, actor, _client, root = owned_import
    actor_id = actor.id
    from app.services import managed_original_writer as writer

    def fail_complete(*_args, **_kwargs):
        raise writer.WriterError("creation_write_unknown", "unknown")

    monkeypatch.setattr(writer, "complete", fail_complete)
    with pytest.raises(Exception):
        await run_import(db, actor, root)
    await db.rollback()
    async with creation.storage_access.engine(db).connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT status, operation_id FROM managed_original_creations"
                    " WHERE requested_by = :actor"
                ),
                {"actor": actor_id},
            )
        ).all()
        assert any(
            status == "unknown" and operation_id is not None
            for status, operation_id in rows
        )
        operations = await connection.scalar(
            text(
                "SELECT count(*) FROM storage_operations WHERE id IN ("
                " SELECT operation_id FROM managed_original_creations"
                " WHERE requested_by = :actor AND operation_id IS NOT NULL)"
            ),
            {"actor": actor_id},
        )
        assert operations and operations > 0
