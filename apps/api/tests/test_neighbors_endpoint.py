"""GET /tasks/{id}/neighbors 端点测试

覆盖判据:
- k=1 / k=5 默认行为
- 首帧 prev 为空 / 末帧 next 为空
- k=5 而 scene 仅 3 帧 → 各返回 < 5 个
- 跨 dataset 不串
- 历史未 backfill task → 200 + 全空
"""

from __future__ import annotations

import uuid


from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from tests.factory import create_project


async def _seed_scene_with_n_tasks(db, *, owner_id, n: int):
    """建一个 lidar 项目 + dataset + scene + n 个连续帧 task。

    返回 (project, dataset, scene, [task_0, task_1, ..., task_{n-1}])。
    每个 task.dataset_item_id 指向该帧 lidar item;item 写 scene_id + frame_index=i。
    """
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云"
    )
    project.data_type = "lidar"

    ds = Dataset(
        display_id=f"DS-NB-{uuid.uuid4().hex[:6]}",
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
            display_id=f"T-NB-{uuid.uuid4().hex[:8]}",
            file_name=f"{stem}.pcd",
            file_path=f"{ds.name}/lidar/{stem}.pcd",
            file_type="point_cloud",
            status="pending",
        )
        db.add(task)
        await db.flush()
        tasks.append(task)

    return project, ds, scene, tasks


async def test_neighbors_k1_default(db_session, httpx_client, super_admin):
    _, _, scene, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=5
    )
    user, token = super_admin
    cur = tasks[2]

    resp = await httpx_client.get(
        f"/api/v1/tasks/{cur.id}/neighbors",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scene_id"] == str(scene.id)
    assert body["frame_index"] == 2
    assert body["scene_total_frames"] == 5
    assert len(body["prev"]) == 1
    assert len(body["next"]) == 1
    assert body["prev"][0]["frame_index"] == 1
    assert body["prev"][0]["task_id"] == str(tasks[1].id)
    assert body["next"][0]["frame_index"] == 3
    assert body["next"][0]["task_id"] == str(tasks[3].id)


async def test_neighbors_k5_full_window(db_session, httpx_client, super_admin):
    _, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=11
    )
    user, token = super_admin
    cur = tasks[5]

    resp = await httpx_client.get(
        f"/api/v1/tasks/{cur.id}/neighbors?k=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["prev"]) == 5
    assert len(body["next"]) == 5
    assert body["prev"][0]["frame_index"] == 4
    assert body["prev"][-1]["frame_index"] == 0
    assert body["next"][0]["frame_index"] == 6
    assert body["next"][-1]["frame_index"] == 10


async def test_neighbors_first_frame_prev_empty(db_session, httpx_client, super_admin):
    _, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    user, token = super_admin

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/neighbors",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["prev"] == []
    assert len(body["next"]) == 1


async def test_neighbors_last_frame_next_empty(db_session, httpx_client, super_admin):
    _, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    user, token = super_admin

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[-1].id}/neighbors",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["next"] == []
    assert len(body["prev"]) == 1


async def test_neighbors_k5_with_only_3_frames(db_session, httpx_client, super_admin):
    """k=5 但 scene 仅 3 帧 → 各方向最多返回剩余数。"""
    _, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    user, token = super_admin
    cur = tasks[1]

    resp = await httpx_client.get(
        f"/api/v1/tasks/{cur.id}/neighbors?k=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert len(body["prev"]) == 1
    assert len(body["next"]) == 1


async def test_neighbors_no_scene_returns_empty(db_session, httpx_client, super_admin):
    """task 无 scene_id(历史未 backfill)→ 200 + 全空。"""
    project = await create_project(
        db_session, owner_id=super_admin[0].id, type_key="image_detection"
    )
    ds = Dataset(
        display_id=f"DS-X-{uuid.uuid4().hex[:6]}",
        name="legacy",
        data_type="image",
        created_by=super_admin[0].id,
    )
    db_session.add(ds)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=ds.id,
        file_name="x.jpg",
        file_path="legacy/x.jpg",
        file_type="image",
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

    user, token = super_admin
    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/neighbors",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prev"] == []
    assert body["next"] == []
    assert body["scene_total_frames"] == 0
    assert body["scene_id"] is None
    assert body["scene_name"] is None
    assert body["frame_index"] is None


async def test_neighbors_does_not_cross_dataset(db_session, httpx_client, super_admin):
    """两个独立 dataset 各一 scene,查 A 不返回 B。"""
    _, _, scene_a, tasks_a = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    _, _, scene_b, tasks_b = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    user, token = super_admin

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks_a[1].id}/neighbors?k=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["scene_id"] == str(scene_a.id)
    returned_task_ids = {n["task_id"] for n in body["prev"] + body["next"]}
    assert returned_task_ids.issubset({str(t.id) for t in tasks_a})
    assert all(str(t.id) not in returned_task_ids for t in tasks_b)


async def test_neighbors_k_out_of_range_400(db_session, httpx_client, super_admin):
    _, _, _, tasks = await _seed_scene_with_n_tasks(
        db_session, owner_id=super_admin[0].id, n=3
    )
    user, token = super_admin

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/neighbors?k=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422

    resp = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/neighbors?k=21",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
