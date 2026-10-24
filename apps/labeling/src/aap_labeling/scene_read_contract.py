"""Closed read requests and replies for Labeling scene data."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
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
)


SCENE_READ_PROTOCOL = "aap-labeling-scene-read/v1"
SCENE_READ_FUNCTION = "aap_labeling_v1.scene_read_query(jsonb)"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Request(_Closed):
    protocol: Literal["aap-labeling-scene-read/v1"] = Field(alias="schema")
    action: StrictStr


class ResolvePrimaryLink(_Request):
    action: Literal["resolve_primary_link"]
    task_id: uuid.UUID


class ResolveTaskFrames(_Request):
    action: Literal["resolve_task_frames"]
    task_ids: list[uuid.UUID]


class SceneFrameTasks(_Request):
    action: Literal["scene_frame_tasks"]
    scene_id: uuid.UUID


class TimelineTasks(_Request):
    action: Literal["timeline_tasks"]
    project_id: uuid.UUID
    item_ids: list[uuid.UUID]


class TimelineAnnotationSummaries(_Request):
    action: Literal["timeline_annotation_summaries"]
    task_ids: list[uuid.UUID]
    track_id: StrictStr | None


class LoadCommandTrack(_Request):
    action: Literal["load_command_track"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr = Field(min_length=1, max_length=64)


class LoadTrackDetail(_Request):
    action: Literal["load_track_detail"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr = Field(min_length=1, max_length=64)


class LoadTimelineTrack(_Request):
    action: Literal["load_timeline_track"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr = Field(min_length=1, max_length=64)


class LoadSceneDiagnostics(_Request):
    action: Literal["load_scene_diagnostics"]
    project_id: uuid.UUID
    scene_id: uuid.UUID


class LoadSceneTrackCache(_Request):
    action: Literal["load_scene_track_cache"]
    scene_track_id: uuid.UUID


class LoadSceneCameraCache(_Request):
    action: Literal["load_scene_camera_cache"]
    scene_track_id: uuid.UUID
    annotation_ids: list[uuid.UUID]


class TrackMemberRows(_Request):
    action: Literal["read_track_members", "lock_track_members"]
    project_id: uuid.UUID
    track_ids: list[StrictStr] = Field(min_length=1, max_length=80)
    limit: StrictInt = Field(ge=1, le=20_001)


class CandidateTrackCounts(_Request):
    action: Literal["candidate_track_counts"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]
    class_name: StrictStr
    exclude_track_id: StrictStr
    limit: StrictInt = Field(ge=1, le=81)


SceneReadRequest: TypeAlias = Annotated[
    ResolvePrimaryLink
    | ResolveTaskFrames
    | SceneFrameTasks
    | TimelineTasks
    | TimelineAnnotationSummaries
    | LoadCommandTrack
    | LoadTrackDetail
    | LoadTimelineTrack
    | LoadSceneDiagnostics
    | LoadSceneTrackCache
    | LoadSceneCameraCache
    | TrackMemberRows
    | CandidateTrackCounts,
    Field(discriminator="action"),
]


class TaskSceneFrame(_Closed):
    task_id: uuid.UUID
    item_id: uuid.UUID | None
    scene_id: uuid.UUID | None
    scene_name: StrictStr | None
    frame_index: StrictInt | None


class SceneFrameTask(_Closed):
    frame_index: StrictInt
    task_id: uuid.UUID


class TimelineTask(_Closed):
    item_id: uuid.UUID
    task_id: uuid.UUID
    status: StrictStr


class TimelineAnnotationSummary(_Closed):
    task_id: uuid.UUID
    annotation_count: StrictInt
    selected_annotation_id: uuid.UUID | None
    selected_source: StrictStr | None
    selected_class_name: StrictStr | None
    selected_temporal_role: StrictStr | None


class SceneTrackRecord(_Closed):
    id: uuid.UUID
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr
    class_name: StrictStr
    presence_mode: StrictStr
    attributes: object
    attributes_meta: object
    revision: StrictInt
    retired_at: AwareDatetime | None


class SceneTrackCacheRecord(_Closed):
    id: uuid.UUID
    class_name: StrictStr
    revision: StrictInt
    updated_at: AwareDatetime


class SceneCameraCacheRecord(_Closed):
    id: uuid.UUID
    class_name: StrictStr
    version: StrictInt
    updated_at: AwareDatetime


class SceneTrackIntervalRecord(_Closed):
    id: uuid.UUID
    scene_track_id: uuid.UUID
    start_frame: StrictInt
    end_frame: StrictInt | None
    source: StrictStr
    version: StrictInt


class SceneAnnotationRecord(_Closed):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID | None
    source: StrictStr
    annotation_type: StrictStr
    class_name: StrictStr
    geometry: object
    track_id: StrictStr | None
    scene_track_id: uuid.UUID | None
    sensor_role: StrictStr | None
    temporal_role: StrictStr
    is_active: StrictBool
    was_cancelled: StrictBool
    is_locked: StrictBool
    is_hidden: StrictBool
    version: StrictInt
    created_at: AwareDatetime
    frame_index: StrictInt | None


class CandidateTrackCount(_Closed):
    track_id: StrictStr
    member_count: StrictInt


class SceneReadData(_Closed):
    primary_item_id: uuid.UUID | None
    frames: list[TaskSceneFrame]
    frame_tasks: list[SceneFrameTask]
    timeline_tasks: list[TimelineTask]
    annotation_summaries: list[TimelineAnnotationSummary]
    track: SceneTrackRecord | None
    tracks: list[SceneTrackRecord]
    track_cache: SceneTrackCacheRecord | None
    camera_members: list[SceneCameraCacheRecord]
    intervals: list[SceneTrackIntervalRecord]
    members: list[SceneAnnotationRecord]
    candidate_counts: list[CandidateTrackCount]


class SceneReadReply(_Closed):
    protocol: Literal["aap-labeling-scene-read/v1"] = Field(alias="schema")
    action: StrictStr
    request: SceneReadRequest
    data: SceneReadData


class SceneReadUnavailable(Exception):
    """The Labeling Scene read owner returned an invalid reply."""


_REQUEST_ADAPTER = TypeAdapter(SceneReadRequest)
_REPLY_ADAPTER = TypeAdapter(SceneReadReply)


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(
        _json_ready(value),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")


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


def decode_scene_read_request(value: object) -> SceneReadRequest:
    try:
        raw = _json_bytes(value)
        json.loads(
            raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        return _REQUEST_ADAPTER.validate_json(raw)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneReadUnavailable() from None


def scene_read_request(action: str, **values: object) -> SceneReadRequest:
    return decode_scene_read_request(
        {"schema": SCENE_READ_PROTOCOL, "action": action, **values}
    )


def scene_read_request_json(value: object) -> str:
    request = decode_scene_read_request(value)
    return request.model_dump_json(by_alias=True, exclude_none=False)


def decode_scene_read_reply(request: object, value: object) -> SceneReadData:
    request = decode_scene_read_request(request)
    try:
        raw = _json_bytes(value)
        json.loads(
            raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        reply = _REPLY_ADAPTER.validate_json(raw)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneReadUnavailable() from None
    if reply.action != request.action or reply.request != request:
        raise SceneReadUnavailable()
    data = reply.data
    if request.action == "resolve_task_frames":
        allowed = set(request.task_ids)
        if any(row.task_id not in allowed for row in data.frames):
            raise SceneReadUnavailable()
    elif request.action == "timeline_tasks":
        allowed = set(request.item_ids)
        if any(row.item_id not in allowed for row in data.timeline_tasks):
            raise SceneReadUnavailable()
    elif request.action == "timeline_annotation_summaries":
        allowed = set(request.task_ids)
        if any(row.task_id not in allowed for row in data.annotation_summaries):
            raise SceneReadUnavailable()
    elif request.action in {
        "load_command_track",
        "load_track_detail",
        "load_timeline_track",
    }:
        track = data.track
        if track is not None and (
            track.project_id != request.project_id
            or track.scene_id != request.scene_id
            or track.track_id != request.track_id
        ):
            raise SceneReadUnavailable()
        if track is None and (data.intervals or data.members):
            raise SceneReadUnavailable()
        if track is not None and (
            any(row.scene_track_id != track.id for row in data.intervals)
            or any(row.scene_track_id != track.id for row in data.members)
        ):
            raise SceneReadUnavailable()
    elif request.action == "load_scene_diagnostics":
        if any(
            track.project_id != request.project_id or track.scene_id != request.scene_id
            for track in data.tracks
        ):
            raise SceneReadUnavailable()
        frame_by_task = {row.task_id: row.frame_index for row in data.frame_tasks}
        if any(
            member.project_id != request.project_id
            or member.task_id not in frame_by_task
            or member.frame_index != frame_by_task[member.task_id]
            for member in data.members
        ):
            raise SceneReadUnavailable()
    elif request.action in {"read_track_members", "lock_track_members"}:
        allowed = set(request.track_ids)
        if any(
            row.project_id != request.project_id or row.track_id not in allowed
            for row in data.members
        ):
            raise SceneReadUnavailable()
    elif request.action == "candidate_track_counts":
        if any(
            row.track_id == request.exclude_track_id or row.member_count < 1
            for row in data.candidate_counts
        ):
            raise SceneReadUnavailable()
    elif request.action == "load_scene_track_cache":
        if (
            data.track_cache is not None
            and data.track_cache.id != request.scene_track_id
        ):
            raise SceneReadUnavailable()
    elif request.action == "load_scene_camera_cache":
        allowed = set(request.annotation_ids)
        if any(row.id not in allowed for row in data.camera_members):
            raise SceneReadUnavailable()
    return data


def scene_read_schema() -> dict[str, object]:
    schema = TypeAdapter(SceneReadRequest | SceneReadReply).json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCENE_READ_PROTOCOL
    schema["x-postgresql-functions"] = {
        SCENE_READ_FUNCTION: {
            "argument_types": ["jsonb"],
            "returns": "jsonb",
            "owner": "labeling",
            "security_definer": True,
            "caller_transaction_required": True,
            "dependencies": ["aap_assets_v1.spatial_item_references"],
            "actions": [
                "resolve_primary_link",
                "resolve_task_frames",
                "scene_frame_tasks",
                "timeline_tasks",
                "timeline_annotation_summaries",
                "load_command_track",
                "load_track_detail",
                "load_timeline_track",
                "load_scene_diagnostics",
                "load_scene_track_cache",
                "load_scene_camera_cache",
                "read_track_members",
                "lock_track_members",
                "candidate_track_counts",
            ],
            "fixed_targets": [
                "public.tasks",
                "public.task_dataset_item_links",
                "public.annotations",
                "public.scene_tracks",
                "public.scene_track_intervals",
                "aap_assets_v1.spatial_item_references",
            ],
            "table_access": {
                "public.tasks": {
                    "select": ["id", "project_id", "status", "dataset_item_id"]
                },
                "public.task_dataset_item_links": {
                    "select": ["task_id", "dataset_item_id", "role"]
                },
                "public.annotations": {
                    "select": [
                        "id",
                        "task_id",
                        "project_id",
                        "source",
                        "annotation_type",
                        "class_name",
                        "geometry",
                        "track_id",
                        "scene_track_id",
                        "sensor_role",
                        "temporal_role",
                        "is_active",
                        "was_cancelled",
                        "is_locked",
                        "is_hidden",
                        "version",
                        "created_at",
                        "updated_at",
                    ]
                },
                "public.scene_tracks": {
                    "select": [
                        "id",
                        "project_id",
                        "scene_id",
                        "track_id",
                        "class_name",
                        "presence_mode",
                        "attributes",
                        "attributes_meta",
                        "revision",
                        "retired_at",
                        "updated_at",
                    ]
                },
                "public.scene_track_intervals": {
                    "select": [
                        "id",
                        "scene_track_id",
                        "start_frame",
                        "end_frame",
                        "source",
                        "version",
                    ]
                },
                "aap_assets_v1.spatial_item_references": {
                    "select": ["item_id", "scene_id", "frame_index", "scene_name"]
                },
            },
            "sequences": [],
        }
    }
    return schema


__all__ = [
    "SCENE_READ_FUNCTION",
    "SCENE_READ_PROTOCOL",
    "CandidateTrackCount",
    "SceneAnnotationRecord",
    "SceneCameraCacheRecord",
    "SceneFrameTask",
    "SceneReadData",
    "SceneReadUnavailable",
    "SceneTrackIntervalRecord",
    "SceneTrackCacheRecord",
    "SceneTrackRecord",
    "TaskSceneFrame",
    "TimelineAnnotationSummary",
    "TimelineTask",
    "decode_scene_read_reply",
    "decode_scene_read_request",
    "scene_read_request",
    "scene_read_request_json",
    "scene_read_schema",
]
