from __future__ import annotations

import uuid

import pytest

from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.export import VideoExportScopeRequest
from app.services.exporting.video_scope import (
    VideoExportScope,
    clip_video_geometry,
    normalize_video_export_scope,
)


def _scope(from_frame: int, to_frame: int) -> VideoExportScope:
    return VideoExportScope(
        task_id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        selection_kind="frames",
        from_frame=from_frame,
        to_frame=to_frame,
    )


def test_clip_track_adds_self_contained_boundary_keyframes():
    geometry = {
        "type": "video_track_bbox",
        "track_id": "car-1",
        "keyframes": [
            {
                "frame_index": 0,
                "bbox": {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2},
                "source": "manual",
            },
            {
                "frame_index": 10,
                "bbox": {"x": 1.0, "y": 1.0, "w": 0.2, "h": 0.2},
                "source": "prediction",
            },
        ],
        "outside": [],
    }

    clipped = clip_video_geometry(geometry, _scope(2, 8))

    assert clipped is not None
    assert [row["frame_index"] for row in clipped["keyframes"]] == [2, 8]
    assert [row["source"] for row in clipped["keyframes"]] == [
        "interpolated",
        "interpolated",
    ]
    assert clipped["keyframes"][0]["bbox"]["x"] == 0.2
    assert clipped["keyframes"][1]["bbox"]["x"] == 0.8


def test_clip_track_preserves_original_sources_and_clips_outside_ranges():
    geometry = {
        "type": "video_track_bbox",
        "track_id": "car-1",
        "keyframes": [
            {
                "frame_index": 3,
                "bbox": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2},
                "source": "prediction",
                "occluded": True,
            },
            {
                "frame_index": 7,
                "bbox": {"x": 0.7, "y": 0.7, "w": 0.2, "h": 0.2},
                "source": "manual",
            },
        ],
        "outside": [
            {"from": 0, "to": 4, "source": "manual"},
            {"from": 6, "to": 12, "source": "prediction"},
        ],
    }

    clipped = clip_video_geometry(geometry, _scope(2, 8))

    assert clipped is not None
    assert clipped["keyframes"] == geometry["keyframes"]
    assert clipped["outside"] == [
        {"from": 2, "to": 4, "source": "manual"},
        {"from": 6, "to": 8, "source": "prediction"},
    ]


def test_clip_mask_track_reuses_held_mask_at_range_boundaries():
    first_mask = {
        "encoding": "coco_rle_ref",
        "size": [10, 10],
        "object_key": f"raster-masks/sha256/aa/bb/{'a' * 64}.json",
        "sha256": "a" * 64,
        "runs": 3,
        "bytes": 12,
    }
    geometry = {
        "type": "video_track_mask",
        "track_id": "mask-1",
        "keyframes": [
            {"frame_index": 0, "mask": first_mask, "source": "manual"},
            {
                "frame_index": 10,
                "mask": {**first_mask, "sha256": "b" * 64},
                "source": "prediction",
            },
        ],
        "outside": [],
    }

    clipped = clip_video_geometry(geometry, _scope(2, 4))

    assert clipped is not None
    assert [row["frame_index"] for row in clipped["keyframes"]] == [2, 4]
    assert all(row["mask"] == first_mask for row in clipped["keyframes"])
    assert all(row["source"] == "manual" for row in clipped["keyframes"])


def test_clip_single_frame_geometry_uses_inclusive_range():
    geometry = {
        "type": "video_keypoint",
        "frame_index": 5,
        "points": [{"x": 0.5, "y": 0.5, "v": 2}],
    }

    assert clip_video_geometry(geometry, _scope(5, 6)) == geometry
    assert clip_video_geometry(geometry, _scope(6, 7)) is None


async def test_normalize_segment_scope_resolves_contiguous_effective_range(
    db_session,
    httpx_client,
    monkeypatch,
    super_admin,
):
    user, token = super_admin
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        display_id=f"P-VS-{suffix}",
        name="Scoped video",
        type_key="video-track",
        type_label="Video Track",
        data_type="video",
        owner_id=user.id,
    )
    dataset = Dataset(
        display_id=f"D-VS-{suffix}",
        name="Scoped dataset",
        data_type="video",
        created_by=user.id,
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="scoped/clip.mp4",
        file_type="video",
        metadata_={"video": {"fps": 10, "frame_count": 10}},
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-VS-{suffix}",
        file_name="clip.mp4",
        file_path="scoped/clip.mp4",
        file_type="video",
    )
    segments = [
        VideoSegment(
            dataset_item_id=item.id,
            segment_index=index,
            start_frame=index * 3,
            end_frame=min(9, index * 3 + 2),
        )
        for index in range(4)
    ]
    db_session.add_all([task, *segments])
    await db_session.flush()

    request = VideoExportScopeRequest.model_validate(
        {
            "task_id": str(task.id),
            "selection": {
                "kind": "segments",
                "start_segment_id": str(segments[1].id),
                "end_segment_id": str(segments[3].id),
            },
        }
    )
    scope = await normalize_video_export_scope(
        db_session,
        project=project,
        request=request,
    )

    assert scope is not None
    assert (scope.from_frame, scope.to_frame) == (3, 9)
    assert [segment.segment_index for segment in scope.segments] == [1, 2, 3]

    dispatched: dict = {}
    monkeypatch.setattr(
        "app.workers.export.run_export.delay",
        lambda **kwargs: dispatched.update(kwargs),
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=video_json",
        json={"scope": request.model_dump(mode="json")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    expected_scope = scope.as_dict()
    assert dispatched["opts"]["video_export_scope"] == expected_scope
    job = await db_session.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    assert job is not None
    assert job.payload["video_export_scope"] == expected_scope
    assert job.payload["format"] == "video_json"

    invalid = VideoExportScopeRequest.model_validate(
        {
            "task_id": str(task.id),
            "selection": {"kind": "frames", "from_frame": 8, "to_frame": 10},
        }
    )
    with pytest.raises(ValueError, match="exceeds source frame_count"):
        await normalize_video_export_scope(
            db_session,
            project=project,
            request=invalid,
        )
