from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app import deps
from app.workers import video_tracker_dispatch


def test_tracker_uses_current_broker_and_existing_gpu_wire(monkeypatch):
    messages = []

    def send(task, **message):
        messages.append((task, message))
        return SimpleNamespace(id=message["task_id"])

    monkeypatch.setattr(
        video_tracker_dispatch, "current_app", SimpleNamespace(send_task=send)
    )
    job_id, execution_id = uuid.uuid4(), str(uuid.uuid4())
    accepted = deps.get_video_tracker_job_dispatcher().dispatch_tracker_job(
        job_id, execution_id=execution_id
    )
    assert accepted == execution_id
    assert messages == [
        (
            "app.workers.video_tracker.run_video_tracker_job",
            {"args": [str(job_id)], "task_id": execution_id, "queue": "gpu"},
        )
    ]


def test_tracker_broker_failure_does_not_resend(monkeypatch):
    messages = []

    def send(task, **message):
        messages.append((task, message))
        raise OSError("broker unavailable")

    monkeypatch.setattr(
        video_tracker_dispatch, "current_app", SimpleNamespace(send_task=send)
    )
    with pytest.raises(OSError, match="broker unavailable"):
        deps.get_video_tracker_job_dispatcher().dispatch_tracker_job(
            uuid.uuid4(), execution_id=str(uuid.uuid4())
        )
    assert len(messages) == 1
