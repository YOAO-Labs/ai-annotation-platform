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


@pytest.mark.asyncio
async def test_remote_quality_dispatch_uses_frozen_routes_and_execution_ids(
    monkeypatch,
):
    _configure(
        monkeypatch,
        jobs_queue_url="http://jobs-queue:8190",
        jobs_queue_token="service-token",
    )
    calls = []
    _install_sync_client(monkeypatch, calls)
    run_ids = [uuid.uuid4() for _ in range(3)]
    generated_ids = iter(
        uuid.UUID(f"00000000-0000-4000-8000-{value:012d}") for value in (1, 2, 3)
    )
    monkeypatch.setattr(jobs_queue.uuid, "uuid4", lambda: next(generated_ids))
    dispatcher = deps.get_quality_job_dispatcher()

    assert (
        dispatcher.dispatch_run("mask_qc", run_ids[0], execution_id="persisted-mask-qc")
        is None
    )
    assert (
        dispatcher.dispatch_run(
            "point_cloud_quality", run_ids[1], execution_id="persisted-point-cloud"
        )
        is None
    )
    assert (
        dispatcher.dispatch_run(
            "video_track_quality", run_ids[2], execution_id="persisted-video"
        )
        is None
    )
    repair_id = dispatcher.dispatch_mask_repair(run_ids[0], rollback=False)
    rollback_id = dispatcher.dispatch_mask_repair(run_ids[1], rollback=True)
    import_id = dispatcher.dispatch_mask_import(run_ids[2])

    expected_generated = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
    ]
    assert [repair_id, rollback_id, import_id] == expected_generated
    requests = [call[1] for call in calls if call[0] == "dispatch"]
    assert [
        (
            request.task_name,
            request.execution_id,
            request.args,
            request.kwargs,
            request.options,
        )
        for request in requests
    ] == [
        (
            "app.workers.mask_qc.run_mask_qc",
            "persisted-mask-qc",
            [str(run_ids[0])],
            {},
            {},
        ),
        (
            "app.workers.point_cloud_quality.run",
            "persisted-point-cloud",
            [str(run_ids[1])],
            {},
            {},
        ),
        (
            "app.workers.video_track_quality.run",
            "persisted-video",
            [str(run_ids[2])],
            {},
            {},
        ),
        (
            "app.workers.mask_repair.run_mask_repair",
            expected_generated[0],
            [str(run_ids[0])],
            {},
            {},
        ),
        (
            "app.workers.mask_repair.rollback_mask_repair",
            expected_generated[1],
            [str(run_ids[1])],
            {},
            {},
        ),
        (
            "app.workers.mask_format_import.run_mask_format_import",
            expected_generated[2],
            [str(run_ids[2])],
            {},
            {},
        ),
    ]


def test_remote_factory_defers_token_and_client_until_dispatch(monkeypatch):
    _configure(monkeypatch, jobs_queue_url="http://jobs-queue:8190")
    calls = []
    monkeypatch.setattr(
        jobs_queue,
        "_jobs_queue_token",
        lambda: calls.append("token") or "service-token",
    )
    _install_sync_client(monkeypatch, calls)

    dispatcher = deps.get_quality_job_dispatcher()
    assert isinstance(dispatcher, jobs_queue.JobsQueueQualityDispatcher)
    assert calls == []

    dispatcher.dispatch_run(
        "mask_qc",
        uuid.UUID("4de18a66-70b8-46bc-a0f0-7e95f8c6d9e0"),
        execution_id="persisted-run",
    )
    assert calls[0] == "token"
    assert calls[1] == ("client", "http://jobs-queue:8190", "service-token")


def test_remote_quality_dispatch_rejects_unsupported_run_kind_first(monkeypatch):
    _configure(monkeypatch, jobs_queue_url="http://jobs-queue:8190")
    monkeypatch.setattr(
        jobs_queue,
        "dispatch_job_sync",
        lambda **_kwargs: pytest.fail("unsupported kinds must fail before publish"),
    )
    dispatcher = jobs_queue.JobsQueueQualityDispatcher()

    with pytest.raises(ValueError, match="Unsupported quality run kind: unknown"):
        dispatcher.dispatch_run("unknown", uuid.uuid4(), execution_id="run-id")


def test_empty_queue_url_keeps_celery_dispatcher_and_dependency_delegates(
    monkeypatch,
):
    _configure(monkeypatch)
    dispatcher = jobs_queue.build_quality_job_dispatcher()
    assert type(dispatcher).__name__ == "CeleryQualityJobDispatcher"
    assert type(dispatcher).__module__ == "app.workers.quality_dispatch"

    expected = object()
    monkeypatch.setattr(jobs_queue, "build_quality_job_dispatcher", lambda: expected)
    assert deps.get_quality_job_dispatcher() is expected


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
