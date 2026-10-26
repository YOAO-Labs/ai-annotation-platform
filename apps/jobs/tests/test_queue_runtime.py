from __future__ import annotations


from starlette.testclient import TestClient

from aap_jobs.queue_config import JobsQueueSettings
from aap_jobs.queue_contract import (
    JOBS_QUEUE_PROTOCOL,
)
from aap_jobs.queue_runtime import create_app


class FakeQueueTransport:
    def __init__(self):
        self.calls = []
        self.failure = None

    def dispatch(self, request):
        if self.failure is not None:
            raise self.failure
        self.calls.append(("dispatch", request))

    def dispatch_canvas(self, request):
        if self.failure is not None:
            raise self.failure
        self.calls.append(("canvas", request))

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
