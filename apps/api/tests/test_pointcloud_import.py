"""v0.13.1 · 点云 scene 建任务 + 标定写入 service 测试。

不真连 MinIO：attach_calibration 通过 monkeypatch storage_service.client.get_object
返回伪 body。其余直接构造 ORM 行 + db_session fixture。
"""

from __future__ import annotations

import io
import hashlib
import json
import uuid

import pytest

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from aap_assets_source.frame_layout import PreparedDatasetItem, group_frames
from app.services import pointcloud_import
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services.storage import storage_service
from app.services.task_dataset_link import get_linked_items
from tests.factory import create_project


pytestmark = pytest.mark.asyncio


async def _seed_dataset(db_session, owner_id: uuid.UUID) -> Dataset:
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-PC-{uuid.uuid4().hex[:6]}",
        name=f"pc-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db_session.add(ds)
    await db_session.flush()
    return ds


async def _seed_lidar_project(
    db_session, owner_id: uuid.UUID, *, dataset_id: uuid.UUID
):
    project = await create_project(
        db_session, owner_id=owner_id, type_key="lidar", type_label="点云检测"
    )
    project.data_type = "lidar"
    await db_session.flush()
    db_session.add(ProjectDataset(dataset_id=dataset_id, project_id=project.id))
    await db_session.flush()
    return project


def _fact(item: DatasetItem) -> PreparedDatasetItem:
    return PreparedDatasetItem(
        id=item.id,
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item.file_type,
        scene_id=item.scene_id,
    )


def _add_item(db_session, dataset_id, file_path, file_type):
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        file_type=file_type,
    )
    db_session.add(item)
    return item


async def _seed_scene(db_session, ds, frames, cameras):
    """构造 frames × cameras 的 scene：每帧 1 lidar + 每相机 1 image。

    返回 {frame_id: {"lidar": item, "cameras": {cam: item}}}（用于断言）。
    """
    name = ds.name
    out: dict = {}
    for frame in frames:
        lidar = _add_item(db_session, ds.id, f"{name}/lidar/{frame}.pcd", "point_cloud")
        cams = {}
        for cam in cameras:
            cams[cam] = _add_item(
                db_session, ds.id, f"{name}/camera/{cam}/{frame}.jpg", "image"
            )
        out[frame] = {"lidar": lidar, "cameras": cams}
    await db_session.flush()
    return out


async def test_link_retry_resumes_after_calibration_commit(test_engine, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.application import dataset_task_creation
    from tests.test_task_creation_handoff import _cleanup, _seed

    fixture = await _seed(test_engine, item_count=1)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    operation_ids = []
    try:
        async with test_engine.begin() as owner:
            for sql in (
                "DELETE FROM public.project_datasets WHERE dataset_id = :dataset_id",
                "UPDATE public.datasets SET data_type = 'point_cloud' WHERE id = :dataset_id",
                "UPDATE public.projects SET data_type = 'lidar' WHERE id = :project_id",
                "UPDATE public.dataset_items SET file_type = 'point_cloud', "
                "file_name = '000001.pcd', file_path = 'recovery/lidar/000001.pcd' "
                "WHERE dataset_id = :dataset_id",
            ):
                await owner.execute(text(sql), fixture)

        original = dataset_task_creation.prepare_pointcloud_frames

        async def unavailable(*args, **kwargs):
            raise OSError("temporary preparation failure")

        monkeypatch.setattr(
            dataset_task_creation, "prepare_pointcloud_frames", unavailable
        )
        async with maker() as first:
            with pytest.raises(OSError, match="temporary preparation"):
                await DatasetTaskCreationWorkflow(first).link_project(
                    fixture["dataset_id"],
                    fixture["project_id"],
                    actor_id=fixture["actor_id"],
                )
            await first.rollback()
        async with test_engine.connect() as observer:
            operation_id, state = (
                await observer.execute(
                    text(
                        "SELECT id, state FROM public.task_creation_operations WHERE dataset_id = :dataset_id"
                    ),
                    fixture,
                )
            ).one()
            operation_ids.append(operation_id)
            assert state == "accepted"

        monkeypatch.setattr(
            dataset_task_creation, "prepare_pointcloud_frames", original
        )
        async with maker() as retry:
            result = await DatasetTaskCreationWorkflow(retry).link_project(
                fixture["dataset_id"],
                fixture["project_id"],
                actor_id=fixture["actor_id"],
            )
            assert result.link_id == operation_id
            assert result.created_tasks == 1 and result.async_job_id is None
            await retry.commit()
            again = await DatasetTaskCreationWorkflow(retry).link_project(
                fixture["dataset_id"],
                fixture["project_id"],
                actor_id=fixture["actor_id"],
            )
            assert again.created_tasks == 0
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.task_creation_operations WHERE dataset_id = :dataset_id"
                    ),
                    fixture,
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture, operation_ids)


