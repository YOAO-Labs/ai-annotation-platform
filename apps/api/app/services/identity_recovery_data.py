"""Identity recovery capabilities on an API caller's existing transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import set_committed_value

from aap_identity.contract import IdentityUnavailable
from aap_identity.recovery_contract import (
    RecoveryFailure,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
    decode_native_recovery_prepare_reply,
    decode_native_recovery_reply,
)
from aap_identity.registration_contract import (
    REGISTRATION_STATUS_PROTOCOL,
    RegistrationStatusFallbacks,
    RegistrationStatusRequest,
    decode_registration_status_reply,
)
from app.db.models.user import User
from app.services.identity_session_data import _json_value


class IdentityRecoveryCommandData:
    """Validate before commit and synchronize only already-loaded legacy objects."""

    def __init__(self, db):
        self.db = db

    async def read_registration_status(self, fallbacks: dict):
        validated = RegistrationStatusFallbacks.model_validate(fallbacks)
        try:
            if self.db.autoflush:
                await self.db.flush()
            raw = await self.db.scalar(
                text(
                    "SELECT aap_identity_recovery_v1.read_registration_status(CAST(:fallbacks AS jsonb))"
                ),
                {"fallbacks": validated.model_dump_json()},
            )
        except SQLAlchemyError:
            raise IdentityUnavailable() from None
        return decode_registration_status_reply(
            _json_value(raw),
            RegistrationStatusRequest(
                schema=REGISTRATION_STATUS_PROTOCOL, operation="read_status"
            ),
        )

    @asynccontextmanager
    async def transaction(self):
        try:
            await self.db.flush()
            yield self
            await self.db.commit()
        except RecoveryFailure:
            # Rejected policies precede writes. The request session closes its
            # remaining read/lock transaction, as the original handlers did.
            raise
        except BaseException:
            await self.db.rollback()
            raise

    async def _scalar(self, function, command):
        try:
            return _json_value(
                await self.db.scalar(
                    text(
                        f"SELECT aap_identity_recovery_v1.{function}(CAST(:command AS jsonb))"
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
        except SQLAlchemyError:
            raise IdentityUnavailable() from None

    def _sync_actor(self, actor, **additional):
        if actor is None:
            return
        values = actor.model_dump(mode="python")
        actor_id = values.pop("id")
        values.update(additional)
        for instance in list(self.db.identity_map.values()):
            if isinstance(instance, User) and instance.id == actor_id:
                for name, value in values.items():
                    set_committed_value(instance, name, value)

    async def prepare_recovery_command(self, payload):
        command = decode_native_recovery_prepare_command(payload)
        raw = await self._scalar("prepare_recovery_command", command)
        reply = decode_native_recovery_prepare_reply(command, raw)
        self._sync_actor(reply.data.actor)
        return raw

    async def apply_recovery_command(self, payload):
        command = decode_native_recovery_apply_command(payload)
        raw = await self._scalar("apply_recovery_command", command)
        reply = decode_native_recovery_reply(command, raw)
        additional = {}
        if reply.data.password_changed:
            if not isinstance(command.password_hash, str):
                raise IdentityUnavailable()
            additional = {
                "password_hash": command.password_hash,
                "password_admin_reset_at": None,
            }
        self._sync_actor(reply.data.actor, **additional)
        if not reply.data.replayed and reply.data.result.account is not None:
            account = reply.data.result.account
            for instance in list(self.db.identity_map.values()):
                if isinstance(instance, User) and instance.id == account.id:
                    for name, value in account.model_dump(mode="python").items():
                        set_committed_value(instance, name, value)
        return raw
