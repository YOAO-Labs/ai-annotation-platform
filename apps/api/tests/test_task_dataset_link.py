"""TaskDatasetItemLink 中间表及其 service 测试。"""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_dataset_link as svc

from tests.factory import create_project, create_task, create_user


async def _create_dataset_item(db: AsyncSession, owner_id):
    from app.db.models.dataset import Dataset, DatasetItem

    suffix = secrets.token_hex(3)
    dataset = Dataset(
        display_id=f"DS-{suffix}",
        name=f"ds-{suffix}",
        data_type="pointcloud",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()

    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"{suffix}.pcd",
        file_path=f"pc/{suffix}.pcd",
        file_type="pointcloud",
    )
    db.add(item)
    await db.flush()
    return item


@pytest.fixture
async def task_and_items(db_session: AsyncSession):
    user = await create_user(db_session, "project_admin", "pc@test.local", "PC")
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    items = [await _create_dataset_item(db_session, user.id) for _ in range(3)]
    return task, items


async def test_link_and_get(task_and_items, db_session: AsyncSession):
    task, items = task_and_items
    created = await svc.link_items(
        db_session,
        task.id,
        [
            (items[0].id, "primary_lidar", None),
            (items[1].id, "camera_front", "front"),
            (items[2].id, "camera_rear", "rear"),
        ],
    )
    assert len(created) == 3

    fetched = await svc.get_linked_items(db_session, task.id)
    assert len(fetched) == 3
    roles = {link.role for link in fetched}
    assert roles == {"primary_lidar", "camera_front", "camera_rear"}


async def test_invalid_role_raises(task_and_items, db_session: AsyncSession):
    task, items = task_and_items
    with pytest.raises(ValueError):
        await svc.link_items(db_session, task.id, [(items[0].id, "foo", None)])


async def test_unique_role_per_task(task_and_items, db_session: AsyncSession):
    task, items = task_and_items
    await svc.link_items(db_session, task.id, [(items[0].id, "primary_lidar", None)])
    with pytest.raises(IntegrityError):
        await svc.link_items(
            db_session, task.id, [(items[1].id, "primary_lidar", None)]
        )
