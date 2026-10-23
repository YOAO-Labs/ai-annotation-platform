"""Portable mapper for bounded-shape, batched UserBrief reads."""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .briefs_contract import (
    BRIEFS_PROTOCOL,
    BriefsFailure,
    BriefsReply,
    BriefsRequest,
    BriefsUserRecord,
    NativeBriefsCommand,
    NativeBriefsSuccess,
    avatar_initial,
    decode_briefs_reply,
    decode_native_briefs_reply,
    native_briefs_command,
)
from .contract import IdentityUnavailable


class BriefsTransaction(Protocol):
    async def execute_briefs_command(self, payload: dict) -> dict: ...


class BriefsData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[BriefsTransaction]: ...


def _success_payload(operation: str, project_id, users: list[BriefsUserRecord]) -> dict:
    return {
        "schema": BRIEFS_PROTOCOL,
        "ok": True,
        "operation": operation,
        "data": {
            "project_id": str(project_id) if project_id is not None else None,
            "users": [user.model_dump(mode="json", by_alias=True) for user in users],
        },
    }


class BriefsManager:
    def __init__(self, data: BriefsData):
        self.data = data

    async def execute(self, request: BriefsRequest) -> BriefsReply:
        if request.operation == "get_user_briefs":
            project_id = None
            requested_ids = request.payload.user_ids
        else:
            project_id = request.payload.project_id
            requested_ids = request.payload.user_ids

        if not requested_ids:
            return decode_briefs_reply(
                request, _success_payload(request.operation, project_id, [])
            )

        command: NativeBriefsCommand = native_briefs_command(request)
        native_payload = command.model_dump(mode="json", by_alias=True)
        try:
            json.dumps(native_payload, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

        async with self.data.transaction() as transaction:
            try:
                raw = await transaction.execute_briefs_command(native_payload)
                native: NativeBriefsSuccess = decode_native_briefs_reply(command, raw)
            except BriefsFailure:
                # A valid Core request always produces a valid native command.
                # Native rejection here indicates a stale owner contract/config.
                raise IdentityUnavailable() from None

            users = [
                BriefsUserRecord(
                    id=user.id,
                    name=user.name,
                    email=user.email,
                    role=user.role,
                    avatar_initial=avatar_initial(user.name, user.email),
                    avatar_ref=user.avatar_ref,
                )
                for user in native.data.users
            ]
            return decode_briefs_reply(
                request,
                _success_payload(
                    request.operation,
                    native.data.project_id,
                    users,
                ),
            )
