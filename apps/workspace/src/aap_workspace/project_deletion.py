"""Portable manager for the caller-transaction project deletion capability."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .project_deletion_contract import (
    ProjectDeletionCommand,
    ProjectDeletionReceipt,
    decode_project_deletion_command,
    decode_project_deletion_reply,
)


class ProjectDeletionData(Protocol):
    async def delete_project(self, command: ProjectDeletionCommand) -> object: ...


class ProjectDeletionManager:
    """Validate a closed command and bind its native receipt to the same scope."""

    def __init__(self, data: ProjectDeletionData):
        self.data = data

    async def execute(
        self, command: ProjectDeletionCommand | Mapping[str, object] | bytes | str
    ) -> ProjectDeletionReceipt:
        command = decode_project_deletion_command(command)
        receipt = decode_project_deletion_reply(await self.data.delete_project(command))
        if (receipt.operation_id, receipt.actor_id, receipt.project_id) != (
            command.operation_id,
            command.actor_id,
            command.project_id,
        ):
            raise ValueError("Deletion receipt does not match its command scope")
        return receipt


__all__ = ["ProjectDeletionManager"]
