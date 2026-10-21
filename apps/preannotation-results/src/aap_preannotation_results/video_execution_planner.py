"""Freeze video windows using accepted facts and explicitly supplied limits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import StrictStr, model_validator

from . import contract as c
from .video_execution_plan import (
    MAX_VIDEO_PLAN_BYTES,
    BackendCompatibilityIdentity,
    FrozenWindowSettings,
    VideoExecutionContractFailure,
    VideoExecutionPlan,
    VideoExecutionWindowSpec,
    VideoOutputIdentity,
    VideoSourceIdentity,
    VideoTimebaseIdentity,
    _validate_model,
    make_video_context_span,
    make_video_execution_plan,
)


class VideoExecutionInputFacts(c.Value):
    job_id: c.CanonicalUUID
    input_id: c.CanonicalUUID
    task_id: c.CanonicalUUID
    project_id: c.CanonicalUUID
    source_item_id: c.CanonicalUUID | None
    request_digest: c.Digest
    prompt_digest: c.Digest
    source_plan_digest: c.Digest
    requested_model_key: StrictStr
    job_kind: Literal["tracking", "correction"]
    direction: Literal["forward", "backward", "bidirectional"]
    from_frame: c.NonnegativeInt
    to_frame: c.NonnegativeInt
    grid_step: c.PositiveInt
    correction_frame: c.NonnegativeInt | None
    segment_id: c.CanonicalUUID | None
    source: VideoSourceIdentity
    timebase: VideoTimebaseIdentity

    @model_validator(mode="after")
    def coherent_request(self):
        if (
            self.from_frame > self.to_frame
            or self.to_frame >= self.source.frame_count
            or self.grid_step != self.timebase.grid_step
            or not 1 <= len(self.requested_model_key) <= 80
        ):
            raise ValueError("accepted video request identity is inconsistent")
        if self.job_kind == "tracking":
            if self.correction_frame is not None or self.direction == "bidirectional":
                raise ValueError("ordinary tracking cannot have correction branches")
        elif self.correction_frame is None or not (
            self.from_frame <= self.correction_frame <= self.to_frame
        ):
            raise ValueError("correction requires its accepted human frame")
        return self


def freeze_video_window_settings(
    *,
    requested_model_key: str,
    platform_default_frames: int,
    platform_sam3_frames: int,
    backend_compatibility: Sequence[BackendCompatibilityIdentity | Mapping[str, Any]],
    combo_discovery_window_frames: int | None = None,
    accepted_correction_max_frames: int | None = None,
) -> FrozenWindowSettings:
    """Apply selected model, platform, context and accepted correction bounds."""
    try:
        backends = tuple(
            _validate_model(BackendCompatibilityIdentity, item, max_bytes=4096)
            for item in backend_compatibility
        )
        if [item.role for item in backends] not in (["track"], ["track", "discovery"]):
            raise ValueError("backend roles are not canonical")
        for number in (platform_default_frames, platform_sam3_frames):
            if type(number) is not int or number < 1:
                raise ValueError("invalid platform window bound")
        if accepted_correction_max_frames is not None and (
            type(accepted_correction_max_frames) is not int
            or accepted_correction_max_frames < 1
        ):
            raise ValueError("invalid accepted correction bound")
        combo = requested_model_key == "sam3_video_combo"
        if combo != (len(backends) == 2):
            raise ValueError("combo requires both selected backend roles")
        track = backends[0]
        limits = [platform_default_frames]
        if requested_model_key.startswith("sam3"):
            limits.append(platform_sam3_frames)
        if track.max_window_frames is not None:
            limits.append(track.max_window_frames)
        if track.tracker_context_mode == "session" and not combo:
            limits.append(track.max_context_frames)
        if accepted_correction_max_frames is not None:
            limits.append(accepted_correction_max_frames)
        discovery_max = None
        discovery_frames = None
        if combo:
            if (
                type(combo_discovery_window_frames) is not int
                or combo_discovery_window_frames < 1
            ):
                raise ValueError("combo requires its discovery bound")
            discovery_max = backends[1].max_window_frames
            discovery_limits = [combo_discovery_window_frames, platform_sam3_frames]
            if discovery_max is not None:
                discovery_limits.append(discovery_max)
            discovery_frames = min(discovery_limits)
        elif combo_discovery_window_frames is not None:
            raise ValueError("ordinary tracker cannot carry discovery bounds")
        return FrozenWindowSettings(
            platform_default_frames=platform_default_frames,
            platform_sam3_frames=platform_sam3_frames,
            track_backend_max_frames=track.max_window_frames,
            effective_track_max_frames=min(limits),
            combo_discovery_window_frames=combo_discovery_window_frames,
            discovery_backend_max_frames=discovery_max,
            effective_discovery_max_frames=discovery_frames,
        )
    except VideoExecutionContractFailure:
        raise
    except (TypeError, ValueError, IndexError):
        raise VideoExecutionContractFailure("invalid_window_settings") from None


def _windows(start: int, end: int, direction: str, size: int):
    values = [
        (value, min(end, value + size - 1)) for value in range(start, end + 1, size)
    ]
    return list(reversed(values)) if direction == "backward" else values


def build_video_execution_plan(
    *,
    job_id: c.CanonicalUUID,
    facts: VideoExecutionInputFacts | Mapping[str, Any],
    window_settings: FrozenWindowSettings | Mapping[str, Any],
    backend_compatibility: Sequence[BackendCompatibilityIdentity | Mapping[str, Any]],
    output_geometry: Literal["bbox", "polygon", "mask"],
    initial_seed_source: Literal["none", "prompt", "source_geometry", "correction"],
    initial_seed_digest: str | None,
    native_correction: bool = False,
) -> VideoExecutionPlan:
    """Build the complete immutable plan without reading mutable global settings."""
    try:
        accepted = _validate_model(
            VideoExecutionInputFacts, facts, max_bytes=MAX_VIDEO_PLAN_BYTES
        )
        if str(job_id) != str(accepted.job_id):
            raise ValueError("plan belongs to another accepted Job")
        settings = _validate_model(
            FrozenWindowSettings, window_settings, max_bytes=4096
        )
        backends = tuple(
            _validate_model(BackendCompatibilityIdentity, item, max_bytes=4096)
            for item in backend_compatibility
        )
        combo = accepted.requested_model_key == "sam3_video_combo"
        if type(native_correction) is not bool or (
            native_correction and accepted.job_kind != "correction"
        ):
            raise ValueError("native correction flag changed request kind")
        if accepted.job_kind == "correction" and initial_seed_source != "correction":
            raise ValueError("correction must use its accepted seed")
        if (initial_seed_source in {"prompt", "source_geometry", "correction"}) != (
            initial_seed_digest is not None
        ):
            raise ValueError("fixed initial seed identity is incomplete")
        if combo and (initial_seed_source != "none" or initial_seed_digest is not None):
            raise ValueError("combo obtains its seed from discovery")
        size = settings.effective_track_max_frames
        branches = (
            ("backward", "forward")
            if accepted.direction == "bidirectional"
            else (accepted.direction,)
        )
        specs: list[dict[str, Any]] = []
        if combo:
            discovery_size = settings.effective_discovery_max_frames
            if discovery_size is None:
                raise ValueError("discovery bounds are missing")
            if accepted.direction == "backward":
                discovery_start = max(
                    accepted.from_frame, accepted.to_frame - discovery_size + 1
                )
                discovery_end = accepted.to_frame
            else:
                discovery_start = accepted.from_frame
                discovery_end = min(
                    accepted.to_frame, accepted.from_frame + discovery_size - 1
                )
            specs.append(
                dict(
                    phase="discovery",
                    backend_role="discovery",
                    branch="forward",
                    ordinal=0,
                    from_frame=discovery_start,
                    to_frame=discovery_end,
                    is_seed_window=False,
                    seed_required=False,
                    seed_source="none",
                    seed_digest=None,
                    context_span=make_video_context_span(
                        start_ordinal=0,
                        end_ordinal=0,
                        from_frame=discovery_start,
                        to_frame=discovery_end,
                        mode="none",
                    ),
                )
            )
        for branch in branches:
            start = (
                accepted.correction_frame
                if accepted.job_kind == "correction" and branch == "forward"
                else accepted.from_frame
            )
            end = (
                accepted.correction_frame
                if accepted.job_kind == "correction" and branch == "backward"
                else accepted.to_frame
            )
            assert start is not None and end is not None
            windows = _windows(start, end, branch, size)
            if native_correction and len(windows) != 1:
                raise ValueError("native correction must fit one window per direction")
            first_ordinal = len(specs)
            spans = {}
            session = backends[0].tracker_context_mode == "session" and not combo
            first = 0
            while first < len(windows):
                last = first
                lo, hi = windows[first]
                if session:
                    while last + 1 < len(windows):
                        next_lo = min(lo, windows[last + 1][0])
                        next_hi = max(hi, windows[last + 1][1])
                        if next_hi - next_lo + 1 > backends[0].max_context_frames:
                            break
                        last += 1
                        lo, hi = next_lo, next_hi
                span = make_video_context_span(
                    start_ordinal=first_ordinal + first,
                    end_ordinal=first_ordinal + last,
                    from_frame=lo,
                    to_frame=hi,
                    mode="session" if session else "none",
                )
                for index in range(first, last + 1):
                    spans[index] = span
                first = last + 1
            for index, (lo, hi) in enumerate(windows):
                ordinal = len(specs)
                span = spans[index]
                seed_source = (
                    ("discovery" if combo else initial_seed_source)
                    if index == 0
                    else ("session" if span.start_ordinal < ordinal else "continuation")
                )
                specs.append(
                    dict(
                        phase="track",
                        backend_role="track",
                        branch=branch,
                        ordinal=ordinal,
                        from_frame=lo,
                        to_frame=hi,
                        is_seed_window=index == 0,
                        seed_required=seed_source not in {"none", "session"},
                        seed_source=seed_source,
                        seed_digest=initial_seed_digest
                        if seed_source in {"prompt", "source_geometry", "correction"}
                        else None,
                        context_span=span,
                    )
                )
        return make_video_execution_plan(
            request_id=job_id,
            input_id=accepted.input_id,
            task_id=accepted.task_id,
            project_id=accepted.project_id,
            request_digest=accepted.request_digest,
            prompt_digest=accepted.prompt_digest,
            source_plan_digest=accepted.source_plan_digest,
            source=accepted.source,
            timebase=accepted.timebase,
            requested_model_key=accepted.requested_model_key,
            job_kind=accepted.job_kind,
            direction=accepted.direction,
            from_frame=accepted.from_frame,
            to_frame=accepted.to_frame,
            correction_frame=accepted.correction_frame,
            segment_id=accepted.segment_id,
            window_settings=settings,
            backend_compatibility=backends,
            output=VideoOutputIdentity(
                geometry=output_geometry, grid_step=accepted.grid_step
            ),
            windows=[VideoExecutionWindowSpec.model_validate(spec) for spec in specs],
        )
    except VideoExecutionContractFailure:
        raise
    except (ValueError, TypeError, IndexError):
        raise VideoExecutionContractFailure("invalid_video_execution_plan") from None
