"""PostgreSQL client for the Workspace project deletion command."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_audit_contract import ProjectDeletionAuditContext
from app.services.project_deletion_contract import (
    ProjectDeletionReceipt,
    decode_project_deletion_reply,
)


class ProjectDeletionData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def delete(
        self,
        *,
        operation_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        audit_context: ProjectDeletionAuditContext | None = None,
    ) -> ProjectDeletionReceipt:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_workspace_v1.delete_project("
                "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
                "CAST(:project_id AS uuid), CAST(:audit_context AS jsonb))"
            ),
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "project_id": project_id,
                "audit_context": audit_context.model_dump_json()
                if audit_context is not None
                else None,
            },
        )
        receipt = decode_project_deletion_reply(payload)
        if (receipt.operation_id, receipt.actor_id, receipt.project_id) != (
            operation_id,
            actor_id,
            project_id,
        ):
            raise ValueError("Deletion receipt does not match its command scope")
        return receipt
