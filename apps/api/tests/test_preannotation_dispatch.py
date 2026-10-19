"""Pure tests for the application batch-prediction boundary and Celery adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.preannotation_contract import BatchPredictionRequest
from app.application import preannotation_execution
from app.workers.batch_prediction_dispatch import CeleryBatchPredictionDispatcher


def test_celery_dispatch_preserves_request_and_runtime_options(monkeypatch):
    from app.workers.tasks import batch_predict

    assert batch_predict.name == "app.workers.tasks.batch_predict"
    captured = {}

    def fake_apply_async(*, args, kwargs, **options):
        captured.update(args=args, kwargs=kwargs, options=options)
        return SimpleNamespace(id="celery-id")

    monkeypatch.setattr(batch_predict, "apply_async", fake_apply_async)
    request = BatchPredictionRequest(
        project_id="project-id",
        ml_backend_id="backend-id",
        task_ids=[],
        prompt="find object",
        output_mode="box",
        batch_id="batch-id",
        user_id="actor-id",
        params={"threshold": 0.25},
        predict_mode="overwrite",
        model_id="model-id",
        task_type="detection",
        model_variants={"series": "yolov8", "size": "n"},
        class_filter=[2, 4],
        pipeline_stages=[{"stage": 3, "parent_stage": None}],
        execution_unit="frame",
        execution_scope="workbench",
        async_job_id="durable-job-id",
        frozen_model_ref={"model_version_id": "version-id"},
        accepted_inputs_protocol="aap-assets-inputs/v1",
        accepted_input_ids={"task-id": "input-id"},
        task="detection",
        prompt_type="structured",
    )

    result_id = CeleryBatchPredictionDispatcher().dispatch(
        request, queue="cpu", soft_time_limit=123
    )

    assert result_id == "celery-id"
    assert captured == {
        "args": ["project-id", "backend-id", []],
        "kwargs": {
            "prompt": "find object",
            "output_mode": "box",
            "batch_id": "batch-id",
            "user_id": "actor-id",
            "params": {"threshold": 0.25},
            "predict_mode": "overwrite",
            "model_id": "model-id",
            "task": "detection",
            "task_type": "detection",
            "prompt_type": "structured",
            "model_variants": {"series": "yolov8", "size": "n"},
            "class_filter": [2, 4],
            "pipeline_stages": [{"stage": 3, "parent_stage": None}],
            "execution_unit": "frame",
            "execution_scope": "workbench",
            "async_job_id": "durable-job-id",
            "frozen_model_ref": {"model_version_id": "version-id"},
            "accepted_inputs_protocol": "aap-assets-inputs/v1",
            "accepted_input_ids": {"task-id": "input-id"},
        },
        "options": {
            "queue": "cpu",
            "task_id": "durable-job-id",
            "soft_time_limit": 123,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_unit", ["task", "frame"])
async def test_application_command_disposes_engine_when_workflow_exits(
    monkeypatch,
    execution_unit,
):
    import sqlalchemy.ext.asyncio as sa_async

    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append(True)

    class FailingSessionFactory:
        def __call__(self):
            raise RuntimeError("simulated early workflow exit")

    async def fail_after_command_starts(*_args, **_kwargs):
        raise RuntimeError("simulated early workflow exit")

    monkeypatch.setattr(sa_async, "create_async_engine", lambda *_a, **_k: FakeEngine())
    monkeypatch.setattr(
        sa_async, "async_sessionmaker", lambda *_a, **_k: FailingSessionFactory()
    )
    monkeypatch.setattr(
        preannotation_execution, "_execute_preannotation", fail_after_command_starts
    )

    expected = (
        pytest.raises(ValueError, match="requires a retained video manifest")
        if execution_unit == "frame"
        else pytest.raises(RuntimeError, match="simulated early workflow exit")
    )
    with expected:
        await preannotation_execution.run_preannotation(
            BatchPredictionRequest(
                project_id="p", ml_backend_id="b", execution_unit=execution_unit
            ),
            execution_id="celery-id",
        )

    assert disposed == [True]


def test_pending_input_deferral_retries_after_default_retry_budget(monkeypatch):
    from app.application.preannotation_execution import PreannotationInputDeferred
    from app.workers import tasks
    from app.workers.tasks import batch_predict

    assert batch_predict.max_retries is None
    captured = {}

    class RetryScheduled(Exception):
        pass

    def defer(coroutine):
        coroutine.close()
        raise PreannotationInputDeferred("input_not_ready")

    def fake_retry(self, *, exc, countdown, max_retries):
        captured.update(
            retries=self.request.retries,
            exception=exc,
            countdown=countdown,
            max_retries=max_retries,
        )
        raise RetryScheduled()

    monkeypatch.setattr(tasks.asyncio, "run", defer)
    monkeypatch.setattr(batch_predict, "retry", fake_retry.__get__(batch_predict))
    batch_predict.push_request(id="durable-job-id", retries=8)
    try:
        with pytest.raises(RetryScheduled):
            batch_predict.run(
                "project-id",
                "backend-id",
                task_ids=["task-id"],
                execution_scope="workbench",
                async_job_id="durable-job-id",
            )
    finally:
        batch_predict.pop_request()
    assert captured["retries"] == 8
    assert isinstance(captured["exception"], PreannotationInputDeferred)
    assert captured["countdown"] == 30
    assert captured["max_retries"] is None


def test_worker_builds_manifest_request_from_celery_arguments(monkeypatch):
    import asyncio

    from app.workers import tasks
    from app.workers.tasks import batch_predict

    captured = {}
    original_run = asyncio.run

    async def fake_run_preannotation(request, **_kwargs):
        captured["request"] = request

    monkeypatch.setattr(tasks, "run_preannotation", fake_run_preannotation)
    monkeypatch.setattr(tasks.asyncio, "run", original_run)
    batch_predict.push_request(id="manifest-job-id")
    try:
        batch_predict.run(
            "project-id",
            "backend-id",
            task_ids=None,
            batch_id="batch-id",
            user_id="actor-id",
            execution_scope="bulk",
            async_job_id="manifest-job-id",
            accepted_input_manifest_protocol="aap-assets-input-manifest/v1",
            accepted_input_manifest_id="manifest-job-id",
            accepted_input_manifest_digest="b" * 64,
        )
    finally:
        batch_predict.pop_request()

    request = captured["request"]
    assert request.accepted_input_manifest_protocol == "aap-assets-input-manifest/v1"
    assert request.accepted_input_manifest_id == "manifest-job-id"
    assert request.accepted_input_manifest_digest == "b" * 64


def test_celery_dispatch_preserves_video_manifest_identity(monkeypatch):
    from app.workers.tasks import batch_predict

    captured = {}

    def fake_apply_async(*, args, kwargs, **options):
        captured.update(args=args, kwargs=kwargs, options=options)
        return SimpleNamespace(id="video-manifest-job-id")

    monkeypatch.setattr(batch_predict, "apply_async", fake_apply_async)
    selection = {
        "batch_id": None,
        "task_ids": ["task-id"],
        "predict_mode": "overwrite",
        "execution_scope": "workbench",
        "sampling": {
            "max_frames": 24,
            "step": 1,
            "width": 1280,
            "format": "jpeg",
        },
    }
    request = BatchPredictionRequest(
        project_id="project-id",
        ml_backend_id="backend-id",
        task_ids=["task-id"],
        execution_unit="frame",
        execution_scope="workbench",
        async_job_id="video-manifest-job-id",
        accepted_video_inputs_protocol="aap-assets-video-inputs/v1",
        accepted_video_manifest_id="video-manifest-job-id",
        accepted_video_selection_digest="c" * 64,
        accepted_video_selection=selection,
    )

    CeleryBatchPredictionDispatcher().dispatch(request, queue="ml")

    assert captured["args"] == ["project-id", "backend-id", ["task-id"]]
    assert captured["kwargs"]["accepted_video_inputs_protocol"] == (
        "aap-assets-video-inputs/v1"
    )
    assert captured["kwargs"]["accepted_video_manifest_id"] == ("video-manifest-job-id")
    assert captured["kwargs"]["accepted_video_selection_digest"] == "c" * 64
    assert captured["kwargs"]["accepted_video_selection"] == selection
    assert captured["options"]["task_id"] == "video-manifest-job-id"


def test_worker_builds_video_manifest_request_from_celery_arguments(monkeypatch):
    import asyncio

    from app.workers import tasks
    from app.workers.tasks import batch_predict

    captured = {}
    original_run = asyncio.run

    async def fake_run_preannotation(request, **_kwargs):
        captured["request"] = request

    monkeypatch.setattr(tasks, "run_preannotation", fake_run_preannotation)
    monkeypatch.setattr(tasks.asyncio, "run", original_run)
    selection = {"sampling": {"max_frames": 9}}
    batch_predict.push_request(id="video-manifest-job-id")
    try:
        batch_predict.run(
            "project-id",
            "backend-id",
            task_ids=None,
            user_id="actor-id",
            execution_unit="frame",
            execution_scope="bulk",
            async_job_id="video-manifest-job-id",
            accepted_video_inputs_protocol="aap-assets-video-inputs/v1",
            accepted_video_manifest_id="video-manifest-job-id",
            accepted_video_selection_digest="d" * 64,
            accepted_video_selection=selection,
        )
    finally:
        batch_predict.pop_request()

    request = captured["request"]
    assert request.accepted_video_inputs_protocol == "aap-assets-video-inputs/v1"
    assert request.accepted_video_manifest_id == "video-manifest-job-id"
    assert request.accepted_video_selection_digest == "d" * 64
    assert request.accepted_video_selection == selection


def test_celery_dispatch_omits_unset_durable_options(monkeypatch):
    from app.workers.tasks import batch_predict

    captured = {}

    def fake_apply_async(*, args, kwargs, **options):
        captured.update(args=args, kwargs=kwargs, options=options)
        return SimpleNamespace(id="celery-id")

    monkeypatch.setattr(batch_predict, "apply_async", fake_apply_async)
    request = BatchPredictionRequest(
        project_id="project-id", ml_backend_id="backend-id"
    )

    CeleryBatchPredictionDispatcher().dispatch(request, queue="ml")

    assert captured["args"] == ["project-id", "backend-id", None]
    assert "async_job_id" not in captured["kwargs"]
    assert "frozen_model_ref" not in captured["kwargs"]
    assert "accepted_inputs_protocol" not in captured["kwargs"]
    assert "accepted_input_ids" not in captured["kwargs"]
    assert "accepted_input_manifest_protocol" not in captured["kwargs"]
    assert "accepted_input_manifest_id" not in captured["kwargs"]
    assert "accepted_input_manifest_digest" not in captured["kwargs"]
    assert "accepted_video_inputs_protocol" not in captured["kwargs"]
    assert "accepted_video_manifest_id" not in captured["kwargs"]
    assert "accepted_video_selection_digest" not in captured["kwargs"]
    assert "accepted_video_selection" not in captured["kwargs"]
    assert "task_type" not in captured["kwargs"]
    assert captured["options"] == {"queue": "ml"}


def test_celery_dispatch_preserves_manifest_identity_without_member_payload(
    monkeypatch,
):
    from app.workers.tasks import batch_predict

    captured = {}

    def fake_apply_async(*, args, kwargs, **options):
        captured.update(args=args, kwargs=kwargs, options=options)
        return SimpleNamespace(id="manifest-job-id")

    monkeypatch.setattr(batch_predict, "apply_async", fake_apply_async)
    request = BatchPredictionRequest(
        project_id="project-id",
        ml_backend_id="backend-id",
        task_ids=None,
        batch_id="batch-id",
        user_id="actor-id",
        predict_mode="overwrite",
        execution_scope="bulk",
        async_job_id="manifest-job-id",
        accepted_input_manifest_protocol="aap-assets-input-manifest/v1",
        accepted_input_manifest_id="manifest-job-id",
        accepted_input_manifest_digest="a" * 64,
    )

    CeleryBatchPredictionDispatcher().dispatch(request, queue="ml")

    assert captured["kwargs"]["accepted_input_manifest_protocol"] == (
        "aap-assets-input-manifest/v1"
    )
    assert captured["kwargs"]["accepted_input_manifest_id"] == "manifest-job-id"
    assert captured["kwargs"]["accepted_input_manifest_digest"] == "a" * 64
    assert "accepted_input_ids" not in captured["kwargs"]
    assert captured["args"] == ["project-id", "backend-id", None]
    assert captured["options"]["task_id"] == "manifest-job-id"


@pytest.mark.asyncio
async def test_frame_dispatch_uses_sealed_ordinals_and_returns_receipt(
    monkeypatch,
):
    import celery

    from app.application.preannotation_contract import (
        FramePreannotationDispatchRequest,
        FramePreannotationDispatchReceipt,
        VideoFrameTaskPlan,
    )
    from app.workers import frame_preannotate

    captured = {"segments": []}

    class FakeTask:
        def __init__(self, name):
            self.name = name

        def s(self, *args, **kwargs):
            signature = (self.name, args, kwargs)
            if self.name == "predict":
                captured["segments"].append(signature)
            return signature

    monkeypatch.setattr(frame_preannotate, "predict_video_segment", FakeTask("predict"))
    monkeypatch.setattr(frame_preannotate, "finalize_frame_job", FakeTask("finalize"))
    monkeypatch.setattr(frame_preannotate.settings, "frame_preannotate_chunk_size", 2)
    monkeypatch.setattr(
        frame_preannotate,
        "_init_progress",
        lambda job_id, total: captured.update(progress=(job_id, total)),
    )

    def fake_group(signatures):
        return tuple(signatures)

    def fake_chord(signatures):
        def submit(callback):
            captured["chord"] = (signatures, callback)

        return submit

    monkeypatch.setattr(celery, "group", fake_group)
    monkeypatch.setattr(celery, "chord", fake_chord)

    receipt = await frame_preannotate.dispatch_frame_preannotate(
        FramePreannotationDispatchRequest(
            project_id="project-id",
            ml_backend_id="backend-id",
            video_task_plans=(
                VideoFrameTaskPlan(
                    task_id="video-id",
                    input_id="input-id",
                    input_job_id="origin-job-id",
                    plan_digest="a" * 64,
                    selected_count=4,
                ),
            ),
            stage0={"stage": 7},
            job_id="00000000-0000-0000-0000-000000000001",
            root_execution_id="root-id",
            actor_id="00000000-0000-0000-0000-000000000002",
            accepted_video_manifest_id="00000000-0000-0000-0000-000000000001",
            accepted_video_selection_digest="b" * 64,
            accepted_video_sealed_digest="c" * 64,
        )
    )

    assert receipt == FramePreannotationDispatchReceipt(
        dispatched=True, segments=2, total_frames=4
    )
    assert captured["progress"] == ("00000000-0000-0000-0000-000000000001", 4)
    signatures, callback = captured["chord"]
    assert len(signatures) == 2
    assert signatures == tuple(captured["segments"])
    assert [call[1][0]["frame_ordinals"] for call in captured["segments"]] == [
        [1, 2],
        [3, 4],
    ]
    assert all(
        call[1][0]["input_job_id"] == "origin-job-id" for call in captured["segments"]
    )
    assert callback[0] == "finalize"
    assert callback[2] == {
        "project_id": "project-id",
        "job_id": "00000000-0000-0000-0000-000000000001",
    }
