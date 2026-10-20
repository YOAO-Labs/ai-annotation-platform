from __future__ import annotations

from uuid import UUID

import pytest

from aap_workspace.configuration_policy import ConfigurationPolicyError
from aap_workspace.configuration_values import VideoCollaborationConfig
from aap_workspace.configuration_video import (
    derive_video_sampling_step,
    validate_project_collaboration_update,
)

_ITEM = UUID("b15b2138-06e9-4141-a1d9-e9c257fc8af6")


def _project(**overrides):
    return {
        "id": "d98da3ab-a9c0-4722-8068-3767530aaac6",
        "video_collaboration": {"enabled": False, "overlap_frames": 0},
        "tool_bindings": {"bbox": {"classes": []}},
        "video_sampling": {"mode": "none"},
        **overrides,
    }


class Steps:
    def __init__(self, **facts):
        self.facts = facts
        self.calls = []

    async def __call__(self, action, args):
        self.calls.append((action, args))
        return self.facts[action]


@pytest.mark.asyncio
async def test_unchanged_disabled_configuration_does_not_query_owner_facts():
    steps = Steps()
    await validate_project_collaboration_update(
        requested=VideoCollaborationConfig(),
        project=_project(),
        data_type="video",
        step=steps,
        video_segment_size_frames=255,
    )
    assert steps.calls == []


@pytest.mark.asyncio
async def test_annotation_freeze_short_circuits_tracker_and_media_reads():
    steps = Steps(video_annotations={"annotation_count": 3})
    with pytest.raises(ConfigurationPolicyError) as exc:
        await validate_project_collaboration_update(
            requested=VideoCollaborationConfig(enabled=True, overlap_frames=1),
            project=_project(),
            data_type="video",
            step=steps,
            video_segment_size_frames=255,
            requested_video_sampling={"mode": "none"},
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "reason": "video_collaboration_config_frozen",
        "annotation_count": 3,
    }
    assert steps.calls == [("video_annotations", {})]


@pytest.mark.asyncio
async def test_sampling_overlap_uses_each_frame_boundary_and_absolute_grid():
    steps = Steps(
        video_annotations={"annotation_count": 0},
        video_trackers={"active_tracker_count": 0},
        video_items={"dataset_item_ids": [str(_ITEM)]},
        video_media={"summaries": {str(_ITEM): {"frame_count": 300, "fps": 30.0}}},
    )
    with pytest.raises(ConfigurationPolicyError) as exc:
        await validate_project_collaboration_update(
            requested=VideoCollaborationConfig(enabled=True, overlap_frames=1),
            project=_project(),
            data_type="video",
            step=steps,
            video_segment_size_frames=255,
            requested_video_sampling={"mode": "step", "frame_step": 2},
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "reason": "video_collaboration_sampling_empty",
        "dataset_item_id": str(_ITEM),
        "boundary_frame": 255,
    }
    assert [action for action, _ in steps.calls] == [
        "video_annotations",
        "video_trackers",
        "video_items",
        "video_media",
    ]


@pytest.mark.asyncio
async def test_missing_and_pending_video_summaries_keep_distinct_responses():
    for summaries, expected_detail, expected_headers in (
        ({}, {"reason": "video_media_summary_unavailable"}, None),
        (
            {str(_ITEM): None},
            {"reason": "video_metadata_not_ready"},
            {"Retry-After": "3"},
        ),
    ):
        steps = Steps(
            video_annotations={"annotation_count": 0},
            video_trackers={"active_tracker_count": 0},
            video_items={"dataset_item_ids": [str(_ITEM)]},
            video_media={"summaries": summaries},
        )
        with pytest.raises(ConfigurationPolicyError) as exc:
            await validate_project_collaboration_update(
                requested=VideoCollaborationConfig(enabled=True, overlap_frames=1),
                project=_project(),
                data_type="video",
                step=steps,
                video_segment_size_frames=255,
                requested_video_sampling={"mode": "none"},
            )
        assert exc.value.status_code == 503
        assert exc.value.detail == expected_detail
        assert exc.value.headers == expected_headers


def test_sampling_step_preserves_rounding_and_invalid_configuration_fallbacks():
    assert derive_video_sampling_step(30.0, {"mode": "step", "frame_step": 0}) == 1
    assert derive_video_sampling_step(10.0, {"mode": "fps", "target_fps": 6.0}) == 2
    assert derive_video_sampling_step(None, {"mode": "fps", "target_fps": 6.0}) == 1
    assert derive_video_sampling_step(30.0, {"mode": "none"}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("item_count", [0, 501])
async def test_media_summary_reads_keep_legacy_batch_size_and_empty_shortcut(
    item_count,
):
    item_ids = [str(UUID(int=index + 1)) for index in range(item_count)]
    batches = []

    async def step(action, args):
        if action == "video_annotations":
            return {"annotation_count": 0}
        if action == "video_trackers":
            return {"active_tracker_count": 0}
        if action == "video_items":
            return {"dataset_item_ids": item_ids}
        assert action == "video_media"
        batches.append(args["item_ids"])
        assert len(args["item_ids"]) <= 500
        return {
            "summaries": {
                item: {"frame_count": 10, "fps": 30} for item in args["item_ids"]
            }
        }

    await validate_project_collaboration_update(
        requested=VideoCollaborationConfig(enabled=True, overlap_frames=1),
        project=_project(),
        data_type="video",
        step=step,
        video_segment_size_frames=255,
    )
    assert [len(batch) for batch in batches] == ([] if item_count == 0 else [500, 1])


@pytest.mark.asyncio
async def test_missing_summary_precedes_earlier_pending_metadata():
    missing = str(UUID(int=1))
    steps = Steps(
        video_annotations={"annotation_count": 0},
        video_trackers={"active_tracker_count": 0},
        video_items={"dataset_item_ids": [str(_ITEM), missing]},
        video_media={"summaries": {str(_ITEM): None}},
    )
    with pytest.raises(ConfigurationPolicyError) as caught:
        await validate_project_collaboration_update(
            requested=VideoCollaborationConfig(enabled=True, overlap_frames=1),
            project=_project(),
            data_type="video",
            step=steps,
            video_segment_size_frames=255,
        )
    assert caught.value.detail == {"reason": "video_media_summary_unavailable"}
    assert caught.value.headers is None
