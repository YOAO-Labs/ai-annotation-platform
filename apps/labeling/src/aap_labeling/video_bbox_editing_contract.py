"""Closed commands for compact video bounding-box annotation writes."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

VIDEO_BBOX_EDITING_PROTOCOL = "aap-labeling-video-bbox-editing/v1"
VIDEO_BBOX_EDITING_FUNCTION = "aap_labeling_v1.video_bbox_editing_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AnnotationFence(_ClosedModel):
    annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)


class VideoBBoxFrame(_ClosedModel):
    annotation_id: uuid.UUID
    frame_index: StrictInt
    x: StrictInt | FiniteFloat
    y: StrictInt | FiniteFloat
    w: StrictInt | FiniteFloat
    h: StrictInt | FiniteFloat


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-video-bbox-editing/v1"] = Field(alias="schema")
    operation: StrictStr
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    source_fences: list[AnnotationFence]

    @model_validator(mode="after")
    def check_unique_sources(self):
        identifiers = [item.annotation_id for item in self.source_fences]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("annotation_ids must be unique")
        return self


class ConvertTrack(_Command):
    operation: Literal["convert_track"]
    conversion_operation: Literal["copy", "split"]
    scope: Literal["frame", "track"]
    frame_index: StrictInt | None
    frame_mode: Literal["keyframes", "all_frames"]
    frame_count: StrictInt | None
    max_created: StrictInt = Field(ge=1)
    created_frames: list[VideoBBoxFrame]
    removed_frame_indexes: list[StrictInt]

    @model_validator(mode="after")
    def check_conversion(self):
        if len(self.source_fences) != 1:
            raise ValueError("convert_track requires exactly one annotation")
        if len(self.created_frames) > self.max_created:
            raise ValueError(
                f"track conversion would create more than {self.max_created} bboxes"
            )
        if len({frame.annotation_id for frame in self.created_frames}) != len(
            self.created_frames
        ):
            raise ValueError("converted frames must have unique annotation_id values")
        if self.conversion_operation == "copy":
            if self.removed_frame_indexes:
                raise ValueError("copy must not mutate its source track")
        elif self.scope == "frame":
            if self.frame_index is None:
                raise ValueError("frame_index is required for frame scope")
            if self.removed_frame_indexes != [self.frame_index]:
                raise ValueError("frame split must remove exactly its selected frame")
        elif not self.removed_frame_indexes:
            raise ValueError("track split must remove at least one visible frame")
        return self


class AggregateBBoxes(_Command):
    operation: Literal["aggregate_bboxes"]
    created_annotation_id: uuid.UUID
    created_track_id: StrictStr
    created_geometry: dict[str, object]
    delete_sources: StrictBool

    @model_validator(mode="after")
    def check_sources(self):
        if not self.source_fences:
            raise ValueError("annotation_ids is required")
        if len(self.created_track_id) > 64:
            raise ValueError("track_id is too long")
        return self


class SplitTrack(_Command):
    operation: Literal["split_track"]
    frame_index: StrictInt
    created_annotation_id: uuid.UUID
    created_track_id: StrictStr
    updated_geometry: dict[str, object]
    created_geometry: dict[str, object]

    @model_validator(mode="after")
    def check_source(self):
        if len(self.source_fences) != 1:
            raise ValueError("split_track requires exactly one annotation")
        if len(self.created_track_id) > 64:
            raise ValueError("track_id is too long")
        return self


class MergeTracks(_Command):
    operation: Literal["merge_tracks"]
    survivor_id: uuid.UUID
    survivor_track_id: StrictStr
    survivor_geometry: dict[str, object]

    @model_validator(mode="after")
    def check_sources(self):
        if len(self.source_fences) != 2:
            raise ValueError("merge_tracks requires exactly two annotations")
        if self.survivor_id != self.source_fences[0].annotation_id:
            raise ValueError("merge_tracks survivor must be the first annotation")
        return self


class JoinTracks(_Command):
    operation: Literal["join_tracks"]
    gap_mode: Literal["interpolate", "outside"]
    survivor_id: uuid.UUID
    survivor_track_id: StrictStr
    survivor_geometry: dict[str, object]

    @model_validator(mode="after")
    def check_sources(self):
        if len(self.source_fences) != 2:
            raise ValueError("join_tracks requires exactly two annotations")
        if self.survivor_id != self.source_fences[0].annotation_id:
            raise ValueError("join_tracks survivor must be the first annotation")
        return self


VideoBBoxEditingCommand: TypeAlias = Annotated[
    ConvertTrack | AggregateBBoxes | SplitTrack | MergeTracks | JoinTracks,
    Field(discriminator="operation"),
]


class VideoBBoxEditingData(_ClosedModel):
    updated_annotation_ids: list[uuid.UUID]
    created_annotation_ids: list[uuid.UUID]
    deleted_annotation_ids: list[uuid.UUID]
    removed_frame_indexes: list[StrictInt]
    deleted_source: StrictBool


class VideoBBoxEditingError(_ClosedModel):
    code: StrictStr
    status_code: StrictInt = Field(ge=400, le=599)
    detail: object


class VideoBBoxEditingReply(_ClosedModel):
    schema_: Literal["aap-labeling-video-bbox-editing/v1"] = Field(alias="schema")
    ok: StrictBool
    request: VideoBBoxEditingCommand
    data: VideoBBoxEditingData | None = None
    error: VideoBBoxEditingError | None = None

    @model_validator(mode="after")
    def check_result(self):
        if self.ok != (self.data is not None and self.error is None):
            raise ValueError("video bbox reply must contain exactly one result")
        if not self.ok and self.error is None:
            raise ValueError("failed video bbox reply must contain an error")
        return self


class VideoBBoxEditingRejected(RuntimeError):
    def __init__(self, error: VideoBBoxEditingError) -> None:
        self.code = error.code
        self.status_code = error.status_code
        self.detail = error.detail
        super().__init__(error.code)


class VideoBBoxEditingUnavailable(RuntimeError):
    """The Labeling video-bbox editing owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(VideoBBoxEditingCommand)
