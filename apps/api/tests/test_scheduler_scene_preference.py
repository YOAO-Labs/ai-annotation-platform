"""v0.14.1 · scheduler prefer_same_scene_continuation 开关行为 + 回归

覆盖判据(plan §1.4 判据 5 / §3.7):
- ON: get_next_task 返回"用户最近标注 task 的同 scene 下一帧"
- ON 但无 recent annotation → 回退既有 sequence 策略
- ON 但 scene 末帧 → 回退既有策略(不串到别的 scene)
- OFF(默认): 行为与既有 sequence 策略一致(零回归)
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.scheduler import get_next_task
from app.services.task_lock import TaskLockService
from tests.factory import create_membership, create_project


async def _seed_scene_project(
    db, *, owner_id, annotator_id, n: int = 4, prefer: bool = False
):
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="点云"
    )
    project.data_type = "lidar"
    project.prefer_same_scene_continuation = prefer
    await db.flush()

    await create_membership(
        db,
        project_id=project.id,
        user_id=annotator_id,
        role="annotator",
        assigned_by=owner_id,
    )

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-SC-{uuid.uuid4().hex[:6]}",
        name="b",
        status="active",
        annotator_id=annotator_id,
        assigned_user_ids=[str(annotator_id)],
    )
    db.add(batch)

    ds = Dataset(
        display_id=f"DS-SC-{uuid.uuid4().hex[:6]}",
        name=f"sc-{uuid.uuid4().hex[:6]}",
        data_type="point_cloud",
        created_by=owner_id,
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
        t = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            batch_id=batch.id,
            dataset_item_id=item.id,
            display_id=f"T-SC-{i}-{uuid.uuid4().hex[:6]}",
            file_name=f"{stem}.pcd",
            file_path=f"{ds.name}/lidar/{stem}.pcd",
            file_type="point_cloud",
            status="pending",
            is_labeled=False,
            sequence_order=i,
        )
        db.add(t)
        await db.flush()
        tasks.append(t)
    return project, scene, tasks


async def _annotate(db, *, task, project, user_id):
    ann = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=user_id,
        source="manual",
        annotation_type="box_3d",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry={
            "type": "box_3d",
            "center": [0, 0, 0],
            "size": [1, 1, 1],
            "rotation": [0, 0, 0],
        },
    )
    db.add(ann)
    await db.flush()
    return ann


@pytest.mark.asyncio
async def test_scene_preference_on_returns_next_frame(
    db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=4, prefer=True
    )
    # 用户刚标完 frame 1
    await _annotate(db_session, task=tasks[1], project=project, user_id=user.id)

    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    assert nxt.id == tasks[2].id  # 同 scene 下一帧


@pytest.mark.asyncio
async def test_scene_preference_off_uses_sequence(db_session, super_admin, annotator):
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=4, prefer=False
    )
    await _annotate(db_session, task=tasks[1], project=project, user_id=user.id)

    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    # OFF: 既有 sequence 策略 → frame 0(已标的 frame1 被排除, 但不优先连续)
    assert nxt.id == tasks[0].id


@pytest.mark.asyncio
async def test_scene_preference_on_no_recent_falls_back(
    db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=4, prefer=True
    )
    # 无 recent annotation → 回退 sequence → frame 0
    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    assert nxt.id == tasks[0].id


@pytest.mark.asyncio
async def test_scene_preference_on_last_frame_falls_back(
    db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=3, prefer=True
    )
    # 标完末帧 frame 2 → scene 内无后续帧 → 回退 sequence → frame 0
    await _annotate(db_session, task=tasks[2], project=project, user_id=user.id)
    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    assert nxt.id == tasks[0].id


@pytest.mark.asyncio
async def test_scene_preference_on_acquire_fails_falls_back(
    db_session, super_admin, annotator, monkeypatch
):
    """TOCTOU 回归: scene 分支命中下一帧 task, 但 acquire 因他人抢先返回 None,
    必须回退到经典分配路径(返回另一把成功上锁的 task), 而不是返回那个未上锁的 scene task。"""
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=4, prefer=True
    )
    # 用户刚标完 frame 1 → scene 分支会命中 frame 2(tasks[2])
    await _annotate(db_session, task=tasks[1], project=project, user_id=user.id)

    scene_next_id = tasks[2].id
    real_acquire = TaskLockService.acquire

    async def fake_acquire(self, task_id, user_id, ttl=None, force_takeover=False):
        # 模拟他人在预过滤与 acquire 之间抢先: 仅对 scene next frame 抢锁失败
        if task_id == scene_next_id:
            return None
        return await real_acquire(
            self, task_id, user_id, ttl=ttl, force_takeover=force_takeover
        )

    monkeypatch.setattr(TaskLockService, "acquire", fake_acquire)

    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    # 不能返回未拿到锁的 scene next frame
    assert nxt.id != scene_next_id
    # 回退经典 sequence 策略: frame1 已标被排除, 取 frame 0
    assert nxt.id == tasks[0].id


@pytest.mark.asyncio
async def test_classic_path_skips_candidate_when_acquire_fails(
    db_session, super_admin, annotator, monkeypatch
):
    """经典路径 TOCTOU 回归: 首个候选(frame0)在「候选查询」与「acquire」之间被他人抢锁,
    acquire 返回 None, 调度必须跳到下一个候选(frame1)并成功上锁返回, 而不是返回未上锁的
    frame0。prefer=False 走经典路径。"""
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=4, prefer=False
    )

    contended_id = tasks[0].id  # 经典 sequence 序首个候选
    real_acquire = TaskLockService.acquire

    async def fake_acquire(self, task_id, user_id, ttl=None, force_takeover=False):
        if task_id == contended_id:
            return None  # 模拟他人抢先上锁
        return await real_acquire(
            self, task_id, user_id, ttl=ttl, force_takeover=force_takeover
        )

    monkeypatch.setattr(TaskLockService, "acquire", fake_acquire)

    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is not None
    assert nxt.id != contended_id  # 不返回未拿到锁的首候选
    assert nxt.id == tasks[1].id  # 跳到下一候选并成功上锁


@pytest.mark.asyncio
async def test_classic_path_returns_none_when_all_candidates_locked(
    db_session, super_admin, annotator, monkeypatch
):
    """经典路径: 候选窗口内全部 acquire 失败(全被他人占)→ 返回 None, 不返回未上锁 task。"""
    owner, _ = super_admin
    user, _ = annotator
    project, _, tasks = await _seed_scene_project(
        db_session, owner_id=owner.id, annotator_id=user.id, n=3, prefer=False
    )

    async def fake_acquire(self, task_id, user_id, ttl=None, force_takeover=False):
        return None  # 所有候选都抢不到锁

    monkeypatch.setattr(TaskLockService, "acquire", fake_acquire)

    nxt = await get_next_task(user, project.id, db_session, project_role="annotator")
    assert nxt is None
