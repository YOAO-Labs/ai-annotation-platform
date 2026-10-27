from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_workspace_contract import WorkspaceProjectSnapshot


def snapshot_payload():
    return {
        "id": str(uuid4()),
        "organization_id": None,
        "display_id": "P-1",
        "name": "Workspace",
        "type_label": "Image Detection",
        "type_key": "image-det",
        "data_type": "image",
        "owner_id": str(uuid4()),
        "status": "active",
        "ai_enabled": False,
        "tool_bindings": {"bbox": {"enabled": True, "future_binding": {"x": 1}}},
        "label_config": {"future_label_config": [1, "x"]},
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": 0.7,
        "box_threshold": 0.35,
        "text_threshold": 0.25,
        "ai_interactive_enabled": True,
        "raster_mask_native_editing_enabled": True,
        "mask_qc_config": {"future_qc": {"enabled": True}},
        "point_cloud_quality_config": {},
        "rendering_config": {"future_render": ["unknown"]},
        "video_sampling": {},
        "video_collaboration": {},
        "scene_mode": False,
        "prefer_same_scene_continuation": False,
        "scene_continuation_window_min": 30,
        "default_variants": {"pool": {"family": "v2", "future_variant": 7}},
        "preannotate_pipeline": [{"future_stage": {"setting": True}}],
        "annotation_guide": None,
        "guide_assets": [{"key": "guides/a.png", "future_field": "kept"}],
        "guide_assets_revision": 1,
        "model_version": None,
        "task_lock_ttl_seconds": 300,
        "due_date": "2026-12-31",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }


def test_snapshot_parses_wire_ids_and_dates_and_preserves_unknown_json():
    project = WorkspaceProjectSnapshot.model_validate(snapshot_payload())

    assert project.organization_id is None
    assert project.due_date == date(2026, 12, 31)
    assert project.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert project.tool_bindings["bbox"]["future_binding"] == {"x": 1}
    assert project.default_variants["pool"]["future_variant"] == 7
    assert project.guide_assets[0]["future_field"] == "kept"
    assert not hasattr(project, "total_tasks")
    assert not hasattr(project, "ml_backend_id")


@pytest.mark.parametrize(
    ("field", "value"),
    [("ai_enabled", "false"), ("maximum_annotations", "1"), ("box_threshold", "1")],
)
def test_snapshot_rejects_coerced_scalar_types(field, value):
    payload = snapshot_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        WorkspaceProjectSnapshot.model_validate(payload)


def test_snapshot_forbids_unknown_top_level_fields_and_mutation():
    payload = snapshot_payload()
    payload["ml_backend_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        WorkspaceProjectSnapshot.model_validate(payload)

    project = WorkspaceProjectSnapshot.model_validate(snapshot_payload())
    with pytest.raises(ValidationError):
        project.name = "changed"
