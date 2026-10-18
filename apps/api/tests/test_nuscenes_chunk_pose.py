"""nuScenes export checks exact, bounded pose-pair requests per task chunk.

The test covers primary-item resolution and verifies pair scope at the public
Assets spatial gateway boundary instead of depending on its former ORM query.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import uuid
import zipfile

import pytest

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.scene_pose import SceneFramePose
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.exporting.packaging import (
    ExportService,
    _build_lidar_export_zip,
    _load_lidar_link_items,
    _primary_lidar_item,
)
from app.services.task_dataset_link import link_items
from tests.factory import create_project

_SOURCE_SCENE = {
    "nuscenes_export": {
        "scene": {
            "token": "source-scene",
            "name": "scene-0001",
            "description": "test",
            "log_token": "source-log",
            "nbr_samples": 2,
            "first_sample_token": "source-sample-0",
            "last_sample_token": "source-sample-1",
        },
        "log": {
            "token": "source-log",
            "logfile": "logfile",
            "vehicle": "vehicle",
            "date_captured": "2026-08-27",
            "location": "test-track",
        },
        "map": {
            "token": "source-map",
            "log_tokens": ["source-log"],
            "category": "semantic_prior",
            "filename": "maps/source-map.png",
        },
    }
}


def _nuscenes_item_metadata(frame_index: int) -> dict:
    source_sample = {
        "token": f"source-sample-{frame_index}",
        "timestamp": 1_000_000 + frame_index * 100_000,
        "scene_token": "source-scene",
        "prev": "source-sample-0" if frame_index else "",
        "next": "source-sample-1" if frame_index == 0 else "",
    }
    ego_pose = {
        "token": f"source-ego-{frame_index}",
        "translation": [float(frame_index), 0, 0],
        "rotation": [1, 0, 0, 0],
        "timestamp": 1_000_010 + frame_index * 100_000,
    }
    calibrated = {
        "token": "source-calibrated",
        "sensor_token": "source-sensor",
        "translation": [0, 0, 0],
        "rotation": [1, 0, 0, 0],
        "camera_intrinsic": [],
    }
    return {
        "point_count": 4,
        "nuscenes_export": {
            "sample_data": {
                "token": f"source-sd-{frame_index}",
                "sample_token": source_sample["token"],
                "ego_pose_token": ego_pose["token"],
                "calibrated_sensor_token": calibrated["token"],
                "filename": f"samples/LIDAR_TOP/{frame_index}.pcd.bin",
                "fileformat": "bin",
                "width": 0,
                "height": 0,
                "timestamp": ego_pose["timestamp"],
                "is_key_frame": True,
            },
            "calibrated_sensor": calibrated,
            "sensor": {
                "token": "source-sensor",
                "channel": "LIDAR_TOP",
                "modality": "lidar",
            },
            "ego_pose": ego_pose,
            "source_storage_key": f"source/{frame_index}.pcd.bin",
            "source_file_size": 80,  # nuScenes bin 语义: 每点 20 字节 × 4 点
            "sample": source_sample,
            "source_sha256": hashlib.sha256(
                f"frame-{frame_index}".encode()
            ).hexdigest(),
        },
    }


def _pcd_payload(point_count: int = 4) -> bytes:
    """构造 4 点 binary PCD(x/y/z float32),供打包器真实解析。"""

    header = (
        "# .PCD v0.7 - Point Cloud Data file format\n"
        "VERSION 0.7\n"
        "FIELDS x y z\n"
        "SIZE 4 4 4\n"
        "TYPE F F F\n"
        "COUNT 1 1 1\n"
        f"WIDTH {point_count}\n"
        "HEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {point_count}\n"
        "DATA binary\n"
    ).encode("ascii")
    body = b"".join(
        struct.pack("<fff", float(i), 0.0, float(i) / 10) for i in range(point_count)
    )
    return header + body


def _sample_scene_frame_pose(scene_id: uuid.UUID, frame_index: int) -> SceneFramePose:
    return SceneFramePose(
        scene_id=scene_id,
        frame_index=frame_index,
        timestamp_us=1_000_010 + frame_index * 100_000,
        ego_translation=[float(frame_index), 0, 0],
        ego_rotation=[1, 0, 0, 0],
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


def _ann(task_id, project_id, user_id) -> Annotation:
    return Annotation(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        class_name="car",
        geometry={
            "type": "box_3d",
            "center": [10.0, 0.0, 0.0],
            "size": [4.0, 2.0, 1.5],
            "rotation": [0.0, 0.0, 0.0],
        },
        is_active=True,
        was_cancelled=False,
    )


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


_SCENE_SOURCE_WITH_MAP = {
    **_SOURCE_SCENE["nuscenes_export"],
    "map_storage_key": "pose-filter/map.png",
    "map_file_size": 32,
    "map_sha256": hashlib.sha256(b"map").hexdigest(),
}


@pytest.mark.asyncio
async def test_nuscenes_chunk_pose_query_is_pair_filtered(
    db_session, super_admin, monkeypatch
):
    """分块导出时,pose 查询只拉当前 chunk 的 (scene, frame) 对。

    chunk 只含同 scene 的 frame 0,注入的探针必须恰好收到
    ``[(scene, 0)]`` 一个过滤对;若实现退回按 scene 全量拉取,探针会收到
    scene 级 IN 条件或多余 pose 行。同时验证导出产物只包含该帧。
    """

    user, _token = super_admin
    project, ds = await _make_pointcloud_project(
        db_session, user, dataset_name="pose-filter"
    )
    scene = Scene(
        display_id=f"S-{uuid.uuid4().hex[:6]}",
        dataset_id=ds.id,
        name="scene-0001",
        source_metadata={"nuscenes_export": dict(_SCENE_SOURCE_WITH_MAP)},
    )
    db_session.add(scene)
    await db_session.flush()
    batch = TaskBatch(
        project_id=project.id,
        dataset_id=ds.id,
        display_id=f"B-{uuid.uuid4().hex[:6]}",
        name="pose filter batch",
        created_by=user.id,
    )
    db_session.add(batch)
    await db_session.flush()

    pcd_bytes = _pcd_payload(4)
    pcd_sha = hashlib.sha256(pcd_bytes).hexdigest()
    items: list[DatasetItem] = []
    tasks: list[Task] = []
    for frame_index in range(2):
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=f"{frame_index:06d}.pcd",
            file_path=f"pose-filter/{frame_index:06d}.pcd",
            file_type="point_cloud",
            file_size=len(pcd_bytes),
            content_hash=pcd_sha,
            metadata_=_nuscenes_item_metadata(frame_index),
            scene_id=scene.id,
            frame_index=frame_index,
        )
        items.append(item)
    db_session.add_all(items)
    await db_session.flush()
    for frame_index, item in enumerate(items):
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            batch_id=batch.id,
            display_id=f"T-{uuid.uuid4().hex[:6]}",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
        )
        tasks.append(task)
    db_session.add_all(tasks)
    await db_session.flush()
    for task, item in zip(tasks, items):
        await link_items(db_session, task.id, [(item.id, "primary_lidar", None)])
    db_session.add(_ann(tasks[0].id, project.id, user.id))
    for pose_frame in range(2):
        db_session.add(_sample_scene_frame_pose(scene.id, pose_frame))
    await db_session.flush()

    # chunk 之外无预加载:生产路径在 _build_lidar_export_zip 内按 chunk 加载
    svc = ExportService(db_session)

    probes: list[list[tuple[uuid.UUID, int]]] = []
    original_read_pairs = AssetsSpatialGateway.read_spatial_pose_pairs

    async def probing_read_pairs(gateway, request):
        probes.append([(pair.scene_id, pair.frame_index) for pair in request.pairs])
        return await original_read_pairs(gateway, request)

    monkeypatch.setattr(
        AssetsSpatialGateway, "read_spatial_pose_pairs", probing_read_pairs
    )
    monkeypatch.setattr(
        "app.services.exporting.packaging._read_dataset_object",
        lambda key: pcd_bytes,
    )

    async def two_chunks():
        # 与生产一致:同 scene 跨多个 chunk;每个 chunk 只带自己的 items
        yield [tasks[0]], {tasks[0].id: []}, {items[0].id: items[0]}
        yield [tasks[1]], {tasks[1].id: []}, {items[1].id: items[1]}

    fd, tmp_path = tempfile.mkstemp(prefix="aap-export-", suffix=".zip")
    os.close(fd)
    try:
        zip_path, _file_count, _size = await _build_lidar_export_zip(
            svc,
            project,
            two_chunks(),
            tmp_path=tmp_path,
            batch_id=batch.id,
            targets=["nuscenes"],
            include_attributes=True,
            format_options={},
        )
        with zipfile.ZipFile(zip_path) as zf:
            scene_rows = json.loads(zf.read("v1.0-aap/scene.json"))
            sample_rows = json.loads(zf.read("v1.0-aap/sample.json"))
            ego_rows = json.loads(zf.read("v1.0-aap/ego_pose.json"))
        assert len(scene_rows) == 1
        assert len(sample_rows) == 2
        assert len(ego_rows) == 2
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Spy at the public owner boundary: each chunk must request only its exact
    # (scene, frame) pair, with no scene-wide pose stream fetched and filtered later.
    assert probes == [
        [(scene.id, 0)],
        [(scene.id, 1)],
    ]
