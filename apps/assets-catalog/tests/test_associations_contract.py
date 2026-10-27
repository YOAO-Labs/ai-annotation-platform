from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_catalog.associations_contract import (
    ASSOCIATIONS_SCHEMA,
    AssociationFailure,
    DatasetProjectsResult,
    LinkedProjectSnapshot,
    ProjectDatasetSummary,
    ProjectDatasetsResult,
    decode_associations_reply,
)


def _workspace_project() -> dict:
    return {
        "id": str(uuid4()),
        "organization_id": None,
        "display_id": "P-1",
        "name": "project",
        "type_label": "image",
        "type_key": "image_classification",
        "data_type": "image",
        "owner_id": str(uuid4()),
        "status": "in_progress",
        "ai_enabled": False,
        "tool_bindings": {"unit": {"enabled": True, "classes": []}},
        "label_config": {},
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": 0.7,
        "box_threshold": 0.35,
        "text_threshold": 0.25,
        "ai_interactive_enabled": True,
        "raster_mask_native_editing_enabled": True,
        "mask_qc_config": {"unknown_field": "preserved"},
        "point_cloud_quality_config": {},
        "rendering_config": {},
        "video_sampling": {},
        "video_collaboration": {},
        "scene_mode": False,
        "prefer_same_scene_continuation": False,
        "scene_continuation_window_min": 30,
        "default_variants": {},
        "preannotate_pipeline": None,
        "annotation_guide": None,
        "guide_assets": [],
        "guide_assets_revision": 1,
        "model_version": None,
        "task_lock_ttl_seconds": 300,
        "due_date": None,
        "created_at": "2026-10-18T00:00:00+00:00",
        "updated_at": "2026-10-18T00:00:00+00:00",
    }


def test_linked_project_decodes_full_workspace_snapshot_and_labeling_counters() -> None:
    dataset_id = uuid4()
    project = {
        **_workspace_project(),
        "total_tasks": 15,
        "completed_tasks": 5,
        "review_tasks": 2,
        "in_progress_tasks": 8,
        "batch_summary": {"unknown_counter": 4},
    }
    result = decode_associations_reply(
        {
            "schema": ASSOCIATIONS_SCHEMA,
            "ok": True,
            "data": {"dataset_id": str(dataset_id), "projects": [project]},
        },
        DatasetProjectsResult,
    )

    assert isinstance(result, DatasetProjectsResult)
    assert isinstance(result.projects[0], LinkedProjectSnapshot)
    assert result.projects[0].tool_bindings["unit"]["enabled"] is True
    assert result.projects[0].total_tasks == 15
    assert result.projects[0].batch_summary == {"unknown_counter": 4}


def test_project_dataset_linked_at_keeps_utc_plus_zero_and_null() -> None:
    project_id = uuid4()
    linked_at = ProjectDatasetSummary(
        id=uuid4(),
        display_id="D-1",
        name="dataset",
        data_type="image",
        linked_at=datetime(2026, 10, 18, tzinfo=UTC),
        items_count=4,
        tasks_in_project=3,
    )
    unlinked_at = ProjectDatasetSummary(
        id=uuid4(),
        display_id="D-2",
        name="empty",
        data_type="image",
        linked_at=None,
        items_count=0,
        tasks_in_project=0,
    )
    result = ProjectDatasetsResult(
        project_id=project_id,
        datasets=[linked_at, unlinked_at],
    )

    payload = result.model_dump(mode="json")
    assert payload["datasets"][0]["linked_at"] == "2026-10-18T00:00:00+00:00"
    assert payload["datasets"][1]["linked_at"] is None


def test_duplicate_project_or_dataset_ids_fail_contract_validation() -> None:
    project = {
        **_workspace_project(),
        "total_tasks": 0,
        "completed_tasks": 0,
        "review_tasks": 0,
        "in_progress_tasks": 0,
        "batch_summary": {},
    }
    dataset_id = uuid4()
    with pytest.raises(ValidationError, match="duplicate ids"):
        decode_associations_reply(
            {
                "schema": ASSOCIATIONS_SCHEMA,
                "ok": True,
                "data": {"dataset_id": str(dataset_id), "projects": [project, project]},
            },
            DatasetProjectsResult,
        )

    dataset = {
        "id": str(uuid4()),
        "display_id": "D-1",
        "name": "dataset",
        "data_type": "image",
        "linked_at": None,
        "items_count": 0,
        "tasks_in_project": 0,
    }
    with pytest.raises(ValidationError, match="duplicate ids"):
        decode_associations_reply(
            {
                "schema": ASSOCIATIONS_SCHEMA,
                "ok": True,
                "data": {"project_id": str(uuid4()), "datasets": [dataset, dataset]},
            },
            ProjectDatasetsResult,
        )


def test_association_failure_is_typed_and_unknown_contract_is_rejected() -> None:
    with pytest.raises(AssociationFailure, match="project_missing"):
        decode_associations_reply(
            {
                "schema": ASSOCIATIONS_SCHEMA,
                "ok": False,
                "error": {"code": "project_missing"},
            },
            ProjectDatasetsResult,
        )

    with pytest.raises(ValueError, match="unknown error"):
        decode_associations_reply(
            {
                "schema": ASSOCIATIONS_SCHEMA,
                "ok": False,
                "error": {"code": "database_error"},
            },
            ProjectDatasetsResult,
        )
