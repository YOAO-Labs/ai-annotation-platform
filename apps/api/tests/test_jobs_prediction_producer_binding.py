from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest

from app import deps
from app.application.preannotation_contract import BatchPredictionRequest
from app.services.jobs_queue import dispatch_job
from app.workers import audit, predictions_retry, task_events, tasks


def test_batch_prediction_preserves_stable_id_route_and_time_limit(monkeypatch):
    calls = []

    def send(**message):
        calls.append(message)
        return SimpleNamespace(id=message["task_id"])

    monkeypatch.setattr(tasks.batch_predict, "apply_async", send)
    job_id = str(uuid.uuid4())
    accepted = deps.get_batch_prediction_dispatcher().dispatch(
        BatchPredictionRequest(
            project_id="project-id",
            ml_backend_id="backend-id",
            task_ids=["task-id"],
            async_job_id=job_id,
        ),
        queue="ml.cpu",
        soft_time_limit=90,
    )
    assert accepted == job_id
    assert len(calls) == 1
    message = calls[0]
    assert message["args"] == ["project-id", "backend-id", ["task-id"]]
    assert message["kwargs"]["async_job_id"] == message["task_id"] == job_id
    assert message["queue"] == "ml.cpu"
    assert message["soft_time_limit"] == 90


def test_prediction_retry_keeps_single_and_batch_routes_and_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        predictions_retry.retry_failed_prediction,
        "apply_async",
        lambda **message: calls.append(message),
    )
    failed_id, actor_id, single_job, batch_job = (uuid.uuid4() for _ in range(4))
    dispatcher = deps.get_prediction_retry_dispatcher()
    dispatcher.dispatch_prediction_retry(
        failed_id, actor_id, execution_queue="ml.cpu", job_id=single_job
    )
    dispatcher.dispatch_job_item_retry(failed_id, actor_id, job_id=batch_job)
    args = [str(failed_id), str(actor_id)]
    assert calls == [
        {
            "args": args,
            "kwargs": {"job_id": str(single_job)},
            "task_id": str(single_job),
            "queue": "ml.cpu",
        },
        {"args": args, "kwargs": {"job_id": str(batch_job)}, "task_id": str(batch_job)},
    ]


def test_prediction_broker_failure_does_not_try_another_route(monkeypatch):
    queues = []

    def fail(**message):
        queues.append(message["queue"])
        raise OSError("broker unavailable")

    monkeypatch.setattr(predictions_retry.retry_failed_prediction, "apply_async", fail)
    with pytest.raises(OSError, match="broker unavailable"):
        deps.get_prediction_retry_dispatcher().dispatch_prediction_retry(
            uuid.uuid4(), uuid.uuid4(), execution_queue="ml.cpu", job_id=uuid.uuid4()
        )
    assert queues == ["ml.cpu"]


def test_broker_error_signals_existing_audit_and_event_fallback(monkeypatch):
    from app.api.v1 import me
    from app.middleware import audit as middleware

    def fail(*_args):
        raise OSError("broker unavailable")

    monkeypatch.setattr(audit.persist_audit_entry, "delay", fail)
    monkeypatch.setattr(task_events.persist_task_events_batch, "delay", fail)
    assert not middleware._enqueue_audit({"action": "http.post"})
    assert not me._enqueue_task_events([{"id": str(uuid.uuid4())}])


@pytest.mark.asyncio
async def test_cancelled_first_publish_drains_broker_thread_before_returning(
    monkeypatch,
):
    from app.workers.celery_app import celery_app

    sending, release = threading.Event(), threading.Event()

    def send(*_args, **_kwargs):
        sending.set()
        if not release.wait(5):
            raise TimeoutError("test did not release broker thread")

    monkeypatch.setattr(celery_app, "send_task", send)
    publication = asyncio.create_task(
        dispatch_job(task_name="test.fenced", execution_id="job-id", args=[])
    )
    try:
        assert await asyncio.to_thread(sending.wait, 5)
        publication.cancel()
        await asyncio.sleep(0)
        assert not publication.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await publication
