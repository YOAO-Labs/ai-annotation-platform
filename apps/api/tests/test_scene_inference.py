"""Assets owner scene planning and layout mutation regressions."""

from __future__ import annotations

import uuid

from app.db.models.dataset import Dataset, DatasetItem
from aap_assets_catalog.spatial_contract import (
    BackfillScenesRequest,
    CreateSceneRequest,
    ReadItemsRequest,
)
from app.services.assets_spatial import AssetsSpatialGateway


async def _make_dataset(db, owner_id, name="ds-test"):
    ds = Dataset(
        display_id=f"DS-{uuid.uuid4().hex[:6]}",
        name=name,
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    return ds


async def _add_item(db, dataset_id, file_path, file_type="point_cloud"):
    item = DatasetItem(
        dataset_id=dataset_id,
        file_name=file_path.split("/")[-1],
        file_path=file_path,
        file_type=file_type,
    )
    db.add(item)
    await db.flush()
    return item


async def _backfill(db, dataset_id, *, mode="auto", dry_run=False):
    return await AssetsSpatialGateway(db).backfill_scenes(
        BackfillScenesRequest(
            operation_id=uuid.uuid4(),
            actor_id=None,
            dataset_id=dataset_id,
            mode=mode,
            dry_run=dry_run,
        ),
        commit=False,
    )


async def _facts(db, dataset_id):
    return await AssetsSpatialGateway(db).read_items(
        ReadItemsRequest(dataset_id=dataset_id), same_session_native=True
    )


async def test_infer_single_scene_sustech_layout(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id, name="pc-scene")
    items = []
    for stem in ["000001", "000002", "000003"]:
        items.append(await _add_item(db_session, ds.id, f"pc-scene/lidar/{stem}.pcd"))
        items.append(
            await _add_item(
                db_session, ds.id, f"pc-scene/camera/front/{stem}.jpg", "image"
            )
        )
    await _add_item(
        db_session,
        ds.id,
        "pc-scene/calib/camera/front.json",
        "other",
    )

    result = await _backfill(db_session, ds.id)
    assert result.created_scenes == 1
    assert result.assigned_items >= 7

    facts = await _facts(db_session, ds.id)
    items_by_id = {item.id: item for item in facts.items}
    lidar_frames = sorted(
        items_by_id[item.id].frame_index
        for item in items
        if item.file_type == "point_cloud"
    )
    camera_frames = sorted(
        items_by_id[item.id].frame_index for item in items if item.file_type == "image"
    )
    assert lidar_frames == [0, 1, 2]
    assert camera_frames == [0, 1, 2]


async def test_infer_multi_scene_per_subdirectory(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id, name="multi")
    items_a, items_b = [], []
    for stem in ["000001", "000002"]:
        items_a.append(
            await _add_item(db_session, ds.id, f"multi/scene_a/lidar/{stem}.pcd")
        )
    for stem in ["000005", "000006", "000007"]:
        items_b.append(
            await _add_item(db_session, ds.id, f"multi/scene_b/lidar/{stem}.pcd")
        )

    result = await _backfill(db_session, ds.id)
    assert result.created_scenes == 2

    facts = await _facts(db_session, ds.id)
    items_by_id = {item.id: item for item in facts.items}
    frames_a = [items_by_id[item.id].frame_index for item in items_a]
    frames_b = [items_by_id[item.id].frame_index for item in items_b]
    scenes_a = {items_by_id[item.id].scene_id for item in items_a}
    scenes_b = {items_by_id[item.id].scene_id for item in items_b}
    assert sorted(frames_a) == [0, 1]
    assert sorted(frames_b) == [0, 1, 2]
    assert scenes_a.isdisjoint(scenes_b)


async def test_infer_flat_image_dataset(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id, name="img-seq")
    items = [
        await _add_item(db_session, ds.id, f"img-seq/{filename}", "image")
        for filename in ["frame_00010.jpg", "frame_00001.jpg", "frame_00002.jpg"]
    ]

    result = await _backfill(db_session, ds.id, mode="single")
    assert result.created_scenes == 1
    facts = await _facts(db_session, ds.id)
    ordered = sorted(
        (
            next(item for item in facts.items if item.id == seeded.id)
            for seeded in items
        ),
        key=lambda item: item.frame_index,
    )
    assert [item.file_name for item in ordered] == [
        "frame_00001.jpg",
        "frame_00002.jpg",
        "frame_00010.jpg",
    ]
    assert [item.frame_index for item in ordered] == [0, 1, 2]


async def test_infer_idempotent_when_scene_exists(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id)
    await _add_item(db_session, ds.id, f"{ds.name}/lidar/000001.pcd")

    first = await _backfill(db_session, ds.id, mode="single")
    assert first.created_scenes == 1
    second = await _backfill(db_session, ds.id, mode="single")
    assert second.created_scenes == 0
    assert any("idempotent" in note for note in second.notes)


async def test_backfill_skips_existing_scene(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id)
    await _add_item(db_session, ds.id, f"{ds.name}/lidar/000001.pcd")
    await AssetsSpatialGateway(db_session).create_scene(
        CreateSceneRequest(
            operation_id=uuid.uuid4(),
            dataset_id=ds.id,
            name="manual",
            source_format=None,
            source_metadata={},
            created_by=user.id,
        ),
        same_session_native=True,
    )

    result = await _backfill(db_session, ds.id, mode="single")
    assert result.created_scenes == 0


async def test_infer_dry_run_no_writes(db_session, super_admin):
    user, _ = super_admin
    ds = await _make_dataset(db_session, user.id, name="dry")
    item = await _add_item(db_session, ds.id, "dry/lidar/000001.pcd")

    result = await _backfill(db_session, ds.id, mode="single", dry_run=True)
    assert result.dry_run is True
    assert result.created_scenes == 1

    facts = await _facts(db_session, ds.id)
    unchanged = next(row for row in facts.items if row.id == item.id)
    assert unchanged.scene_id is None
    assert unchanged.frame_index is None
