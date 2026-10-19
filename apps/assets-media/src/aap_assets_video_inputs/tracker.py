"""Private full-video inputs for the canonical Labeling video-tracker ledger.

The accepted execution snapshot lives beside the Labeling-owned
``VideoTrackerJob``. Assets owns byte materialization and exact-version reads;
the tracker contract never routes through the older frame-only batch authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from . import execution as e
from .values import (
    Count,
    Digest,
    FrozenFramePlan,
    ObjectIdentity,
    Revision,
    TaskVideoBinding,
    Value,
    VideoFacts,
)

TRACKER_SCHEMA = "aap-assets-video-tracker-inputs/v1"
LABELING_SCHEMA = "aap-labeling-video-tracker-inputs/v1"
CONTROL_SCHEMA = "aap-assets-video-tracker-control/v1"
HTTP_PREFIX = "/v1/video-tracker-inputs"
REQUEST_BYTES = 64 * 1024
REPLY_BYTES = 2 * 1024 * 1024
URL_EXPIRY_SECONDS = 3600
WORKSPACE_POLICY_SCHEMA = "aap-workspace-video-collaboration-policy/v1"

JobKind = Literal["tracking", "correction"]
Direction = Literal["forward", "backward", "bidirectional"]
TrackerErrorCode = e.ErrorCode
Status = e.Status
TerminalMemberError = e.TerminalMemberError
Index = Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
_URL_EXPIRY = Annotated[StrictInt, Field(ge=1, le=URL_EXPIRY_SECONDS)]


class TrackerFailure(Exception):
    def __init__(self, code: TrackerErrorCode):
        self.code = code
        super().__init__(code)


TrackerInputFailure = TrackerFailure


class MediaVideoCollaborationPolicy(Value):
    project_id: UUID
    enabled: StrictBool
    overlap_frames: Annotated[StrictInt, Field(ge=0)]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


class TrackerSourceAnnotationSnapshot(Value):
    """Stable source geometry used to seed this tracking execution."""

    annotation_id: UUID
    version: Revision
    frame_index: Index
    geometry: dict[str, Any]
    geometry_digest: Digest


class TrackerCorrectionMaskSnapshot(Value):
    """Exact correction keyframe input captured at admission."""

    annotation_id: UUID
    annotation_version: Revision
    frame_index: Index
    mask: dict[str, Any]
    mask_digest: Digest


class TrackerExecutionSpec(Value):
    """Immutable inference facts, independent of mutable review prompt JSON.

    ``prompt_snapshot`` contains the accepted prompt and seed material after
    excluding fields the existing review workflow mutates (routing backend,
    expected source versions, and touched annotation IDs). It is stored with
    the typed source geometry below so a worker never reconstructs a request
    from the mutable ``VideoTrackerJob.prompt`` column.
    """

    job_kind: JobKind
    model_key: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    direction: Direction
    from_frame: Index
    to_frame: Index
    grid_step: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]
    correction_frame: Index | None
    segment_id: UUID | None
    track_id_snapshot: Annotated[StrictStr, Field(max_length=64)] | None
    target_class_name: Annotated[StrictStr, Field(max_length=100)] | None
    target_tool_unit_id: Annotated[StrictStr, Field(max_length=30)] | None
    prompt_snapshot: dict[str, Any]
    source_annotations: Annotated[
        tuple[TrackerSourceAnnotationSnapshot, ...], Field(max_length=256)
    ]
    correction_mask: TrackerCorrectionMaskSnapshot | None

    @model_validator(mode="after")
    def valid_execution_shape(self):
        if self.from_frame > self.to_frame:
            raise ValueError("tracker range must be ordered")
        if self.job_kind == "tracking":
            if self.correction_frame is not None or self.correction_mask is not None:
                raise ValueError("tracking snapshot cannot include a correction mask")
        elif (
            self.correction_frame is None
            or self.correction_mask is None
            or not self.from_frame <= self.correction_frame <= self.to_frame
            or self.correction_mask.frame_index != self.correction_frame
            or self.track_id_snapshot is None
        ):
            raise ValueError("correction snapshot omitted its exact correction frame")
        # Reject non-JSON and non-finite nested values before an operation can
        # serialize the request into PostgreSQL or an HTTP envelope.
        _json_bytes(self.prompt_snapshot)
        for source in self.source_annotations:
            _json_bytes(source.geometry)
        if self.correction_mask is not None:
            _json_bytes(self.correction_mask.mask)
        return self


class TrackerVideoInputScope(Value):
    """Canonical snapshot projected from Labeling's VideoTrackerJob ledger."""

    tracker_job_id: UUID
    origin_actor_id: UUID
    task: TaskVideoBinding
    source_facts: e.VideoCaptureSource
    execution: TrackerExecutionSpec
    request_digest: Digest
    prompt_digest: Digest

    @model_validator(mode="after")
    def scoped_snapshot(self):
        if self.task.task_id is None or self.task.dataset_item_id != (
            self.source_facts.dataset_item_id
        ):
            raise ValueError("tracker snapshot changed its captured Task source")
        if self.source_facts.file_type != "video":
            raise ValueError("tracker snapshot is not a video source")
        if self.execution.job_kind == "correction" and (
            self.execution.correction_mask is None
            or self.execution.correction_mask.annotation_id
            not in {item.annotation_id for item in self.execution.source_annotations}
        ):
            raise ValueError("correction mask is outside captured source annotations")
        return self


