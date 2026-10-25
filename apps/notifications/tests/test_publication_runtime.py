from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

from starlette.testclient import TestClient
import pytest

from aap_notifications.delivery_config import NotificationPublisherSettings
from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
)
from aap_notifications.publication_contract import IMMEDIATE_PUBLICATION_PROTOCOL
from aap_notifications.publication_runtime import create_app


def _settings(**overrides):
    values = {
        "_env_file": None,
        "database_url": "postgresql://publisher.invalid/notifications",
        "redis_url": "redis://publisher.invalid/0",
        "api_token": "service-token-without-whitespace",
    }
    values.update(overrides)
    return NotificationPublisherSettings(**values)


def _request(*, user_id="60000000-0000-4000-8000-000000000001", authorization=None):
    if authorization is None:
        authorization = {
            "restricted": True,
            "project_id": "60000000-0000-4000-8000-000000000002",
            "export_required": False,
            "member": True,
            "reviewer": False,
            "owner": False,
            "super_admin": False,
        }
    return {
        "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
        "user_id": user_id,
        "message": {
            "id": "60000000-0000-4000-8000-000000000003",
            "type": "future.event",
            "target_type": "task",
            "target_id": "60000000-0000-4000-8000-000000000004",
            "payload": ["native", {"number": 2**90}],
            "created_at": "2026-10-24T12:34:56+08:00",
        },
        "authorization": authorization,
    }


class FakeRedis:
    def __init__(self, *, error=None, ping_error=None):
        self.error = error
        self.ping_error = ping_error
        self.published = []
        self.ping_count = 0

    async def ping(self):
        self.ping_count += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def publish(self, channel, message):
        if self.error is not None:
            raise self.error
        self.published.append((channel, message))
        return 0


class FakeData:
    def __init__(self, *, error=None):
        self.error = error
        self.policy_count = 0

    async def delivery_policy(self):
        self.policy_count += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(policy=NOTIFICATION_DELIVERY_POLICY)


def test_optional_private_app_exposes_only_publish_and_health_routes():
    app = create_app(_settings(), redis=FakeRedis(), data=FakeData())

    assert [route.path for route in app.routes] == [
        "/health/live",
        "/health/ready",
        "/v1/notifications/publish",
    ]


def test_service_bearer_is_checked_before_request_body_read(monkeypatch):
    from starlette.requests import Request

    body_reads = []

    async def forbidden_body(_self):
        body_reads.append(True)
        raise AssertionError("unauthorized requests must not read their body")

    monkeypatch.setattr(Request, "body", forbidden_body)
    app = create_app(_settings(), redis=FakeRedis(), data=FakeData())

    with TestClient(app) as client:
        response = client.post(
            "/v1/notifications/publish",
            content=b'{"secret":"must-not-be-read"}',
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
        "published": False,
    }
    assert body_reads == []
    assert "must-not-be-read" not in response.text


@pytest.mark.asyncio
async def test_non_ascii_bearer_header_is_rejected_before_reading_body():
    app = create_app(_settings(), redis=FakeRedis(), data=FakeData())
    body_reads = []
    sent = []

    async def receive():
        body_reads.append(True)
        return {"type": "http.request", "body": b"private", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/notifications/publish",
        "raw_path": b"/v1/notifications/publish",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer \xff")],
        "client": ("127.0.0.1", 8000),
        "server": ("testserver", 80),
        "app": app,
        "root_path": "",
    }

    await app(scope, receive, send)

    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 401
    assert body_reads == []


def test_publish_derives_channel_and_zero_subscribers_is_success():
    redis = FakeRedis()
    user_id = "abcdef00-0000-4000-8000-000000000001".upper()
    request = _request(user_id=user_id)
    app = create_app(_settings(), redis=redis, data=FakeData())

    with TestClient(app) as client:
        response = client.post(
            "/v1/notifications/publish",
            json=request,
            headers={"Authorization": "Bearer service-token-without-whitespace"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema": IMMEDIATE_PUBLICATION_PROTOCOL,
        "published": True,
    }
    assert redis.published == [
        ("notify:abcdef00-0000-4000-8000-000000000001", json.dumps(request["message"]))
    ]


def test_denied_evidence_is_not_published_and_malformed_or_redis_errors_are_sanitized():
    redis = FakeRedis()
    app = create_app(_settings(), redis=redis, data=FakeData())
    denied = _request(
        authorization={
            "restricted": True,
            "project_id": None,
            "export_required": False,
            "member": False,
            "reviewer": False,
            "owner": False,
            "super_admin": False,
        }
    )
    with TestClient(app) as client:
        denied_response = client.post(
            "/v1/notifications/publish",
            json=denied,
            headers={"Authorization": "Bearer service-token-without-whitespace"},
        )
        malformed_response = client.post(
            "/v1/notifications/publish",
            content=b'{"schema":"secret-payload"}',
            headers={"Authorization": "Bearer service-token-without-whitespace"},
        )

    assert denied_response.status_code == 200
    assert denied_response.json()["published"] is False
    assert malformed_response.status_code == 400
    assert "secret-payload" not in malformed_response.text
    assert redis.published == []

    failing_redis = FakeRedis(error=RuntimeError("redis://private-token@cache secret"))
    failing_app = create_app(_settings(), redis=failing_redis, data=FakeData())
    with TestClient(failing_app) as client:
        failed_response = client.post(
            "/v1/notifications/publish",
            json=_request(),
            headers={"Authorization": "Bearer service-token-without-whitespace"},
        )
    assert failed_response.status_code == 503
    assert failed_response.json()["published"] is False
    assert "private-token" not in failed_response.text


def test_live_and_ready_health_checks_include_loop_and_owner_dependencies():
    redis = FakeRedis()
    data = FakeData()
    publisher_ready = asyncio.Event()
    app = create_app(
        _settings(),
        redis=redis,
        data=data,
        publisher_ready=publisher_ready,
    )

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        unavailable = client.get("/health/ready")
        assert unavailable.status_code == 503
        assert redis.ping_count == data.policy_count == 0

        publisher_ready.set()
        ready = client.get("/health/ready")

    assert ready.status_code == 200
    assert redis.ping_count == data.policy_count == 1


def test_ready_health_is_sanitized_when_redis_or_owner_policy_is_unavailable():
    for redis, data in (
        (FakeRedis(ping_error=RuntimeError("redis://private-secret")), FakeData()),
        (FakeRedis(), FakeData(error=RuntimeError("private owner detail"))),
    ):
        publisher_ready = asyncio.Event()
        publisher_ready.set()
        app = create_app(
            _settings(),
            redis=redis,
            data=data,
            publisher_ready=publisher_ready,
        )
        with TestClient(app) as client:
            response = client.get("/health/ready")
        assert response.status_code == 503
        assert "private" not in response.text


def test_publication_app_requires_optional_service_token():
    try:
        create_app(_settings(api_token=None))
    except ValueError as error:
        assert "API token" in str(error)
    else:
        raise AssertionError("publication route must not start without its bearer")


def test_publication_app_closes_redis_clients_it_creates(monkeypatch):
    clients = []

    class OwnedRedis(FakeRedis):
        async def aclose(self):
            self.closed = True

    class RedisFactory:
        @staticmethod
        def from_url(_url, **_kwargs):
            client = OwnedRedis()
            clients.append(client)
            return client

    redis_module = types.ModuleType("redis")
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.Redis = RedisFactory
    redis_module.asyncio = redis_asyncio
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)
    app = create_app(_settings(), data=FakeData())

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200

    assert len(clients) == 1
    assert clients[0].closed