async def test_group_frames_accepts_role_alias_directories():
    dataset_id = uuid.uuid4()
    lidar = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="000001.pcd",
        file_path="xtreme/lidar_point_cloud_0/000001.pcd",
        file_type="point_cloud",
    )
    cam = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="000001.jpg",
        file_path="xtreme/camera_image_0/000001.jpg",
        file_type="image",
    )
    calib = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="camera_image_0.json",
        file_path="xtreme/calibration/camera_image_0.json",
        file_type="other",
    )

    frames, calib_items = group_frames([_fact(lidar), _fact(cam), _fact(calib)])

    assert frames["000001"].lidar.id == lidar.id
    assert dict(frames["000001"].cameras)["camera_image_0"].id == cam.id
    assert calib_items["camera_image_0"].id == calib.id


async def test_group_frames_channel_less_camera_uses_default_channel():
    """相机图像直接放在角色目录下(camera/000.jpg、<scene>/camera/000.jpg)而无 channel
    子目录时,channel 名应回退到 "default",不再取角色目录名 "camera" 自身
    (否则 link role 会变成畸形的 camera_camera)。"""
    dataset_id = uuid.uuid4()
    cam_flat = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="000.jpg",
        file_path="camera/000.jpg",
        file_type="image",
    )
    cam_under_scene = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="001.jpg",
        file_path="scene_a/camera/001.jpg",
        file_type="image",
    )

    frames, _ = group_frames([_fact(cam_flat), _fact(cam_under_scene)])

    assert dict(frames["000"].cameras)["default"].id == cam_flat.id
    assert dict(frames["001"].cameras)["default"].id == cam_under_scene.id
    assert "camera" not in dict(frames["000"].cameras)


async def test_build_tasks_groups_frames_and_links(db_session, super_admin):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    project = await _seed_lidar_project(db_session, user.id, dataset_id=ds.id)
    seeded = await _seed_scene(db_session, ds, ["000970", "000971"], ["front", "left"])

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert result == {"created": 2, "total": 2}

    from sqlalchemy import select
    from app.db.models import Task

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 2

    by_lidar = {t.dataset_item_id: t for t in tasks}
    for frame_id, frame in seeded.items():
        lidar_id = frame["lidar"].id
        assert lidar_id in by_lidar
        task = by_lidar[lidar_id]
        assert task.file_type == "point_cloud"
        assert task.file_name == frame["lidar"].file_name
        assert task.file_path == frame["lidar"].file_path

        links = await get_linked_items(db_session, task.id)
        assert len(links) == 3
        by_role = {ln.role: ln for ln in links}
        assert by_role["primary_lidar"].dataset_item_id == lidar_id
        assert by_role["primary_lidar"].sensor_name is None
        assert by_role["camera_front"].dataset_item_id == frame["cameras"]["front"].id
        assert by_role["camera_front"].sensor_name == "front"
        assert by_role["camera_left"].dataset_item_id == frame["cameras"]["left"].id
        assert by_role["camera_left"].sensor_name == "left"


