from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project


async def delete_project(db: AsyncSession, project: Project) -> None:
    await db.delete(project)