class TrackerVideoInputRequest(Value):
    actor_id: UUID
    tracker_job_id: UUID
    grid_step: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]


class TrackerInputReadRequest(Value):
    actor_id: UUID
    tracker_job_id: UUID
    task_id: UUID


class TrackerVideoInputRecord(Value):
    input_id: UUID
    scope: TrackerVideoInputScope
    status: Status
    plan: FrozenFramePlan | None
    error: TrackerErrorCode | None
    created_at: e.AwareDatetime
    retire_after: e.AwareDatetime | None

    @model_validator(mode="after")
    def coherent_input(self):
        if self.plan is not None:
            video = self.plan.input
            if (
                video.input_id != self.input_id
                or video.job_id != self.scope.tracker_job_id
                or video.actor_id != self.scope.origin_actor_id
                or video.task != self.scope.task
                or self.plan.sampling.step != self.scope.execution.grid_step
                or self.status not in {"ready", "expired"}
            ):
                raise ValueError("tracker input changed its exact plan identity")
            if self.scope.execution.to_frame >= video.video.frame_count or (
                self.scope.execution.correction_frame is not None
                and self.scope.execution.correction_frame >= video.video.frame_count
            ):
                raise ValueError("tracker range is outside measured source frames")
            captured = self.scope.source_facts
            source = video.source
            if (
                source.dataset_item_id != captured.dataset_item_id
                or source.dataset_id != captured.dataset_id
                or source.source_generation != captured.source_generation
                or source.source_snapshot != captured.source_snapshot
                or source.timetable_revision != captured.timetable_revision
                or (
                    captured.object_identity is not None
                    and source.object_identity != captured.object_identity
                )
            ):
                raise ValueError("tracker plan changed its captured source")
        if self.status in {"ready", "expired"} and self.plan is None:
            raise ValueError("ready tracker input requires a sealed source plan")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("only failed tracker inputs contain an error")
        return self


