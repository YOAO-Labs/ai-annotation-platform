from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_workspace.project_query_contract import ProjectClassRenameEvidence
from app.application.project_workspace import lock_managed_project_context

from app.services.annotation import AnnotationService
from app.services.project_workspace import rename_class_bindings
from app.services.identity_contracts import IdentityActorFacts


async def rename_project_class(
    db: AsyncSession,
    *,
    actor: IdentityActorFacts,
    project_id: uuid.UUID,
    old_name: str,
    new_name: str,
    tool_unit_id: str | None,
) -> tuple[bool, ProjectClassRenameEvidence]:
    """Compose project binding and annotation class-name updates in one transaction."""
    old = old_name.strip()
    new = new_name.strip()
    if not old or not new:
        raise HTTPException(status_code=400, detail="名称不能为空")
    operation_id = uuid.uuid4()
    locked_actor, project, _access = await lock_managed_project_context(
        db,
        actor_id=actor.id,
        project_id=project_id,
        exclusive=old != new,
    )
    changed = old != new
    if changed:
        rename_class_bindings(
            project,
            old_name=old,
            new_name=new,
            tool_unit_id=tool_unit_id,
        )
        await AnnotationService(db).rename_class_name(project.id, old, new)
    # This is trusted API-coordinator context, not a durable authorization receipt.
    evidence = ProjectClassRenameEvidence(
        source="class_rename",
        operation_id=operation_id,
        source_operation="rename_class",
        actor_id=locked_actor.id,
        project_id=project.id,
        owner_id=project.owner_id,
        platform_role=locked_actor.role,
        is_active=locked_actor.is_active,
    )
    return changed, evidence
