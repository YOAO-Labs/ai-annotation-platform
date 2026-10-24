"""Point-cloud manifests expose lazy identities without signing original bytes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services.task_dataset_link import get_linked_items, link_items
from tests.factory import create_project


def _calib(extr_seed: float, intr_seed: float) -> dict:
    return {
        "extrinsic": [extr_seed + i for i in range(16)],
        "intrinsic": [intr_seed + i for i in range(9)],
    }


@pytest.fixture
def _patch_presign(monkeypatch):
    def _fake(key, expires_in=3600, bucket=None):
        pytest.fail("point-cloud manifests must not sign original keys")

    monkeypatch.setattr(
        "app.api.v1.tasks.video.storage_service.generate_download_url", _fake
    )


async def _seed_lidar_scene(db, owner_id):
    """建 lidar 项目 + dataset(1 lidar + 2 相机帧含 calib) + task + links。"""
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云检测"
    )
    project.data_type = "lidar"
    await db.flush()

    ds = Dataset(
        display_id=f"DS-PC-{uuid.uuid4().hex[:6]}",
        name="scene-a",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()

    lidar_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.pcd",
        file_path="scene-a/lidar/000001.pcd",
        file_type="point_cloud",
    )
    front_calib = _calib(1.0, 100.0)
    rear_calib = _calib(2.0, 200.0)
    front_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene-a/camera/front/000001.jpg",
        file_type="image",
        metadata_={"calibration": front_calib},
    )
    rear_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene-a/camera/rear/000001.jpg",
        file_type="image",
        metadata_={"calibration": rear_calib},
    )
    db.add_all([lidar_item, front_item, rear_item])
    await db.flush()

    task = Task(
        project_id=project.id,
        dataset_item_id=lidar_item.id,
        display_id=f"T-PC-{uuid.uuid4().hex[:6]}",
        file_name="000001.pcd",
        file_path="scene-a/lidar/000001.pcd",
        file_type="point_cloud",
        status="pending",
    )
    db.add(task)
    await db.flush()

    await link_items(
        db,
        task.id,
        [
            (lidar_item.id, "primary_lidar", None),
            (front_item.id, "camera_front", "front"),
            (rear_item.id, "camera_rear", "rear"),
        ],
    )
    return task, front_calib, rear_calib


async def test_point_cloud_manifest_returns_scene(
    db_session, httpx_client, super_admin, _patch_presign
):
    user, token = super_admin
    task, front_calib, rear_calib = await _seed_lidar_scene(db_session, user.id)

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["task_id"] == str(task.id)
    assert body["point_cloud_format"] == "pcd"
    assert body["expires_in"] == 0
    assert body["point_cloud_url"] is None
    assert body["original_identity_path"] == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
    # v0.13.11 · dataset 无 axis_convention 时 manifest 字段为 None,前端按 iso_8855 处理。
    assert body["axis_convention"] is None

    cameras = body["cameras"]
    assert [c["name"] for c in cameras] == ["front", "rear"]  # 按 name 排序
    front, rear = cameras
    assert front["role"] == "camera_front"
    links = {link.role: link for link in await get_linked_items(db_session, task.id)}
    for camera in cameras:
        link = links[camera["role"]]
        assert camera["link_id"] == str(link.id)
        assert camera["dataset_item_id"] == str(link.dataset_item_id)
        assert camera["image_url"] is None
        assert camera["original_identity_path"] == (
            f"/api/v1/files/tasks/{task.id}/links/{link.id}/original-identity"
        )
    assert front["calibration"]["extrinsic"] == front_calib["extrinsic"]
    assert front["calibration"]["intrinsic"] == front_calib["intrinsic"]
    assert rear["calibration"]["extrinsic"] == rear_calib["extrinsic"]
    assert rear["calibration"]["intrinsic"] == rear_calib["intrinsic"]


async def test_missing_primary_and_empty_legacy_path_keep_camera_metadata(
    db_session, httpx_client, super_admin, _patch_presign
):
    user, token = super_admin
    task, front_calib, rear_calib = await _seed_lidar_scene(db_session, user.id)
    await db_session.execute(
        delete(TaskDatasetItemLink).where(
            TaskDatasetItemLink.task_id == task.id,
            TaskDatasetItemLink.role == "primary_lidar",
        )
    )
    task.dataset_item_id = None
    task.file_path = ""
    await db_session.flush()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["point_cloud_url"] is None and body["expires_in"] == 0
    assert body["original_identity_path"] == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
    assert [camera["name"] for camera in body["cameras"]] == ["front", "rear"]
    assert [camera["calibration"]["intrinsic"] for camera in body["cameras"]] == [
        front_calib["intrinsic"],
        rear_calib["intrinsic"],
    ]
    assert all(
        camera["link_id"] and camera["image_url"] is None for camera in body["cameras"]
    )
    assert all(
        body[field] is None
        for field in ("axis_convention", "scene_id", "frame_index", "ego_pose")
    )


async def test_point_cloud_manifest_exposes_dataset_axis_convention(
    db_session, httpx_client, super_admin, _patch_presign
):
    """v0.13.11 · 主点云所在 dataset.metadata.axis_convention 透到 manifest 字段。"""
    user, token = super_admin
    task, _, _ = await _seed_lidar_scene(db_session, user.id)

    from app.db.models.dataset import Dataset, DatasetItem
    from sqlalchemy import select

    lidar_item = (
        await db_session.execute(
            select(DatasetItem).where(DatasetItem.id == task.dataset_item_id)
        )
    ).scalar_one()
    dataset = await db_session.get(Dataset, lidar_item.dataset_id)
    dataset.metadata_ = {"axis_convention": "sustechpoints_demo"}
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["axis_convention"] == "sustechpoints_demo"


async def test_lazy_manifest_keeps_scene_frame_and_pose_metadata(
    db_session, httpx_client, super_admin, _patch_presign
):
    from app.schemas.scene_pose import FramePose
    from app.services.scene import create_scene
    from app.services.scene_pose import upsert_frame_poses

    user, token = super_admin
    task, _, _ = await _seed_lidar_scene(db_session, user.id)
    primary_item = await db_session.get(DatasetItem, task.dataset_item_id)
    scene = await create_scene(
        db_session, dataset_id=primary_item.dataset_id, name="Frame sequence"
    )
    primary_item.scene_id = scene.id
    primary_item.frame_index = 7
    pose = FramePose(
        frame_index=7,
        timestamp_us=1234,
        ego_translation=[1.0, 2.0, 3.0],
        ego_rotation=[1.0, 0.0, 0.0, 0.0],
    )
    await upsert_frame_poses(db_session, scene_id=scene.id, poses=[pose])
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["point_cloud_url"] is None and body["original_identity_path"]
    assert body["scene_id"] == str(scene.id)
    assert body["scene_name"] == "Frame sequence"
    assert body["frame_index"] == 7 and body["scene_total_frames"] == 1
    assert body["ego_pose"]["ego_translation"] == pose.ego_translation
    assert body["ego_pose"]["ego_rotation"] == pose.ego_rotation
    assert body["ego_pose"]["timestamp_us"] == 1234


async def test_point_cloud_manifest_rejects_non_lidar_task(
    db_session, httpx_client, super_admin, _patch_presign
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
    # 默认 data_type == "image"
    task = Task(
        project_id=project.id,
        display_id=f"T-IMG-{uuid.uuid4().hex[:6]}",
        file_name="a.jpg",
        file_path="imgs/a.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
