"""Portable frozen window plans and immutable tracker checkpoint values.

The contract contains only stable request, input, backend-compatibility and
result facts. It deliberately has no backend URL, bearer, GPU lease, or tracker
session token. A session-backed checkpoint covers a whole closed context span;
an interrupted span is replayed from its first frozen window.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from tempfile import TemporaryFile
from typing import Annotated, Any, Iterator, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
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
    _json_value,
)

VIDEO_EXECUTION_PLAN_SCHEMA = "aap-video-execution-plan/v1"
VIDEO_CHECKPOINT_CONTENT_SCHEMA = "aap-video-window-checkpoint-content/v1"
VIDEO_CHECKPOINT_SCHEMA = "aap-video-window-checkpoint/v1"
VIDEO_CHECKPOINT_PART_SCHEMA = "aap-video-window-checkpoint-part/v1"
MAX_VIDEO_PLAN_BYTES = MAX_REQUEST_BYTES
MAX_VIDEO_CHECKPOINT_BYTES = 64 * 1024 * 1024
MAX_VIDEO_CHECKPOINT_PART_BYTES = MAX_PART_BYTES
MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS = MAX_PART_BASE64_CHARS
MAX_VIDEO_CHECKPOINT_PARTS = (
    MAX_VIDEO_CHECKPOINT_BYTES + MAX_VIDEO_CHECKPOINT_PART_BYTES - 1
) // MAX_VIDEO_CHECKPOINT_PART_BYTES
MAX_VIDEO_WINDOWS = 8192
MAX_VIDEO_RESULT_ROWS = 1_000_000

_SHA256_PREFIXED = "sha256:"
_GeometryDigest = Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_BoundedName = Annotated[StrictStr, Field(min_length=1, max_length=255)]


class VideoExecutionContractFailure(Exception):
    """Closed validation or checkpoint conflict without provider details."""

    def __init__(self, code: str, *, next_span_revision: int | None = None):
        self.code = code
        self.next_span_revision = next_span_revision
        super().__init__(code)


class _Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


def _json_python(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_python(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, UUID):
        return str(value)
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_python(child) for key, child in value.items()}
    if type(value) in {list, tuple}:
        return [_json_python(child) for child in value]
    if value is None or type(value) in {str, bool, int, float}:
        return value
    raise ValueError("value is not strict JSON")


def canonical_video_json(value: Any) -> bytes:
    try:
        value = _json_python(value)
        _json_value(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoExecutionContractFailure("invalid_json_value") from None
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_video_json(value)).hexdigest()


def _tracker_geometry_digest(geometry: Mapping[str, Any], outside: bool) -> str:
    """Match Labeling's existing staged candidate digest byte for byte."""
    try:
        raw = json.dumps(
            {"geometry": dict(geometry) or {}, "outside": outside},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoExecutionContractFailure("invalid_frame_result") from None
    return _SHA256_PREFIXED + hashlib.sha256(raw).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_json_object(value: Any, *, max_bytes: int) -> dict[str, Any]:
    try:
        if type(value) is bytes:
            raw = value
        elif type(value) is str:
            raw = value.encode("utf-8", errors="strict")
        elif isinstance(value, BaseModel):
            raw = canonical_video_json(value)
        elif isinstance(value, Mapping):
            raw = canonical_video_json(dict(value))
        else:
            raise VideoExecutionContractFailure("invalid_response")
        if len(raw) > max_bytes:
            raise VideoExecutionContractFailure("message_too_large")
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except VideoExecutionContractFailure:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise VideoExecutionContractFailure("invalid_response") from None
    if type(parsed) is not dict:
        raise VideoExecutionContractFailure("invalid_response")
    return parsed


def _validate_model(model: type[_Value], value: Any, *, max_bytes: int):
    raw = _decode_json_object(value, max_bytes=max_bytes)
    try:
        return model.model_validate_json(canonical_video_json(raw))
    except (TypeError, ValueError, ValidationError):
        raise VideoExecutionContractFailure("invalid_response") from None


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    try:
        _json_value(value)
    except ValueError:
        raise ValueError(f"{name} must contain only strict JSON") from None
    return value


class VideoSourceIdentity(_Value):
    """Exact retained source version and measured video facts."""

    source_version_id: _BoundedName
    source_sha256: Digest
    byte_size: PositiveInt
    frame_count: PositiveInt
    width: PositiveInt
    height: PositiveInt
    duration_ms: NonnegativeInt
    video_stream: NonnegativeInt

    @field_validator("source_version_id")
    @classmethod
    def version_is_real(cls, value: str) -> str:
        if not value.strip() or value == "null":
            raise ValueError("source version must be exact")
        return value


class VideoTimebaseIdentity(_Value):
    """Frozen selected-frame timing and the accepted annotation grid."""

    timebase_digest: Digest
    selected_frame_count: PositiveInt
    sampling_step: PositiveInt
    grid_step: PositiveInt


class BackendCompatibilityIdentity(_Value):
    """Non-secret model/backend identity required before checkpoint reuse."""

    role: Literal["track", "discovery"]
    provider_kind: Literal["registry", "builtin"] = "registry"
    registry_id: CanonicalUUID | None
    backend_incarnation: CanonicalUUID | None
    builtin_implementation_id: _BoundedName | None = None
    builtin_implementation_digest: Digest | None = None
    model_key: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    model_id: _BoundedName | None
    weights_release_id: _BoundedName | None
    weights_sha256: Digest | None
    tracker_context_mode: Literal["none", "session"]
    max_context_frames: NonnegativeInt
    max_window_frames: PositiveInt | None
    capability_digest: Digest | None

    @model_validator(mode="after")
    def weights_pair(self) -> BackendCompatibilityIdentity:
        if (self.weights_release_id is None) != (self.weights_sha256 is None):
            raise ValueError("weights release id and digest must be jointly known")
        if self.tracker_context_mode == "none" and self.max_context_frames != 0:
            raise ValueError("stateless tracker cannot declare context frames")
        if self.tracker_context_mode == "session" and self.max_context_frames < 1:
            raise ValueError("session tracker requires a positive context bound")
        if self.provider_kind == "registry":
            if self.registry_id is None or any(
                value is not None
                for value in (
                    self.builtin_implementation_id,
                    self.builtin_implementation_digest,
                )
            ):
                raise ValueError(
                    "registry provider requires its actual registry identity"
                )
        elif (
            self.role != "track"
            or self.model_key != "mock_bbox"
            or self.model_id != "mock_bbox"
            or self.registry_id is not None
            or self.backend_incarnation is not None
            or self.weights_release_id is not None
            or self.weights_sha256 is not None
            or self.tracker_context_mode != "none"
            or self.builtin_implementation_id != "aap-mock-bbox/v1"
        ):
            raise ValueError(
                "builtin provider must identify the local mock implementation"
            )
        return self

    @property
    def identity_complete(self) -> bool:
        if self.provider_kind == "builtin":
            return (
                self.builtin_implementation_digest is not None
                and self.capability_digest is not None
            )
        return (
            self.backend_incarnation is not None
            and self.model_id is not None
            and self.weights_release_id is not None
            and self.weights_sha256 is not None
            and self.capability_digest is not None
        )


class VideoOutputIdentity(_Value):
    geometry: Literal["bbox", "polygon", "mask"]
    grid_step: PositiveInt
    result_schema: Literal["aap-video-tracker-frame-results/v1"] = (
        "aap-video-tracker-frame-results/v1"
    )


class FrozenWindowSettings(_Value):
    """Effective platform/backend window bounds captured when the plan is made."""

    platform_default_frames: PositiveInt
    platform_sam3_frames: PositiveInt
    track_backend_max_frames: PositiveInt | None
    effective_track_max_frames: PositiveInt
    combo_discovery_window_frames: PositiveInt | None
    discovery_backend_max_frames: PositiveInt | None
    effective_discovery_max_frames: PositiveInt | None

    @model_validator(mode="after")
    def effective_limits(self) -> FrozenWindowSettings:
        if self.effective_track_max_frames > max(
            self.platform_default_frames, self.platform_sam3_frames
        ):
            raise ValueError("effective track window exceeds platform limits")
        if (
            self.track_backend_max_frames is not None
            and self.effective_track_max_frames > self.track_backend_max_frames
        ):
            raise ValueError("effective track window exceeds backend limit")
        if (self.combo_discovery_window_frames is None) != (
            self.effective_discovery_max_frames is None
        ):
            raise ValueError("combo discovery platform limits must be jointly present")
        if self.combo_discovery_window_frames is not None:
            assert self.combo_discovery_window_frames is not None
            assert self.effective_discovery_max_frames is not None
            discovery_limits = [
                self.combo_discovery_window_frames,
                self.platform_sam3_frames,
            ]
            if self.discovery_backend_max_frames is not None:
                discovery_limits.append(self.discovery_backend_max_frames)
            if self.effective_discovery_max_frames > min(discovery_limits):
                raise ValueError("effective discovery window exceeds a frozen limit")
        return self


class VideoContextSpanSpec(_Value):
    start_ordinal: NonnegativeInt
    end_ordinal: NonnegativeInt
    from_frame: NonnegativeInt
    to_frame: NonnegativeInt
    mode: Literal["none", "session"]

    @model_validator(mode="after")
    def ordered_span(self) -> VideoContextSpanSpec:
        if self.start_ordinal > self.end_ordinal or self.from_frame > self.to_frame:
            raise ValueError("context span must be ordered")
        if self.mode == "none" and self.start_ordinal != self.end_ordinal:
            raise ValueError("stateless context span must contain one window")
        return self


class VideoExecutionContextSpan(VideoContextSpanSpec):
    span_digest: Digest

    @model_validator(mode="after")
    def span_identity(self) -> VideoExecutionContextSpan:
        value = self.model_dump(mode="json", exclude={"span_digest"})
        if self.span_digest != _digest(value):
            raise ValueError("context span digest does not match its identity")
        return self


def make_video_context_span(
    *,
    start_ordinal: int,
    end_ordinal: int,
    from_frame: int,
    to_frame: int,
    mode: Literal["none", "session"],
) -> VideoExecutionContextSpan:
    fields = {
        "start_ordinal": start_ordinal,
        "end_ordinal": end_ordinal,
        "from_frame": from_frame,
        "to_frame": to_frame,
        "mode": mode,
    }
    typed = _validate_model(VideoContextSpanSpec, fields, max_bytes=1024)
    return VideoExecutionContextSpan(
        **typed.model_dump(mode="python"), span_digest=_digest(typed)
    )


class VideoExecutionWindowSpec(_Value):
    phase: Literal["discovery", "track"]
    backend_role: Literal["track", "discovery"]
    branch: Literal["forward", "backward"]
    ordinal: NonnegativeInt
    from_frame: NonnegativeInt
    to_frame: NonnegativeInt
    is_seed_window: StrictBool
    seed_required: StrictBool
    seed_source: Literal[
        "none",
        "prompt",
        "source_geometry",
        "correction",
        "discovery",
        "continuation",
        "session",
    ]
    seed_digest: Digest | None
    context_span: VideoExecutionContextSpan

    @model_validator(mode="after")
    def coherent_window(self) -> VideoExecutionWindowSpec:
        if self.from_frame > self.to_frame:
            raise ValueError("window frame range must be ordered")
        requires_seed = self.seed_source not in {"none", "session"}
        if self.seed_required is not requires_seed:
            raise ValueError("seed flag does not match the frozen seed source")
        fixed_seed = self.seed_source in {
            "prompt",
            "source_geometry",
            "correction",
        }
        if fixed_seed != (self.seed_digest is not None):
            raise ValueError("fixed seed sources require exactly one seed digest")
        if self.phase == "discovery":
            if (
                self.backend_role != "discovery"
                or self.branch != "forward"
                or self.is_seed_window
                or self.seed_required
                or self.seed_source != "none"
                or self.context_span.mode != "none"
            ):
                raise ValueError("discovery window must be an unseeded forward pass")
        elif self.backend_role != "track":
            raise ValueError("track window must use the track backend role")
        if self.context_span.mode == "none" and (
            self.context_span.start_ordinal != self.ordinal
            or self.context_span.end_ordinal != self.ordinal
            or self.context_span.from_frame != self.from_frame
            or self.context_span.to_frame != self.to_frame
        ):
            raise ValueError("stateless context span must equal its window")
        if self.context_span.mode == "session":
            if not (
                self.context_span.start_ordinal
                <= self.ordinal
                <= self.context_span.end_ordinal
            ) or not (
                self.context_span.from_frame <= self.from_frame
                and self.to_frame <= self.context_span.to_frame
            ):
                raise ValueError("window falls outside its context span")
            if (
                self.ordinal != self.context_span.start_ordinal
                and self.seed_source != "session"
            ):
                raise ValueError("only a context span's first window accepts a seed")
        return self


class VideoExecutionWindow(VideoExecutionWindowSpec):
    window_digest: Digest

    @model_validator(mode="after")
    def window_identity(self) -> VideoExecutionWindow:
        value = self.model_dump(mode="json", exclude={"window_digest"})
        if self.window_digest != _digest(value):
            raise ValueError("window digest does not match its frozen identity")
        return self


class VideoExecutionPlan(_Value):
    schema_: Literal["aap-video-execution-plan/v1"] = Field(alias="schema")
    request_id: CanonicalUUID
    input_id: CanonicalUUID
    task_id: CanonicalUUID
    project_id: CanonicalUUID
    request_digest: Digest
    prompt_digest: Digest
    source_plan_digest: Digest
    source: VideoSourceIdentity
    timebase: VideoTimebaseIdentity
    requested_model_key: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    job_kind: Literal["tracking", "correction"]
    direction: Literal["forward", "backward", "bidirectional"]
    from_frame: NonnegativeInt
    to_frame: NonnegativeInt
    correction_frame: NonnegativeInt | None
    segment_id: CanonicalUUID | None
    window_settings: FrozenWindowSettings
    backend_compatibility: Annotated[
        tuple[BackendCompatibilityIdentity, ...], Field(min_length=1, max_length=2)
    ]
    output: VideoOutputIdentity
    windows: Annotated[
        tuple[VideoExecutionWindow, ...],
        Field(min_length=1, max_length=MAX_VIDEO_WINDOWS),
    ]
    plan_digest: Digest

    @field_validator("backend_compatibility", "windows", mode="before")
    @classmethod
    def arrays_to_tuple(cls, value: Any):
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("plan collections must be arrays")

    @model_validator(mode="after")
    def frozen_plan(self) -> VideoExecutionPlan:
        if self.from_frame > self.to_frame or self.to_frame >= self.source.frame_count:
            raise ValueError("plan range is outside the frozen source")
        if self.output.grid_step != self.timebase.grid_step:
            raise ValueError("output grid does not match the frozen timebase")
        expected_selected = (
            self.source.frame_count + self.timebase.sampling_step - 1
        ) // self.timebase.sampling_step
        if self.timebase.selected_frame_count > expected_selected:
            raise ValueError("selected frame count exceeds the frozen sampling grid")
        roles = [item.role for item in self.backend_compatibility]
        if roles not in (["track"], ["track", "discovery"]):
            raise ValueError("backend compatibility roles must be track then discovery")
        combo = self.requested_model_key == "sam3_video_combo"
        if combo != (roles == ["track", "discovery"]):
            raise ValueError("combo plans require both frozen backend roles")
        by_role = {item.role: item for item in self.backend_compatibility}
        if by_role["track"].model_key != (
            "sam3_video_interactive" if combo else self.requested_model_key
        ):
            raise ValueError("track backend model does not match the request")
        if combo and by_role["discovery"].model_key != "sam3_video":
            raise ValueError("combo discovery must use sam3_video")
        if self.job_kind == "tracking":
            if self.correction_frame is not None or self.direction == "bidirectional":
                raise ValueError("tracking plans cannot carry correction branches")
        elif self.correction_frame is None or not (
            self.from_frame <= self.correction_frame <= self.to_frame
        ):
            raise ValueError("correction plan requires an in-range correction frame")
        self._validate_windows(by_role=by_role, combo=combo)
        track_platform_limit = (
            self.window_settings.platform_sam3_frames
            if self.requested_model_key.startswith("sam3")
            else self.window_settings.platform_default_frames
        )
        track_limits = [track_platform_limit]
        if by_role["track"].max_window_frames is not None:
            track_limits.append(by_role["track"].max_window_frames)
        if self.window_settings.effective_track_max_frames > min(track_limits):
            raise ValueError("effective track window exceeds its selected model limit")
        if (
            self.window_settings.track_backend_max_frames
            != by_role["track"].max_window_frames
        ):
            raise ValueError("track backend window capability changed")
        if combo:
            if (
                self.window_settings.combo_discovery_window_frames is None
                or self.window_settings.effective_discovery_max_frames is None
            ):
                raise ValueError("combo plan omitted frozen discovery limits")
            if (
                self.window_settings.discovery_backend_max_frames
                != by_role["discovery"].max_window_frames
            ):
                raise ValueError("discovery backend window capability changed")
        elif any(
            value is not None
            for value in (
                self.window_settings.combo_discovery_window_frames,
                self.window_settings.discovery_backend_max_frames,
                self.window_settings.effective_discovery_max_frames,
            )
        ):
            raise ValueError("non-combo plan cannot carry discovery limits")
        identity = self.model_dump(mode="json", by_alias=True, exclude={"plan_digest"})
        if self.plan_digest != _digest(identity):
            raise ValueError("plan digest does not match its frozen identity")
        return self

    def _validate_windows(
        self,
        *,
        by_role: Mapping[str, BackendCompatibilityIdentity],
        combo: bool,
    ) -> None:
        if [window.ordinal for window in self.windows] != list(
            range(len(self.windows))
        ):
            raise ValueError("window ordinals must be contiguous and ordered")
        discoveries = [window for window in self.windows if window.phase == "discovery"]
        tracks = [window for window in self.windows if window.phase == "track"]
        if not tracks or len(discoveries) != int(combo):
            raise ValueError("plan has incomplete discovery or tracking windows")
        if discoveries and discoveries[0] != self.windows[0]:
            raise ValueError("discovery must be the first frozen phase")
        if discoveries and (
            (
                discoveries[0].to_frame != self.to_frame
                if self.direction == "backward"
                else discoveries[0].from_frame != self.from_frame
            )
            or discoveries[0].to_frame > self.to_frame
            or discoveries[0].to_frame - discoveries[0].from_frame + 1
            > (self.window_settings.effective_discovery_max_frames or 0)
        ):
            raise ValueError("discovery window exceeds its frozen source or limit")
        for window in self.windows:
            if (
                window.from_frame < self.from_frame
                or window.to_frame > self.to_frame
                or window.to_frame - window.from_frame + 1
                > (
                    self.window_settings.effective_discovery_max_frames
                    if window.phase == "discovery"
                    else self.window_settings.effective_track_max_frames
                )
            ):
                raise ValueError("window exceeds its frozen range or size")
            backend = by_role[window.backend_role]
            if window.context_span.mode == "session" and (
                backend.tracker_context_mode != "session"
                or window.context_span.to_frame - window.context_span.from_frame + 1
                > backend.max_context_frames
            ):
                raise ValueError("session span exceeds the frozen backend capability")
        if combo and not tracks[0].seed_source == "discovery":
            raise ValueError("combo tracking must consume discovery seeds")
        if not combo and any(window.seed_source == "discovery" for window in tracks):
            raise ValueError("non-combo plan cannot consume discovery seeds")
        expected_branches = (
            {"forward", "backward"}
            if self.direction == "bidirectional"
            else {self.direction}
        )
        if {window.branch for window in tracks} != expected_branches:
            raise ValueError("track branches do not match the frozen direction")
        branch_order = [window.branch for window in tracks]
        if self.direction == "bidirectional" and branch_order[0] != "backward":
            raise ValueError(
                "bidirectional execution must freeze backward before forward"
            )
        for branch in expected_branches:
            branch_windows = [window for window in tracks if window.branch == branch]
            if not branch_windows:
                raise ValueError("direction branch omitted its windows")
            if not branch_windows[0].is_seed_window:
                raise ValueError("each direction branch must freeze its seed window")
            if branch_windows[0].seed_source in {"session", "continuation"}:
                raise ValueError(
                    "a direction branch cannot begin from mutable session state"
                )
            if any(window.is_seed_window for window in branch_windows[1:]):
                raise ValueError(
                    "only the first window of each branch is a seed window"
                )
            if self.job_kind == "tracking" and self.direction == "forward":
                if (
                    branch_windows[0].from_frame != self.from_frame
                    or branch_windows[-1].to_frame != self.to_frame
                ):
                    raise ValueError("forward windows do not cover the frozen range")
                for left, right in zip(branch_windows, branch_windows[1:]):
                    if right.from_frame != left.to_frame + 1:
                        raise ValueError("forward windows are not contiguous")
            elif self.job_kind == "tracking" and self.direction == "backward":
                if (
                    branch_windows[0].to_frame != self.to_frame
                    or branch_windows[-1].from_frame != self.from_frame
                ):
                    raise ValueError("backward windows do not cover the frozen range")
                for left, right in zip(branch_windows, branch_windows[1:]):
                    if right.to_frame != left.from_frame - 1:
                        raise ValueError("backward windows are not contiguous")
            elif self.job_kind == "correction" and branch == "backward":
                if (
                    branch_windows[0].to_frame != self.correction_frame
                    or branch_windows[-1].from_frame != self.from_frame
                ):
                    raise ValueError("correction backward windows changed their range")
                for left, right in zip(branch_windows, branch_windows[1:]):
                    if right.to_frame != left.from_frame - 1:
                        raise ValueError("backward windows are not contiguous")
            else:
                expected_start = (
                    self.correction_frame
                    if self.job_kind == "correction"
                    else self.from_frame
                )
                if (
                    branch_windows[0].from_frame != expected_start
                    or branch_windows[-1].to_frame != self.to_frame
                ):
                    raise ValueError("correction forward windows changed their range")
                for left, right in zip(branch_windows, branch_windows[1:]):
                    if right.from_frame != left.to_frame + 1:
                        raise ValueError("forward windows are not contiguous")
            for window in branch_windows[1:]:
                if window.context_span.start_ordinal == window.ordinal:
                    if window.seed_source != "continuation":
                        raise ValueError(
                            "later span must seed from prior portable state"
                        )
                    if not window.seed_required:
                        raise ValueError(
                            "later context span requires a frozen continuation seed"
                        )
                elif window.seed_source != "session":
                    raise ValueError("in-span window must use the frozen session")
                elif window.seed_required:
                    raise ValueError(
                        "in-session windows cannot depend on an external seed"
                    )
        self._validate_context_groups()

    def _validate_context_groups(self) -> None:
        groups: dict[str, list[VideoExecutionWindow]] = {}
        for window in self.windows:
            span = window.context_span
            groups.setdefault(span.span_digest, []).append(window)
        for span_digest, members in groups.items():
            span = members[0].context_span
            if any(item.context_span != span for item in members):
                raise ValueError("context span identity changed between windows")
            expected_ordinals = list(range(span.start_ordinal, span.end_ordinal + 1))
            if [item.ordinal for item in members] != expected_ordinals:
                raise ValueError("context span windows must be contiguous")
            if (
                min(item.from_frame for item in members) != span.from_frame
                or max(item.to_frame for item in members) != span.to_frame
            ):
                raise ValueError("context span frame bounds do not match its windows")
            if (
                len({item.phase for item in members}) != 1
                or len({item.branch for item in members}) != 1
            ):
                raise ValueError("context span cannot cross tracker phases or branches")
            if span.mode == "none" and len(members) != 1:
                raise ValueError("stateless span must contain one window")

    @property
    def compatibility_complete(self) -> bool:
        return all(item.identity_complete for item in self.backend_compatibility)


def make_video_execution_plan(
    *,
    request_id: UUID | str,
    input_id: UUID | str,
    task_id: UUID | str,
    project_id: UUID | str,
    request_digest: str,
    prompt_digest: str,
    source_plan_digest: str,
    source: VideoSourceIdentity | Mapping[str, Any],
    timebase: VideoTimebaseIdentity | Mapping[str, Any],
    requested_model_key: str,
    job_kind: Literal["tracking", "correction"],
    direction: Literal["forward", "backward", "bidirectional"],
    from_frame: int,
    to_frame: int,
    correction_frame: int | None,
    segment_id: UUID | str | None,
    window_settings: FrozenWindowSettings | Mapping[str, Any],
    backend_compatibility: Sequence[BackendCompatibilityIdentity | Mapping[str, Any]],
    output: VideoOutputIdentity | Mapping[str, Any],
    windows: Sequence[VideoExecutionWindowSpec | Mapping[str, Any]],
) -> VideoExecutionPlan:
    """Freeze caller-supplied primitive facts; never consult current settings."""
    try:
        typed_source = _validate_model(
            VideoSourceIdentity, source, max_bytes=MAX_VIDEO_PLAN_BYTES
        )
        typed_timebase = _validate_model(
            VideoTimebaseIdentity, timebase, max_bytes=1024
        )
        typed_settings = _validate_model(
            FrozenWindowSettings, window_settings, max_bytes=1024
        )
        typed_output = _validate_model(VideoOutputIdentity, output, max_bytes=1024)
        typed_backends = tuple(
            _validate_model(BackendCompatibilityIdentity, item, max_bytes=4096)
            for item in backend_compatibility
        )
        typed_specs = tuple(
            _validate_model(VideoExecutionWindowSpec, item, max_bytes=4096)
            for item in windows
        )
        frozen_windows = []
        for spec in typed_specs:
            window_fields = spec.model_dump(mode="json")
            frozen_windows.append(
                VideoExecutionWindow(
                    **window_fields,
                    window_digest=_digest(window_fields),
                )
            )
        draft = {
            "schema": VIDEO_EXECUTION_PLAN_SCHEMA,
            "request_id": str(request_id),
            "input_id": str(input_id),
            "task_id": str(task_id),
            "project_id": str(project_id),
            "request_digest": request_digest,
            "prompt_digest": prompt_digest,
            "source_plan_digest": source_plan_digest,
            "source": typed_source.model_dump(mode="json"),
            "timebase": typed_timebase.model_dump(mode="json"),
            "requested_model_key": requested_model_key,
            "job_kind": job_kind,
            "direction": direction,
            "from_frame": from_frame,
            "to_frame": to_frame,
            "correction_frame": correction_frame,
            "segment_id": str(segment_id) if segment_id is not None else None,
            "window_settings": typed_settings.model_dump(mode="json"),
            "backend_compatibility": [
                item.model_dump(mode="json") for item in typed_backends
            ],
            "output": typed_output.model_dump(mode="json"),
            "windows": [item.model_dump(mode="json") for item in frozen_windows],
        }
        draft["plan_digest"] = _digest(draft)
        plan = _validate_model(
            VideoExecutionPlan, draft, max_bytes=MAX_VIDEO_PLAN_BYTES
        )
        if len(canonical_video_json(plan)) > MAX_VIDEO_PLAN_BYTES:
            raise VideoExecutionContractFailure("plan_too_large")
        return plan
    except VideoExecutionContractFailure:
        raise
    except (TypeError, ValueError, ValidationError):
        raise VideoExecutionContractFailure("invalid_plan") from None


def decode_video_execution_plan(value: Any) -> VideoExecutionPlan:
    return _validate_model(
        VideoExecutionPlan,
        value,
        max_bytes=MAX_VIDEO_PLAN_BYTES,
    )


class VideoFrameResultRow(_Value):
    """Canonical candidate row compatible with the current staged tracker result."""

    frame_index: NonnegativeInt
    geometry: dict[str, Any]
    confidence: StrictFloat | None
    outside: StrictBool
    instance_id: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None
    primary: StrictBool
    geometry_digest: _GeometryDigest
    candidate_key: Annotated[StrictStr, Field(min_length=1, max_length=512)]

    @field_validator("geometry", mode="before")
    @classmethod
    def geometry_is_json_object(cls, value: Any):
        return _json_object(value, "geometry")

    @model_validator(mode="after")
    def candidate_identity(self) -> VideoFrameResultRow:
        expected_geometry_digest = _tracker_geometry_digest(
            self.geometry or {}, self.outside
        )
        expected_key = (
            f"{self.instance_id or '1'}:{self.frame_index}:{expected_geometry_digest}"
        )
        if (
            self.geometry_digest != expected_geometry_digest
            or self.candidate_key != expected_key
        ):
            raise ValueError("frame result candidate identity is not canonical")
        return self


def _frame_result_row(value: Mapping[str, Any]) -> VideoFrameResultRow:
    allowed = {
        "frame_index",
        "geometry",
        "confidence",
        "outside",
        "instance_id",
        "primary",
        "geometry_digest",
        "candidate_key",
    }
    required = {
        "frame_index",
        "geometry",
        "confidence",
        "outside",
        "instance_id",
        "primary",
    }
    if (
        not isinstance(value, Mapping)
        or not required.issubset(value)
        or set(value) - allowed
    ):
        raise VideoExecutionContractFailure("invalid_frame_result")
    normalized = dict(value)
    if normalized["geometry"] is None:
        normalized["geometry"] = {}
    elif type(normalized["geometry"]) is not dict:
        raise VideoExecutionContractFailure("invalid_frame_result")
    geometry_digest = _tracker_geometry_digest(
        normalized["geometry"], normalized["outside"]
    )
    normalized.setdefault("geometry_digest", geometry_digest)
    normalized.setdefault(
        "candidate_key",
        f"{normalized['instance_id'] or '1'}:{normalized['frame_index']}:{geometry_digest}",
    )
    return _validate_model(
        VideoFrameResultRow, normalized, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
    )


def normalize_video_frame_results(
    rows: Iterable[Mapping[str, Any]], *, branch: Literal["forward", "backward"]
) -> tuple[VideoFrameResultRow, ...]:
    """Normalize one closed span's current tracker rows in deterministic order."""
    try:
        normalized = [_frame_result_row(row) for row in rows]
        normalized.sort(
            key=lambda row: (
                row.frame_index if branch == "forward" else -row.frame_index,
                row.instance_id or "",
                not row.primary,
                row.geometry_digest,
            )
        )
        identities = [(row.frame_index, row.instance_id or "1") for row in normalized]
        if len(identities) != len(set(identities)):
            raise VideoExecutionContractFailure("duplicate_frame_instance_result")
        if len(normalized) > MAX_VIDEO_RESULT_ROWS:
            raise VideoExecutionContractFailure("checkpoint_too_many_rows")
        return tuple(normalized)
    except VideoExecutionContractFailure:
        raise
    except (TypeError, ValueError, ValidationError):
        raise VideoExecutionContractFailure("invalid_frame_result") from None


class VideoGeometryByInstance(_Value):
    instance_id: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    geometry: dict[str, Any]

    @field_validator("geometry", mode="before")
    @classmethod
    def geometry_is_json_object(cls, value: Any):
        return _json_object(value, "geometry")


class VideoContinuationPostState(_Value):
    """Portable tracker state; opaque backend session tokens are intentionally absent."""

    last_geometry: dict[str, Any] | None
    last_geom_by_instance: Annotated[
        tuple[VideoGeometryByInstance, ...], Field(max_length=100_000)
    ]
    mp_prev_boundary: Annotated[
        tuple[VideoGeometryByInstance, ...], Field(max_length=100_000)
    ]
    mp_next_global: PositiveInt

    @field_validator("last_geometry", mode="before")
    @classmethod
    def last_geometry_is_object(cls, value: Any):
        return None if value is None else _json_object(value, "last_geometry")

    @field_validator("last_geom_by_instance", "mp_prev_boundary", mode="before")
    @classmethod
    def geometry_map_to_sorted_tuple(cls, value: Any):
        if isinstance(value, Mapping):
            return tuple(
                {"instance_id": key, "geometry": item}
                for key, item in sorted(value.items())
            )
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("instance geometry state must be an object or array")

    @model_validator(mode="after")
    def sorted_unique_instances(self) -> VideoContinuationPostState:
        for values in (self.last_geom_by_instance, self.mp_prev_boundary):
            ids = [item.instance_id for item in values]
            if ids != sorted(set(ids)):
                raise ValueError("instance geometry state must be sorted and unique")
        return self


def continuation_state_digest(
    value: VideoContinuationPostState | Mapping[str, Any],
) -> str:
    state = _validate_model(
        VideoContinuationPostState, value, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
    )
    return _stream_json_digest(
        _continuation_document(state), max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
    )[0]


def _continuation_document(state: VideoContinuationPostState) -> dict[str, Any]:
    return {
        "last_geometry": state.last_geometry,
        "last_geom_by_instance": state.last_geom_by_instance,
        "mp_prev_boundary": state.mp_prev_boundary,
        "mp_next_global": state.mp_next_global,
    }


class VideoDiscoverySeed(_Value):
    obj_id: PositiveInt
    geometry: dict[str, Any]

    @field_validator("geometry", mode="before")
    @classmethod
    def geometry_is_json_object(cls, value: Any):
        return _json_object(value, "geometry")


def combo_discovery_seeds(
    rows: Sequence[VideoFrameResultRow | Mapping[str, Any]], *, seed_frame: int
) -> tuple[VideoDiscoverySeed, ...]:
    """Mirror the current multiplex discovery-to-PVS seed ordering."""
    normalized = [
        row if type(row) is VideoFrameResultRow else _frame_result_row(row)
        for row in rows
    ]
    ordered = sorted(
        normalized, key=lambda row: (row.instance_id or "", row.frame_index)
    )
    seeds = []
    for row in ordered:
        if row.frame_index != seed_frame or row.outside or not row.geometry:
            continue
        seeds.append(VideoDiscoverySeed(obj_id=len(seeds) + 1, geometry=row.geometry))
    return tuple(seeds)


def discovery_seeds_digest(
    seeds: Sequence[VideoDiscoverySeed | Mapping[str, Any]],
) -> str:
    values = tuple(
        item
        if type(item) is VideoDiscoverySeed
        else _validate_model(VideoDiscoverySeed, item, max_bytes=4096)
        for item in seeds
    )
    if [item.obj_id for item in values] != list(range(1, len(values) + 1)):
        raise VideoExecutionContractFailure("discovery_seed_ids_not_canonical")
    return _stream_json_digest(values, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES)[0]


class VideoCheckpointPayload(_Value):
    schema_: Literal["aap-video-window-checkpoint-content/v1"] = Field(alias="schema")
    plan_digest: Digest
    context_span_digest: Digest
    span_revision: PositiveInt
    window_ordinals: Annotated[
        tuple[NonnegativeInt, ...], Field(min_length=1, max_length=MAX_VIDEO_WINDOWS)
    ]
    window_digests: Annotated[
        tuple[Digest, ...], Field(min_length=1, max_length=MAX_VIDEO_WINDOWS)
    ]
    raw_response_digests: Annotated[
        tuple[Digest, ...], Field(min_length=1, max_length=MAX_VIDEO_WINDOWS)
    ]
    seed_input_digest: Digest | None
    predecessor_checkpoint_digest: Digest | None
    frame_results: Annotated[
        tuple[VideoFrameResultRow, ...], Field(max_length=MAX_VIDEO_RESULT_ROWS)
    ]
    continuation: VideoContinuationPostState | None
    discovery_output_digest: Digest | None
    discovery_seeds_digest: Digest | None
    discovery_seeds: Annotated[
        tuple[VideoDiscoverySeed, ...], Field(max_length=100_000)
    ]

    @field_validator(
        "window_ordinals",
        "window_digests",
        "raw_response_digests",
        "frame_results",
        "discovery_seeds",
        mode="before",
    )
    @classmethod
    def arrays_to_tuple(cls, value: Any):
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("checkpoint collections must be arrays")

    @model_validator(mode="after")
    def internal_identity(self) -> VideoCheckpointPayload:
        count = len(self.window_ordinals)
        if (
            self.window_ordinals
            != tuple(range(self.window_ordinals[0], self.window_ordinals[0] + count))
            or len(self.window_digests) != count
            or len(self.raw_response_digests) != count
        ):
            raise ValueError("checkpoint span windows must be contiguous and complete")
        if self.continuation is None:
            if (
                self.discovery_output_digest is None
                or self.discovery_seeds_digest is None
            ):
                raise ValueError(
                    "discovery checkpoint requires output and seed digests"
                )
            rows_digest, _ = _stream_json_digest(
                self.frame_results, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
            )
            seeds_digest = discovery_seeds_digest(self.discovery_seeds)
            if (
                rows_digest != self.discovery_output_digest
                or seeds_digest != self.discovery_seeds_digest
            ):
                raise ValueError(
                    "discovery checkpoint digests do not match their values"
                )
        elif (
            self.discovery_output_digest is not None
            or self.discovery_seeds_digest is not None
            or self.discovery_seeds
        ):
            raise ValueError("track checkpoint cannot replace discovery evidence")
        if (
            self.predecessor_checkpoint_digest is not None
            and self.seed_input_digest is None
        ):
            raise ValueError("checkpoint predecessor requires a seed input digest")
        return self


class VideoCheckpointManifest(_Value):
    schema_: Literal["aap-video-window-checkpoint/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    manifest_digest: Digest
    request_id: CanonicalUUID
    input_id: CanonicalUUID
    plan_digest: Digest
    context_span_digest: Digest
    span_start_ordinal: NonnegativeInt
    span_end_ordinal: NonnegativeInt
    terminal_window_digest: Digest
    window_digests: Annotated[
        tuple[Digest, ...], Field(min_length=1, max_length=MAX_VIDEO_WINDOWS)
    ]
    span_revision: PositiveInt
    context_span_closed: Literal[True]
    predecessor_checkpoint_digest: Digest | None
    raw_response_digests: Annotated[
        tuple[Digest, ...], Field(min_length=1, max_length=MAX_VIDEO_WINDOWS)
    ]
    result_digest: Digest
    result_byte_size: NonnegativeInt
    payload_digest: Digest
    payload_byte_size: PositiveInt
    payload_part_count: PositiveInt
    continuation_digest: Digest | None
    discovery_output_digest: Digest | None
    discovery_seeds_digest: Digest | None

    @model_validator(mode="before")
    @classmethod
    def closed_span_is_strict_true(cls, value: Any):
        if (
            isinstance(value, Mapping)
            and type(value.get("context_span_closed")) is not bool
        ):
            raise ValueError("context span closure must be an explicit boolean")
        if isinstance(value, Mapping) and value.get("context_span_closed") is not True:
            raise ValueError("checkpoint must cover a closed context span")
        return value

    @field_validator("window_digests", "raw_response_digests", mode="before")
    @classmethod
    def arrays_to_tuple(cls, value: Any):
        if isinstance(value, (tuple, list)):
            return tuple(value)
        raise ValueError("manifest collections must be arrays")

    @model_validator(mode="after")
    def immutable_manifest(self) -> VideoCheckpointManifest:
        expected_count = self.span_end_ordinal - self.span_start_ordinal + 1
        if (
            self.span_start_ordinal > self.span_end_ordinal
            or len(self.window_digests) != expected_count
            or len(self.raw_response_digests) != expected_count
            or self.terminal_window_digest != self.window_digests[-1]
            or self.result_byte_size > MAX_VIDEO_CHECKPOINT_BYTES
            or self.payload_byte_size > MAX_VIDEO_CHECKPOINT_BYTES
            or self.payload_part_count
            != max(
                1,
                (self.payload_byte_size + MAX_VIDEO_CHECKPOINT_PART_BYTES - 1)
                // MAX_VIDEO_CHECKPOINT_PART_BYTES,
            )
            or self.payload_part_count > MAX_VIDEO_CHECKPOINT_PARTS
        ):
            raise ValueError("checkpoint manifest violates its frozen bounds")
        core = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"checkpoint_id", "manifest_digest"},
        )
        expected_id = _digest(
            {
                "plan_digest": self.plan_digest,
                "context_span_digest": self.context_span_digest,
                "span_revision": self.span_revision,
            }
        )
        if self.checkpoint_id != expected_id or self.manifest_digest != _digest(core):
            raise ValueError("checkpoint manifest digest mismatch")
        return self


