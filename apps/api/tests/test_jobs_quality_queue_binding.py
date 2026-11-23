from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app import deps
from app.workers import mask_format_import, mask_qc, mask_repair, point_cloud_quality
from app.workers import video_track_quality


def test_quality_dispatch_rejects_unsupported_run_kind_before_publish():
    with pytest.raises(ValueError, match="Unsupported quality run kind"):
        deps.get_quality_job_dispatcher().dispatch_run(
            "unknown", uuid.uuid4(), execution_id="run-id"
        )


@pytest.mark.parametrize(
    ("kind", "task"),
    [
        ("mask_qc", mask_qc.run_mask_qc),
        ("point_cloud_quality", point_cloud_quality.run),
        ("video_track_quality", video_track_quality.run),
    ],
)
def test_quality_run_keeps_original_task_shape(monkeypatch, kind, task):
    calls = []
    monkeypatch.setattr(task, "apply_async", lambda **message: calls.append(message))
    run_id = uuid.uuid4()
    deps.get_quality_job_dispatcher().dispatch_run(
        kind, run_id, execution_id="persisted-run"
    )
    assert calls == [{"args": [str(run_id)], "task_id": "persisted-run"}]


def test_repair_rollback_and_import_keep_same_media_route_and_id(monkeypatch):
    calls = []

    def install(task):
        def send(**message):
            calls.append((task.name, message))
            return SimpleNamespace(id=message["task_id"])

        monkeypatch.setattr(task, "apply_async", send)

    install(mask_repair.run_mask_repair)
    install(mask_repair.rollback_mask_repair)
    install(mask_format_import.run_mask_format_import)
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
    assert calls == [
        (
            mask_repair.run_mask_repair.name,
            {"args": args, "queue": "media", "task_id": "repair"},
        ),
        (
            mask_repair.rollback_mask_repair.name,
            {"args": args, "queue": "media", "task_id": "rollback"},
        ),
        (
            mask_format_import.run_mask_format_import.name,
            {"args": args, "task_id": "import"},
        ),
    ]


def test_quality_broker_failure_does_not_resend(monkeypatch):
    count = 0

    def fail(**_message):
        nonlocal count
        count += 1
        raise OSError("broker unavailable")

    monkeypatch.setattr(mask_qc.run_mask_qc, "apply_async", fail)
    with pytest.raises(OSError, match="broker unavailable"):
        deps.get_quality_job_dispatcher().dispatch_run(
            "mask_qc", uuid.uuid4(), execution_id="persisted-run"
        )
    assert count == 1
