"""Caller-transaction adapter for the fixed Workspace backend-binding command."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CLEAR_PRIMARY_BINDINGS = text(
    "SELECT aap_workspace_v1.clear_primary_backend_bindings(CAST(:pool_id AS uuid))"
)


async def clear_primary_backend_binding_for_pool(
    db: AsyncSession, pool_id: UUID
) -> None:
    """Clear project bindings in the caller's transaction, preserving ORM autoflush."""
    if db.autoflush:
        await db.flush()
    await db.execute(_CLEAR_PRIMARY_BINDINGS, {"pool_id": pool_id})