class VideoCheckpointPart(_Value):
    schema_: Literal["aap-video-window-checkpoint-part/v1"] = Field(alias="schema")
    checkpoint_id: Digest
    manifest_digest: Digest
    part_index: NonnegativeInt
    part_count: PositiveInt
    payload_digest: Digest
    payload_byte_size: PositiveInt
    part_digest: Digest
    content: Annotated[
        StrictStr,
        Field(min_length=1, max_length=MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS),
    ]

    @model_validator(mode="after")
    def bounded_part(self) -> VideoCheckpointPart:
        if (
            not 0 <= self.part_index < self.part_count
            or self.part_count > MAX_VIDEO_CHECKPOINT_PARTS
        ):
            raise ValueError("checkpoint part index exceeds its manifest")
        try:
            decoded = codec.decode_part(self.content)
        except (TypeError, ValueError):
            raise ValueError(
                "checkpoint content is not canonical bounded base64"
            ) from None
        if hashlib.sha256(decoded).hexdigest() != self.part_digest:
            raise ValueError("checkpoint part digest mismatch")
        return self


@dataclass(frozen=True, slots=True)
class VideoCheckpointSnapshot:
    manifest: VideoCheckpointManifest
    _snapshot: codec.Snapshot = field(repr=False, compare=False)

    def iter_parts(self) -> Iterator[VideoCheckpointPart]:
        for index, content in enumerate(self._snapshot.iter_parts()):
            yield VideoCheckpointPart(
                schema=VIDEO_CHECKPOINT_PART_SCHEMA,
                checkpoint_id=self.manifest.checkpoint_id,
                manifest_digest=self.manifest.manifest_digest,
                part_index=index,
                part_count=self.manifest.payload_part_count,
                payload_digest=self.manifest.payload_digest,
                payload_byte_size=self.manifest.payload_byte_size,
                part_digest=hashlib.sha256(content).hexdigest(),
                content=codec.encode_part(content),
            )


