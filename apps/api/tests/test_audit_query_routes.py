from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from aap_audit.query_contract import decode_query_reply
from aap_audit.query_postgres import AuditQueryPostgresData
from app.api.v1.audit_logs import _encode_cursor, export_audit_logs, list_audit_logs
from app.db.enums import UserRole
from app.services.audit import AuditService
from app.services.identity_contracts import IdentityActorFacts


class _Session:
    def __init__(self):
        self.autoflush = True
        self.events = []

    async def commit(self):
        self.events.append("commit")


def _actor():
    return IdentityActorFacts(
        id=uuid4(),
        name="Audit Admin",
        email="admin@example.test",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
    )


def _reply(events, *, operation="list", total="1"):
    return decode_query_reply(
        {
            "schema": "aap-audit-query/v1",
            "ok": True,
            "data": {"operation": operation, "total": total, "events": events},
        }
    )


def _event(event_id="17", *, action="project.create"):
    return {
        "id": event_id,
        "actor_id": None,
        "actor_email": "actor@example.test",
        "actor_role": "super_admin",
        "action": action,
        "target_type": "project",
        "target_id": "project-1",
        "method": "POST",
        "path": "/api/v1/projects",
        "status_code": 201,
        "ip": "127.0.0.1",
        "detail_json": {"result": "created"},
        "request_id": "query-request-1",
        "created_at": "2026-10-23T12:34:56.123456Z",
    }


def _request():
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/audit-logs/export",
            "raw_path": b"/api/v1/audit-logs/export",
            "query_string": b"format=json",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


async def test_super_admin_list_uses_owner_filters_cursor_and_stable_page(monkeypatch):
    session = _Session()
    actor = _actor()
    filter_actor_id = uuid4()
    captured = []

    async def query(self, request):
        captured.append(request)
        return _reply([_event("9007199254740993")], total="41")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    cursor = _encode_cursor(datetime(2026, 10, 23, 12, 34, 56, tzinfo=timezone.utc), 18)
    result = await list_audit_logs(
        page=4,
        page_size=1,
        cursor=cursor,
        action="project.create",
        target_type="project",
        target_id="project-1",
        actor_id=filter_actor_id.hex,
        from_=datetime(2026, 10, 1, tzinfo=timezone.utc),
        to=None,
        business_only=True,
        detail_key="result",
        detail_value="created",
        db=session,
        actor=actor,
    )

    request = captured[0]
    assert request.operation == "list"
    assert request.page == 4 and request.page_size == 1
    assert request.after.id == "18"
    assert request.filters.action == "project.create"
    assert request.filters.actor_id == str(filter_actor_id)
    assert request.filters.business_only is True
    assert result.total == 41
    assert result.items[0].id == 9007199254740993
    assert result.items[0].actor_email == "actor@example.test"
    assert result.next_cursor is not None
    assert session.events == []


async def test_public_list_preserves_zero_and_minimum_signed_bigint_ids(monkeypatch):
    async def query(self, request):
        return _reply([_event("0"), _event("-9223372036854775808")], total="2")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    result = await list_audit_logs(
        page=1,
        page_size=2,
        cursor=None,
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=None,
        to=None,
        business_only=False,
        detail_key=None,
        detail_value=None,
        db=_Session(),
        actor=_actor(),
    )
    assert [item.id for item in result.items] == [0, -9223372036854775808]


async def test_api_normalizes_naive_query_bounds_to_legacy_local_timezone(monkeypatch):
    captured = []

    async def query(self, request):
        captured.append(request)
        return _reply([], total="0")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    naive = datetime(2026, 10, 20, 12)
    await list_audit_logs(
        page=1,
        page_size=20,
        cursor=None,
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=naive,
        to=naive,
        business_only=False,
        detail_key=None,
        detail_value=None,
        db=_Session(),
        actor=_actor(),
    )
    assert captured[0].filters.from_ == naive.astimezone()
    assert captured[0].filters.to == naive.astimezone()


async def test_api_normalizes_naive_cursor_timestamp_to_legacy_local_timezone(
    monkeypatch,
):
    captured = []

    async def query(self, request):
        captured.append(request)
        return _reply([], total="0")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    naive = datetime(2026, 10, 20, 12)
    cursor = _encode_cursor(naive, 17)
    await list_audit_logs(
        page=99,
        page_size=20,
        cursor=cursor,
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=None,
        to=None,
        business_only=False,
        detail_key=None,
        detail_value=None,
        db=_Session(),
        actor=_actor(),
    )
    assert captured[0].after.created_at == naive.astimezone().isoformat()
    assert captured[0].after.id == "17"


async def test_export_commits_audit_before_returning_stream_and_keeps_metadata(
    monkeypatch,
):
    session = _Session()
    actor = _actor()
    captured = []

    async def query(self, request):
        session.events.append("query")
        captured.append(request)
        return _reply([_event()], operation="export")

    async def log(db, **kwargs):
        assert db is session
        assert kwargs["action"] == "audit.export"
        assert kwargs["detail"]["rows"] == 1
        assert kwargs["detail"]["filter_criteria"]["business_only"] is True
        session.events.append("audit-log")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    monkeypatch.setattr(AuditService, "log", staticmethod(log))

    response = await export_audit_logs(
        request=_request(),
        format="json",
        action="project.create",
        target_type="project",
        target_id="project-1",
        actor_id=None,
        from_=None,
        to=None,
        business_only=True,
        detail_key=None,
        detail_value=None,
        db=session,
        current_user=actor,
    )

    assert captured[0].operation == "export"
    assert captured[0].filters.business_only is True
    assert session.events == ["query", "audit-log", "commit"]
    chunks = [chunk async for chunk in response.body_iterator]
    body = json.loads("".join(chunks))
    assert body["_export_meta"]["rows"] == 1
    assert body["_export_meta"]["filter_criteria"] == {
        "action": "project.create",
        "target_type": "project",
        "target_id": "project-1",
        "business_only": True,
    }
    assert body["items"][0]["id"] == 17


async def test_export_over_cap_does_not_append_or_commit(monkeypatch):
    session = _Session()

    async def query(self, request):
        return _reply([], operation="export", total="50001")

    monkeypatch.setattr(AuditQueryPostgresData, "query", query)
    with pytest.raises(HTTPException) as caught:
        await export_audit_logs(
            request=_request(),
            format="json",
            action=None,
            target_type=None,
            target_id=None,
            actor_id=None,
            from_=None,
            to=None,
            business_only=False,
            detail_key=None,
            detail_value=None,
            db=session,
            current_user=_actor(),
        )
    assert caught.value.status_code == 413
    assert session.events == []
