import uuid

from aap_labeling.video_bbox_editing_policy import (
    VideoBBoxAnnotationFacts,
    VideoBBoxTaskFacts,
    compose_video_tracks_command,
    convert_video_track_to_bboxes_command,
)


def _track(task, *, start, end, track_id, outside=None, label=None):
    return VideoBBoxAnnotationFacts(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=task.project_id,
        version=1,
        is_active=True,
        was_cancelled=False,
        annotation_type="video_track_bbox",
        class_name="person",
        geometry={
            "type": "video_track_bbox",
            "track_id": track_id,
            "semantic_label": label,
            "keyframes": [
                {
                    "frame_index": start,
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    "source": "manual",
                    "occluded": False,
                },
                {
                    "frame_index": end,
                    "bbox": {"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.5},
                    "source": "manual",
                    "occluded": False,
                },
            ],
            "outside": outside or [],
        },
        video_segment_id=None,
        source="manual",
        confidence=None,
        attributes={"color": "red"},
        track_id=track_id,
    )


def test_conversion_and_join_keep_visible_frames_and_gap_policy():
    task = VideoBBoxTaskFacts(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        batch_id=None,
        status="in_progress",
        review_round_id=None,
    )
    actor_id = uuid.uuid4()
    source = _track(
        task,
        start=0,
        end=4,
        track_id="trk_source",
        outside=[{"from": 2, "to": 2, "source": "manual"}],
    )

    split = convert_video_track_to_bboxes_command(
        task=task,
        annotation=source,
        actor_id=actor_id,
        operation="split",
        scope="track",
        frame_mode="keyframes",
    )
    assert [frame.frame_index for frame in split.created_frames] == [0, 4]
    assert split.removed_frame_indexes == [0, 4]
    assert [frame.x for frame in split.created_frames] == [0.1, 0.2]

    first = _track(
        task,
        start=5,
        end=7,
        track_id="trk_survivor",
        outside=[{"from": 8, "to": 9, "source": "prediction"}],
        label="truck_alpha",
    )
    second = _track(task, start=0, end=2, track_id="trk_removed")
    joined = compose_video_tracks_command(
        task=task,
        annotations=[first, second],
        actor_id=actor_id,
        operation="join_tracks",
        gap_mode="interpolate",
    )
    assert joined.operation == "join_tracks"
    assert joined.survivor_id == first.id
    assert joined.survivor_geometry["track_id"] == "trk_survivor"
    assert joined.survivor_geometry["semantic_label"] == "truck_alpha"
    assert [
        keyframe["frame_index"] for keyframe in joined.survivor_geometry["keyframes"]
    ] == [0, 2, 5, 7]
    assert joined.survivor_geometry["outside"] == [
        {"from": 8, "to": 9, "source": "prediction"}
    ]

    merged = compose_video_tracks_command(
        task=task,
        annotations=[first, second],
        actor_id=actor_id,
        operation="merge_tracks",
    )
    assert merged.operation == "merge_tracks"
    assert merged.survivor_geometry["outside"] == [
        {"from": 3, "to": 4, "source": "manual"},
        {"from": 8, "to": 9, "source": "prediction"},
    ]
