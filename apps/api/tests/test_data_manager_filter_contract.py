"""Pure contracts for the shared Data Manager filter tree boundary."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.selectable import Alias, TableClause
from sqlalchemy.sql.util import find_tables

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.services.data_management.entity_filters import (
    compile_entity_filter,
    invalid_entity_filter_fields,
    validate_entity_view,
)
from app.services.data_management.filter_tree import (
    MAX_FILTER_DEPTH,
    MAX_FILTER_NODES,
    validate_filter_tree,
)
from app.services.data_management.schema import build_data_manager_schema
from app.services.data_management.task_filters import _MAX_IN_VALUES, compile_filter
from app.services.data_management.views import (
    _last_activity_at_expr,
    builtin_views,
    invalid_filter_fields,
)
from app.services.labeling_core_read_relations import annotation_row


def _project(*, data_type: str = "image", scene_mode: bool = False) -> Project:
    return Project(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        display_id="P-FILTER-CONTRACT",
        name="filter contract",
        type_label="Image",
        type_key="image-det",
        data_type=data_type,
        scene_mode=scene_mode,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": ["car"],
                "attribute_schema": {
                    "fields": [
                        {
                            "key": "color.name",
                            "type": "select",
                            "options": [{"value": "red", "label": "Red"}],
                        },
                        {"key": "score", "type": "number"},
                        {
                            "key": "tags",
                            "type": "multiselect",
                            "options": [{"value": "front", "label": "Front"}],
                        },
                    ]
                },
            }
        },
    )


def _assert_422(callable_, *args, **kwargs) -> None:
    with pytest.raises(HTTPException) as exc:
        callable_(*args, **kwargs)
    assert exc.value.status_code == 422


def test_data_manager_feedback_reads_use_published_facts():
    # Guard the relation boundary, independent of how SQL renders correlation.
    project = _project()
    reads = {
        field: compile_filter(
            {"field": field, "op": "eq", "value": value}, project=project
        )
        for field, value in (
            ("feedback.kind", "issue"),
            ("feedback.status", "resolved"),
            ("feedback.severity", "warn"),
        )
    }
    for field in ("feedback.unresolved_count", "issue.unresolved_count"):
        reads[field] = compile_entity_filter(
            {"field": field, "op": "gt", "value": 0}, annotation_row, project=project
        )
    reads["last_activity_at"] = _last_activity_at_expr()

    for field, expression in reads.items():
        sources = set()
        for relation in find_tables(expression, check_columns=True):
            while isinstance(relation, Alias):
                relation = relation.element
            if isinstance(relation, TableClause) and relation.name in {
                "annotation_feedbacks",
                "annotation_feedback_facts",
            }:
                sources.add(relation.fullname)
        assert sources == {"yoao_labeling_v1.annotation_feedback_facts"}, field


@pytest.mark.parametrize("compiler", [compile_filter, compile_entity_filter])
def test_non_object_children_are_rejected_consistently(compiler):
    tree = {
        "op": "and",
        "rules": [{"field": "task.status", "op": "eq", "value": "pending"}, None],
    }
    if compiler is compile_filter:
        _assert_422(compiler, tree)
    else:
        _assert_422(compiler, tree, Annotation, project=_project())


def test_filter_tree_budgets_count_root_and_accept_boundaries():
    allowed = {
        "op": "and",
        "rules": [{"field": "task.status", "op": "eq", "value": "pending"}]
        * (MAX_FILTER_NODES - 1),
    }
    validate_filter_tree(allowed)
    too_many = {
        "op": "and",
        "rules": [{"field": "task.status", "op": "eq", "value": "pending"}]
        * MAX_FILTER_NODES,
    }
    _assert_422(validate_filter_tree, too_many)

    leaf: dict = {"field": "task.status", "op": "eq", "value": "pending"}
    for _ in range(MAX_FILTER_DEPTH - 1):
        leaf = {"op": "and", "rules": [leaf]}
    validate_filter_tree(leaf)
    _assert_422(
        validate_filter_tree,
        {"op": "and", "rules": [leaf]},
    )


def test_generated_builtin_required_view_stays_within_structural_budget():
    project = _project()
    fields = project.tool_bindings["bbox"]["attribute_schema"]["fields"]
    fields.extend(
        {"key": f"required_{index}", "type": "text", "required": True}
        for index in range(3, (MAX_FILTER_NODES - 1) // 2)
    )
    view = next(
        item
        for item in builtin_views(project.id, project=project)
        if item["key"] == "missing-required-attributes"
    )
    validate_filter_tree(view["filter_json"])


def test_in_limit_applies_to_task_annotation_and_entity_paths():
    values = [f"class-{index}" for index in range(_MAX_IN_VALUES + 1)]
    tree = {
        "op": "and",
        "rules": [{"field": "annotation.class_name", "op": "in", "value": values}],
    }
    _assert_422(compile_filter, tree, project=_project())
    _assert_422(compile_entity_filter, tree, Annotation, project=_project())


def test_wrong_scalar_and_array_shapes_are_422():
    _assert_422(
        compile_filter,
        {"field": "task.status", "op": "eq", "value": ["pending"]},
    )
    _assert_422(
        compile_filter,
        {"field": "annotation.annotation_count", "op": "gt", "value": "1"},
    )
    _assert_422(
        compile_filter,
        {"field": "task.status", "op": "in", "value": "pending"},
    )


def test_schema_advertises_effective_annotation_and_numeric_attribute_operators():
    schema = build_data_manager_schema(_project())
    fields = {field.key: field for field in schema.filter_fields}
    assert "ne" in fields["annotation.source"].operators
    entity_fields = {
        field.key: field
        for field in build_data_manager_schema(_project(), "objects").filter_fields
    }
    assert "ne" in entity_fields["annotation.class_name"].operators
    validate_entity_view(
        entity_scope="objects",
        filter_json={"field": "annotation.class_name", "op": "ne", "value": "person"},
        sort_json=[],
        columns_json=[],
        project=_project(),
    )
    assert fields["annotation.attribute.bbox.score"].operators == [
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "between",
        "exists",
        "missing",
    ]


def test_malformed_saved_filter_tree_is_reported():
    project = _project()
    malformed = {
        "op": "and",
        "rules": [{"field": "task.status", "op": "eq", "value": "pending"}, 1],
    }
    assert invalid_filter_fields(malformed, project) == ["__filter__"]
    assert invalid_entity_filter_fields(malformed, "objects", project) == ["__filter__"]


def test_entity_validation_uses_dynamic_schema_fields():
    project = _project()
    valid = {
        "field": "annotation.attribute.bbox.color.name",
        "op": "eq",
        "value": "red",
    }
    validate_entity_view(
        entity_scope="objects",
        filter_json=valid,
        sort_json=[],
        columns_json=[],
        project=project,
    )
    _assert_422(
        validate_entity_view,
        entity_scope="objects",
        filter_json={
            "field": "annotation.attribute.bbox.retired",
            "op": "eq",
            "value": "x",
        },
        sort_json=[],
        columns_json=[],
        project=project,
    )


def test_project_capabilities_reject_incompatible_saved_task_fields():
    image = _project()
    views = builtin_views(image.id, project=image)
    for view in views:
        compile_filter(view["filter_json"], project=image)
    ai_review = next(view for view in views if view["key"] == "ai-review")
    assert ai_review["filter_json"] == {
        "op": "and",
        "rules": [
            {
                "field": "ai.pending_prediction_shape_count",
                "op": "gt",
                "value": 0,
            }
        ],
    }
    _assert_422(
        compile_filter,
        {"field": "scene.frame_index", "op": "eq", "value": 1},
        project=image,
    )
    _assert_422(
        compile_filter,
        {"field": "ai.pending_tracker_job_count", "op": "gt", "value": 0},
        project=image,
    )


def test_datetime_uuid_and_finite_numeric_filter_values():
    assignee_id = uuid.UUID(int=1)
    date_clause = compile_filter(
        {
            "field": "task.created_at",
            "op": "gte",
            "value": "2026-09-01T00:00:00Z",
        }
    )
    assert date_clause.right.value == datetime(2026, 9, 1, tzinfo=timezone.utc)

    uuid_clause = compile_filter(
        {"field": "task.assignee", "op": "eq", "value": str(assignee_id)}
    )
    assert uuid_clause.right.value == assignee_id

    _assert_422(
        compile_filter,
        {"field": "task.created_at", "op": "gte", "value": "bad-date"},
    )
    _assert_422(
        compile_filter,
        {"field": "task.assignee", "op": "eq", "value": "not-a-uuid"},
    )
    _assert_422(
        compile_filter,
        {"field": "annotation.annotation_count", "op": "gt", "value": math.inf},
    )
    _assert_422(
        compile_filter,
        {"field": "annotation.annotation_count", "op": "gt", "value": 10**1000},
    )
