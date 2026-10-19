from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from aap_assets_video_inputs.evidence import (
    VideoInputEvidence,
    VideoInputFailureEvidence,
)
from aap_assets_video_inputs.values import FrameTimeEntry
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import LabelingWriteScope


def _evidence(*, failure: bool = False):
    values = {
        "input_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "plan_digest": "a" * 64,
        "timebase_digest": "b" * 64,
        "entry": FrameTimeEntry(
            ordinal=2, frame_index=4, pts_ms=200, is_keyframe=False
        ),
        "source_sha256": "c" * 64,
    }
    if failure:
        return VideoInputFailureEvidence(
            **values, frame_id=uuid.uuid4(), frame_sha256=None
        )
    return VideoInputEvidence(**values, frame_id=uuid.uuid4(), frame_sha256="d" * 64)


def _scope(evidence: VideoInputEvidence, *, execution_job_id=None):
    return LabelingWriteScope(
        task_id=evidence.task_id,
        project_id=evidence.project_id,
        actor_id=uuid.uuid4(),
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        task_version=1,
        task_status="pending",
        assignee_id=None,
        review_round_id=None,
        source_annotation_id=None,
        source_annotation_version=None,
        expected_batch_id=None,
        expected_batch_id_known=True,
        job_id=execution_job_id or uuid.uuid4(),
        job_id_known=True,
        source_binding_known=True,
        task_source_generation=1,
        source_dataset_item_id=uuid.uuid4(),
        source_file_path="capture.mp4",
        source_file_type="video",
    )


def _request(evidence: VideoInputEvidence, *, scope=None):
    return ApplyPredictionResultRequest(
        task_id=evidence.task_id,
        project_id=evidence.project_id,
        ml_backend_id=None,
        result=[],
        write_scope=scope or _scope(evidence),
        accepted_video_input=evidence,
    )


def test_video_write_request_separates_origin_and_execution_job():
    from app.services.prediction import _validate_video_write_request

    evidence = _evidence()
    scope = _scope(evidence)
    assert scope.job_id != evidence.job_id
    assert _validate_video_write_request(_request(evidence, scope=scope)) is evidence


def test_video_write_request_rejects_untyped_pipeline_override():
    from app.services.prediction import _validate_video_write_request

    evidence = _evidence()
    request = _request(evidence)
    request = replace(
        request,
        accepted_video_input=None,
        pipeline_extra={
            "accepted_video_input": evidence.model_dump(mode="json", by_alias=True)
        },
    )
    with pytest.raises(ValueError, match="typed evidence"):
        _validate_video_write_request(request)


@pytest.mark.parametrize("frame_index", [0, True, "4"])
def test_video_write_rejects_results_for_a_different_selected_frame(frame_index):
    from app.services.prediction import _validate_video_write_request

    evidence = _evidence()
    request = replace(
        _request(evidence),
        result=[{"geometry": {"type": "video_bbox", "frame_index": frame_index}}],
    )
    with pytest.raises(ValueError, match="selected frame"):
        _validate_video_write_request(request)


