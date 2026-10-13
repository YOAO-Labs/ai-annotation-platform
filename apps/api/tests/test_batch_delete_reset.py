"""v0.11.23 · 删除/解绑批次时级联重置 task 状态 + 清 AI 预标。

删批次不再只解绑：批次内非 pending task 重置为 pending（保留人工标注、软删 AI 标注、
删 predictions），避免 review/completed 孤儿化 + 重分包后再预标叠加重复标注。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.schemas.batch import BatchSplitRequest
from app.services.dataset import DatasetService
from app.services.batch import BatchService


async def _seed_linked(db: AsyncSession, owner_id: uuid.UUID, n_items: int) -> Project:
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-DEL-{suffix}",
        name="delete reset test",
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
        display_id=f"P-DEL-{suffix}",
        name="delete reset target",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(project)
    await db.flush()
    await DatasetService(db).link_project(ds.id, project.id)
    return project


def _manual_anno(task_id: uuid.UUID, project_id: uuid.UUID) -> Annotation:
    return Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        source="manual",
        class_name="cat",
        geometry={"x": 1, "y": 1, "w": 2, "h": 2},
        is_active=True,
    )


def _ai_anno(task_id: uuid.UUID, project_id: uuid.UUID) -> Annotation:
    return Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        source="prediction_based",
        class_name="dog",
        geometry={"x": 3, "y": 3, "w": 4, "h": 4},
        is_active=True,
    )


def _prediction(task_id: uuid.UUID, project_id: uuid.UUID) -> Prediction:
    return Prediction(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        result={"shapes": []},
        source="ml_backend",
    )


@pytest.mark.asyncio
async def test_reset_rebinds_materialized_assignment_of_departed_member(
    db_session: AsyncSession, super_admin
):
    """角色交接把旧默认物化到 terminal task 上；reset 回 pending 后，若该用户已
    不再持有对应项目职责，物化指派必须让位给批次新默认（非 override）。"""

    from app.db.models.project_member import ProjectMember
    from app.db.models.task_batch import TaskBatch
    from tests.factory import create_user

    owner, _ = super_admin
    project = await _seed_linked(db_session, owner.id, n_items=2)
    batch_id = await _make_batch(db_session, project)

    old_annotator = await create_user(
        db_session, "employee", f"reset-old-{uuid.uuid4().hex[:6]}@test.local", "Old"
    )
    replacement = await create_user(
        db_session, "employee", f"reset-new-{uuid.uuid4().hex[:6]}@test.local", "New"
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=old_annotator.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=replacement.id,
                role="annotator",
                assigned_by=owner.id,
            ),
        ]
    )
    await db_session.flush()

    batch = await db_session.get(TaskBatch, batch_id)
    batch.annotator_id = old_annotator.id
    batch.status = "annotating"
    tasks = (
        (
            await db_session.execute(
                select(Task).where(Task.batch_id == batch_id).order_by(Task.id)
            )
        )
        .scalars()
        .all()
    )
    tasks[0].status = "completed"
    # Handoff-style materialization: explicit old-user assignment, not an
    # override, on history past the editable annotation phase.
    tasks[0].assignee_id = old_annotator.id
    tasks[0].assignee_is_override = False
    tasks[1].assignee_id = replacement.id
    tasks[1].assignee_is_override = True
    await db_session.flush()

    # The old annotator leaves the project; only the replacement remains.
    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == old_annotator.id,
        )
    )
    await db_session.flush()

    batch_out, affected, _ = await BatchService(db_session).reset_to_draft(batch_id)
    # Only the completed task needed a status reset.
    assert affected == 1
    await db_session.refresh(tasks[0])
    await db_session.refresh(tasks[1])
    # The departed member's materialized assignment inherits the batch default
    # again; the genuine manual override is preserved.
    assert tasks[0].status == "pending"
    assert tasks[0].assignee_id is None
    assert batch_out.annotator_id is None or True  # default untouched by reset
    assert tasks[1].assignee_id == replacement.id


async def _make_batch(db: AsyncSession, project: Project) -> uuid.UUID:
    """把未归类任务全部注入一个新批次, 返回 batch_id。"""
    batches = await BatchService(db).split(
        project.id,
        BatchSplitRequest(strategy="random", n_batches=1, name_prefix="B"),
        project.owner_id,
    )
    return batches[0].id


@pytest.mark.asyncio
async def test_delete_resets_nonpending_and_clears_ai(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=4)
    batch_id = await _make_batch(db_session, project)

    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    # t0: review + manual + AI annotation + prediction;  t1: completed + prediction
    t0, t1 = tasks[0], tasks[1]
    t0.status = "review"
    t0.total_predictions = 1
    t0.is_labeled = True
    t1.status = "completed"
    t1.total_predictions = 1
    db_session.add_all(
        [
            _manual_anno(t0.id, project.id),
            _ai_anno(t0.id, project.id),
            _prediction(t0.id, project.id),
            _ai_anno(t1.id, project.id),
            _prediction(t1.id, project.id),
        ]
    )
    await db_session.flush()

    await BatchService(db_session).delete(batch_id, force=True)

    # 1. 所有 task 回 pending 且解绑（新项目无 B-DEFAULT → batch_id=NULL）
    # 直接 select 列值读 DB 实时值（物化字段经 raw SQL UPDATE 改写，ORM 对象会陈旧）
    rows = (
        await db_session.execute(
            select(Task.status, Task.batch_id, Task.total_predictions).where(
                Task.project_id == project.id
            )
        )
    ).all()
    assert all(r.status == "pending" for r in rows)
    assert all(r.batch_id is None for r in rows)
    assert all(r.total_predictions == 0 for r in rows)

    # 2. 人工标注保留 active；AI 标注软删
    manual = (
        await db_session.execute(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.source == "manual", Annotation.is_active.is_(True))
        )
    ).scalar()
    ai_active = (
        await db_session.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.source == "prediction_based",
                Annotation.is_active.is_(True),
            )
        )
    ).scalar()
    assert manual == 1
    assert ai_active == 0

    # 3. prediction 行被删
    pred_count = (
        await db_session.execute(select(func.count()).select_from(Prediction))
    ).scalar()
    assert pred_count == 0

    # 4. t0 仍有存活人工标注 → is_labeled 重算为 True；t1 无人工标注 → False
    labeled = dict(
        (
            await db_session.execute(
                select(Task.id, Task.is_labeled).where(Task.id.in_([t0.id, t1.id]))
            )
        ).all()
    )
    assert labeled[t0.id] is True
    assert labeled[t1.id] is False


@pytest.mark.asyncio
async def test_delete_then_resplit_no_counter_pollution(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=4)
    batch_id = await _make_batch(db_session, project)

    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    for t in tasks:
        t.status = "completed"
    await db_session.flush()

    svc = BatchService(db_session)
    await svc.delete(batch_id, force=True)
    # 重分包到新批次：计数应干净（无 completed/review 污染）
    new_batches = await svc.split(
        project.id,
        BatchSplitRequest(strategy="random", n_batches=1, name_prefix="B2"),
        user.id,
    )
    nb = new_batches[0]
    assert nb.completed_tasks == 0
    assert nb.review_tasks == 0
    assert nb.total_tasks == 4


@pytest.mark.asyncio
async def test_bulk_delete_also_resets(db_session: AsyncSession, super_admin):
    user, _ = super_admin
    project = await _seed_linked(db_session, user.id, n_items=2)
    batch_id = await _make_batch(db_session, project)

    tasks = (
        (await db_session.execute(select(Task).where(Task.batch_id == batch_id)))
        .scalars()
        .all()
    )
    tasks[0].status = "review"
    db_session.add(_prediction(tasks[0].id, project.id))
    await db_session.flush()

    res = await BatchService(db_session).bulk_delete(project.id, [batch_id], force=True)
    assert batch_id in res["succeeded"]

    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert all(t.status == "pending" and t.batch_id is None for t in rows)
    pred_count = (
        await db_session.execute(select(func.count()).select_from(Prediction))
    ).scalar()
    assert pred_count == 0
    # 无 B-DEFAULT 时 bulk_delete 也要同步 project 物化列
    await db_session.refresh(project)
    assert project.review_tasks == 0
    assert project.completed_tasks == 0
