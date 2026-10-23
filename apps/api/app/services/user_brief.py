"""Enrich already-authorized result IDs through Identity's batched profile owner."""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.briefs_contract import decode_briefs_request
from app.schemas.user import UserBrief
from app.services.identity_briefs_commands import IdentityBriefsCommands
from app.services.identity_gateway import get_identity_gateway


async def _resolve(db, user_ids, project_id=None):
    ids = [uuid.UUID(str(value)) for value in user_ids if value is not None]
    if not ids:
        return {}
    payload = {"user_ids": [str(value) for value in ids]}
    operation = "get_user_briefs"
    if project_id is not None:
        operation = "get_project_user_briefs"
        payload["project_id"] = str(project_id)
    command = decode_briefs_request(
        {"schema": "aap-identity-briefs/v1", "operation": operation, "payload": payload}
    )
    reply = await IdentityBriefsCommands(db, get_identity_gateway()).execute(command)
    return {
        str(user.id): UserBrief(**user.model_dump(mode="python"))
        for user in reply.data.users
    }


async def resolve_briefs(
    db: AsyncSession,
    user_ids: Iterable[uuid.UUID | str],
) -> dict[str, UserBrief]:
    return await _resolve(db, user_ids)


async def resolve_briefs_with_project_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_ids: Iterable[uuid.UUID | str],
) -> dict[str, UserBrief]:
    return await _resolve(db, user_ids, project_id)
