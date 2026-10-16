"""v0.13.1 · 点云 scene 端到端导入测试。

用真实夹具 third-party/SUSTechPOINTS/data/example 的帧/相机/标定结构，走完整的
dataset task-creation workflow，校验:
  - 每个 lidar 帧建一个 Task (dataset_item_id 指向该帧 lidar item, file_type=point_cloud)
  - link 含 1 primary_lidar + 该帧实际存在的各 camera_<cam>
  - 缺相机的帧 (000950) 只 link primary_lidar, 不报错
  - 各相机 DatasetItem.metadata_["calibration"] 写入且值与源 JSON 一致
不需真连 MinIO: monkeypatch storage_service.client.get_object 返回夹具 calib 字节。
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path, PurePosixPath

import pytest

from app.db.models.dataset import Dataset, DatasetItem
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services.storage import storage_service
from app.services.task_dataset_link import get_linked_items
from tests.factory import create_project, create_user

_FIXTURE = (
    Path(__file__).resolve().parents[3] / "third-party/SUSTechPOINTS/data/example"
)

pytestmark = pytest.mark.skipif(
    not (_FIXTURE / "lidar").is_dir(),
    reason="SUSTechPOINTS 夹具不可用",
)


def _scan_fixture() -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    """返回 (lidar_frames, {cam: [frames]}, {cam: calib_path})。"""
    lidar_frames = sorted(p.stem for p in (_FIXTURE / "lidar").glob("*.pcd"))
    cams: dict[str, list[str]] = {}
    for cam_dir in sorted((_FIXTURE / "camera").iterdir()):
        if cam_dir.is_dir():
            cams[cam_dir.name] = sorted(p.stem for p in cam_dir.glob("*.jpg"))
    calib = {p.stem: str(p) for p in (_FIXTURE / "calib/camera").glob("*.json")}
    return lidar_frames, cams, calib


async def _seed_scene_dataset(db, ds_name: str, created_by: uuid.UUID):
    """按夹具结构建 Dataset + DatasetItem(只存 storage key, 不传真文件)。"""
    lidar_frames, cams, calib = _scan_fixture()
    ds = Dataset(
        display_id=f"DS-PC-{uuid.uuid4().hex[:6]}",
        name=ds_name,
        data_type="point_cloud",
        created_by=created_by,
    )
    db.add(ds)
    await db.flush()

    def _add(relpath: str, file_type: str) -> DatasetItem:
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=PurePosixPath(relpath).name,
            file_path=f"{ds_name}/{relpath}",
            file_type=file_type,
        )
        db.add(item)
        return item

    for frame in lidar_frames:
        _add(f"lidar/{frame}.pcd", "point_cloud")
    for cam, frames in cams.items():
        for frame in frames:
            _add(f"camera/{cam}/{frame}.jpg", "image")
    for cam in calib:
        _add(f"calib/camera/{cam}.json", "other")
    await db.flush()
    return ds


@pytest.fixture
def _patch_calib_read(monkeypatch):
    """get_object(Key=<ds>/calib/camera/<cam>.json) 返回夹具该相机的真实字节。"""
    _, _, calib_paths = _scan_fixture()

    def _fake_get_object(*, Bucket: str, Key: str):
        cam = PurePosixPath(Key).stem
        data = Path(calib_paths[cam]).read_bytes()
        return {"Body": io.BytesIO(data)}

    monkeypatch.setattr(storage_service.client, "get_object", _fake_get_object)


async def test_scene_import_end_to_end(db_session, _patch_calib_read):
    lidar_frames, cams, calib_paths = _scan_fixture()
    owner = await create_user(db_session, "project_admin", "pc@test.local", "PC")
    project = await create_project(
        db_session, owner_id=owner.id, type_key="lidar", type_label="点云检测"
    )
    project.data_type = "lidar"  # 触发点云分流
    await db_session.flush()

    ds = await _seed_scene_dataset(db_session, "scene-a", owner.id)

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(dataset_id=ds.id, project_id=project.id)

    # 每个 lidar 帧一个 Task
    assert result == {"created": len(lidar_frames), "total": len(lidar_frames)}

    from sqlalchemy import select
    from app.db.models.task import Task

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == len(lidar_frames)
    assert all(t.file_type == "point_cloud" for t in tasks)

    # 每帧 link: 1 primary_lidar + 该帧实际存在的相机
    # v0.13.2 · attach_calibration 入库前经 SensorCalibration 归一化（剥未建模杂键、
    # exclude_none），故期望值同样归一化（夹具 left.json 含杂键 extrinsic_ok，被剥掉）。
    from app.schemas._jsonb_types import SensorCalibration

    _known = set(SensorCalibration.model_fields)
    expected_calib = {
        cam: SensorCalibration.model_validate(
            {k: v for k, v in json.loads(Path(p).read_bytes()).items() if k in _known}
        ).model_dump(exclude_none=True)
        for cam, p in calib_paths.items()
    }
    items = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == ds.id)
            )
        )
        .scalars()
        .all()
    )
    by_path = {i.file_path: i for i in items}

    for task in tasks:
        # task.dataset_item_id 指向该帧 lidar item
        lidar_item = by_path[task.file_path]
        assert task.dataset_item_id == lidar_item.id
        frame = PurePosixPath(lidar_item.file_path).stem

        links = await get_linked_items(db_session, task.id)
        roles = sorted(link.role for link in links)
        expected_cams = sorted(cam for cam, frames in cams.items() if frame in frames)
        expected_roles = sorted(
            ["primary_lidar"] + [f"camera_{c}" for c in expected_cams]
        )
        assert roles == expected_roles, f"frame {frame}"

    # 缺相机的帧 (000950) 只有 primary_lidar
    assert "000950" in lidar_frames
    lidar_950 = by_path["scene-a/lidar/000950.pcd"]
    task_950 = next(t for t in tasks if t.dataset_item_id == lidar_950.id)
    links_950 = await get_linked_items(db_session, task_950.id)
    assert [link.role for link in links_950] == ["primary_lidar"]

    # 各相机帧 DatasetItem.metadata_["calibration"] 与源（归一化后）一致
    for cam, frames in cams.items():
        for frame in frames:
            cam_item = by_path[f"scene-a/camera/{cam}/{frame}.jpg"]
            await db_session.refresh(cam_item)
            assert cam_item.metadata_["calibration"] == expected_calib[cam]
