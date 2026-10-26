from __future__ import annotations

import builtins
import uuid

import httpx
import pytest
from aap_jobs.queue_contract import JobsQueueUnavailable
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
        dispatcher.dispatch_tracker_job(uuid.uuid4(), execution_id=str(uuid.uuid4()))
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
        dispatcher.dispatch_tracker_job(uuid.uuid4(), execution_id=str(uuid.uuid4()))

    assert "sensitive broker detail" not in str(failure.value)
    assert len(requests) == 1
    assert not worker_imports
