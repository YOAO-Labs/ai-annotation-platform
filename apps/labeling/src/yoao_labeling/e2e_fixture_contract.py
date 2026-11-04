"""Closed, test-database-only Labeling fixture write protocol."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal, TypeAlias

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

LABELING_E2E_FIXTURE_PROTOCOL = "aap-labeling-e2e-fixtures/v1"
LABELING_E2E_FIXTURE_FUNCTION = "aap_labeling_v1.e2e_fixture_command"
LABELING_E2E_FILTERING_RENDER_FUNCTION = "aap_labeling_v1.prepare_filtering_rendering"
MAX_E2E_FIXTURE_ROWS = 512
MAX_E2E_FIXTURE_REQUEST_BYTES = 16 * 1024 * 1024


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_Closed):
    schema_: Literal["aap-labeling-e2e-fixtures/v1"] = Field(alias="schema")
    operation: StrictStr


class TaskBatchFixtureRow(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    display_id: Annotated[StrictStr, Field(min_length=1, max_length=30)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    status: Literal["draft", "annotating"] | None = None
    assigned_user_ids: (
        list[
            Annotated[
                StrictStr,
                Field(
                    pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            ]
        ]
        | None
    ) = None
    total_tasks: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None
    created_by: uuid.UUID | None = None
    annotator_id: uuid.UUID | None = None
    reviewer_id: uuid.UUID | None = None


class TaskFixtureRow(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    display_id: Annotated[StrictStr, Field(min_length=1, max_length=30)]
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    dataset_item_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    file_type: Literal["image", "video", "point_cloud"] | None = None
    status: (
        Literal["pending", "annotating", "submitted", "review", "completed", "rejected"]
        | None
    ) = None
    sequence_order: (
        Annotated[StrictInt, Field(ge=-2_147_483_648, le=2_147_483_647)] | None
    ) = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    assignee_id: uuid.UUID | None = None
    assigned_at: AwareDatetime | None = None
    reviewer_id: uuid.UUID | None = None
    reviewer_claimed_at: AwareDatetime | None = None
    submitted_at: AwareDatetime | None = None
    reviewed_at: AwareDatetime | None = None
    review_round_id: uuid.UUID | None = None
    annotation_contributor_ids: (
        list[
            Annotated[
                StrictStr,
                Field(
                    pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            ]
        ]
        | None
    ) = None
    is_labeled: StrictBool | None = None
    total_annotations: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None
    total_predictions: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None
    reject_reason: Annotated[StrictStr, Field(max_length=2000)] | None = None
    reject_reason_type: Annotated[StrictStr, Field(max_length=20)] | None = None
    skip_reason: Annotated[StrictStr, Field(max_length=50)] | None = None
    skipped_at: AwareDatetime | None = None


class TaskFixturePatch(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    dataset_item_id: uuid.UUID | None = None
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=500)] | None = None
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)] | None = None
    file_type: Literal["image", "video", "point_cloud"] | None = None
    status: (
        Literal["pending", "annotating", "submitted", "review", "completed", "rejected"]
        | None
    ) = None
    sequence_order: (
        Annotated[StrictInt, Field(ge=-2_147_483_648, le=2_147_483_647)] | None
    ) = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    assignee_id: uuid.UUID | None = None
    assigned_at: AwareDatetime | None = None
    reviewer_id: uuid.UUID | None = None
    reviewer_claimed_at: AwareDatetime | None = None
    submitted_at: AwareDatetime | None = None
    reviewed_at: AwareDatetime | None = None
    review_round_id: uuid.UUID | None = None
    annotation_contributor_ids: (
        list[
            Annotated[
                StrictStr,
                Field(
                    pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                ),
            ]
        ]
        | None
    ) = None
    is_labeled: StrictBool | None = None
    total_annotations: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None
    total_predictions: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None
    reject_reason: Annotated[StrictStr, Field(max_length=2000)] | None = None
    reject_reason_type: Annotated[StrictStr, Field(max_length=20)] | None = None
    skip_reason: Annotated[StrictStr, Field(max_length=50)] | None = None
    skipped_at: AwareDatetime | None = None


class TaskBatchFixturePatch(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    total_tasks: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None


class AnnotationFixtureRow(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID | None = None
    source: Annotated[StrictStr, Field(max_length=20)] | None = None
    annotation_type: Annotated[StrictStr, Field(max_length=30)] | None = None
    tool_unit_id: Annotated[StrictStr, Field(max_length=30)] | None = None
    class_name: Annotated[StrictStr, Field(max_length=100)]
    geometry: dict[str, Any]
    confidence: float | None = None
    parent_prediction_id: uuid.UUID | None = None
    parent_annotation_id: uuid.UUID | None = None
    track_id: Annotated[StrictStr, Field(max_length=64)] | None = None
    scene_track_id: uuid.UUID | None = None
    temporal_role: Annotated[StrictStr, Field(max_length=16)] | None = None
    lead_time: float | None = None
    was_cancelled: StrictBool | None = None
    ground_truth: StrictBool | None = None
    is_active: StrictBool | None = None
    attributes: dict[str, Any] | None = None
    is_locked: StrictBool | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class VideoTrackerJobFixtureRow(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID
    annotation_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    created_by: uuid.UUID | None = None
    status: Literal["pending_review"] | None = None
    job_kind: Literal["tracking"] | None = None
    model_key: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    direction: Literal["forward", "backward", "bidirectional"]
    from_frame: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
    to_frame: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
    prompt: dict[str, Any] | None = None
    staged_result: dict[str, Any] | None = None
    revision: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)] | None = None
    event_channel: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    celery_task_id: Annotated[StrictStr, Field(max_length=64)] | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error_message: StrictStr | None = None


class TaskDatasetItemLinkFixtureRow(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID
    role: Annotated[
        StrictStr,
        Field(
            min_length=1,
            max_length=50,
            pattern=r"^(primary_lidar|camera_.*)$",
        ),
    ]
    sensor_name: Annotated[StrictStr, Field(max_length=100)] | None = None


class InsertFixtureBatches(_Command):
    operation: Literal["insert_batches"]
    project_id: uuid.UUID
    rows: Annotated[
        list[TaskBatchFixtureRow], Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS)
    ]


class InsertFixtureTasks(_Command):
    operation: Literal["insert_tasks"]
    project_id: uuid.UUID
    rows: Annotated[
        list[TaskFixtureRow], Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS)
    ]


class PatchFixtureTasks(_Command):
    operation: Literal["patch_tasks"]
    project_id: uuid.UUID
    rows: Annotated[
        list[TaskFixturePatch], Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS)
    ]


class PatchFixtureBatches(_Command):
    operation: Literal["patch_batches"]
    project_id: uuid.UUID
    rows: Annotated[
        list[TaskBatchFixturePatch],
        Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS),
    ]


class InsertFixtureAnnotations(_Command):
    operation: Literal["insert_annotations"]
    project_id: uuid.UUID
    rows: Annotated[
        list[AnnotationFixtureRow], Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS)
    ]


class InsertFixtureVideoTrackerJobs(_Command):
    operation: Literal["insert_video_tracker_jobs"]
    project_id: uuid.UUID
    rows: Annotated[
        list[VideoTrackerJobFixtureRow],
        Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS),
    ]


class InsertFixtureTaskDatasetItemLinks(_Command):
    operation: Literal["insert_task_dataset_item_links"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    rows: Annotated[
        list[TaskDatasetItemLinkFixtureRow],
        Field(min_length=1, max_length=MAX_E2E_FIXTURE_ROWS),
    ]


class PatchFixtureFeedbackAnchor(_Command):
    operation: Literal["patch_feedback_anchor"]
    project_id: uuid.UUID
    task_id: uuid.UUID
    feedback_id: uuid.UUID
    anchor_position: dict[str, Any]


class PrepareFilteringRendering(_Command):
    operation: Literal["prepare_filtering_rendering"]
    project_id: uuid.UUID
    low_prediction_id: uuid.UUID
    at_prediction_id: uuid.UUID
    above_prediction_id: uuid.UUID


class FixtureRenderedPrediction(_Closed):
    id: uuid.UUID
    result: list[dict[str, Any]]


FixtureCommand: TypeAlias = Annotated[
    InsertFixtureBatches
    | InsertFixtureTasks
    | PatchFixtureTasks
    | PatchFixtureBatches
    | InsertFixtureAnnotations
    | InsertFixtureVideoTrackerJobs
    | InsertFixtureTaskDatasetItemLinks
    | PatchFixtureFeedbackAnchor
    | PrepareFilteringRendering,
    Field(discriminator="operation"),
]


class FixtureWriteData(_Closed):
    ids: list[uuid.UUID] | None = None
    anchor_position: dict[str, Any] | None = None
    rendered_predictions: list[FixtureRenderedPrediction] | None = None


class FixtureWriteReply(_Closed):
    schema_: Literal["aap-labeling-e2e-fixtures/v1"] = Field(alias="schema")
    operation: StrictStr
    ok: StrictBool
    data: FixtureWriteData


class LabelingE2EFixtureUnavailable(Exception):
    """The Labeling E2E fixture owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(FixtureCommand)