async def test_build_tasks_idempotent(db_session, super_admin):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    project = await _seed_lidar_project(db_session, user.id, dataset_id=ds.id)
    await _seed_scene(db_session, ds, ["000970", "000971"], ["front"])

    workflow = DatasetTaskCreationWorkflow(db_session)
    first = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert first["created"] == 2

    second = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert second == {"created": 0, "total": 0}


async def test_lidar_replay_commits_calibration_without_new_tasks(
    db_session, super_admin, monkeypatch
):
    from sqlalchemy import select

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    project = await _seed_lidar_project(db_session, user.id, dataset_id=ds.id)
    seeded = await _seed_scene(db_session, ds, ["000970"], ["front"])
    _add_item(db_session, ds.id, f"{ds.name}/calib/camera/front.json", "other")
    await db_session.flush()
    cam_item_id = seeded["000970"]["cameras"]["front"].id

    first_calibration = {
        "extrinsic": list(range(16)),
        "intrinsic": list(range(9)),
    }
    updated_calibration = {
        "extrinsic": [value + 1 for value in range(16)],
        "intrinsic": [value + 1 for value in range(9)],
    }
    monkeypatch.setattr(
        pointcloud_import, "_read_calibration", lambda _key: first_calibration
    )
    workflow = DatasetTaskCreationWorkflow(db_session)
    first = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert first == {"created": 1, "total": 1}

    monkeypatch.setattr(
        pointcloud_import, "_read_calibration", lambda _key: updated_calibration
    )
    replay = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert replay == {"created": 0, "total": 0}

    await db_session.rollback()
    persisted = await db_session.scalar(
        select(DatasetItem.metadata_).where(DatasetItem.id == cam_item_id)
    )
    assert persisted["calibration"] == updated_calibration


async def test_build_tasks_missing_camera_tolerated(db_session, super_admin):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    project = await _seed_lidar_project(db_session, user.id, dataset_id=ds.id)

    # 帧 000970 完整 (front+left)，帧 000971 只有 lidar+front。
    name = ds.name
    _add_item(db_session, ds.id, f"{name}/lidar/000970.pcd", "point_cloud")
    _add_item(db_session, ds.id, f"{name}/camera/front/000970.jpg", "image")
    _add_item(db_session, ds.id, f"{name}/camera/left/000970.jpg", "image")
    lidar1 = _add_item(db_session, ds.id, f"{name}/lidar/000971.pcd", "point_cloud")
    _add_item(db_session, ds.id, f"{name}/camera/front/000971.jpg", "image")
    await db_session.flush()

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    assert result == {"created": 2, "total": 2}

    from sqlalchemy import select
    from app.db.models import Task

    task1 = (
        await db_session.execute(select(Task).where(Task.dataset_item_id == lidar1.id))
    ).scalar_one()
    links = await get_linked_items(db_session, task1.id)
    # 缺 left：只 link lidar + front 两条，不报错。
    roles = {ln.role for ln in links}
    assert roles == {"primary_lidar", "camera_front"}


