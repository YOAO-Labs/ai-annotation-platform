"""Platform-role capability on the caller's existing transaction."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_identity.platform_role_contract import (
    decode_native_platform_role_command,
    decode_native_platform_role_reply,
)
from app.services.identity_session_data import _json_value


class IdentityPlatformRoleCommandData:
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

    async def execute_platform_role_command(self, payload: dict) -> dict:
        try:
            command = decode_native_platform_role_command(payload)
            await self.db.flush()
            raw = _json_value(
                await self.db.scalar(
                    text(
                        "SELECT aap_identity_platform_roles_v1."
                        "execute_platform_role_command(CAST(:command AS jsonb))"
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
            decode_native_platform_role_reply(command, raw)
            return raw
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise
