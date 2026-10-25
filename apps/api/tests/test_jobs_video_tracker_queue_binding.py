from __future__ import annotations

import asyncio
import builtins
import json
import uuid

import httpx
import pytest

from aap_jobs.queue_contract import JobsQueueUnavailable, load_queue_configuration
from app import deps
from app.config import Settings
from app.services import jobs_queue

_TASK_NAME = "app.workers.video_tracker.run_video_tracker_job"
_REAL_SYNC_CLIENT = httpx.Client


def _configure(monkeypatch, **values):
    config = Settings(_env_file=None, **values)
    monkeypatch.setattr(jobs_queue, "settings", config)
    return config


def _install_sync_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    client_options = []

    def build(**kwargs):
        client_options.append(kwargs)
        return _REAL_SYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", build)
    return client_options


def test_remote_tracker_dispatch_uses_shared_client_and_gpu_route(
    monkeypatch,
):
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    job_id = uuid.UUID("4de18a66-70b8-46bc-a0f0-7e95f8c6d9e0")
    execution_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(jobs_queue.uuid, "uuid4", lambda: execution_id)
    requests = []

    def handler(request):
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

    client_options = _install_sync_transport(monkeypatch, handler)
    dispatcher = deps.get_video_tracker_job_dispatcher()

    async def dispatch_in_active_loop():
        return dispatcher.dispatch_tracker_job(job_id)

    assert asyncio.run(dispatch_in_active_loop()) == str(execution_id)
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "schema": "aap-jobs-queue/v1",
        "operation": "dispatch",
        "task_name": _TASK_NAME,
        "execution_id": str(execution_id),
        "args": [str(job_id)],
        "kwargs": {},
        "options": {},
    }
    assert load_queue_configuration()["task_routes"][_TASK_NAME] == {"queue": "gpu"}
    assert client_options[0]["timeout"] == 10.0
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False


def test_remote_factory_defers_an_invalid_token_until_dispatch(monkeypatch):
    _configure(monkeypatch, jobs_queue_url="http://jobs-queue:8190")
    clients = []
    monkeypatch.setattr(
        jobs_queue,
        "JobsQueueClient",
        lambda *_args: clients.append(True),
    )

    dispatcher = deps.get_video_tracker_job_dispatcher()
    assert isinstance(dispatcher, jobs_queue.JobsQueueVideoTrackerDispatcher)
    assert not clients
    with pytest.raises(JobsQueueUnavailable):
        dispatcher.dispatch_tracker_job(uuid.uuid4())
    assert not clients


def test_remote_failure_is_sanitized_without_worker_import_or_fallback(monkeypatch):
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"detail": "sensitive broker detail"})

    _install_sync_transport(monkeypatch, handler)
    original_import = builtins.__import__
    worker_imports = []

    def reject_worker_import(name, *args, **kwargs):
        if name == "app.workers" or name.startswith("app.workers."):
            worker_imports.append(name)
            raise AssertionError(f"remote dispatch imported worker module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_worker_import)
    dispatcher = deps.get_video_tracker_job_dispatcher()
    with pytest.raises(JobsQueueUnavailable) as failure:
        dispatcher.dispatch_tracker_job(uuid.uuid4())

    assert "sensitive broker detail" not in str(failure.value)
    assert len(requests) == 1
    assert not worker_imports


def test_blank_url_keeps_celery_dispatcher_and_dependency_delegates(monkeypatch):
    _configure(monkeypatch)
    dispatcher = jobs_queue.build_video_tracker_job_dispatcher()
    assert type(dispatcher).__name__ == "CeleryVideoTrackerJobDispatcher"
    assert type(dispatcher).__module__ == "app.workers.video_tracker_dispatch"

    expected = object()
    monkeypatch.setattr(
        jobs_queue, "build_video_tracker_job_dispatcher", lambda: expected
    )
    assert deps.get_video_tracker_job_dispatcher() is expected


def test_mask_repair_worker_uses_selected_dispatcher_without_database(monkeypatch):
    from app.workers import mask_repair

    _configure(monkeypatch, jobs_queue_url="http://jobs-queue:8190")
    selected = []

    async def capture_dispatcher(batch_id, celery_task_id, *, tracker_dispatcher):
        selected.append((batch_id, celery_task_id, tracker_dispatcher))

    monkeypatch.setattr(mask_repair, "_run_mask_repair", capture_dispatcher)

    mask_repair.run_mask_repair.run("batch-id")

    assert len(selected) == 1
    assert selected[0][:2] == ("batch-id", None)
    assert isinstance(selected[0][2], jobs_queue.JobsQueueVideoTrackerDispatcher)