async def test_build_tasks_multi_scene_same_frame_stem(db_session, super_admin):
    """v0.14.2 回归：upload-zip 多 scene 同号帧 stem 不再互相覆盖。

    scene_a/lidar/000.pcd 与 scene_b/lidar/000.pcd 入库 stem 都是 000。修复前
    build_tasks 在全 dataset 跑 group_frames，两 scene 的 000 帧互相 setdefault
    覆盖 → 漏建 task、跨 scene 串相机。分桶后每 scene 独立分组，task 数 == 总帧数。
    """
    from sqlalchemy import select
    from app.db.models import Task

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    project = await _seed_lidar_project(db_session, user.id, dataset_id=ds.id)

    # 两个 scene，各自 2 帧，帧 stem 跨 scene 重复（000 / 001）。
    scenes: dict[str, dict] = {}
    for sidx, sname in enumerate(["scene_a", "scene_b"]):
        scene = Scene(
            id=uuid.uuid4(),
            display_id=f"S-{uuid.uuid4().hex[:6]}",
            dataset_id=ds.id,
            name=sname,
        )
        db_session.add(scene)
        await db_session.flush()
        frames: dict[str, dict] = {}
        for frame in ["000", "001"]:
            lidar = _add_item(
                db_session, ds.id, f"{ds.name}/{sname}/lidar/{frame}.pcd", "point_cloud"
            )
            lidar.scene_id = scene.id
            cam = _add_item(
                db_session,
                ds.id,
                f"{ds.name}/{sname}/camera/front/{frame}.jpg",
                "image",
            )
            cam.scene_id = scene.id
            frames[frame] = {"lidar": lidar, "cameras": {"front": cam}}
        scenes[sname] = {"scene": scene, "frames": frames}
    await db_session.flush()

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
    )
    # 2 scene × 2 帧 = 4 个 task（修复前会因同号帧覆盖只建 2 个）。
    assert result == {"created": 4, "total": 4}

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 4

    # 每个 lidar item 都恰好对应一个 task，且 camera link 指向同 scene 的相机（不串）。
    by_lidar = {t.dataset_item_id: t for t in tasks}
    for sname, sdata in scenes.items():
        for frame_id, frame in sdata["frames"].items():
            lidar_id = frame["lidar"].id
            assert lidar_id in by_lidar, f"{sname}/{frame_id} 漏建 task"
            task = by_lidar[lidar_id]
            links = await get_linked_items(db_session, task.id)
            by_role = {ln.role: ln for ln in links}
            assert (
                by_role["camera_front"].dataset_item_id == frame["cameras"]["front"].id
            )


def _patch_get_object(monkeypatch, payload: dict):
    body = json.dumps(payload).encode()
    etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'

    def fake_get_object(*, Bucket, Key, IfMatch=None):  # noqa: N803 (boto3 kwarg names)
        return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": etag}

    def fake_head_object(*, Bucket, Key, IfMatch=None):  # noqa: N803
        return {"ContentLength": len(body), "ETag": etag}

    monkeypatch.setattr(storage_service.client, "get_object", fake_get_object)
    monkeypatch.setattr(storage_service.client, "head_object", fake_head_object)
    monkeypatch.setattr(
        storage_service.spatial_source_client, "get_object", fake_get_object
    )
    monkeypatch.setattr(
        storage_service.spatial_source_client, "head_object", fake_head_object
    )


async def test_attach_calibration_writes_metadata(db_session, super_admin, monkeypatch):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    seeded = await _seed_scene(db_session, ds, ["000970", "000971"], ["front"])
    _add_item(db_session, ds.id, f"{ds.name}/calib/camera/front.json", "other")
    await db_session.flush()

    calib = {
        "extrinsic": list(range(16)),
        "intrinsic": list(range(9)),
        # rect 是 KITTI 4x4 矫正矩阵（长度 16）。v0.13.2 起 attach_calibration 经
        # SensorCalibration 全字段校验，非法长度的 rect 会让整份标定被拒（warning 跳过）。
        "rect": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }
    _patch_get_object(monkeypatch, calib)

    written = await pointcloud_import.attach_calibration(db_session, dataset_id=ds.id)
    # front 相机 2 帧 → 写 2 个 DatasetItem。
    assert written == 2
    for frame in seeded.values():
        cam_item = frame["cameras"]["front"]
        assert cam_item.metadata_["calibration"] == calib


async def test_attach_calibration_invalid_skipped(db_session, super_admin, monkeypatch):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id)
    seeded = await _seed_scene(db_session, ds, ["000970"], ["front"])
    _add_item(db_session, ds.id, f"{ds.name}/calib/camera/front.json", "other")
    await db_session.flush()

    # extrinsic 长度非 16 → 跳过，不写不抛。
    _patch_get_object(
        monkeypatch, {"extrinsic": [1, 2, 3], "intrinsic": list(range(9))}
    )

    written = await pointcloud_import.attach_calibration(db_session, dataset_id=ds.id)
    assert written == 0
    cam_item = seeded["000970"]["cameras"]["front"]
    assert "calibration" not in (cam_item.metadata_ or {})
