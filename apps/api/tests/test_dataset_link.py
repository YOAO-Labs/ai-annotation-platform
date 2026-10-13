"""v0.6.6 · DatasetService.link_project bulk_insert 路径覆盖。

历史：v0.6.5 之前 link_project 逐条 db.add + 逐条 nextval('display_seq_tasks')，
1000 items 需 ~2s。v0.6.6 改为单次 generate_series + insert，需要验证：
1. tasks 行数 == dataset_items 行数
2. display_id 唯一且符合 T-N 格式
3. project.total_tasks 正确累加
4. 重复 link 同一对返回已存在 link，不创建新 task
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.dataset import DatasetService


async def _seed_dataset(db: AsyncSession, owner_id: uuid.UUID, n_items: int) -> Dataset:
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-LINK-{suffix}",
        name="link_project bulk test",
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
    return ds


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    suffix = uuid.uuid4().hex[:6]
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-LINK-{suffix}",
        name="link_project target",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(p)
    await db.flush()
    return p


@pytest.mark.asyncio
async def test_link_project_creates_tasks_in_bulk(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=25)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)

    count = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert count == 25

    rows = (
        await db_session.execute(
            select(Task.display_id).where(Task.project_id == project.id)
        )
    ).all()
    display_ids = [r[0] for r in rows]
    assert len(set(display_ids)) == 25, "display_id 必须唯一"
    assert all(d.startswith("T-") for d in display_ids), "display_id 必须 T- 前缀"

    await db_session.refresh(project)
    assert project.total_tasks == 25


@pytest.mark.asyncio
async def test_link_project_idempotent(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=5)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    link1 = await svc.link_project(ds.id, project.id)
    link2 = await svc.link_project(ds.id, project.id)
    # v0.12.0：link_project 返回 LinkProjectResult，.link 才是 ProjectDataset 行。
    assert link1.link.id == link2.link.id, "重复 link 应返回同一行"

    count = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert count == 5, "第二次 link 不应再创建 task"


@pytest.mark.asyncio
async def test_link_project_empty_dataset(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=0)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)

    count = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_append_item_after_link_creates_tasks_for_linked_projects(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=0)
    project_a = await _seed_project(db_session, user.id)
    project_b = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project_a.id)
    await svc.link_project(ds.id, project_b.id)

    item = await svc.add_item(
        dataset_id=ds.id,
        file_name="new-video.mp4",
        file_path="link_project bulk test/new-video.mp4",
        file_type="video",
        file_size=123,
    )

    created = await svc.create_tasks_for_items(ds.id, [item.id])
    assert created == 2
    assert await svc.create_tasks_for_items(ds.id, [item.id]) == 0

    for project in (project_a, project_b):
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.project_id == project.id,
                    Task.dataset_item_id == item.id,
                )
            )
        ).scalar()
        assert count == 1
        await db_session.refresh(project)
        assert project.total_tasks == 1


@pytest.mark.asyncio
async def test_link_project_no_default_batch(db_session: AsyncSession, super_admin):
    """v0.7.3：link_project 不再创建「{数据集} 默认包」batch；新建 task 全部 batch_id=NULL，
    走「未归类任务」语义，由用户主动 split 归到 batch。"""
    from app.db.models.task_batch import TaskBatch

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=8)
    ds.name = "MyImageSet"
    await db_session.flush()
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)

    # 不应有任何 batch 自动生成
    batches = (
        (
            await db_session.execute(
                select(TaskBatch).where(
                    TaskBatch.project_id == project.id, TaskBatch.dataset_id == ds.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(batches) == 0

    # task 全部 batch_id=NULL（未归类）
    unclassified = (
        await db_session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == project.id, Task.batch_id.is_(None))
        )
    ).scalar()
    assert unclassified == 8


@pytest.mark.asyncio
async def test_unlink_project_hard_deletes_tasks(db_session: AsyncSession, super_admin):
    """v0.6.7 二修 B-10：unlink 应级联删除该 dataset 在该 project 下的 task，project.total_tasks 归零。"""
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=10)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)
    await db_session.refresh(project)
    assert project.total_tasks == 10

    info = await svc.unlink_project(ds.id, project.id)
    assert info is not None
    assert info["deleted_tasks"] == 10
    assert info["soft"] is False

    await db_session.refresh(project)
    real_total = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert real_total == 0, "hard-unlink 后该 dataset 创建的 task 应清光"
    assert project.total_tasks == 0


@pytest.mark.asyncio
async def test_unlink_cascades_user_split_batches(
    db_session: AsyncSession, super_admin
):
    """v0.7.3 fix：用户把 dataset 任务 split 到 2 个 batch 后取消关联，2 个 batch 都应被清理。
    管理员手工建的与该 dataset task 无关的空草稿不受影响；B-DEFAULT 永远保留。"""
    from app.db.models.task_batch import TaskBatch
    from app.services.display_id import next_display_id

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=6)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)
    # v0.7.3：link 不再自建 batch，task 全部 batch_id=NULL；用户随后调 split

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    sub_a = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=await next_display_id(db_session, "batches"),
        name="sub A",
        status="draft",
        assigned_user_ids=[],
    )
    sub_b = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=await next_display_id(db_session, "batches"),
        name="sub B",
        status="draft",
        assigned_user_ids=[],
    )
    db_session.add_all([sub_a, sub_b])
    await db_session.flush()
    for i, t in enumerate(tasks):
        t.batch_id = sub_a.id if i % 2 == 0 else sub_b.id

    # 同时再加一个空草稿（与 dataset 无关），不应被误删
    manual_empty = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=await next_display_id(db_session, "batches"),
        name="manual draft",
        status="draft",
        assigned_user_ids=[],
    )
    db_session.add(manual_empty)
    await db_session.flush()

    info = await svc.unlink_project(ds.id, project.id)
    assert info is not None
    deleted_ids = set(info["deleted_batch_ids"])
    assert str(sub_a.id) in deleted_ids
    assert str(sub_b.id) in deleted_ids
    assert info["deleted_batches"] == 2

    remaining = (
        (
            await db_session.execute(
                select(TaskBatch).where(TaskBatch.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    names = {b.name for b in remaining}
    assert "manual draft" in names, "手工建的空 batch 不应被误删"


@pytest.mark.asyncio
async def test_unlink_cascades_legacy_default_batch(
    db_session: AsyncSession, super_admin
):
    """v0.7.3：历史数据兼容 —— 老库里残留的「{数据集} 默认包」batch 在 unlink 时同样应清掉。
    模拟方法：link 之后手工补一个挂 dataset_id 的 batch 持有所有 task。"""
    from app.db.models.task_batch import TaskBatch
    from app.services.display_id import next_display_id

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=4)
    ds.name = "Legacy"
    await db_session.flush()
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)

    legacy_default = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_id=ds.id,
        display_id=await next_display_id(db_session, "batches"),
        name="Legacy 默认包",
        status="draft",
        assigned_user_ids=[],
    )
    db_session.add(legacy_default)
    await db_session.flush()
    await db_session.execute(
        Task.__table__.update()
        .where(Task.project_id == project.id)
        .values(batch_id=legacy_default.id)
    )
    await db_session.flush()

    info = await svc.unlink_project(ds.id, project.id)
    assert info is not None
    assert str(legacy_default.id) in info["deleted_batch_ids"]


@pytest.mark.asyncio
async def test_unclassified_count_endpoint(httpx_client, db_session, super_admin):
    """v0.7.3：未归类任务计数端点 — 给 BatchesSection 顶部横带用。"""
    user, token = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=5)
    project = await _seed_project(db_session, user.id)
    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{project.id}/batches/unclassified-count",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 5


@pytest.mark.asyncio
async def test_project_datasets_endpoint(httpx_client, db_session, super_admin):
    """v0.7.3：项目侧 GET /projects/{id}/datasets — 列出已关联 dataset，含 task 数。"""
    user, token = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=3)
    ds.name = "DS-A"
    await db_session.flush()
    project = await _seed_project(db_session, user.id)
    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{project.id}/datasets",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "DS-A"
    assert rows[0]["items_count"] == 3
    assert rows[0]["tasks_in_project"] == 3


@pytest.mark.asyncio
async def test_link_unlink_relink_no_double_count(
    db_session: AsyncSession, super_admin
):
    """v0.6.7 B-10：link → unlink → re-link 不出现 double-count。hard-unlink 下 task 真删 + 重 link 重新创建。"""
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=4)
    project = await _seed_project(db_session, user.id)

    svc = DatasetService(db_session)
    await svc.link_project(ds.id, project.id)
    await svc.unlink_project(ds.id, project.id)
    await svc.link_project(ds.id, project.id)

    await db_session.refresh(project)
    real_total = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert project.total_tasks == real_total
    assert real_total == 4, "重 link 应重新创建 4 个 task"
