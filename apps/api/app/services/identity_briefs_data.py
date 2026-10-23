"""Read Identity profiles in the caller transaction without taking its ownership."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.briefs_contract import (
    decode_native_briefs_command,
    decode_native_briefs_reply,
)
from aap_identity.contract import IdentityUnavailable
from app.services.identity_session_data import _json_value


class IdentityBriefsData:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute_briefs_command(self, payload: dict) -> dict:
        command = decode_native_briefs_command(payload)
        try:
            if self.db.autoflush:
                await self.db.flush()
            raw = _json_value(
                await self.db.scalar(
                    text(
                        "SELECT aap_identity_briefs_v1.execute_briefs_command("
                        "CAST(:command AS jsonb))"
                    ),
                    {
                        "command": json.dumps(
                            command.model_dump(mode="json", by_alias=True),
                            ensure_ascii=True,
                            allow_nan=False,
                        )
                    },
                )
            )
            decode_native_briefs_reply(command, raw)
            return raw
        except SQLAlchemyError:
            raise IdentityUnavailable() from None
