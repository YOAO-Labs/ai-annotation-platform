"""Retained video manifest, materialization and frame-owner capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from aap_assets_media.contract import ErrorCode as MediaErrorCode

from .authority import ErrorCode as AuthorityErrorCode, VideoSelection, json_bytes
from .values import (
    Count,
    Digest,
    FrameBlob,
    FramePageRequest,
    FrameSampling,
    FrameTimeEntry,
    FrameTimePage,
    FrozenFramePlan,
    ObjectIdentity,
    PAGE_SIZE,
    REPLY_BYTES,
    REQUEST_BYTES,
    Revision,
    TaskVideoBinding,
    Value,
    VideoSourceSnapshot,
    require_page_scope,
)

EXECUTION_SCHEMA = "aap-assets-video-input-execution/v1"
ErrorCode = Literal[
    AuthorityErrorCode,
    MediaErrorCode,
    "source_not_ready",
    "input_missing",
    "input_scope_mismatch",
    "input_not_ready",
    "input_expired",
    "input_corrupt",
    "input_busy",
    "manifest_missing",
    "manifest_busy",
    "manifest_not_ready",
    "manifest_cursor_invalid",
    "task_scope_changed",
    "unsupported_media",
    "timebase_changed",
    "timebase_incomplete",
    "frame_missing",
    "frame_not_ready",
    "frame_busy",
    "versioning_required",
    "resource_limit",
    "timeout",
    "processing_failed",
    "reference_busy",
]
TerminalMemberError = Literal[
    "source_missing",
    "source_changed",
    "source_not_ready",
    "not_video",
    "task_scope_changed",
    "timebase_changed",
    "input_expired",
    "input_corrupt",
    "resource_budget_exceeded",
]
Status = Literal["pending", "materializing", "ready", "failed", "expired"]
PageLimit = Annotated[StrictInt, Field(ge=1, le=PAGE_SIZE)]
LeaseSeconds = Annotated[StrictInt, Field(ge=60, le=7200)]


class VideoInputFailure(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)


def attempt_prefix(input_id: UUID, generation: int, lease_token: UUID) -> str:
    return f"accepted-video-inputs/{input_id}/g{generation}/a{lease_token}/"


class CapturedVideoTask(Value):
    """Raw capture can retain a missing link as a fixed member failure."""

    task_id: UUID
    project_id: UUID
    batch_id: UUID | None
    task_version: Revision
    task_source_generation: Revision
    dataset_item_id: UUID | None
    task_source_snapshot: Digest
    file_path: Annotated[StrictStr, Field(max_length=1024)]
    file_type: Literal["video"]

    def binding(self) -> TaskVideoBinding:
        return TaskVideoBinding.model_validate(self.model_dump(exclude={"batch_id"}))


class VideoCaptureSource(Value):
    """Only the Assets facts needed to retain the admitted original bytes."""

    dataset_item_id: UUID
    dataset_id: UUID
    source_generation: Revision
    source_snapshot: Digest
    timetable_revision: Count
    object_identity: ObjectIdentity | None
    source_drifted: StrictBool
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1024)]
    file_type: Literal["video"]
    file_size: Count | None
    content_hash: Annotated[StrictStr, Field(max_length=255)] | None


class VideoManifestScope(Value):
    job_id: UUID
    actor_id: UUID
    project_id: UUID


class CaptureVideoManifestRequest(VideoManifestScope):
    selection: VideoSelection

    @model_validator(mode="after")
    def workbench_scope(self):
        _require_workbench_selection(self.selection)
        return self


def _require_workbench_selection(selection: VideoSelection):
    if selection.execution_scope == "workbench" and selection.batch_id is not None:
        raise ValueError("Workbench video admission cannot select a batch")


class VideoManifestHeader(VideoManifestScope):
    """actor_id attributes admission; read requests carry the current actor."""

    selection: VideoSelection
    selection_digest: Digest
    candidate_count: Count
    selected_count: Count
    prepared_count: Count
    status: Literal["preparing", "sealed"]
    ready_count: Count | None
    failed_count: Count | None
    sealed_digest: Digest | None
    created_at: AwareDatetime
    sealed_at: AwareDatetime | None

    @model_validator(mode="after")
    def complete_barrier(self):
        _require_workbench_selection(self.selection)
        if self.selection.task_ids is not None and self.candidate_count != len(
            self.selection.task_ids
        ):
            raise ValueError("video manifest omitted explicit candidate membership")
        if self.selection.predict_mode != "skip_predicted" and (
            self.selected_count != self.candidate_count
        ):
            raise ValueError("video manifest omitted selected candidate membership")
        if not self.prepared_count <= self.selected_count <= self.candidate_count:
            raise ValueError("video manifest counts exceed captured membership")
        if self.status == "preparing":
            if any(
                value is not None
                for value in (
                    self.ready_count,
                    self.failed_count,
                    self.sealed_digest,
                    self.sealed_at,
                )
            ):
                raise ValueError("preparing manifest cannot assert sealed outcomes")
        elif (
            self.prepared_count != self.selected_count
            or self.ready_count is None
            or self.failed_count is None
            or self.ready_count + self.failed_count != self.selected_count
            or self.sealed_digest is None
            or self.sealed_at is None
            or self.sealed_at < self.created_at
        ):
            raise ValueError("sealed video manifest must account for every member")
        return self


class VideoInputRecord(Value):
    input_id: UUID
    job_id: UUID
    actor_id: UUID
    task: TaskVideoBinding
    source: VideoSourceSnapshot
    sampling: FrameSampling
    status: Status
    plan: FrozenFramePlan | None
    error: ErrorCode | None
    created_at: AwareDatetime
    retire_after: AwareDatetime | None

    @model_validator(mode="after")
    def coherent_input(self):
        if self.task.dataset_item_id != self.source.dataset_item_id:
            raise ValueError("video input changed the captured Task link")
        if self.plan is not None:
            video = self.plan.input
            if (
                video.input_id != self.input_id
                or video.job_id != self.job_id
                or video.actor_id != self.actor_id
                or video.task != self.task
                or video.source != self.source
                or video.created_at != self.created_at
                or self.plan.sampling != self.sampling
                or self.status not in {"ready", "expired"}
            ):
                raise ValueError("input record changed its retained plan identity")
        if self.status in {"ready", "expired"} and self.plan is None:
            raise ValueError("ready video requires a complete retained frame plan")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("only failed video inputs contain an error")
        return self


def _source_matches(source: VideoCaptureSource, expected: VideoSourceSnapshot) -> bool:
    return (
        source.dataset_item_id == expected.dataset_item_id
        and source.dataset_id == expected.dataset_id
        and source.source_generation == expected.source_generation
        and source.source_snapshot == expected.source_snapshot
        and source.timetable_revision == expected.timetable_revision
        and not source.source_drifted
        and source.file_type == "video"
        and (
            source.object_identity is None
            or source.object_identity.model_dump()
            == expected.object_identity.model_dump()
        )
    )


class VideoManifestMember(Value):
    ordinal: Revision
    task: CapturedVideoTask
    source: VideoCaptureSource | None
    state: Literal["unprepared", "preparing", "ready", "failed"]
    input: VideoInputRecord | None
    error: ErrorCode | None

    @model_validator(mode="after")
    def original_member(self):
        if self.source is not None and (
            self.source.dataset_item_id != self.task.dataset_item_id
            or self.source.file_type != "video"
        ):
            raise ValueError("video member changed its captured source")
        if self.input is not None and (
            self.input.task != self.task.binding()
            or self.source is None
            or not _source_matches(self.source, self.input.source)
        ):
            raise ValueError("video member changed its original input")
        if self.state == "unprepared":
            if (
                self.source is None
                or self.source.source_drifted
                or self.input is not None
                or self.error is not None
            ):
                raise ValueError("unprepared member requires only its captured source")
        elif self.state == "preparing":
            if (
                self.input is None
                or self.input.status not in {"pending", "materializing"}
                or self.error is not None
            ):
                raise ValueError("preparing member requires pending original bytes")
        elif self.state == "ready":
            if self.input is None or self.input.plan is None or self.error is not None:
                raise ValueError("ready member requires a sealed original frame plan")
        elif self.error is None or (
            self.input is not None
            and (self.input.status != "failed" or self.input.error != self.error)
        ):
            raise ValueError("failed member requires a coherent fixed input error")
        return self


def _member_scope(header: VideoManifestHeader, member: VideoManifestMember):
    if member.task.project_id != header.project_id or (
        member.input is not None
        and (
            member.input.job_id != header.job_id
            or member.input.actor_id != header.actor_id
        )
    ):
        raise ValueError("video member escaped its admitted scope")
    if (
        header.selection.task_ids is not None
        and member.task.task_id not in header.selection.task_ids
    ) or (
        header.selection.batch_id is not None
        and member.task.batch_id != header.selection.batch_id
    ):
        raise ValueError("video member escaped the captured selector")
    if member.input is not None and member.input.sampling != header.selection.sampling:
        raise ValueError("video member changed the captured sampling policy")
    if (
        member.input is not None
        and member.input.status == "expired"
        and header.status != "sealed"
    ):
        raise ValueError("unsealed video preparation lost its retained input")


class VideoManifestPageRequest(VideoManifestScope):
    selection_digest: Digest
    sealed_digest: Digest | None = None
    cursor: Count = 0
    page_size: PageLimit = PAGE_SIZE


class VideoManifestPage(Value):
    manifest: VideoManifestHeader
    cursor: Count
    next_cursor: Count | None
    members: Annotated[tuple[VideoManifestMember, ...], Field(max_length=PAGE_SIZE)]

    @model_validator(mode="after")
    def complete_page(self):
        count = self.manifest.selected_count
        if self.cursor > count or (self.cursor < count and not self.members):
            raise ValueError("video member page omitted remaining membership")
        if len({m.task.task_id for m in self.members}) != len(self.members):
            raise ValueError("video member page repeated a Task")
        for index, member in enumerate(self.members, start=self.cursor + 1):
            if member.ordinal != index or index > count:
                raise ValueError("video members are unordered or outside the manifest")
            _member_scope(self.manifest, member)
            if self.manifest.status == "sealed" and member.state not in {
                "ready",
                "failed",
            }:
                raise ValueError("sealed manifest contains unfinished preparation")
        end = self.cursor + len(self.members)
        if self.next_cursor != (end if end < count else None):
            raise ValueError("video member cursor does not describe progress")
        return self


class PrepareVideoMemberRequest(VideoManifestScope):
    selection_digest: Digest
    ordinal: Revision
    task_id: UUID


class ObserveVideoMemberRequest(PrepareVideoMemberRequest):
    source_snapshot: Digest | None
    timetable_revision: Count | None
    object_identity: ObjectIdentity | None
    terminal_error: TerminalMemberError | None

    @model_validator(mode="after")
    def one_observation(self):
        if (self.object_identity is None) == (self.terminal_error is None):
            raise ValueError("provide an observed source or a terminal member error")
        if self.object_identity is not None and (
            self.source_snapshot is None or self.timetable_revision is None
        ):
            raise ValueError("video observation requires source and timebase identity")
        return self


class VideoMemberReceipt(Value):
    manifest: VideoManifestHeader
    member: VideoManifestMember

    @model_validator(mode="after")
    def original_scope(self):
        _member_scope(self.manifest, self.member)
        return self


class SealVideoManifestRequest(VideoManifestScope):
    selection_digest: Digest


class ReadVideoInputRequest(Value):
    input_id: UUID
    job_id: UUID
    task_id: UUID
    actor_id: UUID


class VideoInputWork(Value):
    input: VideoInputRecord
    source: VideoCaptureSource

    @model_validator(mode="after")
    def accepted_work(self):
        if self.source.object_identity is None or not _source_matches(
            self.source, self.input.source
        ):
            raise ValueError("video work changed its observed original source")
        return self


class ClaimVideoInputRequest(Value):
    worker_id: UUID
    input_id: UUID | None = None
    lease_seconds: LeaseSeconds = 2100


class VideoInputClaim(VideoInputWork):
    generation: Revision
    lease_token: UUID
    lease_expires_at: AwareDatetime
    blob_key: StrictStr

    @model_validator(mode="after")
    def exact_attempt(self):
        if self.input.status != "materializing" or self.blob_key != (
            attempt_prefix(self.input.input_id, self.generation, self.lease_token)
            + "source"
        ):
            raise ValueError("video claim escaped its writer attempt")
        return self


class ClaimedVideoInput(Value):
    claim: VideoInputClaim | None


class VideoInputLeaseRequest(Value):
    input_id: UUID
    generation: Revision
    lease_token: UUID


class VideoInputLease(VideoInputLeaseRequest):
    lease_expires_at: AwareDatetime


class HeartbeatVideoInputRequest(VideoInputLeaseRequest):
    lease_seconds: LeaseSeconds = 2100


class StageVideoTimesRequest(VideoInputLeaseRequest):
    cursor: Count
    entries: Annotated[
        tuple[FrameTimeEntry, ...], Field(min_length=1, max_length=PAGE_SIZE)
    ]

    @model_validator(mode="after")
    def contiguous_entries(self):
        for offset, entry in enumerate(self.entries, start=self.cursor + 1):
            if entry.ordinal != offset:
                raise ValueError("video time staging changed page progress")
        return self


class StagedVideoTimes(VideoInputLeaseRequest):
    next_cursor: Count


class FinishVideoInputRequest(VideoInputLeaseRequest):
    plan: FrozenFramePlan

    @model_validator(mode="after")
    def exact_original(self):
        if self.plan.input.input_id != self.input_id or self.plan.input.blob.key != (
            attempt_prefix(self.input_id, self.generation, self.lease_token) + "source"
        ):
            raise ValueError("video publication escaped its writer attempt")
        return self


class FailVideoInputRequest(VideoInputLeaseRequest):
    error_code: ErrorCode


class VideoFrameRequest(Value):
    actor_id: UUID
    job_id: UUID
    task_id: UUID
    input_id: UUID
    plan_digest: Digest
    ordinal: Revision


class VideoFrameRecord(Value):
    frame_id: UUID
    plan: FrozenFramePlan
    entry: FrameTimeEntry
    status: Status
    blob: FrameBlob | None
    error: ErrorCode | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def exact_frame(self):
        if self.entry.ordinal > self.plan.selected_count or self.entry.frame_index != (
            (self.entry.ordinal - 1) * self.plan.sampling.step
        ):
            raise ValueError("video frame is outside its sealed plan")
        if self.blob is not None:
            suffix = (
                f"frames/{self.entry.frame_index}/w{self.plan.sampling.width}"
                f"-{self.plan.sampling.format}"
            )
            if (
                self.status not in {"ready", "expired"}
                or not self.blob.key.startswith(
                    f"accepted-video-inputs/{self.plan.input.input_id}/"
                )
                or not self.blob.key.endswith(suffix)
            ):
                raise ValueError("video frame changed its input or rendition")
        if self.status == "ready" and self.blob is None:
            raise ValueError("ready video frame requires exact retained bytes")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("only failed video frames contain an error")
        return self


class ClaimVideoFrameRequest(Value):
    worker_id: UUID
    frame_id: UUID | None = None
    lease_seconds: LeaseSeconds = 2100


class VideoFrameLeaseRequest(Value):
    frame_id: UUID
    generation: Revision
    lease_token: UUID


class VideoFrameClaim(Value):
    frame: VideoFrameRecord
    generation: Revision
    lease_token: UUID
    lease_expires_at: AwareDatetime
    blob_key: StrictStr

    @model_validator(mode="after")
    def exact_attempt(self):
        frame = self.frame
        if frame.status != "materializing" or self.blob_key != (
            attempt_prefix(frame.plan.input.input_id, self.generation, self.lease_token)
            + f"frames/{frame.entry.frame_index}/w{frame.plan.sampling.width}"
            + f"-{frame.plan.sampling.format}"
        ):
            raise ValueError("frame claim escaped its writer attempt")
        return self


class ClaimedVideoFrame(Value):
    claim: VideoFrameClaim | None


class VideoFrameLease(VideoFrameLeaseRequest):
    lease_expires_at: AwareDatetime


class HeartbeatVideoFrameRequest(VideoFrameLeaseRequest):
    lease_seconds: LeaseSeconds = 2100


class FinishVideoFrameRequest(VideoFrameLeaseRequest):
    blob: FrameBlob

    @model_validator(mode="after")
    def exact_attempt(self):
        if self.blob.key.split("/")[2:4] != [
            f"g{self.generation}",
            f"a{self.lease_token}",
        ]:
            raise ValueError("frame publication escaped its writer attempt")
        return self


class FailVideoFrameRequest(VideoFrameLeaseRequest):
    error_code: ErrorCode


class LockVideoFrameRequest(VideoFrameRequest):
    expected_entry: FrameTimeEntry
    expected_blob: FrameBlob

    @model_validator(mode="after")
    def exact_entry(self):
        if self.expected_entry.ordinal != self.ordinal:
            raise ValueError("video write fence changed its frame ordinal")
        return self


class SweepVideoInputsRequest(Value):
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100


class SweptVideoInputs(Value):
    expired: Count


@dataclass(frozen=True)
class NativeOperation:
    request: type[Value]
    result: type[Value]
    arguments: tuple[str, ...]
    sql_types: tuple[str, ...]
    role: Literal["workflow", "control", "worker", "labeling", "system"]


_SCOPE = ("job_id", "actor_id", "project_id")
_SCOPE_TYPES = ("uuid", "uuid", "uuid")
_INPUT = ("input_id", "job_id", "task_id", "actor_id")
_INPUT_TYPES = ("uuid",) * 4
_LEASE = ("input_id", "generation", "lease_token")
_FRAME_LEASE = ("frame_id", "generation", "lease_token")
_LEASE_TYPES = ("uuid", "bigint", "uuid")
_FRAME = ("actor_id", "job_id", "task_id", "input_id", "plan_digest", "ordinal")
_FRAME_TYPES = ("uuid", "uuid", "uuid", "uuid", "text", "bigint")

OPERATIONS = {
    "capture_accepted_video_manifest": NativeOperation(
        CaptureVideoManifestRequest,
        VideoManifestHeader,
        (*_SCOPE, "selection"),
        (*_SCOPE_TYPES, "jsonb"),
        "workflow",
    ),
    "read_accepted_video_manifest": NativeOperation(
        VideoManifestScope,
        VideoManifestHeader,
        _SCOPE,
        _SCOPE_TYPES,
        "control",
    ),
    "read_accepted_video_manifest_page": NativeOperation(
        VideoManifestPageRequest,
        VideoManifestPage,
        (*_SCOPE, "selection_digest", "sealed_digest", "cursor", "page_size"),
        (*_SCOPE_TYPES, "text", "text", "bigint", "integer"),
        "control",
    ),
    "prepare_accepted_video_member": NativeOperation(
        ObserveVideoMemberRequest,
        VideoMemberReceipt,
        (
            *_SCOPE,
            "selection_digest",
            "ordinal",
            "task_id",
            "source_snapshot",
            "timetable_revision",
            "object_identity",
            "terminal_error",
        ),
        (*_SCOPE_TYPES, "text", "bigint", "uuid", "text", "bigint", "jsonb", "text"),
        "control",
    ),
    "seal_accepted_video_manifest": NativeOperation(
        SealVideoManifestRequest,
        VideoManifestHeader,
        (*_SCOPE, "selection_digest"),
        (*_SCOPE_TYPES, "text"),
        "control",
    ),
    "read_accepted_video_input": NativeOperation(
        ReadVideoInputRequest,
        VideoInputRecord,
        _INPUT,
        _INPUT_TYPES,
        "control",
    ),
    "read_accepted_video_times": NativeOperation(
        FramePageRequest,
        FrameTimePage,
        (*_FRAME[:-1], "cursor", "page_size"),
        (*_FRAME_TYPES[:-1], "bigint", "integer"),
        "control",
    ),
    "require_accepted_video_frame": NativeOperation(
        VideoFrameRequest,
        VideoFrameRecord,
        _FRAME,
        _FRAME_TYPES,
        "control",
    ),
    "read_accepted_video_frame": NativeOperation(
        VideoFrameRequest,
        VideoFrameRecord,
        _FRAME,
        _FRAME_TYPES,
        "control",
    ),
    "claim_accepted_video_input": NativeOperation(
        ClaimVideoInputRequest,
        ClaimedVideoInput,
        ("worker_id", "input_id", "lease_seconds"),
        ("uuid", "uuid", "integer"),
        "worker",
    ),
    "heartbeat_accepted_video_input": NativeOperation(
        HeartbeatVideoInputRequest,
        VideoInputLease,
        (*_LEASE, "lease_seconds"),
        (*_LEASE_TYPES, "integer"),
        "worker",
    ),
    "stage_accepted_video_times": NativeOperation(
        StageVideoTimesRequest,
        StagedVideoTimes,
        (*_LEASE, "cursor", "entries"),
        (*_LEASE_TYPES, "bigint", "jsonb"),
        "worker",
    ),
    "finish_accepted_video_input": NativeOperation(
        FinishVideoInputRequest,
        VideoInputRecord,
        (*_LEASE, "plan"),
        (*_LEASE_TYPES, "jsonb"),
        "worker",
    ),
    "fail_accepted_video_input": NativeOperation(
        FailVideoInputRequest,
        VideoInputRecord,
        (*_LEASE, "error_code"),
        (*_LEASE_TYPES, "text"),
        "worker",
    ),
    "claim_accepted_video_frame": NativeOperation(
        ClaimVideoFrameRequest,
        ClaimedVideoFrame,
        ("worker_id", "frame_id", "lease_seconds"),
        ("uuid", "uuid", "integer"),
        "worker",
    ),
    "heartbeat_accepted_video_frame": NativeOperation(
        HeartbeatVideoFrameRequest,
        VideoFrameLease,
        (*_FRAME_LEASE, "lease_seconds"),
        (*_LEASE_TYPES, "integer"),
        "worker",
    ),
    "finish_accepted_video_frame": NativeOperation(
        FinishVideoFrameRequest,
        VideoFrameRecord,
        (*_FRAME_LEASE, "blob"),
        (*_LEASE_TYPES, "jsonb"),
        "worker",
    ),
    "fail_accepted_video_frame": NativeOperation(
        FailVideoFrameRequest,
        VideoFrameRecord,
        (*_FRAME_LEASE, "error_code"),
        (*_LEASE_TYPES, "text"),
        "worker",
    ),
    "lock_accepted_video_frame": NativeOperation(
        LockVideoFrameRequest,
        VideoFrameRecord,
        (*_FRAME, "expected_entry", "expected_blob"),
        (*_FRAME_TYPES, "jsonb", "jsonb"),
        "labeling",
    ),
    "sweep_accepted_video_inputs": NativeOperation(
        SweepVideoInputsRequest,
        SweptVideoInputs,
        ("limit",),
        ("integer",),
        "system",
    ),
}


def require_scope(operation: str, request: Value, result: Value) -> None:
    header = (
        result.manifest
        if isinstance(result, (VideoManifestPage, VideoMemberReceipt))
        else result
    )
    if isinstance(header, VideoManifestHeader):
        for name in ("job_id", "project_id", "selection_digest"):
            expected = getattr(request, name, None)
            if expected is not None and getattr(header, name) != expected:
                raise ValueError("video manifest reply escaped its requested scope")
        if isinstance(request, CaptureVideoManifestRequest) and (
            header.actor_id != request.actor_id or header.selection != request.selection
        ):
            raise ValueError("video capture changed its admitted actor or selection")
        if isinstance(request, SealVideoManifestRequest) and header.status != "sealed":
            raise ValueError("video seal did not complete the preparation barrier")
    if isinstance(result, VideoManifestPage) and (
        result.cursor != request.cursor
        or len(result.members) > request.page_size
        or (
            request.sealed_digest is not None
            and (
                result.manifest.status != "sealed"
                or result.manifest.sealed_digest != request.sealed_digest
            )
        )
    ):
        raise ValueError("video manifest page changed its requested boundary")
    if isinstance(result, VideoMemberReceipt) and (
        result.member.ordinal != request.ordinal
        or result.member.task.task_id != request.task_id
        or result.member.state == "unprepared"
    ):
        raise ValueError("video preparation changed its selected member")
    if isinstance(result, FrameTimePage):
        require_page_scope(request, result)
    if isinstance(result, (VideoInputLease, VideoFrameLease, StagedVideoTimes)):
        names = _LEASE if hasattr(result, "input_id") else _FRAME_LEASE
        if any(getattr(result, n) != getattr(request, n) for n in names):
            raise ValueError("video lease reply changed its writer attempt")
        if isinstance(result, StagedVideoTimes) and result.next_cursor != (
            request.cursor + len(request.entries)
        ):
            raise ValueError("video staging changed its page progress")
    record = result
    if isinstance(result, (ClaimedVideoInput, ClaimedVideoFrame)):
        if result.claim is None:
            return
        record = (
            result.claim.input
            if isinstance(result, ClaimedVideoInput)
            else result.claim.frame
        )
    if isinstance(record, VideoInputRecord):
        for name, actual in (
            ("input_id", record.input_id),
            ("job_id", record.job_id),
            ("task_id", record.task.task_id),
        ):
            expected = getattr(request, name, None)
            if expected is not None and expected != actual:
                raise ValueError("video input reply changed its requested identity")
        if isinstance(request, FinishVideoInputRequest) and (
            record.status != "ready" or record.plan != request.plan
        ):
            raise ValueError("video finish changed its retained plan")
        if isinstance(request, FailVideoInputRequest) and (
            record.status != "failed" or record.error != request.error_code
        ):
            raise ValueError("video failure changed its fixed outcome")
    if isinstance(record, VideoFrameRecord):
        video = record.plan.input
        for name, actual in (
            ("frame_id", record.frame_id),
            ("job_id", video.job_id),
            ("task_id", video.task.task_id),
            ("input_id", video.input_id),
            ("plan_digest", record.plan.plan_digest),
            ("ordinal", record.entry.ordinal),
        ):
            expected = getattr(request, name, None)
            if expected is not None and expected != actual:
                raise ValueError("video frame reply changed its requested identity")
        if isinstance(request, (FinishVideoFrameRequest, LockVideoFrameRequest)) and (
            record.status != "ready"
            or record.blob
            != (
                request.blob
                if isinstance(request, FinishVideoFrameRequest)
                else request.expected_blob
            )
            or (
                isinstance(request, LockVideoFrameRequest)
                and record.entry != request.expected_entry
            )
        ):
            raise ValueError("video frame reply changed its retained bytes or time")
        if isinstance(request, FailVideoFrameRequest) and (
            record.status != "failed" or record.error != request.error_code
        ):
            raise ValueError("frame failure changed its fixed outcome")


class _Error(Value):
    code: ErrorCode


def parse_native(operation: str, request: Value, raw: Any) -> Value:
    definition = OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise VideoInputFailure("invalid_request")
    try:
        if len(json_bytes(request)) > REQUEST_BYTES:
            raise VideoInputFailure("resource_budget_exceeded")
        if isinstance(raw, (str, bytes)):
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > REPLY_BYTES:
                raise ValueError("oversized video owner reply")
            raw = json.loads(raw)
        if (
            not isinstance(raw, dict)
            or len(json_bytes(raw)) > REPLY_BYTES
            or raw.get("schema") != EXECUTION_SCHEMA
        ):
            raise ValueError("invalid video owner envelope")
        if raw.get("ok") is False and set(raw) == {"schema", "ok", "error"}:
            raise VideoInputFailure(_Error.model_validate(raw["error"]).code)
        if raw.get("ok") is not True or set(raw) != {"schema", "ok", "data"}:
            raise ValueError("invalid video owner success")
        result = definition.result.model_validate(raw["data"])
        header = (
            result.manifest
            if isinstance(result, (VideoManifestPage, VideoMemberReceipt))
            else result
        )
        if isinstance(header, VideoManifestHeader):
            raw_header = (
                raw["data"]["manifest"] if header is not result else raw["data"]
            )
            if raw_header.get("selection") != header.selection.model_dump(mode="json"):
                raise ValueError("native video selector is not canonical")
        require_scope(operation, request, result)
        return result
    except VideoInputFailure:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ):
        raise VideoInputFailure("invalid_response") from None


def contract_document() -> dict:
    definitions = {}
    models = [m for op in OPERATIONS.values() for m in (op.request, op.result)] + [
        PrepareVideoMemberRequest,
        VideoInputWork,
        _Error,
    ]
    for model in models:
        shape = model.model_json_schema()
        for name, child in shape.pop("$defs", {}).items():
            if name in definitions and definitions[name] != child:
                raise ValueError("conflicting video execution definitions")
            definitions[name] = child
        if model.__name__ in definitions and definitions[model.__name__] != shape:
            raise ValueError("conflicting video execution model")
        definitions[model.__name__] = shape
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EXECUTION_SCHEMA,
        "$defs": definitions,
        "x-stage": "owner contract; native and HTTP admission not activated",
        "x-budgets": {
            "request_bytes": REQUEST_BYTES,
            "reply_bytes": REPLY_BYTES,
            "page_size": PAGE_SIZE,
        },
        "x-native": {
            name: {
                "signature": "aap_assets_v1."
                + name
                + "("
                + ",".join(op.sql_types)
                + ")",
                "arguments": list(op.arguments),
                "request": op.request.__name__,
                "result": op.result.__name__,
                "role": op.role,
            }
            for name, op in OPERATIONS.items()
        },
    }
