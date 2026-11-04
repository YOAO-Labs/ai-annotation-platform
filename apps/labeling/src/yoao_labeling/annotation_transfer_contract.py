"""Closed Labeling commands for annotation import, conversion, and camera edits."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

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
    model_validator,
)

from .annotation_write_contract import _json_bytes
from .cross_frame_write_contract import CrossFrameAnnotationSnapshot

ANNOTATION_TRANSFER_PROTOCOL = "aap-labeling-annotation-transfer/v1"
ANNOTATION_TRANSFER_FUNCTION = "aap_labeling_v1.annotation_transfer_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-transfer/v1"] = Field(alias="schema")
    operation: StrictStr


class ResolveTask(_Command):
    operation: Literal["resolve_task"]
    project_id: uuid.UUID
    display_id: StrictStr | None
    file_path: StrictStr | None


class ResolveTaskByStem(_Command):
    operation: Literal["resolve_task_by_stem"]
    project_id: uuid.UUID
    normalized_path: StrictStr


class ResolveVideoSequence(_Command):
    operation: Literal["resolve_video_sequence"]
    project_id: uuid.UUID
    target: StrictStr


class ReadImportCameraFacts(_Command):
    operation: Literal["read_import_camera_facts"]
    task_id: uuid.UUID
    track_id: StrictStr
    sensor_role: StrictStr
    overwrite: StrictBool


class PurgeImportedAnnotations(_Command):
    operation: Literal["purge_imported_annotations"]
    task_id: uuid.UUID


class ImportedAnnotation(_ClosedModel):
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    source: Literal["manual", "prediction_based", "interpolated"]
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    track_id: StrictStr | None
    confidence: float | None
    attributes: dict[str, object]
    temporal_role: Literal["keyframe", "derived", "sample"]
    created_at: datetime | None
    scene_track_id: uuid.UUID | None
    sensor_dataset_item_id: uuid.UUID | None
    sensor_role: StrictStr | None
    sensor_visibility: StrictStr | None
    calibration_revision: StrictInt | None
    calibration_digest: StrictStr | None
    scene_id: uuid.UUID | None
    frame_index: StrictInt | None


class CreateImportedAnnotation(_Command, ImportedAnnotation):
    operation: Literal["create_imported_annotation"]


class ImportedInterval(_ClosedModel):
    interval_id: uuid.UUID
    start_frame: StrictInt = Field(ge=0)
    end_frame: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("interval end_frame must not precede start_frame")
        return self


class ReplaceImportedSceneTrack(_Command):
    operation: Literal["replace_imported_scene_track"]
    project_id: uuid.UUID
    actor_id: uuid.UUID
    track_id: StrictStr
    scene_name: StrictStr | None
    class_name: StrictStr
    intervals: list[ImportedInterval]
    attributes: dict[str, object]
    attributes_meta: dict[str, object]
    presence_mode: Literal["inferred", "explicit"]


class CreateConversionPlan(_Command):
    operation: Literal["create_conversion_plan"]
    plan_id: uuid.UUID
    token_hash: StrictStr
    task_id: uuid.UUID
    actor_id: uuid.UUID
    request_digest: StrictStr
    snapshot_digest: StrictStr
    request_json: dict[str, object]
    plan_json: dict[str, object]
    expires_at: AwareDatetime


class ReadConversionPlan(_Command):
    operation: Literal["read_conversion_plan"]
    token_hash: StrictStr


class ReadConversionSources(_Command):
    operation: Literal["read_conversion_sources"]
    task_id: uuid.UUID
    annotation_ids: list[uuid.UUID]
    lock: StrictBool


class ReadConversionVideoSegments(_Command):
    operation: Literal["read_conversion_video_segments"]
    dataset_item_id: uuid.UUID


class LockConversionPlan(_Command):
    operation: Literal["lock_conversion_plan"]
    token_hash: StrictStr
    task_id: uuid.UUID
    actor_id: uuid.UUID


class ExpireConversionPlans(_Command):
    operation: Literal["expire_conversion_plans"]
    now: AwareDatetime


class ReadConversionReplay(_Command):
    operation: Literal["read_conversion_replay"]
    task_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr


class ConversionSourceFence(_ClosedModel):
    annotation_id: uuid.UUID
    version: StrictInt = Field(ge=1)
    digest: StrictStr


class ConversionAction(_ClosedModel):
    kind: Literal["update", "create", "deactivate"]
    source_id: uuid.UUID
    result_id: uuid.UUID | None
    geometry: dict[str, object] | None
    annotation_type: StrictStr | None
    tool_unit_id: StrictStr | None
    track_id: StrictStr | None
    frame_index: StrictInt | None

    @model_validator(mode="after")
    def check_payload(self):
        if self.kind == "create":
            if self.result_id is None:
                raise ValueError("create action requires a result identity")
        elif self.kind == "update":
            if self.result_id is not None:
                raise ValueError("update action cannot set a result identity")
        else:
            if (
                self.geometry is not None
                or self.annotation_type is not None
                or self.tool_unit_id is not None
                or self.track_id is not None
            ):
                raise ValueError("deactivate action cannot contain target data")
            return self
        if (
            self.geometry is None
            or self.annotation_type is None
            or self.tool_unit_id is None
        ):
            raise ValueError("write action requires geometry and target metadata")
        return self


class ApplyConversion(_Command):
    operation: Literal["apply_conversion"]
    plan_token_hash: StrictStr
    plan_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    execution_id: uuid.UUID
    idempotency_key: StrictStr
    execution_digest: StrictStr
    scope_fingerprint: StrictStr
    source_fences: list[ConversionSourceFence]
    actions: list[ConversionAction]
    report: dict[str, object]


class FinalizeConversion(_Command):
    operation: Literal["finalize_conversion"]
    plan_token_hash: StrictStr
    task_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr
    operation_id: uuid.UUID
    response_json: dict[str, object]


class ReadCameraLinks(_Command):
    operation: Literal["read_camera_links"]
    task_id: uuid.UUID
    roles: list[StrictStr]


class ReadCameraGraph(_Command):
    operation: Literal["read_camera_graph"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    scene_track_id: uuid.UUID | None
    include_inactive: StrictBool


class MutateCameraMember(_Command):
    operation: Literal["mutate_camera_member"]
    action: Literal["create", "update", "delete", "restore"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    member_id: uuid.UUID
    source_annotation_id: uuid.UUID | None
    camera_role: StrictStr | None
    camera_item_id: uuid.UUID | None
    bbox: dict[str, object] | None
    visibility: StrictStr | None
    expected_version: Annotated[StrictInt, Field(ge=1)] | None
    expected_track_revision: StrictInt = Field(ge=1)
    expected_calibration_revision: Annotated[StrictInt, Field(ge=1)] | None
    expected_calibration_digest: StrictStr | None

    @model_validator(mode="after")
    def check_scope(self):
        if self.action == "create":
            if (
                self.source_annotation_id is None
                or self.camera_role is None
                or self.camera_item_id is None
                or self.bbox is None
                or self.visibility is None
                or self.expected_version is not None
                or self.expected_calibration_revision is None
                or self.expected_calibration_digest is None
            ):
                raise ValueError("camera create requires source and calibration facts")
        elif self.action == "delete":
            if (
                self.source_annotation_id is not None
                or self.camera_role is not None
                or self.camera_item_id is not None
                or self.bbox is not None
                or self.visibility is not None
                or self.expected_version is None
                or self.expected_calibration_revision is not None
                or self.expected_calibration_digest is not None
            ):
                raise ValueError("camera delete must contain only its version fence")
        else:
            if (
                self.source_annotation_id is not None
                or self.camera_role is None
                or self.camera_item_id is None
                or self.expected_version is None
                or self.expected_calibration_revision is None
                or self.expected_calibration_digest is None
                or (
                    self.action == "restore"
                    and (self.bbox is not None or self.visibility is not None)
                )
                or (
                    self.action == "update"
                    and self.bbox is None
                    and self.visibility is None
                )
            ):
                raise ValueError("camera update or restore scope is incomplete")
        return self


class LockCameraMemberScope(_Command):
    operation: Literal["lock_camera_member_scope"]
    action: Literal["create", "update", "restore"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    member_id: uuid.UUID | None
    source_annotation_id: uuid.UUID | None
    expected_version: Annotated[StrictInt, Field(ge=1)] | None
    expected_track_revision: StrictInt = Field(ge=1)


class CreateMaskFormatImport(_Command):
    operation: Literal["create_mask_format_import"]
    id: uuid.UUID
    project_id: uuid.UUID
    requested_by_id: uuid.UUID
    format_id: StrictStr
    adapter_version: StrictStr
    manifest_version: StrictStr
    staged_object_key: StrictStr
    staged_sha256: StrictStr
    mapping_json: dict[str, object]
    options_json: dict[str, object]
    mapping_digest: StrictStr
    options_digest: StrictStr
    plan_json: dict[str, object]
    plan_digest: StrictStr
    token_hash: StrictStr
    receipt_expires_at: AwareDatetime
    result_json: dict[str, object]


class ReadMaskFormatImport(_Command):
    operation: Literal["read_mask_format_import"]
    id: uuid.UUID
    project_id: uuid.UUID | None


class LockMaskFormatImportReceipt(_Command):
    operation: Literal["lock_mask_format_import_receipt"]
    token_hash: StrictStr
    project_id: uuid.UUID
    requested_by_id: uuid.UUID


class LockMaskFormatImport(_Command):
    operation: Literal["lock_mask_format_import"]
    id: uuid.UUID | None
    project_id: uuid.UUID | None
    async_job_id: uuid.UUID | None

    @model_validator(mode="after")
    def check_identity(self):
        if self.id is None and self.async_job_id is None:
            raise ValueError("mask format import lock requires an identity")
        return self


class LinkMaskFormatImportJob(_Command):
    operation: Literal["link_mask_format_import_job"]
    id: uuid.UUID
    project_id: uuid.UUID
    requested_by_id: uuid.UUID
    async_job_id: uuid.UUID
    resume: StrictBool


class MarkMaskFormatImportRunning(_Command):
    operation: Literal["mark_mask_format_import_running"]
    id: uuid.UUID
    async_job_id: uuid.UUID


class CommittedMaskImportItem(_ClosedModel):
    status: Literal["committed"]
    task_id: StrictStr
    imported: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)


class SkippedMaskImportItem(_ClosedModel):
    status: Literal["skipped"]
    skip_code: StrictStr


class FailedMaskImportItem(_ClosedModel):
    status: Literal["failed"]
    reason: StrictStr = Field(max_length=500)


MaskFormatImportItemResult: TypeAlias = Annotated[
    CommittedMaskImportItem | SkippedMaskImportItem | FailedMaskImportItem,
    Field(discriminator="status"),
]


class RecordMaskFormatImportItem(_Command):
    operation: Literal["record_mask_format_import_item"]
    id: uuid.UUID
    async_job_id: uuid.UUID
    item_key: StrictStr
    item_result: MaskFormatImportItemResult


class FinishMaskFormatImport(_Command):
    operation: Literal["finish_mask_format_import"]
    action: Literal["cancel", "complete", "fail", "reconcile_cancel", "reconcile_fail"]
    id: uuid.UUID | None
    async_job_id: uuid.UUID | None
    failed: StrictInt | None = Field(ge=0)
    completed: StrictInt | None = Field(ge=0)
    skipped: StrictInt | None = Field(ge=0)

    @model_validator(mode="after")
    def check_action_scope(self):
        if self.action == "complete":
            if (
                self.id is None
                or self.async_job_id is None
                or any(
                    value is None
                    for value in (self.failed, self.completed, self.skipped)
                )
            ):
                raise ValueError("complete requires a batch, job and item counts")
        elif self.action == "cancel":
            if (
                self.id is None
                or self.async_job_id is None
                or any(
                    value is not None
                    for value in (self.failed, self.completed, self.skipped)
                )
            ):
                raise ValueError("terminal batch action requires a batch and job only")
        elif self.action == "fail":
            if (self.id is None and self.async_job_id is None) or any(
                value is not None
                for value in (self.failed, self.completed, self.skipped)
            ):
                raise ValueError("failure action requires a batch or job only")
        elif (
            self.async_job_id is None
            or self.id is not None
            or any(
                value is not None
                for value in (self.failed, self.completed, self.skipped)
            )
        ):
            raise ValueError("terminal reconciliation requires a job identity only")
        return self


class CancelPendingMaskFormatImport(_Command):
    operation: Literal["cancel_pending_mask_format_import"]
    async_job_id: uuid.UUID


AnnotationTransferCommand: TypeAlias = Annotated[
    ResolveTask
    | ResolveTaskByStem
    | ResolveVideoSequence
    | ReadImportCameraFacts
    | PurgeImportedAnnotations
    | CreateImportedAnnotation
    | ReplaceImportedSceneTrack
    | CreateConversionPlan
    | ReadConversionPlan
    | ReadConversionSources
    | ReadConversionVideoSegments
    | LockConversionPlan
    | ExpireConversionPlans
    | ReadConversionReplay
    | ApplyConversion
    | FinalizeConversion
    | ReadCameraLinks
    | ReadCameraGraph
    | MutateCameraMember
    | LockCameraMemberScope
    | CreateMaskFormatImport
    | ReadMaskFormatImport
    | LockMaskFormatImportReceipt
    | LockMaskFormatImport
    | LinkMaskFormatImportJob
    | MarkMaskFormatImportRunning
    | RecordMaskFormatImportItem
    | FinishMaskFormatImport
    | CancelPendingMaskFormatImport,
    Field(discriminator="operation"),
]


class TaskMatchSnapshot(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    dataset_item_id: uuid.UUID | None
    display_id: StrictStr
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr


class ConversionPlanSnapshot(_ClosedModel):
    id: uuid.UUID
    token_hash: StrictStr
    task_id: uuid.UUID
    actor_id: uuid.UUID
    request_digest: StrictStr
    snapshot_digest: StrictStr
    request_json: dict[str, object]
    plan_json: dict[str, object]
    executed_operation_id: uuid.UUID | None
    executed_idempotency_key: StrictStr | None
    created_at: AwareDatetime
    expires_at: AwareDatetime


class CameraLink(_ClosedModel):
    role: StrictStr
    dataset_item_id: uuid.UUID


class CameraTrackSnapshot(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr
    class_name: StrictStr
    revision: StrictInt
    retired_at: AwareDatetime | None


class CameraGraph(_ClosedModel):
    task_scene_id: uuid.UUID | None
    members: list[CrossFrameAnnotationSnapshot]
    sources: list[CrossFrameAnnotationSnapshot]
    tracks: list[CameraTrackSnapshot]


class ImportCameraFacts(_ClosedModel):
    sensor_dataset_item_id: uuid.UUID | None
    scene_track_id: uuid.UUID | None
    primary_exists: StrictBool
    member_exists: StrictBool


class ImportedAnnotationResult(_ClosedModel):
    annotation_id: uuid.UUID
    scene_track_id: uuid.UUID | None


class ImportSceneTrackResult(_ClosedModel):
    updated: StrictBool
    reason: StrictStr | None
    scene_track_id: uuid.UUID | None


class ConversionSources(_ClosedModel):
    annotations: list[CrossFrameAnnotationSnapshot]


class ConversionVideoSegment(_ClosedModel):
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    start_frame: StrictInt
    end_frame: StrictInt
    segment_index: StrictInt
    locked_by: uuid.UUID | None
    lock_expires_at: AwareDatetime | None


class ConversionReplay(_ClosedModel):
    operation_id: uuid.UUID
    request_digest: StrictStr
    response_json: dict[str, object]


class ConversionApplyResult(_ClosedModel):
    operation_id: uuid.UUID
    updated_annotations: list[CrossFrameAnnotationSnapshot]
    created_annotations: list[CrossFrameAnnotationSnapshot]
    deleted_annotation_ids: list[uuid.UUID]
    result_versions: dict[StrictStr, StrictInt]


class CameraMutationResult(_ClosedModel):
    member: CrossFrameAnnotationSnapshot
    track_revision: StrictInt


class CameraLockScope(_ClosedModel):
    source: CrossFrameAnnotationSnapshot | None
    member: CrossFrameAnnotationSnapshot | None
    track: CameraTrackSnapshot


class MaskFormatImportSnapshot(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    requested_by_id: uuid.UUID | None
    async_job_id: uuid.UUID | None
    format_id: StrictStr
    adapter_version: StrictStr
    manifest_version: StrictStr
    staged_object_key: StrictStr
    staged_sha256: StrictStr
    mapping_json: dict[str, object]
    options_json: dict[str, object]
    mapping_digest: StrictStr
    options_digest: StrictStr
    plan_json: dict[str, object]
    plan_digest: StrictStr
    token_hash: StrictStr
    receipt_expires_at: AwareDatetime
    status: Literal[
        "staged", "pending", "running", "partial", "completed", "failed", "cancelled"
    ]
    result_json: dict[str, object]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class AnnotationTransferData(_ClosedModel):
    operation: StrictStr
    task: TaskMatchSnapshot | None = None
    reason: StrictStr | None = None
    tasks: list[TaskMatchSnapshot] = Field(default_factory=list)
    camera_facts: ImportCameraFacts | None = None
    purged_count: StrictInt | None = None
    imported_annotation: ImportedAnnotationResult | None = None
    scene_track: ImportSceneTrackResult | None = None
    plan: ConversionPlanSnapshot | None = None
    conversion_sources: list[CrossFrameAnnotationSnapshot] = Field(default_factory=list)
    video_segments: list[ConversionVideoSegment] = Field(default_factory=list)
    expired_count: StrictInt | None = None
    replay: ConversionReplay | None = None
    conversion: ConversionApplyResult | None = None
    camera_links: list[CameraLink] = Field(default_factory=list)
    camera_graph: CameraGraph | None = None
    camera_mutation: CameraMutationResult | None = None
    camera_lock_scope: CameraLockScope | None = None
    mask_import: MaskFormatImportSnapshot | None = None
    committed_count: StrictInt | None = None


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-transfer/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AnnotationTransferCommand
    data: AnnotationTransferData


class AnnotationTransferUnavailable(RuntimeError):
    """The Labeling annotation transfer owner returned an invalid reply."""


_COMMAND = TypeAdapter(AnnotationTransferCommand)
_REPLY = TypeAdapter(_Reply)


def decode_annotation_transfer_command(value: object) -> AnnotationTransferCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationTransferUnavailable() from None


def annotation_transfer_command_json(command: AnnotationTransferCommand) -> str:
    return decode_annotation_transfer_command(
        command.model_dump_json(by_alias=True)
    ).model_dump_json(by_alias=True)


def decode_annotation_transfer_reply(
    request: AnnotationTransferCommand, value: object
) -> AnnotationTransferData:
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationTransferUnavailable() from None
    if (
        not reply.ok
        or reply.request != request
        or reply.data.operation != request.operation
    ):
        raise AnnotationTransferUnavailable()
    data = reply.data
    if isinstance(request, (ResolveTask, ResolveTaskByStem, ResolveVideoSequence)):
        if data.task is not None and data.task.project_id != request.project_id:
            raise AnnotationTransferUnavailable()
        if any(task.project_id != request.project_id for task in data.tasks):
            raise AnnotationTransferUnavailable()
    if isinstance(request, (LockConversionPlan, CreateConversionPlan)):
        if data.plan is None or data.plan.task_id != request.task_id:
            raise AnnotationTransferUnavailable()
    if isinstance(request, ReadConversionPlan) and data.plan is not None:
        if data.plan.token_hash != request.token_hash:
            raise AnnotationTransferUnavailable()
    if isinstance(request, (ReadImportCameraFacts,)) and data.camera_facts is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, PurgeImportedAnnotations) and data.purged_count is None:
        raise AnnotationTransferUnavailable()
    if (
        isinstance(request, CreateImportedAnnotation)
        and data.imported_annotation is None
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, ReplaceImportedSceneTrack) and data.scene_track is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, ExpireConversionPlans) and data.expired_count is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, ReadConversionSources) and any(
        row.task_id != request.task_id for row in data.conversion_sources
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, ReadConversionVideoSegments) and any(
        row.dataset_item_id != request.dataset_item_id for row in data.video_segments
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, ApplyConversion) and data.conversion is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, FinalizeConversion) and data.plan is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, ReadCameraGraph) and data.camera_graph is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, MutateCameraMember) and data.camera_mutation is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, LockCameraMemberScope) and data.camera_lock_scope is None:
        raise AnnotationTransferUnavailable()
    if isinstance(request, CreateMaskFormatImport | LockMaskFormatImportReceipt):
        if data.mask_import is None:
            raise AnnotationTransferUnavailable()
    if (
        isinstance(
            request,
            (
                LinkMaskFormatImportJob,
                MarkMaskFormatImportRunning,
                RecordMaskFormatImportItem,
            ),
        )
        and data.mask_import is None
    ):
        raise AnnotationTransferUnavailable()
    if (
        isinstance(request, FinishMaskFormatImport)
        and request.action in {"cancel", "complete", "fail"}
        and data.mask_import is None
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, CreateMaskFormatImport | LinkMaskFormatImportJob):
        if (
            data.mask_import is None
            or data.mask_import.id != request.id
            or data.mask_import.project_id != request.project_id
        ):
            raise AnnotationTransferUnavailable()
    if isinstance(request, ReadMaskFormatImport) and data.mask_import is not None:
        if data.mask_import.id != request.id or (
            request.project_id is not None
            and data.mask_import.project_id != request.project_id
        ):
            raise AnnotationTransferUnavailable()
    if isinstance(request, LockMaskFormatImportReceipt) and (
        data.mask_import is None
        or data.mask_import.project_id != request.project_id
        or data.mask_import.requested_by_id != request.requested_by_id
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, LockMaskFormatImport) and data.mask_import is not None:
        if (
            (request.id is not None and data.mask_import.id != request.id)
            or (
                request.project_id is not None
                and data.mask_import.project_id != request.project_id
            )
            or (
                request.async_job_id is not None
                and data.mask_import.async_job_id != request.async_job_id
            )
        ):
            raise AnnotationTransferUnavailable()
    if isinstance(
        request,
        (MarkMaskFormatImportRunning, RecordMaskFormatImportItem),
    ) and (
        data.mask_import is None
        or data.mask_import.id != request.id
        or data.mask_import.async_job_id != request.async_job_id
    ):
        raise AnnotationTransferUnavailable()
    if isinstance(request, FinishMaskFormatImport) and data.mask_import is not None:
        if (request.id is not None and data.mask_import.id != request.id) or (
            request.async_job_id is not None
            and data.mask_import.async_job_id != request.async_job_id
        ):
            raise AnnotationTransferUnavailable()
    return data
