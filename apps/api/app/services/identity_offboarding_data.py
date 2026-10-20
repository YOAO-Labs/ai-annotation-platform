"""Keep offboarding prepare, portable policy and apply in one caller transaction."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    decode_native_offboarding_command,
    decode_native_offboarding_reply,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.identity_session_data import _json_value


class IdentityOffboardingCommandData:
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

    async def _command(self, function, payload):
        try:
            command = decode_native_offboarding_command(payload)
            raw = _json_value(
                await self.db.scalar(
                    text(
                        f"SELECT aap_identity_offboarding_v1.{function}"
                        "(CAST(:command AS jsonb))"
                    ),
                    {
                        "command": json.dumps(
                            command.model_dump(mode="json", by_alias=True),
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    },
                )
            )
            decode_native_offboarding_reply(command, raw)
            return raw
        except SQLAlchemyError:
            raise IdentityUnavailable() from None

    async def prepare_offboarding_command(self, payload: dict) -> dict:
        return await self._command("prepare_offboarding_command", payload)

    async def apply_offboarding_command(self, payload: dict) -> dict:
        return await self._command("apply_offboarding_command", payload)
