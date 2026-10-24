"""Exact video message scope and independently durable input allocation."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import export_media_bundles as bundles, export_storage
from app.services.content_snapshot import ContentError
from app.services.exporting import video_inputs, video_jobs as jobs, video_media_paths


def world(*, selected=False):
    project_id, job_id, bundle_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_ids = [uuid.uuid4()] if selected else None
    opts = {"include_attributes": True, "axis_frame": "iso"}
    selected_ids = sorted(str(value) for value in task_ids) if task_ids else None
    snapshot = {
        "schema": 4,
        "project_id": str(project_id),
        "batch_id": None,
        "task_ids": selected_ids,
        "targets": ["video_json"],
        "options": opts,
    }
    receipt = SimpleNamespace(
        id=bundle_id,
        job_id=job_id,
        project_id=project_id,
        input_snapshot=snapshot,
        input_digest="a" * 64,
        storage_ref={"bucket": "saved-exports"},
        zip_output_id=uuid.uuid4(),
    )
    payload = {"targets": ["video_json"], "batch_id": None, **opts}
    if task_ids:
        payload.update(task_ids=selected_ids, scope={"task_ids": selected_ids})
    job = SimpleNamespace(
        id=job_id,
        kind="export",
        project_id=project_id,
        status="pending",
        payload=payload,
    )
    args = dict(
        project_id=project_id,
        batch_id=None,
        targets=["video_json"],
        opts=opts,
        task_ids=task_ids,
    )
    return job, receipt, args


def pin(job, receipt):
    job.payload["video_media"] = {
        "schema": 4,
        "bundle_id": str(receipt.id),
        "input_digest": receipt.input_digest,
    }


@pytest.mark.parametrize("selected", [False, True])
def test_matching_historical_delivery_can_be_failed_without_recapturing(selected):
    job, _, args = world(selected=selected)
    assert jobs.validate_missing_message(job, **args) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "task_ids",
        "batch",
        "targets",
        "options",
        "project",
        "proof_schema",
        "bundle",
        "digest",
        "scope",
        "bool_int",
    ],
)
def test_changed_delivery_never_replaces_original_inputs(mutation):
    job, receipt, args = world(selected=True)
    pin(job, receipt)
    if mutation == "task_ids":
        args["task_ids"] = [uuid.uuid4()]
    elif mutation == "batch":
        args["batch_id"] = uuid.uuid4()
    elif mutation == "targets":
        args["targets"] = ["aap_json"]
    elif mutation == "options":
        args["opts"] = {**args["opts"], "axis_frame": "source"}
    elif mutation == "project":
        args["project_id"] = uuid.uuid4()
    elif mutation == "proof_schema":
        job.payload["video_media"]["schema"] = True
    elif mutation == "bundle":
        job.payload["video_media"]["bundle_id"] = str(uuid.uuid4())
    elif mutation == "digest":
        job.payload["video_media"]["input_digest"] = "b" * 64
    elif mutation == "scope":
        job.payload["scope"]["task_ids"] = [str(uuid.uuid4())]
    else:
        args["opts"] = {**args["opts"], "include_attributes": 1}
    with pytest.raises(ContentError):
        jobs.validate_message(job, receipt, **args)


async def test_capture_flushes_once_and_repeat_preserves_original_proof(monkeypatch):
    job, receipt, args = world(selected=True)
    original = deepcopy(job.payload)
    db = SimpleNamespace(flush=AsyncMock(), get=AsyncMock(return_value=receipt))
    project = SimpleNamespace(id=job.project_id, data_type="video")
    collected = SimpleNamespace(
        snapshot=receipt.input_snapshot, digest=receipt.input_digest
    )
    collect, allocate = (
        AsyncMock(return_value=collected),
        AsyncMock(return_value=receipt),
    )
    monkeypatch.setattr(video_inputs, "collect", collect)
    monkeypatch.setattr(
        video_media_paths, "logical_sources", lambda _: {"video_json": {}}
    )
    monkeypatch.setattr(bundles, "allocate", allocate)
    monkeypatch.setattr(bundles, "snapshot", lambda row: row)
    monkeypatch.setattr(
        export_storage, "allocate", AsyncMock(return_value=receipt.storage_ref)
    )
    capture_args = {key: value for key, value in args.items() if key != "project_id"}
    assert await jobs.capture_job(db, job, project, **capture_args) is receipt
    saved = deepcopy(job.payload)
    assert saved == {
        **original,
        "video_media": {
            "schema": 4,
            "bundle_id": str(receipt.id),
            "input_digest": receipt.input_digest,
        },
    }
    assert await jobs.capture_job(db, job, project, **capture_args) is receipt
    collect.assert_awaited_once()
    allocate.assert_awaited_once()
    assert job.payload == saved
    db.flush.assert_awaited_once()
    jobs.validate_message(job, receipt, **args)


async def test_changed_repeat_is_rejected_before_collect_or_allocation(monkeypatch):
    job, receipt, args = world()
    pin(job, receipt)
    args["opts"] = {**args["opts"], "axis_frame": "source"}
    db = SimpleNamespace(get=AsyncMock(return_value=receipt))
    collect = AsyncMock()
    monkeypatch.setattr(video_inputs, "collect", collect)
    with pytest.raises(ContentError):
        await jobs.capture_job(
            db,
            job,
            SimpleNamespace(id=job.project_id, data_type="video"),
            **{key: value for key, value in args.items() if key != "project_id"},
        )
    collect.assert_not_awaited()


async def test_zip_location_mismatch_does_not_publish_proof(monkeypatch):
    job, receipt, args = world()
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(
        video_inputs,
        "collect",
        AsyncMock(
            return_value=SimpleNamespace(snapshot={}, digest=receipt.input_digest)
        ),
    )
    monkeypatch.setattr(
        video_media_paths, "logical_sources", lambda _: {"video_json": {}}
    )
    monkeypatch.setattr(bundles, "allocate", AsyncMock(return_value=receipt))
    monkeypatch.setattr(
        export_storage,
        "allocate",
        AsyncMock(return_value={"bucket": "current-wrong-bucket"}),
    )
    with pytest.raises(ContentError, match="storage_binding_changed"):
        await jobs.capture_job(
            db,
            job,
            SimpleNamespace(id=job.project_id, data_type="video"),
            **{key: value for key, value in args.items() if key != "project_id"},
        )
    assert "video_media" not in job.payload
    db.flush.assert_not_awaited()


@pytest.mark.parametrize("other", ["image_media", "lidar_media"])
def test_other_protocol_proof_cannot_be_reinterpreted_as_video(other):
    job, receipt, args = world()
    pin(job, receipt)
    job.payload[other] = dict(job.payload["video_media"])
    with pytest.raises(ContentError):
        jobs.validate_message(job, receipt, **args)


def test_frozen_temporal_scope_cannot_change_between_message_and_bundle():
    job, receipt, args = world()
    temporal = {
        "task_id": str(uuid.uuid4()),
        "dataset_item_id": None,
        "selection_kind": "frames",
        "from_frame": 3,
        "to_frame": 9,
    }
    args["opts"] = {**args["opts"], "video_export_scope": temporal}
    job.payload.update(deepcopy(args["opts"]))
    receipt.input_snapshot["options"] = deepcopy(args["opts"])
    pin(job, receipt)
    jobs.validate_message(job, receipt, **args)
    args["opts"]["video_export_scope"]["to_frame"] = 10
    with pytest.raises(ContentError, match="options_changed"):
        jobs.validate_message(job, receipt, **args)


def test_public_job_dto_hides_video_proof_but_preserves_request_scope():
    from datetime import datetime, timezone
    from app.schemas.async_job import AsyncJobOut

    job, receipt, _ = world()
    pin(job, receipt)
    now = datetime.now(timezone.utc)
    public = AsyncJobOut.model_validate(
        {
            "id": job.id,
            "kind": "export",
            "project_id": job.project_id,
            "user_id": None,
            "status": "pending",
            "progress_pct": 0,
            "payload": job.payload,
            "result": {},
            "error_message": None,
            "celery_task_id": None,
            "started_at": None,
            "completed_at": None,
            "created_at": now,
            "updated_at": now,
        }
    ).model_dump(mode="json")
    assert "video_media" not in public["payload"]
    assert public["payload"]["targets"] == ["video_json"]
    assert "video_media" in job.payload
