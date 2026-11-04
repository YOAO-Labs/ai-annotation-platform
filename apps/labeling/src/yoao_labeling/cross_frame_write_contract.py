"""Closed, transaction-local commands for cross-frame Annotation writes."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Literal, TypeAlias

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

from .cross_frame_write_policy import PROPAGATABLE_GEOMETRY_TYPES
from .scene_track_domain_contract import SceneTrackRecord

CROSS_FRAME_WRITE_PROTOCOL = "aap-labeling-cross-frame-writes/v1"
CROSS_FRAME_WRITE_FUNCTION = "aap_labeling_v1.cross_frame_write_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-cross-frame-writes/v1"] = Field(alias="schema")
    operation: StrictStr


class CrossFrameTaskFence(_ClosedModel):
    task_id: uuid.UUID
    project_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)


class CrossFrameSourceFence(_ClosedModel):
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID | None
    expected_version: StrictInt = Field(ge=1)
    geometry_type: StrictStr
    class_name: StrictStr
    expected_track_id: StrictStr | None
    target_track_id: StrictStr | None
    expected_scene_track_id: uuid.UUID | None
    expected_temporal_role: Literal["keyframe", "derived", "sample"]


class CrossFrameTargetWrite(_ClosedModel):
    annotation_id: uuid.UUID
    source_annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    video_segment_id: uuid.UUID | None
    source: Literal["manual", "interpolated"]
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    track_id: StrictStr | None
    attributes: dict[str, object]
    temporal_role: Literal["derived", "sample"]


class CrossFrameSceneTrackEnsure(_ClosedModel):
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr
    class_name: StrictStr
    frames: list[StrictInt]
    interval_source: Literal["derived"]


_CrossFrameWriteOperation: TypeAlias = Literal[
    "propagate", "propagate_batch", "interpolate_range"
]


class CrossFrameWriteCommand(_Command):
    operation: _CrossFrameWriteOperation
    actor_id: uuid.UUID
    task_fences: list[CrossFrameTaskFence]
    source_annotations: list[CrossFrameSourceFence]
    target_annotations: list[CrossFrameTargetWrite]
    scene_tracks: list[CrossFrameSceneTrackEnsure]

    @model_validator(mode="after")
    def check_scope(self):
        tasks = {item.task_id: item for item in self.task_fences}
        sources = {item.annotation_id: item for item in self.source_annotations}
        if not tasks or len(tasks) != len(self.task_fences):
            raise ValueError("task fences must be non-empty and unique")
        if len({item.project_id for item in self.task_fences}) != 1:
            raise ValueError("cross-frame task fences must share a project")
        if not sources or len(sources) != len(self.source_annotations):
            raise ValueError("source annotations must be non-empty and unique")
        if any(
            source.task_id not in tasks
            or source.geometry_type not in PROPAGATABLE_GEOMETRY_TYPES
            for source in self.source_annotations
        ):
            raise ValueError("source annotation fence is outside the task scope")
        if any(
            target.task_id not in tasks
            or target.source_annotation_id not in sources
            or target.project_id != tasks[target.task_id].project_id
            for target in self.target_annotations
        ):
            raise ValueError("target annotation write is outside the task scope")
        if any(
            track.project_id != next(iter(tasks.values())).project_id
            for track in self.scene_tracks
        ):
            raise ValueError("scene track ensure is outside the project scope")
        if self.operation == "propagate":
            if len(sources) != 1 or len(self.target_annotations) != 1:
                raise ValueError("propagate requires one source and one target")
        elif self.operation == "propagate_batch":
            if not self.target_annotations or {
                item.source_annotation_id for item in self.target_annotations
            } != set(sources):
                raise ValueError("propagate_batch requires targets for every source")
        elif len(sources) != 2:
            raise ValueError("interpolate_range requires two endpoint annotations")

        if self.operation in {"propagate", "propagate_batch"}:
            source_by_id = {
                item.annotation_id: item for item in self.source_annotations
            }
            for target in self.target_annotations:
                source = source_by_id[target.source_annotation_id]
                if (target.geometry.get("type") or "") != source.geometry_type:
                    raise ValueError("propagated geometry type must match its source")
                if target.track_id != source.target_track_id:
                    raise ValueError("propagated track id must match its source")
            expected_track_ids = [
                target.track_id
                for target in self.target_annotations
                if target.geometry.get("type") == "box_3d"
            ]
            if [track.track_id for track in self.scene_tracks] != expected_track_ids:
                raise ValueError("SceneTrack ensures must follow box_3d target order")
            expected_tracks = [
                source_by_id[target.source_annotation_id]
                for target in self.target_annotations
                if target.geometry.get("type") == "box_3d"
            ]
            if any(
                track.track_id != source.target_track_id
                or track.class_name != source.class_name
                for track, source in zip(
                    self.scene_tracks, expected_tracks, strict=True
                )
            ):
                raise ValueError("SceneTrack ensure does not match its source")
        else:
            if any(
                source.geometry_type != "box_3d"
                or source.expected_track_id != source.target_track_id
                for source in self.source_annotations
            ):
                raise ValueError("interpolation endpoints must share a box_3d track")
            if len(self.scene_tracks) != 1 or any(
                target.track_id != self.source_annotations[0].target_track_id
                or target.geometry.get("type") != "box_3d"
                for target in self.target_annotations
            ):
                raise ValueError("interpolation writes require their SceneTrack")
            if (
                self.scene_tracks[0].project_id != next(iter(tasks.values())).project_id
                or self.scene_tracks[0].track_id
                != self.source_annotations[0].target_track_id
                or self.scene_tracks[0].class_name
                != self.source_annotations[0].class_name
            ):
                raise ValueError("interpolation SceneTrack ensure does not match")
        return self


class CrossFrameAnnotationSnapshot(_ClosedModel):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID | None
    video_segment_id: uuid.UUID | None
    user_id: uuid.UUID | None
    source: StrictStr
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
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
    attributes: dict[str, object]
    attributes_meta: dict[str, object]
    z_order: StrictInt
    is_locked: StrictBool
    is_hidden: StrictBool
    version: StrictInt
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CrossFrameSourceSnapshot(_ClosedModel):
    annotation_id: uuid.UUID
    version: StrictInt
    track_id: StrictStr | None
    scene_track_id: uuid.UUID | None
    temporal_role: Literal["keyframe", "derived", "sample"]
    updated_at: AwareDatetime
    written_fields: list[
        Literal["track_id", "scene_track_id", "temporal_role", "version", "updated_at"]
    ]


class CrossFrameSceneTrackSnapshot(_ClosedModel):
    track: SceneTrackRecord
    changed: StrictBool


class CrossFrameWriteData(_ClosedModel):
    annotations: list[CrossFrameAnnotationSnapshot]
    sources: list[CrossFrameSourceSnapshot]
    scene_tracks: list[CrossFrameSceneTrackSnapshot]


class CrossFrameWriteReply(_ClosedModel):
    schema_: Literal["aap-labeling-cross-frame-writes/v1"] = Field(alias="schema")
    ok: Literal[True]
    request: CrossFrameWriteCommand
    data: CrossFrameWriteData


class CrossFrameWriteUnavailable(RuntimeError):
    """The Labeling cross-frame write owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(CrossFrameWriteCommand)
_REPLY_ADAPTER = TypeAdapter(CrossFrameWriteReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(
            _json_ready(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def decode_cross_frame_write_command(value: object) -> CrossFrameWriteCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise CrossFrameWriteUnavailable() from None


def cross_frame_write_command(operation: str, **values) -> CrossFrameWriteCommand:
    try:
        return decode_cross_frame_write_command(
            {"schema": CROSS_FRAME_WRITE_PROTOCOL, "operation": operation, **values}
        )
    except CrossFrameWriteUnavailable:
        raise CrossFrameWriteUnavailable() from None


def cross_frame_write_command_json(command: CrossFrameWriteCommand) -> str:
    validated = decode_cross_frame_write_command(
        command.model_dump_json(by_alias=True, exclude_none=False)
    )
    return validated.model_dump_json(by_alias=True, exclude_none=False)


def decode_cross_frame_write_reply(
    request: CrossFrameWriteCommand, value: object
) -> CrossFrameWriteData:
    request = decode_cross_frame_write_command(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise CrossFrameWriteUnavailable() from None
    if reply.request != request:
        raise CrossFrameWriteUnavailable()
    if (
        len(reply.data.sources) != len(request.source_annotations)
        or len(reply.data.annotations) != len(request.target_annotations)
        or len(reply.data.scene_tracks) != len(request.scene_tracks)
    ):
        raise CrossFrameWriteUnavailable()
    if (
        [item.annotation_id for item in reply.data.sources]
        != [item.annotation_id for item in request.source_annotations]
        or [item.id for item in reply.data.annotations]
        != [item.annotation_id for item in request.target_annotations]
        or any(
            snapshot.track.track_id != expected.track_id
            or snapshot.track.project_id != expected.project_id
            or snapshot.track.scene_id != expected.scene_id
            or snapshot.track.class_name != expected.class_name
            for snapshot, expected in zip(
                reply.data.scene_tracks, request.scene_tracks, strict=True
            )
        )
    ):
        raise CrossFrameWriteUnavailable()
    return reply.data


def cross_frame_write_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = CROSS_FRAME_WRITE_PROTOCOL
    schema["x-postgresql-functions"] = {
        CROSS_FRAME_WRITE_FUNCTION: {
            "argument_types": ["jsonb"],
            "returns": "jsonb",
            "owner": "labeling",
            "security_definer": True,
            "caller_transaction_required": True,
            "function_only_login": True,
            "operations": ["propagate", "propagate_batch", "interpolate_range"],
            "table_access": {
                "public.tasks": {"select": ["id", "project_id", "version"]},
                "public.annotations": {
                    "select": [
                        "id",
                        "task_id",
                        "project_id",
                        "video_segment_id",
                        "user_id",
                        "source",
                        "annotation_type",
                        "tool_unit_id",
                        "class_name",
                        "geometry",
                        "confidence",
                        "parent_prediction_id",
                        "parent_annotation_id",
                        "track_id",
                        "scene_track_id",
                        "sensor_dataset_item_id",
                        "sensor_role",
                        "sensor_visibility",
                        "calibration_revision",
                        "calibration_digest",
                        "temporal_role",
                        "lead_time",
                        "was_cancelled",
                        "ground_truth",
                        "is_active",
                        "attributes",
                        "attributes_meta",
                        "z_order",
                        "is_locked",
                        "is_hidden",
                        "version",
                        "created_at",
                        "updated_at",
                    ],
                    "insert": [
                        "id",
                        "task_id",
                        "project_id",
                        "video_segment_id",
                        "user_id",
                        "source",
                        "annotation_type",
                        "tool_unit_id",
                        "class_name",
                        "geometry",
                        "track_id",
                        "temporal_role",
                        "attributes",
                    ],
                    "update": [
                        "track_id",
                        "scene_track_id",
                        "temporal_role",
                        "version",
                        "updated_at",
                    ],
                },
                "public.scene_tracks": {"select": ["id"]},
            },
            "sequence_access": [],
            "public_capabilities": [
                "aap_labeling_v1.scene_track_domain_command(jsonb)"
            ],
        }
    }
    return schema


__all__ = [
    "CROSS_FRAME_WRITE_FUNCTION",
    "CROSS_FRAME_WRITE_PROTOCOL",
    "CrossFrameAnnotationSnapshot",
    "CrossFrameSceneTrackSnapshot",
    "CrossFrameSceneTrackEnsure",
    "CrossFrameSourceFence",
    "CrossFrameSourceSnapshot",
    "CrossFrameTaskFence",
    "CrossFrameTargetWrite",
    "CrossFrameWriteCommand",
    "CrossFrameWriteData",
    "CrossFrameWriteUnavailable",
    "cross_frame_write_command",
    "cross_frame_write_command_json",
    "cross_frame_write_schema",
    "decode_cross_frame_write_command",
    "decode_cross_frame_write_reply",
]
