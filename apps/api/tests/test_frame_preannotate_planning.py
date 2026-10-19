"""Retained video frame segmentation is driven by frozen manifest ordinals."""

import pytest

from app.application.preannotation_contract import VideoFrameTaskPlan
from app.workers.frame_preannotate import plan_video_task_segments


def _plan(task_id: str, count: int) -> VideoFrameTaskPlan:
    return VideoFrameTaskPlan(
        task_id=task_id,
        input_id=f"input-{task_id}",
        input_job_id=f"origin-{task_id}",
        plan_digest=f"digest-{task_id}",
        selected_count=count,
    )


def test_chunks_canonical_ordinals_without_consulting_frame_count():
    segments, total = plan_video_task_segments(
        (_plan("task-a", 5), _plan("task-b", 2)), chunk_size=2
    )

    assert total == 7
    assert segments == [
        {
            "task_id": "task-a",
            "input_id": "input-task-a",
            "input_job_id": "origin-task-a",
            "plan_digest": "digest-task-a",
            "frame_ordinals": [1, 2],
        },
        {
            "task_id": "task-a",
            "input_id": "input-task-a",
            "input_job_id": "origin-task-a",
            "plan_digest": "digest-task-a",
            "frame_ordinals": [3, 4],
        },
        {
            "task_id": "task-a",
            "input_id": "input-task-a",
            "input_job_id": "origin-task-a",
            "plan_digest": "digest-task-a",
            "frame_ordinals": [5],
        },
        {
            "task_id": "task-b",
            "input_id": "input-task-b",
            "input_job_id": "origin-task-b",
            "plan_digest": "digest-task-b",
            "frame_ordinals": [1, 2],
        },
    ]


def test_segment_count_is_bounded_by_timebase_page_size():
    from aap_assets_video_inputs.values import PAGE_SIZE

    plans, total = plan_video_task_segments((_plan("task-a", PAGE_SIZE + 1),), 500)

    assert total == PAGE_SIZE + 1
    assert [len(segment["frame_ordinals"]) for segment in plans] == [PAGE_SIZE, 1]


def test_duplicate_task_and_empty_plan_fail_closed():
    with pytest.raises(ValueError, match="repeated a Task"):
        plan_video_task_segments((_plan("task-a", 1), _plan("task-a", 1)), 1)

    with pytest.raises(ValueError, match="no selected frames"):
        plan_video_task_segments((_plan("task-a", 0),), 1)
