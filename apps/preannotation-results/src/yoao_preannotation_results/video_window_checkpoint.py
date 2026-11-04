"""Immutable per-window tracker outputs and bounded checkpoint transport.

This module binds one frozen plan window to its normalized output and
post-window continuation state. It stores no backend session token, URL, or
credential. Session closure remains an explicit owner fact used only by the
resume decision helper.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from tempfile import TemporaryFile
from typing import Annotated, Any, Iterator, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from . import codec
from .contract import (
    CanonicalUUID,
    Digest,
    MAX_PART_BASE64_CHARS,
    MAX_PART_BYTES,
    MAX_REQUEST_BYTES,
    NonnegativeInt,
    PositiveInt,
)
from .video_execution_plan import (
    MAX_VIDEO_CHECKPOINT_BYTES,
    MAX_VIDEO_RESULT_ROWS,
    VideoContinuationPostState,
    VideoDiscoverySeed,
    VideoExecutionContractFailure,
    VideoExecutionPlan,
    VideoExecutionWindow,
    VideoFrameResultRow,
    _digest,
    _stream_json_digest,
    _stream_json_snapshot,
    _validate_model,
    combo_discovery_seeds,
    decode_video_execution_plan,
    discovery_seeds_digest,
    normalize_video_frame_results,
)

VIDEO_WINDOW_CHECKPOINT_CONTENT_SCHEMA = "aap-video-window-result-content/v1"
VIDEO_WINDOW_CHECKPOINT_SCHEMA = "aap-video-window-result/v1"
VIDEO_WINDOW_CHECKPOINT_PART_SCHEMA = "aap-video-window-result-part/v1"
VIDEO_WINDOW_REPLAY_SCHEMA = "aap-video-window-replay-match/v1"
VIDEO_WINDOW_RESUME_SCHEMA = "aap-video-window-resume-decision/v1"
MAX_VIDEO_WINDOW_CHECKPOINT_PARTS = (
    MAX_VIDEO_CHECKPOINT_BYTES + MAX_PART_BYTES - 1
) // MAX_PART_BYTES


class _WindowValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _failure(code: str):
    raise VideoExecutionContractFailure(code)


def _reject_model_extras(value: Any, *, seen: set[int] | None = None) -> None:
    """Catch extra attributes added to otherwise frozen Pydantic instances."""
    if isinstance(value, BaseModel):
        fields = set(type(value).model_fields)
        if set(value.__dict__) - fields:
            _failure("invalid_window_checkpoint")
        seen = set() if seen is None else seen
        marker = id(value)
        if marker in seen:
            _failure("invalid_window_checkpoint")
        seen.add(marker)
        try:
            for name in fields:
                _reject_model_extras(getattr(value, name), seen=seen)
        finally:
            seen.remove(marker)
    elif type(value) in {tuple, list, dict}:
        seen = set() if seen is None else seen
        marker = id(value)
        if marker in seen:
            _failure("invalid_window_checkpoint")
        seen.add(marker)
        try:
            children = value.values() if type(value) is dict else value
            for item in children:
                _reject_model_extras(item, seen=seen)
        finally:
            seen.remove(marker)


def _revalidate(model: type[_WindowValue], value: Any, *, max_bytes: int):
    """Round-trip typed values so in-place mutations cannot bypass validation."""
    _reject_model_extras(value)
    try:
        if type(value) is model:
            value = value.model_dump(mode="json", by_alias=True)
        return _validate_model(model, value, max_bytes=max_bytes)
    except VideoExecutionContractFailure:
        raise
    except (
        TypeError,
        ValueError,
        ValidationError,
        RecursionError,
        OverflowError,
        UnicodeError,
    ):
        _failure("invalid_window_checkpoint")


def _typed_plan(value: VideoExecutionPlan | Mapping[str, Any]) -> VideoExecutionPlan:
    _reject_model_extras(value)
    try:
        if type(value) is VideoExecutionPlan:
            value = value.model_dump(mode="json", by_alias=True)
        return decode_video_execution_plan(value)
    except (
        TypeError,
        ValueError,
        ValidationError,
        RecursionError,
        OverflowError,
        UnicodeError,
    ):
        _failure("invalid_video_execution_plan")


def _checkpoint_id(plan_digest: str, window_ordinal: int) -> str:
    return _digest({"plan_digest": plan_digest, "window_ordinal": window_ordinal})


def _plan_window(plan: VideoExecutionPlan, ordinal: int) -> VideoExecutionWindow:
    if type(ordinal) is not int or ordinal < 0 or ordinal >= len(plan.windows):
        _failure("window_ordinal_out_of_range")
    return plan.windows[ordinal]


def _result_document(
    frame_results: Sequence[VideoFrameResultRow],
    continuation: VideoContinuationPostState | None,
    discovery_output_digest: str | None,
    discovery_seeds: Sequence[VideoDiscoverySeed],
    discovery_seeds_digest_value: str | None,
) -> dict[str, Any]:
    return {
        "frame_results": frame_results,
        "continuation": continuation,
        "discovery_output_digest": discovery_output_digest,
        "discovery_seeds": discovery_seeds,
        "discovery_seeds_digest": discovery_seeds_digest_value,
    }


def _discovery_seed_frame(plan: VideoExecutionPlan) -> int:
    return plan.to_frame if plan.direction == "backward" else plan.from_frame


def _expected_predecessor_window(
    plan: VideoExecutionPlan, window: VideoExecutionWindow
) -> VideoExecutionWindow | None:
    if window.phase == "discovery" or window.seed_source in {
        "none",
        "prompt",
        "source_geometry",
        "correction",
    }:
        return None
    if window.seed_source == "discovery":
        return next((item for item in plan.windows if item.phase == "discovery"), None)
    if window.seed_source in {"continuation", "session"}:
        candidates = [
            item
            for item in plan.windows
            if item.phase == "track"
            and item.branch == window.branch
            and item.ordinal < window.ordinal
        ]
        return candidates[-1] if candidates else None
    _failure("unsupported_window_seed_source")


def _validate_predecessor(
    plan: VideoExecutionPlan,
    window: VideoExecutionWindow,
    predecessor: VideoWindowCheckpointManifest | Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    expected_window = _expected_predecessor_window(plan, window)
    if expected_window is None:
        if predecessor is not None:
            _failure("unexpected_window_predecessor")
        if window.seed_source in {"prompt", "source_geometry", "correction"}:
            return window.seed_digest, None
        return None, None
    if predecessor is None:
        _failure("window_predecessor_missing")
    typed = decode_video_window_checkpoint_manifest(predecessor)
    if (
        typed.plan_digest != plan.plan_digest
        or typed.request_id != plan.request_id
        or typed.input_id != plan.input_id
        or typed.window_ordinal != expected_window.ordinal
        or typed.window_digest != expected_window.window_digest
        or typed.checkpoint_id
        != _checkpoint_id(plan.plan_digest, expected_window.ordinal)
    ):
        _failure("window_predecessor_mismatch")
    predecessor_window = _validate_manifest_plan(plan, typed)
    if window.seed_source == "discovery":
        if (
            predecessor_window.phase != "discovery"
            or typed.phase != "discovery"
            or typed.discovery_seeds_digest is None
        ):
            _failure("window_discovery_predecessor_mismatch")
        seed_input_digest = typed.discovery_seeds_digest
    else:
        if (
            predecessor_window.phase != "track"
            or predecessor_window.branch != window.branch
            or typed.phase != "track"
        ):
            _failure("window_branch_predecessor_mismatch")
        if window.seed_source == "session":
            seed_input_digest = None
        elif typed.continuation_digest is None:
            _failure("window_predecessor_state_missing")
        else:
            seed_input_digest = typed.continuation_digest
    return seed_input_digest, typed.manifest_digest


class VideoWindowCheckpointPayload(_WindowValue):
    """Canonical normalized output for exactly one frozen window."""

    schema_: Literal["aap-video-window-result-content/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    request_id: CanonicalUUID
    input_id: CanonicalUUID
    plan_digest: Digest
    window_ordinal: NonnegativeInt
    window_digest: Digest
    context_span_digest: Digest
    phase: Literal["discovery", "track"]
    branch: Literal["forward", "backward"]
    seed_input_digest: Digest | None
    predecessor_checkpoint_digest: Digest | None
    raw_response_digest: Digest
    frame_results: tuple[VideoFrameResultRow, ...]
    continuation: VideoContinuationPostState | None
    discovery_output_digest: Digest | None
    discovery_seeds_digest: Digest | None
    discovery_seeds: tuple[VideoDiscoverySeed, ...]
    result_digest: Digest

    @field_validator("frame_results", "discovery_seeds", mode="before")
    @classmethod
    def arrays_are_tuples(cls, value: Any):
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("checkpoint collections must be arrays")

    @model_validator(mode="after")
    def output_identity(self) -> VideoWindowCheckpointPayload:
        if self.checkpoint_id != _checkpoint_id(self.plan_digest, self.window_ordinal):
            raise ValueError("window checkpoint id mismatch")
        if self.phase == "discovery":
            if (
                self.continuation is not None
                or self.seed_input_digest is not None
                or self.predecessor_checkpoint_digest is not None
                or self.discovery_output_digest is None
                or self.discovery_seeds_digest is None
            ):
                raise ValueError("discovery output must be unseeded and stateless")
            output_digest = _stream_json_digest(
                self.frame_results, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
            )[0]
            seeds_digest = discovery_seeds_digest(self.discovery_seeds)
            if (
                output_digest != self.discovery_output_digest
                or seeds_digest != self.discovery_seeds_digest
                or [item.obj_id for item in self.discovery_seeds]
                != list(range(1, len(self.discovery_seeds) + 1))
            ):
                raise ValueError("discovery evidence digest mismatch")
        elif (
            self.continuation is None
            or self.discovery_output_digest is not None
            or self.discovery_seeds_digest is not None
            or self.discovery_seeds
        ):
            raise ValueError("tracking output requires continuation state only")
        expected_result_digest = _stream_json_digest(
            _result_document(
                self.frame_results,
                self.continuation,
                self.discovery_output_digest,
                self.discovery_seeds,
                self.discovery_seeds_digest,
            ),
            max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
        )[0]
        if self.result_digest != expected_result_digest:
            raise ValueError("checkpoint result digest mismatch")
        return self


class VideoWindowCheckpointManifest(_WindowValue):
    """Small immutable index for a per-window checkpoint payload."""

    schema_: Literal["aap-video-window-result/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    manifest_digest: Digest
    request_id: CanonicalUUID
    input_id: CanonicalUUID
    plan_digest: Digest
    window_ordinal: NonnegativeInt
    window_digest: Digest
    context_span_digest: Digest
    phase: Literal["discovery", "track"]
    seed_input_digest: Digest | None
    predecessor_checkpoint_digest: Digest | None
    raw_response_digest: Digest
    result_digest: Digest
    result_byte_size: PositiveInt
    payload_digest: Digest
    payload_byte_size: PositiveInt
    payload_part_count: PositiveInt
    continuation_digest: Digest | None
    discovery_output_digest: Digest | None
    discovery_seeds_digest: Digest | None

    @model_validator(mode="after")
    def immutable_identity(self) -> VideoWindowCheckpointManifest:
        if (
            self.checkpoint_id != _checkpoint_id(self.plan_digest, self.window_ordinal)
            or self.result_byte_size > MAX_VIDEO_CHECKPOINT_BYTES
            or self.payload_byte_size > MAX_VIDEO_CHECKPOINT_BYTES
            or self.payload_part_count
            != max(1, (self.payload_byte_size + MAX_PART_BYTES - 1) // MAX_PART_BYTES)
            or self.payload_part_count > MAX_VIDEO_WINDOW_CHECKPOINT_PARTS
            or (self.phase == "track") != (self.continuation_digest is not None)
            or (self.phase == "discovery") != (self.discovery_output_digest is not None)
            or (self.phase == "discovery") != (self.discovery_seeds_digest is not None)
        ):
            raise ValueError("checkpoint manifest exceeds its frozen bounds")
        core = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"checkpoint_id", "manifest_digest"},
        )
        if self.manifest_digest != _digest(core):
            raise ValueError("checkpoint manifest digest mismatch")
        return self


class VideoWindowCheckpointPart(_WindowValue):
    schema_: Literal["aap-video-window-result-part/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    manifest_digest: Digest
    part_index: NonnegativeInt
    part_count: PositiveInt
    payload_digest: Digest
    payload_byte_size: PositiveInt
    part_digest: Digest
    data_base64: Annotated[
        StrictStr,
        Field(min_length=1, max_length=MAX_PART_BASE64_CHARS),
    ]

    @model_validator(mode="after")
    def bounded_content(self) -> VideoWindowCheckpointPart:
        if (
            not 0 <= self.part_index < self.part_count
            or self.payload_byte_size > MAX_VIDEO_CHECKPOINT_BYTES
            or self.part_count
            != max(1, (self.payload_byte_size + MAX_PART_BYTES - 1) // MAX_PART_BYTES)
        ):
            raise ValueError("checkpoint part index or payload size is inconsistent")
        if self.part_count > MAX_VIDEO_WINDOW_CHECKPOINT_PARTS:
            raise ValueError("checkpoint part count exceeds the total budget")
        try:
            content = codec.decode_part(self.data_base64)
        except (TypeError, ValueError):
            raise ValueError(
                "checkpoint part is not canonical bounded base64"
            ) from None
        if hashlib.sha256(content).hexdigest() != self.part_digest:
            raise ValueError("checkpoint part digest mismatch")
        if len(content) != min(
            MAX_PART_BYTES, self.payload_byte_size - self.part_index * MAX_PART_BYTES
        ):
            raise ValueError("checkpoint part size mismatch")
        return self


@dataclass(frozen=True, slots=True)
class VideoWindowCheckpointSnapshot:
    manifest: VideoWindowCheckpointManifest
    _snapshot: codec.Snapshot = field(repr=False, compare=False)

    def iter_parts(self) -> Iterator[VideoWindowCheckpointPart]:
        for index, content in enumerate(self._snapshot.iter_parts()):
            yield VideoWindowCheckpointPart(
                schema=VIDEO_WINDOW_CHECKPOINT_PART_SCHEMA,
                checkpoint_id=self.manifest.checkpoint_id,
                manifest_digest=self.manifest.manifest_digest,
                part_index=index,
                part_count=self.manifest.payload_part_count,
                payload_digest=self.manifest.payload_digest,
                payload_byte_size=self.manifest.payload_byte_size,
                part_digest=hashlib.sha256(content).hexdigest(),
                data_base64=codec.encode_part(content),
            )


class VideoWindowReplayMatch(_WindowValue):
    schema_: Literal["aap-video-window-replay-match/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    stored_manifest_digest: Digest
    stored_raw_response_digest: Digest
    observed_raw_response_digest: Digest
    result_digest: Digest
    raw_response_matches: StrictBool


class VideoWindowResumeDecision(_WindowValue):
    schema_: Literal["aap-video-window-resume-decision/v1"] = Field(alias="schema")
    action: Literal["complete", "execute", "replay"]
    completed_prefix_count: NonnegativeInt
    reuse_ordinals: tuple[NonnegativeInt, ...]
    replay_ordinals: tuple[NonnegativeInt, ...]
    execute_ordinals: tuple[NonnegativeInt, ...]
    replay_context_span_digests: tuple[Digest, ...]

    @field_validator(
        "reuse_ordinals",
        "replay_ordinals",
        "execute_ordinals",
        "replay_context_span_digests",
        mode="before",
    )
    @classmethod
    def arrays_are_tuples(cls, value: Any):
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("resume decision collections must be arrays")


def _decode(model: type[_WindowValue], value: Any, *, max_bytes: int):
    return _revalidate(model, value, max_bytes=max_bytes)


def decode_video_window_checkpoint_payload(value: Any) -> VideoWindowCheckpointPayload:
    return _decode(
        VideoWindowCheckpointPayload,
        value,
        max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
    )


def decode_video_window_checkpoint_manifest(
    value: Any,
) -> VideoWindowCheckpointManifest:
    return _decode(
        VideoWindowCheckpointManifest,
        value,
        max_bytes=MAX_REQUEST_BYTES,
    )


def decode_video_window_checkpoint_part(value: Any) -> VideoWindowCheckpointPart:
    return _decode(
        VideoWindowCheckpointPart,
        value,
        max_bytes=MAX_PART_BASE64_CHARS + 4096,
    )


def _rows_for_window(
    plan: VideoExecutionPlan,
    window: VideoExecutionWindow,
    rows: Iterable[Mapping[str, Any] | VideoFrameResultRow],
) -> tuple[VideoFrameResultRow, ...]:
    count = 0

    def bounded_rows():
        nonlocal count
        for row in rows:
            count += 1
            if count > MAX_VIDEO_RESULT_ROWS:
                _failure("checkpoint_too_many_rows")
            if type(row) is VideoFrameResultRow:
                _reject_model_extras(row)
                row = row.model_dump(mode="json", by_alias=True)
            if not isinstance(row, Mapping):
                _failure("invalid_frame_result")
            yield row

    normalized = normalize_video_frame_results(bounded_rows(), branch=window.branch)
    for row in normalized:
        if not window.from_frame <= row.frame_index <= window.to_frame:
            _failure("window_result_out_of_range")
        if row.frame_index == plan.correction_frame and plan.job_kind == "correction":
            _failure("correction_human_frame_included")
        if (
            window.phase == "track"
            and row.geometry
            and row.geometry.get("type") != plan.output.geometry
        ):
            _failure("window_result_geometry_mismatch")
    _stream_json_digest(normalized, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES)
    return normalized


def _validate_continuation_geometry(
    plan: VideoExecutionPlan,
    continuation: VideoContinuationPostState,
) -> None:
    # An empty/outside first window retains the original accepted seed geometry,
    # which may be a track or box even when model output is a raster Mask.
    if continuation.last_geometry and continuation.last_geometry.get("type") not in {
        "bbox",
        "video_bbox",
        "polygon",
        "mask",
        "video_track_bbox",
        "video_track_polygon",
        "video_track_mask",
    }:
        _failure("window_continuation_geometry_mismatch")
    geometries = [item.geometry for item in continuation.last_geom_by_instance]
    geometries.extend(item.geometry for item in continuation.mp_prev_boundary)
    if any(
        geometry and geometry.get("type") != plan.output.geometry
        for geometry in geometries
    ):
        _failure("window_continuation_geometry_mismatch")


def make_video_window_checkpoint_payload(
    *,
    plan: VideoExecutionPlan | Mapping[str, Any],
    window_ordinal: int,
    raw_response_digest: str,
    frame_results: Iterable[Mapping[str, Any] | VideoFrameResultRow],
    continuation: VideoContinuationPostState | Mapping[str, Any] | None,
    predecessor_checkpoint: VideoWindowCheckpointManifest
    | Mapping[str, Any]
    | None = None,
) -> VideoWindowCheckpointPayload:
    """Create immutable output for one window, bound to its exact predecessor."""
    typed_plan = _typed_plan(plan)
    window = _plan_window(typed_plan, window_ordinal)
    rows = _rows_for_window(typed_plan, window, frame_results)
    try:
        seed_input_digest, predecessor_digest = _validate_predecessor(
            typed_plan, window, predecessor_checkpoint
        )
        if window.phase == "discovery":
            if continuation is not None:
                _failure("discovery_checkpoint_has_continuation")
            seeds = combo_discovery_seeds(
                rows, seed_frame=_discovery_seed_frame(typed_plan)
            )
            output_digest = _stream_json_digest(
                rows, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
            )[0]
            seeds_digest = discovery_seeds_digest(seeds)
            typed_continuation = None
        else:
            if continuation is None:
                _failure("track_checkpoint_missing_continuation")
            typed_continuation = _decode(
                VideoContinuationPostState,
                continuation,
                max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
            )
            _validate_continuation_geometry(typed_plan, typed_continuation)
            seeds = ()
            output_digest = None
            seeds_digest = None
        result_digest = _stream_json_digest(
            _result_document(
                rows,
                typed_continuation,
                output_digest,
                seeds,
                seeds_digest,
            ),
            max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
        )[0]
        value = {
            "schema": VIDEO_WINDOW_CHECKPOINT_CONTENT_SCHEMA,
            "checkpoint_id": _checkpoint_id(typed_plan.plan_digest, window.ordinal),
            "request_id": str(typed_plan.request_id),
            "input_id": str(typed_plan.input_id),
            "plan_digest": typed_plan.plan_digest,
            "window_ordinal": window.ordinal,
            "window_digest": window.window_digest,
            "context_span_digest": window.context_span.span_digest,
            "phase": window.phase,
            "branch": window.branch,
            "seed_input_digest": seed_input_digest,
            "predecessor_checkpoint_digest": predecessor_digest,
            "raw_response_digest": raw_response_digest,
            "frame_results": rows,
            "continuation": typed_continuation,
            "discovery_output_digest": output_digest,
            "discovery_seeds_digest": seeds_digest,
            "discovery_seeds": seeds,
            "result_digest": result_digest,
        }
        return decode_video_window_checkpoint_payload(value)
    except VideoExecutionContractFailure:
        raise
    except (TypeError, ValueError, ValidationError):
        _failure("invalid_window_checkpoint")


def _validate_manifest_plan(
    plan: VideoExecutionPlan,
    manifest: VideoWindowCheckpointManifest,
) -> VideoExecutionWindow:
    window = _plan_window(plan, manifest.window_ordinal)
    if (
        manifest.checkpoint_id != _checkpoint_id(plan.plan_digest, window.ordinal)
        or manifest.request_id != plan.request_id
        or manifest.input_id != plan.input_id
        or manifest.plan_digest != plan.plan_digest
        or manifest.window_digest != window.window_digest
        or manifest.context_span_digest != window.context_span.span_digest
        or manifest.phase != window.phase
    ):
        _failure("window_checkpoint_identity_mismatch")
    return window


def validate_video_window_checkpoint_payload(
    plan: VideoExecutionPlan | Mapping[str, Any],
    payload: VideoWindowCheckpointPayload | Mapping[str, Any] | bytes | str,
    *,
    predecessor_checkpoint: VideoWindowCheckpointManifest
    | Mapping[str, Any]
    | None = None,
) -> tuple[VideoExecutionPlan, VideoWindowCheckpointPayload]:
    typed_plan = _typed_plan(plan)
    typed_payload = decode_video_window_checkpoint_payload(payload)
    window = _plan_window(typed_plan, typed_payload.window_ordinal)
    if (
        typed_payload.checkpoint_id
        != _checkpoint_id(typed_plan.plan_digest, window.ordinal)
        or typed_payload.request_id != typed_plan.request_id
        or typed_payload.input_id != typed_plan.input_id
        or typed_payload.plan_digest != typed_plan.plan_digest
        or typed_payload.window_digest != window.window_digest
        or typed_payload.context_span_digest != window.context_span.span_digest
        or typed_payload.phase != window.phase
        or typed_payload.branch != window.branch
    ):
        _failure("window_checkpoint_identity_mismatch")
    normalized = normalize_video_frame_results(
        (
            row.model_dump(mode="json", by_alias=True)
            for row in typed_payload.frame_results
        ),
        branch=window.branch,
    )
    if normalized != typed_payload.frame_results:
        _failure("window_checkpoint_results_not_normalized")
    for row in typed_payload.frame_results:
        if not window.from_frame <= row.frame_index <= window.to_frame:
            _failure("window_result_out_of_range")
        if (
            typed_plan.job_kind == "correction"
            and row.frame_index == typed_plan.correction_frame
        ):
            _failure("correction_human_frame_included")
        if (
            window.phase == "track"
            and row.geometry
            and row.geometry.get("type") != typed_plan.output.geometry
        ):
            _failure("window_result_geometry_mismatch")
    expected_seed, expected_predecessor = _validate_predecessor(
        typed_plan, window, predecessor_checkpoint
    )
    if (
        typed_payload.seed_input_digest != expected_seed
        or typed_payload.predecessor_checkpoint_digest != expected_predecessor
    ):
        _failure("window_checkpoint_seed_mismatch")
    if window.phase == "discovery":
        seeds = combo_discovery_seeds(
            typed_payload.frame_results,
            seed_frame=_discovery_seed_frame(typed_plan),
        )
        if (
            typed_payload.discovery_seeds != seeds
            or typed_payload.discovery_output_digest
            != _stream_json_digest(
                typed_payload.frame_results, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
            )[0]
            or typed_payload.discovery_seeds_digest != discovery_seeds_digest(seeds)
        ):
            _failure("window_discovery_output_mismatch")
    elif typed_payload.continuation is None:
        _failure("track_checkpoint_missing_continuation")
    else:
        _validate_continuation_geometry(typed_plan, typed_payload.continuation)
    return typed_plan, typed_payload


def _result_identity(payload: VideoWindowCheckpointPayload) -> dict[str, Any]:
    return _result_document(
        payload.frame_results,
        payload.continuation,
        payload.discovery_output_digest,
        payload.discovery_seeds,
        payload.discovery_seeds_digest,
    )


def _manifest_core(
    payload: VideoWindowCheckpointPayload,
    plan: VideoExecutionPlan,
    *,
    result_byte_size: int,
    snapshot: codec.Snapshot,
) -> dict[str, Any]:
    _plan_window(plan, payload.window_ordinal)
    return {
        "schema": VIDEO_WINDOW_CHECKPOINT_SCHEMA,
        "checkpoint_id": payload.checkpoint_id,
        "request_id": str(payload.request_id),
        "input_id": str(payload.input_id),
        "plan_digest": payload.plan_digest,
        "window_ordinal": payload.window_ordinal,
        "window_digest": payload.window_digest,
        "context_span_digest": payload.context_span_digest,
        "phase": payload.phase,
        "seed_input_digest": payload.seed_input_digest,
        "predecessor_checkpoint_digest": payload.predecessor_checkpoint_digest,
        "raw_response_digest": payload.raw_response_digest,
        "result_digest": payload.result_digest,
        "result_byte_size": result_byte_size,
        "payload_digest": snapshot.sha256,
        "payload_byte_size": snapshot.byte_size,
        "payload_part_count": snapshot.part_count,
        "continuation_digest": (
            _stream_json_digest(
                payload.continuation, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
            )[0]
            if payload.continuation is not None
            else None
        ),
        "discovery_output_digest": payload.discovery_output_digest,
        "discovery_seeds_digest": payload.discovery_seeds_digest,
    }


@dataclass(frozen=True, slots=True)
class _ManifestSnapshot:
    manifest: VideoWindowCheckpointManifest
    snapshot: codec.Snapshot


def _snapshot_manifest(
    plan: VideoExecutionPlan,
    payload: VideoWindowCheckpointPayload,
) -> _ManifestSnapshot:
    result_digest, result_byte_size = _stream_json_digest(
        _result_identity(payload), max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
    )
    if result_digest != payload.result_digest:
        _failure("window_checkpoint_result_digest_mismatch")
    snapshot = _stream_json_snapshot(
        payload.model_dump(mode="json", by_alias=True),
        max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
    )
    try:
        core = _manifest_core(
            payload, plan, result_byte_size=result_byte_size, snapshot=snapshot
        )
        manifest_digest_value = _digest(
            {key: value for key, value in core.items() if key != "checkpoint_id"}
        )
        manifest = decode_video_window_checkpoint_manifest(
            {**core, "manifest_digest": manifest_digest_value}
        )
        return _ManifestSnapshot(manifest=manifest, snapshot=snapshot)
    except BaseException:
        snapshot._file.close()
        raise


@contextmanager
def snapshot_video_window_checkpoint(
    plan: VideoExecutionPlan | Mapping[str, Any],
    payload: VideoWindowCheckpointPayload | Mapping[str, Any],
    *,
    predecessor_checkpoint: VideoWindowCheckpointManifest
    | Mapping[str, Any]
    | None = None,
) -> Iterator[VideoWindowCheckpointSnapshot]:
    typed_plan, typed_payload = validate_video_window_checkpoint_payload(
        plan, payload, predecessor_checkpoint=predecessor_checkpoint
    )
    snapshot = _snapshot_manifest(typed_plan, typed_payload)
    try:
        yield VideoWindowCheckpointSnapshot(snapshot.manifest, snapshot.snapshot)
    finally:
        snapshot.snapshot._file.close()


@contextmanager
def assemble_video_window_checkpoint_parts(
    manifest: VideoWindowCheckpointManifest | Mapping[str, Any],
    parts: Iterable[VideoWindowCheckpointPart | Mapping[str, Any]],
) -> Iterator[Any]:
    """Verify ordered bounded parts into a temporary file without buffering them."""
    typed_manifest = decode_video_window_checkpoint_manifest(manifest)
    handle = TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    total_bytes = 0
    part_count = 0
    try:
        for index, value in enumerate(parts):
            part = decode_video_window_checkpoint_part(value)
            if (
                part.checkpoint_id != typed_manifest.checkpoint_id
                or part.manifest_digest != typed_manifest.manifest_digest
                or part.part_index != index
                or part.part_count != typed_manifest.payload_part_count
                or part.payload_digest != typed_manifest.payload_digest
                or part.payload_byte_size != typed_manifest.payload_byte_size
            ):
                _failure("window_checkpoint_part_identity_mismatch")
            content = codec.decode_part(part.data_base64)
            expected_part_size = min(
                MAX_PART_BYTES,
                typed_manifest.payload_byte_size - index * MAX_PART_BYTES,
            )
            if len(content) != expected_part_size:
                _failure("window_checkpoint_part_size_mismatch")
            total_bytes += len(content)
            if total_bytes > typed_manifest.payload_byte_size:
                _failure("window_checkpoint_payload_too_large")
            handle.write(content)
            digest.update(content)
            part_count += 1
        if (
            part_count != typed_manifest.payload_part_count
            or total_bytes != typed_manifest.payload_byte_size
            or digest.hexdigest() != typed_manifest.payload_digest
        ):
            _failure("window_checkpoint_payload_incomplete")
        handle.flush()
        handle.seek(0)
        yield handle
    except VideoExecutionContractFailure:
        raise
    except (OSError, TypeError, ValueError, ValidationError):
        _failure("invalid_window_checkpoint_part")
    finally:
        handle.close()


def require_video_window_replay_match(
    *,
    plan: VideoExecutionPlan | Mapping[str, Any],
    stored_manifest: VideoWindowCheckpointManifest | Mapping[str, Any],
    stored_payload: VideoWindowCheckpointPayload | Mapping[str, Any],
    replayed_payload: VideoWindowCheckpointPayload | Mapping[str, Any],
    predecessor_checkpoint: VideoWindowCheckpointManifest
    | Mapping[str, Any]
    | None = None,
) -> VideoWindowReplayMatch:
    """Accept an equivalent replay while keeping original raw evidence immutable."""
    typed_plan = _typed_plan(plan)
    typed_manifest = decode_video_window_checkpoint_manifest(stored_manifest)
    _, old_payload = validate_video_window_checkpoint_payload(
        typed_plan, stored_payload, predecessor_checkpoint=predecessor_checkpoint
    )
    _, new_payload = validate_video_window_checkpoint_payload(
        typed_plan, replayed_payload, predecessor_checkpoint=predecessor_checkpoint
    )
    old_snapshot = _snapshot_manifest(typed_plan, old_payload)
    try:
        if old_snapshot.manifest != typed_manifest:
            _failure("stored_window_checkpoint_mismatch")
    finally:
        old_snapshot.snapshot._file.close()
    new_snapshot = _snapshot_manifest(typed_plan, new_payload)
    try:
        proposed = new_snapshot.manifest
        if (
            proposed.checkpoint_id != typed_manifest.checkpoint_id
            or proposed.plan_digest != typed_manifest.plan_digest
            or proposed.window_digest != typed_manifest.window_digest
            or proposed.context_span_digest != typed_manifest.context_span_digest
            or proposed.seed_input_digest != typed_manifest.seed_input_digest
            or proposed.predecessor_checkpoint_digest
            != typed_manifest.predecessor_checkpoint_digest
            or proposed.result_digest != typed_manifest.result_digest
            or proposed.continuation_digest != typed_manifest.continuation_digest
            or proposed.discovery_output_digest
            != typed_manifest.discovery_output_digest
            or proposed.discovery_seeds_digest != typed_manifest.discovery_seeds_digest
        ):
            _failure("window_checkpoint_replay_conflict")
        return VideoWindowReplayMatch(
            schema=VIDEO_WINDOW_REPLAY_SCHEMA,
            checkpoint_id=typed_manifest.checkpoint_id,
            stored_manifest_digest=typed_manifest.manifest_digest,
            stored_raw_response_digest=typed_manifest.raw_response_digest,
            observed_raw_response_digest=new_payload.raw_response_digest,
            result_digest=typed_manifest.result_digest,
            raw_response_matches=(
                typed_manifest.raw_response_digest == new_payload.raw_response_digest
            ),
        )
    finally:
        new_snapshot.snapshot._file.close()


def _context_span_groups(
    plan: VideoExecutionPlan,
) -> tuple[tuple[VideoExecutionWindow, ...], ...]:
    groups: dict[str, list[VideoExecutionWindow]] = {}
    for window in plan.windows:
        groups.setdefault(window.context_span.span_digest, []).append(window)
    return tuple(
        sorted(
            (tuple(values) for values in groups.values()),
            key=lambda values: values[0].ordinal,
        )
    )


def _validate_resume_manifest_chain(
    plan: VideoExecutionPlan,
    manifests: Mapping[int, VideoWindowCheckpointManifest],
) -> None:
    for ordinal, manifest in manifests.items():
        window = _validate_manifest_plan(plan, manifest)
        expected_window = _expected_predecessor_window(plan, window)
        if expected_window is None:
            expected_seed = (
                window.seed_digest
                if window.seed_source in {"prompt", "source_geometry", "correction"}
                else None
            )
            expected_id = None
        else:
            predecessor = manifests.get(expected_window.ordinal)
            if predecessor is None:
                _failure("window_checkpoint_gap")
            expected_id = predecessor.manifest_digest
            if window.seed_source == "discovery":
                if predecessor.phase != "discovery":
                    _failure("window_discovery_predecessor_mismatch")
                expected_seed = predecessor.discovery_seeds_digest
            else:
                if (
                    predecessor.phase != "track"
                    or plan.windows[expected_window.ordinal].branch != window.branch
                ):
                    _failure("window_branch_predecessor_mismatch")
                expected_seed = (
                    None
                    if window.seed_source == "session"
                    else predecessor.continuation_digest
                )
        if (
            manifest.seed_input_digest != expected_seed
            or manifest.predecessor_checkpoint_digest != expected_id
        ):
            _failure("window_checkpoint_seed_mismatch")


def decide_video_window_resume(
    plan: VideoExecutionPlan | Mapping[str, Any],
    manifests: Sequence[VideoWindowCheckpointManifest | Mapping[str, Any]],
    *,
    closed_context_span_digests: Sequence[str],
) -> VideoWindowResumeDecision:
    """Select a contiguous checkpoint prefix for reuse, session replay or execution."""
    typed_plan = _typed_plan(plan)
    typed_manifests = tuple(
        decode_video_window_checkpoint_manifest(item) for item in manifests
    )
    known_spans = {
        span[0].context_span.span_digest: span
        for span in _context_span_groups(typed_plan)
    }
    raw_closed = tuple(closed_context_span_digests)
    if any(
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in raw_closed
    ):
        _failure("invalid_closed_context_span_digest")
    closed = set(raw_closed)
    if len(closed) != len(raw_closed) or closed - set(known_spans):
        _failure("unknown_or_duplicate_closed_context_span")
    ordered_ordinals = [item.window_ordinal for item in typed_manifests]
    if ordered_ordinals != sorted(ordered_ordinals):
        _failure("window_checkpoint_manifests_not_ordered")
    selected: dict[int, VideoWindowCheckpointManifest] = {}
    for manifest in typed_manifests:
        _validate_manifest_plan(typed_plan, manifest)
        existing = selected.get(manifest.window_ordinal)
        if existing is None:
            selected[manifest.window_ordinal] = manifest
        elif existing.manifest_digest != manifest.manifest_digest:
            _failure("divergent_duplicate_window_checkpoint")
    if sorted(selected) != list(range(len(selected))):
        _failure("window_checkpoint_gap")
    _validate_resume_manifest_chain(typed_plan, selected)
    if selected and not typed_plan.compatibility_complete:
        _failure("backend_compatibility_identity_unavailable")
    for span_digest in closed:
        span = known_spans[span_digest]
        if any(window.ordinal not in selected for window in span):
            _failure("closed_context_span_missing_window")
    reuse: list[int] = []
    replay: list[int] = []
    execute: list[int] = []
    replay_spans: list[str] = []
    open_session_started = False
    for span in _context_span_groups(typed_plan):
        span_digest = span[0].context_span.span_digest
        ordinals = [window.ordinal for window in span]
        present = [ordinal for ordinal in ordinals if ordinal in selected]
        if span[0].context_span.mode == "none":
            if present:
                reuse.extend(present)
            else:
                execute.extend(ordinals)
            continue
        is_closed = span_digest in closed
        if is_closed:
            if open_session_started:
                _failure("closed_span_after_open_session")
            reuse.extend(ordinals)
        elif present:
            if open_session_started:
                _failure("multiple_open_session_spans")
            open_session_started = True
            replay.extend(present)
            replay_spans.append(span_digest)
            missing = [ordinal for ordinal in ordinals if ordinal not in selected]
            execute.extend(missing)
        else:
            execute.extend(ordinals)
    if open_session_started:
        open_spans = set(replay_spans)
        after_open = [
            window.ordinal
            for window in typed_plan.windows
            if window.ordinal
            > max(known_spans[digest][-1].ordinal for digest in open_spans)
            and window.ordinal in selected
        ]
        if after_open:
            _failure("output_after_unclosed_context_span")
    for values in (reuse, replay, execute):
        values.sort()
    action = (
        "complete" if not execute and not replay else "replay" if replay else "execute"
    )
    return VideoWindowResumeDecision(
        schema=VIDEO_WINDOW_RESUME_SCHEMA,
        action=action,
        completed_prefix_count=len(selected),
        reuse_ordinals=tuple(reuse),
        replay_ordinals=tuple(replay),
        execute_ordinals=tuple(execute),
        replay_context_span_digests=tuple(replay_spans),
    )


@contextmanager
def verify_video_window_checkpoint_parts(
    plan: VideoExecutionPlan | Mapping[str, Any],
    manifest: VideoWindowCheckpointManifest | Mapping[str, Any],
    parts: Iterable[VideoWindowCheckpointPart | Mapping[str, Any]],
    *,
    predecessor_checkpoint: VideoWindowCheckpointManifest
    | Mapping[str, Any]
    | None = None,
) -> Iterator[VideoWindowCheckpointPayload]:
    """Assemble, decode and validate payload bytes against the manifest and plan."""
    typed_plan = _typed_plan(plan)
    typed_manifest = decode_video_window_checkpoint_manifest(manifest)
    _validate_manifest_plan(typed_plan, typed_manifest)
    with assemble_video_window_checkpoint_parts(typed_manifest, parts) as handle:
        handle.seek(0)
        raw = handle.read(MAX_VIDEO_CHECKPOINT_BYTES + 1)
        if len(raw) > MAX_VIDEO_CHECKPOINT_BYTES:
            _failure("window_checkpoint_payload_too_large")
        _, payload = validate_video_window_checkpoint_payload(
            typed_plan,
            raw,
            predecessor_checkpoint=predecessor_checkpoint,
        )
        snapshot = _snapshot_manifest(typed_plan, payload)
        try:
            if snapshot.manifest != typed_manifest:
                _failure("window_checkpoint_manifest_payload_mismatch")
        finally:
            snapshot.snapshot._file.close()
        yield payload


def _field(type_: str, *, nullable: bool = False, **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": type_, **extra}
    return {"anyOf": [schema, {"type": "null"}]} if nullable else schema


def _object_schema(
    required: Sequence[str], properties: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def video_window_checkpoint_schema() -> dict[str, Any]:
    """Return the JSON Schema snapshot for public per-window checkpoint values."""
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    geometry_digest = {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$",
    }
    geometry_by_instance = _object_schema(
        ["instance_id", "geometry"],
        {
            "instance_id": {"type": "string", "minLength": 1, "maxLength": 255},
            "geometry": {"type": "object"},
        },
    )
    frame_result = _object_schema(
        [
            "frame_index",
            "geometry",
            "confidence",
            "outside",
            "instance_id",
            "primary",
            "geometry_digest",
            "candidate_key",
        ],
        {
            "frame_index": {"type": "integer", "minimum": 0},
            "geometry": {"type": "object"},
            "confidence": _field("number", nullable=True),
            "outside": {"type": "boolean"},
            "instance_id": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 255},
                    {"type": "null"},
                ]
            },
            "primary": {"type": "boolean"},
            "geometry_digest": geometry_digest,
            "candidate_key": {"type": "string", "minLength": 1, "maxLength": 512},
        },
    )
    continuation = _object_schema(
        [
            "last_geometry",
            "last_geom_by_instance",
            "mp_prev_boundary",
            "mp_next_global",
        ],
        {
            "last_geometry": _field("object", nullable=True),
            "last_geom_by_instance": {
                "type": "array",
                "maxItems": 100000,
                "items": geometry_by_instance,
            },
            "mp_prev_boundary": {
                "type": "array",
                "maxItems": 100000,
                "items": geometry_by_instance,
            },
            "mp_next_global": {"type": "integer", "minimum": 1},
        },
    )
    discovery_seed = _object_schema(
        ["obj_id", "geometry"],
        {"obj_id": {"type": "integer", "minimum": 1}, "geometry": {"type": "object"}},
    )
    payload = _object_schema(
        [
            "schema",
            "checkpoint_id",
            "request_id",
            "input_id",
            "plan_digest",
            "window_ordinal",
            "window_digest",
            "context_span_digest",
            "phase",
            "branch",
            "seed_input_digest",
            "predecessor_checkpoint_digest",
            "raw_response_digest",
            "frame_results",
            "continuation",
            "discovery_output_digest",
            "discovery_seeds_digest",
            "discovery_seeds",
            "result_digest",
        ],
        {
            "schema": {"const": VIDEO_WINDOW_CHECKPOINT_CONTENT_SCHEMA},
            "checkpoint_id": digest,
            "request_id": {"type": "string", "format": "uuid"},
            "input_id": {"type": "string", "format": "uuid"},
            "plan_digest": digest,
            "window_ordinal": {"type": "integer", "minimum": 0},
            "window_digest": digest,
            "context_span_digest": digest,
            "phase": {"enum": ["discovery", "track"]},
            "branch": {"enum": ["forward", "backward"]},
            "seed_input_digest": {"anyOf": [digest, {"type": "null"}]},
            "predecessor_checkpoint_digest": {"anyOf": [digest, {"type": "null"}]},
            "raw_response_digest": digest,
            "frame_results": {
                "type": "array",
                "maxItems": MAX_VIDEO_RESULT_ROWS,
                "items": frame_result,
            },
            "continuation": {"anyOf": [continuation, {"type": "null"}]},
            "discovery_output_digest": {"anyOf": [digest, {"type": "null"}]},
            "discovery_seeds_digest": {"anyOf": [digest, {"type": "null"}]},
            "discovery_seeds": {
                "type": "array",
                "maxItems": 100000,
                "items": discovery_seed,
            },
            "result_digest": digest,
        },
    )
    manifest = _object_schema(
        [
            "schema",
            "checkpoint_id",
            "manifest_digest",
            "request_id",
            "input_id",
            "plan_digest",
            "window_ordinal",
            "window_digest",
            "context_span_digest",
            "phase",
            "seed_input_digest",
            "predecessor_checkpoint_digest",
            "raw_response_digest",
            "result_digest",
            "result_byte_size",
            "payload_digest",
            "payload_byte_size",
            "payload_part_count",
            "continuation_digest",
            "discovery_output_digest",
            "discovery_seeds_digest",
        ],
        {
            "schema": {"const": VIDEO_WINDOW_CHECKPOINT_SCHEMA},
            "checkpoint_id": digest,
            "manifest_digest": digest,
            "request_id": {"type": "string", "format": "uuid"},
            "input_id": {"type": "string", "format": "uuid"},
            "plan_digest": digest,
            "window_ordinal": {"type": "integer", "minimum": 0},
            "window_digest": digest,
            "context_span_digest": digest,
            "phase": {"enum": ["discovery", "track"]},
            "seed_input_digest": {"anyOf": [digest, {"type": "null"}]},
            "predecessor_checkpoint_digest": {"anyOf": [digest, {"type": "null"}]},
            "raw_response_digest": digest,
            "result_digest": digest,
            "result_byte_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIDEO_CHECKPOINT_BYTES,
            },
            "payload_digest": digest,
            "payload_byte_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIDEO_CHECKPOINT_BYTES,
            },
            "payload_part_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIDEO_WINDOW_CHECKPOINT_PARTS,
            },
            "continuation_digest": {"anyOf": [digest, {"type": "null"}]},
            "discovery_output_digest": {"anyOf": [digest, {"type": "null"}]},
            "discovery_seeds_digest": {"anyOf": [digest, {"type": "null"}]},
        },
    )
    part = _object_schema(
        [
            "schema",
            "checkpoint_id",
            "manifest_digest",
            "part_index",
            "part_count",
            "payload_digest",
            "payload_byte_size",
            "part_digest",
            "data_base64",
        ],
        {
            "schema": {"const": VIDEO_WINDOW_CHECKPOINT_PART_SCHEMA},
            "checkpoint_id": digest,
            "manifest_digest": digest,
            "part_index": {"type": "integer", "minimum": 0},
            "part_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIDEO_WINDOW_CHECKPOINT_PARTS,
            },
            "payload_digest": digest,
            "payload_byte_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIDEO_CHECKPOINT_BYTES,
            },
            "part_digest": digest,
            "data_base64": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_PART_BASE64_CHARS,
                "contentEncoding": "base64",
            },
        },
    )
    replay = _object_schema(
        [
            "schema",
            "checkpoint_id",
            "stored_manifest_digest",
            "stored_raw_response_digest",
            "observed_raw_response_digest",
            "result_digest",
            "raw_response_matches",
        ],
        {
            "schema": {"const": VIDEO_WINDOW_REPLAY_SCHEMA},
            "checkpoint_id": digest,
            "stored_manifest_digest": digest,
            "stored_raw_response_digest": digest,
            "observed_raw_response_digest": digest,
            "result_digest": digest,
            "raw_response_matches": {"type": "boolean"},
        },
    )
    resume = _object_schema(
        [
            "schema",
            "action",
            "completed_prefix_count",
            "reuse_ordinals",
            "replay_ordinals",
            "execute_ordinals",
            "replay_context_span_digests",
        ],
        {
            "schema": {"const": VIDEO_WINDOW_RESUME_SCHEMA},
            "action": {"enum": ["complete", "execute", "replay"]},
            "completed_prefix_count": {"type": "integer", "minimum": 0},
            "reuse_ordinals": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
            "replay_ordinals": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
            "execute_ordinals": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
            "replay_context_span_digests": {"type": "array", "items": digest},
        },
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "aap-video-window-checkpoints/v1",
        "title": "AAP immutable per-window video checkpoint values",
        "oneOf": [
            {"$ref": "#/$defs/VideoWindowCheckpointPayload"},
            {"$ref": "#/$defs/VideoWindowCheckpointManifest"},
            {"$ref": "#/$defs/VideoWindowCheckpointPart"},
            {"$ref": "#/$defs/VideoWindowReplayMatch"},
            {"$ref": "#/$defs/VideoWindowResumeDecision"},
        ],
        "x-budgets": {
            "payload_bytes": MAX_VIDEO_CHECKPOINT_BYTES,
            "part_bytes": MAX_PART_BYTES,
            "part_base64_chars": MAX_PART_BASE64_CHARS,
            "part_count": MAX_VIDEO_WINDOW_CHECKPOINT_PARTS,
        },
        "$defs": {
            "VideoWindowCheckpointPayload": payload,
            "VideoWindowCheckpointManifest": manifest,
            "VideoWindowCheckpointPart": part,
            "VideoWindowReplayMatch": replay,
            "VideoWindowResumeDecision": resume,
        },
    }


__all__ = [
    "MAX_VIDEO_WINDOW_CHECKPOINT_PARTS",
    "VIDEO_WINDOW_CHECKPOINT_CONTENT_SCHEMA",
    "VIDEO_WINDOW_CHECKPOINT_PART_SCHEMA",
    "VIDEO_WINDOW_CHECKPOINT_SCHEMA",
    "VIDEO_WINDOW_REPLAY_SCHEMA",
    "VIDEO_WINDOW_RESUME_SCHEMA",
    "VideoWindowCheckpointManifest",
    "VideoWindowCheckpointPart",
    "VideoWindowCheckpointPayload",
    "VideoWindowCheckpointSnapshot",
    "VideoWindowReplayMatch",
    "VideoWindowResumeDecision",
    "assemble_video_window_checkpoint_parts",
    "decide_video_window_resume",
    "decode_video_window_checkpoint_manifest",
    "decode_video_window_checkpoint_part",
    "decode_video_window_checkpoint_payload",
    "make_video_window_checkpoint_payload",
    "require_video_window_replay_match",
    "snapshot_video_window_checkpoint",
    "validate_video_window_checkpoint_payload",
    "verify_video_window_checkpoint_parts",
    "video_window_checkpoint_schema",
]
