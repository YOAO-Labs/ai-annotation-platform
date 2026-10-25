"""Pure checks for Labeling-owned video record projections."""

import uuid

from app.services.video_tracking.runner import _video_control_job_record


def test_human_review_record_keeps_sourceless_target_fields():
    job_id = uuid.uuid4()
    row = _video_control_job_record(
        {
            "id": job_id,
            "task_id": uuid.uuid4(),
            "dataset_item_id": uuid.uuid4(),
            "annotation_id": None,
            "segment_id": uuid.uuid4(),
            "target_class_name": "vehicle",
            "target_tool_unit_id": "bbox",
            "created_by": uuid.uuid4(),
            "status": "pending_review",
            "job_kind": "tracking",
            "track_id_snapshot": None,
            "correction_frame": None,
            "model_key": "mock_bbox",
            "direction": "forward",
            "from_frame": 0,
            "to_frame": 1,
            "prompt": {},
            "staged_result": {"results": []},
            "revision": 1,
            "event_channel": f"video-tracker-job:{job_id}",
            "celery_task_id": None,
            "cancel_requested_at": None,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "created_at": "2026-10-25T00:00:00Z",
            "updated_at": "2026-10-25T00:00:00Z",
        }
    )

    assert row.target_class_name == "vehicle"
    assert row.target_tool_unit_id == "bbox"
    assert row.staged_result == {"results": []}
