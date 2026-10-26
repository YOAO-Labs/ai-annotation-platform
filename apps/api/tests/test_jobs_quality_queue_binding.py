from __future__ import annotations

import builtins
import uuid

import pytest

from aap_jobs.queue_contract import (
    JobsQueueUnavailable,
    decode_queue_request,
    encode_dispatch_request,
)
from app import deps
from app.config import Settings
from app.services import jobs_queue


def _configure(monkeypatch, **values):
    config = Settings(_env_file=None, **values)
    monkeypatch.setattr(jobs_queue, "settings", config)
    return config


def _install_sync_client(monkeypatch, calls, *, error=None):
    class FakeQueueClient:
        def __init__(self, origin, token):
            calls.append(("client", origin, token))

        def dispatch_job_sync(
            self, task_name, execution_id, args, kwargs=None, options=None
        ):
            request = decode_queue_request(
                encode_dispatch_request(
                    task_name,
                    execution_id,
                    args,
                    kwargs=kwargs,
                    options=options,
                )
            )
            calls.append(("dispatch", request))
            if error is not None:
                raise error
            return execution_id

    monkeypatch.setattr(jobs_queue, "JobsQueueClient", FakeQueueClient)


def test_remote_quality_dispatch_rejects_unsupported_run_kind_first(monkeypatch):
    _configure(monkeypatch, jobs_queue_url="http://jobs-queue:8190")
    monkeypatch.setattr(
        jobs_queue,
        "dispatch_job_sync",
        lambda **_kwargs: pytest.fail("unsupported kinds must fail before publish"),
    )
    dispatcher = jobs_queue.JobsQueueQualityDispatcher()

    with pytest.raises(ValueError):
        dispatcher.dispatch_run("unknown", uuid.uuid4(), execution_id="run-id")


def test_remote_dispatch_errors_do_not_import_workers_or_fall_back(monkeypatch):
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    calls = []
    _install_sync_client(monkeypatch, calls, error=RuntimeError("sensitive detail"))
    original_import = builtins.__import__
    worker_imports = []

    def reject_worker_import(name, *args, **kwargs):
        if name == "app.workers" or name.startswith("app.workers."):
            worker_imports.append(name)
            raise AssertionError(f"remote dispatch imported worker module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_worker_import)
    dispatcher = deps.get_quality_job_dispatcher()

    with pytest.raises(JobsQueueUnavailable) as failure:
        dispatcher.dispatch_run("mask_qc", uuid.uuid4(), execution_id="persisted-run")
    assert "sensitive detail" not in str(failure.value)
    assert any(call[0] == "dispatch" for call in calls)
    assert not worker_imports
