from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.application.dataset_reset import DatasetResetWorkflow
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.core.security import verify_password
from app.db.enums import UserRole
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.task import Task
from app.db.models.user import User
from app.services.dataset import DatasetService
from app.services.identity_commands import IdentityCommandService
from tests.factory import create_project


@pytest.mark.asyncio
async def test_deferred_image_import_items_keep_dataset_count(db_session, super_admin):
    owner, _ = super_admin
    assets = DatasetService(db_session)
    dataset = await assets.create_local_image_import_dataset(
        name="operational image import",
        description="from local images",
        created_by=owner.id,
    )
    for filename in ("one.png", "two.png"):
        await assets.add_item(
            dataset_id=dataset.id,
            file_name=filename,
            file_path=f"custom-folder/{filename}",
            file_type="image",
            file_size=17,
            flush=False,
        )
    await db_session.flush()
    await db_session.refresh(dataset, ["file_count"])

    stored_items = await db_session.scalar(
        select(func.count())
        .select_from(DatasetItem)
        .where(DatasetItem.dataset_id == dataset.id)
    )
    assert dataset.file_count == stored_items == 2


@pytest.mark.asyncio
async def test_generic_task_creation_can_stay_in_callers_transaction(
    db_session, super_admin
):
    owner, _ = super_admin
    assets = DatasetService(db_session)
    dataset = await assets.create_local_image_import_dataset(
        name="generic transaction dataset",
        description="caller-owned task transaction",
        created_by=owner.id,
    )
    await assets.add_item(
        dataset_id=dataset.id,
        file_name="frame.jpg",
        file_path="generic/frame.jpg",
        file_type="image",
        file_size=23,
    )
    project = await create_project(db_session, owner_id=owner.id)
    project.data_type = "image"
    await assets.associate_project(dataset.id, project.id, actor_id=owner.id)
    await db_session.flush()

    project_id = project.id
    result = await DatasetTaskCreationWorkflow(
        db_session
    ).create_tasks_for_linked_dataset(
        dataset_id=dataset.id,
        project_id=project_id,
        commit_task_chunks=False,
        actor_id=owner.id,
    )
    assert result == {"created": 1, "total": 1}
    await db_session.refresh(project, ["total_tasks"])
    assert project.total_tasks == 1

    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_dataset_reset_detaches_tasks_and_deletes_database_assets(
    db_session, super_admin
):
    owner, _ = super_admin
    assets = DatasetService(db_session)
    dataset = await assets.create_local_image_import_dataset(
        name="reset target",
        description="database reset contract",
        created_by=owner.id,
    )
    item = await assets.add_item(
        dataset_id=dataset.id,
        file_name="frame.jpg",
        file_path="reset-target/frame.jpg",
        file_type="image",
        file_size=23,
    )
    project = await create_project(db_session, owner_id=owner.id)
    await assets.associate_project(dataset.id, project.id, actor_id=owner.id)
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-RESET-{uuid.uuid4().hex[:8]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item.file_type,
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    workflow = DatasetResetWorkflow(db_session)

    await workflow.reset()
    await db_session.flush()
    await db_session.refresh(task)

    assert task.dataset_item_id is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectDataset)
            .where(ProjectDataset.dataset_id == dataset.id)
        )
        == 0
    )
    assert await db_session.get(Dataset, dataset.id) is None


@pytest.mark.asyncio
async def test_import_admin_creation_keeps_existing_identity_semantics(db_session):
    commands = IdentityCommandService(db_session)

    class MissingAdminSession:
        async def scalar(self, _query):
            return None

    with pytest.raises(RuntimeError):
        await IdentityCommandService(MissingAdminSession()).ensure_import_admin(
            environment="production"
        )

    existing_admin = await db_session.scalar(select(User).where(User.email == "admin"))
    existing_state = (
        (
            existing_admin.id,
            existing_admin.role,
            existing_admin.is_active,
            existing_admin.password_hash,
        )
        if existing_admin is not None
        else None
    )
    admin = await commands.ensure_import_admin(environment="test")
    assert admin.email == "admin"
    if existing_state is None:
        assert admin.role == UserRole.SUPER_ADMIN.value
        assert admin.is_active is True
        assert verify_password("123456", admin.password_hash)
    else:
        assert (
            admin.id,
            admin.role,
            admin.is_active,
            admin.password_hash,
        ) == existing_state

    replayed = await commands.ensure_import_admin(environment="production")
    assert replayed.id == admin.id
