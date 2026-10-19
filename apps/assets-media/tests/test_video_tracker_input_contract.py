"""Portable contract checks for full-video tracker input retention."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest

from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as t
from aap_assets_video_inputs.execution import VideoCaptureSource, attempt_prefix
from aap_assets_video_inputs.values import (
    FrameSampling,
    ObjectIdentity,
    TaskVideoBinding,
    VideoFacts,
    VideoSourceSnapshot,
)

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
JOB = UUID("20000000-0000-4000-8000-000000000001")
TASK = UUID("30000000-0000-4000-8000-000000000001")
PROJECT = UUID("40000000-0000-4000-8000-000000000001")
ITEM = UUID("50000000-0000-4000-8000-000000000001")
INPUT = UUID("60000000-0000-4000-8000-000000000001")
DIGEST = "a" * 64


def _scope(*, grid_step=2, file_path=""):
    task = TaskVideoBinding(
        task_id=TASK,
        project_id=PROJECT,
        task_version=4,
        task_source_generation=7,
        dataset_item_id=ITEM,
        task_source_snapshot=DIGEST,
        file_path=file_path,
        file_type="video",
    )
    source = VideoCaptureSource(
        dataset_item_id=ITEM,
        dataset_id=UUID("70000000-0000-4000-8000-000000000001"),
        source_generation=7,
        source_snapshot=DIGEST,
        timetable_revision=0,
        object_identity=None,
        source_drifted=False,
        file_path="assets/original.mp4",
        file_type="video",
        file_size=None,
        content_hash=None,
    )
    execution = t.TrackerExecutionSpec(
        job_kind="tracking",
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=5,
        grid_step=grid_step,
        correction_frame=None,
        segment_id=None,
        track_id_snapshot=None,
        target_class_name="person",
        target_tool_unit_id="bbox",
        prompt_snapshot={"text": "person", "seeds": []},
        source_annotations=(),
        correction_mask=None,
    )
    return t.TrackerVideoInputScope(
        tracker_job_id=JOB,
        origin_actor_id=ACTOR,
        task=task,
        source_facts=source,
        execution=execution,
        request_digest=DIGEST,
        prompt_digest="b" * 64,
    )


def _record(status="pending", *, scope=None):
    return t.TrackerVideoInputRecord(
        input_id=INPUT,
        scope=scope or _scope(),
        status=status,
        plan=None,
        error=None,
        created_at=datetime.now(UTC),
        retire_after=None,
    )


def _literal_values(alias):
    values = set()
    for value in get_args(alias):
        nested = get_args(value)
        values.update(_literal_values(value) if nested else {value})
    return values


def test_tracker_artifacts_match_the_native_contract():
    expected = t.contract_document()
    package_schema = Path(t.__file__).with_name("tracker.schema.json")
    migration_schema = (
        Path(__file__).parents[2]
        / "api/alembic/sql/0228_video_tracker_inputs.schema.json"
    )
    assert json.loads(package_schema.read_text()) == expected
    assert json.loads(migration_schema.read_text()) == expected
    assert list(t.TRACKER_OPERATIONS) == [
        "capture_video_tracker_input",
        "read_video_tracker_input",
        "prepare_video_tracker_input",
        "reference_video_tracker_source",
        "claim_video_tracker_input",
        "heartbeat_video_tracker_input",
        "stage_video_tracker_times",
        "finish_video_tracker_input",
        "fail_video_tracker_input",
        "sweep_video_tracker_inputs",
        "lock_video_tracker_input_for_accept",
        "lock_video_tracker_input_for_stage",
        "attach_annotation_video_tracker_input_source",
        "copy_annotation_video_tracker_input_sources",
        "video_tracker_input_referenced",
    ]


def test_capture_accepts_only_a_frozen_positive_grid_step():
    request = t.TrackerVideoInputRequest(
        actor_id=ACTOR, tracker_job_id=JOB, grid_step=3
    )
    assert t.TRACKER_OPERATIONS["capture_video_tracker_input"].arguments == (
        "actor_id",
        "tracker_job_id",
        "grid_step",
    )
    assert request.grid_step == 3
    with pytest.raises(ValueError):
        t.TrackerVideoInputRequest(actor_id=ACTOR, tracker_job_id=JOB, grid_step=0)


def test_materializer_reuses_the_original_video_worker_dtos():
    assert (
        t.TRACKER_OPERATIONS["claim_video_tracker_input"].request
        is e.ClaimVideoInputRequest
    )
    assert (
        t.TRACKER_OPERATIONS["claim_video_tracker_input"].result is e.ClaimedVideoInput
    )
    assert (
        t.TRACKER_OPERATIONS["heartbeat_video_tracker_input"].request
        is e.HeartbeatVideoInputRequest
    )
    assert (
        t.TRACKER_OPERATIONS["stage_video_tracker_times"].request
        is e.StageVideoTimesRequest
    )
    assert (
        t.TRACKER_OPERATIONS["finish_video_tracker_input"].request
        is e.FinishVideoInputRequest
    )
    assert (
        t.TRACKER_OPERATIONS["fail_video_tracker_input"].request
        is e.FailVideoInputRequest
    )


def test_empty_tracker_claim_uses_the_shared_worker_receipt_contract():
    request = e.ClaimVideoInputRequest(worker_id=ACTOR)
    result = t.parse_native(
        "claim_video_tracker_input",
        request,
        {
            "schema": t.TRACKER_SCHEMA,
            "ok": True,
            "data": {"claim": None},
        },
    )
    assert isinstance(result, e.ClaimedVideoInput)
    assert result.claim is None


def test_tracker_claim_scope_checks_the_shared_claim_receipt_without_a_private_scope():
    scope = _scope()
    identity = ObjectIdentity(etag="source-etag", byte_size=128, version_id="source-v1")
    source = scope.source_facts.model_copy(
        update={
            "object_identity": identity,
            "file_size": 128,
            "content_hash": "c" * 64,
        }
    )
    created_at = datetime.now(UTC)
    input_record = e.VideoInputRecord(
        input_id=INPUT,
        job_id=JOB,
        actor_id=ACTOR,
        task=scope.task,
        source=VideoSourceSnapshot(
            dataset_item_id=ITEM,
            dataset_id=source.dataset_id,
            source_generation=source.source_generation,
            source_snapshot=source.source_snapshot,
            timetable_revision=source.timetable_revision,
            object_identity=identity,
        ),
        sampling=FrameSampling(max_frames=2_147_483_647, step=2),
        status="materializing",
        plan=None,
        error=None,
        created_at=created_at,
        retire_after=None,
    )
    token = UUID("80000000-0000-4000-8000-000000000001")
    prefix = attempt_prefix(INPUT, 1, token)
    claim = e.VideoInputClaim(
        input=input_record,
        source=source,
        generation=1,
        lease_token=token,
        lease_expires_at=created_at,
        blob_key=prefix + "source",
    )
    request = e.ClaimVideoInputRequest(worker_id=ACTOR, input_id=INPUT)
    result = t.parse_native(
        "claim_video_tracker_input",
        request,
        {
            "schema": t.TRACKER_SCHEMA,
            "ok": True,
            "data": {"claim": claim.model_dump(mode="json")},
        },
    )
    assert isinstance(result, e.ClaimedVideoInput)
    assert result.claim is not None and result.claim.input.input_id == INPUT


def test_pending_capture_accepts_an_unknown_original_head_identity():
    scope = _scope(file_path="")
    work = t.TrackerVideoInputWork(
        record=_record(scope=scope), source_facts=scope.source_facts
    )
    assert work.source_facts.object_identity is None


def test_materializing_work_requires_the_observed_source_identity():
    scope = _scope()
    with pytest.raises(ValueError, match="measured source version"):
        t.TrackerVideoInputWork(
            record=_record("materializing", scope=scope),
            source_facts=scope.source_facts,
        )


def test_sanitized_http_dtos_do_not_publish_file_paths_or_storage_keys():
    status_schema = t.TrackerVideoInputStatus.model_json_schema()
    reference_schema = t.TrackerVideoSourceReference.model_json_schema()
    status_properties = status_schema["properties"]
    assert "source_facts" not in status_properties
    assert "task" not in status_properties
    assert "file_path" not in status_properties
    ref_text = json.dumps(reference_schema)
    assert '"key"' not in ref_text
    assert "file_path" not in ref_text
    assert set(t.HTTP_PREFIX.split("/")) >= {"v1", "video-tracker-inputs"}


def test_ready_status_rejects_a_range_outside_measured_video_facts():
    with pytest.raises(ValueError, match="outside measured source frames"):
        t.TrackerVideoInputStatus(
            tracker_job_id=JOB,
            input_id=INPUT,
            origin_actor_id=ACTOR,
            task_id=TASK,
            project_id=PROJECT,
            execution=_scope().execution,
            request_digest=DIGEST,
            prompt_digest="b" * 64,
            status="ready",
            plan_digest="c" * 64,
            timebase_digest="d" * 64,
            video=VideoFacts(frame_count=5, width=1920, height=1080, duration_ms=500),
            error=None,
            created_at=datetime.now(UTC),
            retire_after=None,
        )


def test_terminal_prepare_errors_are_always_valid_tracker_response_codes():
    errors = _literal_values(t.TrackerErrorCode)
    assert _literal_values(e.TerminalMemberError) <= errors
    assert "invalid_frame_range" in errors