@pytest.mark.asyncio
async def test_video_write_replay_returns_existing_receipt_under_task_lock(monkeypatch):
    from app.services import prediction as prediction_module

    evidence = _evidence()
    request = _request(evidence)
    created_at = datetime.now(timezone.utc)
    existing = SimpleNamespace(
        id=uuid.uuid4(), created_at=created_at, tool_unit_id="bbox"
    )
    task = SimpleNamespace(
        id=evidence.task_id, project_id=evidence.project_id, total_predictions=1
    )
    calls = []

    class Result:
        def scalars(self):
            return self

        def first(self):
            return existing

    class Session:
        async def get(self, *_args, **_kwargs):
            return task

        async def execute(self, statement):
            compiled = statement.compile(dialect=postgresql.dialect())
            calls.append((compiled.string, compiled.params))
            return Result()

        def add(self, _row):
            pytest.fail("idempotent replay must not create another Prediction")

        async def flush(self):
            pytest.fail("idempotent replay must not flush a duplicate")

    async def preflight(*_args, **_kwargs):
        return None

    async def lock(_db, scope, **kwargs):
        assert scope.job_id != evidence.job_id
        assert kwargs["accepted_video_input"] == evidence
        return task, object(), object()

    async def prepare(*_args, **_kwargs):
        pytest.fail("duplicate result should return before payload preparation")

    monkeypatch.setattr(
        prediction_module, "preflight_mask_payload_for_write", preflight
    )
    monkeypatch.setattr(prediction_module, "lock_labeling_write", lock)
    monkeypatch.setattr(prediction_module, "prepare_mask_payload_for_write", prepare)

    receipt = await prediction_module.PredictionService(
        Session()
    ).apply_prediction_result(request)
    assert receipt.id == existing.id
    assert receipt.created_at == created_at
    assert len(calls) == 1
    sql, params = calls[0]
    assert "->>" in sql
    assert "accepted_video_execution_job_id" in repr(params)
    assert "accepted_video_input" in repr(params)
    assert "plan_digest" in repr(params)
    assert "ordinal" in repr(params)


@pytest.mark.asyncio
async def test_persist_stamps_typed_evidence_and_trusted_execution_job():
    from app.services.prediction import PredictionService

    evidence = _evidence()
    request = _request(evidence)
    rows = []

    class Session:
        def add(self, row):
            rows.append(row)

        async def flush(self):
            for row in rows:
                if getattr(row, "created_at", None) is None:
                    row.created_at = datetime.now(timezone.utc)

    task = SimpleNamespace(total_predictions=0)
    await PredictionService(Session())._persist_prediction_result(request, task, [])
    metadata = next(row for row in rows if row.__class__.__name__ == "PredictionMeta")
    assert metadata.extra["accepted_video_input"] == evidence.model_dump(
        mode="json", by_alias=True
    )
    assert metadata.extra["accepted_video_execution_job_id"] == str(
        request.write_scope.job_id
    )
    assert metadata.extra["accepted_video_execution_job_id"] != str(evidence.job_id)
    assert task.total_predictions == 1


@pytest.mark.asyncio
async def test_raw_video_fence_accepts_current_workflow_version_with_original_source(
    monkeypatch,
):
    from app.services import accepted_video_inputs, labeling_write_guard
    from tests.test_accepted_video_inputs_gateway import facts

    _scope_value, _header, members, _sealed = facts()
    plan = members[0].input.plan
    binding = plan.input.task
    scope = replace(
        _scope(_evidence()),
        task_id=binding.task_id,
        project_id=binding.project_id,
        job_id=plan.input.job_id,
        task_version=binding.task_version + 1,
        task_source_generation=binding.task_source_generation,
        source_dataset_item_id=binding.dataset_item_id,
        source_file_path=binding.file_path,
        source_file_type=binding.file_type,
    )

    async def lock_source(_db, request):
        assert request.input_id == plan.input.input_id
        assert request.job_id == scope.job_id
        assert request.actor_id == scope.actor_id
        return SimpleNamespace(plan=plan)

    monkeypatch.setattr(accepted_video_inputs, "lock_video_input_source", lock_source)
    await labeling_write_guard._lock_accepted_video_input_source(object(), scope, plan)


