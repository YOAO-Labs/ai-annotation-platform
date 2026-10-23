"""Validate invitation owner replies within the caller's short transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_identity.invitation_contract import (
    decode_native_invitation_apply_command,
    decode_native_invitation_apply_reply,
    decode_native_invitation_prepare_command,
    decode_native_invitation_prepare_reply,
)
from app.services.identity_session_data import _json_value


class IdentityInvitationCommandData:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        try:
            await self.db.flush()
            yield self
            await self.db.commit()
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise

    async def prepare_invitation_command(self, payload: dict) -> dict:
        command = decode_native_invitation_prepare_command(payload)
        raw = _json_value(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_invitations_v1.prepare_invitation_command("
                    "CAST(:command AS jsonb))"
                ),
                {"command": command.model_dump_json(by_alias=True)},
            )
        )
        decode_native_invitation_prepare_reply(command, raw)
        return raw

    async def apply_invitation_command(self, payload: dict) -> dict:
        command = decode_native_invitation_apply_command(payload)
        raw = _json_value(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_invitations_v1.apply_invitation_command("
                    "CAST(:command AS jsonb))"
                ),
                {"command": command.model_dump_json(by_alias=True)},
            )
        )
        decode_native_invitation_apply_reply(command, raw)
        return raw
