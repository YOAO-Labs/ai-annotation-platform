from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from aap_assets_inputs import contract as c
from app.application.preannotation_contract import BatchPredictionRequest
from app.application.preannotation_execution import (
    PreannotationInputDeferred,
    _apply_prediction_result_with_retries,
    _prepare_accepted_input,
)
from app.services.labeling_prediction_contract import ApplyPredictionResultError


class _Session:
    def __init__(self, job, task):
        self.job, self.task = job, task

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, model, _identity, **_kwargs):
        if model.__name__ == "AsyncJob":
            return self.job
        if model.__name__ == "Task":
            return self.task
        return None


def _request(*, protocol=c.INPUT_SCHEMA, input_ids=None):
    job_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    input_id = uuid.uuid4()
    mapping = input_ids if input_ids is not None else {str(task_id): str(input_id)}
    payload = {
        "execution_scope": "workbench",
        "task_ids": [str(task_id)],
        "scope": {"task_ids": [str(task_id)]},
        "accepted_inputs_protocol": protocol,
        "accepted_input_ids": mapping,
    }
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        task_ids=[str(task_id)],
        execution_scope="workbench",
        async_job_id=str(job_id),
        user_id=str(actor_id),
        accepted_inputs_protocol=protocol,
        accepted_input_ids=mapping if protocol is not None else None,
    )
    job = SimpleNamespace(
        status="pending",
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        payload=payload,
    )
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        file_type="image",
        version=17,
    )
    return request, job, task, input_id


def _session_factory(job, task):
    return lambda: _Session(job, task)


@pytest.mark.asyncio
async def test_missing_legacy_workbench_image_identity_fails_closed(monkeypatch):
    request, job, task, _input_id = _request(protocol=None)
    job.payload.pop("accepted_inputs_protocol")
    job.payload.pop("accepted_input_ids")
    called = False

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        nonlocal called
        called = True
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    with pytest.raises(ValueError, match="legacy Workbench image"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pass
    assert not called
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_remote_pending_input_defers_before_worker_claim(monkeypatch):
    request, job, task, _input_id = _request()

    class Gateway:
        async def ensure_reference(self, _request):
            raise c.InputFailure("input_not_ready")

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        yield Gateway()

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    with pytest.raises(PreannotationInputDeferred, match="input_not_ready"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pytest.fail("pending input must not reach the inference workflow")
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_ready_input_is_downloaded_to_a_private_path_and_keeps_receipt(
    monkeypatch,
):
    request, job, task, input_id = _request()
    input_record = SimpleNamespace(
        input_id=input_id,
        job_id=uuid.UUID(request.async_job_id),
        task_id=task.id,
        project_id=task.project_id,
        actor_id=uuid.UUID(request.user_id),
        task_version=task.version,
        sha256="a" * 64,
        byte_size=4,
    )
    reference = SimpleNamespace(
        input=input_record, url="https://inputs.test/source?versionId=v1"
    )
    downloaded = []

    class Gateway:
        async def ensure_reference(self, _request):
            return reference

        async def download(self, _reference, destination):
            destination.write_bytes(b"data")
            destination.chmod(0o400)
            downloaded.append(destination)

        async def verify_source(self, _request):
            return input_record

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        yield Gateway()

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    async with _prepare_accepted_input(
        request, _session_factory(job, task)
    ) as prepared:
        assert prepared.reference.url.endswith("versionId=v1")
        assert prepared.local_path == downloaded[0]
        assert prepared.local_path.read_bytes() == b"data"
        assert prepared.local_path.stat().st_mode & 0o777 == 0o400
        assert prepared.local_path.parent.stat().st_mode & 0o777 == 0o700
        assert await prepared.verify_source() is input_record
    assert not prepared.local_path.exists()


@pytest.mark.asyncio
async def test_request_input_map_must_match_durable_job_scope(monkeypatch):
    request, job, task, _input_id = _request()
    job.payload["accepted_input_ids"] = {str(task.id): str(uuid.uuid4())}

    @asynccontextmanager
    async def unexpected_input_gateway(**_kwargs):
        pytest.fail("durable-map mismatch must fail before input access")
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", unexpected_input_gateway
    )
    with pytest.raises(ValueError, match="differs from durable job scope"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pass


@pytest.mark.asyncio
async def test_busy_write_retries_the_same_model_result_after_rollback(monkeypatch):
    request = SimpleNamespace(result=[{"class_name": "car"}])
    calls = []

    class Writer:
        async def apply_prediction_result(self, received):
            calls.append(received)
            if len(calls) < 3:
                raise ApplyPredictionResultError(
                    code="input_busy",
                    status_code=409,
                    detail={"reason": "input_busy"},
                )
            return "saved"

    class Session:
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def no_sleep(_delay):
        return None

    import app.application.preannotation_execution as execution

    monkeypatch.setattr(execution.asyncio, "sleep", no_sleep)
    session = Session()
    result = await _apply_prediction_result_with_retries(Writer(), session, request)

    assert result == "saved"
    assert calls == [request, request, request]
    assert all(received is request for received in calls)
    assert session.rollbacks == 2


@pytest.mark.asyncio
async def test_failed_prediction_write_retries_same_receipt_after_rollback(monkeypatch):
    from app.workers.predictions_retry import _apply_retry_prediction_with_retries

    request = SimpleNamespace(result=[{"class_name": "car"}])
    calls = []

    class Writer:
        async def apply_prediction_result(self, received):
            calls.append(received)
            if len(calls) == 2:
                raise ApplyPredictionResultError(
                    code="input_busy",
                    status_code=409,
                    detail={"reason": "input_busy"},
                )
            return "saved"

    class Session:
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def no_sleep(_delay):
        return None

    import app.workers.predictions_retry as retry_worker

    monkeypatch.setattr(retry_worker.asyncio, "sleep", no_sleep)
    session = Session()
    result = await _apply_retry_prediction_with_retries(Writer(), session, request)

    assert result == "saved"
    assert len(calls) == 3 and all(received is request for received in calls)
    assert session.rollbacks == 1
