from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import DBAPIError

from aap_audit.query_contract import (
    AuditQueryFilters,
    AuditQueryRequest,
    AuditQueryUnavailable,
)
from aap_audit.query_postgres import AuditQueryPostgresData


class _Session:
    def __init__(self, reply, *, autoflush):
        self.reply = reply
        self.autoflush = autoflush
        self.events = []
        self.calls = []
        self.commits = 0

    async def flush(self):
        self.events.append("flush")

    async def scalar(self, statement, parameters=None):
        self.events.append("query")
        self.calls.append((str(statement), parameters))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _request():
    filters = AuditQueryFilters(
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=None,
        to=None,
        business_only=False,
        detail_key=None,
        detail_value=None,
    )
    return AuditQueryRequest.list_page(filters, page=2, page_size=20)


def _reply():
    return {
        "schema": "aap-audit-query/v1",
        "ok": True,
        "data": {"operation": "list", "total": "0", "events": []},
    }


@pytest.mark.parametrize(
    "autoflush,expected", [(True, ["flush", "query"]), (False, ["query"])]
)
async def test_query_uses_borrowed_session_and_preserves_autoflush(autoflush, expected):
    session = _Session(_reply(), autoflush=autoflush)

    result = await AuditQueryPostgresData(session).query(_request())

    statement, parameters = session.calls[0]
    assert "aap_audit_v1.query_events" in statement
    assert "pg_catalog.jsonb" in statement
    assert json.loads(parameters["request"])["page"] == 2
    assert session.events == expected
    assert session.commits == 0
    assert result.data.total == "0"


async def test_query_propagates_database_failures_and_rejects_foreign_replies():
    db_error = DBAPIError("query", {}, RuntimeError("permission denied"))
    session = _Session(db_error, autoflush=False)
    with pytest.raises(DBAPIError) as caught:
        await AuditQueryPostgresData(session).query(_request())
    assert caught.value is db_error

    malformed = _Session({"schema": "foreign/v1"}, autoflush=False)
    with pytest.raises(AuditQueryUnavailable):
        await AuditQueryPostgresData(malformed).query(_request())