class TrackerVideoInputStatus(Value):
    """Sanitized status exposed by the private tracker HTTP endpoint."""

    tracker_job_id: UUID
    input_id: UUID
    origin_actor_id: UUID
    task_id: UUID
    project_id: UUID
    file_type: Literal["video"] = "video"
    execution: TrackerExecutionSpec
    request_digest: Digest
    prompt_digest: Digest
    status: Status
    plan_digest: Digest | None
    timebase_digest: Digest | None
    video: VideoFacts | None
    error: TrackerErrorCode | None
    created_at: e.AwareDatetime
    retire_after: e.AwareDatetime | None

    @model_validator(mode="after")
    def coherent_status(self):
        if self.status in {"ready", "expired"} and (
            self.plan_digest is None
            or self.timebase_digest is None
            or self.video is None
        ):
            raise ValueError("ready tracker status requires sealed video facts")
        if self.video is not None and (
            self.execution.to_frame >= self.video.frame_count
            or (
                self.execution.correction_frame is not None
                and self.execution.correction_frame >= self.video.frame_count
            )
        ):
            raise ValueError("tracker range is outside measured source frames")
        if self.status not in {"ready", "expired"} and any(
            value is not None
            for value in (self.plan_digest, self.timebase_digest, self.video)
        ):
            raise ValueError(
                "unready tracker status cannot assert measured video facts"
            )
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("only failed tracker status contains an error")
        return self

    @classmethod
    def from_record(cls, record: TrackerVideoInputRecord):
        plan = record.plan
        return cls(
            tracker_job_id=record.scope.tracker_job_id,
            input_id=record.input_id,
            origin_actor_id=record.scope.origin_actor_id,
            task_id=record.scope.task.task_id,
            project_id=record.scope.task.project_id,
            execution=record.scope.execution,
            request_digest=record.scope.request_digest,
            prompt_digest=record.scope.prompt_digest,
            status=record.status,
            plan_digest=plan.plan_digest if plan else None,
            timebase_digest=plan.timebase_digest if plan else None,
            video=plan.input.video if plan else None,
            error=record.error,
            created_at=record.created_at,
            retire_after=record.retire_after,
        )


class TrackerVideoObjectVersion(Value):
    """Version identity without exposing the internal storage object key."""

    version_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    etag: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Annotated[StrictInt, Field(gt=0, le=9_007_199_254_740_991)]
    sha256: Digest

    @model_validator(mode="after")
    def real_version(self):
        if not self.version_id.strip() or self.version_id == "null":
            raise ValueError("tracker source requires an exact object version")
        return self


class TrackerVideoInputWork(Value):
    record: TrackerVideoInputRecord
    source_facts: e.VideoCaptureSource

    @model_validator(mode="after")
    def same_capture(self):
        captured = self.record.scope.source_facts
        if (
            self.source_facts.dataset_item_id != captured.dataset_item_id
            or self.source_facts.dataset_id != captured.dataset_id
            or self.source_facts.source_generation != captured.source_generation
            or self.source_facts.source_snapshot != captured.source_snapshot
            or self.source_facts.timetable_revision != captured.timetable_revision
            or self.source_facts.file_path != captured.file_path
            or self.source_facts.file_size != captured.file_size
            or self.source_facts.content_hash != captured.content_hash
            or self.source_facts.file_type != "video"
            or self.source_facts.source_drifted
        ):
            raise ValueError("tracker work changed its accepted current source")
        if self.record.status in {"materializing", "ready", "expired"} and (
            self.source_facts.object_identity is None
        ):
            raise ValueError(
                "materialized tracker work requires a measured source version"
            )
        if (
            captured.object_identity is not None
            and self.source_facts.object_identity != captured.object_identity
        ):
            raise ValueError("tracker work changed its captured object version")
        return self


class TrackerInputRequest(TrackerInputReadRequest):
    input_id: UUID
    request_digest: Digest


class PrepareTrackerInputRequest(TrackerInputRequest):
    source_snapshot: Digest | None
    timetable_revision: Count | None
    object_identity: ObjectIdentity | None
    terminal_error: TerminalMemberError | None

    @model_validator(mode="after")
    def one_observation(self):
        if (self.object_identity is None) == (self.terminal_error is None):
            raise ValueError("provide one tracker input observation")
        if self.object_identity is not None and (
            self.source_snapshot is None or self.timetable_revision is None
        ):
            raise ValueError("source observation requires source and timebase digests")
        return self


class VerifyTrackerInputRequest(TrackerInputRequest):
    plan_digest: Digest


class TrackerSourceReferenceRequest(VerifyTrackerInputRequest):
    pass


class TrackerVideoSourceMaterial(Value):
    """Native, exact-version facts before the Assets control service signs URL."""

    input: TrackerVideoInputRecord
    source_facts: e.VideoCaptureSource

    @model_validator(mode="after")
    def ready_exact_source(self):
        if (
            self.input.status != "ready"
            or self.input.plan is None
            or self.input.plan.input.blob.version_id in {"", "null"}
            or self.source_facts.object_identity is None
            or self.source_facts.object_identity
            != self.input.plan.input.source.object_identity
        ):
            raise ValueError("tracker reference requires the retained exact source")
        return self


