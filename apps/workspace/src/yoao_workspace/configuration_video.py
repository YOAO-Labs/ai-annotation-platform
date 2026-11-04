"""Video collaboration mutation checks for the portable configuration owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .configuration_policy import ConfigurationPolicyError
from .configuration_values import VideoCollaborationConfig

VIDEO_TOOL_UNITS = frozenset({"bbox", "polyline", "keypoint", "region", "rotated_bbox"})


Step = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]


def derive_video_sampling_step(
    source_fps: float | None, sampling: Mapping[str, Any]
) -> int:
    """Return the source-frame sampling grid used by the existing video workflow."""
    if not sampling:
        return 1
    mode = sampling.get("mode", "none")
    if mode == "step":
        frame_step = sampling.get("frame_step")
        if frame_step is None:
            return 1
        return max(1, int(frame_step))
    if mode == "fps":
        target_fps = sampling.get("target_fps")
        if not source_fps or not target_fps:
            return 1
        return max(1, round(source_fps / target_fps))
    return 1


def _summary_for(summaries: Mapping[Any, Any], item_id: Any) -> tuple[bool, Any]:
    if item_id in summaries:
        return True, summaries[item_id]
    string_id = str(item_id)
    if string_id in summaries:
        return True, summaries[string_id]
    return False, None


async def validate_project_collaboration_update(
    *,
    requested: VideoCollaborationConfig,
    project: Mapping[str, Any] | None,
    data_type: str,
    step: Step,
    video_segment_size_frames: int,
    video_sampling_changed: bool = False,
    requested_video_sampling: Mapping[str, Any] | None = None,
    requested_tool_bindings: Mapping[str, Any] | None = None,
) -> None:
    """Apply the old collaboration freeze and sampling-overlap checks via owner steps."""
    if requested.enabled and data_type != "video":
        raise ConfigurationPolicyError(422, "video_collaboration 只能用于 video 项目")
    if requested.overlap_frames >= max(1, video_segment_size_frames):
        raise ConfigurationPolicyError(
            422,
            "overlap_frames 必须小于 VIDEO_SEGMENT_SIZE_FRAMES",
        )

    tool_bindings = requested_tool_bindings
    if tool_bindings is None and project is not None:
        tool_bindings = project.get("tool_bindings") or {}
    invalid_units = sorted(set(tool_bindings or {}) - VIDEO_TOOL_UNITS)
    if requested.enabled and invalid_units:
        raise ConfigurationPolicyError(
            422,
            {
                "reason": "video_collaboration_invalid_tools",
                "tool_units": invalid_units,
            },
        )
    if project is None:
        return

    current = VideoCollaborationConfig.model_validate(
        project.get("video_collaboration") or {}
    )
    collaboration_changed = requested != current
    if not collaboration_changed and not (video_sampling_changed and current.enabled):
        return

    annotations = await step("video_annotations", {})
    annotation_count = int(annotations["annotation_count"])
    if annotation_count:
        raise ConfigurationPolicyError(
            409,
            {
                "reason": "video_collaboration_config_frozen",
                "annotation_count": annotation_count,
            },
        )

    trackers = await step("video_trackers", {})
    active_tracker_count = int(trackers["active_tracker_count"])
    if active_tracker_count:
        raise ConfigurationPolicyError(
            409,
            {
                "reason": "video_collaboration_tracker_active",
                "active_tracker_count": active_tracker_count,
            },
        )

    if not requested.enabled:
        return

    sampling = requested_video_sampling
    if sampling is None:
        sampling = project.get("video_sampling") or {}

    item_result = await step("video_items", {})
    item_ids = list(item_result["dataset_item_ids"])
    summaries = {}
    for offset in range(0, len(item_ids), 500):
        media_result = await step(
            "video_media", {"item_ids": item_ids[offset : offset + 500]}
        )
        summaries.update(media_result["summaries"])
    # Missing sources precede pending metadata and sampling failures, even when
    # an earlier item in the input list has one of those conditions.
    if any(not _summary_for(summaries, item_id)[0] for item_id in item_ids):
        raise ConfigurationPolicyError(
            503, {"reason": "video_media_summary_unavailable"}
        )
    for item_id in item_ids:
        _, metadata = _summary_for(summaries, item_id)
        if metadata is None:
            raise ConfigurationPolicyError(
                503,
                {"reason": "video_metadata_not_ready"},
                {"Retry-After": "3"},
            )
        metadata = metadata or {}
        frame_count = max(0, int(metadata.get("frame_count") or 0))
        sampling_step = derive_video_sampling_step(metadata.get("fps"), sampling)
        for boundary in range(
            video_segment_size_frames,
            frame_count,
            video_segment_size_frames,
        ):
            left = requested.overlap_frames // 2
            right = requested.overlap_frames - left
            start = max(0, boundary - left)
            end = min(frame_count - 1, boundary + right - 1)
            first_sample = (
                (start + sampling_step - 1) // sampling_step
            ) * sampling_step
            if first_sample > end:
                raise ConfigurationPolicyError(
                    422,
                    {
                        "reason": "video_collaboration_sampling_empty",
                        "dataset_item_id": str(item_id),
                        "boundary_frame": boundary,
                    },
                )
