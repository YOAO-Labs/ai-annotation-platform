"""Portable manager for caller-transaction Dataset unlink capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel

from .dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkPreview,
    DatasetUnlinkPreviewCommand,
    DatasetUnlinkReceipt,
    decode_unlink_command,
    decode_unlink_preview_command,
    decode_unlink_reply,
)


class DatasetUnlinkData(Protocol):
    async def unlink_dataset(self, command: DatasetUnlinkCommand) -> object: ...

    async def preview_dataset_unlink(
        self, command: DatasetUnlinkPreviewCommand
    ) -> object: ...


class DatasetUnlinkManager:
    """Validate closed commands and bind unlink receipts to their exact scope."""

    def __init__(self, data: DatasetUnlinkData):
        self.data = data

    async def execute(
        self,
        command: DatasetUnlinkCommand | Mapping[str, object] | BaseModel | bytes | str,
    ) -> DatasetUnlinkReceipt:
        command = decode_unlink_command(command)
        receipt = decode_unlink_reply(await self.data.unlink_dataset(command))
        assert isinstance(receipt, DatasetUnlinkReceipt)
        if (
            receipt.operation_id,
            receipt.actor_id,
            receipt.dataset_id,
            receipt.project_id,
            receipt.cleanup_bucket,
        ) != (
            command.operation_id,
            command.actor_id,
            command.dataset_id,
            command.project_id,
            command.cleanup_bucket,
        ):
            raise ValueError("Unlink receipt does not match its command scope")
        return receipt

    async def preview(
        self,
        command: DatasetUnlinkPreviewCommand
        | Mapping[str, object]
        | BaseModel
        | bytes
        | str,
    ) -> DatasetUnlinkPreview:
        command = decode_unlink_preview_command(command)
        preview = decode_unlink_reply(
            await self.data.preview_dataset_unlink(command), preview=True
        )
        assert isinstance(preview, DatasetUnlinkPreview)
        return preview


__all__ = ["DatasetUnlinkManager"]
