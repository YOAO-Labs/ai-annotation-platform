"""Closed, language-neutral requests for Labeling SceneTrack mutations."""

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
    model_validator,
)

SCENE_TRACK_DOMAIN_PROTOCOL = "aap-labeling-scene-track-domain/v1"
SCENE_TRACK_DOMAIN_FUNCTION = "aap_labeling_v1.scene_track_domain_command(jsonb)"
INTERVAL_SOURCES = frozenset({"legacy_envelope", "manual", "imported", "derived"})


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    protocol: Literal["aap-labeling-scene-track-domain/v1"] = Field(alias="schema")
    action: StrictStr


class EnsureSceneTrack(_Command):
    action: Literal["ensure"]
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr
    class_name: StrictStr
    frames: list[StrictInt]
    actor_id: uuid.UUID | None
    interval_source: StrictStr
    member_change: StrictBool


class TouchSceneTrack(_Command):
    action: Literal["touch"]
    annotation_id: uuid.UUID
    scene_track_id: uuid.UUID | None
    track_id: StrictStr | None
    class_name: StrictStr


class ReclassifySceneTrack(_Command):
    action: Literal["reclassify"]
    annotation_id: uuid.UUID
    scene_track_id: uuid.UUID
    track_id: StrictStr | None
    expected_class_name: StrictStr
    class_name: StrictStr


SceneTrackDomainCommand: TypeAlias = Annotated[
    EnsureSceneTrack | TouchSceneTrack | ReclassifySceneTrack,
    Field(discriminator="action"),
]


class SceneTrackRecord(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    scene_id: uuid.UUID
    track_id: StrictStr
    class_name: StrictStr
    presence_mode: StrictStr
    attributes: object
    attributes_meta: object
    revision: StrictInt
    created_by: uuid.UUID | None
    retired_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SceneTrackCameraMember(_ClosedModel):
    id: uuid.UUID
    class_name: StrictStr
    version: StrictInt
    updated_at: AwareDatetime


class SceneTrackDomainData(_ClosedModel):
    track: SceneTrackRecord | None
    touched: StrictBool
    changed: StrictBool
    camera_members: list[SceneTrackCameraMember]


class SceneTrackDomainError(_ClosedModel):
    code: StrictStr
    message: StrictStr


class SceneTrackDomainReply(_ClosedModel):
    protocol: Literal["aap-labeling-scene-track-domain/v1"] = Field(alias="schema")
    action: StrictStr
    ok: StrictBool
    data: SceneTrackDomainData | None
    error: SceneTrackDomainError | None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("Successful SceneTrack reply must contain data")
        elif self.error is None or self.data is not None:
            raise ValueError("Failed SceneTrack reply must contain only an error")
        return self


class SceneTrackDomainFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SceneTrackDomainUnavailable(Exception):
    """The Labeling SceneTrack owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(SceneTrackDomainCommand)
_REPLY_ADAPTER = TypeAdapter(SceneTrackDomainReply)


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
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value


def decode_scene_track_command(value: object) -> SceneTrackDomainCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneTrackDomainUnavailable() from None


def scene_track_command(action: str, **values) -> SceneTrackDomainCommand:
    try:
        return decode_scene_track_command(
            {"schema": SCENE_TRACK_DOMAIN_PROTOCOL, "action": action, **values}
        )
    except SceneTrackDomainUnavailable:
        raise SceneTrackDomainUnavailable() from None


def scene_track_command_json(value: object) -> str:
    command = decode_scene_track_command(value)
    return command.model_dump_json(by_alias=True, exclude_none=False)


def decode_scene_track_reply(request: object, value: object) -> SceneTrackDomainData:
    request = decode_scene_track_command(request)
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SceneTrackDomainUnavailable() from None
    if reply.action != request.action:
        raise SceneTrackDomainUnavailable()
    if not reply.ok:
        assert reply.error is not None
        raise SceneTrackDomainFailure(reply.error.code, reply.error.message)
    assert reply.data is not None
    data = reply.data
    if request.action == "ensure":
        if (
            data.track is None
            or data.track.project_id != request.project_id
            or data.track.scene_id != request.scene_id
            or data.track.track_id != request.track_id
            or data.track.class_name != request.class_name
            or data.touched
            or data.camera_members
        ):
            raise SceneTrackDomainUnavailable()
    elif request.action == "touch":
        if (
            data.touched != (request.scene_track_id is not None)
            or (data.track is None) != (request.scene_track_id is None)
            or data.changed
            or data.camera_members
            or (
                data.track is not None
                and (
                    data.track.id != request.scene_track_id
                    or data.track.track_id != request.track_id
                    or data.track.class_name != request.class_name
                )
            )
        ):
            raise SceneTrackDomainUnavailable()
    elif (
        data.track is None
        or data.track.id != request.scene_track_id
        or data.track.track_id != request.track_id
        or data.track.class_name != request.class_name
        or data.touched
        or (data.changed != (request.expected_class_name != request.class_name))
        or any(
            member.class_name != request.class_name for member in data.camera_members
        )
    ):
        raise SceneTrackDomainUnavailable()
    return data


def scene_track_domain_schema() -> dict[str, object]:
    schema = TypeAdapter(SceneTrackDomainCommand | SceneTrackDomainReply).json_schema(
        by_alias=True
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCENE_TRACK_DOMAIN_PROTOCOL
    schema["x-postgresql-functions"] = {
        SCENE_TRACK_DOMAIN_FUNCTION: {
            "argument_types": ["jsonb"],
            "returns": "jsonb",
            "owner": "labeling",
            "security_definer": True,
            "caller_transaction_required": True,
            "fixed_targets": [
                "public.scene_tracks",
                "public.scene_track_intervals",
                "public.annotations",
            ],
            "actions": ["ensure", "reclassify", "touch"],
            "interval_sources": sorted(INTERVAL_SOURCES),
            "table_access": {
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
                        "created_by",
                    ],
                    "update": ["class_name", "revision", "updated_at"],
                },
                "public.scene_track_intervals": {
                    "select": [
                        "id",
                        "scene_track_id",
                        "start_frame",
                        "end_frame",
                        "source",
                        "version",
                        "created_by",
                    ],
                    "insert": [
                        "id",
                        "scene_track_id",
                        "start_frame",
                        "end_frame",
                        "source",
                        "created_by",
                    ],
                    "update": ["start_frame", "end_frame", "version", "updated_at"],
                    "delete": True,
                },
                "public.annotations": {
                    "select": [
                        "id",
                        "scene_track_id",
                        "track_id",
                        "class_name",
                        "is_active",
                        "was_cancelled",
                        "sensor_role",
                        "version",
                        "updated_at",
                    ],
                    "update": ["class_name", "version", "updated_at"],
                },
            },
        }
    }
    return schema


__all__ = [
    "INTERVAL_SOURCES",
    "SCENE_TRACK_DOMAIN_FUNCTION",
    "SCENE_TRACK_DOMAIN_PROTOCOL",
    "EnsureSceneTrack",
    "ReclassifySceneTrack",
    "SceneTrackCameraMember",
    "SceneTrackDomainData",
    "SceneTrackDomainFailure",
    "SceneTrackDomainUnavailable",
    "SceneTrackRecord",
    "SceneTrackDomainCommand",
    "TouchSceneTrack",
    "decode_scene_track_command",
    "decode_scene_track_reply",
    "scene_track_command",
    "scene_track_command_json",
    "scene_track_domain_schema",
]
