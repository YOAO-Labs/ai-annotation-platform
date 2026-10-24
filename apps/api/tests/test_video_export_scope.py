from __future__ import annotations

import uuid
import hashlib
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.storage import storage_service
from app.services import task_originals
from tests.test_task_originals import (
    original_db as original_db,
    original_client as original_client,
    verified,
)
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
    # This is an API metadata-only fixture: a real verified historical locator,
    # never a guessed current key. No original bytes are read while enqueuing.
    await ensure_legacy_bindings(db_session)
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="video",
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=hashlib.sha256(b"video").hexdigest(),
        size_bytes=5,
    )
    db_session.add(asset)
    await db_session.flush()
    db_session.add(
        DatasetAssetLocation(
            asset_id=asset.id,
            location_id=LEGACY_LOCATION_ID,
            bucket=storage_service.datasets_bucket,
            object_key=item.file_path,
            role="managed_original",
            reference_state="present",
            verified_at=datetime.now(timezone.utc),
        )
    )
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
    # Mutable historical Item JSON does not authorize a source frame extent.
    # The Worker checks the exact original's probe before allocating frames.
    deferred = await normalize_video_export_scope(
        db_session,
        project=project,
        request=invalid,
    )
    assert (deferred.from_frame, deferred.to_frame) == (8, 10)


async def test_standalone_video_frame_scope_needs_no_fake_item(
    original_db, original_client
):
    db_session = original_db
    task, allocation, created = await verified(db_session, original_client)
    task.file_type = "video"
    await task_originals.publish(db_session, task, allocation, created)
    await db_session.commit()
    project = SimpleNamespace(id=task.project_id, data_type="video")
    request = VideoExportScopeRequest.model_validate(
        {
            "task_id": str(task.id),
            "selection": {"kind": "frames", "from_frame": 0, "to_frame": 17},
        }
    )
    scope = await normalize_video_export_scope(
        db_session, project=project, request=request
    )
    assert scope.dataset_item_id is None
    assert VideoExportScope.from_dict(scope.as_dict()) == scope
    segment_request = VideoExportScopeRequest.model_validate(
        {
            "task_id": str(task.id),
            "selection": {
                "kind": "segments",
                "start_segment_id": str(uuid.uuid4()),
                "end_segment_id": str(uuid.uuid4()),
            },
        }
    )
    with pytest.raises(ValueError, match="require a dataset item"):
        await normalize_video_export_scope(
            db_session, project=project, request=segment_request
        )
