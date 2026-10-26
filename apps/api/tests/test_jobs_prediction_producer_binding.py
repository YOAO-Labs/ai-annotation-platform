from __future__ import annotations

import builtins
import uuid

import pytest

from aap_jobs.queue_contract import JobsQueueUnavailable
from app import deps
from app.application.preannotation_contract import BatchPredictionRequest
from app.config import Settings
from app.services import jobs_queue


def _configure_remote(monkeypatch):
    config = Settings(
        _env_file=None,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    monkeypatch.setattr(jobs_queue, "settings", config)
    return config


def _install_sync_client(monkeypatch, *, error=None):
    calls = []

    class FakeQueueClient:
        def __init__(self, origin, token):
            calls.append(("client", origin, token))

        def dispatch_job_sync(
            self, task_name, execution_id, args, kwargs=None, options=None
        ):
            calls.append(
                {
                    "task_name": task_name,
                    "execution_id": execution_id,
                    "args": args,
                    "kwargs": kwargs,
                    "options": options,
                }
            )
            if error is not None:
                raise error
            return execution_id

    monkeypatch.setattr(jobs_queue, "JobsQueueClient", FakeQueueClient)
    return calls


def test_remote_prediction_failure_never_imports_workers_or_falls_back(monkeypatch):
    _configure_remote(monkeypatch)
    calls = _install_sync_client(monkeypatch, error=RuntimeError("broker detail"))
    original_import = builtins.__import__
    worker_imports = []

    def reject_worker_import(name, *args, **kwargs):
        if name == "app.workers" or name.startswith("app.workers."):
            worker_imports.append(name)
            raise AssertionError(f"remote dispatch imported worker module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_worker_import)
    batch_dispatcher = deps.get_batch_prediction_dispatcher()
    retry_dispatcher = deps.get_prediction_retry_dispatcher()
    with pytest.raises(JobsQueueUnavailable):
        batch_dispatcher.dispatch(
            BatchPredictionRequest(
                project_id="project-id",
                ml_backend_id="backend-id",
                async_job_id="durable-job-id",
            ),
            queue="ml",
        )
    with pytest.raises(JobsQueueUnavailable):
        retry_dispatcher.dispatch_prediction_retry(
            uuid.uuid4(),
            uuid.uuid4(),
            execution_queue="ml",
            job_id=uuid.uuid4(),
        )

    assert len([call for call in calls if isinstance(call, dict)]) == 2
    assert not worker_imports


def test_http_queue_failure_signals_existing_sync_fallback(monkeypatch):
    config = _configure_remote(monkeypatch)
    _install_sync_client(monkeypatch, error=RuntimeError("broker detail"))
    from app.api.v1 import me
    from app.middleware import audit

    monkeypatch.setattr(me, "settings", config)
    monkeypatch.setattr(audit, "settings", config)

    assert not audit._enqueue_audit({"action": "http.post"})
    assert not me._enqueue_task_events([{"id": str(uuid.uuid4())}])
