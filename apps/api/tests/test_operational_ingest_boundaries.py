from __future__ import annotations

import ast
from pathlib import Path
import uuid

import pytest
from aap_assets_source.operator_contract import (
    CreateOperatorImageDatasetRequest,
    OperatorSourceEntry,
)
from sqlalchemy import func, select

from app.application.dataset_reset import DatasetResetWorkflow
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.core.security import verify_password
from app.db.enums import UserRole
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.task import Task
from app.db.models.user import User
from app.services.assets_operator import AssetsOperatorGateway
from app.services.dataset import DatasetService
from app.services.identity_commands import IdentityCommandService
from tests.factory import create_project


async def _create_operator_image_dataset(db, *, name: str, description: str, owner_id):
    return await AssetsOperatorGateway(db).create_operator_image_dataset(
        CreateOperatorImageDatasetRequest(
            operation_id=uuid.uuid4(),
            created_by=owner_id,
            name=name,
            description=description,
        )
    )


@pytest.mark.asyncio
async def test_deferred_image_import_items_keep_dataset_count(db_session, super_admin):
    owner, _ = super_admin
    created = await _create_operator_image_dataset(
        db_session,
        name="operational image import",
        description="from local images",
        owner_id=owner.id,
    )
    dataset = created.dataset
    await AssetsOperatorGateway(db_session).append_all_items(
        dataset.id,
        [
            OperatorSourceEntry(
                id=uuid.uuid4(),
                file_name=filename,
                file_path=f"custom-folder/{filename}",
                file_type="image",
                file_size=17,
                metadata={},
            )
            for filename in ("one.png", "two.png")
        ],
    )

    stored_items = await db_session.scalar(
        select(func.count())
        .select_from(DatasetItem)
        .where(DatasetItem.dataset_id == dataset.id)
    )
    file_count = await db_session.scalar(
        select(Dataset.file_count).where(Dataset.id == dataset.id)
    )
    assert file_count == stored_items == 2


@pytest.mark.asyncio
async def test_generic_task_creation_can_stay_in_callers_transaction(
    db_session, super_admin
):
    owner, _ = super_admin
    created = await _create_operator_image_dataset(
        db_session,
        name="generic transaction dataset",
        description="caller-owned task transaction",
        owner_id=owner.id,
    )
    dataset = created.dataset
    await AssetsOperatorGateway(db_session).append_all_items(
        dataset.id,
        [
            OperatorSourceEntry(
                id=uuid.uuid4(),
                file_name="frame.jpg",
                file_path="generic/frame.jpg",
                file_type="image",
                file_size=23,
                metadata={},
            )
        ],
    )
    project = await create_project(db_session, owner_id=owner.id)
    project.data_type = "image"
    await DatasetService(db_session).associate_project(
        dataset.id, project.id, actor_id=owner.id
    )
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
    created = await _create_operator_image_dataset(
        db_session,
        name="reset target",
        description="database reset contract",
        owner_id=owner.id,
    )
    dataset = created.dataset
    (item,) = await AssetsOperatorGateway(db_session).append_all_items(
        dataset.id,
        [
            OperatorSourceEntry(
                id=uuid.uuid4(),
                file_name="frame.jpg",
                file_path="reset-target/frame.jpg",
                file_type="image",
                file_size=23,
                metadata={},
            )
        ],
    )
    project = await create_project(db_session, owner_id=owner.id)
    await DatasetService(db_session).associate_project(
        dataset.id, project.id, actor_id=owner.id
    )
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
    if existing_admin is None:
        with pytest.raises(RuntimeError):
            await commands.ensure_import_admin(environment="production")
    admin_id = await commands.ensure_import_admin(environment="test")
    admin = await db_session.get(User, admin_id, populate_existing=True)
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
    assert replayed == admin.id


def test_operator_importers_do_not_use_retired_dataset_item_writers():
    scripts = Path(__file__).parents[1] / "scripts"
    retired_methods = {
        "create_local_image_import_dataset",
        "ensure_nuscenes_dataset",
        "add_item",
        "set_nuscenes_export_context",
        "backfill_camera_dimensions",
        "refresh_imported_item_content",
        "record_uploaded_item_content",
        "recount_items",
    }
    for script in ("import_images.py", "import_nuscenes_scene.py"):
        source = (scripts / script).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls.intersection(retired_methods), script
        assert "AssetsOperatorGateway" in source
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.db.models.dataset"
            and any(alias.name in {"Dataset", "DatasetItem"} for alias in node.names)
            for node in ast.walk(tree)
        ), script

    assert all(not hasattr(DatasetService, name) for name in retired_methods)
