import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
import pytest

from aap_assets_video_inputs import authority as c
from test_video_input_values import video_input


def selection():
    video = video_input()
    return c.VideoSelection(
        batch_id=None,
        task_ids=[video.task.task_id],
        predict_mode="overwrite",
        execution_scope="bulk",
        sampling={"max_frames": 900},
    )


def job_request(**changes):
    video = video_input()
    return c.VideoJobRequest(
        **{
            "job_id": video.job_id,
            "task_id": video.task.task_id,
            "actor_id": video.actor_id,
            "project_id": video.task.project_id,
            "selection": selection(),
            **changes,
        }
    )


def job_wire(request):
    return {
        "schema": c.JOB_SCHEMA,
        "ok": True,
        "data": {
            "job": {
                "job_id": str(request.job_id),
                "task_id": str(request.task_id),
                "actor_id": str(request.actor_id),
                "project_id": str(request.project_id),
                "retained_project_id": str(request.project_id),
                "selection": request.selection.model_dump(mode="json"),
                "status": "pending",
                "cancel_requested": False,
                "active": True,
            }
        },
    }


def source_wire():
    video = video_input()
    return {
        "schema": c.TASK_SCHEMA,
        "ok": True,
        "data": {
            "actor_id": str(video.actor_id),
            "binding": video.task.model_dump(mode="json"),
            "upload_ready": True,
        },
    }


def test_selection_canonicalizes_requests_and_distinguishes_workbench():
    task = video_input().task.task_id
    other = uuid4()
    raw = selection().model_dump()
    raw["task_ids"] = [task, other, task]
    assert c.VideoSelection(**raw).task_ids == tuple(sorted([task, other], key=str))
    raw["execution_scope"] = "workbench"
    with pytest.raises(ValidationError):
        c.VideoSelection(**raw)
    raw["task_ids"] = None
    with pytest.raises(ValidationError):
        c.VideoSelection(**raw)


@pytest.mark.parametrize("fault", [None, "actor", "task", "extra", "schema"])
def test_task_read_is_scoped(fault):
    video = video_input()
    request = c.ReadVideoTaskRequest(
        task_id=video.task.task_id, actor_id=video.actor_id
    )
    wire = source_wire()
    if fault == "actor":
        wire["data"]["actor_id"] = str(uuid4())
    elif fault == "task":
        wire["data"]["binding"]["task_id"] = str(uuid4())
    elif fault == "extra":
        wire["debug"] = "unexpected"
    elif fault == "schema":
        wire["schema"] = c.JOB_SCHEMA
    if fault is None:
        assert (
            c.parse_native("read_video_input_task_source", request, wire).binding
            == video.task
        )
    else:
        with pytest.raises(c.AuthorityFailure, match="invalid_response"):
            c.parse_native("read_video_input_task_source", request, wire)


@pytest.mark.parametrize(
    "changed",
    [
        None,
        "task_version",
        "task_source_generation",
        "dataset_item_id",
        "file_path",
        "task_source_snapshot",
        "upload_ready",
    ],
)
def test_source_lock_fences_source_but_allows_workflow_version_progress(changed):
    video = video_input()
    request = c.LockVideoTaskRequest(
        task_id=video.task.task_id, actor_id=video.actor_id, expected_binding=video.task
    )
    wire = source_wire()
    if changed == "task_version":
        wire["data"]["binding"][changed] += 1
    elif changed == "task_source_generation":
        wire["data"]["binding"][changed] += 1
    elif changed == "dataset_item_id":
        wire["data"]["binding"][changed] = str(uuid4())
    elif changed == "file_path":
        wire["data"]["binding"][changed] = "replacement.mp4"
    elif changed == "task_source_snapshot":
        wire["data"]["binding"][changed] = "f" * 64
    elif changed == "upload_ready":
        wire["data"][changed] = False
    if changed in {None, "task_version"}:
        assert c.parse_native(
            "lock_video_input_task_source", request, wire
        ).upload_ready
    else:
        with pytest.raises(c.AuthorityFailure, match="invalid_response"):
            c.parse_native("lock_video_input_task_source", request, wire)


@pytest.mark.parametrize(
    "fault",
    [
        "actor",
        "project",
        "retained_project",
        "cancelled",
        "active",
        "sampling",
        "duplicate_scope",
    ],
)
def test_active_job_rejects_crossed_or_noncanonical_scope(fault):
    request = job_request()
    wire = job_wire(request)
    job = wire["data"]["job"]
    if fault == "actor":
        job["actor_id"] = str(uuid4())
    elif fault == "project":
        job["project_id"] = str(uuid4())
    elif fault == "retained_project":
        job["retained_project_id"] = str(uuid4())
    elif fault == "cancelled":
        job["cancel_requested"] = True
    elif fault == "active":
        job["active"] = False
    elif fault == "sampling":
        job["selection"]["sampling"]["step"] = 2
    else:
        job["selection"]["task_ids"] *= 2
    with pytest.raises(c.AuthorityFailure, match="invalid_response"):
        c.parse_native("project_video_input_job", request, wire)


@pytest.mark.parametrize("missing", [False, True])
def test_history_cannot_grant_execution_after_fk_nulling_or_job_pruning(missing):
    request = job_request(require_active=False)
    wire = job_wire(request)
    if missing:
        wire["data"]["job"] = None
    else:
        wire["data"]["job"].update(actor_id=None, project_id=None, active=False)
    c.parse_native("project_video_input_job", request, wire)
    active = job_request()
    with pytest.raises(c.AuthorityFailure, match="invalid_response"):
        c.parse_native("project_video_input_job", active, wire)


def test_missing_job_is_distinct_from_scope_rejection():
    request = job_request(require_active=False)
    wire = {
        "schema": c.JOB_SCHEMA,
        "ok": False,
        "error": {"code": "manifest_scope_mismatch"},
    }
    with pytest.raises(c.AuthorityFailure, match="manifest_scope_mismatch"):
        c.parse_native("project_video_input_job", request, wire)
    wire["error"]["code"] = "unpublished_error"
    with pytest.raises(c.AuthorityFailure, match="invalid_response"):
        c.parse_native("project_video_input_job", request, wire)


def test_authority_schema_and_native_metadata_are_published():
    assert (
        json.loads(Path(c.__file__).with_name("authority.schema.json").read_text())
        == c.contract_document()
    )
    for name, op in c.OPERATIONS.items():
        assert len(op.arguments) == len(op.sql_types)
        assert set(op.arguments) == set(op.request.model_fields)
        assert op.consumer == "assets_owner"
        assert name not in {
            "reserve_accepted_image_input",
            "capture_accepted_image_input_manifest",
        }
