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
async def test_application_command_disposes_engine_when_workflow_exits(
    monkeypatch,
):
    import sqlalchemy.ext.asyncio as sa_async

    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append(True)

    async def fail_after_command_starts(*_args, **_kwargs):
        raise RuntimeError("simulated early workflow exit")

    monkeypatch.setattr(sa_async, "create_async_engine", lambda *_a, **_k: FakeEngine())
    monkeypatch.setattr(sa_async, "async_sessionmaker", lambda *_a, **_k: object())
    monkeypatch.setattr(
        preannotation_execution, "_execute_preannotation", fail_after_command_starts
    )

    with pytest.raises(RuntimeError, match="simulated early workflow exit"):
        await preannotation_execution.run_preannotation(
            BatchPredictionRequest(project_id="p", ml_backend_id="b"),
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
    assert "task_type" not in captured["kwargs"]
    assert captured["options"] == {"queue": "ml"}


@pytest.mark.asyncio
async def test_frame_dispatch_uses_only_supplied_counts_and_returns_receipt(
    monkeypatch,
):
    import celery

    from app.application.preannotation_contract import (
        FramePreannotationDispatchRequest,
        FramePreannotationDispatchReceipt,
        TaskFrameCount,
    )
    from app.workers import frame_preannotate

    captured = {"extractions": []}

    class FakeTask:
        def __init__(self, name):
            self.name = name

        def s(self, *args, **kwargs):
            signature = (self.name, args, kwargs)
            if self.name == "extract":
                captured["extractions"].append(signature)
            return signature

    monkeypatch.setattr(frame_preannotate, "extract_frames_task", FakeTask("extract"))
    monkeypatch.setattr(frame_preannotate, "launch_predict_phase", FakeTask("predict"))
    monkeypatch.setattr(frame_preannotate.settings, "frame_preannotate_max_frames", 4)
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
            task_frame_counts=(
                TaskFrameCount(task_id="video-id", frame_count=9),
                TaskFrameCount(task_id="no-frames", frame_count=0),
            ),
            stage0={"stage": 7},
            job_id="job-id",
            root_execution_id="root-id",
        )
    )

    assert receipt == FramePreannotationDispatchReceipt(
        dispatched=True, segments=2, total_frames=4
    )
    assert captured["progress"] == ("job-id", 4)
    assert captured["extractions"] == [
        ("extract", ("video-id", [0, 1, 2, 3]), {"job_id": "job-id"})
    ]
    signatures, callback = captured["chord"]
    assert len(signatures) == 1
    assert callback == (
        "predict",
        (),
        {
            "project_id": "project-id",
            "ml_backend_id": "backend-id",
            "stage0": {"stage": 7},
            "job_id": "job-id",
            "segments": [
                {"task_id": "video-id", "frame_indices": [0, 1]},
                {"task_id": "video-id", "frame_indices": [2, 3]},
            ],
            "celery_root_task_id": "root-id",
        },
    )
