"""API key generation and owner-scoped management.

Admission is owned by the Identity runtime. This module retains the current
HTTP management adapter until the API-key management endpoints move there.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models.api_key import ApiKey
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate
from app.services.api_key_contracts import ApiKeyManagementPort

_PREFIX = "ak_"
_PREFIX_LEN = 12  # "ak_" + 9 chars
_TOKEN_RANDOM_BYTES = 24  # token_urlsafe(24) -> 32 chars


def generate_token() -> tuple[str, str]:
    """返回 (plaintext, key_prefix)。plaintext 仅返回一次，不入库。"""
    plaintext = _PREFIX + secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    return plaintext, plaintext[:_PREFIX_LEN]


async def _create_key_for_owner(
    db: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """生成 + 入库；返回 (entity, plaintext)。调用方需 commit。"""
    plaintext, prefix = generate_token()
    key = ApiKey(
        user_id=user_id,
        name=name,
        key_prefix=prefix,
        key_hash=hash_password(plaintext),
        scopes=scopes,
        expires_at=expires_at,
    )
    db.add(key)
    await db.flush()
    await db.refresh(key)
    return key, plaintext


async def _get_owned(db: AsyncSession, user_id, key_id) -> ApiKey | None:
    res = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
    )
    return res.scalar_one_or_none()


def _expires_at_from_days(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


class SqlApiKeyManagement(ApiKeyManagementPort):
    """Manage one owner's keys in the caller's existing SQL transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_keys(self, owner_id: uuid.UUID) -> list[ApiKeyOut]:
        res = await self._db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == owner_id)
            .order_by(ApiKey.created_at.desc())
        )
        keys = list(res.scalars().all())
        return [ApiKeyOut.model_validate(key) for key in keys]

    async def create_key(
        self, owner_id: uuid.UUID, payload: ApiKeyCreate
    ) -> ApiKeyCreated:
        expires_at = _expires_at_from_days(payload.expires_in_days)
        key, plaintext = await _create_key_for_owner(
            self._db, owner_id, payload.name, payload.scopes, expires_at
        )
        await self._db.commit()
        await self._db.refresh(key)
        return ApiKeyCreated(
            **ApiKeyOut.model_validate(key).model_dump(), plaintext=plaintext
        )

    async def update_key(
        self, owner_id: uuid.UUID, key_id: uuid.UUID, payload: ApiKeyUpdate
    ) -> ApiKeyOut | None:
        fields = payload.model_fields_set
        expires_at = None
        if "expires_in_days" in fields:
            # Keep the original route's clock read before its owned-row query.
            expires_at = _expires_at_from_days(payload.expires_in_days)
        key = await _get_owned(self._db, owner_id, key_id)
        if key is None or key.revoked_at is not None:
            return None
        if "name" in fields:
            key.name = payload.name
        if "scopes" in fields:
            key.scopes = payload.scopes
        if "expires_in_days" in fields:
            key.expires_at = expires_at
        await self._db.flush()
        await self._db.refresh(key)
        await self._db.commit()
        await self._db.refresh(key)
        return ApiKeyOut.model_validate(key)

    async def rotate_key(
        self, owner_id: uuid.UUID, key_id: uuid.UUID
    ) -> ApiKeyCreated | None:
        key = await _get_owned(self._db, owner_id, key_id)
        if key is None or key.revoked_at is not None:
            return None
        plaintext, prefix = generate_token()
        key.key_hash = hash_password(plaintext)
        key.key_prefix = prefix
        key.last_used_at = None
        await self._db.flush()
        await self._db.refresh(key)
        await self._db.commit()
        await self._db.refresh(key)
        return ApiKeyCreated(
            **ApiKeyOut.model_validate(key).model_dump(), plaintext=plaintext
        )

    async def revoke_key(self, owner_id: uuid.UUID, key_id: uuid.UUID) -> bool:
        key = await _get_owned(self._db, owner_id, key_id)
        if key is None or key.revoked_at is not None:
            return False
        key.revoked_at = datetime.now(timezone.utc)
        await self._db.flush()
        await self._db.commit()
        return True