_REPLY_ADAPTER = TypeAdapter(VideoBBoxEditingReply)


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def decode_video_bbox_editing_command(value: object) -> VideoBBoxEditingCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise VideoBBoxEditingUnavailable() from None


def decode_video_bbox_editing_reply(
    request: VideoBBoxEditingCommand, value: object
) -> VideoBBoxEditingData:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise VideoBBoxEditingUnavailable() from None
    if reply.request != request:
        raise VideoBBoxEditingUnavailable()
    if not reply.ok:
        if reply.error is None:
            raise VideoBBoxEditingUnavailable()
        raise VideoBBoxEditingRejected(reply.error)
    if reply.data is None:
        raise VideoBBoxEditingUnavailable()
    return reply.data


def video_bbox_editing_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = VIDEO_BBOX_EDITING_PROTOCOL
    schema["x-postgresql-functions"] = [VIDEO_BBOX_EDITING_FUNCTION]
    return schema


def video_bbox_editing_command_json(command: VideoBBoxEditingCommand) -> str:
    validated = decode_video_bbox_editing_command(
        command.model_dump_json(by_alias=True, exclude_none=False)
    )
    return validated.model_dump_json(by_alias=True, exclude_none=False)


__all__ = [
    "AggregateBBoxes",
    "AnnotationFence",
    "ConvertTrack",
    "JoinTracks",
    "MergeTracks",
    "SplitTrack",
    "VIDEO_BBOX_EDITING_FUNCTION",
    "VIDEO_BBOX_EDITING_PROTOCOL",
    "VideoBBoxEditingData",
    "VideoBBoxEditingRejected",
    "VideoBBoxEditingUnavailable",
    "VideoBBoxFrame",
    "VideoBBoxEditingCommand",
    "VideoBBoxEditingReply",
    "decode_video_bbox_editing_command",
    "decode_video_bbox_editing_reply",
    "video_bbox_editing_command_json",
    "video_bbox_editing_schema",
]
