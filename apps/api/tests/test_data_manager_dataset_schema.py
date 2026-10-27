"""Published dataset filters use the existing shared Data Manager contract."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.db.models.project import Project
from app.services.data_management.entity_filters import validate_entity_view
from app.services.data_management.schema import build_data_manager_schema
from app.services.data_management.task_filters import compile_filter


def _project(data_type: str = "image", scene_mode: bool = False) -> Project:
    return Project(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        display_id="P-DATASET-FILTER",
        name="dataset filters",
        type_label=data_type,
        type_key=f"{data_type}-det",
        data_type=data_type,
        scene_mode=scene_mode,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
            }
        },
    )


@pytest.mark.parametrize(
    ("data_type", "scene_mode"),
    [("image", False), ("video", False), ("lidar", False), ("lidar", True)],
)
def test_dataset_field_is_published_for_each_available_entity_scope(
    data_type, scene_mode
):
    project = _project(data_type, scene_mode)
    for scope in build_data_manager_schema(project).available_entity_scopes:
        schema = build_data_manager_schema(project, scope)
        fields = [
            field for field in schema.filter_fields if field.key == "dataset.dataset_id"
        ]
        assert len(fields) == 1
        assert fields[0].value_type == "text"
        assert fields[0].operators == ["eq", "ne", "in"]
        assert fields[0].options == []
        rule = {
            "field": fields[0].key,
            "op": "eq",
            "value": str(uuid.uuid4()),
        }
        compile_filter(rule, project=project)
        if scope != "tasks":
            validate_entity_view(
                entity_scope=scope,
                filter_json=rule,
                sort_json=[],
                columns_json=[],
                project=project,
            )


@pytest.mark.parametrize(
    ("op", "value"),
    [("ne", str(uuid.uuid4())), ("in", [str(uuid.uuid4()), str(uuid.uuid4())])],
)
def test_advertised_dataset_operators_are_accepted(op, value):
    compile_filter(
        {"field": "dataset.dataset_id", "op": op, "value": value},
        project=_project(),
    )


@pytest.mark.parametrize(
    ("op", "value"),
    [
        ("eq", "not-a-uuid"),
        ("in", [str(uuid.uuid4()), "not-a-uuid"]),
        ("contains", str(uuid.uuid4())),
    ],
)
def test_published_dataset_filter_preserves_uuid_and_operator_validation(op, value):
    with pytest.raises(HTTPException) as exc:
        compile_filter(
            {"field": "dataset.dataset_id", "op": op, "value": value},
            project=_project(),
        )
    assert exc.value.status_code == 422