class TrackerVideoSourceReference(Value):
    """Private HTTP result used only by the tracker inference executor."""

    input: TrackerVideoInputStatus
    source_version: TrackerVideoObjectVersion
    url: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    expires_in: _URL_EXPIRY

    @model_validator(mode="after")
    def exact_requested_blob(self):
        if self.input.status != "ready" or self.input.plan_digest is None:
            raise ValueError("tracker source reference requires a ready input")
        return self


class TrackerVideoInputEvidence(Value):
    """Source-level tracker provenance; deliberately has no frame identifier."""

    protocol_schema: Literal["aap-assets-video-tracker-input-evidence/v1"] = Field(
        default="aap-assets-video-tracker-input-evidence/v1",
        alias="schema",
        serialization_alias="schema",
    )
    tracker_job_id: UUID
    input_id: UUID
    task_id: UUID
    project_id: UUID
    request_digest: Digest
    prompt_digest: Digest
    plan_digest: Digest
    timebase_digest: Digest
    source_version_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    source_sha256: Digest
    from_frame: Index
    to_frame: Index
    direction: Direction
    grid_step: Revision

    @model_validator(mode="after")
    def exact_scope(self):
        if self.from_frame > self.to_frame:
            raise ValueError("tracker evidence range must be ordered")
        if not self.source_version_id.strip() or self.source_version_id == "null":
            raise ValueError("tracker evidence requires an exact source version")
        return self


class LockTrackerVideoInputRequest(TrackerSourceReferenceRequest):
    pass


class TrackerVideoInputAcceptance(Value):
    input: TrackerVideoInputRecord
    evidence: TrackerVideoInputEvidence

    @model_validator(mode="after")
    def evidence_matches(self):
        plan = self.input.plan
        if plan is None:
            raise ValueError("accepted tracker evidence requires a ready input")
        evidence = self.evidence
        if (
            evidence.input_id != self.input.input_id
            or evidence.tracker_job_id != self.input.scope.tracker_job_id
            or evidence.task_id != self.input.scope.task.task_id
            or evidence.project_id != self.input.scope.task.project_id
            or evidence.request_digest != self.input.scope.request_digest
            or evidence.prompt_digest != self.input.scope.prompt_digest
            or evidence.plan_digest != plan.plan_digest
            or evidence.timebase_digest != plan.timebase_digest
            or evidence.source_sha256 != plan.input.blob.sha256
            or evidence.source_version_id != plan.input.blob.version_id
            or evidence.from_frame != self.input.scope.execution.from_frame
            or evidence.to_frame != self.input.scope.execution.to_frame
            or evidence.direction != self.input.scope.execution.direction
            or evidence.grid_step != self.input.scope.execution.grid_step
        ):
            raise ValueError("tracker evidence changed its immutable accepted scope")
        return self


class AttachTrackerVideoInputSourceRequest(Value):
    actor_id: UUID
    tracker_job_id: UUID
    annotation_id: UUID
    evidence: TrackerVideoInputEvidence


class TrackerVideoInputPin(Value):
    annotation_id: UUID
    tracker_job_id: UUID
    input_id: UUID
    plan_digest: Digest
    attached: StrictBool


class CopyTrackerVideoInputSourcesRequest(Value):
    actor_id: UUID
    source_annotation_id: UUID
    target_annotation_id: UUID


class TrackerVideoInputPins(Value):
    source_annotation_id: UUID
    target_annotation_id: UUID
    copied: Count


class TrackerVideoInputReferenceRequest(Value):
    input_id: UUID


class TrackerVideoInputReferenceStatus(Value):
    input_id: UUID
    referenced: StrictBool
    retained_until: e.AwareDatetime | None


class _Error(Value):
    code: TrackerErrorCode


@dataclass(frozen=True)
class NativeOperation:
    namespace: Literal["aap_assets_v1", "aap_labeling_v1"]
    request: type[Value]
    result: type[Value]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["control", "worker", "labeling", "cleanup"]


