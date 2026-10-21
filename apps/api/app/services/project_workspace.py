"""API-side import and adjacent project write helpers."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project


def set_point_cloud_quality_config(*, project: Project, config: dict[str, Any]) -> None:
    project.point_cloud_quality_config = config


async def clear_primary_backend_binding_for_pool(
    db: AsyncSession, pool_id: UUID
) -> None:
    await db.execute(
        update(Project)
        .where(Project.ml_backend_pool_id == pool_id)
        .values(ml_backend_pool_id=None)
    )


async def find_import_project(db: AsyncSession, display_id: str) -> Project | None:
    return await db.scalar(select(Project).where(Project.display_id == display_id))


async def create_import_project(
    db: AsyncSession,
    *,
    display_id: str,
    name: str,
    owner_id: uuid.UUID,
    tool_bindings: dict | None,
) -> Project:
    project = Project(
        id=uuid.uuid4(),
        display_id=display_id,
        name=name,
        type_label="点云检测",
        type_key="lidar",
        data_type="lidar",
        scene_mode=True,
        prefer_same_scene_continuation=True,
        owner_id=owner_id,
        tool_bindings=tool_bindings or {},
        ai_enabled=False,
    )
    db.add(project)
    await db.flush()
    return project
