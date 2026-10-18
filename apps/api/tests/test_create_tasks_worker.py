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
from app.services.dataset import DatasetService
from app.services.labeling_tasks import LabelingTaskService
from app.services.task_creation_contract import TaskCreationFailure
from app.services.task_creation_jobs import TaskCreationJobs


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
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
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
        dataset_id=ds.id, project_id=project.id, actor_id=user.id
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
        ds.id, project.id, actor_id=user.id
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
        ds.id, project.id, actor_id=user.id
    )
    assert result.async_job_id is not None
    assert result.created_tasks == 0


async def test_repeated_link_recovers_the_same_accepted_job(
    db_session: AsyncSession, super_admin
):
    user, _ = super_admin
    ds = await _seed_dataset(db_session, user.id, n_items=2)
    project = await _seed_project(db_session, user.id)
    await SystemSettingsService.set_many(
        db_session, {"task_create_sync_threshold": 0}, actor_id=user.id
    )
    workflow = DatasetTaskCreationWorkflow(db_session)
    first = await workflow.link_project(ds.id, project.id, actor_id=user.id)
    await db_session.commit()
    recovered = await workflow.link_project(ds.id, project.id, actor_id=user.id)
    assert recovered.link_id == first.link_id
    assert recovered.async_job_id == first.async_job_id is not None
    claim = await TaskCreationJobs(db_session).claim(first.async_job_id, "recovery")
    assert claim.actor_id == user.id and claim.association_id == first.link_id
    assert claim.operation_id == first.async_job_id


async def test_new_manager_can_cancel_after_a_committed_chunk(
    db_session: AsyncSession, project_admin, httpx_client
):
    from app.core.security import create_access_token
    from app.db.models.user import User

    owner, _ = project_admin
    ds = await _seed_dataset(db_session, owner.id, n_items=2)
    project = await _seed_project(db_session, owner.id)
    association = await DatasetService(db_session).associate_project(
        ds.id, project.id, actor_id=owner.id
    )
    operation_id = uuid.uuid4()
    jobs = TaskCreationJobs(db_session)
    await jobs.create(
        operation_id=operation_id,
        actor_id=owner.id,
        dataset_id=ds.id,
        project_id=project.id,
        association_id=association.link_id,
    )
    labeling = LabelingTaskService(db_session)
    await labeling.accept_dataset_task_creation(
        operation_id=operation_id,
        actor_id=owner.id,
        dataset_id=ds.id,
        project_id=project.id,
        association_id=association.link_id,
        job_id=operation_id,
        mode="generic",
        chunk_size=1,
    )
    await labeling.prepare_dataset_task_creation(operation_id=operation_id, frames=None)
    first = await labeling.advance_dataset_task_creation(operation_id, 0)
    await db_session.commit()
    assert first.cumulative_created == 1 and first.complete is False

    manager = User(
        id=uuid.uuid4(),
        email=f"creation-manager-{uuid.uuid4().hex}@test.local",
        name="New manager",
        role="project_admin",
        password_hash="unused-test-hash",
        is_active=True,
    )
    db_session.add(manager)
    await db_session.flush()
    project.owner_id = manager.id
    owner.is_active = False
    await db_session.commit()
    token = create_access_token(subject=str(manager.id), role=manager.role)
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{operation_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    with pytest.raises(TaskCreationFailure) as blocked:
        await labeling.advance_dataset_task_creation(operation_id, 1)
    assert blocked.value.code == "job_cancelled"
    assert (
        await db_session.scalar(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        )
        == 1
    )


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
    db_session.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
    await db_session.flush()

    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(
        dataset_id=dataset.id, project_id=project.id, actor_id=user.id
    )

    assert result == {"created": item_count, "total": item_count}
    count = await db_session.scalar(
        select(func.count()).select_from(Task).where(Task.project_id == project.id)
    )
    await db_session.refresh(project)
    assert count == project.total_tasks == item_count
