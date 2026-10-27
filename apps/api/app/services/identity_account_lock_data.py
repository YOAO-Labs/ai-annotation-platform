"""Identity account facts fenced by the caller's own database transaction."""

from __future__ import annotations

import uuid
from typing import Iterable

import re

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.account_lock_contract import (
    ACCOUNT_LOCK_PROTOCOL,
    ACCEPTED_API_KEY_PROTOCOL,
    AcceptedApiKeyRequest,
    AccountLockMode,
    AccountLockRequest,
    decode_account_lock_reply,
)
from aap_identity.contract import IdentityUnavailable
from app.services.identity_contracts import (
    AcceptedApiKeyFacts,
    IdentityActorFacts,
    IdentityActorStatus,
)

_LOCK_QUERIES = {
    mode: text(
        f"SELECT aap_identity_v1.lock_accounts_{mode}("
        "CAST(:account_ids AS uuid[]), CAST(:nowait AS boolean))"
    )
    for mode in ("share", "update", "key_share")
}


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="身份认证服务暂不可用")


class IdentityAccountLockData:
    """Read locked values without taking ownership of the caller's transaction."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def accepted_api_key_authorized(
        self,
        actor_id: uuid.UUID,
        api_key_id: uuid.UUID,
        required_scopes: Iterable[str],
    ) -> bool:
        """Retain the accepted key's SHARE fence without reading credential data."""
        request = AcceptedApiKeyRequest(
            schema=ACCEPTED_API_KEY_PROTOCOL,
            actor_id=actor_id,
            api_key_id=api_key_id,
            required_scopes=sorted(set(required_scopes)),
        )
        if self.db.autoflush:
            await self.db.flush()
        authorized = await self.db.scalar(
            text(
                "SELECT aap_identity_v1.check_accepted_api_key("
                "CAST(:actor_id AS uuid), CAST(:api_key_id AS uuid), "
                "CAST(:required_scopes AS text[]))"
            ),
            {
                "actor_id": request.actor_id,
                "api_key_id": request.api_key_id,
                "required_scopes": request.required_scopes,
            },
        )
        if type(authorized) is not bool:
            raise _unavailable()
        return authorized

    async def lock_accounts(
        self,
        account_ids: Iterable[uuid.UUID],
        *,
        mode: AccountLockMode,
        nowait: bool = True,
    ) -> dict[uuid.UUID, IdentityActorFacts]:
        request = AccountLockRequest(
            schema=ACCOUNT_LOCK_PROTOCOL,
            account_ids=sorted(set(account_ids)),
            mode=mode,
            nowait=nowait,
        )
        # Text SQL does not trigger ORM autoflush. Keep the original query's
        # visibility without overriding a surrounding no_autoflush scope.
        if self.db.autoflush:
            await self.db.flush()
        raw = await self.db.scalar(
            _LOCK_QUERIES[request.mode],
            {"account_ids": request.account_ids, "nowait": request.nowait},
        )
        try:
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            elif isinstance(raw, (bytearray, memoryview)):
                raw = bytes(raw)
            records = decode_account_lock_reply(raw, request)
        except (IdentityUnavailable, UnicodeError):
            raise _unavailable() from None
        return {
            record.id: IdentityActorFacts(**record.model_dump()) for record in records
        }

    async def read_accepted_api_key_facts(
        self, actor_id: uuid.UUID, api_key_id: uuid.UUID
    ) -> AcceptedApiKeyFacts | None:
        """Current non-secret facts of one accepted key, under its SHARE fence."""
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.execute(
            text(
                "SELECT scopes, expires_at, revision "
                "FROM aap_identity_v1.read_accepted_api_key_facts("
                "CAST(:actor_id AS uuid), CAST(:api_key_id AS uuid))"
            ),
            {"actor_id": actor_id, "api_key_id": api_key_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        scopes, expires_at, revision = (
            row["scopes"],
            row["expires_at"],
            row["revision"],
        )
        if (
            not isinstance(scopes, list)
            or any(not isinstance(scope, str) for scope in scopes)
            or not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{64}", revision) is None
            or (expires_at is not None and expires_at.utcoffset() is None)
        ):
            raise _unavailable()
        return AcceptedApiKeyFacts(tuple(scopes), expires_at, revision)

    async def read_actor_status(
        self, actor_id: uuid.UUID
    ) -> IdentityActorStatus | None:
        """Reuse the existing minimum account-status capability without a lock."""
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.execute(
            text(
                "SELECT actor_id, platform_role, is_active "
                "FROM aap_identity_v1.read_actor_facts(CAST(:actor_id AS uuid))"
            ),
            {"actor_id": actor_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if (
            row["actor_id"] != actor_id
            or not isinstance(row["platform_role"], str)
            or type(row["is_active"]) is not bool
        ):
            raise _unavailable()
        return IdentityActorStatus(
            id=row["actor_id"],
            role=row["platform_role"],
            is_active=row["is_active"],
        )
