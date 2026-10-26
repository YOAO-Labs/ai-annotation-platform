from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aap_jobs.queue_client import JobsQueueClient, JobsQueueExecutionControl
from aap_jobs.queue_contract import (
    JOBS_QUEUE_PROTOCOL,
    JobsQueueContractError,
    JobsQueueUnavailable,
)

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


def _install_sync_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return _REAL_SYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", build)
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
                "schema": JOBS_QUEUE_PROTOCOL,
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
            options={"queue": "ml.cpu", "soft_time_limit": 30},
        )
    )
    assert result == execution_id
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/jobs/queue"
    assert requests[0].headers["authorization"] == "Bearer service-token"
    assert json.loads(requests[0].content)["options"] == {
        "queue": "ml.cpu",
        "soft_time_limit": 30,
    }
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_async_canvas_and_envelope_publication_use_the_v2_contract(monkeypatch):
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "schema": JOBS_QUEUE_PROTOCOL,
                "accepted": True,
                "execution_id": payload["execution_id"],
            },
        )

    _install_async_transport(monkeypatch, handler)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    task = {
        "task_name": "app.workers.frame_preannotate.predict_video_segment",
        "execution_id": "child-id",
        "args": ["job-id", 0, 10],
        "kwargs": {},
        "options": {"soft_time_limit": 900},
    }
    callback = {
        "task_name": "app.workers.frame_preannotate.launch_predict_phase",
        "execution_id": "callback-id",
        "args": ["job-id"],
        "kwargs": {},
        "options": {},
    }

    async def publish():
        canvas_id = await client.dispatch_canvas(
            "callback-id", "group-id", [task], callback
        )
        dispatch_id = await client.publish_envelope(
            {
                "schema": JOBS_QUEUE_PROTOCOL,
                "operation": "dispatch",
                "task_name": "some.task",
                "execution_id": "relay-id",
                "args": [],
                "kwargs": {},
                "options": {"queue": "ml"},
            }
        )
        return canvas_id, dispatch_id

    assert asyncio.run(publish()) == ("callback-id", "relay-id")
    assert [item["operation"] for item in requests] == ["canvas", "dispatch"]
    assert requests[0]["tasks"][0]["options"] == {"soft_time_limit": 900}
    assert requests[1]["options"] == {"queue": "ml"}


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
            client.dispatch_job(
                "some.task", "task-id", [], options={"unsupported": "ml"}
            )
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


def test_sync_client_dispatches_once_from_an_active_async_loop(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schema": JOBS_QUEUE_PROTOCOL,
                "accepted": True,
                "execution_id": payload["execution_id"],
            },
        )

    options = _install_sync_transport(monkeypatch, handler)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    execution_id = "stable-execution-id"

    async def dispatch_in_active_loop():
        return client.dispatch_job_sync(
            "app.workers.cross_frame_job.run_cross_frame_job",
            execution_id,
            ["job-id"],
            kwargs={"source": "sync"},
            options={"queue": "ml.cpu", "soft_time_limit": 12},
        )

    assert asyncio.run(dispatch_in_active_loop()) == execution_id
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/jobs/queue"
    assert requests[0].headers["authorization"] == "Bearer service-token"
    assert requests[0].headers["content-type"] == "application/json"
    assert json.loads(requests[0].content) == {
        "schema": JOBS_QUEUE_PROTOCOL,
        "operation": "dispatch",
        "task_name": "app.workers.cross_frame_job.run_cross_frame_job",
        "execution_id": execution_id,
        "args": ["job-id"],
        "kwargs": {"source": "sync"},
        "options": {"queue": "ml.cpu", "soft_time_limit": 12},
    }
    assert options[0]["timeout"] == 10.0
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_sync_canvas_and_envelope_publication_use_the_v2_contract(monkeypatch):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "schema": JOBS_QUEUE_PROTOCOL,
                "accepted": True,
                "execution_id": payload["execution_id"],
            },
        )

    _install_sync_transport(monkeypatch, handler)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    task = {
        "task_name": "some.task",
        "execution_id": "child-id",
        "args": [],
        "kwargs": {},
        "options": {},
    }
    callback = {
        "task_name": "some.callback",
        "execution_id": "callback-id",
        "args": [],
        "kwargs": {},
        "options": {},
    }
    assert (
        client.dispatch_canvas_sync("callback-id", "group-id", [task], callback)
        == "callback-id"
    )
    assert (
        client.publish_envelope_sync(
            {
                "schema": JOBS_QUEUE_PROTOCOL,
                "operation": "canvas",
                "execution_id": "callback-id-2",
                "group_id": "group-id-2",
                "tasks": [{**task, "execution_id": "child-id-2"}],
                "callback": {**callback, "execution_id": "callback-id-2"},
            }
        )
        == "callback-id-2"
    )
    assert [item["operation"] for item in requests] == ["canvas", "canvas"]


@pytest.mark.parametrize(
    ("task_name", "execution_id", "args", "kwargs", "options"),
    [
        ("", "task-id", [], None, None),
        ("some.task", "", [], None, None),
        ("some.task", "task-id", {}, None, None),
        ("some.task", "task-id", [], {1: "invalid"}, None),
        ("some.task", "task-id", [], None, {"unsupported": "custom"}),
    ],
)
def test_sync_client_rejects_invalid_contract_without_http(
    monkeypatch, task_name, execution_id, args, kwargs, options
):
    def unexpected(_request):
        raise AssertionError("unexpected HTTP")

    calls = _install_sync_transport(monkeypatch, unexpected)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    with pytest.raises(JobsQueueContractError):
        client.dispatch_job_sync(
            task_name,
            execution_id,
            args,
            kwargs=kwargs,
            options=options,
        )
    assert not calls


@pytest.mark.parametrize(
    "failure", ["oversized", "malformed", "rejected", "mismatched", "status", "timeout"]
)
def test_sync_client_fails_closed_without_retry_or_async_fallback(monkeypatch, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "oversized":
            return httpx.Response(200, content=b"secret" * 700)
        if failure == "malformed":
            return httpx.Response(200, content=b"secret response")
        if failure == "timeout":
            raise httpx.ReadTimeout("secret timeout", request=request)
        payload = json.loads(request.content)
        accepted = failure != "rejected"
        reply_id = "other-id" if failure == "mismatched" else payload["execution_id"]
        status = 503 if failure == "status" else 200
        return httpx.Response(
            status,
            json={
                "schema": JOBS_QUEUE_PROTOCOL,
                "accepted": accepted,
                "execution_id": reply_id,
            },
        )

    sync_calls = _install_sync_transport(monkeypatch, handler)
    async_calls = []

    def unexpected_async(**_kwargs):
        async_calls.append(True)
        raise AssertionError("unexpected async fallback")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_async)
    client = JobsQueueClient("http://jobs-queue:8190", "service-token")
    with pytest.raises(JobsQueueUnavailable) as error:
        client.dispatch_job_sync("some.task", "task-id", ["job-id"])

    assert len(sync_calls) == len(requests) == 1
    assert not async_calls
    assert "secret" not in str(error.value)


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
            "schema": JOBS_QUEUE_PROTOCOL,
            "accepted": True,
            "execution_id": payload["execution_id"],
        },
    )
