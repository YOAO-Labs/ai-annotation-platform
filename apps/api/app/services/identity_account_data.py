"""Identity capabilities on an API caller's existing SQL transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import set_committed_value

from aap_identity.account_contract import (
    AccountFailure,
    decode_native_apply_command,
    decode_native_prepare_command,
    decode_native_prepare_reply,
    decode_native_account_reply,
)
from aap_identity.contract import IdentityUnavailable
from app.db.models.user import User
from app.services.identity_session_data import _json_value


class IdentityAccountCommandData:
    """Keep prepare, portable policy and apply on one caller-owned connection.

    The adapter never loads User rows. While legacy ORM coordinators coexist,
    synchronize only instances the caller already holds after native mutations.
    """

    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        try:
            await self.db.flush()
            yield self
            await self.db.commit()
        except AccountFailure:
            # Policy rejections precede any mutation, as in the original HTTP
            # handlers. The request's session closes its read/lock transaction.
            raise
        except BaseException:
            await self.db.rollback()
            raise

    async def _scalar(self, function: str, command):
        def encode(value):
            if isinstance(value, (datetime, uuid.UUID)):
                return value.isoformat() if isinstance(value, datetime) else str(value)
            raise TypeError("Unsupported Identity account native value")

        try:
            return _json_value(
                await self.db.scalar(
                    text(
                        f"SELECT aap_identity_accounts_v1.{function}(CAST(:command AS jsonb))"
                    ),
                    {
                        "command": json.dumps(
                            command.model_dump(mode="python", by_alias=True),
                            default=encode,
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    },
                )
            )
        except SQLAlchemyError:
            raise IdentityUnavailable() from None

    def _sync_profile(self, account):
        values = account.model_dump(mode="python")
        for instance in list(self.db.identity_map.values()):
            if isinstance(instance, User) and instance.id == account.id:
                for name, value in values.items():
                    set_committed_value(instance, name, value)

    def _sync_field(self, actor_id, **values):
        for instance in list(self.db.identity_map.values()):
            if isinstance(instance, User) and instance.id == actor_id:
                for name, value in values.items():
                    set_committed_value(instance, name, value)

    async def prepare_account_command(self, payload: dict) -> dict:
        command = decode_native_prepare_command(payload)
        raw = await self._scalar("prepare_account_command", command)
        reply = decode_native_prepare_reply(command, raw)
        self._sync_profile(reply.data.account)
        return raw

    async def apply_account_command(self, payload: dict) -> dict:
        command = decode_native_apply_command(payload)
        raw = await self._scalar("apply_account_command", command)
        reply = decode_native_account_reply(command, raw)
        if reply.data.account is not None:
            self._sync_profile(reply.data.account)
        elif command.operation == "patch_preferences":
            self._sync_field(command.actor.actor_id, preferences=command.new_value)
        elif command.operation == "change_password":
            self._sync_field(
                command.actor.actor_id,
                password_hash=command.new_value,
                password_admin_reset_at=None,
            )
        return raw


class IdentityPresenceCommandData:
    def __init__(self, db):
        self.db = db

    async def mark_inactive_accounts_offline(self, cutoff) -> int:
        try:
            await self.db.flush()
            affected = await self.db.scalar(
                text(
                    "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline(:cutoff)"
                ),
                {"cutoff": cutoff},
            )
        except SQLAlchemyError:
            raise IdentityUnavailable() from None
        if type(affected) is not int or not 0 <= affected < 2**63:
            raise IdentityUnavailable()
        return affected
