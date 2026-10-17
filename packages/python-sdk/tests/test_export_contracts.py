from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_annotation import (
    ExportRequestBody,
    LidarExportOptions,
    VideoExportFrameSelection,
    VideoExportScopeRequest,
    VideoExportSegmentSelection,
)


def test_video_frame_selection_checks_bounds_and_order():
    with pytest.raises(ValidationError):
        VideoExportFrameSelection(from_frame=-1, to_frame=2)
    with pytest.raises(ValidationError, match="to_frame must be greater"):
        VideoExportFrameSelection(from_frame=3, to_frame=2)


def test_video_scope_accepts_discriminated_frame_or_uuid_segment_selection():
    task_id = uuid4()
    scope = VideoExportScopeRequest.model_validate(
        {
            "task_id": str(task_id),
            "selection": {"kind": "frames", "from_frame": 2, "to_frame": 4},
        }
    )
    assert scope.task_id == task_id
    assert isinstance(scope.selection, VideoExportFrameSelection)

    segment_id = uuid4()
    segments = VideoExportSegmentSelection(
        start_segment_id=segment_id, end_segment_id=uuid4()
    )
    assert segments.start_segment_id == segment_id

    with pytest.raises(ValidationError):
        VideoExportScopeRequest.model_validate(
            {
                "task_id": "not-a-uuid",
                "selection": {"kind": "frames", "from_frame": 0, "to_frame": 0},
            }
        )


def test_export_models_forbid_unknown_fields_and_validate_lidar_role():
    with pytest.raises(ValidationError):
        ExportRequestBody.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        LidarExportOptions(kitti_camera_role="camera/left")
    with pytest.raises(ValidationError):
        LidarExportOptions(kitti_camera_role="x" * 51)
    assert LidarExportOptions(kitti_camera_role=None).kitti_camera_role is None
