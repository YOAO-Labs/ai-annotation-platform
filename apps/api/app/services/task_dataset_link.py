"""v0.13.0 · 点云多文件关联 service。

task 与 dataset_item 的多对一关联（中间表 task_dataset_item_links）。
role 约定：`primary_lidar`（主点云）或 `camera_<name>`（如 camera_front）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites


def _validate_role(role: str) -> None:
    if role != "primary_lidar" and not role.startswith("camera_"):
        raise ValueError(
            f"invalid role {role!r}: must be 'primary_lidar' or start with 'camera_'"
        )


async def link_items(
    session: AsyncSession,
    task_id: uuid.UUID,
    items: list[tuple[uuid.UUID, str, str | None]],
) -> list[TaskDatasetItemLink]:
    """为 task 关联多个 dataset_item。

    items 每项为 (dataset_item_id, role, sensor_name)。逐项校验 role 后
    创建 link 行，flush 后返回。
    """
    if not items:
        await session.flush()
        return []
    links: list[TaskDatasetItemLink] = []
    for dataset_item_id, role, sensor_name in items:
        _validate_role(role)
        link = TaskDatasetItemLink(
            task_id=task_id,
            dataset_item_id=dataset_item_id,
            role=role,
            sensor_name=sensor_name,
        )
        links.append(link)
    task = await get_core_row(session, task_row, task_id)
    if task is None:
        raise ValueError("task not found")
    return await LabelingE2EFixtureWrites(session).insert_task_dataset_item_links(
        project_id=task.project_id,
        task_id=task_id,
        rows=links,
    )


async def get_linked_items(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> list[TaskDatasetItemLink]:
    """返回某 task 的全部关联 link 行。"""
    from sqlalchemy import select

    from app.services.labeling_core_read_relations import (
        execute_core_read,
        task_dataset_item_link_row,
    )

    result = await execute_core_read(
        session,
        select(task_dataset_item_link_row).where(
            task_dataset_item_link_row.task_id == task_id
        ),
    )
    return list(result.scalars().all())