def _operation(
    namespace: Literal["aap_assets_v1", "aap_labeling_v1"],
    request: type[Value],
    result: type[Value],
    arguments: tuple[str, ...],
    sql_types: tuple[str, ...],
    role: Literal["control", "worker", "labeling", "cleanup"],
) -> NativeOperation:
    return NativeOperation(namespace, request, result, arguments, sql_types, role)


TRACKER_OPERATIONS: dict[str, NativeOperation] = {
    "capture_video_tracker_input": _operation(
        "aap_labeling_v1",
        TrackerVideoInputRequest,
        TrackerVideoInputRecord,
        ("actor_id", "tracker_job_id", "grid_step"),
        ("uuid", "uuid", "integer"),
        "labeling",
    ),
    "read_video_tracker_input": _operation(
        "aap_assets_v1",
        TrackerInputReadRequest,
        TrackerVideoInputWork,
        ("actor_id", "tracker_job_id", "task_id"),
        ("uuid", "uuid", "uuid"),
        "control",
    ),
    "prepare_video_tracker_input": _operation(
        "aap_assets_v1",
        PrepareTrackerInputRequest,
        TrackerVideoInputRecord,
        (
            "actor_id",
            "tracker_job_id",
            "task_id",
            "input_id",
            "request_digest",
            "source_snapshot",
            "timetable_revision",
            "object_identity",
            "terminal_error",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text", "bigint", "jsonb", "text"),
        "control",
    ),
    "reference_video_tracker_source": _operation(
        "aap_assets_v1",
        TrackerSourceReferenceRequest,
        TrackerVideoSourceMaterial,
        (
            "actor_id",
            "tracker_job_id",
            "task_id",
            "input_id",
            "request_digest",
            "plan_digest",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text"),
        "control",
    ),
    "claim_video_tracker_input": _operation(
        "aap_assets_v1",
        e.ClaimVideoInputRequest,
        e.ClaimedVideoInput,
        ("worker_id", "input_id", "lease_seconds"),
        ("uuid", "uuid", "integer"),
        "worker",
    ),
    "heartbeat_video_tracker_input": _operation(
        "aap_assets_v1",
        e.HeartbeatVideoInputRequest,
        e.VideoInputLease,
        ("input_id", "generation", "lease_token", "lease_seconds"),
        ("uuid", "bigint", "uuid", "integer"),
        "worker",
    ),
    "stage_video_tracker_times": _operation(
        "aap_assets_v1",
        e.StageVideoTimesRequest,
        e.StagedVideoTimes,
        ("input_id", "generation", "lease_token", "cursor", "entries"),
        ("uuid", "bigint", "uuid", "bigint", "jsonb"),
        "worker",
    ),
    "finish_video_tracker_input": _operation(
        "aap_assets_v1",
        e.FinishVideoInputRequest,
        e.VideoInputRecord,
        ("input_id", "generation", "lease_token", "plan"),
        ("uuid", "bigint", "uuid", "jsonb"),
        "worker",
    ),
    "fail_video_tracker_input": _operation(
        "aap_assets_v1",
        e.FailVideoInputRequest,
        e.VideoInputRecord,
        ("input_id", "generation", "lease_token", "error_code"),
        ("uuid", "bigint", "uuid", "text"),
        "worker",
    ),
    "sweep_video_tracker_inputs": _operation(
        "aap_assets_v1",
        e.SweepVideoInputsRequest,
        e.SweptVideoInputs,
        ("limit",),
        ("integer",),
        "worker",
    ),
    "lock_video_tracker_input_for_accept": _operation(
        "aap_labeling_v1",
        LockTrackerVideoInputRequest,
        TrackerVideoInputAcceptance,
        (
            "actor_id",
            "tracker_job_id",
            "task_id",
            "input_id",
            "request_digest",
            "plan_digest",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text"),
        "labeling",
    ),
    "lock_video_tracker_input_for_stage": _operation(
        "aap_labeling_v1",
        LockTrackerVideoInputRequest,
        TrackerVideoInputAcceptance,
        (
            "actor_id",
            "tracker_job_id",
            "task_id",
            "input_id",
            "request_digest",
            "plan_digest",
        ),
        ("uuid", "uuid", "uuid", "uuid", "text", "text"),
        "labeling",
    ),
    "attach_annotation_video_tracker_input_source": _operation(
        "aap_labeling_v1",
        AttachTrackerVideoInputSourceRequest,
        TrackerVideoInputPin,
        ("actor_id", "tracker_job_id", "annotation_id", "evidence"),
        ("uuid", "uuid", "uuid", "jsonb"),
        "labeling",
    ),
    "copy_annotation_video_tracker_input_sources": _operation(
        "aap_labeling_v1",
        CopyTrackerVideoInputSourcesRequest,
        TrackerVideoInputPins,
        ("actor_id", "source_annotation_id", "target_annotation_id"),
        ("uuid", "uuid", "uuid"),
        "labeling",
    ),
    "video_tracker_input_referenced": _operation(
        "aap_labeling_v1",
        TrackerVideoInputReferenceRequest,
        TrackerVideoInputReferenceStatus,
        ("input_id",),
        ("uuid",),
        "cleanup",
    ),
}

LABELING_OPERATIONS = {
    name: operation
    for name, operation in TRACKER_OPERATIONS.items()
    if operation.namespace == "aap_labeling_v1"
}
ASSETS_OPERATIONS = {
    name: operation
    for name, operation in TRACKER_OPERATIONS.items()
    if operation.namespace == "aap_assets_v1"
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def parse_native(operation: str, request: Value, raw: Any) -> Value:
    """Parse one bounded reply from either native namespace."""
    definition = TRACKER_OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TrackerFailure("invalid_request")
    try:
        if isinstance(raw, (str, bytes)):
            payload = raw.encode("utf-8") if isinstance(raw, str) else raw
            if len(payload) > REPLY_BYTES:
                raise ValueError("oversized tracker reply")
            raw = json.loads(payload)
        elif len(_canonical_bytes(raw)) > REPLY_BYTES:
            raise ValueError("oversized tracker reply")
        schema = (
            LABELING_SCHEMA
            if definition.namespace == "aap_labeling_v1"
            else TRACKER_SCHEMA
        )
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != schema
            or type(raw.get("ok")) is not bool
        ):
            raise ValueError("invalid tracker envelope")
        if raw["ok"] is False and set(raw) == {"schema", "ok", "error"}:
            raise TrackerFailure(_Error.model_validate(raw["error"]).code)
        if raw["ok"] is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid tracker success envelope")
        result = definition.result.model_validate(raw["data"])
        require_scope(operation, request, result)
        return result
    except TrackerFailure:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        ValidationError,
    ):
        raise TrackerFailure("invalid_response") from None


def parse_labeling_native(operation: str, request: Value, raw: Any) -> Value:
    definition = LABELING_OPERATIONS.get(operation)
    if definition is None:
        raise TrackerFailure("invalid_request")
    return parse_native(operation, request, raw)


def require_scope(operation: str, request: Value, result: Value) -> None:
    record = getattr(result, "record", None) or getattr(result, "input", None)
    if isinstance(result, TrackerVideoInputRecord):
        record = result
    if isinstance(result, TrackerVideoInputWork):
        record = result.record
    if isinstance(result, e.ClaimedVideoInput):
        claim = result.claim
        if claim is None:
            if not isinstance(request, e.ClaimVideoInputRequest):
                raise ValueError("empty tracker claim escaped worker request")
            return
        # The shared worker contract deliberately returns execution.VideoInputRecord,
        # whose scope is represented by the canonical tracker ledger rather than
        # duplicated into the Asset-owned receipt DTO.
        if request.input_id is not None and claim.input.input_id != request.input_id:
            raise ValueError("tracker worker claim changed its requested input")
        return
    if isinstance(result, TrackerVideoSourceMaterial):
        record = result.input
    if isinstance(result, TrackerVideoInputAcceptance):
        record = result.input
    if isinstance(result, TrackerVideoInputStatus):
        if isinstance(
            request, (TrackerVideoInputRequest, TrackerInputReadRequest)
        ) and (
            result.tracker_job_id != request.tracker_job_id
            or result.task_id != getattr(request, "task_id", result.task_id)
        ):
            raise ValueError("tracker status escaped its canonical Job")
    if isinstance(result, TrackerVideoSourceReference) and isinstance(
        request, TrackerSourceReferenceRequest
    ):
        if (
            result.input.tracker_job_id != request.tracker_job_id
            or result.input.input_id != request.input_id
            or result.input.task_id != request.task_id
            or result.input.request_digest != request.request_digest
            or result.input.plan_digest != request.plan_digest
        ):
            raise ValueError("tracker source reference escaped its accepted scope")
        return
    if record is not None:
        scope = record.scope
        if isinstance(
            request, (TrackerVideoInputRequest, TrackerInputReadRequest)
        ) and (
            scope.tracker_job_id != request.tracker_job_id
            or scope.task.task_id != getattr(request, "task_id", scope.task.task_id)
        ):
            raise ValueError("tracker input reply escaped its canonical Job")
        if isinstance(request, TrackerVideoInputRequest) and (
            scope.origin_actor_id != request.actor_id
            or scope.execution.grid_step != request.grid_step
        ):
            raise ValueError("tracker capture changed its accepted actor or grid")
        if isinstance(request, TrackerInputReadRequest) and (
            scope.tracker_job_id != request.tracker_job_id
            or scope.task.task_id != request.task_id
        ):
            raise ValueError("tracker input read changed its Task scope")
        if isinstance(request, TrackerInputRequest) and (
            record.input_id != request.input_id
            or scope.tracker_job_id != request.tracker_job_id
            or scope.task.task_id != request.task_id
            or scope.request_digest != request.request_digest
        ):
            raise ValueError("tracker input reply changed its accepted request")
    if isinstance(result, e.VideoInputRecord):
        if (
            isinstance(request, (e.FinishVideoInputRequest, e.FailVideoInputRequest))
            and result.input_id != request.input_id
        ):
            raise ValueError("tracker write reply changed its input")
        if isinstance(request, e.FinishVideoInputRequest) and (
            result.plan is None or result.plan.plan_digest != request.plan.plan_digest
        ):
            raise ValueError("tracker finish reply changed its sealed plan")
    if isinstance(result, e.VideoInputLease) and isinstance(
        request, e.VideoInputLeaseRequest
    ):
        if (
            result.input_id != request.input_id
            or result.generation != request.generation
            or result.lease_token != request.lease_token
        ):
            raise ValueError("tracker lease reply changed its fencing token")
    if isinstance(result, e.StagedVideoTimes) and isinstance(
        request, e.StageVideoTimesRequest
    ):
        if (
            result.input_id != request.input_id
            or result.generation != request.generation
            or result.lease_token != request.lease_token
            or result.next_cursor != request.cursor + len(request.entries)
        ):
            raise ValueError("tracker staged page changed its cursor or lease")
    if isinstance(result, TrackerVideoInputAcceptance) and isinstance(
        request, LockTrackerVideoInputRequest
    ):
        if (
            result.input.input_id != request.input_id
            or result.input.scope.tracker_job_id != request.tracker_job_id
            or result.input.scope.task.task_id != request.task_id
            or result.input.scope.request_digest != request.request_digest
            or result.input.plan is None
            or result.input.plan.plan_digest != request.plan_digest
        ):
            raise ValueError("tracker acceptance lock changed its request")
    if isinstance(result, TrackerVideoInputPin) and isinstance(
        request, AttachTrackerVideoInputSourceRequest
    ):
        if (
            result.annotation_id != request.annotation_id
            or result.tracker_job_id != request.tracker_job_id
            or result.input_id != request.evidence.input_id
            or result.plan_digest != request.evidence.plan_digest
        ):
            raise ValueError("tracker annotation pin changed its evidence")
    if isinstance(result, TrackerVideoInputPins) and isinstance(
        request, CopyTrackerVideoInputSourcesRequest
    ):
        if (
            result.source_annotation_id != request.source_annotation_id
            or result.target_annotation_id != request.target_annotation_id
        ):
            raise ValueError("tracker annotation copy changed its target")
    if (
        isinstance(result, TrackerVideoInputReferenceStatus)
        and isinstance(request, TrackerVideoInputReferenceRequest)
        and result.input_id != request.input_id
    ):
        raise ValueError("tracker retention reply changed its input")


def prepare_native_call(
    operation: str, request: Value, *, allowed_roles: tuple[str, ...] | set[str]
) -> tuple[str, tuple[Any, ...]]:
    definition = TRACKER_OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid tracker operation/request pair")
    if definition.role not in allowed_roles:
        raise PermissionError("tracker operation is outside this SQL login scope")
    document = request.model_dump(mode="json", by_alias=True)
    if len(_canonical_bytes(document)) > REQUEST_BYTES:
        raise TrackerFailure("resource_budget_exceeded")
    arguments: list[Any] = []
    placeholders: list[str] = []
    for index, (name, sql_type) in enumerate(
        zip(definition.arguments, definition.sql_types, strict=True), start=1
    ):
        value = getattr(request, name)
        if value is not None and sql_type == "jsonb":
            value = _canonical_bytes(document[name]).decode("utf-8")
        arguments.append(value)
        placeholders.append(f"${index}::{sql_type}")
    return (
        f"SELECT {definition.namespace}.{operation}({', '.join(placeholders)})",
        tuple(arguments),
    )


def json_bytes(value: Any) -> bytes:
    if isinstance(value, Value):
        value = value.model_dump(mode="json", by_alias=True)
    return _canonical_bytes(value)


def contract_document() -> dict[str, Any]:
    models = {
        operation.request.__name__: operation.request
        for operation in TRACKER_OPERATIONS.values()
    }
    models.update(
        {
            operation.result.__name__: operation.result
            for operation in TRACKER_OPERATIONS.values()
        }
    )
    for model in (
        TrackerExecutionSpec,
        TrackerSourceAnnotationSnapshot,
        TrackerCorrectionMaskSnapshot,
        TrackerVideoInputScope,
        TrackerVideoInputRecord,
        TrackerVideoInputStatus,
        TrackerVideoObjectVersion,
        TrackerVideoInputWork,
        TrackerVideoSourceMaterial,
        TrackerVideoSourceReference,
        TrackerVideoInputEvidence,
        TrackerVideoInputAcceptance,
        TrackerVideoInputPin,
        TrackerVideoInputPins,
        TrackerVideoInputReferenceStatus,
        MediaVideoCollaborationPolicy,
    ):
        models[model.__name__] = model
    definitions: dict[str, Any] = {}
    for model in models.values():
        shape = model.model_json_schema(ref_template="#/$defs/{model}")
        nested = shape.pop("$defs", {})
        for name, definition in nested.items():
            if name in definitions and definitions[name] != definition:
                raise ValueError(f"conflicting tracker schema definition {name}")
            definitions[name] = definition
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError(f"conflicting tracker DTO {model.__name__}")
        definitions[model.__name__] = shape
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": TRACKER_SCHEMA,
        "$defs": definitions,
        "oneOf": [
            {"$ref": "#/$defs/TrackerVideoInputRecord"},
            {"$ref": "#/$defs/TrackerVideoInputEvidence"},
        ],
        "x-native": {
            name: {
                "namespace": operation.namespace,
                "signature": f"{operation.namespace}.{name}({','.join(operation.sql_types)})",
                "arguments": list(operation.arguments),
                "request": operation.request.__name__,
                "result": operation.result.__name__,
                "role": operation.role,
                "owner": "labeling"
                if operation.namespace == "aap_labeling_v1"
                else "assets",
                "private": True,
            }
            for name, operation in TRACKER_OPERATIONS.items()
        },
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "timebase_page_size": e.PAGE_SIZE,
            "url_expiry_seconds": URL_EXPIRY_SECONDS,
        },
        "x-workspace-projections": {
            "read_media_video_collaboration_policy": {
                "signature": "aap_workspace_v1.read_media_video_collaboration_policy(uuid)",
                "schema": WORKSPACE_POLICY_SCHEMA,
                "result": "MediaVideoCollaborationPolicy",
                "owner": "workspace",
                "caller": "labeling",
                "requires": "current project access locked in the same transaction",
            }
        },
    }
