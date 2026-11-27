from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.models.dataset import VideoSegment
from app.services.exporting.video_scope import VideoExportScope
from app.services.video_canonical import (
    _required_boundaries,
    accepted_boundary_runs,
    merge_canonical_annotations,
)


def _segment(index: int) -> VideoSegment:
    return VideoSegment(
        id=uuid4(),
        dataset_item_id=uuid4(),
        segment_index=index,
        start_frame=index * 100,
        end_frame=index * 100 + 99,
        status="completed",
    )


def _annotation(segment: VideoSegment, track_id: str, frames: list[int]):
    annotation_id = uuid4()
    return SimpleNamespace(
        id=annotation_id,
        video_segment_id=segment.id,
        track_id=track_id,
        geometry={
            "type": "video_track_bbox",
            "track_id": track_id,
            "keyframes": [
                {"frame_index": frame, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
                for frame in frames
            ],
            "outside": [],
        },
    )


def test_required_boundaries_only_gate_crossing_scope():
    segments = [_segment(index) for index in range(3)]
    task_id = uuid4()
    single_core = VideoExportScope(
        task_id=task_id,
        dataset_item_id=segments[0].dataset_item_id,
        selection_kind="frames",
        from_frame=10,
        to_frame=90,
    )
    crossing = VideoExportScope(
        task_id=task_id,
        dataset_item_id=segments[0].dataset_item_id,
        selection_kind="frames",
        from_frame=90,
        to_frame=110,
    )

    assert _required_boundaries(segments, single_core) == []
    assert _required_boundaries(segments, crossing) == [(segments[0], segments[1])]
    assert len(_required_boundaries(segments, None)) == 2


def test_canonical_merge_uses_stable_track_id_and_sorted_keyframes():
    left_segment, right_segment = _segment(0), _segment(1)
    left = _annotation(left_segment, "trk_b", [0, 99])
    right = _annotation(right_segment, "trk_a", [100, 150])
    run = SimpleNamespace(
        pairs=[
            {
                "left_annotation_id": str(left.id),
                "right_annotation_id": str(right.id),
                "decision": "same_track",
            }
        ]
    )

    [merged] = merge_canonical_annotations([right, left], [run])

    assert merged.track_id == "trk_a"
    assert merged.geometry["track_id"] == "trk_a"
    assert [row["frame_index"] for row in merged.geometry["keyframes"]] == [
        0,
        99,
        100,
        150,
    ]


@pytest.mark.asyncio
async def test_canonical_uses_older_accepted_run_when_newer_run_is_stale(monkeypatch):
    left, right = _segment(0), _segment(1)
    task = SimpleNamespace(id=uuid4())
    newest = SimpleNamespace(
        id=uuid4(),
        task_id=task.id,
        left_segment_id=left.id,
        right_segment_id=right.id,
        status="completed",
    )
    accepted = SimpleNamespace(
        id=uuid4(),
        task_id=task.id,
        left_segment_id=left.id,
        right_segment_id=right.id,
        status="accepted",
    )

    async def list_runs(_db, operation, *, task_id):
        assert operation == "list_video_runs" and task_id == task.id
        return {"rows": [newest, accepted]}

    async def refresh(_db, run):
        if run is newest:
            run.status = "stale"
            return True
        return False

    monkeypatch.setattr("app.services.video_canonical.spatial_quality_data", list_runs)
    monkeypatch.setattr(
        "app.services.video_canonical.quality_rows", lambda _model, rows: list(rows)
    )
    monkeypatch.setattr("app.services.video_canonical.refresh_staleness", refresh)

    assert await accepted_boundary_runs(
        SimpleNamespace(), task=task, segments=[left, right], scope=None
    ) == [accepted]
