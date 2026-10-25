from datetime import date, datetime, timezone
import json
from uuid import uuid4

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from aap_workspace_contract.project_query_values import materialize_project_base_row


def _project_row(project_id, owner_id):
    return {
        "id": str(project_id),
        "organization_id": None,
        "display_id": "P-1",
        "name": "Project",
        "type_label": "Image",
        "type_key": "image",
        "data_type": "image",
        "owner_id": str(owner_id),
        "status": "active",
        "ai_enabled": False,
        "ml_backend_pool_id": None,
        "default_variants": '{"pool":{"axis":"value"}}',
        "preannotate_pipeline": None,
        "label_config": "{}",
        "tool_bindings": '{"bbox":{"enabled":true}}',
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": "0.7",
        "box_threshold": "0.35",
        "text_threshold": "0.25",
        "ai_interactive_enabled": True,
        "raster_mask_native_editing_enabled": True,
        "mask_qc_config": "{}",
        "point_cloud_quality_config": "{}",
        "rendering_config": "{}",
        "annotation_guide": None,
        "guide_assets": "[]",
        "model_version": None,
        "task_lock_ttl_seconds": 300,
        "total_tasks": 4,
        "completed_tasks": 2,
        "review_tasks": 1,
        "in_progress_tasks": 1,
        "batch_summary": '{"total":4}',
        "video_sampling": "{}",
        "video_collaboration": "{}",
        "scene_mode": False,
        "prefer_same_scene_continuation": False,
        "scene_continuation_window_min": 30,
        "due_date": "2026-01-02",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-03T00:00:00+00:00",
    }


def test_base_materializer_returns_all_orm_columns_without_query_enrichments():
    project_id, owner_id = uuid4(), uuid4()

    row = materialize_project_base_row(
        json.dumps(_project_row(project_id, owner_id)), target_id=project_id
    )

    assert set(row) == set(PROJECT_ROW_COLUMNS)
    assert row["id"] == project_id
    assert row["owner_id"] == owner_id
    assert row["default_variants"] == {"pool": {"axis": "value"}}
    assert row["tool_bindings"] == {"bbox": {"enabled": True}}
    assert row["iou_dedup_threshold"] == 0.7
    assert row["due_date"] == date(2026, 1, 2)
    assert row["created_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert row["updated_at"] == datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert "owner_name" not in row
    assert "member_count" not in row


def test_base_materializer_preserves_asyncpg_infinity_values():
    project_id, owner_id = uuid4(), uuid4()
    raw = _project_row(project_id, owner_id)
    raw.update(
        due_date="infinity",
        created_at="infinity",
        updated_at="-infinity",
    )

    row = materialize_project_base_row(json.dumps(raw), target_id=project_id)

    assert row["due_date"] == date.max
    assert row["created_at"] == datetime.max
    assert row["updated_at"] == datetime.min
