"""Borrow the caller's transaction and preserve the existing lock failure states."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from aap_workspace.owner_transfer import OwnerTransferCommandFailure
from aap_workspace.owner_transfer_contract import (
    OwnerTransferError,
    OwnerTransferUnavailable,
    decode_owner_transfer_policy,
)

_BUSY = {"55P03", "40001", "40P01"}


def _diagnostic(error):
    state, hint = None, ""
    current = error
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        state = (
            state
            or getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
        )
        hint = hint or getattr(current, "hint", "") or ""
        current = getattr(current, "orig", None) or current.__cause__
    return state, hint.split("\n", 1)[0]


def _account_busy():
    return OwnerTransferCommandFailure(
        OwnerTransferError(
            status_code=409, detail="账号正在更新，请刷新后重试", rollback=True
        )
    )


class WorkspaceOwnerTransferData:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self, *, write: bool):
        try:
            try:
                if self.db.autoflush:
                    await self.db.flush()
            except DBAPIError as error:
                await self.db.rollback()
                if _diagnostic(error)[0] in _BUSY:
                    raise _account_busy() from error
                raise
            try:
                policy = await self.db.scalar(
                    text(
                        "SELECT aap_workspace_owner_transfer_v1.owner_transfer_policy()"
                    )
                )
            except SQLAlchemyError:
                raise OwnerTransferUnavailable() from None
            decode_owner_transfer_policy(policy)
            yield self
        except OwnerTransferCommandFailure as error:
            if error.error.rollback:
                await self.db.rollback()
            raise
        except OwnerTransferUnavailable:
            await self.db.rollback()
            raise

    async def transfer_project_owner(self, request: dict):
        try:
            return await self.db.scalar(
                text(
                    "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner"
                    "(CAST(:command AS jsonb))"
                ),
                {"command": json.dumps(request, allow_nan=False)},
            )
        except DBAPIError as error:
            state, marker = _diagnostic(error)
            if marker == "aap.workspace.owner_transfer.account_lock":
                await self.db.rollback()
                if state in _BUSY:
                    raise _account_busy() from error
            elif (
                marker == "aap.workspace.owner_transfer.project_lock" and state in _BUSY
            ):
                raise OwnerTransferCommandFailure(
                    OwnerTransferError(
                        status_code=409,
                        detail={"reason": "project_busy"},
                        rollback=False,
                    )
                ) from error
            raise
