"""Borrowed-transaction PostgreSQL binding for Workspace project deletion."""

from __future__ import annotations

from .project_deletion_contract import (
    NATIVE_SCHEMA,
    ProjectDeletionCommand,
    decode_project_deletion_command,
)

_PROJECT_DELETION_FUNCTIONS = {
    ("delete_project", "uuid, uuid, uuid, jsonb"),
}
_DELETE_PROJECT_SQL = (
    f"SELECT {NATIVE_SCHEMA}.delete_project($1::uuid, $2::uuid, $3::uuid, $4::jsonb)"
)


class ProjectDeletionPostgresData:
    """Execute the native function on a borrowed asyncpg transaction connection."""

    def __init__(self, connection):
        self.connection = connection

    async def delete_project(self, command: ProjectDeletionCommand) -> object:
        command = decode_project_deletion_command(command)
        connection = self.connection
        is_closed = getattr(connection, "is_closed", None)
        is_in_transaction = getattr(connection, "is_in_transaction", None)
        if not callable(is_closed) or not callable(is_in_transaction):
            raise ValueError("Project deletion requires an open asyncpg connection")
        if is_closed():
            raise ValueError("Project deletion requires an open asyncpg connection")
        if not is_in_transaction():
            raise ValueError("Project deletion requires an active caller transaction")

        from aap_owner_data.postgres import verify_function_only_role

        await verify_function_only_role(
            connection,
            schema=NATIVE_SCHEMA,
            functions=_PROJECT_DELETION_FUNCTIONS,
            role="workspace_project_deletion_client",
        )
        if is_closed():
            raise ValueError("Project deletion requires an open asyncpg connection")
        if not is_in_transaction():
            raise ValueError("Project deletion requires an active caller transaction")

        audit_context = (
            command.audit_context.model_dump_json()
            if command.audit_context is not None
            else None
        )
        return await connection.fetchval(
            _DELETE_PROJECT_SQL,
            command.operation_id,
            command.actor_id,
            command.project_id,
            audit_context,
        )


__all__ = ["ProjectDeletionPostgresData"]
