"""v0.10.29 · 视频帧逻辑采样: VideoSamplingConfig 校验 + 网格 helper 单测。

契约地基 (Wave 1-A)。算法规格见 docs/plans/2026-05-21-v0.10.29-video-frame-sampling.md §1。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_workspace.configuration_requests import ProjectCreate, ProjectUpdate
from aap_workspace.configuration_values import (
    VideoCollaborationConfig,
    VideoSamplingConfig,
)
from app.services.video_collaboration import geometry_frame_bounds, segment_work_bounds
from app.services.video_context import derive_sampled_frames, derive_step


# ── VideoSamplingConfig 校验 ─────────────────────────────────────────


def test_sampling_config_default_is_none():
    cfg = VideoSamplingConfig()
    assert cfg.mode == "none"
    assert cfg.target_fps is None
    assert cfg.frame_step is None


def test_sampling_config_empty_dict_validates_as_none():
    cfg = VideoSamplingConfig.model_validate({})
    assert cfg.mode == "none"


def test_sampling_config_fps_valid():
    cfg = VideoSamplingConfig(mode="fps", target_fps=10)
    assert cfg.mode == "fps"
    assert cfg.target_fps == 10


def test_sampling_config_step_valid():
    cfg = VideoSamplingConfig(mode="step", frame_step=5)
    assert cfg.mode == "step"
    assert cfg.frame_step == 5


def test_sampling_config_fps_requires_target_fps():
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="fps")


def test_sampling_config_step_requires_frame_step():
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="step")


def test_sampling_config_none_rejects_extra_fields():
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="none", target_fps=10)
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="none", frame_step=5)


def test_sampling_config_rejects_non_positive_target_fps():
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="fps", target_fps=0)


def test_sampling_config_rejects_zero_frame_step():
    with pytest.raises(ValidationError):
        VideoSamplingConfig(mode="step", frame_step=0)


def test_video_collaboration_requires_overlap_when_enabled():
    with pytest.raises(ValidationError):
        VideoCollaborationConfig(enabled=True, overlap_frames=0)


def test_video_collaboration_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        VideoCollaborationConfig.model_validate(
            {"enabled": False, "overlap_frames": 0, "future": True}
        )


def test_project_schemas_reject_null_video_collaboration():
    with pytest.raises(ValidationError):
        ProjectCreate(
            name="video",
            type_label="video",
            video_collaboration=None,
        )
    with pytest.raises(ValidationError):
        ProjectUpdate(video_collaboration=None)


@pytest.mark.parametrize(
    ("segment_index", "expected"),
    [
        (0, (0, 104)),
        (1, (95, 204)),
        (2, (195, 299)),
    ],
)
def test_segment_work_bounds_share_exact_even_overlap(segment_index, expected):
    assert (
        segment_work_bounds(
            start_frame=segment_index * 100,
            end_frame=segment_index * 100 + 99,
            segment_index=segment_index,
            segment_count=3,
            frame_count=300,
            overlap_frames=10,
        )
        == expected
    )


def test_segment_work_bounds_split_odd_overlap_without_losing_a_frame():
    left = segment_work_bounds(
        start_frame=0,
        end_frame=99,
        segment_index=0,
        segment_count=2,
        frame_count=200,
        overlap_frames=9,
    )
    right = segment_work_bounds(
        start_frame=100,
        end_frame=199,
        segment_index=1,
        segment_count=2,
        frame_count=200,
        overlap_frames=9,
    )
    assert left == (0, 104)
    assert right == (96, 199)
    assert left[1] - right[0] + 1 == 9


def test_geometry_frame_bounds_includes_keyframes_and_outside_ranges():
    assert geometry_frame_bounds(
        {
            "type": "video_track_bbox",
            "keyframes": [{"frame_index": 10}, {"frame_index": 20}],
            "outside": [{"from": 5, "to": 25}],
        }
    ) == (5, 25)
    assert geometry_frame_bounds(
        {"type": "video_keypoint", "frame_index": 7, "x": 0.5, "y": 0.5}
    ) == (7, 7)


# ── derive_step ──────────────────────────────────────────────────────


def test_derive_step_empty_config_is_one():
    assert derive_step(60, {}) == 1


def test_derive_step_none_mode_is_one():
    assert derive_step(60, {"mode": "none"}) == 1


def test_derive_step_explicit_step():
    assert derive_step(60, {"mode": "step", "frame_step": 5}) == 5


def test_derive_step_fps_integer_ratio():
    assert derive_step(60, {"mode": "fps", "target_fps": 5}) == 12
    assert derive_step(30, {"mode": "fps", "target_fps": 10}) == 3


def test_derive_step_fps_non_integer_ratio_rounds():
    # 60 / 25 = 2.4 → round → 2 (≈30fps, 计划 §7 已知近似)
    assert derive_step(60, {"mode": "fps", "target_fps": 25}) == 2
    # 30 / 12 = 2.5 → round → 2 (banker's rounding)
    assert derive_step(30, {"mode": "fps", "target_fps": 12}) == 2


def test_derive_step_fps_target_exceeds_source_floors_to_one():
    # target_fps > source_fps → ratio < 1 → round → 0 → clamp to 1
    assert derive_step(10, {"mode": "fps", "target_fps": 30}) == 1


def test_derive_step_fps_missing_source_fps_is_one():
    assert derive_step(None, {"mode": "fps", "target_fps": 10}) == 1
    assert derive_step(0, {"mode": "fps", "target_fps": 10}) == 1


def test_derive_step_fps_missing_target_is_one():
    assert derive_step(60, {"mode": "fps"}) == 1


def test_derive_step_step_missing_frame_step_is_one():
    assert derive_step(60, {"mode": "step"}) == 1


def test_derive_step_unknown_mode_is_one():
    assert derive_step(60, {"mode": "bogus"}) == 1


# ── derive_sampled_frames ────────────────────────────────────────────


def test_derive_sampled_frames_step_one_is_all_frames():
    assert derive_sampled_frames(5, 1) == [0, 1, 2, 3, 4]


def test_derive_sampled_frames_step_five():
    assert derive_sampled_frames(12, 5) == [0, 5, 10]


def test_derive_sampled_frames_exact_multiple_boundary():
    # frame_count=10, step=5 → 0,5 (帧号 < frame_count, 不含 10)
    assert derive_sampled_frames(10, 5) == [0, 5]


def test_derive_sampled_frames_zero_frame_count():
    assert derive_sampled_frames(0, 5) == []


def test_derive_sampled_frames_step_clamped_to_one():
    assert derive_sampled_frames(3, 0) == [0, 1, 2]


def test_derive_sampled_frames_single_frame():
    assert derive_sampled_frames(1, 5) == [0]
