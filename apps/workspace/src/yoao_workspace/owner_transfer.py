"""Portable manager for the single Workspace project-owner transfer."""

from __future__ import annotations

from typing import AsyncContextManager, Protocol

from .owner_transfer_contract import (
    OwnerTransferError,
    OwnerTransferFailure,
    OwnerTransferReply,
    OwnerTransferRequest,
    decode_owner_transfer_reply,
    decode_owner_transfer_request,
    owner_transfer_failure_reply,
)


class OwnerTransferTransaction(Protocol):
    async def transfer_project_owner(
        self, request_json: dict
    ) -> dict | bytes | str: ...


class OwnerTransferData(Protocol):
    def transaction(
        self, *, write: bool
    ) -> AsyncContextManager[OwnerTransferTransaction]: ...


class OwnerTransferCommandFailure(Exception):
    """Business failure that must cross the transaction boundary before reply."""

    def __init__(self, error: OwnerTransferError):
        self.error = error
        super().__init__("Workspace owner transfer rejected")


class OwnerTransferManager:
    def __init__(self, data: OwnerTransferData):
        self.data = data

    async def execute(
        self, request: OwnerTransferRequest | dict | bytes | str
    ) -> OwnerTransferReply:
        request = decode_owner_transfer_request(request)
        try:
            async with self.data.transaction(write=True) as transaction:
                reply = decode_owner_transfer_reply(
                    request,
                    await transaction.transfer_project_owner(
                        request.model_dump(mode="json", by_alias=True)
                    ),
                )
                if isinstance(reply, OwnerTransferFailure):
                    raise OwnerTransferCommandFailure(reply.error)
        except OwnerTransferCommandFailure as exc:
            return owner_transfer_failure_reply(exc.error)
        return reply
