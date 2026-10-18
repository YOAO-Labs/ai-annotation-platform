"""PostgreSQL bindings for Workspace's Dataset unlink operation."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
    decode_unlink_reply,
)


class DatasetUnlinkData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def preview(
        self, *, actor_id: UUID, dataset_id: UUID, project_id: UUID
    ) -> DatasetUnlinkPreview:
        payload = await self.db.scalar(
            text(
                "SELECT aap_workspace_v1.preview_dataset_unlink(CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:project AS uuid))"
            ),
            {"actor": actor_id, "dataset": dataset_id, "project": project_id},
        )
        result = decode_unlink_reply(payload, preview=True)
        assert isinstance(result, DatasetUnlinkPreview)
        return result

    async def unlink(self, command: DatasetUnlinkCommand) -> DatasetUnlinkReceipt:
        await self.db.flush()
        payload = await self.db.scalar(
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
        result = decode_unlink_reply(payload)
        assert isinstance(result, DatasetUnlinkReceipt)
        if (
            result.operation_id,
            result.actor_id,
            result.dataset_id,
            result.project_id,
            result.cleanup_bucket,
        ) != (
            command.operation_id,
            command.actor_id,
            command.dataset_id,
            command.project_id,
            command.cleanup_bucket,
        ):
            raise ValueError("Unlink receipt does not match its command scope")
        return result
