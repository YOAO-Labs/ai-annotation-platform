"""SQLAlchemy binding for the versioned Audit query function."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text

from .query_contract import (
    AuditQueryReply,
    AuditQueryRequest,
    QUERY_PROTOCOL,
    decode_query_reply,
    validate_query_reply,
)


class AuditQuerySession(Protocol):
    autoflush: bool

    async def flush(self) -> None: ...

    async def scalar(self, statement, parameters=None) -> Any: ...


class AuditQueryPostgresData:
    """Query the Audit owner on a borrowed caller session without committing."""

    def __init__(self, session: AuditQuerySession):
        self.session = session

    async def query(self, request: AuditQueryRequest) -> AuditQueryReply:
        if request.protocol != QUERY_PROTOCOL:
            raise ValueError("unsupported audit query protocol")
        if self.session.autoflush:
            await self.session.flush()
        reply = await self.session.scalar(
            text(
                "SELECT aap_audit_v1.query_events(CAST(:request AS pg_catalog.jsonb))"
            ),
            {"request": request.model_dump_json(by_alias=True)},
        )
        return validate_query_reply(request, decode_query_reply(reply))
