"""Compatibility names backed by durable Identity credential functions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from aap_identity.contract import REFRESH_GRACE_SECONDS
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.identity_session_data import IdentitySessionData
from app.services.identity_gateway import IdentityGatewayUnavailable


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _raise_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="身份认证服务暂不可用",
    )


async def blacklist_token(
    jti: str | uuid.UUID,
    ttl_seconds: int,
    *,
    db: AsyncSession | None = None,
) -> None:
    retain_until = datetime.now(timezone.utc) + timedelta(
        seconds=max(ttl_seconds, 0) + REFRESH_GRACE_SECONDS
    )
    try:
        if db is not None:
            await IdentitySessionData(db).revoke_jti(_uuid(jti), retain_until)
            return
        from app.db.base import async_session

        async with async_session() as session:
            await IdentitySessionData(session).revoke_jti(_uuid(jti), retain_until)
            await session.commit()
    except IdentityGatewayUnavailable:
        _raise_unavailable()


async def is_blacklisted(
    jti: str | uuid.UUID, *, db: AsyncSession | None = None
) -> bool:
    try:
        if db is not None:
            return await IdentitySessionData(db).is_jti_revoked(_uuid(jti))
        from app.db.base import async_session

        async with async_session() as session:
            return await IdentitySessionData(session).is_jti_revoked(_uuid(jti))
    except IdentityGatewayUnavailable:
        _raise_unavailable()


async def increment_user_generation(
    user_id: str | uuid.UUID, *, db: AsyncSession | None = None
) -> int:
    actor_id = _uuid(user_id)
    try:
        if db is not None:
            return await IdentitySessionData(db).advance_credential_generation(actor_id)
        from app.db.base import async_session

        async with async_session() as session:
            generation = await IdentitySessionData(
                session
            ).advance_credential_generation(actor_id)
            await session.commit()
            return generation
    except IdentityGatewayUnavailable:
        _raise_unavailable()


async def get_user_generation(
    user_id: str | uuid.UUID, *, db: AsyncSession | None = None
) -> int:
    actor_id = _uuid(user_id)
    try:
        if db is not None:
            return await IdentitySessionData(db).get_credential_generation(actor_id)
        from app.db.base import async_session

        async with async_session() as session:
            return await IdentitySessionData(session).get_credential_generation(
                actor_id
            )
    except IdentityGatewayUnavailable:
        _raise_unavailable()
