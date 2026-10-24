from __future__ import annotations

import asyncio

import pytest

from aap_jobs.queue_client import JobsQueueExecutionControl
from aap_jobs.queue_contract import (
    JobsQueueContractError,
    JobsQueueUnavailable,
    load_queue_configuration,
)
from app.config import Settings
from app import deps
from app.services import jobs_queue
from app.workers.celery_app import celery_app
from app.workers.job_execution_control import CeleryJobExecutionControl


_TASK_NAME = "app.workers.cross_frame_job.run_cross_frame_job"
_EXECUTION_ID = "predeclared-execution-id"
_JOB_ID = "8ccbf8e4-cce2-4eec-a15d-9f58222987a1"


def _configure(monkeypatch, **values):
    config = Settings(_env_file=None, **values)
    monkeypatch.setattr(jobs_queue, "settings", config)
    return config


def test_api_celery_assembly_uses_the_jobs_route_resource():
    config = load_queue_configuration()
    assert celery_app.conf.task_default_queue == config["task_default_queue"]
    assert celery_app.conf.task_serializer == config["task_serializer"]
    assert celery_app.conf.result_serializer == config["result_serializer"]
    assert celery_app.conf.accept_content == config["accept_content"]
    assert celery_app.conf.task_routes == config["task_routes"]
    assert celery_app.conf.task_routes[_TASK_NAME] == {"queue": "default"}
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.task_track_started is True
    assert celery_app.conf.worker_max_memory_per_child == 512_000


def test_settings_keep_queue_disabled_by_default_and_hide_the_token():
    config = Settings(_env_file=None, jobs_queue_token="service-token")
    assert config.jobs_queue_url == ""
    assert config.jobs_queue_token_file == ""
    assert "service-token" not in repr(config)


def test_embedded_dispatch_uses_send_task_with_stable_id_and_preserved_wire(
    monkeypatch,
):
    _configure(monkeypatch)
    calls = []

    def send_task(task_name, **kwargs):
        calls.append((task_name, kwargs))

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(celery_app, "send_task", send_task)
    monkeypatch.setattr(jobs_queue.asyncio, "to_thread", to_thread)

    result = asyncio.run(
        jobs_queue.dispatch_job(
            task_name=_TASK_NAME,
            execution_id=_EXECUTION_ID,
            args=[_JOB_ID],
            kwargs=None,
            options=None,
        )
    )
    assert result == _EXECUTION_ID
    assert calls == [
        (
            _TASK_NAME,
            {
                "args": [_JOB_ID],
                "kwargs": None,
                "task_id": _EXECUTION_ID,
            },
        )
    ]


def test_explicit_remote_dispatch_uses_selected_client_and_never_falls_back(
    monkeypatch,
):
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    clients = []

    class FakeQueueClient:
        def __init__(self, origin, token):
            clients.append((origin, token))

        async def dispatch_job(self, task_name, execution_id, args, **kwargs):
            clients.append((task_name, execution_id, args, kwargs))
            return execution_id

    def no_embedded_fallback(*_args, **_kwargs):
        raise AssertionError("explicit remote failure must not use the local broker")

    monkeypatch.setattr(jobs_queue, "JobsQueueClient", FakeQueueClient)
    monkeypatch.setattr(jobs_queue.asyncio, "to_thread", no_embedded_fallback)
    result = asyncio.run(
        jobs_queue.dispatch_job(
            task_name=_TASK_NAME,
            execution_id=_EXECUTION_ID,
            args=[_JOB_ID],
        )
    )
    assert result == _EXECUTION_ID
    assert clients == [
        ("http://jobs-queue:8190", "service-token"),
        (_TASK_NAME, _EXECUTION_ID, [_JOB_ID], {"kwargs": None, "options": None}),
    ]

    async def unavailable(*_args, **_kwargs):
        raise JobsQueueUnavailable()

    FakeQueueClient.dispatch_job = unavailable
    with pytest.raises(JobsQueueUnavailable):
        asyncio.run(
            jobs_queue.dispatch_job(
                task_name=_TASK_NAME,
                execution_id=_EXECUTION_ID,
                args=[_JOB_ID],
            )
        )


def test_remote_token_file_and_selector_failures_are_sanitized(tmp_path, monkeypatch):
    token_file = tmp_path / "jobs-queue-token"
    token_file.write_text("service-token\n", encoding="utf-8")
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token_file=str(token_file),
    )
    assert jobs_queue._jobs_queue_token() == "service-token"

    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="direct-token",
        jobs_queue_token_file=str(token_file),
    )
    with pytest.raises(JobsQueueUnavailable) as conflict:
        jobs_queue._jobs_queue_token()
    assert str(token_file) not in str(conflict.value)

    missing = tmp_path / "missing-token"
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token_file=str(missing),
    )
    with pytest.raises(JobsQueueUnavailable) as unreadable:
        jobs_queue._jobs_queue_token()
    assert str(missing) not in str(unreadable.value)


def test_sync_execution_control_is_remote_only_when_queue_url_is_selected(
    monkeypatch,
):
    _configure(monkeypatch)
    assert isinstance(
        jobs_queue.build_job_execution_control(), CeleryJobExecutionControl
    )

    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    control = jobs_queue.build_job_execution_control()
    assert isinstance(control, JobsQueueExecutionControl)
    assert control.origin == "http://jobs-queue:8190"
    assert control.token == "service-token"


def test_fastapi_dependency_uses_the_queue_execution_control_factory(monkeypatch):
    expected = object()
    monkeypatch.setattr(jobs_queue, "build_job_execution_control", lambda: expected)
    assert deps.get_job_execution_control() is expected


def test_embedded_dispatch_rejects_unqualified_options_before_publish(monkeypatch):
    _configure(monkeypatch)
    published = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *_args, **_kwargs: published.append(True)
    )

    with pytest.raises(JobsQueueContractError):
        asyncio.run(
            jobs_queue.dispatch_job(
                task_name=_TASK_NAME,
                execution_id=_EXECUTION_ID,
                args=[_JOB_ID],
                options={"queue": "ml"},
            )
        )
    assert not published
