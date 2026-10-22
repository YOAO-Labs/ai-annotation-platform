"""Borrowed PostgreSQL adapter for the Workspace project deletion command."""

from uuid import UUID

from aap_workspace.project_deletion import ProjectDeletionManager
from aap_workspace.project_deletion_contract import (
    ProjectDeletionCommand,
    ProjectDeletionReceipt,
)
from aap_audit_contract import ProjectDeletionAuditContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ProjectDeletionData:
    """Adapt the native Workspace capability to the caller's SQLAlchemy session."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def delete_project(self, command: ProjectDeletionCommand) -> object:
        """Invoke the owner function in the caller's current transaction."""
        await self.db.flush()
        return await self.db.scalar(
            text(
                "SELECT aap_workspace_v1.delete_project("
                "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
                "CAST(:project_id AS uuid), CAST(:audit_context AS jsonb))"
            ),
            {
                "operation_id": command.operation_id,
                "actor_id": command.actor_id,
                "project_id": command.project_id,
                "audit_context": command.audit_context.model_dump_json()
                if command.audit_context is not None
                else None,
            },
        )

    async def delete(
        self,
        *,
        operation_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        audit_context: ProjectDeletionAuditContext | None = None,
    ) -> ProjectDeletionReceipt:
        """Preserve the API adapter while delegating command semantics to Workspace."""
        command = ProjectDeletionCommand(
            operation_id=operation_id,
            actor_id=actor_id,
            project_id=project_id,
            audit_context=audit_context,
        )
        return await ProjectDeletionManager(self).execute(command)
