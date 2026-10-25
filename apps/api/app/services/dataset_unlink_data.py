"""Borrowed PostgreSQL adapter for the Workspace Dataset unlink command."""

from uuid import UUID

from aap_workspace.dataset_unlink import DatasetUnlinkManager
from aap_workspace.dataset_unlink_contract import DatasetUnlinkPreviewCommand
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
)


class DatasetUnlinkData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def preview_dataset_unlink(
        self, command: DatasetUnlinkPreviewCommand
    ) -> object:
        return await self.db.scalar(
            text(
                "SELECT aap_workspace_v1.preview_dataset_unlink(CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:project AS uuid))"
            ),
            {
                "actor": command.actor_id,
                "dataset": command.dataset_id,
                "project": command.project_id,
            },
        )

    async def unlink_dataset(self, command: DatasetUnlinkCommand) -> object:
        await self.db.flush()
        return await self.db.scalar(
            text(
                "SELECT aap_workspace_v1.unlink_dataset(CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:project AS uuid), :bucket, CAST(:audit AS jsonb))"
            ),
            {
                "operation": command.operation_id,
                "actor": command.actor_id,
                "dataset": command.dataset_id,
                "project": command.project_id,
                "bucket": command.cleanup_bucket,
                "audit": command.audit_context.model_dump_json()
                if command.audit_context is not None
                else None,
            },
        )

    async def preview(
        self, *, actor_id: UUID, dataset_id: UUID, project_id: UUID
    ) -> DatasetUnlinkPreview:
        command = DatasetUnlinkPreviewCommand(
            actor_id=actor_id,
            dataset_id=dataset_id,
            project_id=project_id,
        )
        return await DatasetUnlinkManager(self).preview(command)

    async def unlink(self, command: DatasetUnlinkCommand) -> DatasetUnlinkReceipt:
        return await DatasetUnlinkManager(self).execute(command)
