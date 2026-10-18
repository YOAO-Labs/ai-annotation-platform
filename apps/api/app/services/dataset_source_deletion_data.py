"""PostgreSQL binding for the Assets-owned source deletion transaction."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset_source_deletion_contract import (
    DatasetSourceDeletionCommand,
    DatasetSourceDeletionReceipt,
    decode_source_deletion_reply,
)


class DatasetSourceDeletionData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def delete(
        self, command: DatasetSourceDeletionCommand
    ) -> DatasetSourceDeletionReceipt:
        await self.db.flush()
        function = (
            "delete_dataset" if command.item_id is None else "delete_dataset_item"
        )
        item_argument = "CAST(:item AS uuid), " if command.item_id is not None else ""
        payload = await self.db.scalar(
            text(
                f"SELECT aap_assets_v1.{function}(CAST(:operation AS uuid), "
                f"CAST(:actor AS uuid), CAST(:dataset AS uuid), {item_argument}:bucket, CAST(:audit AS jsonb))"
            ),
            {
                "operation": command.operation_id,
                "actor": command.actor_id,
                "dataset": command.dataset_id,
                "item": command.item_id,
                "bucket": command.cleanup_bucket,
                "audit": command.audit_context.model_dump_json()
                if command.audit_context
                else None,
            },
        )
        receipt = decode_source_deletion_reply(payload)
        if (
            receipt.operation_id,
            receipt.actor_id,
            receipt.dataset_id,
            receipt.item_id,
            receipt.cleanup_bucket,
        ) != (
            command.operation_id,
            command.actor_id,
            command.dataset_id,
            command.item_id,
            command.cleanup_bucket,
        ):
            raise ValueError("Source deletion receipt does not match its command scope")
        return receipt
