from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

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
