"""Primary resolution and bounded pose loading from registered NuScenes inputs.

The frozen-input collector owns physical-scope pose selection. The query test
uses cross-scene/frame pairs to detect both scene-wide loading and an accidental
Cartesian product between selected scene IDs and frame indices.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.scene_pose import SceneFramePose
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.exporting import lidar_inputs
from app.services.exporting.packaging import (
    ExportService,
    _load_lidar_link_items,
    _primary_lidar_item,
)
from app.services.storage import storage_service
from app.services.task_dataset_link import link_items
from scripts.import_nuscenes_scene import import_nuscenes
from tests.factory import create_project
from tests.test_import_nuscenes_lite import (
    _FakeS3Client,
    _use_distinct_sensor_files,
    _write_fake_nuscenes,
    controlled_transport as controlled_transport,
)


async def _make_pointcloud_project(db_session, user, *, dataset_name: str):
    project = await create_project(
        db_session,
        owner_id=user.id,
        type_key="lidar",
        type_label="点云检测",
        classes=["car"],
    )
    ds = Dataset(
        display_id=f"DS-{uuid.uuid4().hex[:6]}",
        name=dataset_name,
        data_type="point_cloud",
        created_by=user.id,
        metadata_={"axis_convention": "iso_8855"},
    )
    db_session.add(ds)
    await db_session.flush()
    return project, ds


@pytest.mark.asyncio
async def test_primary_lidar_item_prefers_link_over_task_fallback(
    db_session, super_admin
):
    user, _token = super_admin
    project, ds = await _make_pointcloud_project(
        db_session, user, dataset_name="primary-item"
    )

    link_item = DatasetItem(
        dataset_id=ds.id,
        file_name="link.pcd",
        file_path="primary-item/link.pcd",
        file_type="point_cloud",
    )
    fallback_item = DatasetItem(
        dataset_id=ds.id,
        file_name="fallback.pcd",
        file_path="primary-item/fallback.pcd",
        file_type="point_cloud",
    )
    db_session.add_all([link_item, fallback_item])
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=fallback_item.id,
        display_id=f"T-{uuid.uuid4().hex[:6]}",
        file_name="fallback.pcd",
        file_path=fallback_item.file_path,
        file_type="point_cloud",
    )
    db_session.add(task)
    await db_session.flush()
    await link_items(db_session, task.id, [(link_item.id, "primary_lidar", None)])
    await db_session.flush()

    svc = ExportService(db_session)
    links_by_task = await _load_lidar_link_items(svc, [task])
    dataset_items = {link_item.id: link_item, fallback_item.id: fallback_item}

    resolved = _primary_lidar_item(task, links_by_task, dataset_items)
    assert resolved is not None and resolved.id == link_item.id

    # 无 primary_lidar link 时回退 task.dataset_item_id
    links_by_task_wo_link = await _load_lidar_link_items(svc, [])
    resolved_fallback = _primary_lidar_item(task, links_by_task_wo_link, dataset_items)
    assert resolved_fallback is not None and resolved_fallback.id == fallback_item.id

    # 两者皆缺时返回 None
    assert _primary_lidar_item(task, {}, {}) is None


@pytest.mark.asyncio
async def test_nuscenes_chunk_pose_query_is_pair_filtered(
    db_session, super_admin, monkeypatch, tmp_path
):
    user, _token = super_admin
    root = tmp_path / "registered-source"
    _write_fake_nuscenes(root, scenes=2, samples_per=2)
    _use_distinct_sensor_files(root)
    monkeypatch.setattr(storage_service, "client", _FakeS3Client())
    imported = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000", "scene-0001"],
        dataset_name="pose-filter",
        owner_id=user.id,
    )
    scenes = list(await db_session.scalars(select(Scene).order_by(Scene.name)))
    selected = {(scenes[0].id, 0), (scenes[1].id, 1)}
    batch = TaskBatch(
        project_id=imported["project_id"],
        dataset_id=imported["dataset_id"],
        display_id="BT-POSE",
        name="cross-pair scope",
        created_by=user.id,
    )
    db_session.add(batch)
    await db_session.flush()
    pairs = list(
        (
            await db_session.execute(
                select(Task, DatasetItem)
                .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                .where(Task.project_id == imported["project_id"])
            )
        ).all()
    )
    for task, item in pairs:
        if (item.scene_id, item.frame_index) in selected:
            task.batch_id = batch.id
    db_session.add(
        SceneFramePose(
            scene_id=scenes[0].id,
            frame_index=999,
            timestamp_us=999,
            ego_translation=[999, 0, 0],
            ego_rotation=[1, 0, 0, 0],
        )
    )
    await db_session.flush()

    probes = []
    original_execute = db_session.execute

    async def probing_execute(statement, *args, **kwargs):
        result = await original_execute(statement, *args, **kwargs)
        descriptions = getattr(statement, "column_descriptions", [])
        if descriptions and descriptions[0].get("type") is SceneFramePose:
            frozen = result.freeze()
            probes.append(
                {(row[0].scene_id, row[0].frame_index) for row in frozen().all()}
            )
            return frozen()
        return result

    monkeypatch.setattr(db_session, "execute", probing_execute)
    captured = await lidar_inputs.collect(
        db_session, imported["project_id"], batch.id, ["nuscenes"], {}
    )
    assert probes == [selected]
    assert len(captured.snapshot["tasks"]) == 2
    assert {
        (uuid.UUID(pose["scene_id"]), pose["frame_index"])
        for pose in captured.snapshot["poses"]
    } == selected
    assert len(captured.snapshot["scene_scope_tasks"]) == 4
