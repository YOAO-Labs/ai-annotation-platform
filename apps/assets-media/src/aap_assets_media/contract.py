"""Frozen media data protocol, independent of the API, ORM and task runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

MEDIA_SCHEMA = "aap-assets-media/v1"
REQUEST_BYTES = 8 * 1024 * 1024
REPLY_BYTES = 16 * 1024 * 1024
PAGE_BYTES = 15 * 1024 * 1024
BATCH_SIZE = 500
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
Revision = Annotated[StrictInt, Field(ge=1, le=9_223_372_036_854_775_807)]
Index = Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
PageSize = Annotated[StrictInt, Field(ge=1, le=BATCH_SIZE)]
JsonObject = dict[str, JsonValue]
FrameFormat = Literal["webp", "jpeg"]
WorkKind = Literal[
    "video_metadata", "video_timetable", "frame", "chunk", "video_backfill"
]
ErrorCode = Literal[
    "invalid_request",
    "invalid_response",
    "storage_unavailable",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "source_missing",
    "source_changed",
    "source_busy",
    "task_not_visible",
    "task_busy",
    "permission_denied",
    "metadata_not_ready",
    "invalid_frame_range",
    "asset_missing",
    "operation_conflict",
    "operation_busy",
    "lease_lost",
    "dependency_changed",
    "resource_budget_exceeded",
    "object_missing",
]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("*", mode="after")
    @classmethod
    def require_json_safe_values(cls, value):
        def check(item):
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("NUL is not supported by PostgreSQL values")
            if isinstance(item, dict):
                for key, nested in item.items():
                    check(key)
                    check(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    check(nested)

        check(value)
        return value


class VideoMetadata(BaseModel):
    """Preserve the existing public video metadata fields and defaults."""

    duration_ms: int | None = None
    fps: float | None = None
    frame_count: int | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    playback_path: str | None = None
    playback_codec: str | None = None
    playback_error: str | None = None
    poster_frame_path: str | None = None
    probe_error: str | None = None
    poster_error: str | None = None
    frame_timetable_frame_count: int | None = None
    frame_timetable_error: str | None = None


class ObjectIdentity(Value):
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Count
    version_id: Annotated[StrictStr, Field(max_length=255)] | None = None


class MediaSource(Value):
    dataset_item_id: UUID
    dataset_id: UUID
    source_generation: Revision
    source_snapshot: Digest
    timetable_revision: Count
    materialized_generation: Count
    object_identity: ObjectIdentity | None
    source_drifted: StrictBool
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr
    file_size: Count | None
    content_hash: StrictStr | None
    width: StrictInt | None
    height: StrictInt | None
    thumbnail_path: StrictStr | None
    blurhash: StrictStr | None
    video_metadata: JsonObject


class ReadSourceRequest(Value):
    actor_id: UUID
    dataset_item_id: UUID
    task_id: UUID | None = None


class ObserveSourceRequest(ReadSourceRequest):
    source_snapshot: Digest
    object_identity: ObjectIdentity


class SummaryRequest(Value):
    item_ids: Annotated[list[UUID], Field(max_length=BATCH_SIZE)]


class MediaSummary(Value):
    dataset_item_id: UUID
    width: StrictInt | None
    height: StrictInt | None
    thumbnail_path: StrictStr | None
    blurhash: StrictStr | None
    file_type: StrictStr
    metadata: JsonObject
    source_generation: Revision


class MediaSummaries(Value):
    items: Annotated[list[MediaSummary], Field(max_length=BATCH_SIZE)]


class FrameEntry(Value):
    frame_index: Index
    pts_ms: StrictInt
    is_keyframe: StrictBool
    pict_type: Annotated[StrictStr, Field(max_length=8)] | None = None
    byte_offset: Count | None = None


class PacketSample(Value):
    frame_index: Index
    pts_ms: StrictInt
    duration_ms: Count
    is_keyframe: StrictBool
    size_bytes: Count
    offset_in_chunk: Count


class BackfillSelection(Value):
    dataset_id: UUID | None = None
    dataset_item_ids: Annotated[list[UUID], Field(max_length=BATCH_SIZE)] = Field(
        default_factory=list
    )
    all_items: StrictBool = False
    only_missing: StrictBool = True
    accepted_before: AwareDatetime | None = None

    @model_validator(mode="after")
    def one_selector(self):
        if (
            sum(
                (
                    self.dataset_id is not None,
                    bool(self.dataset_item_ids),
                    self.all_items,
                )
            )
            != 1
        ):
            raise ValueError("backfill requires one selector")
        return self


class BackfillSpec(Value):
    kind: Literal["video_backfill"] = "video_backfill"
    selection: BackfillSelection


class MetadataSpec(Value):
    kind: Literal["video_metadata"] = "video_metadata"
    generate_playback: StrictBool = True
    generate_poster: StrictBool = True
    probe_timetable: StrictBool = True
    expected_source_snapshot: Digest | None = None


class TimetableSpec(Value):
    kind: Literal["video_timetable"] = "video_timetable"
    sparse_stride: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 1
    expected_source_snapshot: Digest | None = None


class FrameSpec(Value):
    kind: Literal["frame"] = "frame"
    frame_index: Index
    width: Annotated[StrictInt, Field(ge=1, le=4096)] = 512
    format: FrameFormat = "webp"
    expected_source_snapshot: Digest | None = None


class ChunkSpec(Value):
    kind: Literal["chunk"] = "chunk"
    chunk_id: Index
    chunk_size_frames: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 60
    expected_source_snapshot: Digest | None = None


WorkSpec = Annotated[
    MetadataSpec | TimetableSpec | FrameSpec | ChunkSpec | BackfillSpec,
    Field(discriminator="kind"),
]


class FrameSelection(Value):
    kind: Literal["frames"] = "frames"
    frame_indices: Annotated[list[Index], Field(max_length=BATCH_SIZE)]
    width: Annotated[StrictInt, Field(ge=1, le=4096)] = 512
    format: FrameFormat = "webp"
    force: StrictBool = False
    failed_only: StrictBool = False

    @model_validator(mode="after")
    def unique_frames(self):
        if len(set(self.frame_indices)) != len(self.frame_indices):
            raise ValueError("duplicate frame indices")
        if self.force and self.failed_only:
            raise ValueError("forced selection cannot filter failed rows")
        return self


class ChunkSelection(Value):
    kind: Literal["chunks"] = "chunks"
    chunk_ids: Annotated[list[Index], Field(max_length=BATCH_SIZE)]
    chunk_size_frames: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 60
    force: StrictBool = False
    failed_only: StrictBool = False

    @model_validator(mode="after")
    def unique_chunks(self):
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("duplicate chunk IDs")
        if self.force and self.failed_only:
            raise ValueError("forced selection cannot filter failed rows")
        return self


Selection = Annotated[FrameSelection | ChunkSelection, Field(discriminator="kind")]


class AssetsRequest(ReadSourceRequest):
    selection: Selection


class MediaAsset(Value):
    row_id: UUID
    dataset_item_id: UUID
    kind: Literal["frame", "chunk"]
    source_generation: Revision
    timetable_revision: Count
    status: Literal["pending", "running", "ready", "failed"]
    storage_key: StrictStr | None = None
    byte_size: Count | None = None
    error: StrictStr | None = None
    job_id: UUID | None = None
    generation: Count = 0
    frame_index: Index | None = None
    width: StrictInt | None = None
    format: FrameFormat | None = None
    chunk_id: Index | None = None
    start_frame: Index | None = None
    end_frame: Index | None = None
    start_pts_ms: StrictInt | None = None
    end_pts_ms: StrictInt | None = None
    generation_mode: Literal["smart_copy", "transcode"] | None = None
    diagnostics: JsonObject = Field(default_factory=dict)
    updated_at: AwareDatetime | None = None
    url: StrictStr | None = None
    expires_in: Count | None = None

    @model_validator(mode="after")
    def validate_locator(self):
        if self.kind == "frame":
            if (
                self.frame_index is None
                or self.width is None
                or self.width <= 0
                or self.format is None
            ):
                raise ValueError("incomplete frame locator")
            if (
                self.chunk_id is not None
                or self.start_frame is not None
                or self.end_frame is not None
            ):
                raise ValueError("frame reply has a chunk locator")
        elif (
            self.chunk_id is None or self.start_frame is None or self.end_frame is None
        ):
            raise ValueError("incomplete chunk locator")
        if self.kind == "chunk" and self.end_frame < self.start_frame:
            raise ValueError("invalid chunk bounds")
        if self.status == "ready" and not self.storage_key:
            raise ValueError("ready media has no object")
        return self


class MediaAssets(Value):
    source: MediaSource
    assets: Annotated[list[MediaAsset], Field(max_length=BATCH_SIZE)]


class RequestWorkRequest(Value):
    operation_id: UUID
    actor_id: UUID
    dataset_item_id: UUID | None
    spec: WorkSpec
    force: StrictBool = False

    @model_validator(mode="after")
    def require_source_scope(self):
        if (self.dataset_item_id is None) != isinstance(self.spec, BackfillSpec):
            raise ValueError("only backfill work omits its source item")
        return self


class ScheduleWorkRequest(Value):
    operation_id: UUID
    dataset_item_id: UUID | None
    spec: WorkSpec
    force: StrictBool = False

    @model_validator(mode="after")
    def require_source_scope(self):
        if (self.dataset_item_id is None) != isinstance(self.spec, BackfillSpec):
            raise ValueError("only backfill work omits its source item")
        return self


class WorkReference(Value):
    job_id: UUID
    generation: Revision
    dataset_item_id: UUID | None
    source_generation: Count
    kind: WorkKind
    status: Literal["pending", "running", "ready", "failed", "canceled"]


class AcceptedWork(Value):
    operation_id: UUID
    source: MediaSource | None
    work: WorkReference
    spec: WorkSpec


class WorkStatusRequest(Value):
    job_id: UUID
    actor_id: UUID | None = None


class WorkStatus(Value):
    work: WorkReference
    spec: WorkSpec
    progress: JsonObject
    error_code: StrictStr | None
    error_detail: StrictStr | None
    lease_expires_at: AwareDatetime | None


class TimetableRequest(ReadSourceRequest):
    from_frame: Index | None = None
    to_frame: Index | None = None
    after_frame: Index | None = None
    page_size: PageSize = BATCH_SIZE


class TimetablePage(Value):
    source: MediaSource
    snapshot_digest: Digest
    total: Count
    after_frame: Index | None
    next_frame: Index | None
    complete: StrictBool
    frames: Annotated[list[FrameEntry], Field(max_length=BATCH_SIZE)]


class SamplesRequest(ReadSourceRequest):
    chunk_id: Index
    after_sample: Index | None = None
    page_size: PageSize = BATCH_SIZE


class SamplesPage(Value):
    dataset_item_id: UUID
    chunk_id: Index
    source_generation: Revision
    timetable_revision: Count
    snapshot_digest: Digest
    codec_string: StrictStr
    description: StrictStr | None
    width: Count
    height: Count
    total: Count
    after_sample: Index | None
    next_sample: Index | None
    complete: StrictBool
    samples: Annotated[list[PacketSample], Field(max_length=BATCH_SIZE)]


class FailureItem(Value):
    asset_key: StrictStr
    asset_type: Literal["probe", "poster", "frame_timetable", "chunk", "frame"]
    dataset_item_id: UUID
    file_name: StrictStr
    error: StrictStr
    task_id: UUID | None = None
    task_display_id: StrictStr | None = None
    project_id: UUID | None = None
    project_name: StrictStr | None = None
    updated_at: AwareDatetime | None = None
    chunk_id: Index | None = None
    frame_index: Index | None = None
    width: Count | None = None
    format: StrictStr | None = None


class FailuresRequest(Value):
    actor_id: UUID
    limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50
    offset: Count = 0


class FailurePage(Value):
    items: Annotated[list[FailureItem], Field(max_length=200)]
    total: Count
    limit: Annotated[StrictInt, Field(ge=1, le=200)]
    offset: Count


class RetrySpec(Value):
    asset_type: Literal["probe", "poster", "frame_timetable", "chunk", "frame"]
    dataset_item_id: UUID
    chunk_id: Index | None = None
    frame_index: Index | None = None
    width: Annotated[StrictInt, Field(ge=1, le=4096)] | None = None
    format: FrameFormat | None = None


class RetryRequest(Value):
    operation_id: UUID
    actor_id: UUID
    spec: RetrySpec


class Invalidation(Value):
    source_snapshot: Digest
    expected_key: StrictStr
    kind: Literal["poster", "playback", "frame", "chunk"]
    frame_index: Index | None = None
    width: Count | None = None
    format: FrameFormat | None = None
    chunk_id: Index | None = None


class InvalidateRequest(ReadSourceRequest):
    invalidation: Invalidation


class InvalidationResult(Value):
    source: MediaSource
    changed: StrictBool


class ClaimRequest(Value):
    worker_id: UUID
    job_id: UUID | None = None
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] = 1200
    kinds: Annotated[list[WorkKind], Field(min_length=1, max_length=5)] = Field(
        default_factory=lambda: [
            "video_metadata",
            "video_timetable",
            "frame",
            "chunk",
            "video_backfill",
        ]
    )


class WorkTiming(Value):
    start_frame: Index | None = None
    end_frame: Index | None = None
    anchors: Annotated[list[FrameEntry], Field(max_length=4)] = Field(
        default_factory=list
    )
    is_keyframe: StrictBool | None = None


class MediaClaim(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    lease_expires_at: AwareDatetime
    source: MediaSource | None
    spec: WorkSpec
    output_prefix: StrictStr | None
    timetable_revision: Count
    timing: WorkTiming

    @model_validator(mode="after")
    def require_attempt_prefix(self):
        expected = f"media-assets/{self.job_id}/g{self.generation}/a{self.lease_token}/"
        if isinstance(self.spec, BackfillSpec):
            if self.output_prefix is not None or self.source is not None:
                raise ValueError("backfill claim has a source or output prefix")
        elif self.output_prefix != expected or self.source is None:
            raise ValueError("media claim escaped its attempt prefix or source")
        return self


class ClaimedWork(Value):
    claim: MediaClaim | None


class LeaseRequest(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID


class HeartbeatRequest(LeaseRequest):
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] = 1200


class LeaseState(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    active: StrictBool
    lease_expires_at: AwareDatetime | None


class StageTimetableRequest(LeaseRequest):
    rows: Annotated[list[FrameEntry], Field(min_length=1, max_length=BATCH_SIZE)]


class StageResult(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    staged_count: Count


class Artifact(Value):
    key: StrictStr
    byte_size: Count
    sha256: Digest


class MetadataResult(Value):
    kind: Literal["video_metadata", "video_timetable"]
    source_identity: ObjectIdentity
    source_sha256: Digest
    metadata_patch: JsonObject
    clear_errors: list[
        Literal[
            "probe_error", "playback_error", "poster_error", "frame_timetable_error"
        ]
    ] = Field(default_factory=list)
    width: Count | None = None
    height: Count | None = None
    timetable_total_frames: Count | None = None
    timetable_staged_count: Count | None = None
    poster: Artifact | None = None
    playback: Artifact | None = None

    @model_validator(mode="after")
    def require_artifact_paths(self):
        for name, artifact in (
            ("poster_frame_path", self.poster),
            ("playback_path", self.playback),
        ):
            if name in self.metadata_patch and (
                artifact is None or self.metadata_patch[name] != artifact.key
            ):
                raise ValueError("metadata path lacks its matching artifact")
        if (self.timetable_total_frames is None) != (
            self.timetable_staged_count is None
        ):
            raise ValueError("timetable publication requires both counts")
        if (
            self.timetable_total_frames is not None
            and self.timetable_staged_count > self.timetable_total_frames
        ):
            raise ValueError("staged timetable exceeds the source frame count")
        return self


class FrameResult(Value):
    kind: Literal["frame"] = "frame"
    source_identity: ObjectIdentity
    source_sha256: Digest
    artifact: Artifact


class ChunkResult(Value):
    kind: Literal["chunk"] = "chunk"
    source_identity: ObjectIdentity
    source_sha256: Digest
    artifact: Artifact
    generation_mode: Literal["smart_copy", "transcode"]
    diagnostics: JsonObject
    start_pts_ms: StrictInt | None = None
    end_pts_ms: StrictInt | None = None


WorkResult = Annotated[
    MetadataResult | FrameResult | ChunkResult, Field(discriminator="kind")
]


class FinishRequest(LeaseRequest):
    result: WorkResult


class WorkOutcome(Value):
    job_id: UUID
    generation: Revision
    outcome: Literal[
        "published",
        "replayed",
        "failed",
        "lease_lost",
        "source_changed",
        "dependency_changed",
    ]


class FailRequest(LeaseRequest):
    error_code: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    error_detail: Annotated[StrictStr, Field(max_length=2000)]


class CancelRequest(Value):
    operation_id: UUID
    actor_id: UUID
    job_id: UUID


class CancelResult(Value):
    operation_id: UUID
    job_id: UUID
    canceled: StrictBool


class ExpireRequest(Value):
    frame_ttl_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 14
    chunk_ttl_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 30
    batch_size: PageSize = BATCH_SIZE


class ExpireResult(Value):
    frames_expired: Count
    chunks_expired: Count
    more: StrictBool


class ClaimGarbageRequest(Value):
    worker_id: UUID
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] = 120
    batch_size: Annotated[StrictInt, Field(ge=1, le=100)] = 20


class GarbageItem(Value):
    garbage_id: UUID
    lease_token: UUID
    prefix: StrictStr | None
    keys: Annotated[list[StrictStr], Field(max_length=BATCH_SIZE)]

    @model_validator(mode="after")
    def require_exact_scope(self):
        if bool(self.prefix) == bool(self.keys):
            raise ValueError("garbage requires one exact prefix or explicit keyset")
        return self


class GarbageBatch(Value):
    items: Annotated[list[GarbageItem], Field(max_length=100)]


class CompleteGarbageRequest(Value):
    garbage_id: UUID
    lease_token: UUID


class GarbageAck(Value):
    garbage_id: UUID
    completed: StrictBool


class BackfillRequest(Value):
    selection: BackfillSelection
    after_item_id: UUID | None = None
    page_size: PageSize = BATCH_SIZE


class BackfillPage(Value):
    items: Annotated[list[MediaSource], Field(max_length=BATCH_SIZE)]
    snapshot_digest: Digest
    total: Count
    accepted_before: AwareDatetime
    after_item_id: UUID | None
    next_item_id: UUID | None
    complete: StrictBool


class AdvanceBackfillRequest(LeaseRequest):
    batch_size: PageSize = BATCH_SIZE


class BackfillProgress(Value):
    job_id: UUID
    generation: Revision
    lease_token: UUID
    scheduled: Count
    skipped: Count
    complete: StrictBool
    next_item_id: UUID | None


class InternalFrameRequest(Value):
    dataset_item_id: UUID
    frame_index: Index
    width: Annotated[StrictInt, Field(ge=1, le=4096)] = 512
    format: FrameFormat = "webp"


class UsageRequest(Value):
    pass


class MediaUsage(Value):
    frame_bytes: Count
    chunk_bytes: Count
    pending: Count
    running: Count
    failed: Count


class MediaPreferences(Value):
    warmup_lookahead: Count | None


@dataclass(frozen=True)
class NativeOperation:
    request: type[BaseModel]
    result: type[BaseModel]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["control", "worker", "system"]


OPERATIONS = {
    "read_media_source": NativeOperation(
        ReadSourceRequest,
        MediaSource,
        ("actor_id", "dataset_item_id", "task_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "read_media_summaries": NativeOperation(
        SummaryRequest, MediaSummaries, ("item_ids",), ("uuid[]",), "control"
    ),
    "ensure_video_media": NativeOperation(
        AssetsRequest,
        MediaAssets,
        ("actor_id", "dataset_item_id", "task_id", "selection"),
        ("uuid", "uuid", "uuid", "jsonb"),
        "control",
    ),
    "observe_media_source": NativeOperation(
        ObserveSourceRequest,
        MediaSource,
        (
            "actor_id",
            "dataset_item_id",
            "task_id",
            "source_snapshot",
            "object_identity",
        ),
        ("uuid", "uuid", "uuid", "text", "jsonb"),
        "control",
    ),
    "request_video_media": NativeOperation(
        RequestWorkRequest,
        AcceptedWork,
        ("operation_id", "actor_id", "dataset_item_id", "spec", "force"),
        ("uuid", "uuid", "uuid", "jsonb", "boolean"),
        "control",
    ),
    "schedule_video_media": NativeOperation(
        ScheduleWorkRequest,
        AcceptedWork,
        ("operation_id", "dataset_item_id", "spec", "force"),
        ("uuid", "uuid", "jsonb", "boolean"),
        "system",
    ),
    "read_video_work": NativeOperation(
        WorkStatusRequest,
        WorkStatus,
        ("job_id", "actor_id"),
        ("uuid", "uuid"),
        "system",
    ),
    "read_video_timetable": NativeOperation(
        TimetableRequest,
        TimetablePage,
        (
            "actor_id",
            "dataset_item_id",
            "task_id",
            "from_frame",
            "to_frame",
            "after_frame",
            "page_size",
        ),
        ("uuid", "uuid", "uuid", "integer", "integer", "integer", "integer"),
        "control",
    ),
    "read_video_samples": NativeOperation(
        SamplesRequest,
        SamplesPage,
        (
            "actor_id",
            "dataset_item_id",
            "task_id",
            "chunk_id",
            "after_sample",
            "page_size",
        ),
        ("uuid", "uuid", "uuid", "integer", "integer", "integer"),
        "control",
    ),
    "read_video_failures": NativeOperation(
        FailuresRequest,
        FailurePage,
        ("actor_id", "limit", "offset"),
        ("uuid", "integer", "bigint"),
        "control",
    ),
    "retry_video_media": NativeOperation(
        RetryRequest,
        AcceptedWork,
        ("operation_id", "actor_id", "spec"),
        ("uuid", "uuid", "jsonb"),
        "control",
    ),
    "invalidate_video_media": NativeOperation(
        InvalidateRequest,
        InvalidationResult,
        ("actor_id", "dataset_item_id", "task_id", "invalidation"),
        ("uuid", "uuid", "uuid", "jsonb"),
        "control",
    ),
    "claim_video_media": NativeOperation(
        ClaimRequest,
        ClaimedWork,
        ("worker_id", "job_id", "lease_seconds", "kinds"),
        ("uuid", "uuid", "integer", "text[]"),
        "worker",
    ),
    "cancel_video_media": NativeOperation(
        CancelRequest,
        CancelResult,
        ("operation_id", "actor_id", "job_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "heartbeat_video_media": NativeOperation(
        HeartbeatRequest,
        LeaseState,
        ("job_id", "generation", "lease_token", "lease_seconds"),
        ("uuid", "bigint", "uuid", "integer"),
        "worker",
    ),
    "stage_video_timetable": NativeOperation(
        StageTimetableRequest,
        StageResult,
        ("job_id", "generation", "lease_token", "rows"),
        ("uuid", "bigint", "uuid", "jsonb"),
        "worker",
    ),
    "finish_video_media": NativeOperation(
        FinishRequest,
        WorkOutcome,
        ("job_id", "generation", "lease_token", "result"),
        ("uuid", "bigint", "uuid", "jsonb"),
        "worker",
    ),
    "fail_video_media": NativeOperation(
        FailRequest,
        WorkOutcome,
        ("job_id", "generation", "lease_token", "error_code", "error_detail"),
        ("uuid", "bigint", "uuid", "text", "text"),
        "worker",
    ),
    "expire_video_media": NativeOperation(
        ExpireRequest,
        ExpireResult,
        ("frame_ttl_days", "chunk_ttl_days", "batch_size"),
        ("integer", "integer", "integer"),
        "worker",
    ),
    "claim_video_media_garbage": NativeOperation(
        ClaimGarbageRequest,
        GarbageBatch,
        ("worker_id", "lease_seconds", "batch_size"),
        ("uuid", "integer", "integer"),
        "worker",
    ),
    "complete_video_media_garbage": NativeOperation(
        CompleteGarbageRequest,
        GarbageAck,
        ("garbage_id", "lease_token"),
        ("uuid", "uuid"),
        "worker",
    ),
    "scan_video_backfill": NativeOperation(
        BackfillRequest,
        BackfillPage,
        ("selection", "after_item_id", "page_size"),
        ("jsonb", "uuid", "integer"),
        "system",
    ),
    "advance_video_backfill": NativeOperation(
        AdvanceBackfillRequest,
        BackfillProgress,
        ("job_id", "generation", "lease_token", "batch_size"),
        ("uuid", "bigint", "uuid", "integer"),
        "worker",
    ),
    "read_internal_video_frame": NativeOperation(
        InternalFrameRequest,
        MediaAssets,
        ("dataset_item_id", "frame_index", "width", "format"),
        ("uuid", "integer", "integer", "text"),
        "system",
    ),
    "read_video_media_usage": NativeOperation(
        UsageRequest, MediaUsage, (), (), "worker"
    ),
    "read_media_preferences": NativeOperation(
        UsageRequest, MediaPreferences, (), (), "control"
    ),
}


class MediaFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


class _Error(Value):
    code: ErrorCode


_Result = TypeVar("_Result", bound=BaseModel)


def json_bytes(value: Any) -> bytes:
    def default(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, UUID):
            return str(item)
        raise TypeError("not a media JSON value")

    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, default=default
    ).encode("utf-8")


def _reject_nonfinite(_value):
    raise ValueError("nonfinite JSON")


def decode_reply(raw: Any, result_type: type[_Result]) -> _Result:
    try:
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized reply")
            raw = json.loads(raw, parse_constant=_reject_nonfinite)
        elif len(json_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized reply")
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != MEDIA_SCHEMA
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid media envelope")
        if not raw["ok"]:
            if set(raw) != {"schema", "ok", "error"}:
                raise ValueError("invalid media failure")
            raise MediaFailure(_Error.model_validate(raw["error"]).code)
        if set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid media success")
        return result_type.model_validate(raw["data"])
    except MediaFailure:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise MediaFailure("invalid_response") from None


def require_scope(request: BaseModel, result: BaseModel) -> None:
    for field in ("operation_id", "job_id", "generation", "lease_token", "garbage_id"):
        if (
            hasattr(request, field)
            and hasattr(result, field)
            and getattr(request, field) != getattr(result, field)
        ):
            raise ValueError("media result escaped its request")
    if hasattr(request, "dataset_item_id"):
        source = getattr(result, "source", result)
        if (
            hasattr(source, "dataset_item_id")
            and source.dataset_item_id != request.dataset_item_id
        ):
            raise ValueError("media source escaped its request")
    if isinstance(result, MediaAssets):
        if any(asset.status == "ready" for asset in result.assets) and (
            result.source.source_drifted
            or result.source.materialized_generation != result.source.source_generation
        ):
            raise ValueError("media served an unverified source generation")
        if any(
            asset.dataset_item_id != result.source.dataset_item_id
            or asset.source_generation != result.source.source_generation
            or asset.timetable_revision != result.source.timetable_revision
            for asset in result.assets
        ):
            raise ValueError("media assets mixed source generations")
        if isinstance(request, AssetsRequest):
            selection = request.selection
            if isinstance(selection, FrameSelection):
                requested = set(selection.frame_indices)
                if any(
                    asset.kind != "frame"
                    or asset.width != selection.width
                    or asset.format != selection.format
                    or (requested and asset.frame_index not in requested)
                    for asset in result.assets
                ):
                    raise ValueError("media returned an unrequested frame")
                locators = [asset.frame_index for asset in result.assets]
            else:
                requested = set(selection.chunk_ids)
                if any(
                    asset.kind != "chunk"
                    or (requested and asset.chunk_id not in requested)
                    for asset in result.assets
                ):
                    raise ValueError("media returned an unrequested chunk")
                locators = [asset.chunk_id for asset in result.assets]
            if len(set(locators)) != len(locators):
                raise ValueError("media returned duplicate assets")
            if requested and not selection.failed_only and set(locators) != requested:
                raise ValueError("media omitted requested assets")
    if isinstance(result, ClaimedWork) and result.claim is not None:
        if request.job_id is not None and request.job_id != result.claim.job_id:
            raise ValueError("media claim substituted another job")
        if result.claim.spec.kind not in request.kinds:
            raise ValueError("media claim exceeded worker capabilities")
    if isinstance(result, WorkStatus) and result.work.job_id != request.job_id:
        raise ValueError("work status substituted another job")
    if isinstance(result, TimetablePage):
        indices = [frame.frame_index for frame in result.frames]
        if (
            result.after_frame != request.after_frame
            or indices != sorted(set(indices))
            or len(indices) > request.page_size
        ):
            raise ValueError("invalid timetable page ordering or cursor")
        if any(
            (request.after_frame is not None and index <= request.after_frame)
            or (request.from_frame is not None and index < request.from_frame)
            or (request.to_frame is not None and index > request.to_frame)
            for index in indices
        ):
            raise ValueError("timetable page escaped its range")
        if (
            result.complete != (result.next_frame is None)
            or (
                not result.complete
                and (not indices or result.next_frame != indices[-1])
            )
            or len(indices) > result.total
        ):
            raise ValueError("invalid timetable page progress")
    if isinstance(result, SamplesPage):
        if (
            result.chunk_id != request.chunk_id
            or result.after_sample != request.after_sample
            or len(result.samples) > request.page_size
        ):
            raise ValueError("samples escaped their chunk or cursor")
        end = (
            request.after_sample + 1 if request.after_sample is not None else 0
        ) + len(result.samples)
        if (
            end > result.total
            or result.complete != (end == result.total)
            or result.complete != (result.next_sample is None)
            or (
                not result.complete
                and (not result.samples or result.next_sample != end - 1)
            )
        ):
            raise ValueError("invalid sample page progress")
    if isinstance(result, BackfillPage):
        ids = [item.dataset_item_id for item in result.items]
        if (
            result.after_item_id != request.after_item_id
            or ids != sorted(set(ids))
            or len(ids) > request.page_size
            or any(
                request.after_item_id is not None and item <= request.after_item_id
                for item in ids
            )
        ):
            raise ValueError("invalid backfill page order or scope")
        if result.complete != (result.next_item_id is None) or (
            not result.complete and (not ids or result.next_item_id != ids[-1])
        ):
            raise ValueError("invalid backfill page progress")
    if isinstance(result, AcceptedWork):
        if result.work.kind == "video_backfill":
            if (
                result.source is not None
                or result.work.dataset_item_id is not None
                or result.work.source_generation != 0
            ):
                raise ValueError("backfill result has a source")
        elif result.source is None or (
            result.work.dataset_item_id != result.source.dataset_item_id
            or result.work.source_generation != result.source.source_generation
        ):
            raise ValueError("accepted work mixed source generations")
        if hasattr(request, "spec") and isinstance(
            request.spec,
            (MetadataSpec, TimetableSpec, FrameSpec, ChunkSpec, BackfillSpec),
        ):
            if result.work.kind != request.spec.kind:
                raise ValueError("accepted work changed its kind")
            expected = getattr(request.spec, "expected_source_snapshot", None)
            if expected is not None and (
                result.source is None or expected != result.source.source_snapshot
            ):
                raise ValueError("accepted work changed its source")
            wanted = request.spec.model_dump(mode="json")
            accepted = result.spec.model_dump(mode="json")
            if isinstance(request.spec, BackfillSpec):
                if request.spec.selection.accepted_before is None:
                    if result.spec.selection.accepted_before is None:
                        raise ValueError("backfill acceptance omitted its cutoff")
                    wanted["selection"]["accepted_before"] = accepted["selection"][
                        "accepted_before"
                    ]
            if wanted != accepted:
                raise ValueError("accepted work changed its parameters")


def require_result_scope(claim: MediaClaim, result: WorkResult) -> None:
    if claim.spec.kind != result.kind:
        raise ValueError("media result changed the accepted operation")
    if isinstance(result, MetadataResult):
        artifacts = ((result.poster, "poster.webp"), (result.playback, "playback.mp4"))
    elif isinstance(result, FrameResult):
        artifacts = ((result.artifact, "frame." + claim.spec.format),)
    else:
        artifacts = ((result.artifact, "chunk.mp4"),)
    if claim.output_prefix is None or any(
        artifact is not None and artifact.key != claim.output_prefix + suffix
        for artifact, suffix in artifacts
    ):
        raise ValueError("media artifact escaped its leased prefix")


def contract_document() -> dict:
    definitions: dict[str, Any] = {}
    for operation in OPERATIONS.values():
        for model in (operation.request, operation.result):
            schema = model.model_json_schema()
            for key, value in schema.pop("$defs", {}).items():
                if key in definitions and definitions[key] != value:
                    raise ValueError("conflicting media schema definitions")
                definitions[key] = value
            if model.__name__ in definitions and definitions[model.__name__] != schema:
                raise ValueError("conflicting media schema definition")
            definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": MEDIA_SCHEMA,
        "$defs": definitions,
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_bytes": PAGE_BYTES,
            "batch_size": BATCH_SIZE,
        },
        "x-native": {
            name: {
                "signature": "aap_assets_v1."
                + name
                + "("
                + ",".join(operation.sql_types)
                + ")",
                "arguments": list(operation.arguments),
                "request": operation.request.__name__,
                "result": operation.result.__name__,
                "role": operation.role,
            }
            for name, operation in OPERATIONS.items()
        },
    }
