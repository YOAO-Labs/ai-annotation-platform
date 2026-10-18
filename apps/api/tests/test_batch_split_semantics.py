"""v0.11.22 · BatchService.split 新增语义覆盖。

1. n_batches=1：把全部未归类任务注入一个新建批次（修复「分包选单个批次无法注入 task」缺口）。
2. shuffle=False：顺序切分，按 task 创建顺序（_splittable_task_ids 的 order_by）分配，不打乱。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import BatchStatus
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas.batch import BatchSplitRequest
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services import batch as batch_service_mod
from app.services.batch import BatchService


async def _seed_linked(db: AsyncSession, owner_id: uuid.UUID, n_items: int) -> Project:
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-SPLIT-{suffix}",
        name="split semantics test",
        data_type="image",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    for i in range(n_items):
        db.add(
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=ds.id,
                file_name=f"img-{i:04d}.jpg",
                file_path=f"/tmp/img-{i:04d}.jpg",
                file_type="image",
            )
        )
    await db.flush()

    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-SPLIT-{suffix}",
        name="split target",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(project)
    await db.flush()

    await DatasetTaskCreationWorkflow(db).link_project(
        ds.id, project.id, actor_id=owner_id
    )
    return project


@pytest.mark.asyncio
async def test_split_into_single_batch_injects_all_unclassified(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=6)

    batches = await BatchService(db_session).split(
        project.id,
        BatchSplitRequest(strategy="random", n_batches=1, name_prefix="第 1 批"),
        user.id,
    )

    assert len(batches) == 1
    batch = batches[0]
    # n=1 直接用名称本身，不加 " 1" 后缀
    assert batch.name == "第 1 批"
    # 6 个未归类 task 全部注入这一个新批次
    rows = (
        await db_session.execute(select(Task.id).where(Task.batch_id == batch.id))
    ).all()
    assert len(rows) == 6
    # 没有残留未归类任务
    leftover = (
        await db_session.execute(
            select(Task.id).where(
                Task.project_id == project.id, Task.batch_id.is_(None)
            )
        )
    ).all()
    assert leftover == []


@pytest.mark.asyncio
async def test_sequential_split_preserves_creation_order(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=6)

    ordered_task_ids = [
        r[0]
        for r in (
            await db_session.execute(
                select(Task.id)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at, Task.id)
            )
        ).all()
    ]

    batches = await BatchService(db_session).split(
        project.id,
        BatchSplitRequest(strategy="random", n_batches=2, shuffle=False),
        user.id,
    )
    assert len(batches) == 2

    # 顺序切分：第 1 批是前 3 个、第 2 批是后 3 个（与创建顺序一致，未打乱）
    first = [
        r[0]
        for r in (
            await db_session.execute(
                select(Task.id).where(Task.batch_id == batches[0].id)
            )
        ).all()
    ]
    second = [
        r[0]
        for r in (
            await db_session.execute(
                select(Task.id).where(Task.batch_id == batches[1].id)
            )
        ).all()
    ]
    assert set(first) == set(ordered_task_ids[:3])
    assert set(second) == set(ordered_task_ids[3:])


@pytest.mark.asyncio
async def test_assign_tasks_chunks_assign_all(
    db_session: AsyncSession, super_admin, monkeypatch
):
    """v0.12.0 B5 · _assign_tasks 分块回写 batch_id：跨多块的长 list 仍全量分配。

    用 monkeypatch 把分块大小压到 7（远小于 30 条），强制走多轮 UPDATE，
    覆盖分块循环的边界，而无需真造 5000 条。
    """
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=30)

    task_ids = [
        r[0]
        for r in (
            await db_session.execute(
                select(Task.id).where(Task.project_id == project.id)
            )
        ).all()
    ]
    assert len(task_ids) == 30

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-{uuid.uuid4().hex[:6]}",
        name="chunk target",
        status=BatchStatus.DRAFT,
        created_by=user.id,
    )
    db_session.add(batch)
    await db_session.flush()

    # 7 不整除 30 → 末块更短，确认边界处理无遗漏
    monkeypatch.setattr(batch_service_mod, "ASSIGN_CHUNK_SIZE", 7)
    assigned = await BatchService(db_session)._assign_tasks(batch.id, task_ids)
    assert assigned == 30

    rows = (
        await db_session.execute(select(Task.id).where(Task.batch_id == batch.id))
    ).all()
    assert len(rows) == 30
