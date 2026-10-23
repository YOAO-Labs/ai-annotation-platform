from __future__ import annotations

import json

from sqlalchemy.exc import IntegrityError

from aap_audit.append_contract import (
    APPEND_PROTOCOL,
    AppendEvent,
    AppendRequest,
    AuditAppendUnavailable,
)
from aap_audit.postgres import AuditAppendPostgresData


class _FakeSession:
    def __init__(self, reply, *, autoflush: bool):
        self.reply = reply
        self.autoflush = autoflush
        self.flushes = 0
        self.calls = []
        self.commits = 0

    async def flush(self):
        self.flushes += 1

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _request() -> AppendRequest:
    return AppendRequest.create(
        [
            AppendEvent(
                actor_id=None,
                actor_email=None,
                actor_role=None,
                action="future.action",
                target_type=None,
                target_id=None,
                method="POST",
                path="/api/v1/example",
                status_code=201,
                ip=None,
                detail_json=None,
                request_id=None,
            )
        ]
    )


def _reply():
    return {
        "schema": APPEND_PROTOCOL,
        "ok": True,
        "data": {"events": [{"id": "42", "created_at": "2026-10-23T00:00:00+00:00"}]},
    }


async def test_postgres_binding_leaves_flush_and_commit_order_to_caller():
    for autoflush in (True, False):
        session = _FakeSession(_reply(), autoflush=autoflush)
        reply = await AuditAppendPostgresData(session).append(_request())

        assert session.flushes == 0
        assert session.commits == 0
        statement, parameters = session.calls[0]
        assert "aap_audit_v1.append_events" in statement
        assert json.loads(parameters["request"])["events"][0]["detail_json"] is None
        assert reply.data.events[0].id == "42"


async def test_postgres_binding_does_not_translate_database_integrity_errors():
    error = IntegrityError("append", {}, RuntimeError("foreign key"))
    session = _FakeSession(error, autoflush=True)

    try:
        await AuditAppendPostgresData(session).append(_request())
    except IntegrityError as actual:
        assert actual is error
    else:
        raise AssertionError("IntegrityError must propagate unchanged")


async def test_postgres_binding_rejects_missing_owner_receipts():
    session = _FakeSession(
        {"schema": APPEND_PROTOCOL, "ok": True, "data": {"events": []}},
        autoflush=True,
    )
    try:
        await AuditAppendPostgresData(session).append(_request())
    except AuditAppendUnavailable:
        pass
    else:
        raise AssertionError("one request event must produce one owner receipt")
