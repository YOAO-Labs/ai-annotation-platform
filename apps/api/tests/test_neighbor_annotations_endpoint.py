"""v0.15.17 · GET /tasks/{id}/neighbor-annotations 批量端点测试

覆盖判据(plan §1.4 判据 4):
- 一次返回 ±k 帧的邻帧标注,按距中心远近排序
- track_id 给定 → 服务端只回该跨帧链(scope=selected)
- track_id 省略 → 回区间全部框(scope=all)
- 不含中心帧自身
- 历史未 backfill / 非 scene task → 200 + frames=[]
- 跨 dataset 不串
"""

from __future__ import annotations

import uuid

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from tests.factory import create_membership, create_batch, create_project, create_user


async def _seed_scene_with_n_tasks(db, *, owner_id, n: int):
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云"
    )
    project.data_type = "lidar"

    ds = Dataset(
        display_id=f"DS-NA-{uuid.uuid4().hex[:6]}",
        name=f"scene-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()

    scene = Scene(
        display_id=f"SCN-{uuid.uuid4().hex[:6]}",
        dataset_id=ds.id,
        name=f"sc-{uuid.uuid4().hex[:6]}",
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
            display_id=f"T-NA-{uuid.uuid4().hex[:8]}",
            file_name=f"{stem}.pcd",
            file_path=f"{ds.name}/lidar/{stem}.pcd",
            file_type="point_cloud",
            status="pending",
        )
        db.add(task)
        await db.flush()
        tasks.append(task)
    return project, ds, scene, tasks


def _box3d():
    return {
        "type": "box_3d",
        "center": [1.0, 2.0, 3.0],
        "size": [4.0, 5.0, 6.0],
        "rotation": [0.0, 0.0, 0.0],
        "convention_at_create": "iso_8855",
    }


async def _add_box(db, *, task, project, user_id, track_id=None):
    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type="box_3d",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry=_box3d(),
        track_id=track_id,
    )
    db.add(ann)
    await db.flush()
    return ann


async def test_neighbor_annotations_scope_all(db_session, httpx_client, super_admin):
    """track_id 省略 → 回 ±k 帧全部框,按距中心远近排序,不含中心帧。"""
    user, token = super_admin
    project, _, scene, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=user.id, n=5
    )
    # 每帧各放两个框
    for t in tasks:
        await _add_box(db_session, task=t, project=project, user_id=user.id)
        await _add_box(db_session, task=t, project=project, user_id=user.id)

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[2].id}/neighbor-annotations?k=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scene_id"] == str(scene.id)
    assert body["frame_index"] == 2
    # k=1 → 前后各一帧,prev 在前(就近)
    frames = body["frames"]
    assert [f["frame_index"] for f in frames] == [1, 3]
    # 不含中心帧 2
    assert all(f["frame_index"] != 2 for f in frames)
    # 每帧两个框
    assert all(len(f["annotations"]) == 2 for f in frames)
    assert frames[0]["task_id"] == str(tasks[1].id)


async def test_neighbor_annotations_scope_selected(
    db_session, httpx_client, super_admin
):
    """track_id 给定 → 服务端只回该 track (v0.21.2 · ADR-0045)。"""
    user, token = super_admin
    project, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=user.id, n=5
    )
    for t in tasks:
        await _add_box(
            db_session, task=t, project=project, user_id=user.id, track_id="trk_7"
        )
        await _add_box(
            db_session, task=t, project=project, user_id=user.id, track_id="trk_8"
        )

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[2].id}/neighbor-annotations?k=2&track_id=trk_7",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    frames = body["frames"]
    assert [f["frame_index"] for f in frames] == [1, 0, 3, 4]
    for f in frames:
        assert len(f["annotations"]) == 1
        assert f["annotations"][0]["track_id"] == "trk_7"


async def test_neighbor_annotations_no_scene_empty(
    db_session, httpx_client, super_admin
):
    """非 scene task → 200 + frames=[]。"""
    user, token = super_admin
    project = await create_project(
        db_session, owner_id=user.id, type_key="image_detection"
    )
    ds = Dataset(
        display_id=f"DS-X-{uuid.uuid4().hex[:6]}",
        name="legacy",
        data_type="image",
        created_by=user.id,
    )
    db_session.add(ds)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=ds.id, file_name="x.jpg", file_path="legacy/x.jpg", file_type="image"
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-X-{uuid.uuid4().hex[:8]}",
        file_name="x.jpg",
        file_path="legacy/x.jpg",
        file_type="image",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/neighbor-annotations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scene_id"] is None
    assert body["frames"] == []


async def test_neighbor_annotations_does_not_cross_dataset(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project_a, _, scene_a, tasks_a = await _seed_scene_with_n_tasks(
        db_session, owner_id=user.id, n=3
    )
    project_b, _, _, tasks_b = await _seed_scene_with_n_tasks(
        db_session, owner_id=user.id, n=3
    )
    for t in tasks_a:
        await _add_box(db_session, task=t, project=project_a, user_id=user.id)
    for t in tasks_b:
        await _add_box(db_session, task=t, project=project_b, user_id=user.id)

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks_a[1].id}/neighbor-annotations?k=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["scene_id"] == str(scene_a.id)
    returned = {f["task_id"] for f in body["frames"]}
    assert returned.issubset({str(t.id) for t in tasks_a})
    assert all(str(t.id) not in returned for t in tasks_b)


async def test_neighbor_annotations_k_out_of_range_422(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    _, _, _, tasks = await _seed_scene_with_n_tasks(db_session, owner_id=user.id, n=3)
    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/neighbor-annotations?k=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_neighbor_annotations_filters_cross_batch(
    db_session, httpx_client, super_admin, annotator
):
    """v0.15.26 · annotator 只看到自己 batch 内邻帧的框;落在他人 batch 的邻帧返回
    frame 占位但 annotations=[],不外泄其几何(与旧逐 task getAnnotations 链路同口径)。"""
    admin, _ = super_admin
    anno, anno_token = annotator
    # 项目 owner 为 admin(非 annotator),否则 annotator 成 owner 即特权全可见。
    project, _, scene, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=admin.id, n=3
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=anno.id,
        role="annotator",
        assigned_by=admin.id,
    )
    # 中心帧(1)与后邻帧(2)在 annotator 自己的 active batch;前邻帧(0)在别人的 batch。
    other_anno = await create_user(
        db_session, "annotator", f"anno2-{uuid.uuid4().hex[:6]}@test.local", "Anno2"
    )
    mine = await create_batch(db_session, project_id=project.id, status="active")
    mine.annotator_id = anno.id
    other = await create_batch(db_session, project_id=project.id, status="active")
    other.annotator_id = other_anno.id  # 分派给别人
    await db_session.flush()
    tasks[0].batch_id = other.id
    tasks[1].batch_id = mine.id
    tasks[2].batch_id = mine.id
    await db_session.flush()

    for t in tasks:
        await _add_box(db_session, task=t, project=project, user_id=admin.id)

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[1].id}/neighbor-annotations?k=1",
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scene_id"] == str(scene.id)
    frames = {f["frame_index"]: f for f in body["frames"]}
    assert set(frames) == {0, 2}
    # 前邻帧 0 在他人 batch → 占位但无框(不外泄)
    assert frames[0]["annotations"] == []
    # 后邻帧 2 在自己 batch → 正常返回框
    assert len(frames[2]["annotations"]) == 1
