"""Caller-transaction adapter for the fixed Workspace backend-binding command."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

_CLEAR_PRIMARY_BINDINGS = text(
    "SELECT aap_workspace_v1.clear_primary_backend_bindings(CAST(:pool_id AS uuid))"
)


async def lock_backend_project_reference(db: AsyncSession, project_id: UUID) -> None:
    if db.autoflush:
        await db.flush()
    reference = await db.scalar(
        text(
            "SELECT aap_workspace_v1.lock_project_reference(CAST(:project_id AS uuid))"
        ),
        {"project_id": project_id},
    )
    if reference is None:
        raise HTTPException(status_code=404, detail="Project not found")


async def clear_primary_backend_binding_for_pool(
    db: AsyncSession, pool_id: UUID
) -> None:
    """Clear project bindings in the caller's transaction, preserving ORM autoflush."""
    if db.autoflush:
        await db.flush()
    try:
        await db.execute(_CLEAR_PRIMARY_BINDINGS, {"pool_id": pool_id})
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from error
        raise
