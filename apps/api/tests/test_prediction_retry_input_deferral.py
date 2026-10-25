from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import inspect

from aap_assets_inputs.contract import InputFailure
from app.workers.predictions_retry import (
    FrozenInputRetryDeferred,
    _prepare_retry_input,
)


def test_empty_prediction_is_valid_but_missing_target_result_is_not():
    from app.application.durable_prediction_retry import _one_result

    task_id = uuid.uuid4()
    empty = SimpleNamespace(
        task_id=str(task_id),
        result=[],
        score=0,
        model_version=None,
        inference_time_ms=0,
        meta=None,
    )
    assert _one_result([empty], task_id) is empty
    with pytest.raises(ValueError, match="exactly one"):
        _one_result([], task_id)


class _ReadSession:
    def __init__(self, failed, task, backend):
        self.failed, self.task, self.backend = failed, task, backend
        self.identity_map = {}
        self.autoflush = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, model, _identity):
        if model.__name__ == "FailedPrediction":
            return self.failed
        if model.__name__ == "Task":
            return self.task
        if model.__name__ == "MLBackendRegistry":
            return self.backend
        return None

    async def execute(self, statement):
        model = inspect(statement.column_descriptions[0]["entity"]).mapper.class_
        row = self.failed if model.__name__ == "FailedPrediction" else self.task
        return SimpleNamespace(scalar_one_or_none=lambda: row)


def _frozen_failed_prediction():
    failed_id, task_id, project_id, backend_id = (uuid.uuid4() for _ in range(4))
    input_id, origin_job_id, actor_id = (uuid.uuid4() for _ in range(3))
    task = SimpleNamespace(id=task_id, project_id=project_id, file_type="image")
    backend = SimpleNamespace(id=backend_id)
    failed = SimpleNamespace(
        id=failed_id,
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=backend_id,
        retry_count=0,
        extra={
            "retry_pending": True,
            "execution_scope": "workbench",
            "accepted_input": {
                "input_id": str(input_id),
                "job_id": str(origin_job_id),
                "task_id": str(task_id),
                "sha256": "a" * 64,
                "byte_size": 128,
            },
        },
    )
    return failed, task, backend, actor_id, input_id, origin_job_id


def _session_factory(failed, task, backend):
    return lambda: _ReadSession(failed, task, backend)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "code"),
    [
        ("reference", "input_not_ready"),
        ("download", "storage_unavailable"),
    ],
)
async def test_transient_frozen_input_wait_has_no_retry_side_effects(
    monkeypatch, failure_point, code
):
    from app.services import async_job as async_job_service
    from app.services.notification import NotificationService

    failed, task, backend, actor_id, input_id, origin_job_id = (
        _frozen_failed_prediction()
    )
    events = []
    reference = SimpleNamespace(
        input=SimpleNamespace(
            input_id=input_id,
            job_id=origin_job_id,
            task_id=task.id,
            project_id=task.project_id,
            sha256="a" * 64,
            byte_size=128,
        ),
        url="https://inputs.test/source?versionId=stable-version",
    )

    class Gateway:
        async def reference(self, request):
            events.append(("reference", request.input_id))
            if failure_point == "reference":
                raise InputFailure(code)
            return reference

        async def download(self, _reference, _destination):
            events.append("download")
            if failure_point == "download":
                raise InputFailure(code)

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    async def unexpected_side_effect(*_args, **_kwargs):
        events.append("unexpected-side-effect")
        pytest.fail("a transient input wait must precede retry side effects")

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    monkeypatch.setattr(async_job_service, "track_job", unexpected_side_effect)
    monkeypatch.setattr(NotificationService, "notify_terminal", unexpected_side_effect)

    with pytest.raises(FrozenInputRetryDeferred, match=code):
        async with _prepare_retry_input(
            _session_factory(failed, task, backend),
            str(failed.id),
            str(actor_id),
        ):
            pytest.fail("pending input must defer before model execution")

    assert events[0] == ("reference", input_id)
    assert "unexpected-side-effect" not in events
    assert failed.retry_count == 0
    assert failed.extra["retry_pending"] is True


@pytest.mark.asyncio
async def test_recovery_reuses_the_claimed_input_and_version(monkeypatch):
    failed, task, backend, _actor_id, input_id, origin_job_id = (
        _frozen_failed_prediction()
    )
    request_ids = []
    events = []
    record = SimpleNamespace(
        input_id=input_id,
        job_id=origin_job_id,
        task_id=task.id,
        project_id=task.project_id,
        sha256="a" * 64,
        byte_size=128,
    )
    reference = SimpleNamespace(
        input=record,
        url="https://inputs.test/source?versionId=claimed-version",
    )

    class Gateway:
        async def reference(self, request):
            request_ids.append((request.input_id, request.job_id))
            if len(request_ids) == 1:
                raise InputFailure("input_not_ready")
            return reference

        async def download(self, _reference, destination):
            destination.write_bytes(b"original accepted bytes")
            events.append("download")

        async def verify_source(self, _request):
            events.append("verify")
            return record

    @asynccontextmanager
    async def fake_gateway(**_kwargs):
        yield Gateway()

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", fake_gateway)
    factory = _session_factory(failed, task, backend)
    with pytest.raises(FrozenInputRetryDeferred, match="input_not_ready"):
        async with _prepare_retry_input(factory, str(failed.id)):
            pytest.fail("the first pending read must defer before model execution")

    async with _prepare_retry_input(factory, str(failed.id)) as prepared:
        assert prepared.accepted_input["input_id"] == input_id
        assert prepared.reference.url.endswith("versionId=claimed-version")
        assert prepared.local_path.read_bytes() == b"original accepted bytes"
    assert request_ids == [
        (input_id, origin_job_id),
        (input_id, origin_job_id),
    ]
    assert events == ["download", "verify"]
    assert failed.extra["retry_pending"] is True


def test_frozen_input_deferral_retries_the_same_celery_execution_past_three(
    monkeypatch,
):
    from celery.exceptions import Retry

    from app.workers import predictions_retry as retry_worker

    task = retry_worker.retry_failed_prediction
    assert task.max_retries is None
    captured = {}

    def defer_before_job(coroutine):
        frame = coroutine.cr_frame
        captured["failed_id"] = frame.f_locals["failed_id"]
        captured["celery_task_id"] = frame.f_locals["celery_task_id"]
        coroutine.close()
        raise FrozenInputRetryDeferred("input_not_ready")

    monkeypatch.setattr(retry_worker.asyncio, "run", defer_before_job)
    task.push_request(
        id="original-retry-execution",
        retries=8,
        is_eager=True,
        called_directly=False,
        args=["failed-id", "actor-id"],
        kwargs={},
    )
    try:
        with pytest.raises(Retry) as retry_info:
            task.run("failed-id", "actor-id")
        captured["task_id"] = task.request.id
        captured["retries"] = task.request.retries
    finally:
        task.pop_request()

    assert captured["failed_id"] == "failed-id"
    assert captured["celery_task_id"] == "original-retry-execution"
    assert captured["task_id"] == "original-retry-execution"
    assert captured["retries"] == 8
    assert retry_info.value.when == 30
