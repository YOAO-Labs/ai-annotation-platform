"""Point-cloud manifests expose authenticated identities and spatial facts."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.scene_pose import SceneFramePose
from app.db.models.task import Task
from app.services.task_dataset_link import link_items
from tests.factory import create_project


def _calib(extr_seed: float, intr_seed: float) -> dict:
    return {
        "extrinsic": [extr_seed + i for i in range(16)],
        "intrinsic": [intr_seed + i for i in range(9)],
    }


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
    db_session, httpx_client, super_admin
):
    from app.services.task_dataset_link import get_linked_items

    user, token = super_admin
    task, front_calib, rear_calib = await _seed_lidar_scene(db_session, user.id)
    links = {link.role: link for link in await get_linked_items(db_session, task.id)}

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
    assert body["axis_convention"] is None

    cameras = body["cameras"]
    assert [c["name"] for c in cameras] == ["front", "rear"]
    front, rear = cameras
    assert front["role"] == "camera_front"
    assert front["image_url"] is None
    assert front["dataset_item_id"] == str(links["camera_front"].dataset_item_id)
    assert front["link_id"] == str(links["camera_front"].id)
    assert front["original_identity_path"] == (
        f"/api/v1/files/tasks/{task.id}/links/{links['camera_front'].id}/original-identity"
    )
    assert front["calibration"]["extrinsic"] == front_calib["extrinsic"]
    assert front["calibration"]["intrinsic"] == front_calib["intrinsic"]
    assert rear["dataset_item_id"] == str(links["camera_rear"].dataset_item_id)
    assert rear["link_id"] == str(links["camera_rear"].id)
    assert rear["image_url"] is None
    assert rear["original_identity_path"] == (
        f"/api/v1/files/tasks/{task.id}/links/{links['camera_rear'].id}/original-identity"
    )
    assert rear["calibration"]["extrinsic"] == rear_calib["extrinsic"]
    assert rear["calibration"]["intrinsic"] == rear_calib["intrinsic"]


async def test_point_cloud_manifest_exposes_dataset_axis_convention(
    db_session, httpx_client, super_admin
):
    """主点云所在 dataset.metadata.axis_convention 透到 manifest 字段。"""
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


async def test_point_cloud_manifest_keeps_cameras_when_calibration_is_unavailable(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, _, _ = await _seed_lidar_scene(db_session, user.id)
    from app.services.task_dataset_link import get_linked_items

    links = await get_linked_items(db_session, task.id)
    linked_items = {
        item.id: item
        for item in (
            await db_session.scalars(
                select(DatasetItem).where(
                    DatasetItem.id.in_([link.dataset_item_id for link in links])
                )
            )
        ).all()
    }
    front = next(
        linked_items[link.dataset_item_id]
        for link in links
        if link.role == "camera_front"
    )
    rear = next(
        linked_items[link.dataset_item_id]
        for link in links
        if link.role == "camera_rear"
    )
    front.metadata_ = {"other": "no calibration"}
    rear.metadata_ = {"calibration": {"extrinsic": [1], "intrinsic": [1]}}
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    cameras = response.json()["cameras"]
    assert [camera["name"] for camera in cameras] == ["front", "rear"]
    for camera in cameras:
        assert camera["image_url"] is None
        assert camera["original_identity_path"] == (
            f"/api/v1/files/tasks/{task.id}/links/{camera['link_id']}/original-identity"
        )
        assert camera["calibration"] is None
        assert camera["calibration_revision"] is None
        assert camera["calibration_digest"] is None


@pytest.mark.parametrize("file_path", ["pointcloud/fallback.pcd", ""])
async def test_point_cloud_manifest_without_links_requires_original_identity(
    file_path, db_session, httpx_client, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="lidar")
    project.data_type = "lidar"
    task = Task(
        project_id=project.id,
        display_id=f"T-PC-FB-{uuid.uuid4().hex[:8]}",
        file_name="fallback.pcd",
        file_path=file_path,
        file_type="point_cloud",
        status="pending",
    )
    db_session.add(task)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["point_cloud_url"] is None
    assert body["original_identity_path"] == (
        f"/api/v1/files/tasks/{task.id}/original-identity"
    )
    assert body["cameras"] == []
    identity = await httpx_client.get(
        body["original_identity_path"], headers={"Authorization": f"Bearer {token}"}
    )
    assert identity.status_code == 409, identity.text
    assert identity.json()["detail"]["code"] == "no_readable_original"


async def test_point_cloud_manifest_rejects_non_lidar_task(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="image-det")
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


async def test_point_cloud_manifest_pose_facts_detach_nested_source_metadata(
    db_session, super_admin
):
    from app.services.assets_point_cloud_manifest import (
        SqlAlchemyPointCloudManifestAssetsReader,
    )

    user, _ = super_admin
    task, _, _ = await _seed_lidar_scene(db_session, user.id)
    primary = await db_session.get(DatasetItem, task.dataset_item_id)
    scene = Scene(
        display_id=f"S-PC-{uuid.uuid4().hex[:8]}",
        dataset_id=primary.dataset_id,
        name="point-cloud manifest pose",
        created_by=user.id,
    )
    db_session.add(scene)
    await db_session.flush()
    primary.scene_id, primary.frame_index = scene.id, 1
    pose = SceneFramePose(
        scene_id=scene.id,
        frame_index=1,
        ego_translation=[2.0, 0.0, 0.0],
        ego_rotation=[1.0, 0.0, 0.0, 0.0],
        source_metadata={"source": {"tokens": ["original"]}},
    )
    db_session.add(pose)
    await db_session.flush()

    reader = SqlAlchemyPointCloudManifestAssetsReader(db_session)
    await reader.read_items((primary.id,))
    facts = await reader.read_primary_context(primary.id)
    assert facts is not None and facts.ego_pose is not None
    facts.ego_pose.source_metadata["source"]["tokens"].append("consumer change")
    facts.ego_pose.ego_translation[0] = 100.0

    assert pose.source_metadata == {"source": {"tokens": ["original"]}}
    assert pose.ego_translation == [2.0, 0.0, 0.0]