@pytest.mark.asyncio
async def test_preannotation_video_job_requires_matching_frame_proof(monkeypatch):
    from fastapi import HTTPException

    from aap_assets_video_inputs.values import VIDEO_INPUT_SCHEMA
    from app.services import labeling_write_guard

    evidence = _evidence()
    job_id = uuid.uuid4()
    scope = _scope(evidence, execution_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        project_id=scope.project_id,
        user_id=scope.actor_id,
        kind="batch_predict",
        status="running",
        payload={
            "batch_id": None,
            "execution_scope": "bulk",
            "task_ids": [str(scope.task_id)],
            "accepted_video_inputs_protocol": VIDEO_INPUT_SCHEMA,
            "accepted_video_manifest_id": str(job_id),
            "accepted_video_selection_digest": "f" * 64,
            "accepted_video_selection": {"task_ids": [str(scope.task_id)]},
        },
    )

    async def locked_job(*_args, **_kwargs):
        return job

    monkeypatch.setattr(labeling_write_guard, "_locked_row", locked_job)

    with pytest.raises(HTTPException) as no_proof:
        await labeling_write_guard._assert_execution_current(
            object(), scope, task_id=scope.task_id, batch_id=None
        )
    assert (
        no_proof.value.detail["reason"] == "prediction_video_manifest_origin_mismatch"
    )

    with pytest.raises(HTTPException) as wrong_job:
        await labeling_write_guard._assert_execution_current(
            object(),
            scope,
            task_id=scope.task_id,
            batch_id=None,
            accepted_video_input=evidence,
        )
    assert (
        wrong_job.value.detail["reason"] == "prediction_video_manifest_origin_mismatch"
    )

    matching = evidence.model_copy(update={"job_id": job_id})
    await labeling_write_guard._assert_execution_current(
        object(),
        scope,
        task_id=scope.task_id,
        batch_id=None,
        accepted_video_input=matching,
    )

    with pytest.raises(HTTPException) as mixed:
        await labeling_write_guard._assert_execution_current(
            object(),
            scope,
            task_id=scope.task_id,
            batch_id=None,
            accepted_input_job_id=job_id,
            accepted_video_input=matching,
        )
    assert mixed.value.detail["reason"] == "prediction_input_manifest_origin_mismatch"


@pytest.mark.asyncio
async def test_video_retry_prepares_exact_retained_frame_before_retry_admission(
    monkeypatch,
):
    import aap_assets_video_inputs.evidence as video_evidence
    from app.services import accepted_video_inputs
    from app.workers.predictions_retry import _prepare_retry_input

    failure = _evidence(failure=True)
    actor_id = uuid.uuid4()
    failed_id, backend_id = uuid.uuid4(), uuid.uuid4()
    failed = SimpleNamespace(
        id=failed_id,
        task_id=failure.task_id,
        project_id=failure.project_id,
        ml_backend_id=backend_id,
        extra={
            "execution_unit": "frame",
            "accepted_video_input": failure.model_dump(mode="json", by_alias=True),
        },
    )
    task = SimpleNamespace(
        id=failure.task_id, project_id=failure.project_id, file_type="video"
    )
    backend = SimpleNamespace(id=backend_id)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity):
            if model.__name__ == "FailedPrediction":
                return failed
            if model.__name__ == "Task":
                return task
            if model.__name__ == "MLBackendRegistry":
                return backend
            return None

    class Plan:
        pass

    plan = Plan()
    blob = object()
    reference = SimpleNamespace(
        plan=plan,
        entry=failure.entry,
        blob=blob,
        url="https://frame.test/f?versionId=exact",
    )
    frame = SimpleNamespace(plan=plan, entry=failure.entry, blob=blob)
    ready = VideoInputEvidence(
        input_id=failure.input_id,
        job_id=failure.job_id,
        task_id=failure.task_id,
        project_id=failure.project_id,
        plan_digest=failure.plan_digest,
        timebase_digest=failure.timebase_digest,
        frame_id=failure.frame_id,
        entry=failure.entry,
        source_sha256=failure.source_sha256,
        frame_sha256="e" * 64,
    )
    monkeypatch.setattr(video_evidence, "ready_evidence", lambda *_args: ready)
    requests = []

    class Gateway:
        async def verify(self, request):
            assert request.job_id == failure.job_id
            assert request.actor_id == actor_id
            return SimpleNamespace(status="ready", plan=plan)

        async def ensure_reference(self, request):
            requests.append(request)
            return reference

        async def frame(self, request):
            assert request is requests[-1]
            return frame

    @asynccontextmanager
    async def gateway_factory(**_kwargs):
        yield Gateway()

    monkeypatch.setattr(accepted_video_inputs, "video_input_gateway", gateway_factory)

    async with _prepare_retry_input(
        lambda: Session(), str(failed_id), actor_id=str(actor_id)
    ) as prepared:
        assert prepared.accepted_video_failure == failure
        assert prepared.accepted_video_input == ready
        assert prepared.reference.url == reference.url
        assert prepared.request.actor_id == actor_id
        assert prepared.request.job_id == failure.job_id
        assert prepared.request.ordinal == failure.entry.ordinal
        assert prepared.terminal_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_at", ["source", "frame"])
async def test_video_retry_busy_frame_defers_before_model_execution(
    monkeypatch, wait_at
):
    from aap_assets_video_inputs.execution import VideoInputFailure
    from app.services import accepted_video_inputs
    from app.workers.predictions_retry import (
        FrozenInputRetryDeferred,
        _prepare_retry_input,
    )

    failure = _evidence(failure=True)
    actor_id = uuid.uuid4()
    failed = SimpleNamespace(
        id=uuid.uuid4(),
        task_id=failure.task_id,
        project_id=failure.project_id,
        ml_backend_id=uuid.uuid4(),
        retry_count=0,
        extra={
            "execution_unit": "frame",
            "retry_pending": True,
            "accepted_video_input": failure.model_dump(mode="json", by_alias=True),
        },
    )
    task = SimpleNamespace(
        id=failure.task_id, project_id=failure.project_id, file_type="video"
    )
    backend = SimpleNamespace(id=failed.ml_backend_id)
    events = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity):
            if model.__name__ == "FailedPrediction":
                return failed
            if model.__name__ == "Task":
                return task
            if model.__name__ == "MLBackendRegistry":
                return backend
            return None

    class Gateway:
        async def verify(self, _request):
            events.append("source")
            if wait_at == "source":
                raise VideoInputFailure("input_busy")
            return SimpleNamespace(status="ready", plan=object())

        async def ensure_reference(self, _request):
            events.append("reference")
            raise VideoInputFailure("frame_busy")

    @asynccontextmanager
    async def gateway_factory(**_kwargs):
        yield Gateway()

    @asynccontextmanager
    async def unexpected_job(*_args, **_kwargs):
        events.append("job")
        pytest.fail("a busy frame must defer before creating a retry job")
        yield None

    monkeypatch.setattr(accepted_video_inputs, "video_input_gateway", gateway_factory)
    monkeypatch.setattr("app.services.async_job.track_job", unexpected_job)

    code = "input_busy" if wait_at == "source" else "frame_busy"
    with pytest.raises(FrozenInputRetryDeferred, match=code):
        async with _prepare_retry_input(
            lambda: Session(), str(failed.id), str(actor_id)
        ):
            pytest.fail("a pending frame must defer before model execution")
    assert events == (["source"] if wait_at == "source" else ["source", "reference"])
    assert failed.retry_count == 0
    assert failed.extra["retry_pending"] is True


@pytest.mark.asyncio
async def test_video_frame_retry_requires_typed_evidence():
    from app.workers.predictions_retry import _prepare_retry_input

    failed_id, task_id, project_id, backend_id = (uuid.uuid4() for _ in range(4))
    failed = SimpleNamespace(
        id=failed_id,
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=backend_id,
        extra={"execution_unit": "frame", "execution_scope": "bulk"},
    )
    task = SimpleNamespace(id=task_id, project_id=project_id, file_type="video")
    backend = SimpleNamespace(id=backend_id)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity):
            if model.__name__ == "FailedPrediction":
                return failed
            if model.__name__ == "Task":
                return task
            if model.__name__ == "MLBackendRegistry":
                return backend
            return None

    async with _prepare_retry_input(
        lambda: Session(), str(failed_id), actor_id=str(uuid.uuid4())
    ) as prepared:
        assert isinstance(prepared.terminal_error, ValueError)
        assert "no retained evidence" in str(prepared.terminal_error)
        assert prepared.accepted_video_failure is None
        assert prepared.gateway is None
