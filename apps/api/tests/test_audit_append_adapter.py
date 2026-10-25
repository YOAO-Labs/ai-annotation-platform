from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
from uuid import uuid4

from app.db.models.audit_log import AuditLog
from app.middleware.request_id import request_id_var
from app.services.audit import AuditService
from app.services.audit_origin_contract import AuditOriginContext


class _AuditSession:
    def __init__(self, reply):
        self.reply = reply
        self.events = []
        self.merged = []
        self.selects = []
        self.no_autoflush = nullcontext()

    async def scalar(self, statement, parameters=None):
        self.events.append("append")
        self.request = json.loads(parameters["request"])
        return self.reply

    async def execute(self, statement, parameters=None):
        self.selects.append(str(statement))
        raise AssertionError("Audit append must not SELECT the public table")

    async def merge(self, instance, *, load=True):
        assert load is False
        self.events.append("merge")
        self.merged.append(instance)
        return instance

    async def flush(self, objects=None):
        self.events.append("flush")


def _reply(ids):
    return {
        "schema": "aap-audit-append/v1",
        "ok": True,
        "data": {
            "events": [
                {"id": str(id_), "created_at": "2026-10-23T00:00:00+00:00"}
                for id_ in ids
            ]
        },
    }


async def test_log_merges_receipt_snapshot_before_full_flush_without_select():
    actor_id = uuid4()
    db = _AuditSession(_reply([17]))
    token = request_id_var.set("request-append-17")
    try:
        result = await AuditService.log(
            db,
            actor=SimpleNamespace(
                id=actor_id, email="actor@example.test", role="employee"
            ),
            action="future.action",
            target_type="task",
            target_id=42,
            origin=AuditOriginContext("PATCH", "/api/v1/tasks/42", "127.0.0.1"),
            status_code=200,
            detail={"x": [1, True]},
        )
    finally:
        request_id_var.reset(token)

    assert isinstance(result, AuditLog)
    assert result.id == 17
    assert result.created_at.isoformat() == "2026-10-23T00:00:00+00:00"
    assert result.ip == "127.0.0.1"
    assert result.detail_json == {"x": [1, True]}
    assert db.events == ["append", "merge", "flush"]
    assert db.selects == []
    assert db.merged == [result]
    assert db.request["schema"] == "aap-audit-append/v1"
    event = db.request["events"][0]
    assert event["actor_id"] == str(actor_id)
    assert event["action"] == "future.action"
    assert event["target_id"] == "42"
    assert event["request_id"] == "request-append-17"


async def test_log_many_returns_orm_objects_in_input_order():
    db = _AuditSession(_reply([21, 22]))
    result = await AuditService.log_many(
        db,
        actor=None,
        action="bulk.action",
        target_type="task",
        items=[{"target_id": "a", "detail": {"index": 0}}, {"target_id": "b"}],
    )

    assert [row.id for row in result] == [21, 22]
    assert [row.target_id for row in result] == ["a", "b"]
    assert db.events == ["append", "merge", "merge", "flush"]
    assert db.selects == []
    assert [event["target_id"] for event in db.request["events"]] == ["a", "b"]
    assert db.request["events"][1]["detail_json"] is None


async def test_empty_log_many_skips_append_and_flush():
    db = _AuditSession(_reply([]))

    assert (
        await AuditService.log_many(
            db, actor=None, action="future.action", target_type="task", items=[]
        )
        == []
    )
    assert db.events == []
