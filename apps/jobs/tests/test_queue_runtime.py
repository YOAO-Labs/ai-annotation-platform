from __future__ import annotations

import json
from types import SimpleNamespace

from celery import Celery, signals
from starlette.testclient import TestClient

from aap_jobs.queue_config import JobsQueueSettings
from aap_jobs.queue_contract import JOBS_QUEUE_PROTOCOL, QueueDispatch
from aap_jobs.queue_runtime import CeleryQueueTransport, create_app


class FakeQueueTransport:
    def __init__(self):
        self.calls = []
        self.failure = None

    def dispatch(self, request):
        if self.failure is not None:
            raise self.failure
        self.calls.append(("dispatch", request))

    def revoke(self, request):
        if self.failure is not None:
            raise self.failure
        self.calls.append(("revoke", request))

    def ping(self):
        if self.failure is not None:
            raise self.failure


def _settings():
    return JobsQueueSettings(
        broker_url="redis://private-broker.example/5",
        token="service-token",
    )


def _request(operation: str, execution_id: str = "stable-task-id") -> dict:
    value = {
        "schema": JOBS_QUEUE_PROTOCOL,
        "operation": operation,
        "execution_id": execution_id,
    }
    if operation == "dispatch":
        value.update(
            task_name="app.workers.cross_frame_job.run_cross_frame_job",
            args=["job-uuid"],
            kwargs={},
            options={},
        )
    return value


def test_private_queue_http_owner_dispatches_and_revokes_without_completion_claims():
    transport = FakeQueueTransport()
    app = create_app(_settings(), transport=transport)
    with TestClient(app) as client:
        dispatched = client.post(
            "/v1/jobs/queue",
            headers={"Authorization": "Bearer service-token"},
            json=_request("dispatch"),
        )
        revoked = client.post(
            "/v1/jobs/queue",
            headers={"Authorization": "Bearer service-token"},
            json=_request("revoke", "legacy-revoke-id"),
        )
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200

    assert dispatched.json() == {
        "schema": JOBS_QUEUE_PROTOCOL,
        "accepted": True,
        "execution_id": "stable-task-id",
    }
    assert revoked.json() == {
        "schema": JOBS_QUEUE_PROTOCOL,
        "accepted": True,
        "execution_id": "legacy-revoke-id",
    }
    assert [kind for kind, _request in transport.calls] == ["dispatch", "revoke"]
    assert transport.calls[0][1].args == ["job-uuid"]
    assert transport.calls[1][1].execution_id == "legacy-revoke-id"


def test_bearer_is_checked_before_request_body_is_read():
    app = create_app(_settings(), transport=FakeQueueTransport())
    sent = []
    reads = []

    async def receive():
        reads.append(True)
        raise AssertionError("unauthorized request body was read")

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/jobs/queue",
                "raw_path": b"/v1/jobs/queue",
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer wrong")],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 8190),
            },
            receive,
            send,
        )
    )
    assert not reads
    assert sent[0]["status"] == 401


def test_transport_failure_is_sanitized_and_does_not_claim_acceptance():
    transport = FakeQueueTransport()
    transport.failure = RuntimeError("redis://user:secret@broker.example/5")
    with TestClient(create_app(_settings(), transport=transport)) as client:
        response = client.post(
            "/v1/jobs/queue",
            headers={"Authorization": "Bearer service-token"},
            json=_request("dispatch"),
        )
        readiness = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["accepted"] is False
    assert "secret" not in response.text
    assert "broker.example" not in response.text
    assert readiness.status_code == 503


class _FakeCelery:
    def __init__(self):
        self.conf = SimpleNamespace(task_publish_retry=True, update=self.update)
        self.updates = {}
        self.control = SimpleNamespace(revoke=self.revoke)
        self.published = []
        self.revoked = []
        self.closed = False

    def update(self, **kwargs):
        self.updates.update(kwargs)

    def send_task(self, task_name, **kwargs):
        self.published.append((task_name, kwargs))

    def revoke(self, execution_id, *, terminate):
        self.revoked.append((execution_id, terminate))

    def connection_for_read(self):
        return SimpleNamespace(
            connect_timeout=None,
            connect=lambda: None,
            release=lambda: None,
        )

    def close(self):
        self.closed = True


def test_celery_transport_uses_frozen_routes_and_request_only_revoke():
    app = _FakeCelery()
    transport = CeleryQueueTransport(_settings(), celery_app=app)
    dispatch = _request("dispatch")
    from aap_jobs.queue_contract import decode_queue_request

    transport.dispatch(decode_queue_request(json.dumps(dispatch).encode()))
    transport.revoke(
        decode_queue_request(json.dumps(_request("revoke", "old-task-id")).encode())
    )
    transport.ping()

    assert app.updates["task_default_queue"] == "default"
    assert app.updates["task_serializer"] == "json"
    assert app.updates["result_serializer"] == "json"
    assert app.updates["accept_content"] == ["json"]
    assert app.updates["task_routes"][
        "app.workers.cross_frame_job.run_cross_frame_job"
    ] == {"queue": "default"}
    assert "task_publish_retry" not in app.updates
    assert app.conf.task_publish_retry is True
    assert app.published == [
        (
            "app.workers.cross_frame_job.run_cross_frame_job",
            {"args": ["job-uuid"], "kwargs": {}, "task_id": "stable-task-id"},
        )
    ]
    assert app.revoked == [("old-task-id", False)]


def test_celery_publish_uses_the_current_default_route_and_retry_policy():
    app = Celery("jobs-queue-pure-test", broker="memory://localhost/0")
    retry = app.conf.task_publish_retry
    retry_policy = app.conf.task_publish_retry_policy
    messages = []

    def capture(
        sender=None, headers=None, body=None, exchange=None, routing_key=None, **_
    ):
        messages.append((headers, body, exchange, routing_key))

    signals.before_task_publish.connect(capture, weak=False)
    try:
        CeleryQueueTransport(_settings(), celery_app=app).dispatch(
            QueueDispatch(
                "app.workers.cross_frame_job.run_cross_frame_job",
                "predeclared-task-id",
                ["job-uuid"],
                {},
                {},
            )
        )
    finally:
        signals.before_task_publish.disconnect(capture)

    assert len(messages) == 1
    headers, body, _exchange, routing_key = messages[0]
    assert headers["id"] == "predeclared-task-id"
    assert body[0] == ["job-uuid"]
    assert body[1] == {}
    assert routing_key == "default"
    assert app.conf.task_publish_retry == retry
    assert app.conf.task_publish_retry_policy == retry_policy
