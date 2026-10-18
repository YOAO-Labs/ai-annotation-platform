from __future__ import annotations

import uuid

import pytest

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from aap_assets_catalog.axis_convention import R_NORM
from app.services.task_dataset_link import link_items
from tests.factory import create_project


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _calib_for(convention: str) -> dict:
    m = R_NORM[convention]
    return {
        "extrinsic": [
            1,
            0,
            0,
            0,
            0,
            1,
            0,
            0,
            m[0],
            m[1],
            m[2],
            0,
            0,
            0,
            0,
            1,
        ],
        "intrinsic": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    }


async def _seed_dataset(db, owner_id, *, with_front: bool = True):
    ds = Dataset(
        display_id=f"DS-SNIFF-{uuid.uuid4().hex[:6]}",
        name="sniff-scene",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()

    front_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene/camera/front/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("sustechpoints_demo")}
        if with_front
        else {},
    )
    side_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene/camera/left/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("apollo")},
    )
    db.add_all([front_item, side_item])
    await db.flush()
    return ds, front_item, side_item


async def test_sniff_axis_convention_prefers_front_task_link(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    ds, front_item, side_item = await _seed_dataset(db_session, user.id)
    project = await create_project(
        db_session,
        owner_id=user.id,
        type_key="lidar",
        type_label="点云检测",
    )
    project.data_type = "lidar"
    task = Task(
        project_id=project.id,
        dataset_item_id=None,
        display_id=f"T-SNIFF-{uuid.uuid4().hex[:6]}",
        file_name="000001.pcd",
        file_path="scene/lidar/000001.pcd",
        file_type="point_cloud",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    await link_items(
        db_session,
        task.id,
        [
            (side_item.id, "camera_left", "left"),
            (front_item.id, "camera_front", "front"),
        ],
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["best"] == "sustechpoints_demo"
    assert body["score"] == pytest.approx(1.0)
    assert body["source"] == "task_link"
    assert body["camera_role"] == "camera_front"
    assert body["camera_item_id"] == str(front_item.id)


async def test_sniff_axis_convention_falls_back_to_dataset_item_with_discount(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    ds, _, side_item = await _seed_dataset(db_session, user.id, with_front=False)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["best"] == "apollo"
    assert body["score"] == pytest.approx(0.75)
    assert body["source"] == "dataset_item"
    assert body["camera_item_id"] == str(side_item.id)


async def test_sniff_axis_convention_skips_non_image_items(
    db_session,
    httpx_client,
    super_admin,
):
    # 回归: 点云 item(file_type=point_cloud)即便 metadata 带 lidar→ego 外参,也不应被
    # 当相机喂给 sniff 污染推断(fallback 只看 file_type=="image")。这里点云项路径含 front
    # (若不过滤会被排到最前), 外参指向 sustechpoints_demo; image 项指向 apollo。
    user, token = super_admin
    ds = Dataset(
        display_id=f"DS-SKIP-{uuid.uuid4().hex[:6]}",
        name="skip-non-image",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()
    db_session.add(
        DatasetItem(
            dataset_id=ds.id,
            file_name="000001.pcd",
            file_path="scene/lidar/front/000001.pcd",
            file_type="point_cloud",
            metadata_={"calibration": _calib_for("sustechpoints_demo")},
        )
    )
    image_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene/camera/left/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("apollo")},
    )
    db_session.add(image_item)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 只看到 image item → apollo; 若回归(未过滤 file_type)则会变成 sustechpoints_demo。
    assert body["best"] == "apollo"
    assert body["source"] == "dataset_item"
    assert body["camera_item_id"] == str(image_item.id)


async def test_sniff_axis_convention_picks_canonical_front_not_front_side(
    db_session,
    httpx_client,
    super_admin,
):
    """v0.14.2 回归:nuScenes 式多相机装置,CAM_FRONT_LEFT/RIGHT 也含 "front",
    旧实现会把它们当正前并按 created_at 选中(实测 CAM_FRONT_RIGHT→iso_8855 误判)。
    新实现只认 canonical 正前(不含 left/right/back/rear),故无论建序都锁定 CAM_FRONT。

    这里故意让 CAM_FRONT_RIGHT 先建(created_at 更早),CAM_FRONT 后建。
    CAM_FRONT 标定指向 apollo(真值),CAM_FRONT_RIGHT 指向 iso_8855(误导)。
    """
    user, token = super_admin
    ds = Dataset(
        display_id=f"DS-NUSC-{uuid.uuid4().hex[:6]}",
        name="nusc-multicam",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()

    # 先建侧前相机(更早 created_at)——旧实现会优先取它而判错。
    front_right = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="nusc/scene-0061/camera/CAM_FRONT_RIGHT/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("iso_8855")},
    )
    db_session.add(front_right)
    await db_session.flush()

    front = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="nusc/scene-0061/camera/CAM_FRONT/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("apollo")},
    )
    back_left = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="nusc/scene-0061/camera/CAM_BACK_LEFT/000001.jpg",
        file_type="image",
        metadata_={"calibration": _calib_for("sustechpoints_demo")},
    )
    db_session.add_all([front, back_left])
    await db_session.flush()

    project = await create_project(
        db_session, owner_id=user.id, type_key="lidar", type_label="点云检测"
    )
    project.data_type = "lidar"
    task = Task(
        project_id=project.id,
        dataset_item_id=None,
        display_id=f"T-NUSC-{uuid.uuid4().hex[:6]}",
        file_name="000001.pcd",
        file_path="nusc/scene-0061/lidar/000001.pcd",
        file_type="point_cloud",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    await link_items(
        db_session,
        task.id,
        [
            (front_right.id, "camera_CAM_FRONT_RIGHT", "CAM_FRONT_RIGHT"),
            (back_left.id, "camera_CAM_BACK_LEFT", "CAM_BACK_LEFT"),
            (front.id, "camera_CAM_FRONT", "CAM_FRONT"),
        ],
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["best"] == "apollo", body
    assert body["score"] == pytest.approx(1.0)
    assert body["camera_role"] == "camera_CAM_FRONT"
    assert body["camera_item_id"] == str(front.id)
    assert body["agreement"] == pytest.approx(1 / 3)
    assert body["per_camera"] == [
        {"camera_role": "camera_CAM_FRONT", "best": "apollo", "score": 1.0},
        {
            "camera_role": "camera_CAM_BACK_LEFT",
            "best": "sustechpoints_demo",
            "score": 1.0,
        },
        {
            "camera_role": "camera_CAM_FRONT_RIGHT",
            "best": "iso_8855",
            "score": 1.0,
        },
    ]


async def test_sniff_axis_convention_votes_when_no_canonical_front(
    db_session,
    httpx_client,
    super_admin,
):
    """无 canonical 正前相机时跨相机投票取众数,结果与建序无关。

    两个 apollo + 一个 iso_8855(均侧 / 后)→ 投票 apollo 胜,score 打 0.75 折。
    """
    user, token = super_admin
    ds = Dataset(
        display_id=f"DS-VOTE-{uuid.uuid4().hex[:6]}",
        name="vote-noscene",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()
    db_session.add_all(
        [
            DatasetItem(
                dataset_id=ds.id,
                file_name="000001.jpg",
                file_path="v/camera/CAM_BACK_RIGHT/000001.jpg",
                file_type="image",
                metadata_={"calibration": _calib_for("iso_8855")},
            ),
            DatasetItem(
                dataset_id=ds.id,
                file_name="000001.jpg",
                file_path="v/camera/CAM_BACK_LEFT/000001.jpg",
                file_type="image",
                metadata_={"calibration": _calib_for("apollo")},
            ),
            DatasetItem(
                dataset_id=ds.id,
                file_name="000001.jpg",
                file_path="v/camera/CAM_BACK/000001.jpg",
                file_type="image",
                metadata_={"calibration": _calib_for("apollo")},
            ),
        ]
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["best"] == "apollo", body
    assert body["score"] == pytest.approx(0.75)
    assert body["source"] == "dataset_item"
    assert body["agreement"] == pytest.approx(2 / 3)
    assert len(body["per_camera"]) == 3


async def test_sniff_axis_convention_returns_null_without_calibration(
    db_session,
    httpx_client,
    super_admin,
):
    user, token = super_admin
    ds = Dataset(
        display_id=f"DS-NULL-{uuid.uuid4().hex[:6]}",
        name="no-calib",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()
    db_session.add(
        DatasetItem(
            dataset_id=ds.id,
            file_name="000001.jpg",
            file_path="scene/camera/front/000001.jpg",
            file_type="image",
            metadata_={},
        )
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds.id}/sniff-axis-convention",
        headers=_bearer(token),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "best": None,
        "score": None,
        "candidates": [],
        "source": None,
        "camera_role": None,
        "camera_item_id": None,
        "per_camera": None,
        "agreement": None,
    }
