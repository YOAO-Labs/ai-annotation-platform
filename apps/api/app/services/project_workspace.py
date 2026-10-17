from __future__ import annotations

from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project


async def delete_project(db: AsyncSession, project: Project) -> None:
    await db.delete(project)


def transfer_project_owner(*, project: Project, new_owner_id: UUID) -> None:
    """Transfer ownership on the already-locked project row."""

    project.owner_id = new_owner_id
