from __future__ import annotations

import uuid
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_workspace import lock_managed_project

from app.services.annotation import AnnotationService
from app.services.project_workspace import rename_class_bindings


async def rename_project_class(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    old_name: str,
    new_name: str,
    tool_unit_id: str | None,
) -> bool:
    """Compose project binding and annotation class-name updates in one transaction."""
    old = old_name.strip()
    new = new_name.strip()
    if not old or not new:
        raise HTTPException(status_code=400, detail="名称不能为空")
    project = await lock_managed_project(
        db, actor_id=actor_id, project_id=project_id, exclusive=old != new
    )
    if old == new:
        return False

    rename_class_bindings(
        project,
        old_name=old,
        new_name=new,
        tool_unit_id=tool_unit_id,
    )
    await AnnotationService(db).rename_class_name(project.id, old, new)
    return True
