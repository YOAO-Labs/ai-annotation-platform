"""Borrowed-transaction PostgreSQL binding for Dataset unlink."""

from __future__ import annotations

from aap_owner_data.postgres import verify_function_only_role

from .dataset_unlink_contract import (
    NATIVE_SCHEMA,
    DatasetUnlinkCommand,
    DatasetUnlinkPreviewCommand,
    decode_unlink_command,
    decode_unlink_preview_command,
)

_DATASET_UNLINK_FUNCTIONS = {
    ("unlink_dataset", "uuid, uuid, uuid, uuid, text, jsonb"),
    ("preview_dataset_unlink", "uuid, uuid, uuid"),
}
_UNLINK_DATASET_SQL = (
    f"SELECT {NATIVE_SCHEMA}.unlink_dataset("
    "$1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::text, $6::jsonb)"
)
_PREVIEW_DATASET_UNLINK_SQL = (
    f"SELECT {NATIVE_SCHEMA}.preview_dataset_unlink($1::uuid, $2::uuid, $3::uuid)"
)


class DatasetUnlinkPostgresData:
    """Call the existing VOLATILE Native functions on a borrowed transaction."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _require_active_connection(connection) -> None:
        is_closed = getattr(connection, "is_closed", None)
        is_in_transaction = getattr(connection, "is_in_transaction", None)
        if not callable(is_closed) or not callable(is_in_transaction) or is_closed():
            raise ValueError("Dataset unlink requires an open asyncpg connection")
        if not is_in_transaction():
            raise ValueError("Dataset unlink requires an active caller transaction")

    async def _verify_active_connection(self) -> None:
        connection = self.connection
        self._require_active_connection(connection)
        await verify_function_only_role(
            connection,
            schema=NATIVE_SCHEMA,
            functions=_DATASET_UNLINK_FUNCTIONS,
            role="workspace_dataset_unlink_client",
        )
        self._require_active_connection(connection)

    async def unlink_dataset(self, command: DatasetUnlinkCommand) -> object:
        command = decode_unlink_command(command)
        await self._verify_active_connection()
        audit_context = (
            command.audit_context.model_dump_json()
            if command.audit_context is not None
            else None
        )
        return await self.connection.fetchval(
            _UNLINK_DATASET_SQL,
            command.operation_id,
            command.actor_id,
            command.dataset_id,
            command.project_id,
            command.cleanup_bucket,
            audit_context,
        )

    async def preview_dataset_unlink(
        self, command: DatasetUnlinkPreviewCommand
    ) -> object:
        command = decode_unlink_preview_command(command)
        await self._verify_active_connection()
        return await self.connection.fetchval(
            _PREVIEW_DATASET_UNLINK_SQL,
            command.actor_id,
            command.dataset_id,
            command.project_id,
        )


__all__ = ["DatasetUnlinkPostgresData"]
