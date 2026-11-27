"""Persist storage observations to the audit ledger."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from yoao_audit.append_contract import AppendEvent, AppendRequest
from yoao_audit.postgres import AuditAppendPostgresData

from app.services.identity_read_relations import account_profiles


async def append_observation(
    connection: AsyncConnection,
    *,
    action: str,
    detail: dict,
    actor_id: str | None,
    request_id: str | None,
) -> None:
    committed_actor_id = None
    if actor_id is not None:
        committed_actor_id = await connection.scalar(
            select(account_profiles.c.id).where(
                account_profiles.c.id == uuid.UUID(actor_id)
            )
        )
    event = AppendEvent(
        actor_id=committed_actor_id,
        actor_email=None,
        actor_role=None,
        action=action,
        target_type="storage_location",
        target_id=detail["location_id"],
        method=None,
        path=None,
        status_code=None,
        ip=None,
        detail_json=detail,
        request_id=request_id,
    )
    await AuditAppendPostgresData(connection).append(AppendRequest.create([event]))
