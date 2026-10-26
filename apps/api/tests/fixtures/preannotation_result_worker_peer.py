"""Test-only Celery peer for killing a worker after its raw result is sealed."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from celery.signals import task_prerun, worker_ready


def _append_json(name: str, value: dict) -> None:
    path = Path(os.environ[name])
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(value, sort_keys=True) + "\n").encode())
    finally:
        os.close(descriptor)


@worker_ready.connect
def _mark_worker_ready(**_kwargs) -> None:
    Path(os.environ["AAP_TEST_BATCH_RESULT_WORKER_READY_PATH"]).write_text(
        str(os.getpid()), encoding="ascii"
    )


@task_prerun.connect
def _record_batch_delivery(task_id, task, **_kwargs) -> None:
    if getattr(task, "name", None) not in {
        "app.workers.tasks.batch_predict",
        "app.workers.predictions_retry.retry_failed_prediction",
    }:
        return
    request = task.request
    delivery_info = request.delivery_info or {}
    _append_json(
        "AAP_TEST_BATCH_RESULT_DELIVERIES_PATH",
        {
            "task_id": task_id,
            "pid": os.getpid(),
            "retries": request.retries,
            "redelivered": bool(delivery_info.get("redelivered")),
        },
    )


def install() -> None:
    from app.application.preannotation_results import BatchResultExecution
    from aap_inference_contract.execution import ModelTaskResult
    from app.workers.tasks import batch_predict
    from app.workers.predictions_retry import retry_failed_prediction
    from tests.inference_execution_test_ports import pipeline_output

    original_call = BatchResultExecution.call
    original_capture = BatchResultExecution.capture
    original_retry = batch_predict.retry

    async def call_with_short_test_lease(
        self, operation: str, *, leased: bool = True, db=None, **values
    ):
        if operation in {
            "claim_batch_result_execution",
            "heartbeat_batch_result_execution",
        }:
            values["lease_seconds"] = 5
        _append_json(
            "AAP_TEST_BATCH_RESULT_NATIVE_EVENTS_PATH",
            {"pid": os.getpid(), "operation": operation, "event": "start"},
        )
        try:
            result = await original_call(
                self, operation, leased=leased, db=db, **values
            )
        except Exception as exc:
            _append_json(
                "AAP_TEST_BATCH_RESULT_NATIVE_EVENTS_PATH",
                {
                    "pid": os.getpid(),
                    "operation": operation,
                    "event": "error",
                    "code": str(exc),
                },
            )
            raise
        _append_json(
            "AAP_TEST_BATCH_RESULT_NATIVE_EVENTS_PATH",
            {"pid": os.getpid(), "operation": operation, "event": "done"},
        )
        if operation == "claim_batch_result_execution":
            _append_json(
                "AAP_TEST_BATCH_RESULT_CLAIMS_PATH",
                {
                    "pid": os.getpid(),
                    "generation": result.lease.generation,
                    "worker_id": str(result.lease.worker_id),
                },
            )
        return result

    async def renew_with_short_test_interval(self):
        try:
            while True:
                await asyncio.sleep(1)
                result = await self.call(
                    "heartbeat_batch_result_execution", lease_seconds=5
                )
                self.lease = result.lease
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost = "lease_lost"

    async def capture_then_hold(self, ordinal, requests, *, stage, context):
        result = await original_capture(
            self, ordinal, requests, stage=stage, context=context
        )
        if stage == "raw":
            ready_path = Path(os.environ["AAP_TEST_BATCH_RESULT_CAPTURED_PATH"])
            try:
                descriptor = os.open(
                    ready_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                return result
            try:
                marker = {
                    "pid": os.getpid(),
                    "ordinal": ordinal,
                    "state": result.result.state,
                    "raw_complete": result.result.raw_complete,
                    "result_count": result.result.result_count,
                }
                os.write(descriptor, json.dumps(marker, sort_keys=True).encode())
            finally:
                os.close(descriptor)
            while True:
                await asyncio.sleep(0.1)
        return result

    async def fake_model_pipeline(task, stages, *_args, **_kwargs):
        from sqlalchemy import inspect

        task_id = str(inspect(task).identity[0])
        backend_id = uuid.UUID(str(stages[0]["ml_backend_id"]))
        _append_json(
            "AAP_TEST_BATCH_RESULT_MODEL_CALLS_PATH",
            {"pid": os.getpid(), "task_id": task_id},
        )
        return pipeline_output(
            [
                ModelTaskResult(
                    task_id=task_id,
                    result=(),
                    score=0.91,
                    model_version="worker-recovery-fixture",
                    inference_time_ms=7,
                    meta={"total_cost": 0.0042},
                )
            ],
            instance_id=backend_id,
        )

    async def fake_retry_pipeline(task_id, stages, *_args, **_kwargs):
        backend_id = uuid.UUID(str(stages[0]["ml_backend_id"]))
        return pipeline_output(
            [
                ModelTaskResult(
                    task_id=str(task_id),
                    result=(),
                    score=0.91,
                    model_version="worker-recovery-fixture",
                    inference_time_ms=7,
                    meta={"total_cost": 0.0042},
                )
            ],
            instance_id=backend_id,
        )

    original_prediction_retry = retry_failed_prediction.retry

    def prediction_retry_after_lease_window(*args, **kwargs):
        kwargs["countdown"] = 1
        return original_prediction_retry(*args, **kwargs)

    def retry_after_lease_window(*args, **kwargs):
        kwargs["countdown"] = 1
        return original_retry(*args, **kwargs)

    BatchResultExecution.call = call_with_short_test_lease
    BatchResultExecution._renew = renew_with_short_test_interval
    BatchResultExecution.capture = capture_then_hold
    batch_predict.retry = retry_after_lease_window
    retry_failed_prediction.retry = prediction_retry_after_lease_window
    import app.application.durable_prediction_retry as retry
    import app.application.preannotation_execution as execution

    execution._model_execution_port = lambda *_args, **_kwargs: object()
    execution._run_task_pipeline = fake_model_pipeline
    retry._model_execution_port = lambda *_args, **_kwargs: object()
    retry.execute_task_pipeline = fake_retry_pipeline


install()