def _span(
    plan: VideoExecutionPlan, context_span_digest: str
) -> tuple[VideoExecutionWindow, ...]:
    windows = tuple(
        window
        for window in plan.windows
        if window.context_span.span_digest == context_span_digest
    )
    if not windows:
        raise VideoExecutionContractFailure("context_span_not_in_plan")
    return windows


def make_video_checkpoint_payload(
    *,
    plan: VideoExecutionPlan | Mapping[str, Any],
    context_span_digest: str,
    span_revision: int,
    raw_response_digests: Sequence[str],
    frame_results: Iterable[Mapping[str, Any] | VideoFrameResultRow],
    continuation: VideoContinuationPostState | Mapping[str, Any] | None,
    predecessor_checkpoint: VideoCheckpointManifest | Mapping[str, Any] | None = None,
) -> VideoCheckpointPayload:
    """Create one immutable whole-span checkpoint from explicit result facts."""
    typed_plan = decode_video_execution_plan(plan)
    span_windows = _span(typed_plan, context_span_digest)
    rows = normalize_video_frame_results(
        (
            row.model_dump(mode="json", by_alias=True)
            if type(row) is VideoFrameResultRow
            else row
            for row in frame_results
        ),
        branch=span_windows[0].branch,
    )
    typed_continuation = (
        None
        if continuation is None
        else _validate_model(
            VideoContinuationPostState,
            continuation,
            max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
        )
    )
    predecessor = (
        None
        if predecessor_checkpoint is None
        else decode_video_checkpoint_manifest(predecessor_checkpoint)
    )
    if span_windows[0].phase == "discovery":
        if continuation is not None or predecessor is not None:
            raise VideoExecutionContractFailure("discovery_checkpoint_has_predecessor")
        seeds = combo_discovery_seeds(rows, seed_frame=span_windows[0].from_frame)
        output_digest = _stream_json_digest(rows, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES)[
            0
        ]
        seeds_digest = discovery_seeds_digest(seeds)
        seed_input_digest = None
        predecessor_digest = None
        typed_continuation = None
    else:
        seeds = ()
        output_digest = None
        seeds_digest = None
        seed_window = span_windows[0]
        if seed_window.seed_source in {"prompt", "source_geometry", "correction"}:
            if predecessor is not None:
                raise VideoExecutionContractFailure("fixed_seed_has_predecessor")
            seed_input_digest = seed_window.seed_digest
            predecessor_digest = None
        elif seed_window.seed_source in {"discovery", "continuation"}:
            if predecessor is None:
                raise VideoExecutionContractFailure("checkpoint_predecessor_missing")
            if not checkpoint_identity_matches_plan(typed_plan, predecessor):
                raise VideoExecutionContractFailure("checkpoint_predecessor_mismatch")
            if predecessor.span_end_ordinal >= span_windows[0].ordinal:
                raise VideoExecutionContractFailure(
                    "checkpoint_predecessor_order_invalid"
                )
            if seed_window.seed_source == "discovery":
                seed_input_digest = predecessor.discovery_seeds_digest
            else:
                seed_input_digest = predecessor.continuation_digest
            if seed_input_digest is None:
                raise VideoExecutionContractFailure(
                    "checkpoint_predecessor_state_missing"
                )
            predecessor_digest = predecessor.manifest_digest
        else:
            seed_input_digest = None
            predecessor_digest = None
    payload = VideoCheckpointPayload(
        schema=VIDEO_CHECKPOINT_CONTENT_SCHEMA,
        plan_digest=typed_plan.plan_digest,
        context_span_digest=context_span_digest,
        span_revision=span_revision,
        window_ordinals=tuple(window.ordinal for window in span_windows),
        window_digests=tuple(window.window_digest for window in span_windows),
        raw_response_digests=tuple(raw_response_digests),
        seed_input_digest=seed_input_digest,
        predecessor_checkpoint_digest=predecessor_digest,
        frame_results=rows,
        continuation=typed_continuation,
        discovery_output_digest=output_digest,
        discovery_seeds_digest=seeds_digest,
        discovery_seeds=seeds,
    )
    validate_video_checkpoint_payload(
        typed_plan, payload, predecessor_checkpoint=predecessor
    )
    return payload


