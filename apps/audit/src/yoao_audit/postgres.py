"""SQLAlchemy binding for the versioned Audit append function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .append_contract import (
    AppendReply,
    AppendRequest,
    AuditAppendUnavailable,
    decode_append_reply,
)


class AuditAppendSession(Protocol):
    async def scalar(self, statement, parameters=None) -> Any: ...


class AuditAppendPostgresData:
    """Call the Audit owner inside a caller-managed PostgreSQL transaction."""

    def __init__(self, session: AuditAppendSession):
        self.session = session

    async def append(self, request: AppendRequest) -> AppendReply:
        if request.protocol != "aap-audit-append/v1":
            raise ValueError("unsupported audit append protocol")
        reply = await self.session.scalar(
            text(
                "SELECT aap_audit_v1.append_events(CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
        decoded = decode_append_reply(reply)
        ids = [event.id for event in decoded.data.events]
        if len(ids) != len(request.events) or len(set(ids)) != len(ids):
            raise AuditAppendUnavailable()
        return decoded
