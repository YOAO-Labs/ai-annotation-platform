"""Closed Labeling Mask QC and repair data commands."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

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
    field_validator,
)

from .annotation_write_contract import _json_bytes

QUALITY_DATA_PROTOCOL = "aap-labeling-quality-data/v1"
QUALITY_DATA_FUNCTIONS = (
    "aap_labeling_v1.mask_qc_command(jsonb)",
    "aap_labeling_v1.mask_repair_command(jsonb)",
    "aap_labeling_v1.raster_mask_retention_query(jsonb)",
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-quality-data/v1"] = Field(alias="schema")


class ReadMaskQCSource(_Command):
    operation: Literal["read_source"]
    project_id: uuid.UUID
    scope: Literal["project", "task_ids", "annotation_ids"]
    task_ids: list[uuid.UUID] = Field(max_length=1000)
    annotation_ids: list[uuid.UUID] = Field(max_length=1000)


class CreateMaskQCRun(_Command):
    operation: Literal["create_run"]
    run_id: uuid.UUID
    project_id: uuid.UUID
    async_job_id: uuid.UUID
    requested_by_id: uuid.UUID
    scope_json: dict[str, object]
    config_revision: StrictInt = Field(ge=1, le=2147483647)
    config_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    config_snapshot: dict[str, object]
    source_snapshot: list[dict[str, object]]
    source_snapshot_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    task_snapshot_digests: dict[str, object]
    singleflight_key: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    summary: dict[str, object]


class FindMaskQCRun(_Command):
    operation: Literal["find_run"]
    project_id: uuid.UUID
    singleflight_key: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class ReadMaskQCRun(_Command):
    operation: Literal["read_run"]
    run_id: uuid.UUID
    lock: StrictBool


class ReadMaskQCRunByJob(_Command):
    operation: Literal["read_run_by_job"]
    async_job_id: uuid.UUID
    lock: StrictBool


class BeginMaskQCRun(_Command):
    operation: Literal["begin_run"]
    run_id: uuid.UUID
    at: AwareDatetime


class ProgressMaskQCRun(_Command):
    operation: Literal["progress_run"]
    run_id: uuid.UUID
    progress_pct: StrictInt = Field(ge=0, le=100)


class CompleteMaskQCRun(_Command):
    operation: Literal["complete_run"]
    run_id: uuid.UUID
    at: AwareDatetime
    summary: dict[str, object]


class FailMaskQCRun(_Command):
    operation: Literal["fail_run"]
    run_id: uuid.UUID
    at: AwareDatetime
    error_message: StrictStr


class CancelMaskQCRun(_Command):
    operation: Literal["cancel_run"]
    run_id: uuid.UUID
    at: AwareDatetime


class ReconcileMaskQCRun(_Command):
    operation: Literal["reconcile_run"]
    run_id: uuid.UUID
    at: AwareDatetime
    terminal_cancelled: StrictBool
    error_message: StrictStr | None


class ReadMaskQCRunSummary(_Command):
    operation: Literal["read_run_summary"]
    run_id: uuid.UUID


class MaskQCIssueData(_ClosedModel):
    id: uuid.UUID
    run_id: uuid.UUID
    last_seen_run_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID
    annotation_id: uuid.UUID
    annotation_version: StrictInt = Field(ge=1, le=2147483647)
    related_annotation_ids: list[uuid.UUID]
    source_versions: dict[StrictStr, StrictInt]
    code: StrictStr = Field(min_length=1, max_length=64)
    severity: Literal["blocker", "warning", "info"]
    severity_rank: StrictInt = Field(ge=0, le=2)
    status: Literal["open"]
    frame_start: StrictInt | None = Field(ge=0, le=2147483647)
    frame_end: StrictInt | None = Field(ge=0, le=2147483647)
    metric: dict[str, object]
    threshold: dict[str, object]
    region_bbox: dict[str, object] | None
    region_mask_ref: dict[str, object] | None
    region_digest: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    dedupe_key: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source: dict[str, object]
    suggestion: StrictStr | None
    updated_at: AwareDatetime

    @field_validator("source_versions")
    @classmethod
    def _source_version_keys(cls, value: dict[str, StrictInt]) -> dict[str, StrictInt]:
        for annotation_id, version in value.items():
            try:
                uuid.UUID(annotation_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("source version keys must be UUIDs") from None
            if not 1 <= version <= 2147483647:
                raise ValueError("source versions must fit a positive integer")
        return value


class UpsertMaskQCIssue(_Command):
    operation: Literal["upsert_issue"]
    issue: MaskQCIssueData


class MarkMaskQCIssuesSeen(_Command):
    operation: Literal["mark_seen"]
    project_id: uuid.UUID
    run_id: uuid.UUID
    dedupe_keys: list[StrictStr]


class ReadMaskQCIssue(_Command):
    operation: Literal["read_issue"]
    issue_id: uuid.UUID
    lock: StrictBool
    nowait: StrictBool


class ReadMaskQCIssues(_Command):
    operation: Literal["read_issues"]
    project_id: uuid.UUID
    issue_ids: list[uuid.UUID]


class ListMaskQCIssues(_Command):
    operation: Literal["list_issues"]
    project_id: uuid.UUID
    limit: StrictInt = Field(ge=1, le=201)
    status: Literal["open", "resolved", "wont_fix", "stale"] | None
    severity: Literal["blocker", "warning", "info"] | None
    code: StrictStr | None
    task_id: uuid.UUID | None
    annotation_id: uuid.UUID | None
    frame: StrictInt | None = Field(ge=0, le=2147483647)
    allowed_task_ids: list[uuid.UUID] | None
    cursor_rank: StrictInt | None = Field(ge=0, le=2)
    cursor_created_at: AwareDatetime | None
    cursor_id: uuid.UUID | None


class ReadTaskRunMaskQCIssues(_Command):
    operation: Literal["read_task_run_issues"]
    task_id: uuid.UUID
    run_id: uuid.UUID


class FindLatestTaskMaskQCRun(_Command):
    operation: Literal["find_latest_task_run"]
    project_id: uuid.UUID
    task_id: uuid.UUID


class FindCurrentCompletedTaskMaskQCRun(_Command):
    operation: Literal["find_current_completed_task_run"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    config_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class ReadOpenTaskMaskQCIssues(_Command):
    operation: Literal["read_open_task_issues"]
    task_id: uuid.UUID
    severity: Literal["blocker", "warning", "info"] | None
    issue_ids: list[uuid.UUID] | None


class DecideMaskQCIssue(_Command):
    operation: Literal["decide_issue"]
    issue_id: uuid.UUID
    expected_task_id: uuid.UUID
    actor_id: uuid.UUID
    status: Literal["open", "resolved", "wont_fix"]
    decided_at: AwareDatetime


class ResolveRepairedMaskQCIssue(_Command):
    operation: Literal["resolve_repaired_issue"]
    issue_id: uuid.UUID
    actor_id: uuid.UUID
    decided_at: AwareDatetime


class ReadMaskQCIssueStatuses(_Command):
    operation: Literal["read_issue_statuses"]
    issue_ids: list[uuid.UUID]


class CheckMaskQCIssueVersions(_Command):
    operation: Literal["issue_versions_current"]
    issue_id: uuid.UUID


class ReadMaskAnnotationRevision(_Command):
    operation: Literal["read_annotation_revision"]
    task_id: uuid.UUID
    annotation_id: uuid.UUID
    annotation_version: StrictInt = Field(ge=1)


class ReadTrackerCandidateContext(_Command):
    operation: Literal["read_tracker_candidate_context"]
    task_id: uuid.UUID
    job_id: uuid.UUID


class CreateMaskRepairBatch(_Command):
    operation: Literal["create_batch"]
    batch_id: uuid.UUID
    project_id: uuid.UUID
    requested_by_id: uuid.UUID
    token_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    request_json: dict[str, object]
    plan_json: dict[str, object]
    result_json: dict[str, object]
    receipt_expires_at: AwareDatetime


class ReadMaskRepairBatchById(_Command):
    operation: Literal["read_batch_by_id"]
    batch_id: uuid.UUID
    lock: StrictBool


class ReadMaskRepairBatchByToken(_Command):
    operation: Literal["read_batch_by_token"]
    token_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    lock: StrictBool


class ReadMaskRepairBatchByJob(_Command):
    operation: Literal["read_batch_by_job"]
    job_id: uuid.UUID
    terminal_kind: Literal["mask_repair", "mask_repair_rollback"]
    lock: StrictBool


class AttachRepairJob(_Command):
    operation: Literal["attach_job"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID


class AttachRepairRollbackJob(_Command):
    operation: Literal["attach_rollback_job"]
    batch_id: uuid.UUID
    rollback_job_id: uuid.UUID


class ResumeRepairBatch(_Command):
    operation: Literal["resume_batch"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    actor_id: uuid.UUID


class BeginRepairExecution(_Command):
    operation: Literal["begin_execution"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    at: AwareDatetime


class BeginRepairRollback(_Command):
    operation: Literal["begin_rollback"]
    batch_id: uuid.UUID
    rollback_job_id: uuid.UUID


class RecordRepairShard(_Command):
    operation: Literal["record_shard"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    shard_id: StrictStr = Field(pattern=r"^[0-9a-f]{24}$")
    result_value: dict[str, object]
    rollback_expires_at: AwareDatetime


class RecordRepairCandidate(_Command):
    operation: Literal["record_candidate"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    item_index: StrictInt = Field(ge=0, le=2147483647)
    result_value: dict[str, object]


class FailRepairShard(_Command):
    operation: Literal["record_shard_failure"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    shard_id: StrictStr = Field(pattern=r"^[0-9a-f]{24}$")
    task_id: uuid.UUID
    error_message: StrictStr = Field(max_length=500)


class FinishRepairExecution(_Command):
    operation: Literal["finish_execution"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    status: Literal["completed", "partial", "failed"]
    completed_at: AwareDatetime


class FinishRepairRollback(_Command):
    operation: Literal["finish_rollback"]
    batch_id: uuid.UUID
    rollback_job_id: uuid.UUID
    rolled_back_at: AwareDatetime
    result_json: dict[str, object]


class FailRepairRollback(_Command):
    operation: Literal["fail_rollback"]
    batch_id: uuid.UUID
    rollback_job_id: uuid.UUID
    error_message: StrictStr
    result_json: dict[str, object]
    failed_at: AwareDatetime


class FailRepairDispatch(_Command):
    operation: Literal["fail_dispatch", "fail_rollback_dispatch"]
    batch_id: uuid.UUID
    async_job_id: uuid.UUID
    error_message: StrictStr


class CancelRepairBatch(_Command):
    operation: Literal["cancel_pending"]
    batch_id: uuid.UUID
    status: Literal["cancelled", "partial"]
    completed_at: AwareDatetime


class ReconcileRepairBatch(_Command):
    operation: Literal["reconcile_terminal"]
    job_id: uuid.UUID
    terminal_kind: Literal["mask_repair", "mask_repair_rollback"]
    terminal_cancelled: StrictBool
    status: Literal["cancelled", "partial", "failed", "rollback_failed"]
    completed_at: AwareDatetime


class InsertMaskReviewScope(_Command):
    operation: Literal["insert_review_scope"]
    scope_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID
    annotation_id: uuid.UUID
    qc_issue_id: uuid.UUID
    source_job_id: uuid.UUID
    expected_segment_id: uuid.UUID | None
    reviewer_id: uuid.UUID
    source_annotation_version: StrictInt = Field(ge=1, le=2147483647)
    result_annotation_version: StrictInt = Field(ge=1, le=2147483647)
    source_job_revision: StrictInt = Field(ge=0, le=2147483647)
    frame_start: StrictInt = Field(ge=0, le=2147483647)
    frame_end: StrictInt = Field(ge=0, le=2147483647)
    region_mask_ref: dict[str, object]
    region_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_digest: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]


class ReadMaskReviewScopes(_Command):
    operation: Literal["read_review_scopes"]
    annotation_id: uuid.UUID
    result_annotation_version: StrictInt = Field(ge=1, le=2147483647)
    frame_start: StrictInt = Field(ge=0, le=2147483647)
    frame_end: StrictInt = Field(ge=0, le=2147483647)
    lock: StrictBool


class ReadMaskRepairTargets(_Command):
    operation: Literal["read_repair_targets"]
    issue_id: uuid.UUID
    include_related: StrictBool


class ReadMaskVideoSegment(_Command):
    operation: Literal["read_video_segment"]
    dataset_item_id: uuid.UUID | None
    frame_index: StrictInt = Field(ge=0, le=2147483647)
    requested_id: uuid.UUID | None


class ReadRasterMaskReferences(_Command):
    operation: Literal["read_all_references"]


class CheckRasterMaskReference(_Command):
    operation: Literal["check_reference"]
    object_key: StrictStr = Field(min_length=1, max_length=1024)


MaskQCCommand = Annotated[
    ReadMaskQCSource
    | CreateMaskQCRun
    | FindMaskQCRun
    | ReadMaskQCRun
    | ReadMaskQCRunByJob
    | BeginMaskQCRun
    | ProgressMaskQCRun
    | CompleteMaskQCRun
    | FailMaskQCRun
    | CancelMaskQCRun
    | ReconcileMaskQCRun
    | ReadMaskQCRunSummary
    | UpsertMaskQCIssue
    | MarkMaskQCIssuesSeen
    | ReadMaskQCIssue
    | ReadMaskQCIssues
    | ListMaskQCIssues
    | ReadTaskRunMaskQCIssues
    | FindLatestTaskMaskQCRun
    | FindCurrentCompletedTaskMaskQCRun
    | ReadOpenTaskMaskQCIssues
    | DecideMaskQCIssue
    | ResolveRepairedMaskQCIssue
    | ReadMaskQCIssueStatuses
    | CheckMaskQCIssueVersions
    | ReadMaskAnnotationRevision
    | ReadTrackerCandidateContext,
    Field(discriminator="operation"),
]

MaskRepairCommand = Annotated[
    CreateMaskRepairBatch
    | ReadMaskRepairBatchById
    | ReadMaskRepairBatchByToken
    | ReadMaskRepairBatchByJob
    | AttachRepairJob
    | AttachRepairRollbackJob
    | ResumeRepairBatch
    | BeginRepairExecution
    | BeginRepairRollback
    | RecordRepairShard
    | RecordRepairCandidate
    | FailRepairShard
    | FinishRepairExecution
    | FinishRepairRollback
    | FailRepairRollback
    | FailRepairDispatch
    | CancelRepairBatch
    | ReconcileRepairBatch
    | InsertMaskReviewScope
    | ReadMaskReviewScopes
    | ReadMaskRepairTargets
    | ReadMaskVideoSegment,
    Field(discriminator="operation"),
]

QualityDataCommand = Annotated[
    ReadMaskQCSource
    | CreateMaskQCRun
    | FindMaskQCRun
    | ReadMaskQCRun
    | ReadMaskQCRunByJob
    | BeginMaskQCRun
    | ProgressMaskQCRun
    | CompleteMaskQCRun
    | FailMaskQCRun
    | CancelMaskQCRun
    | ReconcileMaskQCRun
    | ReadMaskQCRunSummary
    | UpsertMaskQCIssue
    | MarkMaskQCIssuesSeen
    | ReadMaskQCIssue
    | ReadMaskQCIssues
    | ListMaskQCIssues
    | ReadTaskRunMaskQCIssues
    | FindLatestTaskMaskQCRun
    | FindCurrentCompletedTaskMaskQCRun
    | ReadOpenTaskMaskQCIssues
    | DecideMaskQCIssue
    | ResolveRepairedMaskQCIssue
    | ReadMaskQCIssueStatuses
    | CheckMaskQCIssueVersions
    | ReadMaskAnnotationRevision
    | ReadTrackerCandidateContext
    | CreateMaskRepairBatch
    | ReadMaskRepairBatchById
    | ReadMaskRepairBatchByToken
    | ReadMaskRepairBatchByJob
    | AttachRepairJob
    | AttachRepairRollbackJob
    | ResumeRepairBatch
    | BeginRepairExecution
    | BeginRepairRollback
    | RecordRepairShard
    | RecordRepairCandidate
    | FailRepairShard
    | FinishRepairExecution
    | FinishRepairRollback
    | FailRepairRollback
    | FailRepairDispatch
    | CancelRepairBatch
    | ReconcileRepairBatch
    | InsertMaskReviewScope
    | ReadMaskReviewScopes
    | ReadMaskRepairTargets
    | ReadMaskVideoSegment
    | ReadRasterMaskReferences
    | CheckRasterMaskReference,
    Field(discriminator="operation"),
]


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-quality-data/v1"] = Field(alias="schema")
    ok: StrictBool
    request: QualityDataCommand
    data: (
        dict[str, object]
        | list[dict[str, object]]
        | list[StrictStr]
        | StrictBool
        | None
    )


class QualityDataUnavailable(RuntimeError):
    """The Labeling quality owner returned invalid or mismatched data."""


_COMMAND = TypeAdapter(QualityDataCommand)
_REPLY = TypeAdapter(_Reply)


def decode_quality_data_command(value: object) -> QualityDataCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise QualityDataUnavailable() from None


def quality_data_command_json(command: QualityDataCommand) -> str:
    return decode_quality_data_command(
        command.model_dump_json(by_alias=True)
    ).model_dump_json(by_alias=True)


def decode_quality_data_reply(request: QualityDataCommand, value: object) -> object:
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise QualityDataUnavailable() from None
    if not reply.ok or reply.request != request:
        raise QualityDataUnavailable()
    return reply.data


def quality_data_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = QUALITY_DATA_PROTOCOL
    schema["x-postgresql-functions"] = list(QUALITY_DATA_FUNCTIONS)
    schema["x-postgresql-dependencies"] = [
        "aap_identity_v1.lock_active_actor(uuid)",
        "aap_labeling_v1.video_tracker_input_referenced(uuid)",
    ]
    return schema
