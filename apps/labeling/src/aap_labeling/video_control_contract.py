"""Typed transaction-local commands for Labeling video-tracker control."""

from __future__ import annotations

import uuid
import json
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .annotation_write_contract import _json_bytes

VIDEO_CONTROL_PROTOCOL = "aap-labeling-video-control/v1"
VIDEO_CONTROL_FUNCTION = "aap_labeling_v1.video_control_command(jsonb)"
MAX_VIDEO_CONTROL_REQUEST_BYTES = 135_266_304


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_Closed):
    schema_: Literal["aap-labeling-video-control/v1"] = Field(alias="schema")
    operation: StrictStr


class ReadJob(_Command):
    operation: Literal["read_job"]
    job_id: uuid.UUID
    actor_id: uuid.UUID


class ReadWorkerJob(_Command):
    operation: Literal["read_worker_job"]
    job_id: uuid.UUID


class LockWorkerJob(_Command):
    operation: Literal["lock_worker_job"]
    job_id: uuid.UUID


class StartWorkerJob(_Command):
    operation: Literal["start_worker_job"]
    job_id: uuid.UUID
    celery_task_id: Annotated[StrictStr, Field(min_length=1)] | None
    started_at: AwareDatetime


class LockReviewJob(_Command):
    operation: Literal["lock_review_job"]
    job_id: uuid.UUID
    actor_id: uuid.UUID


