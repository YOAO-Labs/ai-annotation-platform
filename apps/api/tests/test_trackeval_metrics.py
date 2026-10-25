from types import SimpleNamespace
import uuid

import numpy as np
import pytest

from app.services.video_track_quality import (
    _issue_frame_counts,
    metrics_with_current_chapters,
)
from app.vendor.trackeval_metrics import evaluate_sequence


def _data(gt, tracker, similarities, *, gt_ids, tracker_ids):
    return {
        "gt_ids": [np.asarray(row, dtype=int) for row in gt],
        "tracker_ids": [np.asarray(row, dtype=int) for row in tracker],
        "similarity_scores": [np.asarray(row, dtype=float) for row in similarities],
        "num_gt_ids": gt_ids,
        "num_tracker_ids": tracker_ids,
    }


def test_trackeval_subset_perfect_and_identity_switch():
    perfect = evaluate_sequence(
        _data(
            [[0], [0]],
            [[0], [0]],
            [[[1.0]], [[1.0]]],
            gt_ids=1,
            tracker_ids=1,
        )
    )
    assert perfect["HOTA"] == 1
    assert perfect["IDF1"] == 1
    assert perfect["MOTA_left"] == 1

    switched = evaluate_sequence(
        _data(
            [[0], [0]],
            [[0], [1]],
            [[[1.0]], [[1.0]]],
            gt_ids=1,
            tracker_ids=2,
        )
    )
    assert switched["IDSW"] == 1
    assert switched["Frag"] == 0
    assert switched["MOTA_left"] == 0.5


def test_trackeval_subset_fp_fn_and_fragmentation():
    result = evaluate_sequence(
        _data(
            [[0], [0], [0]],
            [[0], [1], [0]],
            [[[1.0]], [[0.0]], [[1.0]]],
            gt_ids=1,
            tracker_ids=2,
        )
    )
    assert result["FP"] == 1
    assert result["FN"] == 1
    assert result["Frag"] == 1


def test_quality_issue_aggregate_clips_to_requested_range():
    issues = [
        {
            "code": "false_negative",
            "left_annotation_id": "left",
            "right_annotation_id": None,
            "frame_start": 4,
            "frame_end": 8,
        },
        {
            "code": "false_positive",
            "left_annotation_id": None,
            "right_annotation_id": "right",
            "frame_start": 7,
            "frame_end": 9,
        },
    ]

    assert _issue_frame_counts(issues, frame_range=(6, 7)) == {
        "false_negative": 2,
        "false_positive": 1,
    }
    assert _issue_frame_counts(issues, annotation_id="left") == {"false_negative": 5}


@pytest.mark.asyncio
async def test_chapter_metrics_use_labeling_projection_and_preserve_counts(monkeypatch):
    from app.services import video_track_quality

    task_id = uuid.UUID("f8e07f08-71d5-454c-a512-45b64f6830bf")
    dataset_item_id = uuid.UUID("4ea13c3b-691a-4cbf-9e56-1021b6b25cc2")
    first_id = "4815b54a-3626-423c-97cf-4ad9a605dfd2"
    second_id = "6a42d4b7-1f04-4560-a0ee-c3e9a476c084"
    calls = []

    async def read_chapters(_db, action, **values):
        calls.append((action, values))
        return {
            "chapters": [
                {
                    "id": first_id,
                    "title": "opening",
                    "start_frame": 1,
                    "end_frame": 5,
                },
                {
                    "id": second_id,
                    "title": "ending",
                    "start_frame": 6,
                    "end_frame": 10,
                },
            ]
        }

    monkeypatch.setattr(video_track_quality, "spatial_quality_data", read_chapters)
    metrics = await metrics_with_current_chapters(
        object(),
        task=SimpleNamespace(id=task_id, dataset_item_id=dataset_item_id),
        run=SimpleNamespace(metrics={"aggregates": {"kept": True}}),
        issues=[
            SimpleNamespace(
                code="false_negative",
                left_annotation_id=None,
                right_annotation_id=None,
                frame_start=4,
                frame_end=7,
            )
        ],
    )

    assert calls == [
        (
            "read_video_chapters",
            {"task_id": task_id, "dataset_item_id": dataset_item_id},
        )
    ]
    assert metrics == {
        "aggregates": {
            "kept": True,
            "chapters": [
                {
                    "chapter_id": first_id,
                    "title": "opening",
                    "start_frame": 1,
                    "end_frame": 5,
                    "issue_frames": 2,
                    "issues": {"false_negative": 2},
                },
                {
                    "chapter_id": second_id,
                    "title": "ending",
                    "start_frame": 6,
                    "end_frame": 10,
                    "issue_frames": 2,
                    "issues": {"false_negative": 2},
                },
            ],
        }
    }
