"""Caller-transaction access to Identity's credential functions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models.user import User
from app.services.identity_gateway import IdentityGatewayUnavailable


def _json_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            raise IdentityGatewayUnavailable() from None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise IdentityGatewayUnavailable() from None
    return value


def _retirement_result(value) -> dict:
    payload = _json_value(value)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "generation", "revoked_key_ids", "revoked_at"}
        or payload.get("schema") != "aap-identity-retirement/v1"
        or type(payload.get("generation")) is not int
        or payload["generation"] < 0
        or not isinstance(payload.get("revoked_key_ids"), list)
        or not isinstance(payload.get("revoked_at"), str)
    ):
        raise IdentityGatewayUnavailable()
    try:
        key_ids = [uuid.UUID(value) for value in payload["revoked_key_ids"]]
        revoked_at = datetime.fromisoformat(
            payload["revoked_at"].replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise IdentityGatewayUnavailable() from None
    if revoked_at.utcoffset() is None:
        raise IdentityGatewayUnavailable()
    return {
        "generation": payload["generation"],
        "revoked_key_ids": key_ids,
        "revoked_at": revoked_at,
    }


class IdentitySessionData:
    """Run Identity-owned functions inside the caller's SQL transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _scalar(self, statement, parameters: dict):
        try:
            return await self.db.scalar(statement, parameters)
        except SQLAlchemyError:
            raise IdentityGatewayUnavailable() from None

    async def read_jwt_principal(
        self,
        actor_id: uuid.UUID,
        jti: uuid.UUID,
        generation: int,
        expires_at: datetime,
        *,
        for_refresh: bool = False,
    ):
        await self.db.flush()
        value = await self._scalar(
            text(
                "SELECT aap_identity_v1.read_jwt_principal("
                ":actor_id, :jti, :generation, :expires_at, :for_refresh)"
            ),
            {
                "actor_id": actor_id,
                "jti": jti,
                "generation": generation,
                "expires_at": expires_at,
                "for_refresh": for_refresh,
            },
        )
        result = _json_value(value)
        if not isinstance(result, dict):
            raise IdentityGatewayUnavailable()
        return result

    async def read_api_key_candidates(
        self, prefix: str, after_id: uuid.UUID | None, limit: int
    ) -> dict:
        value = await self._scalar(
            text(
                "SELECT aap_identity_v1.read_api_key_candidates("
                ":prefix, :after_id, :limit)"
            ),
            {"prefix": prefix, "after_id": after_id, "limit": limit},
        )
        result = _json_value(value)
        if not isinstance(result, dict):
            raise IdentityGatewayUnavailable()
        return result

    async def confirm_api_key_principal(
        self, key_id: uuid.UUID, expected_hash: str
    ) -> dict:
        await self.db.flush()
        value = await self._scalar(
            text(
                "SELECT aap_identity_v1.confirm_api_key_principal("
                ":key_id, :expected_hash)"
            ),
            {"key_id": key_id, "expected_hash": expected_hash},
        )
        result = _json_value(value)
        if not isinstance(result, dict):
            raise IdentityGatewayUnavailable()
        return result

    async def get_credential_generation(self, actor_id: uuid.UUID) -> int:
        value = await self._scalar(
            text("SELECT aap_identity_v1.get_credential_generation(:actor_id)"),
            {"actor_id": actor_id},
        )
        if value is None:
            return 0
        if type(value) is not int or value < 0:
            raise IdentityGatewayUnavailable()
        return value

    async def advance_credential_generation(self, actor_id: uuid.UUID) -> int:
        await self.db.flush()
        value = await self._scalar(
            text("SELECT aap_identity_v1.advance_credential_generation(:actor_id)"),
            {"actor_id": actor_id},
        )
        if type(value) is not int or value < 0:
            raise IdentityGatewayUnavailable()
        generation = value
        for instance in self.db.sync_session.identity_map.values():
            if isinstance(instance, User) and instance.id == actor_id:
                set_committed_value(instance, "credential_generation", generation)
        return generation

    async def revoke_jti(self, jti: uuid.UUID, retain_until: datetime) -> bool:
        await self.db.flush()
        value = await self._scalar(
            text("SELECT aap_identity_v1.revoke_jti(:jti, :retain_until)"),
            {"jti": jti, "retain_until": retain_until},
        )
        if type(value) is not bool:
            raise IdentityGatewayUnavailable()
        return value

    async def is_jti_revoked(self, jti: uuid.UUID) -> bool:
        value = await self._scalar(
            text("SELECT aap_identity_v1.is_jti_revoked(:jti)"), {"jti": jti}
        )
        if type(value) is not bool:
            raise IdentityGatewayUnavailable()
        return value

    async def retire_account_credentials(self, actor_id: uuid.UUID) -> dict:
        await self.db.flush()
        value = await self._scalar(
            text("SELECT aap_identity_v1.retire_account_credentials(:actor_id)"),
            {"actor_id": actor_id},
        )
        result = _retirement_result(value)
        for instance in self.db.sync_session.identity_map.values():
            if isinstance(instance, User) and instance.id == actor_id:
                set_committed_value(
                    instance, "credential_generation", result["generation"]
                )
        return result
