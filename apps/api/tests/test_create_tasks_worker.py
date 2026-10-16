"""v0.12.0 · B4 dataset task-creation workflow coverage.

Directly exercises initial task creation and request-time link branch selection:
1. Creates N tasks with no batch_id, updates project.total_tasks, and is idempotent.
2. A small dataset links synchronously; a zero threshold freezes the queued branch.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, func, insert, literal
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services.system_settings_service import SystemSettingsService


async def _seed_dataset(db: AsyncSession, owner_id: uuid.UUID, n_items: int) -> Dataset:
    suffix = uuid.uuid4().hex[:6]
    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-CT-{suffix}",
        name="create_tasks worker test",
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
        display_id=f"P-CT-{suffix}",
        name="create_tasks target",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        total_tasks=0,
    )
    db.add(p)
    await db.flush()
    return p


@pytest.mark.asyncio
async def test_initial_task_creation_is_idempotent(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=12)
    project = await _seed_project(db_session, user.id)

    # The worker receives a dataset/project pair whose link is already committed.
    db_session.add(ProjectDataset(dataset_id=ds.id, project_id=project.id))
    await db_session.flush()

    workflow = DatasetTaskCreationWorkflow(db_session)
    result = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id
    )
    assert result == {"created": 12, "total": 12}

    tasks = (
        await db_session.execute(
            select(Task.batch_id).where(Task.project_id == project.id)
        )
    ).all()
    assert len(tasks) == 12
    assert all(row[0] is None for row in tasks)

    await db_session.refresh(project)
    assert project.total_tasks == 12

    # 幂等：再跑一次不双建。
    again = await workflow.create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project.id
    )
    assert again == {"created": 0, "total": 0}

    count = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert count == 12
    await db_session.refresh(project)
    assert project.total_tasks == 12


@pytest.mark.asyncio
async def test_link_project_small_dataset_is_sync(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    n = min(8, settings.task_create_sync_threshold)
    ds = await _seed_dataset(db_session, user.id, n_items=n)
    project = await _seed_project(db_session, user.id)

    link_result = await DatasetTaskCreationWorkflow(db_session).link_project(
        ds.id, project.id
    )

    assert link_result.async_job_id is None
    assert link_result.created_tasks == n

    count = (
        await db_session.execute(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
    ).scalar()
    assert count == n


async def test_link_project_zero_threshold_is_async(
    db_session: AsyncSession, super_admin
):
    """Zero is a real setting: every non-empty dataset link goes async."""

    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=1)
    project = await _seed_project(db_session, user.id)
    await SystemSettingsService.set_many(
        db_session, {"task_create_sync_threshold": 0}, actor_id=user.id
    )
    await db_session.flush()

    result = await DatasetTaskCreationWorkflow(db_session).link_project(
        ds.id, project.id
    )
    assert result.async_job_id is not None
    assert result.created_tasks == 0


async def test_initial_task_creation_exceeds_database_bind_limit(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    dataset = await _seed_dataset(db_session, user.id, n_items=0)
    project = await _seed_project(db_session, user.id)
    item_count = 33000
    number = func.generate_series(1, item_count).column_valued("number")
    name = func.concat("large-", number, ".jpg")
    await db_session.execute(
        insert(DatasetItem).from_select(
            ["id", "dataset_id", "file_name", "file_path", "file_type"],
            select(
                func.gen_random_uuid(),
                literal(dataset.id),
                name,
                func.concat("large/", name),
                literal("image"),
            ),
        )
    )

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(dataset_id=dataset.id, project_id=project.id)

    assert result == {"created": item_count, "total": item_count}
    count = await db_session.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == project.id)
    )
    await db_session.refresh(project)
    assert count == project.total_tasks == item_count
