"""Caller-transaction bindings to Identity's deactivation capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_identity.deactivation_contract import (
    decode_native_deactivation_command,
    decode_native_deactivation_reply,
    decode_deactivation_due_request,
    decode_deactivation_due_reply,
    decode_native_deactivation_due_reply,
    native_deactivation_due_command,
)
from app.services.identity_session_data import _json_value


class IdentityDeactivationCommandData:
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

    async def execute_deactivation_command(self, payload: dict) -> dict:
        command = decode_native_deactivation_command(payload)
        raw = _json_value(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_deactivation_v1.execute_deactivation_command("
                    "CAST(:command AS jsonb))"
                ),
                {"command": command.model_dump_json(by_alias=True)},
            )
        )
        decode_native_deactivation_reply(command, raw)
        return raw


class IdentityDeactivationDueData:
    def __init__(self, db):
        self.db = db

    async def process_due_deactivations(self, request):
        try:
            parsed = decode_deactivation_due_request(
                request.model_dump(mode="json", by_alias=True)
                if hasattr(request, "model_dump")
                else request
            )
            await self.db.flush()
            command = native_deactivation_due_command(parsed)
            raw = _json_value(
                await self.db.scalar(
                    text(
                        "SELECT aap_identity_deactivation_v1.process_due_deactivations("
                        "CAST(:command AS jsonb))"
                    ),
                    {"command": command.model_dump_json(by_alias=True)},
                )
            )
            native = decode_native_deactivation_due_reply(parsed, raw)
            reply = decode_deactivation_due_reply(
                parsed,
                {
                    **native.model_dump(mode="json", by_alias=True),
                    "schema": "aap-identity-deactivation-due/v1",
                },
            )
            await self.db.commit()
            return reply
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise
