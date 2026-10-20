"""Keep Workspace prepare, policy and apply on the caller's existing session."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from sqlalchemy import text

from aap_workspace.contract import (
    MembershipInvalidRequest,
    NativeMembershipApplyRequest,
    MembershipUnavailable,
    decode_membership_request,
    decode_native_membership_reply,
)


from aap_workspace.manager import MembershipCommandFailure


class WorkspaceMembershipData:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self, *, write: bool):
        try:
            if self.db.autoflush:
                await self.db.flush()
            yield self
            if write:
                await self.db.flush()
                await self.db.commit()
        except MembershipCommandFailure as error:
            if error.error.rollback:
                await self.db.rollback()
            raise

    async def _command(self, function: str, payload: dict):
        try:
            if function == "apply_membership_command":
                payload = NativeMembershipApplyRequest.model_validate_json(
                    json.dumps(payload, ensure_ascii=True, allow_nan=False)
                ).model_dump(mode="json", by_alias=True)
            request = decode_membership_request(
                payload["request"]
                if function == "apply_membership_command"
                else payload
            )
            raw = await self.db.scalar(
                text(
                    f"SELECT aap_workspace_membership_v1.{function}"
                    "(CAST(:command AS jsonb))"
                ),
                {"command": json.dumps(payload, ensure_ascii=True, allow_nan=False)},
            )
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            elif isinstance(raw, (bytearray, memoryview)):
                raw = bytes(raw)
            reply = decode_native_membership_reply(request, raw)
            return reply.model_dump(mode="json", by_alias=True)
        except (
            MembershipInvalidRequest,
            KeyError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            raise MembershipUnavailable() from None

    async def prepare_membership_command(self, payload: dict):
        return await self._command("prepare_membership_command", payload)

    async def apply_membership_command(self, payload: dict):
        return await self._command("apply_membership_command", payload)
