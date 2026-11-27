from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kombu import Connection

from app import deps
from app.workers.celery_app import celery_app


@pytest.fixture
def media_queue(monkeypatch):
    send_task = celery_app.send_task
    with (
        Connection("memory://") as broker,
        broker.SimpleQueue("media", no_ack=True) as queue,
    ):

        def send_without_result_backend(*args, **options):
            options.update(
                ignore_result=True,
                result_cls=lambda task_id: SimpleNamespace(id=task_id),
            )
            return send_task(*args, connection=broker, **options)

        monkeypatch.setattr(celery_app, "send_task", send_without_result_backend)
        yield queue
        queue.clear()


def _messages(queue):
    messages = []
    while queue.qsize():
        message = queue.get_nowait()
        args, kwargs, _ = message.payload
        messages.append((message.headers["task"], message.headers["id"], args, kwargs))
    return messages


def test_quality_dispatch_rejects_unsupported_run_kind_before_publish(monkeypatch):
    send = Mock()
    monkeypatch.setattr(celery_app, "send_task", send)
    with pytest.raises(ValueError, match="Unsupported quality run kind"):
        deps.get_quality_job_dispatcher().dispatch_run(
            "unknown", uuid.uuid4(), execution_id="run-id"
        )
    send.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "task_name"),
    [
        ("mask_qc", "app.workers.mask_qc.run_mask_qc"),
        ("point_cloud_quality", "app.workers.point_cloud_quality.run"),
        ("video_track_quality", "app.workers.video_track_quality.run"),
    ],
)
def test_quality_run_keeps_original_task_shape(media_queue, kind, task_name):
    run_id = uuid.uuid4()
    execution_id = str(uuid.uuid4())
    result = deps.get_quality_job_dispatcher().dispatch_run(
        kind, run_id, execution_id=execution_id
    )
    assert result is None
    assert _messages(media_queue) == [(task_name, execution_id, [str(run_id)], {})]


def test_repair_rollback_and_import_keep_same_media_route_and_id(media_queue):
    batch_id = uuid.uuid4()
    dispatcher = deps.get_quality_job_dispatcher()
    assert (
        dispatcher.dispatch_mask_repair(batch_id, rollback=False, execution_id="repair")
        == "repair"
    )
    assert (
        dispatcher.dispatch_mask_repair(
            batch_id, rollback=True, execution_id="rollback"
        )
        == "rollback"
    )
    assert dispatcher.dispatch_mask_import(batch_id, execution_id="import") == "import"
    args = [str(batch_id)]
    assert _messages(media_queue) == [
        ("app.workers.mask_repair.run_mask_repair", "repair", args, {}),
        ("app.workers.mask_repair.rollback_mask_repair", "rollback", args, {}),
        ("app.workers.mask_format_import.run_mask_format_import", "import", args, {}),
    ]


def test_quality_broker_failure_does_not_resend(monkeypatch):
    send = Mock(side_effect=OSError("broker unavailable"))
    monkeypatch.setattr(celery_app, "send_task", send)
    with pytest.raises(OSError, match="broker unavailable"):
        deps.get_quality_job_dispatcher().dispatch_run(
            "mask_qc", uuid.uuid4(), execution_id="persisted-run"
        )
    send.assert_called_once()