_REPLY_ADAPTER = TypeAdapter(FixtureWriteReply)


def fixture_command_json(request: FixtureCommand) -> str:
    validated = _COMMAND_ADAPTER.validate_python(request)
    payload = validated.model_dump_json(by_alias=True, exclude_unset=True)
    if len(payload.encode("utf-8")) > MAX_E2E_FIXTURE_REQUEST_BYTES:
        raise ValueError("Labeling E2E fixture request exceeds the byte limit")
    return payload


def e2e_fixture_schema() -> dict[str, Any]:
    schema = _COMMAND_ADAPTER.json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": LABELING_E2E_FIXTURE_PROTOCOL,
        "$defs": schema.get("$defs", {}),
        "oneOf": schema["oneOf"],
        "discriminator": schema.get("discriminator"),
        "x-postgresql-functions": {
            LABELING_E2E_FIXTURE_FUNCTION: {
                "arguments": "jsonb",
                "request": "oneOf",
                "returns": "jsonb",
                "reply": "aap-labeling-e2e-fixtures/v1",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
            LABELING_E2E_FILTERING_RENDER_FUNCTION: {
                "arguments": "jsonb",
                "request": {"$ref": "#/$defs/PrepareFilteringRendering"},
                "returns": "jsonb",
                "reply": "aap-labeling-e2e-fixtures/v1",
                "volatility": "volatile",
                "parallel": "unsafe",
            },
        },
        "x-security": {
            "database": "actual database name must end in _test or _e2e",
            "owner": "Labeling definer; runtime execution is test-database only",
            "search_path": "pg_catalog,pg_temp",
        },
        "x-transactions": {
            "commit": False,
            "rollback": False,
            "caller": "the original E2E seed Session and outer transaction",
        },
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def decode_fixture_write_reply(
    request: FixtureCommand, raw: object
) -> FixtureWriteData:
    try:
        if isinstance(raw, (str, bytes, bytearray)):
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        else:
            value = raw
        reply = _REPLY_ADAPTER.validate_json(json.dumps(value, allow_nan=False))
        rendering = isinstance(request, PrepareFilteringRendering)
        expected_rendering_ids = (
            [
                request.low_prediction_id,
                request.at_prediction_id,
                request.above_prediction_id,
            ]
            if rendering
            else None
        )
        if (
            not reply.ok
            or reply.operation != request.operation
            or (
                request.operation == "patch_feedback_anchor"
                and reply.data.anchor_position is None
            )
            or (request.operation != "patch_feedback_anchor" and reply.data.ids is None)
            or (
                rendering
                and (
                    reply.data.rendered_predictions is None
                    or reply.data.ids != expected_rendering_ids
                    or [row.id for row in reply.data.rendered_predictions]
                    != expected_rendering_ids
                )
            )
            or (not rendering and reply.data.rendered_predictions is not None)
        ):
            raise ValueError("Labeling E2E fixture reply does not match its request")
        expected_count = (
            1
            if isinstance(request, PatchFixtureFeedbackAnchor)
            else 3
            if rendering
            else len(request.rows)
        )
        if reply.data.ids is not None and len(reply.data.ids) != expected_count:
            raise ValueError("Labeling E2E fixture owner returned the wrong row count")
        return reply.data
    except (TypeError, ValueError, ValidationError) as exc:
        raise LabelingE2EFixtureUnavailable from exc


__all__ = [
    "LABELING_E2E_FILTERING_RENDER_FUNCTION",
    "LABELING_E2E_FIXTURE_FUNCTION",
    "LABELING_E2E_FIXTURE_PROTOCOL",
    "MAX_E2E_FIXTURE_REQUEST_BYTES",
    "MAX_E2E_FIXTURE_ROWS",
    "AnnotationFixtureRow",
    "FixtureCommand",
    "FixtureWriteData",
    "FixtureRenderedPrediction",
    "InsertFixtureAnnotations",
    "InsertFixtureBatches",
    "InsertFixtureTaskDatasetItemLinks",
    "InsertFixtureTasks",
    "InsertFixtureVideoTrackerJobs",
    "LabelingE2EFixtureUnavailable",
    "PatchFixtureBatches",
    "PatchFixtureFeedbackAnchor",
    "PatchFixtureTasks",
    "PrepareFilteringRendering",
    "TaskBatchFixturePatch",
    "TaskBatchFixtureRow",
    "TaskDatasetItemLinkFixtureRow",
    "TaskFixturePatch",
    "TaskFixtureRow",
    "VideoTrackerJobFixtureRow",
    "decode_fixture_write_reply",
    "e2e_fixture_schema",
    "fixture_command_json",
]