class ReadTaskJobs(_Command):
    operation: Literal["read_task_jobs"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    statuses: tuple[
        Literal[
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "pending_review",
            "partially_reviewed",
            "accepted",
            "discarded",
        ],
        ...,
    ]


class ReadActiveCorrection(_Command):
    operation: Literal["read_active_correction"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    track_id: StrictStr


class ReadAnnotations(_Command):
    operation: Literal["read_annotations"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    annotation_ids: tuple[uuid.UUID, ...]
    lock_for_update: StrictBool = False


class ReviewSourceVersion(_Closed):
    annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)


class SourceAnnotationWrite(_Closed):
    annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)
    geometry: dict[str, Any]
    track_id: StrictStr | None
    annotation_type: StrictStr
    tool_unit_id: StrictStr


class NewTrackWrite(_Closed):
    annotation_id: uuid.UUID
    video_segment_id: uuid.UUID | None
    user_id: uuid.UUID | None
    class_name: StrictStr
    tool_unit_id: StrictStr
    geometry: dict[str, Any]
    track_id: StrictStr
    annotation_type: StrictStr


class VideoTrackerJobRecord(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID
    annotation_id: uuid.UUID | None
    segment_id: uuid.UUID | None
    target_class_name: StrictStr | None
    target_tool_unit_id: StrictStr | None
    created_by: uuid.UUID | None
    status: Literal[
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "pending_review",
        "partially_reviewed",
        "accepted",
        "discarded",
    ]
    job_kind: Literal["tracking", "correction"]
    track_id_snapshot: StrictStr | None
    correction_frame: StrictInt | None
    model_key: StrictStr
    direction: Literal["forward", "backward", "bidirectional"]
    from_frame: StrictInt
    to_frame: StrictInt
    prompt: dict[str, Any]
    staged_result: dict[str, Any] | None
    revision: StrictInt
    event_channel: StrictStr
    celery_task_id: StrictStr | None
    cancel_requested_at: AwareDatetime | None
    started_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    error_message: StrictStr | None
    created_at: AwareDatetime
    updated_at: AwareDatetime | None


class VideoAnnotationRecord(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID | None
    video_segment_id: uuid.UUID | None
    user_id: uuid.UUID | None
    source: StrictStr
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, Any]
    confidence: float | None
    parent_prediction_id: uuid.UUID | None
    parent_annotation_id: uuid.UUID | None
    track_id: StrictStr | None
    scene_track_id: uuid.UUID | None
    sensor_dataset_item_id: uuid.UUID | None
    sensor_role: StrictStr | None
    sensor_visibility: StrictStr | None
    calibration_revision: StrictInt | None
    calibration_digest: StrictStr | None
    temporal_role: Literal["keyframe", "derived", "sample"]
    lead_time: float | None
    was_cancelled: StrictBool
    ground_truth: StrictBool
    is_active: StrictBool
    attributes: dict[str, Any]
    attributes_meta: dict[str, Any]
    z_order: StrictInt
    is_locked: StrictBool
    is_hidden: StrictBool
    version: StrictInt
    created_at: AwareDatetime
    updated_at: AwareDatetime


class VideoSegmentRecord(_Closed):
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    segment_index: StrictInt
    start_frame: StrictInt
    end_frame: StrictInt
    assignee_id: uuid.UUID | None
    status: StrictStr
    locked_by: uuid.UUID | None
    locked_at: AwareDatetime | None
    lock_expires_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class MaskReviewScopeWrite(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID
    annotation_id: uuid.UUID
    qc_issue_id: uuid.UUID
    source_job_id: uuid.UUID
    reviewer_id: uuid.UUID
    source_annotation_version: StrictInt = Field(ge=1)
    result_annotation_version: StrictInt = Field(ge=1)
    source_job_revision: StrictInt = Field(ge=1)
    frame_start: StrictInt = Field(ge=0)
    frame_end: StrictInt = Field(ge=0)
    region_mask_ref: dict[str, Any]
    region_digest: StrictStr
    candidate_digest: StrictStr
    decision: Literal["accept", "reject"]


class CheckReviewScopes(_Command):
    operation: Literal["check_review_scopes"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    annotation_id: uuid.UUID
    result_annotation_version: StrictInt = Field(ge=1)
    from_frame: StrictInt = Field(ge=0)
    to_frame: StrictInt = Field(ge=0)
    frame_indices: tuple[StrictInt, ...] | None


class MaskReviewScopeRecord(_Closed):
    scope_id: uuid.UUID
    frame_start: StrictInt
    frame_end: StrictInt
    region_mask_ref: dict[str, Any]
    region_digest: StrictStr
    decision: Literal["accept", "reject"]


class CheckReviewScopesData(_Closed):
    scopes: tuple[MaskReviewScopeRecord, ...]


class VideoControlAnnotationWriteData(_Closed):
    annotation_id: uuid.UUID
    version: StrictInt = Field(ge=1)
    updated_at: AwareDatetime


class VideoControlSegmentsData(_Closed):
    segments: tuple[VideoSegmentRecord, ...]
    segment_count: StrictInt = Field(ge=0)


class VideoControlWorkerSegmentData(_Closed):
    segment: VideoSegmentRecord | None
    segment_count: StrictInt = Field(ge=0)


class VideoControlExpiryData(_Closed):
    expired: StrictInt = Field(ge=0)


class VideoControlTimelineRow(_Closed):
    job: VideoTrackerJobRecord
    project_id: uuid.UUID


class VideoControlTimelineData(_Closed):
    counts: dict[StrictStr, StrictInt]
    rows: tuple[VideoControlTimelineRow, ...]
    has_more: StrictBool


class LockReviewAnnotations(_Command):
    operation: Literal["lock_review_annotations"]
    job_id: uuid.UUID
    task_id: uuid.UUID
    actor_id: uuid.UUID
    annotations: tuple[ReviewSourceVersion, ...]


class ReadSegments(_Command):
    operation: Literal["read_segments"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    dataset_item_id: uuid.UUID
    segment_id: uuid.UUID | None
    from_frame: StrictInt = Field(ge=0)
    to_frame: StrictInt = Field(ge=0)
    nowait: StrictBool = False
    count_segments: StrictBool = True


class ReadWorkerSegment(_Command):
    operation: Literal["read_worker_segment"]
    job_id: uuid.UUID


class EnsureSegments(_Command):
    operation: Literal["ensure_segments"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_task_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    dataset_item_id: uuid.UUID
    segment_id: uuid.UUID | None
    from_frame: StrictInt = Field(ge=0)
    to_frame: StrictInt = Field(ge=0)
    frame_count: StrictInt = Field(ge=1)
    segment_size_frames: StrictInt = Field(ge=1)
    now: AwareDatetime
    count_segments: StrictBool = True


class ExpireCandidates(_Command):
    operation: Literal["expire_candidates"]
    cutoff: AwareDatetime


class CreateJob(_Command):
    operation: Literal["create_job"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    dataset_item_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_task_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    annotation_id: uuid.UUID | None
    segment_id: uuid.UUID
    target_class_name: StrictStr | None
    target_tool_unit_id: StrictStr | None
    job_kind: Literal["tracking", "correction"]
    track_id_snapshot: StrictStr | None
    correction_frame: StrictInt | None
    model_key: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    direction: Literal["forward", "backward", "bidirectional"]
    from_frame: StrictInt = Field(ge=0)
    to_frame: StrictInt = Field(ge=0)
    prompt: dict[str, Any]


class CancelJob(_Command):
    operation: Literal["cancel_job"]
    job_id: uuid.UUID
    actor_id: uuid.UUID
    now: AwareDatetime


class SaveKeyframe(_Command):
    operation: Literal["save_keyframe"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_task_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    annotation_id: uuid.UUID
    segment_id: uuid.UUID
    frame_index: StrictInt = Field(ge=0)
    expected_version: StrictInt = Field(ge=1)
    geometry: dict[str, Any]


class OperateKeyframe(_Command):
    operation: Literal["operate_keyframe"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_task_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    annotation_id: uuid.UUID
    segment_id: uuid.UUID
    frame_index: StrictInt = Field(ge=0)
    expected_version: StrictInt = Field(ge=1)
    geometry: dict[str, Any]


class ApplyReview(_Command):
    operation: Literal["apply_review"]
    job_id: uuid.UUID
    task_id: uuid.UUID
    actor_id: uuid.UUID
    kind: Literal["accept", "decide", "discard"]
    expected_revision: StrictInt = Field(ge=1)
    expected_status: Literal["pending_review", "partially_reviewed", "cancelled"]
    prompt: dict[str, Any]
    staged_result: dict[str, Any] | None
    status: Literal["partially_reviewed", "accepted", "discarded"]
    source_annotations: tuple[SourceAnnotationWrite, ...]
    new_tracks: tuple[NewTrackWrite, ...]
    mask_review_scope: MaskReviewScopeWrite | None = None


class MarkEnqueued(_Command):
    operation: Literal["mark_enqueued"]
    job_id: uuid.UUID
    celery_task_id: StrictStr


class MarkEnqueueFailed(_Command):
    operation: Literal["mark_enqueue_failed"]
    job_id: uuid.UUID
    completed_at: AwareDatetime


class ListTimeline(_Command):
    operation: Literal["list_timeline"]
    actor_id: uuid.UUID
    model_key: StrictStr | None
    project_id: uuid.UUID | None
    allowed_project_ids: tuple[uuid.UUID, ...] | None
    status: StrictStr | None
    cursor_created_at: AwareDatetime | None
    cursor_id: uuid.UUID | None
    limit: StrictInt = Field(ge=1, le=100)


VideoControlCommand = Annotated[
    ReadJob
    | ReadWorkerJob
    | LockWorkerJob
    | StartWorkerJob
    | LockReviewJob
    | ReadTaskJobs
    | ReadActiveCorrection
    | ReadAnnotations
    | LockReviewAnnotations
    | ReadSegments
    | ReadWorkerSegment
    | EnsureSegments
    | CheckReviewScopes
    | ExpireCandidates
    | CreateJob
    | CancelJob
    | SaveKeyframe
    | OperateKeyframe
    | ApplyReview
    | MarkEnqueued
    | MarkEnqueueFailed
    | ListTimeline,
    Field(discriminator="operation"),
]


class VideoControlReply(_Closed):
    schema_: Literal["aap-labeling-video-control/v1"] = Field(alias="schema")
    ok: StrictBool
    request: VideoControlCommand
    data: Any


class VideoControlUnavailable(RuntimeError):
    """The video control owner returned invalid or mismatched data."""


_COMMAND = TypeAdapter(VideoControlCommand)
_REPLY = TypeAdapter(VideoControlReply)
_JOB_ROWS = TypeAdapter(tuple[VideoTrackerJobRecord, ...])
_ANNOTATION_ROWS = TypeAdapter(tuple[VideoAnnotationRecord, ...])
_UUID_OR_NONE = TypeAdapter(uuid.UUID | None)


def _decode_result(request: VideoControlCommand, data: Any):
    if isinstance(
        request,
        (
            ReadJob,
            ReadWorkerJob,
            LockWorkerJob,
            StartWorkerJob,
            LockReviewJob,
            CreateJob,
            CancelJob,
            ApplyReview,
            MarkEnqueued,
            MarkEnqueueFailed,
        ),
    ):
        if data is None:
            return None
        row = VideoTrackerJobRecord.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
        if isinstance(request, (ReadWorkerJob, LockWorkerJob, StartWorkerJob)):
            if row["id"] != request.job_id:
                raise VideoControlUnavailable()
        return row
    if isinstance(request, ReadTaskJobs):
        return tuple(
            value.model_dump(mode="python")
            for value in _JOB_ROWS.validate_json(
                json.dumps(data, ensure_ascii=True, allow_nan=False)
            )
        )
    if isinstance(request, ReadActiveCorrection):
        if data is None:
            return None
        return _UUID_OR_NONE.validate_json(json.dumps(data))
    if isinstance(request, (ReadAnnotations, LockReviewAnnotations)):
        return tuple(
            value.model_dump(mode="python")
            for value in _ANNOTATION_ROWS.validate_json(
                json.dumps(data, ensure_ascii=True, allow_nan=False)
            )
        )
    if isinstance(request, (ReadSegments, EnsureSegments)):
        return VideoControlSegmentsData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
    if isinstance(request, ReadWorkerSegment):
        return VideoControlWorkerSegmentData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
    if isinstance(request, ExpireCandidates):
        return VideoControlExpiryData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
    if isinstance(request, CheckReviewScopes):
        return CheckReviewScopesData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).scopes
    if isinstance(request, (SaveKeyframe, OperateKeyframe)):
        return VideoControlAnnotationWriteData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
    if isinstance(request, ListTimeline):
        return VideoControlTimelineData.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        ).model_dump(mode="python")
    raise VideoControlUnavailable()


def _control_bytes(value: object) -> bytes:
    raw = _json_bytes(value)
    if len(raw) > MAX_VIDEO_CONTROL_REQUEST_BYTES:
        raise ValueError("video control message exceeds its wire limit")
    return raw


def decode_video_control_command(value: object) -> VideoControlCommand:
    try:
        return _COMMAND.validate_json(_control_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise VideoControlUnavailable() from None


def video_control_command_json(command: VideoControlCommand) -> str:
    encoded = decode_video_control_command(
        command.model_dump_json(by_alias=True)
    ).model_dump_json(by_alias=True)
    _control_bytes(encoded)
    return encoded


def decode_video_control_reply(request: VideoControlCommand, value: object) -> Any:
    try:
        reply = _REPLY.validate_json(_control_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise VideoControlUnavailable() from None
    if not reply.ok or reply.request != request:
        raise VideoControlUnavailable()
    try:
        return _decode_result(request, reply.data)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise VideoControlUnavailable() from None


def video_control_schema() -> dict[str, Any]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = VIDEO_CONTROL_PROTOCOL
    schema["x-postgresql-functions"] = [VIDEO_CONTROL_FUNCTION]
    for model in (
        VideoTrackerJobRecord,
        VideoAnnotationRecord,
        VideoSegmentRecord,
        VideoControlSegmentsData,
        VideoControlWorkerSegmentData,
        CheckReviewScopesData,
        VideoControlExpiryData,
        VideoControlAnnotationWriteData,
        VideoControlTimelineData,
    ):
        model_schema = model.model_json_schema()
        schema["$defs"].update(model_schema.pop("$defs", {}))
        schema["$defs"][model.__name__] = model_schema
    job = {"$ref": "#/$defs/VideoTrackerJobRecord"}
    annotation = {"$ref": "#/$defs/VideoAnnotationRecord"}
    segment_data = {"$ref": "#/$defs/VideoControlSegmentsData"}
    worker_segment_data = {"$ref": "#/$defs/VideoControlWorkerSegmentData"}
    schema["x-data-contracts"] = {
        "read_job": job,
        "read_worker_job": {"oneOf": [job, {"type": "null"}]},
        "lock_worker_job": {"oneOf": [job, {"type": "null"}]},
        "start_worker_job": {"oneOf": [job, {"type": "null"}]},
        "lock_review_job": job,
        "read_task_jobs": {"type": "array", "items": job},
        "read_active_correction": {
            "oneOf": [
                {"format": "uuid", "type": "string"},
                {"type": "null"},
            ]
        },
        "read_annotations": {"type": "array", "items": annotation},
        "lock_review_annotations": {"type": "array", "items": annotation},
        "read_segments": segment_data,
        "read_worker_segment": worker_segment_data,
        "ensure_segments": segment_data,
        "check_review_scopes": {"$ref": "#/$defs/CheckReviewScopesData"},
        "expire_candidates": {"$ref": "#/$defs/VideoControlExpiryData"},
        "create_job": job,
        "cancel_job": job,
        "save_keyframe": {"$ref": "#/$defs/VideoControlAnnotationWriteData"},
        "operate_keyframe": {"$ref": "#/$defs/VideoControlAnnotationWriteData"},
        "apply_review": job,
        "mark_enqueued": job,
        "mark_enqueue_failed": {"oneOf": [job, {"type": "null"}]},
        "list_timeline": {"$ref": "#/$defs/VideoControlTimelineData"},
    }
    return schema


__all__ = [
    "VIDEO_CONTROL_FUNCTION",
    "VIDEO_CONTROL_PROTOCOL",
    "MAX_VIDEO_CONTROL_REQUEST_BYTES",
    "ApplyReview",
    "CancelJob",
    "CheckReviewScopes",
    "CreateJob",
    "ExpireCandidates",
    "EnsureSegments",
    "LockReviewAnnotations",
    "LockReviewJob",
    "LockWorkerJob",
    "MarkEnqueued",
    "MarkEnqueueFailed",
    "MaskReviewScopeWrite",
    "OperateKeyframe",
    "ReadAnnotations",
    "ReadActiveCorrection",
    "ReadJob",
    "ReadSegments",
    "ReadTaskJobs",
    "ReadWorkerJob",
    "ReadWorkerSegment",
    "ReviewSourceVersion",
    "SaveKeyframe",
    "SourceAnnotationWrite",
    "NewTrackWrite",
    "StartWorkerJob",
    "VideoControlCommand",
    "VideoControlUnavailable",
    "VideoControlWorkerSegmentData",
    "decode_video_control_command",
    "decode_video_control_reply",
    "video_control_command_json",
    "video_control_schema",
]
