"""Closed wire contract for point-cloud and video quality persistence."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)


SPATIAL_QUALITY_PROTOCOL = "aap-labeling-spatial-quality/v1"
SPATIAL_QUALITY_FUNCTION = "aap_labeling_v1.spatial_quality_command(jsonb)"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Request(_Closed):
    protocol: Literal["aap-labeling-spatial-quality/v1"] = Field(alias="schema")
    action: StrictStr


class ReadPointSource(_Request):
    action: Literal["read_point_source"]
    project_id: uuid.UUID


class ReadPointDetails(_Request):
    action: Literal["read_point_details"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID] = Field(max_length=5000)
    scene_ids: list[uuid.UUID] = Field(max_length=5000)
    track_ids: list[uuid.UUID] = Field(max_length=20_000)
    annotation_ids: list[uuid.UUID] = Field(max_length=5000)
    annotation_scope: Literal["task_ids", "annotation_ids", "all"]


class ReadQualityFacts(_Request):
    action: Literal["read_quality_facts"]
    project_id: uuid.UUID | None = None
    annotation_ids: list[uuid.UUID] = Field(max_length=20_000)
    task_ids: list[uuid.UUID] = Field(max_length=5000)
    track_ids: list[uuid.UUID] = Field(max_length=20_000)
    run_ids: list[uuid.UUID] = Field(max_length=20_000)


class ReadVideoAnnotations(_Request):
    action: Literal["read_video_annotations"]
    task_id: uuid.UUID
    segment_ids: list[uuid.UUID] = Field(min_length=1, max_length=2)


class ReadVideoBoundary(_Request):
    action: Literal["read_video_boundary"]
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID
    left_segment_id: uuid.UUID
    right_segment_id: uuid.UUID


class ReadVideoTaskSegments(_Request):
    action: Literal["read_video_task_segments"]
    task_id: uuid.UUID


class ReadVideoChapters(_Request):
    action: Literal["read_video_chapters"]
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID


class ReadVideoTaskAnnotations(_Request):
    action: Literal["read_video_task_annotations"]
    task_id: uuid.UUID


class QualityCommand(_Request):
    action: Literal[
        "find_point_run",
        "create_point_run",
        "get_point_run",
        "update_point_run",
        "reconcile_point_run",
        "upsert_point_issue",
        "get_point_issue",
        "lock_point_issue",
        "list_point_issues",
        "mark_point_issues_stale",
        "decide_point_issue",
        "list_point_evaluation_samples",
        "create_point_evaluation",
        "get_point_evaluation",
        "list_point_evaluations",
        "promote_point_evaluation",
        "find_video_run",
        "create_video_run",
        "get_video_run",
        "lock_video_run",
        "list_video_runs",
        "list_accepted_video_runs",
        "update_video_run",
        "replace_video_issues",
        "list_video_issues",
        "mark_video_runs_stale",
    ]
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    issue_id: uuid.UUID | None = None
    evaluation_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    async_job_id: uuid.UUID | None = None
    left_segment_id: uuid.UUID | None = None
    right_segment_id: uuid.UUID | None = None
    segment_ids: list[uuid.UUID] | None = Field(default=None, max_length=5000)
    status: StrictStr | None = None
    statuses: list[StrictStr] | None = Field(default=None, max_length=8)
    progress_pct: StrictInt | None = Field(default=None, ge=0, le=100)
    scope_json: dict[str, object] | None = None
    config_revision: StrictInt | None = Field(default=None, ge=1)
    config_digest: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_snapshot: dict[str, object] | None = None
    source_snapshot: list[dict[str, object]] | None = None
    source_snapshot_digest: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    singleflight_key: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    summary: dict[str, object] | None = None
    error_message: StrictStr | None = Field(default=None, max_length=4000)
    started_at: str | None = None
    completed_at: str | None = None
    stale_at: str | None = None
    accepted_by_id: uuid.UUID | None = None
    accepted_at: str | None = None
    code: StrictStr | None = None
    dedupe_key: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scene_id: uuid.UUID | None = None
    annotation_id: uuid.UUID | None = None
    annotation_version: StrictInt | None = Field(default=None, ge=1)
    scene_track_id: uuid.UUID | None = None
    track_revision: StrictInt | None = Field(default=None, ge=1)
    related_annotation_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=20_000
    )
    source_versions: dict[str, StrictInt] | None = None
    class_name: StrictStr | None = None
    rule_version: StrictInt | None = Field(default=None, ge=1)
    severity: StrictStr | None = None
    severity_rank: StrictInt | None = Field(default=None, ge=0, le=2)
    frame_start: StrictInt | None = Field(default=None, ge=0)
    frame_end: StrictInt | None = Field(default=None, ge=0)
    metric: dict[str, object] | None = None
    threshold: dict[str, object] | None = None
    evidence: dict[str, object] | None = None
    locator: dict[str, object] | None = None
    suggested_command: StrictStr | None = None
    candidate_config_digest: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    candidate_config_snapshot: dict[str, object] | None = None
    baseline_config_revision: StrictInt | None = Field(default=None, ge=1)
    baseline_config_digest: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    baseline_config_snapshot: dict[str, object] | None = None
    cutoff_at: str | None = None
    sample_count: StrictInt | None = Field(default=None, ge=0)
    sample_snapshot: list[dict[str, object]] | None = None
    gate_status: StrictStr | None = None
    gate_reasons: list[dict[str, object]] | None = None
    promoted_by_id: uuid.UUID | None = None
    promoted_at: str | None = None
    promoted_config_revision: StrictInt | None = Field(default=None, ge=1)
    review_status: StrictStr | None = None
    refresh_candidates: StrictBool = False
    resolution_reason: StrictStr | None = None
    review_verdict: StrictStr | None = None
    review_note: StrictStr | None = None
    limit: StrictInt | None = Field(default=None, ge=1, le=100_000)
    offset: StrictInt | None = Field(default=None, ge=0, le=100_000)
    severity_filter: StrictStr | None = None
    code_filter: StrictStr | None = None
    scene_filter: uuid.UUID | None = None
    task_filter: uuid.UUID | None = None
    annotation_filter: uuid.UUID | None = None
    track_filter: uuid.UUID | None = None
    frame_filter: StrictInt | None = Field(default=None, ge=0)
    allowed_task_ids: list[uuid.UUID] | None = Field(default=None, max_length=5000)
    input_digest: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sampling_digest: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_snapshot: list[dict[str, object]] | None = None
    metrics: dict[str, object] | None = None
    pairs: list[dict[str, object]] | None = None
    issue_rows: list[dict[str, object]] | None = None
    decision_rows: list[dict[str, object]] | None = None
    stale_issue_ids: list[uuid.UUID] | None = Field(default=None, max_length=20_000)
    stale_run_ids: list[uuid.UUID] | None = Field(default=None, max_length=20_000)
    dataset_item_id: uuid.UUID | None = None
    new_run_id: uuid.UUID | None = None
    new_issue_id: uuid.UUID | None = None
    new_evaluation_id: uuid.UUID | None = None
    expected_task_id: uuid.UUID | None = None
    resolved_at: str | None = None
    reviewed_at: str | None = None

    def model_post_init(self, __context: object) -> None:
        fields = set(self.model_fields_set) - {"protocol", "action"}
        allowed, required = _ACTION_FIELDS[self.action]
        if not required <= fields or not fields <= allowed:
            raise ValueError("invalid spatial quality command fields")
        if (
            self.action == "list_point_issues"
            and "allowed_task_ids" in fields
            and self.allowed_task_ids is None
        ):
            raise ValueError("allowed task ids must be an array when provided")


SpatialQualityRequest: TypeAlias = Annotated[
    ReadPointSource
    | ReadPointDetails
    | ReadQualityFacts
    | ReadVideoAnnotations
    | ReadVideoBoundary
    | ReadVideoTaskSegments
    | ReadVideoChapters
    | ReadVideoTaskAnnotations
    | QualityCommand,
    Field(discriminator="action"),
]


class SpatialQualityReply(_Closed):
    protocol: Literal["aap-labeling-spatial-quality/v1"] = Field(alias="schema")
    action: StrictStr
    data: dict[str, object]


class SpatialQualityUnavailable(Exception):
    """The Labeling spatial quality owner returned an invalid reply."""


_ACTION_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "read_point_source": ({"project_id"}, {"project_id"}),
    "read_point_details": (
        {
            "project_id",
            "task_ids",
            "scene_ids",
            "track_ids",
            "annotation_ids",
            "annotation_scope",
        },
        {
            "project_id",
            "task_ids",
            "scene_ids",
            "track_ids",
            "annotation_ids",
            "annotation_scope",
        },
    ),
    "read_quality_facts": (
        {"project_id", "annotation_ids", "task_ids", "track_ids", "run_ids"},
        {"annotation_ids", "task_ids", "track_ids", "run_ids"},
    ),
    "read_video_annotations": ({"task_id", "segment_ids"}, {"task_id", "segment_ids"}),
    "read_video_boundary": (
        {"task_id", "dataset_item_id", "left_segment_id", "right_segment_id"},
        {"task_id", "dataset_item_id", "left_segment_id", "right_segment_id"},
    ),
    "read_video_task_segments": ({"task_id"}, {"task_id"}),
    "read_video_chapters": (
        {"task_id", "dataset_item_id"},
        {"task_id", "dataset_item_id"},
    ),
    "read_video_task_annotations": ({"task_id"}, {"task_id"}),
    "find_point_run": (
        {"project_id", "singleflight_key", "statuses"},
        {"project_id", "singleflight_key", "statuses"},
    ),
    "create_point_run": (
        {
            "new_run_id",
            "project_id",
            "async_job_id",
            "actor_id",
            "scope_json",
            "config_revision",
            "config_digest",
            "config_snapshot",
            "source_snapshot",
            "source_snapshot_digest",
            "singleflight_key",
            "summary",
        },
        {
            "new_run_id",
            "project_id",
            "async_job_id",
            "actor_id",
            "scope_json",
            "config_revision",
            "config_digest",
            "config_snapshot",
            "source_snapshot",
            "source_snapshot_digest",
            "singleflight_key",
            "summary",
        },
    ),
    "get_point_run": ({"run_id"}, {"run_id"}),
    "update_point_run": (
        {
            "run_id",
            "status",
            "progress_pct",
            "summary",
            "error_message",
            "started_at",
            "completed_at",
        },
        {"run_id", "status"},
    ),
    "reconcile_point_run": (
        {"async_job_id", "status", "completed_at", "error_message"},
        {"async_job_id", "status", "completed_at"},
    ),
    "upsert_point_issue": (
        {
            "new_issue_id",
            "run_id",
            "project_id",
            "scene_id",
            "task_id",
            "annotation_id",
            "annotation_version",
            "scene_track_id",
            "track_revision",
            "related_annotation_ids",
            "source_versions",
            "class_name",
            "code",
            "rule_version",
            "severity",
            "severity_rank",
            "frame_start",
            "frame_end",
            "metric",
            "threshold",
            "evidence",
            "locator",
            "suggested_command",
            "dedupe_key",
        },
        {
            "new_issue_id",
            "run_id",
            "project_id",
            "scene_id",
            "related_annotation_ids",
            "source_versions",
            "code",
            "rule_version",
            "severity",
            "severity_rank",
            "metric",
            "threshold",
            "evidence",
            "locator",
            "dedupe_key",
        },
    ),
    "get_point_issue": ({"issue_id"}, {"issue_id"}),
    "lock_point_issue": ({"issue_id"}, {"issue_id"}),
    "list_point_issues": (
        {
            "project_id",
            "review_status",
            "refresh_candidates",
            "limit",
            "offset",
            "severity_filter",
            "code_filter",
            "scene_filter",
            "task_filter",
            "annotation_filter",
            "track_filter",
            "frame_filter",
            "allowed_task_ids",
        },
        {"project_id", "limit", "offset"},
    ),
    "mark_point_issues_stale": ({"stale_issue_ids"}, {"stale_issue_ids"}),
    "decide_point_issue": (
        {
            "issue_id",
            "actor_id",
            "review_status",
            "expected_task_id",
            "resolution_reason",
            "review_verdict",
            "review_note",
            "resolved_at",
            "reviewed_at",
        },
        {"issue_id", "actor_id", "review_status", "expected_task_id", "resolved_at"},
    ),
    "list_point_evaluation_samples": (
        {"project_id", "cutoff_at", "limit"},
        {"project_id", "cutoff_at", "limit"},
    ),
    "create_point_evaluation": (
        {
            "new_evaluation_id",
            "project_id",
            "actor_id",
            "baseline_config_revision",
            "baseline_config_digest",
            "baseline_config_snapshot",
            "candidate_config_digest",
            "candidate_config_snapshot",
            "cutoff_at",
            "sample_count",
            "sample_snapshot",
            "summary",
            "gate_status",
            "gate_reasons",
        },
        {
            "new_evaluation_id",
            "project_id",
            "actor_id",
            "baseline_config_revision",
            "baseline_config_digest",
            "baseline_config_snapshot",
            "candidate_config_digest",
            "candidate_config_snapshot",
            "cutoff_at",
            "sample_count",
            "sample_snapshot",
            "summary",
            "gate_status",
            "gate_reasons",
        },
    ),
    "get_point_evaluation": ({"evaluation_id"}, {"evaluation_id"}),
    "list_point_evaluations": (
        {"project_id", "limit", "offset"},
        {"project_id", "limit", "offset"},
    ),
    "promote_point_evaluation": (
        {"evaluation_id", "actor_id", "promoted_config_revision", "promoted_at"},
        {"evaluation_id", "actor_id", "promoted_config_revision", "promoted_at"},
    ),
    "find_video_run": (
        {"task_id", "left_segment_id", "right_segment_id", "input_digest", "statuses"},
        {"task_id", "left_segment_id", "right_segment_id", "input_digest", "statuses"},
    ),
    "create_video_run": (
        {
            "new_run_id",
            "task_id",
            "left_segment_id",
            "right_segment_id",
            "async_job_id",
            "actor_id",
            "input_snapshot",
            "input_digest",
            "sampling_digest",
            "metrics",
        },
        {
            "new_run_id",
            "task_id",
            "left_segment_id",
            "right_segment_id",
            "async_job_id",
            "actor_id",
            "input_snapshot",
            "input_digest",
            "sampling_digest",
            "metrics",
        },
    ),
    "get_video_run": ({"run_id"}, {"run_id"}),
    "lock_video_run": ({"run_id"}, {"run_id"}),
    "list_video_runs": ({"task_id"}, {"task_id"}),
    "list_accepted_video_runs": ({"task_id", "run_id"}, {"task_id", "run_id"}),
    "update_video_run": (
        {
            "run_id",
            "status",
            "progress_pct",
            "error_message",
            "started_at",
            "completed_at",
            "stale_at",
            "accepted_by_id",
            "accepted_at",
            "metrics",
            "pairs",
        },
        {"run_id", "status"},
    ),
    "replace_video_issues": ({"run_id", "issue_rows"}, {"run_id", "issue_rows"}),
    "list_video_issues": ({"run_id"}, {"run_id"}),
    "mark_video_runs_stale": ({"segment_ids"}, {"segment_ids"}),
}


_REQUEST_ADAPTER = TypeAdapter(SpatialQualityRequest)
_REPLY_ADAPTER = TypeAdapter(SpatialQualityReply)
_REPLY_KEYS: dict[str, frozenset[str]] = {
    "read_point_source": frozenset({"tasks", "track_scene_ids", "dataset_ids"}),
    "read_point_details": frozenset(
        {"annotations", "camera_annotations", "tracks", "intervals", "links"}
    ),
    "read_quality_facts": frozenset(
        {"annotations", "tasks", "tracks", "links", "runs"}
    ),
    "read_video_annotations": frozenset({"annotations"}),
    "read_video_task_annotations": frozenset({"annotations"}),
    "read_video_boundary": frozenset({"segments", "segment_count"}),
    "read_video_task_segments": frozenset({"segments"}),
    "read_video_chapters": frozenset({"chapters"}),
    "find_point_run": frozenset({"row"}),
    "create_point_run": frozenset({"row"}),
    "get_point_run": frozenset({"row"}),
    "update_point_run": frozenset({"row"}),
    "reconcile_point_run": frozenset({"changed"}),
    "upsert_point_issue": frozenset({"row"}),
    "get_point_issue": frozenset({"row"}),
    "lock_point_issue": frozenset({"row"}),
    "list_point_issues": frozenset({"rows", "total"}),
    "mark_point_issues_stale": frozenset({"changed"}),
    "decide_point_issue": frozenset({"row"}),
    "list_point_evaluation_samples": frozenset({"rows"}),
    "create_point_evaluation": frozenset({"row"}),
    "get_point_evaluation": frozenset({"row"}),
    "list_point_evaluations": frozenset({"rows", "total"}),
    "promote_point_evaluation": frozenset({"row"}),
    "find_video_run": frozenset({"row"}),
    "create_video_run": frozenset({"row"}),
    "get_video_run": frozenset({"row"}),
    "lock_video_run": frozenset({"row"}),
    "list_video_runs": frozenset({"rows"}),
    "list_accepted_video_runs": frozenset({"rows"}),
    "update_video_run": frozenset({"row"}),
    "replace_video_issues": frozenset({"stored"}),
    "list_video_issues": frozenset({"rows"}),
    "mark_video_runs_stale": frozenset({"changed"}),
}


def spatial_quality_request(action: str, **values: object) -> SpatialQualityRequest:
    payload = {"schema": SPATIAL_QUALITY_PROTOCOL, "action": action, **values}
    try:
        return _REQUEST_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ValueError("invalid Labeling spatial quality request") from exc


def spatial_quality_request_json(request: SpatialQualityRequest) -> str:
    return request.model_dump_json(by_alias=True, exclude_unset=True)


def spatial_quality_json_schema() -> dict[str, object]:
    schema = deepcopy(_REQUEST_ADAPTER.json_schema())
    definitions = schema["$defs"]
    source_models = [
        ("read_point_source", "ReadPointSource"),
        ("read_point_details", "ReadPointDetails"),
        ("read_quality_facts", "ReadQualityFacts"),
        ("read_video_annotations", "ReadVideoAnnotations"),
        ("read_video_boundary", "ReadVideoBoundary"),
        ("read_video_task_segments", "ReadVideoTaskSegments"),
        ("read_video_chapters", "ReadVideoChapters"),
        ("read_video_task_annotations", "ReadVideoTaskAnnotations"),
    ]
    variants = [{"$ref": f"#/$defs/{name}"} for _, name in source_models]
    mapping = {action: f"#/$defs/{name}" for action, name in source_models}
    source_actions = set(mapping)
    for action, (allowed, required) in _ACTION_FIELDS.items():
        if action in source_actions:
            continue
        name = "QualityCommand_" + action
        properties = {
            key: {"$ref": f"#/$defs/QualityCommand/properties/{key}"}
            for key in sorted(allowed | {"schema", "action"})
        }
        if action == "list_point_issues":
            properties["allowed_task_ids"] = {
                "allOf": [
                    {"$ref": "#/$defs/QualityCommand/properties/allowed_task_ids"},
                    {"not": {"type": "null"}},
                ]
            }
        properties["action"] = {"const": action, "type": "string"}
        definitions[name] = {
            "type": "object",
            "properties": properties,
            "required": ["schema", "action", *sorted(required)],
            "additionalProperties": False,
        }
        variants.append({"$ref": f"#/$defs/{name}"})
        mapping[action] = f"#/$defs/{name}"
    schema["oneOf"] = variants
    schema["discriminator"] = {"propertyName": "action", "mapping": mapping}
    schema["x-response-data-keys"] = {
        action: sorted(keys) for action, keys in _REPLY_KEYS.items()
    }
    return schema


def decode_spatial_quality_reply(
    request: SpatialQualityRequest, value: object
) -> dict[str, object]:
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpatialQualityUnavailable from None
    if not isinstance(value, Mapping):
        raise SpatialQualityUnavailable
    try:
        reply = _REPLY_ADAPTER.validate_python(value)
    except ValidationError:
        raise SpatialQualityUnavailable from None
    if reply.action != request.action:
        raise SpatialQualityUnavailable
    if set(reply.data) != _REPLY_KEYS.get(request.action):
        raise SpatialQualityUnavailable
    for name in (
        "tasks",
        "track_scene_ids",
        "dataset_ids",
        "annotations",
        "camera_annotations",
        "tracks",
        "intervals",
        "links",
        "runs",
        "segments",
        "chapters",
        "rows",
    ):
        if name in reply.data:
            values = reply.data[name]
            if not isinstance(values, list):
                raise SpatialQualityUnavailable
            if name not in {"track_scene_ids", "dataset_ids"} and any(
                not isinstance(item, dict) for item in values
            ):
                raise SpatialQualityUnavailable
    if (
        "row" in reply.data
        and reply.data["row"] is not None
        and not isinstance(reply.data["row"], dict)
    ):
        raise SpatialQualityUnavailable
    if "total" in reply.data and (
        type(reply.data["total"]) is not int or reply.data["total"] < 0
    ):
        raise SpatialQualityUnavailable
    if "segment_count" in reply.data and (
        type(reply.data["segment_count"]) is not int or reply.data["segment_count"] < 0
    ):
        raise SpatialQualityUnavailable
    if "changed" in reply.data and type(reply.data["changed"]) not in {bool, int}:
        raise SpatialQualityUnavailable
    if "stored" in reply.data and type(reply.data["stored"]) is not bool:
        raise SpatialQualityUnavailable
    return reply.data