def validate_video_checkpoint_payload(
    plan: VideoExecutionPlan | Mapping[str, Any],
    payload: VideoCheckpointPayload | Mapping[str, Any],
    *,
    predecessor_checkpoint: VideoCheckpointManifest | Mapping[str, Any] | None = None,
) -> tuple[VideoExecutionPlan, VideoCheckpointPayload]:
    typed_plan = decode_video_execution_plan(plan)
    typed_payload = _validate_model(
        VideoCheckpointPayload, payload, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
    )
    span_windows = _span(typed_plan, typed_payload.context_span_digest)
    if (
        typed_payload.plan_digest != typed_plan.plan_digest
        or typed_payload.window_ordinals
        != tuple(window.ordinal for window in span_windows)
        or typed_payload.window_digests
        != tuple(window.window_digest for window in span_windows)
        or typed_payload.raw_response_digests
        and len(typed_payload.raw_response_digests) != len(span_windows)
    ):
        raise VideoExecutionContractFailure("checkpoint_plan_mismatch")
    if any(
        row.frame_index < span_windows[0].context_span.from_frame
        or row.frame_index > span_windows[0].context_span.to_frame
        for row in typed_payload.frame_results
    ):
        raise VideoExecutionContractFailure("checkpoint_result_outside_span")
    expected_phase = span_windows[0].phase
    if any(window.phase != expected_phase for window in span_windows):
        raise VideoExecutionContractFailure("checkpoint_crosses_execution_phase")
    if expected_phase == "discovery":
        if typed_payload.continuation is not None:
            raise VideoExecutionContractFailure("discovery_has_tracker_continuation")
    elif typed_payload.continuation is None:
        raise VideoExecutionContractFailure("track_checkpoint_missing_continuation")
    seed_window = span_windows[0]
    if expected_phase == "discovery":
        if (
            typed_payload.seed_input_digest is not None
            or typed_payload.predecessor_checkpoint_digest is not None
        ):
            raise VideoExecutionContractFailure("discovery_has_seed_predecessor")
    if seed_window.seed_source in {"prompt", "source_geometry", "correction"}:
        if (
            typed_payload.seed_input_digest != seed_window.seed_digest
            or typed_payload.predecessor_checkpoint_digest is not None
        ):
            raise VideoExecutionContractFailure("checkpoint_seed_input_mismatch")
    elif seed_window.seed_source in {"continuation", "discovery"}:
        if predecessor_checkpoint is None:
            raise VideoExecutionContractFailure("checkpoint_predecessor_missing")
        predecessor = decode_video_checkpoint_manifest(predecessor_checkpoint)
        predecessor_windows = _span(typed_plan, predecessor.context_span_digest)
        if (
            not checkpoint_identity_matches_plan(typed_plan, predecessor)
            or predecessor.manifest_digest
            != typed_payload.predecessor_checkpoint_digest
            or predecessor.span_end_ordinal >= span_windows[0].ordinal
        ):
            raise VideoExecutionContractFailure("checkpoint_predecessor_mismatch")
        if seed_window.seed_source == "discovery":
            if predecessor_windows[0].phase != "discovery":
                raise VideoExecutionContractFailure(
                    "checkpoint_discovery_predecessor_mismatch"
                )
        elif (
            predecessor_windows[0].phase != "track"
            or predecessor_windows[0].branch != span_windows[0].branch
        ):
            raise VideoExecutionContractFailure(
                "checkpoint_branch_predecessor_mismatch"
            )
        expected_seed_digest = (
            predecessor.discovery_seeds_digest
            if seed_window.seed_source == "discovery"
            else predecessor.continuation_digest
        )
        if (
            expected_seed_digest is None
            or typed_payload.seed_input_digest != expected_seed_digest
        ):
            raise VideoExecutionContractFailure("checkpoint_predecessor_state_mismatch")
    elif (
        typed_payload.seed_input_digest is not None
        or typed_payload.predecessor_checkpoint_digest is not None
    ):
        raise VideoExecutionContractFailure("unexpected_checkpoint_seed_input")
    row_keys = [
        (
            row.frame_index
            if span_windows[0].branch == "forward"
            else -row.frame_index,
            row.instance_id or "",
            not row.primary,
            row.geometry_digest,
        )
        for row in typed_payload.frame_results
    ]
    if row_keys != sorted(row_keys):
        raise VideoExecutionContractFailure("checkpoint_results_not_normalized")
    return typed_plan, typed_payload


