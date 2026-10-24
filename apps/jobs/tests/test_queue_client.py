from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aap_jobs.queue_client import JobsQueueClient, JobsQueueExecutionControl
from aap_jobs.queue_contract import JobsQueueContractError, JobsQueueUnavailable

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_SYNC_CLIENT = httpx.Client


def _install_async_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build)
    return calls


def test_async_client_dispatches_once_without_environment_proxy_or_redirect(
    monkeypatch,
):
    requests = []

    async def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schema": "aap-jobs-queue/v1",
                "accepted": True,
                "execution_id": payload["execution_id"],
            },
        )

    options = _install_async_transport(monkeypatch, handler)
    execution_id = "stable-execution-id"
    result = asyncio.run(
        JobsQueueClient("http://jobs-queue:8190", "service-token").dispatch_job(
            "app.workers.cross_frame_job.run_cross_frame_job",
            execution_id,
            ["8ccbf8e4-cce2-4eec-a15d-9f58222987a1"],
        )
    )
    assert result == execution_id
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/jobs/queue"
    assert requests[0].headers["authorization"] == "Bearer service-token"
    assert json.loads(requests[0].content)["options"] == {}
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_async_client_bounds_reply_and_does_not_retry_failed_status(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(503, content=b"x" * 4097)

    _install_async_transport(monkeypatch, handler)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    with pytest.raises(JobsQueueUnavailable):
        asyncio.run(client.dispatch_job("some.task", "task-id", ["job-id"]))
    assert len(requests) == 1


def test_async_client_propagates_cancellation_and_rejects_custom_options(monkeypatch):
    async def unexpected(_request):
        raise AssertionError("unexpected HTTP")

    calls = _install_async_transport(monkeypatch, unexpected)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    with pytest.raises(JobsQueueContractError):
        asyncio.run(
            client.dispatch_job("some.task", "task-id", [], options={"queue": "ml"})
        )
    assert not calls

    async def cancel(_request):
        raise asyncio.CancelledError

    _install_async_transport(monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.dispatch_job("some.task", "task-id", []))


def test_sync_execution_control_sends_only_a_cooperative_revoke(monkeypatch):
    requests = []
    transport = httpx.MockTransport(lambda request: _revoke_response(request, requests))
    options = []

    def build(**kwargs):
        options.append(kwargs)
        return _REAL_SYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", build)
    JobsQueueExecutionControl("http://jobs-queue:8190", "service-token").request_cancel(
        "legacy-revoke-id"
    )
    assert len(requests) == 1
    assert requests[0]["operation"] == "revoke"
    assert requests[0]["execution_id"] == "legacy-revoke-id"
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_client_rejects_credentials_in_origin_and_invalid_header_tokens():
    with pytest.raises(ValueError):
        JobsQueueClient("http://user:password@jobs-queue:8190", "service-token")
    with pytest.raises(ValueError):
        JobsQueueExecutionControl("http://jobs-queue:8190", "token\twith-tab")


def _revoke_response(request, requests):
    payload = json.loads(request.content)
    requests.append(payload)
    return httpx.Response(
        200,
        json={
            "schema": "aap-jobs-queue/v1",
            "accepted": True,
            "execution_id": payload["execution_id"],
        },
    )
