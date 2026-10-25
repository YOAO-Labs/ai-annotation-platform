"""API bindings to Audit's published read-only event relation."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aap_audit.read_relation_postgres import audit_events
from app.db.models.audit_log import AuditLog

# Map the published relation to the existing ORM entity. Normal ORM reads then
# retain the same identity-map reuse, dirty-field behavior, and expired-field
# refresh as the former private-table SELECTs.
audit_event = aliased(AuditLog, audit_events, adapt_on_names=True)


async def execute_audit_relation_read(db: AsyncSession, statement) -> Any:
    """Execute a Core projection while preserving ORM autoflush semantics."""
    if db.autoflush:
        await db.flush()
    return await db.execute(statement)