def _json_default(value: Any):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("value is not part of the video checkpoint contract")


def _stream_json_digest(value: Any, *, max_bytes: int) -> tuple[str, int]:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=_json_default,
    )
    digest = hashlib.sha256()
    byte_size = 0
    try:
        for text in encoder.iterencode(value):
            part = text.encode("utf-8", errors="strict")
            byte_size += len(part)
            if byte_size > max_bytes:
                raise VideoExecutionContractFailure("checkpoint_too_large")
            digest.update(part)
    except VideoExecutionContractFailure:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise VideoExecutionContractFailure("checkpoint_serialization_failed") from None
    return digest.hexdigest(), byte_size


def _stream_json_snapshot(value: Any, *, max_bytes: int) -> codec.Snapshot:
    handle = TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    byte_size = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=_json_default,
    )
    try:
        for text in encoder.iterencode(value):
            chunk = text.encode("utf-8", errors="strict")
            byte_size += len(chunk)
            if byte_size > max_bytes:
                raise VideoExecutionContractFailure("checkpoint_too_large")
            handle.write(chunk)
            digest.update(chunk)
        handle.flush()
        handle.seek(0)
        return codec.Snapshot(
            sha256=digest.hexdigest(),
            byte_size=byte_size,
            part_count=max(
                1,
                (byte_size + MAX_VIDEO_CHECKPOINT_PART_BYTES - 1)
                // MAX_VIDEO_CHECKPOINT_PART_BYTES,
            ),
            _file=handle,
        )
    except VideoExecutionContractFailure:
        handle.close()
        raise
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        handle.close()
        raise VideoExecutionContractFailure("checkpoint_serialization_failed") from None


