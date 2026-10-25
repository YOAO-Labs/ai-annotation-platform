from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.audit import AuditMiddleware
from app.middleware.request_id import RequestIDMiddleware, request_id_var
from app.services.audit import append_http_metadata


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.rollbacks = 0
        self.commits = 0

    async def scalar(self, statement, parameters=None):
        self.requests.append(json.loads(parameters["request"]))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    async def rollback(self):
        self.rollbacks += 1

    async def commit(self):
        self.commits += 1


def _reply():
    return {
        "schema": "aap-audit-append/v1",
        "ok": True,
        "data": {"events": [{"id": "5", "created_at": "2026-10-23T00:00:00+00:00"}]},
    }


def _payload(actor_id):
    return {
        "actor_id": str(actor_id),
        "actor_role": "employee",
        "action": "http.post",
        "method": "POST",
        "path": "/api/v1/example",
        "status_code": 201,
        "ip": "127.0.0.1",
        "request_id": "request-1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audit_async", "request_id", "stored_request_id"),
    [
        (False, "r" * 36, "r" * 36),
        (False, "r" * 37, None),
        (True, "r" * 36, "r" * 36),
        (True, "r" * 37, None),
    ],
)
async def test_http_metadata_append_bounds_request_id_and_preserves_response(
    monkeypatch, audit_async, request_id, stored_request_id
):
    from app.middleware import audit as audit_module

    actor_id = uuid4()
    session = _Session([_reply()])

    class _SessionScope:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return None

    if audit_async:
        from app.workers import audit as audit_worker

        def broker_unavailable(_payload):
            raise RuntimeError("broker unavailable")

        monkeypatch.setattr(
            audit_worker.persist_audit_entry, "delay", broker_unavailable
        )

    monkeypatch.setattr(audit_module.settings, "audit_async", audit_async)
    monkeypatch.setattr(audit_module, "async_session", lambda: _SessionScope())
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/example",
            "raw_path": b"/api/v1/example",
            "query_string": b"",
            "headers": [(b"x-request-id", request_id.encode())],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "state": {
                "identity_principal": SimpleNamespace(
                    actor_id=actor_id, role="employee"
                )
            },
        }
    )
    response = Response("committed", status_code=201)
    audit_middleware = AuditMiddleware(lambda *_args: None)

    async def route_response(_request):
        return response

    async def call_audit(_request):
        return await audit_middleware.dispatch(request, route_response)

    previous_request_id = request_id_var.set("")
    try:
        result = await RequestIDMiddleware(lambda *_args: None).dispatch(
            request, call_audit
        )
        assert request_id_var.get() == request_id
    finally:
        request_id_var.reset(previous_request_id)

    assert result is response
    assert result.status_code == 201
    assert result.headers["X-Request-ID"] == request_id
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.requests) == 1
    event = session.requests[0]["events"][0]
    assert event["request_id"] == stored_request_id
    assert event["actor_id"] == str(actor_id)
    assert event["actor_role"] == "employee"
    assert event["action"] == "http.post"


async def test_stale_http_actor_rolls_back_and_retries_anonymous():
    actor_id = uuid4()
    error = IntegrityError("append", {}, RuntimeError("foreign key"))
    session = _Session([error, _reply()])

    await append_http_metadata(session, _payload(actor_id))

    assert session.rollbacks == 1
    assert session.commits == 0
    assert session.requests[0]["events"][0]["actor_id"] == str(actor_id)
    assert session.requests[1]["events"][0]["actor_id"] is None
    assert session.requests[1]["events"][0]["actor_email"] is None
    assert session.requests[1]["events"][0]["detail_json"] is None


async def test_integrity_error_without_actor_is_not_translated_or_retried():
    error = IntegrityError("append", {}, RuntimeError("invalid append"))
    session = _Session([error])

    with pytest.raises(IntegrityError) as caught:
        await append_http_metadata(session, {**_payload(uuid4()), "actor_id": None})

    assert caught.value is error
    assert session.rollbacks == 1
    assert len(session.requests) == 1


async def test_celery_writer_uses_anonymous_retry_then_commits(monkeypatch):
    from app.workers import _db, audit as audit_worker

    actor_id = uuid4()
    error = IntegrityError("append", {}, RuntimeError("foreign key"))
    session = _Session([error, _reply()])

    class _TaskSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(_db, "task_session", lambda: _TaskSession())
    await audit_worker._async_persist(_payload(actor_id))

    assert session.rollbacks == 1
    assert session.commits == 1
    assert session.requests[0]["events"][0]["actor_id"] == str(actor_id)
    assert session.requests[1]["events"][0]["actor_id"] is None
