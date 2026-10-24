"""v0.14.1 · 跨帧 propagate 复制语义 + axis_convention + 权限/边界

覆盖判据(plan §1.4 判据 1 / §3.4):
- box_3d 复制 geometry/class/attributes + 共享 track_id(源无则新分配并写回, ADR-0045)
- convention_at_create 取**目标** dataset 的 axis_convention(不是源的)
- 源已有 track_id → 复用, 不再分配新值
- 2D bbox / polygon 同样可 propagate
- 不支持的几何(video_bbox / point_mask_3d)→ 422
- 跨 project → 422; 同 task 自身 → 422
- API 端点端到端: 201 + 返回新 annotation
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.services.annotation import AnnotationService
from app.services.scene_track_domain import ensure_scene_track
from tests.factory import create_project


async def _seed_scene(
    db, *, owner_id, n: int = 3, axis_convention: str | None = None, data_type="lidar"
):
    """lidar 项目 + dataset(可带 axis_convention)+ scene + n 帧 task。"""
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云"
    )
    project.data_type = "lidar"

    ds = Dataset(
        display_id=f"DS-PR-{uuid.uuid4().hex[:6]}",
        name=f"sc-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
        metadata_={"axis_convention": axis_convention} if axis_convention else {},
    )
    db.add(ds)
    await db.flush()

    scene = Scene(
        display_id=f"SCN-{uuid.uuid4().hex[:6]}",
        dataset_id=ds.id,
        name=f"s-{uuid.uuid4().hex[:6]}",
    )
    db.add(scene)
    await db.flush()

    tasks = []
    for i in range(n):
        stem = f"{i:06d}"
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=f"{stem}.pcd",
            file_path=f"{ds.name}/lidar/{stem}.pcd",
            file_type="point_cloud",
            scene_id=scene.id,
            frame_index=i,
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-PR-{uuid.uuid4().hex[:8]}",
            file_name=f"{stem}.pcd",
            file_path=f"{ds.name}/lidar/{stem}.pcd",
            file_type="point_cloud",
            status="pending",
        )
        db.add(task)
        await db.flush()
        tasks.append(task)
    return project, ds, scene, tasks


def _box3d(center=(1.0, 2.0, 3.0), size=(4.0, 5.0, 6.0), rotation=(0.0, 0.0, 0.5)):
    return {
        "type": "box_3d",
        "center": list(center),
        "size": list(size),
        "rotation": list(rotation),
        "convention_at_create": "iso_8855",
    }


async def _add_annotation(db, *, task, project, user_id, geometry, track_id=None):
    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type=geometry["type"],
        tool_unit_id="lidar_box_3d" if geometry["type"] == "box_3d" else "bbox",
        class_name="car",
        geometry=geometry,
        track_id=track_id,
        attributes={"occluded": True},
    )
    db.add(ann)
    await db.flush()
    return ann


# ── service-level 复制语义 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propagate_box3d_copies_and_assigns_shared_group(db_session, super_admin):
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, axis_convention="kitti_camera"
    )
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )

    svc = AnnotationService(db_session)
    new, _ = await svc._propagate(
        source_annotation_id=src.id,
        target_task_id=tasks[1].id,
        user_id=user.id,
    )

    # 几何 PSR 原值复制
    assert new.geometry["center"] == [1.0, 2.0, 3.0]
    assert new.geometry["size"] == [4.0, 5.0, 6.0]
    assert new.geometry["rotation"] == [0.0, 0.0, 0.5]
    # convention_at_create 取目标 dataset(不是源的 iso_8855)
    assert new.geometry["convention_at_create"] == "kitti_camera"
    # class / attributes 深拷贝
    assert new.class_name == "car"
    assert new.attributes == {"occluded": True}
    assert new.task_id == tasks[1].id
    # v0.21.2 · ADR-0045 · 跨帧共享 track_id 为唯一标识 (group_id 列已删)
    assert new.track_id is not None and new.track_id.startswith("trk_")
    assert src.track_id == new.track_id


@pytest.mark.asyncio
async def test_propagate_reuses_existing_track_id(db_session, super_admin):
    # v0.21.2 · 源已有 track_id → 复用不重分配 (跨帧链身份延续的关键)。
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_preexisting",
    )
    svc = AnnotationService(db_session)
    new, _ = await svc._propagate(
        source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
    )
    assert new.track_id == "trk_preexisting"
    assert src.track_id == "trk_preexisting"


@pytest.mark.asyncio
async def test_propagate_cannot_silently_cross_explicit_absence(
    db_session, super_admin
):
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk-explicit-stop",
    )
    track = await ensure_scene_track(
        db_session,
        project_id=project.id,
        scene_id=scene.id,
        track_id=src.track_id,
        class_name=src.class_name,
        frames={0},
        actor_id=user.id,
        interval_source="manual",
    )
    track.presence_mode = "explicit"
    src.scene_track_id = track.id
    await db_session.flush()

    with pytest.raises(HTTPException) as captured:
        await AnnotationService(db_session)._propagate(
            source_annotation_id=src.id,
            target_task_id=tasks[1].id,
            user_id=user.id,
        )

    assert captured.value.status_code == 409
    assert captured.value.detail["reason"] == "track_frame_absent"


@pytest.mark.asyncio
async def test_propagate_box3d_null_axis_convention(db_session, super_admin):
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, axis_convention=None
    )
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    new, _ = await svc._propagate(
        source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
    )
    assert new.geometry["convention_at_create"] is None


@pytest.mark.asyncio
async def test_propagate_bbox_2d(db_session, super_admin):
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    bbox = {"type": "bbox", "x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0}
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=bbox
    )
    svc = AnnotationService(db_session)
    new, _ = await svc._propagate(
        source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
    )
    assert new.geometry == bbox
    # 2D 不写 convention_at_create
    assert "convention_at_create" not in new.geometry
    # v0.21.2 · ADR-0045 · 跨帧标识走 track_id
    assert new.track_id is not None and new.track_id.startswith("trk_")


@pytest.mark.asyncio
async def test_propagate_rejects_video_bbox(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    vb = {"type": "video_bbox", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=vb
    )
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate(
            source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_propagate_rejects_cross_project(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    project_a, _, _, tasks_a = await _seed_scene(db_session, owner_id=user.id)
    _, _, _, tasks_b = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session,
        task=tasks_a[0],
        project=project_a,
        user_id=user.id,
        geometry=_box3d(),
    )
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate(
            source_annotation_id=src.id,
            target_task_id=tasks_b[0].id,
            user_id=user.id,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_propagate_rejects_same_task(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate(
            source_annotation_id=src.id, target_task_id=tasks[0].id, user_id=user.id
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_propagate_rejects_cross_scene(db_session, super_admin):
    """同 project / 同 dataset 但目标 task 属于另一个 scene → 422。"""
    from fastapi import HTTPException

    user, _ = super_admin
    project, ds, _, tasks = await _seed_scene(db_session, owner_id=user.id)

    scene_b = Scene(
        display_id=f"SCN-{uuid.uuid4().hex[:6]}",
        dataset_id=ds.id,
        name=f"s2-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(scene_b)
    await db_session.flush()
    stem = "100000"
    item_b = DatasetItem(
        dataset_id=ds.id,
        file_name=f"{stem}.pcd",
        file_path=f"{ds.name}/lidar/{stem}.pcd",
        file_type="point_cloud",
        scene_id=scene_b.id,
        frame_index=0,
    )
    db_session.add(item_b)
    await db_session.flush()
    task_b = Task(
        project_id=project.id,
        dataset_item_id=item_b.id,
        display_id=f"T-PR-{uuid.uuid4().hex[:8]}",
        file_name=f"{stem}.pcd",
        file_path=f"{ds.name}/lidar/{stem}.pcd",
        file_type="point_cloud",
        status="pending",
    )
    db_session.add(task_b)
    await db_session.flush()

    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate(
            source_annotation_id=src.id,
            target_task_id=task_b.id,
            user_id=user.id,
        )
    assert exc.value.status_code == 422


# ── API 端点端到端 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propagate_endpoint_201(db_session, httpx_client, super_admin):
    user, token = super_admin
    project, _, _, tasks = await _seed_scene(
        db_session, owner_id=user.id, axis_convention="ros_rep103"
    )
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )

    resp = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/annotations/{src.id}/propagate-to-task",
        json={"target_task_id": str(tasks[1].id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    ann = body["annotation"]
    assert ann["task_id"] == str(tasks[1].id)
    assert ann["geometry"]["convention_at_create"] == "ros_rep103"
    # v0.21.2 · ADR-0045 · 跨帧标识走 track_id
    assert ann["track_id"] is not None and ann["track_id"].startswith("trk_")


@pytest.mark.asyncio
async def test_propagate_endpoint_target_not_found(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    resp = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/annotations/{src.id}/propagate-to-task",
        json={"target_task_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_propagate_endpoint_annotation_task_mismatch_404(
    db_session, httpx_client, super_admin
):
    """越权防护: annotation 不属于 URL 里的源 task → 404。

    攻击路径: 用一个可见 task_id 作 URL 源, 配上别处 annotation_id 复制他人草稿。
    """
    user, token = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    # annotation 实际属于 tasks[1], 但 URL 源 task 用 tasks[0]。
    src = await _add_annotation(
        db_session, task=tasks[1], project=project, user_id=user.id, geometry=_box3d()
    )
    resp = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/annotations/{src.id}/propagate-to-task",
        json={"target_task_id": str(tasks[2].id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ── v0.15.1 · 运动补偿 / 批量 / 区间插值 ─────────────────────────────────


async def _seed_poses(db, scene_id, frames: int, step: float = 2.0):
    """直线轨迹: 帧 i 的 ego 在世界系 x = i*step,无旋转。"""
    from aap_assets_catalog.spatial_values import FramePose
    from aap_assets_catalog.spatial_contract import UpsertPosesRequest
    from app.services.assets_spatial import AssetsSpatialGateway

    await AssetsSpatialGateway(db).upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(),
            scene_id=scene_id,
            poses=[
                FramePose(
                    frame_index=i,
                    timestamp_us=i * 500000,
                    ego_translation=[i * step, 0.0, 0.0],
                    ego_rotation=[1.0, 0.0, 0.0, 0.0],
                )
                for i in range(frames)
            ],
        ),
        same_session_native=True,
    )


@pytest.mark.asyncio
async def test_propagate_motion_compensation(db_session, super_admin):
    """有 ego pose: 静止物世界位置不变 → 目标帧 ego 系 x 减去车的前进量。"""
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    await _seed_poses(db_session, scene.id, frames=3, step=2.0)

    src = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 2.0, 1.0)),
    )
    svc = AnnotationService(db_session)
    new, compensated = await svc._propagate(
        source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
    )
    assert compensated is True
    # 车从 x=0 前进到 x=2 → 框在新 ego 系 x = 10 - 2 = 8
    assert new.geometry["center"] == pytest.approx([8.0, 2.0, 1.0])
    assert new.geometry["size"] == [4.0, 5.0, 6.0]
    assert new.geometry["rotation"] == pytest.approx([0.0, 0.0, 0.5])


@pytest.mark.asyncio
async def test_propagate_without_pose_keeps_v0141_bytes(db_session, super_admin):
    """无 pose scene: 原样复制 + motion_compensated=False(v0.14.1 零回归)。"""
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    new, compensated = await svc._propagate(
        source_annotation_id=src.id, target_task_id=tasks[1].id, user_id=user.id
    )
    assert compensated is False
    assert new.geometry["center"] == [1.0, 2.0, 3.0]
    assert new.geometry["rotation"] == [0.0, 0.0, 0.5]


@pytest.mark.asyncio
async def test_propagate_override_psr_skips_compensation(db_session, super_admin):
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    await _seed_poses(db_session, scene.id, frames=3)
    src = await _add_annotation(
        db_session, task=tasks[0], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    new, compensated = await svc._propagate(
        source_annotation_id=src.id,
        target_task_id=tasks[1].id,
        user_id=user.id,
        override_psr={"center": [99.0, 0.0, 0.0]},
    )
    assert compensated is False
    assert new.geometry["center"] == [99.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_propagate_batch_all_box3d(db_session, super_admin):
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    await _seed_poses(db_session, scene.id, frames=3, step=2.0)
    a1 = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 0.0, 0.0)),
    )
    a2 = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(20.0, 5.0, 0.0)),
    )
    svc = AnnotationService(db_session)
    results, compensated = await svc._propagate_batch(
        source_task_id=tasks[0].id,
        target_task_id=tasks[1].id,
        annotation_ids=None,
        user_id=user.id,
    )
    assert compensated is True
    assert len(results) == 2
    by_src = {src_id: ann for src_id, ann in results}
    assert by_src[a1.id].geometry["center"] == pytest.approx([8.0, 0.0, 0.0])
    assert by_src[a2.id].geometry["center"] == pytest.approx([18.0, 5.0, 0.0])
    # v0.21.2 · 各自延续独立 track 链 (原 group 链)
    t1, t2 = by_src[a1.id].track_id, by_src[a2.id].track_id
    assert t1 and t2 and t1 != t2
    assert all(ann.task_id == tasks[1].id for _, ann in results)


@pytest.mark.asyncio
async def test_propagate_batch_resolution_not_amplified_by_box_count(
    db_session, super_admin, monkeypatch
):
    """N+1 回归守卫(PR #38 code-review 🟡): 批量 propagate 的 scene/frame
    /axis/pose 解析整批只做一次,不随框数线性放大。

    一次 batch 内所有框共享同一 source/target task,context(同 task/project
    /scene 校验 + axis_convention + 源/目标 ego pose)对整批恒定。这里放 4 个
    box_3d,断言 _resolve_axis_convention 只被调 1 次、_frame_pose 只被调 2 次
    (源帧 + 目标帧),而非分别 4 次 / 8 次 —— 直接锁住「不逐框重复解析」。
    """
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id)
    await _seed_poses(db_session, scene.id, frames=3, step=2.0)
    for i in range(4):
        await _add_annotation(
            db_session,
            task=tasks[0],
            project=project,
            user_id=user.id,
            geometry=_box3d(center=(float(i), 0.0, 0.0)),
        )

    from app.services.labeling_cross_frame_writes import (
        LabelingCrossFrameWriteService,
    )

    svc = LabelingCrossFrameWriteService(db_session)
    counters = {"axis": 0, "pose": 0}
    orig_axis = svc._resolve_axis_convention
    orig_pose = svc._frame_pose

    async def _spy_axis(task):
        counters["axis"] += 1
        return await orig_axis(task)

    async def _spy_pose(scene_id, frame_index):
        counters["pose"] += 1
        return await orig_pose(scene_id, frame_index)

    monkeypatch.setattr(svc, "_resolve_axis_convention", _spy_axis)
    monkeypatch.setattr(svc, "_frame_pose", _spy_pose)

    results, compensated = await svc._propagate_batch(
        source_task_id=tasks[0].id,
        target_task_id=tasks[1].id,
        annotation_ids=None,
        user_id=user.id,
    )
    assert len(results) == 4
    assert compensated is True
    # 恒定: 与框数(4)无关。若退回逐框 propagate 会是 4 / 8。
    assert counters["axis"] == 1
    assert counters["pose"] == 2


@pytest.mark.asyncio
async def test_propagate_batch_empty_source_422(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    _, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate_batch(
            source_task_id=tasks[0].id,
            target_task_id=tasks[1].id,
            annotation_ids=None,
            user_id=user.id,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_propagate_batch_foreign_annotation_404(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id)
    foreign = await _add_annotation(
        db_session, task=tasks[2], project=project, user_id=user.id, geometry=_box3d()
    )
    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._propagate_batch(
            source_task_id=tasks[0].id,
            target_task_id=tasks[1].id,
            annotation_ids=[foreign.id],
            user_id=user.id,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_interpolate_range_world_lerp(db_session, super_admin):
    """帧 0 与帧 4 各一框(共享 group),插值生成帧 1/2/3;世界系线性内插。"""
    user, _ = super_admin
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id, n=5)
    await _seed_poses(db_session, scene.id, frames=5, step=2.0)

    # 世界系: 框从 (10,0,0) 匀速移到 (18,4,0); 帧 i 的 ego 在 x=2i
    # → 帧 0 ego 系 center=(10,0,0); 帧 4 ego 系 center=(18-8, 4, 0)=(10,4,0)
    a0 = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)),
        track_id="trk_wl",
    )
    await _add_annotation(
        db_session,
        task=tasks[4],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 4.0, 0.0), rotation=(0.0, 0.0, 0.0)),
        track_id="trk_wl",
    )
    svc = AnnotationService(db_session)
    created, compensated, skipped = await svc._interpolate_range(
        track_id="trk_wl",
        from_task_id=tasks[0].id,
        to_task_id=tasks[4].id,
        user_id=user.id,
    )
    assert compensated is True
    assert skipped == []
    assert [a.task_id for a in created] == [t.id for t in tasks[1:4]]
    # 帧 2 (t=0.5): 世界中心 (14,2,0),ego x=4 → ego 系 (10,2,0)
    mid = created[1]
    assert mid.geometry["center"] == pytest.approx([10.0, 2.0, 0.0])
    assert mid.source == "interpolated"
    assert mid.track_id == "trk_wl"
    assert mid.class_name == a0.class_name

    # 幂等: 重跑全部跳过,不重复生成
    created2, _, skipped2 = await svc._interpolate_range(
        track_id="trk_wl",
        from_task_id=tasks[0].id,
        to_task_id=tasks[4].id,
        user_id=user.id,
    )
    assert created2 == []
    assert skipped2 == [1, 2, 3]


@pytest.mark.asyncio
async def test_interpolate_range_no_pose_degrades(db_session, super_admin):
    """无 pose scene: 纯 ego 系线性插值,motion_compensated=False。"""
    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)),
        track_id="trk_np",
    )
    await _add_annotation(
        db_session,
        task=tasks[2],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(4.0, 2.0, 0.0), rotation=(0.0, 0.0, 0.0)),
        track_id="trk_np",
    )
    svc = AnnotationService(db_session)
    created, compensated, _ = await svc._interpolate_range(
        track_id="trk_np",
        from_task_id=tasks[0].id,
        to_task_id=tasks[2].id,
        user_id=user.id,
    )
    assert compensated is False
    assert len(created) == 1
    assert created[0].geometry["center"] == pytest.approx([2.0, 1.0, 0.0])


@pytest.mark.asyncio
async def test_interpolate_range_validations(db_session, super_admin):
    from fastapi import HTTPException

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    svc = AnnotationService(db_session)

    # 两端缺框 → 422
    with pytest.raises(HTTPException) as exc:
        await svc._interpolate_range(
            track_id="trk_v0",
            from_task_id=tasks[0].id,
            to_task_id=tasks[2].id,
            user_id=user.id,
        )
    assert exc.value.status_code == 422

    # 相邻帧无中间帧 → 422
    await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_v1",
    )
    await _add_annotation(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_v1",
    )
    with pytest.raises(HTTPException) as exc:
        await svc._interpolate_range(
            track_id="trk_v1",
            from_task_id=tasks[0].id,
            to_task_id=tasks[1].id,
            user_id=user.id,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_interpolate_range_locked_mid_task_rejected(db_session, super_admin):
    """中间帧 task 处于锁态 → 整批拒,无部分写入。"""
    from fastapi import HTTPException
    from sqlalchemy import select as sa_select

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_lk",
    )
    await _add_annotation(
        db_session,
        task=tasks[2],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_lk",
    )
    tasks[1].status = "completed"
    await db_session.flush()

    def _deny_locked(t):
        if t.status in {"review", "completed"}:
            raise HTTPException(status_code=409, detail="task_locked")

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._interpolate_range(
            track_id="trk_lk",
            from_task_id=tasks[0].id,
            to_task_id=tasks[2].id,
            user_id=user.id,
            assert_task_editable=_deny_locked,
        )
    assert exc.value.status_code == 409
    rows = (
        (
            await db_session.execute(
                sa_select(Annotation).where(Annotation.task_id == tasks[1].id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_interpolate_range_invisible_mid_task_rejected(db_session, super_admin):
    """中间帧 task 对用户不可见(跨批次未分派)→ 整批拒,无部分写入。

    防权限漂移: 两端 task 可见不代表中间帧批次对该用户可见/已分派,
    interpolate_range 必须对每个中间帧 task 再走一遍可见性校验。
    """
    from fastapi import HTTPException
    from sqlalchemy import select as sa_select

    user, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=user.id, n=3)
    await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_inv",
    )
    await _add_annotation(
        db_session,
        task=tasks[2],
        project=project,
        user_id=user.id,
        geometry=_box3d(),
        track_id="trk_inv",
    )

    async def _deny_visible(t):
        # 模拟中间帧 task 所在批次对该用户不可见
        if t.id == tasks[1].id:
            raise HTTPException(status_code=404, detail="Task not found")

    svc = AnnotationService(db_session)
    with pytest.raises(HTTPException) as exc:
        await svc._interpolate_range(
            track_id="trk_inv",
            from_task_id=tasks[0].id,
            to_task_id=tasks[2].id,
            user_id=user.id,
            assert_task_visible=_deny_visible,
        )
    assert exc.value.status_code == 404
    rows = (
        (
            await db_session.execute(
                sa_select(Annotation).where(Annotation.task_id == tasks[1].id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_batch_and_interpolate_endpoints(db_session, httpx_client, super_admin):
    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    project, _, scene, tasks = await _seed_scene(db_session, owner_id=user.id, n=4)
    await _seed_poses(db_session, scene.id, frames=4, step=2.0)
    await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 0.0, 0.0)),
        track_id="trk_bi",
    )

    # propagate-batch: 帧 0 全部 box_3d → 帧 1
    resp = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/annotations/propagate-batch",
        json={"target_task_id": str(tasks[1].id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["motion_compensated"] is True
    assert len(body["items"]) == 1
    assert body["items"][0]["annotation"]["geometry"]["center"] == [8.0, 0.0, 0.0]

    # interpolate-range: 帧 0 ↔ 帧 3(帧 3 手动补一框),生成帧 1 跳过(已有)、帧 2 新建
    await _add_annotation(
        db_session,
        task=tasks[3],
        project=project,
        user_id=user.id,
        geometry=_box3d(center=(10.0, 3.0, 0.0)),
        track_id="trk_bi",
    )
    resp = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/annotations/interpolate-range",
        json={"track_id": "trk_bi", "to_task_id": str(tasks[3].id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["skipped_frames"] == [1]
    assert len(body["annotations"]) == 1
    assert body["annotations"][0]["source"] == "interpolated"