@contextmanager
def snapshot_video_checkpoint(
    plan: VideoExecutionPlan | Mapping[str, Any],
    payload: VideoCheckpointPayload | Mapping[str, Any],
    *,
    predecessor_checkpoint: VideoCheckpointManifest | Mapping[str, Any] | None = None,
) -> Iterator[VideoCheckpointSnapshot]:
    """Serialize a complete span checkpoint into bounded temporary-file parts."""
    typed_plan, typed_payload = validate_video_checkpoint_payload(
        plan, payload, predecessor_checkpoint=predecessor_checkpoint
    )
    snapshot: codec.Snapshot | None = None
    try:
        result_digest, result_byte_size = _stream_json_digest(
            list(typed_payload.frame_results),
            max_bytes=MAX_VIDEO_CHECKPOINT_BYTES,
        )
        payload_document = {
            "schema": VIDEO_CHECKPOINT_CONTENT_SCHEMA,
            "plan_digest": typed_payload.plan_digest,
            "context_span_digest": typed_payload.context_span_digest,
            "span_revision": typed_payload.span_revision,
            "window_ordinals": typed_payload.window_ordinals,
            "window_digests": typed_payload.window_digests,
            "raw_response_digests": typed_payload.raw_response_digests,
            "seed_input_digest": typed_payload.seed_input_digest,
            "predecessor_checkpoint_digest": typed_payload.predecessor_checkpoint_digest,
            "frame_results": typed_payload.frame_results,
            "continuation": (
                _continuation_document(typed_payload.continuation)
                if typed_payload.continuation is not None
                else None
            ),
            "discovery_output_digest": typed_payload.discovery_output_digest,
            "discovery_seeds_digest": typed_payload.discovery_seeds_digest,
            "discovery_seeds": typed_payload.discovery_seeds,
        }
        snapshot = _stream_json_snapshot(
            payload_document, max_bytes=MAX_VIDEO_CHECKPOINT_BYTES
        )
        span_windows = _span(typed_plan, typed_payload.context_span_digest)
        checkpoint_id = _digest(
            {
                "plan_digest": typed_plan.plan_digest,
                "context_span_digest": typed_payload.context_span_digest,
                "span_revision": typed_payload.span_revision,
            }
        )
        core = {
            "schema": VIDEO_CHECKPOINT_SCHEMA,
            "checkpoint_id": checkpoint_id,
            "request_id": str(typed_plan.request_id),
            "input_id": str(typed_plan.input_id),
            "plan_digest": typed_plan.plan_digest,
            "context_span_digest": typed_payload.context_span_digest,
            "span_start_ordinal": span_windows[0].ordinal,
            "span_end_ordinal": span_windows[-1].ordinal,
            "terminal_window_digest": span_windows[-1].window_digest,
            "window_digests": [item.window_digest for item in span_windows],
            "span_revision": typed_payload.span_revision,
            "context_span_closed": True,
            "predecessor_checkpoint_digest": typed_payload.predecessor_checkpoint_digest,
            "raw_response_digests": list(typed_payload.raw_response_digests),
            "result_digest": result_digest,
            "result_byte_size": result_byte_size,
            "payload_digest": snapshot.sha256,
            "payload_byte_size": snapshot.byte_size,
            "payload_part_count": snapshot.part_count,
            "continuation_digest": (
                continuation_state_digest(typed_payload.continuation)
                if typed_payload.continuation is not None
                else None
            ),
            "discovery_output_digest": typed_payload.discovery_output_digest,
            "discovery_seeds_digest": typed_payload.discovery_seeds_digest,
        }
        manifest_digest = _digest(
            {key: value for key, value in core.items() if key != "checkpoint_id"}
        )
        manifest = _validate_model(
            VideoCheckpointManifest,
            {**core, "manifest_digest": manifest_digest},
            max_bytes=MAX_VIDEO_PLAN_BYTES,
        )
        yield VideoCheckpointSnapshot(manifest, snapshot)
    except VideoExecutionContractFailure:
        raise
    except (OSError, TypeError, ValueError, ValidationError):
        raise VideoExecutionContractFailure("checkpoint_serialization_failed") from None
    finally:
        if snapshot is not None:
            snapshot._file.close()


