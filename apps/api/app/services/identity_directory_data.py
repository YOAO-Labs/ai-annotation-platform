"""Identity directory capabilities on the caller's existing transaction."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_identity.directory_contract import (
    decode_native_directory_command,
    decode_native_directory_reply,
)
from app.services.identity_session_data import _json_value


class IdentityDirectoryCommandData:
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

    async def execute_directory_command(self, payload: dict) -> dict:
        try:
            command = decode_native_directory_command(payload)
            await self.db.flush()
            raw = _json_value(
                await self.db.scalar(
                    text(
                        "SELECT aap_identity_directory_v1.execute_directory_command("
                        "CAST(:command AS jsonb))"
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
            decode_native_directory_reply(command, raw)
            return raw
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise
