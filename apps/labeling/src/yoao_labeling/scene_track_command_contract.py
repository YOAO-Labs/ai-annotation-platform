"""Closed wire contract for Labeling SceneTrack lifecycle commands."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
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


SCENE_TRACK_COMMAND_PROTOCOL = "aap-labeling-scene-track-commands/v1"
SCENE_TRACK_COMMAND_FUNCTION = "aap_labeling_v1.scene_track_command(jsonb)"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
TrackId = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    protocol: Literal["aap-labeling-scene-track-commands/v1"] = Field(alias="schema")
    action: StrictStr


class SceneTrackCommandInput(_ClosedModel):
    kind: Literal["split", "merge", "mark_absent", "resume", "terminate"]
    track_id: TrackId
    secondary_track_id: TrackId | None = None
    frame_index: StrictInt | None = Field(default=None, ge=0)
    resume_frame: StrictInt | None = Field(default=None, ge=0)
    source_annotation_id: uuid.UUID | None
    confirm_member_deactivation: StrictBool

    @model_validator(mode="after")
    def check_kind_fields(self):
        if self.kind == "merge":
            if (
                self.secondary_track_id is None
                or self.secondary_track_id == self.track_id
            ):
                raise ValueError("Merge requires two different SceneTracks")
        elif self.secondary_track_id is not None:
            raise ValueError("Secondary SceneTrack is accepted only for merge")
        if (
            self.kind in {"split", "mark_absent", "terminate"}
            and self.frame_index is None
        ):
            raise ValueError("Frame index is required for this SceneTrack command")
        if self.kind == "resume" and (
            self.resume_frame is None or self.source_annotation_id is None
        ):
            raise ValueError("Resume requires a frame and source Annotation")
        return self


class IntervalSnapshot(_ClosedModel):
    id: uuid.UUID
    start_frame: StrictInt = Field(ge=0)
    end_frame: StrictInt | None = Field(ge=0)
    source: Literal["legacy_envelope", "manual", "imported", "derived"]
    version: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def check_end(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("SceneTrack interval end precedes its start")
        return self


class MemberSnapshot(_ClosedModel):
    id: uuid.UUID
    version: StrictInt = Field(ge=1)
    task_id: uuid.UUID
    track_id: TrackId | None
    scene_track_id: uuid.UUID | None
    class_name: StrictStr
    is_active: StrictBool
    temporal_role: StrictStr
    sensor_role: StrictStr | None


class TrackSnapshot(_ClosedModel):
    id: uuid.UUID
    track_id: TrackId
    revision: StrictInt = Field(ge=1)
    presence_mode: Literal["inferred", "explicit"]
    retired_at: AwareDatetime | None
    intervals: list[IntervalSnapshot]
    members: list[MemberSnapshot]

    @model_validator(mode="after")
    def check_member_limit(self):
        if len(self.members) > 5000:
            raise ValueError("SceneTrack command cannot exceed 5000 members")
        return self


class IntervalSpec(_ClosedModel):
    start_frame: StrictInt = Field(ge=0)
    end_frame: StrictInt | None = Field(ge=0)
    source: Literal["legacy_envelope", "manual", "imported", "derived"]

    @model_validator(mode="after")
    def check_end(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("SceneTrack interval end precedes its start")
        return self


class TrackIntervalSet(_ClosedModel):
    track_id: TrackId
    intervals: list[IntervalSpec]


class PreviewImpact(_ClosedModel):
    total: StrictInt
    by_temporal_role: dict[StrictStr, StrictInt]
    frames: list[StrictInt]
    requires_confirmation: StrictBool


class PreviewInterval(_ClosedModel):
    id: uuid.UUID
    start_frame: StrictInt = Field(ge=0)
    end_frame: StrictInt | None = Field(ge=0)
    source: Literal["legacy_envelope", "manual", "imported", "derived"]
    version: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def check_end(self):
        if self.end_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("SceneTrack interval end precedes its start")
        return self


class SceneTrackPreview(_ClosedModel):
    kind: Literal["split", "merge", "mark_absent", "resume", "terminate"]
    scene_id: uuid.UUID
    scene_name: StrictStr | None
    track_id: TrackId
    secondary_track_id: TrackId | None
    frame_index: StrictInt | None
    resume_frame: StrictInt | None
    source_revisions: dict[StrictStr, StrictInt]
    before_intervals: dict[StrictStr, list[PreviewInterval]]
    after_intervals: dict[StrictStr, list[PreviewInterval]]
    affected_members: PreviewImpact
    snapshot_token: StrictStr = Field(pattern=_DIGEST_PATTERN)


class LockSceneTrackCommand(_Command):
    action: Literal["lock"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_ids: list[TrackId] = Field(min_length=1, max_length=2)
    resume_frame: StrictInt | None
    target_task_id: uuid.UUID | None


class ReplaySceneTrackCommand(_Command):
    action: Literal["replay"]
    scene_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)
    request_digest: StrictStr = Field(pattern=_DIGEST_PATTERN)


class ApplySceneTrackCommand(_Command):
    action: Literal["apply"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)
    request_digest: StrictStr = Field(pattern=_DIGEST_PATTERN)
    expected_snapshot_token: StrictStr = Field(pattern=_DIGEST_PATTERN)
    command: SceneTrackCommandInput
    preview: SceneTrackPreview
    snapshot: list[TrackSnapshot]
    after_intervals: list[TrackIntervalSet]
    created_track_id: StrictStr | None
    target_task_id: uuid.UUID | None
    created_track_attributes: object
    created_track_attributes_meta: object

    @model_validator(mode="after")
    def check_prepared_command(self):
        if (
            self.command.kind != self.preview.kind
            or self.command.track_id != self.preview.track_id
            or self.command.secondary_track_id != self.preview.secondary_track_id
            or self.command.frame_index != self.preview.frame_index
            or self.command.resume_frame != self.preview.resume_frame
            or self.preview.scene_id != self.scene_id
            or self.expected_snapshot_token != self.preview.snapshot_token
        ):
            raise ValueError("Prepared SceneTrack command does not match its preview")
        expected_tracks = {self.command.track_id}
        if self.command.secondary_track_id is not None:
            expected_tracks.add(self.command.secondary_track_id)
        if self.command.kind == "split":
            if self.created_track_id is None:
                raise ValueError("Split command requires a created track ID")
            expected_intervals = {self.command.track_id, self.created_track_id}
        elif self.command.kind == "merge":
            expected_intervals = {
                self.command.track_id,
                self.command.secondary_track_id,
            }
        else:
            expected_intervals = {self.command.track_id}
        if (
            {track.track_id for track in self.snapshot} != expected_tracks
            or len(self.snapshot) != len(expected_tracks)
            or {entry.track_id for entry in self.after_intervals} != expected_intervals
            or len(self.after_intervals) != len(expected_intervals)
            or set(self.preview.source_revisions) != expected_tracks
            or (self.command.kind != "split" and self.created_track_id is not None)
            or (self.command.kind != "resume" and self.target_task_id is not None)
            or (self.command.kind == "resume" and self.target_task_id is None)
        ):
            raise ValueError("Prepared SceneTrack scope does not match its command")
        return self


class InspectSceneTrackOperation(_Command):
    action: Literal["inspect"]
    scene_id: uuid.UUID
    operation_id: uuid.UUID


class ListSceneTrackOperations(_Command):
    action: Literal["list"]
    scene_id: uuid.UUID
    track_id: TrackId
    limit: StrictInt = Field(ge=1, le=400)
    committed_only: StrictBool
    exclude_reverts: StrictBool


class RevertSceneTrackOperation(_Command):
    action: Literal["revert"]
    scene_id: uuid.UUID
    operation_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)


SceneTrackCommand: TypeAlias = Annotated[
    LockSceneTrackCommand
    | ReplaySceneTrackCommand
    | ApplySceneTrackCommand
    | InspectSceneTrackOperation
    | ListSceneTrackOperations
    | RevertSceneTrackOperation,
    Field(discriminator="action"),
]


class SceneTrackCommandOperation(_ClosedModel):
    id: uuid.UUID
    kind: Literal["split", "merge", "mark_absent", "resume", "terminate", "revert"]
    status: Literal["committed", "reverted"]
    created_at: AwareDatetime
    completed_at: AwareDatetime
    response_json: dict[str, object]
    task_ids: list[uuid.UUID]


class SceneTrackCommandData(_ClosedModel):
    operation: SceneTrackCommandOperation | None = None
    response_json: dict[str, object] | None = None
    task_ids: list[uuid.UUID] = Field(default_factory=list)
    found: StrictBool | None = None
    operations: list[SceneTrackCommandOperation] | None = None
    locked: StrictBool | None = None
    before_state: dict[str, object] | None = None
    after_state: dict[str, object] | None = None
    request_digest: StrictStr | None = None
    replaced_track_ids: list[uuid.UUID] = Field(default_factory=list)


class SceneTrackCommandError(_ClosedModel):
    status_code: StrictInt
    reason: StrictStr
    message: StrictStr


class SceneTrackCommandReply(_ClosedModel):
    protocol: Literal["aap-labeling-scene-track-commands/v1"] = Field(alias="schema")
    action: StrictStr
    ok: StrictBool
    data: SceneTrackCommandData | None
    error: SceneTrackCommandError | None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("Successful SceneTrack reply must contain data")
        elif self.error is None or self.data is not None:
            raise ValueError("Failed SceneTrack reply must contain only an error")
        return self


class SceneTrackCommandFailure(Exception):
    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message


class SceneTrackCommandUnavailable(Exception):
    """The Labeling SceneTrack owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(SceneTrackCommand)