def decode_video_checkpoint_manifest(value: Any) -> VideoCheckpointManifest:
    return _validate_model(
        VideoCheckpointManifest, value, max_bytes=MAX_VIDEO_PLAN_BYTES
    )


def decode_video_checkpoint_part(value: Any) -> VideoCheckpointPart:
    return _validate_model(
        VideoCheckpointPart,
        value,
        max_bytes=MAX_PART_BASE64_CHARS + 4096,
    )


@contextmanager
def assemble_video_checkpoint_parts(
    manifest: VideoCheckpointManifest | Mapping[str, Any],
    parts: Iterable[VideoCheckpointPart | Mapping[str, Any]],
) -> Iterator[Any]:
    """Verify ordered bounded parts into a temporary file without buffering them."""
    typed_manifest = decode_video_checkpoint_manifest(manifest)
    handle = TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    total_bytes = 0
    part_count = 0
    try:
        for index, value in enumerate(parts):
            part = decode_video_checkpoint_part(value)
            if (
                part.checkpoint_id != typed_manifest.checkpoint_id
                or part.manifest_digest != typed_manifest.manifest_digest
                or part.part_index != index
                or part.part_count != typed_manifest.payload_part_count
                or part.payload_digest != typed_manifest.payload_digest
                or part.payload_byte_size != typed_manifest.payload_byte_size
            ):
                raise VideoExecutionContractFailure("checkpoint_part_identity_mismatch")
            content = codec.decode_part(part.content)
            total_bytes += len(content)
            if total_bytes > typed_manifest.payload_byte_size:
                raise VideoExecutionContractFailure("checkpoint_payload_too_large")
            handle.write(content)
            digest.update(content)
            part_count += 1
        if (
            part_count != typed_manifest.payload_part_count
            or total_bytes != typed_manifest.payload_byte_size
            or digest.hexdigest() != typed_manifest.payload_digest
        ):
            raise VideoExecutionContractFailure("checkpoint_payload_incomplete")
        handle.flush()
        handle.seek(0)
        yield handle
    except VideoExecutionContractFailure:
        raise
    except (OSError, TypeError, ValueError, ValidationError):
        raise VideoExecutionContractFailure("invalid_checkpoint_part") from None
    finally:
        handle.close()


def checkpoint_identity_matches_plan(
    plan: VideoExecutionPlan | Mapping[str, Any],
    manifest: VideoCheckpointManifest | Mapping[str, Any],
) -> bool:
    typed_plan = decode_video_execution_plan(plan)
    typed_manifest = decode_video_checkpoint_manifest(manifest)
    span_windows = _span(typed_plan, typed_manifest.context_span_digest)
    predecessor_required = span_windows[0].seed_source in {
        "discovery",
        "continuation",
    }
    return (
        typed_manifest.plan_digest == typed_plan.plan_digest
        and typed_manifest.request_id == typed_plan.request_id
        and typed_manifest.input_id == typed_plan.input_id
        and typed_manifest.span_start_ordinal == span_windows[0].ordinal
        and typed_manifest.span_end_ordinal == span_windows[-1].ordinal
        and typed_manifest.window_digests
        == tuple(item.window_digest for item in span_windows)
        and typed_manifest.terminal_window_digest == span_windows[-1].window_digest
        and typed_manifest.context_span_closed is True
        and (typed_manifest.predecessor_checkpoint_digest is not None)
        == predecessor_required
    )


class VideoResumeDecision(_Value):
    action: Literal["complete", "execute_span", "replay_span", "reuse_span"]
    context_span_digest: Digest | None
    start_ordinal: NonnegativeInt | None
    end_ordinal: NonnegativeInt | None
    span_revision: PositiveInt | None
    checkpoint_id: Digest | None


def _context_spans(
    plan: VideoExecutionPlan,
) -> tuple[tuple[VideoExecutionWindow, ...], ...]:
    groups: dict[str, list[VideoExecutionWindow]] = {}
    for window in plan.windows:
        groups.setdefault(window.context_span.span_digest, []).append(window)
    return tuple(
        sorted(
            (tuple(group) for group in groups.values()),
            key=lambda group: group[0].ordinal,
        )
    )


def decide_video_resume(
    plan: VideoExecutionPlan | Mapping[str, Any],
    checkpoints: Sequence[VideoCheckpointManifest | Mapping[str, Any]],
    *,
    target_ordinal: int | None = None,
    active_checkpoint_digests: Mapping[str, str] | None = None,
    next_span_revisions: Mapping[str, int] | None = None,
) -> VideoResumeDecision:
    """Reuse only a selected closed span, else replay that exact span from seed."""
    typed_plan = decode_video_execution_plan(plan)
    typed_checkpoints = tuple(
        decode_video_checkpoint_manifest(item) for item in checkpoints
    )
    active_checkpoint_digests = dict(active_checkpoint_digests or {})
    next_span_revisions = dict(next_span_revisions or {})
    spans = _context_spans(typed_plan)
    if target_ordinal is not None and (
        type(target_ordinal) is not int
        or target_ordinal < 0
        or target_ordinal >= len(typed_plan.windows)
    ):
        raise VideoExecutionContractFailure("resume_ordinal_out_of_range")
    grouped: dict[str, list[VideoCheckpointManifest]] = {}
    for checkpoint in typed_checkpoints:
        if not checkpoint_identity_matches_plan(typed_plan, checkpoint):
            raise VideoExecutionContractFailure("checkpoint_identity_mismatch")
        grouped.setdefault(checkpoint.context_span_digest, []).append(checkpoint)
    if grouped and not typed_plan.compatibility_complete:
        raise VideoExecutionContractFailure(
            "backend_compatibility_identity_unavailable"
        )
    selected: dict[str, VideoCheckpointManifest] = {}
    for span_digest, values in grouped.items():
        by_digest = {item.manifest_digest: item for item in values}
        chosen_digest = active_checkpoint_digests.get(span_digest)
        if chosen_digest is None:
            if len(by_digest) != 1:
                raise VideoExecutionContractFailure(
                    "checkpoint_revision_selection_required"
                )
            chosen_digest = next(iter(by_digest))
        checkpoint = by_digest.get(chosen_digest)
        if checkpoint is None:
            raise VideoExecutionContractFailure("checkpoint_selection_not_found")
        selected[span_digest] = checkpoint
    if set(active_checkpoint_digests) - set(grouped):
        raise VideoExecutionContractFailure("checkpoint_selection_not_found")
    selected_by_digest = {
        checkpoint.manifest_digest: checkpoint for checkpoint in selected.values()
    }
    for span_digest, checkpoint in selected.items():
        span_windows = _span(typed_plan, span_digest)
        seed_source = span_windows[0].seed_source
        if seed_source in {"discovery", "continuation"}:
            predecessor = selected_by_digest.get(
                checkpoint.predecessor_checkpoint_digest or ""
            )
            if (
                predecessor is None
                or predecessor.span_end_ordinal >= span_windows[0].ordinal
            ):
                raise VideoExecutionContractFailure("checkpoint_predecessor_missing")
            predecessor_windows = _span(typed_plan, predecessor.context_span_digest)
            if seed_source == "discovery":
                if (
                    predecessor_windows[0].phase != "discovery"
                    or predecessor.discovery_seeds_digest is None
                ):
                    raise VideoExecutionContractFailure(
                        "checkpoint_discovery_predecessor_mismatch"
                    )
            elif (
                predecessor_windows[0].phase != "track"
                or predecessor_windows[0].branch != span_windows[0].branch
                or predecessor.continuation_digest is None
            ):
                raise VideoExecutionContractFailure(
                    "checkpoint_branch_predecessor_mismatch"
                )
    target_span = next(
        (
            span
            for span in spans
            if target_ordinal is None
            or span[0].ordinal <= target_ordinal <= span[-1].ordinal
        ),
        None,
    )
    if target_ordinal is not None:
        assert target_span is not None
        for span in spans:
            if span[0].ordinal > target_span[0].ordinal:
                break
            if span[0].context_span.span_digest not in selected:
                return _missing_span_decision(span, next_span_revisions)
        existing = selected.get(target_span[0].context_span.span_digest)
        if existing is not None:
            return VideoResumeDecision(
                action="reuse_span",
                context_span_digest=existing.context_span_digest,
                start_ordinal=existing.span_start_ordinal,
                end_ordinal=existing.span_end_ordinal,
                span_revision=existing.span_revision,
                checkpoint_id=existing.checkpoint_id,
            )
        return _missing_span_decision(target_span, next_span_revisions)
    for span in spans:
        digest = span[0].context_span.span_digest
        if digest not in selected:
            return _missing_span_decision(span, next_span_revisions)
    return VideoResumeDecision(
        action="complete",
        context_span_digest=None,
        start_ordinal=None,
        end_ordinal=None,
        span_revision=None,
        checkpoint_id=None,
    )


