from uuid import uuid4

import httpx
import pytest

from yoao.errors import ConflictError, JobFailedError, JobTimeoutError
from yoao.models import JobPage, JobRetryResult

from .conftest import API

JOB_ID = str(uuid4())


def _job(status: str, **extra) -> dict:
    return {
        "id": JOB_ID,
        "kind": "export",
        "status": status,
        "project_id": None,
        "user_id": None,
        "progress_pct": 0,
        "payload": {},
        "result": {},
        "error_message": None,
        "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        **extra,
    }


def test_list_jobs(client, respx_mock):
    route = respx_mock.get(f"{API}/async-jobs").mock(
        return_value=httpx.Response(200, json={"items": [_job("running")], "total": 1})
    )
    page = client.jobs.list(status=["pending", "running"], kind="export", limit=10)
    req = route.calls.last.request
    assert req.url.params.get_list("status") == ["pending", "running"]
    assert req.url.params.get_list("kind") == ["export"]
    assert req.url.params["limit"] == "10"
    assert isinstance(page, JobPage)
    assert page.total == 1


def test_get_job(client, respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=_job("running"))
    )
    job = client.jobs.get(JOB_ID)
    assert job.status == "running"


def test_retry_failed_job_items(client, respx_mock):
    route = respx_mock.post(f"{API}/async-jobs/{JOB_ID}/retry-failed").mock(
        return_value=httpx.Response(
            202,
            json={"status": "queued", "job_id": JOB_ID, "queued": 3, "skipped": 1},
        )
    )
    result = client.jobs.retry_failed(JOB_ID)
    assert route.called
    assert isinstance(result, JobRetryResult)
    assert result.queued == 3


def test_retry_failed_job_items_maps_conflict(client, respx_mock):
    respx_mock.post(f"{API}/async-jobs/{JOB_ID}/retry-failed").mock(
        return_value=httpx.Response(409, json={"detail": "nothing retryable"})
    )
    with pytest.raises(ConflictError):
        client.jobs.retry_failed(JOB_ID)


def test_wait_polls_until_completed(client, respx_mock):
    route = respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(200, json=_job("pending")),
            httpx.Response(200, json=_job("running", progress_pct=50)),
            httpx.Response(200, json=_job("completed", progress_pct=100)),
        ]
    )
    seen = []
    job = client.jobs.wait(
        JOB_ID,
        timeout=5,
        poll_interval=0.001,
        on_progress=lambda j: seen.append(j.status),
    )
    assert job.status == "completed"
    assert seen == ["pending", "running", "completed"]
    assert route.call_count == 3


def test_wait_failed_raises(client, respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=_job("failed", error_message="boom"))
    )
    with pytest.raises(JobFailedError) as ei:
        client.jobs.wait(JOB_ID, timeout=1, poll_interval=0.001)
    assert ei.value.job.error_message == "boom"


def test_wait_cancelled_raises(client, respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=_job("cancelled"))
    )
    with pytest.raises(JobFailedError):
        client.jobs.wait(JOB_ID, timeout=1, poll_interval=0.001)


def test_wait_timeout(client, respx_mock):
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, json=_job("running"))
    )
    with pytest.raises(JobTimeoutError):
        client.jobs.wait(JOB_ID, timeout=0.01, poll_interval=0.001)