_REPLY_ADAPTER = TypeAdapter(SceneTrackCommandReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(
            value.model_dump(mode="json", by_alias=True)
            if isinstance(value, BaseModel)
            else _json_ready(value),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def decode_scene_track_command(value: object) -> SceneTrackCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneTrackCommandUnavailable() from None


def scene_track_command(action: str, **values: object) -> SceneTrackCommand:
    try:
        return decode_scene_track_command(
            {"schema": SCENE_TRACK_COMMAND_PROTOCOL, "action": action, **values}
        )
    except SceneTrackCommandUnavailable:
        raise SceneTrackCommandUnavailable() from None


def scene_track_command_json(value: object) -> str:
    command = decode_scene_track_command(value)
    return command.model_dump_json(by_alias=True, exclude_none=False)


def decode_scene_track_reply(request: object, value: object) -> SceneTrackCommandData:
    command = decode_scene_track_command(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneTrackCommandUnavailable() from None
    if reply.action != command.action:
        raise SceneTrackCommandUnavailable()
    if not reply.ok:
        assert reply.error is not None
        raise SceneTrackCommandFailure(
            reply.error.status_code, reply.error.reason, reply.error.message
        )
    assert reply.data is not None
    if command.action == "lock" and reply.data.locked is not True:
        raise SceneTrackCommandUnavailable()
    if command.action == "replay" and (
        reply.data.found is None
        or (reply.data.found and reply.data.response_json is None)
        or (reply.data.found and reply.data.request_digest != command.request_digest)
        or (not reply.data.found and reply.data.response_json is not None)
    ):
        raise SceneTrackCommandUnavailable()
    if command.action == "inspect" and reply.data.operation is None:
        raise SceneTrackCommandUnavailable()
    if command.action == "list" and reply.data.operations is None:
        raise SceneTrackCommandUnavailable()
    if command.action in {"apply", "revert"} and (
        reply.data.operation is None
        or reply.data.response_json is None
        or reply.data.before_state is None
        or reply.data.after_state is None
    ):
        raise SceneTrackCommandUnavailable()
    if command.action == "apply" and reply.data.operation.kind != command.command.kind:
        raise SceneTrackCommandUnavailable()
    if command.action == "revert" and reply.data.operation.kind != "revert":
        raise SceneTrackCommandUnavailable()
    return reply.data


def scene_track_command_schema() -> dict[str, object]:
    schema = TypeAdapter(SceneTrackCommand | SceneTrackCommandReply).json_schema(
        by_alias=True
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCENE_TRACK_COMMAND_PROTOCOL
    schema["x-postgresql-functions"] = {
        SCENE_TRACK_COMMAND_FUNCTION: {
            "argument_types": ["jsonb"],
            "returns": "jsonb",
            "owner": "labeling",
            "security_definer": True,
            "caller_transaction_required": True,
            "dependencies": ["aap_assets_v1.spatial_item_references"],
            "actions": ["lock", "replay", "apply", "inspect", "list", "revert"],
            "fixed_targets": [
                "public.tasks",
                "public.task_dataset_item_links",
                "aap_assets_v1.spatial_item_references",
                "public.scene_tracks",
                "public.scene_track_intervals",
                "public.annotations",
                "public.scene_track_operations",
            ],
            "table_access": {
                "public.tasks": {"select": ["id", "dataset_item_id"]},
                "public.task_dataset_item_links": {
                    "select": ["task_id", "dataset_item_id", "role"]
                },
                "aap_assets_v1.spatial_item_references": {
                    "select": ["item_id", "scene_id", "frame_index"]
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
                        "created_by",
                        "retired_at",
                        "created_at",
                        "updated_at",
                    ],
                    "insert": [
                        "id",
                        "project_id",
                        "scene_id",
                        "track_id",
                        "class_name",
                        "presence_mode",
                        "attributes",
                        "attributes_meta",
                        "revision",
                        "created_by",
                    ],
                    "update": [
                        "class_name",
                        "presence_mode",
                        "revision",
                        "retired_at",
                        "attributes",
                        "attributes_meta",
                        "updated_at",
                    ],
                },
                "public.scene_track_intervals": {
                    "select": [
                        "id",
                        "scene_track_id",
                        "start_frame",
                        "end_frame",
                        "source",
                        "version",
                    ],
                    "insert": [
                        "id",
                        "scene_track_id",
                        "start_frame",
                        "end_frame",
                        "source",
                        "version",
                        "created_by",
                        "operation_id",
                    ],
                    "delete": True,
                },
                "public.annotations": {
                    "select": [
                        "id",
                        "task_id",
                        "project_id",
                        "user_id",
                        "source",
                        "annotation_type",
                        "tool_unit_id",
                        "class_name",
                        "geometry",
                        "track_id",
                        "scene_track_id",
                        "temporal_role",
                        "sensor_role",
                        "attributes",
                        "attributes_meta",
                        "z_order",
                        "is_active",
                        "is_hidden",
                        "version",
                        "was_cancelled",
                        "is_locked",
                    ],
                    "insert": [
                        "id",
                        "task_id",
                        "project_id",
                        "user_id",
                        "source",
                        "annotation_type",
                        "tool_unit_id",
                        "class_name",
                        "geometry",
                        "track_id",
                        "scene_track_id",
                        "temporal_role",
                        "attributes",
                        "attributes_meta",
                        "z_order",
                        "is_active",
                        "is_hidden",
                        "version",
                        "was_cancelled",
                        "ground_truth",
                        "is_locked",
                    ],
                    "update": [
                        "track_id",
                        "scene_track_id",
                        "class_name",
                        "temporal_role",
                        "is_active",
                        "is_hidden",
                        "version",
                        "updated_at",
                    ],
                },
                "public.scene_track_operations": {
                    "select": [
                        "id",
                        "scene_id",
                        "actor_id",
                        "kind",
                        "idempotency_key",
                        "request_digest",
                        "snapshot_token",
                        "source_revisions",
                        "result_revisions",
                        "before_state",
                        "after_state",
                        "inverse_payload",
                        "response_json",
                        "status",
                        "reverted_by_operation_id",
                        "created_at",
                        "completed_at",
                    ],
                    "insert": [
                        "id",
                        "scene_id",
                        "actor_id",
                        "kind",
                        "idempotency_key",
                        "request_digest",
                        "snapshot_token",
                        "source_revisions",
                        "result_revisions",
                        "before_state",
                        "after_state",
                        "inverse_payload",
                        "response_json",
                        "status",
                    ],
                    "update": [
                        "result_revisions",
                        "after_state",
                        "response_json",
                        "status",
                        "reverted_by_operation_id",
                    ],
                },
            },
            "sequences": [],
        }
    }
    return schema


__all__ = [
    "ApplySceneTrackCommand",
    "IntervalSnapshot",
    "IntervalSpec",
    "ListSceneTrackOperations",
    "LockSceneTrackCommand",
    "MemberSnapshot",
    "PreviewImpact",
    "PreviewInterval",
    "ReplaySceneTrackCommand",
    "RevertSceneTrackOperation",
    "SceneTrackCommand",
    "SceneTrackCommandData",
    "SceneTrackCommandFailure",
    "SceneTrackCommandInput",
    "SceneTrackCommandOperation",
    "SceneTrackCommandUnavailable",
    "SceneTrackPreview",
    "TrackIntervalSet",
    "TrackSnapshot",
    "decode_scene_track_command",
    "decode_scene_track_reply",
    "scene_track_command",
    "scene_track_command_json",
    "scene_track_command_schema",
]