def _missing_span_decision(
    span: tuple[VideoExecutionWindow, ...],
    next_span_revisions: Mapping[str, int],
) -> VideoResumeDecision:
    first = span[0]
    span_digest = first.context_span.span_digest
    revision = next_span_revisions.get(span_digest, 1)
    if type(revision) is not int or revision < 1:
        raise VideoExecutionContractFailure("invalid_span_revision")
    mode = "replay_span" if first.context_span.mode == "session" else "execute_span"
    return VideoResumeDecision(
        action=mode,
        context_span_digest=span_digest,
        start_ordinal=first.ordinal,
        end_ordinal=span[-1].ordinal,
        span_revision=revision,
        checkpoint_id=None,
    )


def require_checkpoint_replay_match(
    existing: VideoCheckpointManifest | Mapping[str, Any],
    proposed: VideoCheckpointManifest | Mapping[str, Any],
) -> None:
    """Treat same-revision divergence as conflict; caller must append a revision."""
    left = decode_video_checkpoint_manifest(existing)
    right = decode_video_checkpoint_manifest(proposed)
    key_left = (
        left.plan_digest,
        left.context_span_digest,
        left.span_revision,
        left.window_digests,
    )
    key_right = (
        right.plan_digest,
        right.context_span_digest,
        right.span_revision,
        right.window_digests,
    )
    if key_left != key_right:
        raise VideoExecutionContractFailure("checkpoint_identity_conflict")
    if (
        left.manifest_digest != right.manifest_digest
        or left.raw_response_digests != right.raw_response_digests
        or left.result_digest != right.result_digest
        or left.payload_digest != right.payload_digest
    ):
        raise VideoExecutionContractFailure(
            "immutable_checkpoint_conflict",
            next_span_revision=max(left.span_revision, right.span_revision) + 1,
        )


def video_execution_schema() -> dict[str, Any]:
    """Return the versioned JSON Schema snapshot for public portable values."""
    models: tuple[type[BaseModel], ...] = (
        VideoExecutionPlan,
        VideoExecutionWindow,
        VideoExecutionWindowSpec,
        VideoExecutionContextSpan,
        VideoContextSpanSpec,
        VideoSourceIdentity,
        VideoTimebaseIdentity,
        BackendCompatibilityIdentity,
        VideoOutputIdentity,
        FrozenWindowSettings,
        VideoFrameResultRow,
        VideoContinuationPostState,
        VideoGeometryByInstance,
        VideoDiscoverySeed,
        VideoCheckpointPayload,
        VideoCheckpointManifest,
        VideoCheckpointPart,
        VideoResumeDecision,
    )
    definitions: dict[str, Any] = {}
    for model in models:
        document = model.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(document.pop("$defs", {}))
        definitions[model.__name__] = document
    definitions["BackendCompatibilityIdentity"]["allOf"] = [
        {
            "if": {
                "properties": {"provider_kind": {"const": "builtin"}},
                "required": ["provider_kind"],
            },
            "then": {
                "properties": {
                    "role": {"const": "track"},
                    "model_key": {"const": "mock_bbox"},
                    "model_id": {"const": "mock_bbox"},
                    "registry_id": {"type": "null"},
                    "backend_incarnation": {"type": "null"},
                    "weights_release_id": {"type": "null"},
                    "weights_sha256": {"type": "null"},
                    "tracker_context_mode": {"const": "none"},
                    "builtin_implementation_id": {"const": "aap-mock-bbox/v1"},
                },
                "required": ["builtin_implementation_id"],
            },
            "else": {
                "properties": {
                    "registry_id": {"type": "string", "format": "uuid"},
                    "builtin_implementation_id": {"type": "null"},
                    "builtin_implementation_digest": {"type": "null"},
                }
            },
        }
    ]
    public = (
        "VideoExecutionPlan",
        "VideoCheckpointPayload",
        "VideoCheckpointManifest",
        "VideoCheckpointPart",
        "VideoResumeDecision",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "aap-video-execution-plan/v1",
        "title": "AAP frozen video execution and checkpoint values",
        "oneOf": [{"$ref": f"#/$defs/{name}"} for name in public],
        "x-aap-schema": VIDEO_EXECUTION_PLAN_SCHEMA,
        "x-budgets": {
            "plan_bytes": MAX_VIDEO_PLAN_BYTES,
            "checkpoint_result_bytes": MAX_VIDEO_CHECKPOINT_BYTES,
            "checkpoint_payload_bytes": MAX_VIDEO_CHECKPOINT_BYTES,
            "checkpoint_part_bytes": MAX_VIDEO_CHECKPOINT_PART_BYTES,
            "checkpoint_part_base64_chars": MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS,
            "checkpoint_parts": MAX_VIDEO_CHECKPOINT_PARTS,
            "video_windows": MAX_VIDEO_WINDOWS,
        },
        "$defs": definitions,
    }


__all__ = [
    "BackendCompatibilityIdentity",
    "FrozenWindowSettings",
    "VideoCheckpointManifest",
    "VideoCheckpointPart",
    "VideoCheckpointPayload",
    "VideoCheckpointSnapshot",
    "VideoContinuationPostState",
    "VideoContextSpanSpec",
    "VideoDiscoverySeed",
    "VideoExecutionContractFailure",
    "VideoExecutionContextSpan",
    "VideoExecutionPlan",
    "VideoExecutionWindow",
    "VideoExecutionWindowSpec",
    "VideoFrameResultRow",
    "VideoGeometryByInstance",
    "VideoOutputIdentity",
    "VideoResumeDecision",
    "VideoSourceIdentity",
    "VideoTimebaseIdentity",
    "VIDEO_CHECKPOINT_CONTENT_SCHEMA",
    "VIDEO_CHECKPOINT_PART_SCHEMA",
    "VIDEO_CHECKPOINT_SCHEMA",
    "VIDEO_EXECUTION_PLAN_SCHEMA",
    "assemble_video_checkpoint_parts",
    "MAX_VIDEO_CHECKPOINT_BYTES",
    "MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS",
    "MAX_VIDEO_CHECKPOINT_PART_BYTES",
    "MAX_VIDEO_CHECKPOINT_PARTS",
    "MAX_VIDEO_PLAN_BYTES",
    "MAX_VIDEO_RESULT_ROWS",
    "MAX_VIDEO_WINDOWS",
    "canonical_video_json",
    "checkpoint_identity_matches_plan",
    "combo_discovery_seeds",
    "continuation_state_digest",
    "decide_video_resume",
    "decode_video_checkpoint_manifest",
    "decode_video_checkpoint_part",
    "decode_video_execution_plan",
    "discovery_seeds_digest",
    "make_video_context_span",
    "make_video_checkpoint_payload",
    "make_video_execution_plan",
    "normalize_video_frame_results",
    "require_checkpoint_replay_match",
    "snapshot_video_checkpoint",
    "validate_video_checkpoint_payload",
    "video_execution_schema",
]
