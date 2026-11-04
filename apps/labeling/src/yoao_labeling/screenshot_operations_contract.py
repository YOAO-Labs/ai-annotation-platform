"""Closed commands for screenshot-managed Labeling maintenance operations."""

from __future__ import annotations

import json
import uuid
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
)

SCREENSHOT_OPERATIONS_PROTOCOL = "aap-labeling-screenshot-operations/v1"
SCREENSHOT_OPERATIONS_FUNCTION = "aap_labeling_v1.screenshot_operations_command(jsonb)"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_Closed):
    schema_: Literal["aap-labeling-screenshot-operations/v1"] = Field(alias="schema")
    operation: StrictStr


ScreenshotProjectDisplayId = Literal[
    "P-COCO8",
    "P-VIDEO-DEV",
    "P-PC-DEV",
    "P-PC-MULTI",
    "P-OCR",
]


class SeedTaskReset(_Closed):
    id: uuid.UUID
    file_path: StrictStr
    display_id: StrictStr
    sequence_order: StrictInt
    status: Literal["pending", "in_progress", "review", "completed"]


ScreenshotBatchDisplayId = Literal[
    "B-SS-DRAFT",
    "B-SS-ACTIVE",
    "B-SS-ANNOTATING",
    "B-SS-REVIEW",
    "B-SS-COMPLETE",
]
ScreenshotBatchStatus = Literal[
    "draft", "active", "annotating", "reviewing", "approved"
]


class ResetSeedTasks(_Command):
    operation: Literal["reset_seed_tasks"]
    project_id: uuid.UUID
    project_display_id: ScreenshotProjectDisplayId
    tasks: tuple[SeedTaskReset, ...]


class SeedBatch(_Closed):
    id: uuid.UUID
    existing: StrictBool
    display_id: ScreenshotBatchDisplayId
    name: Literal[
        "截图 · 待分派",
        "截图 · 待预标",
        "截图 · 标注中",
        "截图 · 待审核",
        "截图 · 已完成",
    ]
    status: ScreenshotBatchStatus
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    assigned_user_ids: tuple[StrictStr, ...]


class SeedTaskBatch(_Closed):
    task_id: uuid.UUID
    batch_id: uuid.UUID


class ReconcileSeedBatches(_Command):
    operation: Literal["reconcile_seed_batches"]
    project_id: uuid.UUID
    dataset_id: uuid.UUID
    created_by: uuid.UUID
    fixed_time: AwareDatetime
    batches: tuple[SeedBatch, ...]


class LinkSeedTasksToBatches(_Command):
    operation: Literal["link_seed_tasks_to_batches"]
    project_id: uuid.UUID
    tasks: tuple[SeedTaskBatch, ...]


class SeedTaskState(_Closed):
    task_id: uuid.UUID
    status: Literal["pending", "in_progress", "review", "completed"]
    sequence_order: StrictInt
    assignee_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    assigned_at: AwareDatetime | None
    submitted_at: AwareDatetime | None
    reviewer_claimed_at: AwareDatetime | None
    reviewed_at: AwareDatetime | None


class SetImageSeedTaskState(_Command):
    operation: Literal["set_image_seed_task_state"]
    project_id: uuid.UUID
    tasks: tuple[SeedTaskState, ...]


class ReconcileSeedPredictions(_Command):
    operation: Literal["reconcile_seed_predictions"]
    project_id: uuid.UUID
    task_ids: tuple[uuid.UUID, ...]
    predicted_task_id: uuid.UUID
    keep_prediction_id: uuid.UUID
    remove_prediction_ids: tuple[uuid.UUID, ...]
    model_version: StrictStr
    fixed_time: AwareDatetime


class SeedAnnotation(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    geometry: dict[str, object]
    ground_truth: StrictBool
    created_at: AwareDatetime


class SeedTaskEvidence(_Closed):
    task_id: uuid.UUID
    status: Literal["pending", "in_progress", "review", "completed"]
    review_round_id: uuid.UUID | None
    annotation_contributor_ids: tuple[StrictStr, ...] | None
    review_contributor_ids: tuple[StrictStr, ...] | None
    review_submitter_id: uuid.UUID | None


class ReconcileSeedAnnotations(_Command):
    operation: Literal["reconcile_seed_annotations"]
    project_id: uuid.UUID
    annotations: tuple[SeedAnnotation, ...]
    tasks: tuple[SeedTaskEvidence, ...]
    annotator_id: uuid.UUID
    fixed_time: AwareDatetime


class SeedBatchCounters(_Closed):
    id: uuid.UUID
    batch_key: Literal["draft", "active", "annotating", "review", "completed"]


class SyncSeedBatchCounters(_Command):
    operation: Literal["sync_seed_batch_counters"]
    project_id: uuid.UUID
    task_ids: tuple[uuid.UUID, ...]
    batches: tuple[SeedBatchCounters, ...]


class SeedTaskTimestamp(_Closed):
    project_id: uuid.UUID
    project_display_id: ScreenshotProjectDisplayId
    task_id: uuid.UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PinSeedTaskTimestamps(_Command):
    operation: Literal["pin_seed_task_timestamps"]
    tasks: tuple[SeedTaskTimestamp, ...]


class CleanupOCRRows(_Command):
    operation: Literal["cleanup_ocr_rows"]
    project_key: Literal["image_demo", "video_demo", "ocr_demo"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    tracker_job_ids: tuple[uuid.UUID, ...]
    annotation_ids: tuple[uuid.UUID, ...]


class DeleteOCRPredictions(_Command):
    operation: Literal["delete_ocr_predictions"]
    project_key: Literal["image_demo", "video_demo", "ocr_demo"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    prediction_ids: tuple[uuid.UUID, ...]


class FinishOCRCleanup(_Command):
    operation: Literal["finish_ocr_cleanup"]
    project_key: Literal["image_demo", "video_demo", "ocr_demo"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    expected_status: Literal["pending", "in_progress", "review", "completed"]


class CleanupRecoveryRows(_Command):
    operation: Literal["cleanup_recovery_rows"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    failed_prediction_ids: tuple[uuid.UUID, ...]
    prediction_ids: tuple[uuid.UUID, ...]


class RestoreReviewReject(_Command):
    operation: Literal["restore_review_reject"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    reviewer_id: uuid.UUID
    expected_status: Literal["review"]
    review_round_id: uuid.UUID
    review_contributor_ids: tuple[StrictStr, ...] | None
    review_submitter_id: uuid.UUID | None
    reason_texts: tuple[StrictStr, ...]


ScreenshotOperationsCommand: TypeAlias = Annotated[
    ResetSeedTasks
    | ReconcileSeedBatches
    | LinkSeedTasksToBatches
    | SetImageSeedTaskState
    | ReconcileSeedPredictions
    | ReconcileSeedAnnotations
    | SyncSeedBatchCounters
    | PinSeedTaskTimestamps
    | CleanupOCRRows
    | DeleteOCRPredictions
    | FinishOCRCleanup
    | CleanupRecoveryRows
    | RestoreReviewReject,
    Field(discriminator="operation"),
]


class ScreenshotOperationsResult(_Closed):
    operation: StrictStr
    changed: StrictBool
    counts: dict[StrictStr, StrictInt]


_RESULT_ADAPTER = TypeAdapter(ScreenshotOperationsResult)


def screenshot_operations_command_json(request: ScreenshotOperationsCommand) -> str:
    return json.dumps(
        request.model_dump(mode="json", by_alias=True), separators=(",", ":")
    )


def decode_screenshot_operations_result(
    request: ScreenshotOperationsCommand, value: object
) -> ScreenshotOperationsResult:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid screenshot operations JSON reply") from exc
    result = _RESULT_ADAPTER.validate_python(value)
    if result.operation != request.operation:
        raise ValueError("Screenshot operations reply does not match its request")
    return result


__all__ = [
    "CleanupOCRRows",
    "CleanupRecoveryRows",
    "DeleteOCRPredictions",
    "FinishOCRCleanup",
    "LinkSeedTasksToBatches",
    "PinSeedTaskTimestamps",
    "ReconcileSeedAnnotations",
    "ReconcileSeedBatches",
    "ReconcileSeedPredictions",
    "ResetSeedTasks",
    "RestoreReviewReject",
    "SCREENSHOT_OPERATIONS_FUNCTION",
    "SCREENSHOT_OPERATIONS_PROTOCOL",
    "ScreenshotOperationsCommand",
    "ScreenshotOperationsResult",
    "SeedAnnotation",
    "SeedBatch",
    "SeedBatchCounters",
    "SeedTaskBatch",
    "SeedTaskEvidence",
    "SeedTaskReset",
    "SeedTaskState",
    "SeedTaskTimestamp",
    "SetImageSeedTaskState",
    "SyncSeedBatchCounters",
    "decode_screenshot_operations_result",
    "screenshot_operations_command_json",
]
