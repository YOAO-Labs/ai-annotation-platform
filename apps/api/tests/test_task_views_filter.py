"""Pure unit tests for the Data Manager task-view filter/sort/column compiler.

These exercise the SQL whitelist surface (app.services.data_management) without a DB:
compile_filter / validate_* build SQLAlchemy expressions, so we only assert that
allowed inputs compile and disallowed inputs raise HTTP 422.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.elements import ColumnElement

from app.services.data_management.task_filters import _MAX_IN_VALUES, compile_filter
from app.services.data_management.views import (
    validate_columns,
    validate_filter,
    validate_sort,
)

_SUPPORTED_RULES = [
    ("task.status", "in", ["pending", "review"]),
    ("task.status", "eq", "pending"),
    ("task.assignee", "eq", "00000000-0000-0000-0000-000000000001"),
    ("task.reviewer", "ne", "00000000-0000-0000-0000-000000000002"),
    ("task.batch_id", "eq", "00000000-0000-0000-0000-000000000003"),
    ("task.created_at", "gte", "2026-01-01T00:00:00Z"),
    ("task.updated_at", "lt", "2026-12-31T00:00:00Z"),
    ("task.frame_index", "eq", 3),
    ("scene.frame_index", "gt", 1),
    ("task.scene_id", "in", ["00000000-0000-0000-0000-000000000004"]),
    ("scene.scene_id", "eq", "00000000-0000-0000-0000-000000000005"),
    ("dataset.dataset_id", "eq", "00000000-0000-0000-0000-000000000006"),
    ("dataset.file_type", "in", ["image", "pcd"]),
    ("annotation.annotation_count", "gte", 1),
    ("annotation.class_name", "exists", None),
    ("annotation.class_name", "eq", "car"),
    ("annotation.class_name", "in", ["car", "bus"]),
    ("prediction.prediction_count", "gt", 0),
    ("prediction.model_version", "eq", "sam3-v1"),
    ("prediction.model_version", "exists", None),
    ("prediction.source", "in", ["auto", "manual"]),
    ("prediction.avg_confidence", "lte", 0.9),
    ("feedback.unresolved_count", "gt", 0),
    ("feedback.kind", "eq", "issue"),
    ("feedback.severity", "in", ["warn", "error"]),
    ("scene.scene_name", "eq", "scene-1"),
]


@pytest.mark.parametrize("field,op,value", _SUPPORTED_RULES)
def test_compile_filter_supported_rules(field, op, value):
    clause = compile_filter(
        {"op": "and", "rules": [{"field": field, "op": op, "value": value}]}
    )
    assert isinstance(clause, ColumnElement)


def test_compile_filter_nested_and_or():
    clause = compile_filter(
        {
            "op": "or",
            "rules": [
                {"field": "task.status", "op": "eq", "value": "pending"},
                {
                    "op": "and",
                    "rules": [
                        {
                            "field": "annotation.annotation_count",
                            "op": "gte",
                            "value": 1,
                        },
                        {
                            "field": "prediction.prediction_count",
                            "op": "gt",
                            "value": 0,
                        },
                    ],
                },
            ],
        }
    )
    assert isinstance(clause, ColumnElement)


@pytest.mark.parametrize(
    "filter_json",
    [
        {"op": "and", "rules": [{"field": "task.raw_sql", "op": "eq", "value": "1"}]},
        {"op": "xor", "rules": []},
        {"op": "and", "rules": "not-a-list"},
        {"op": "and", "rules": [{"op": "eq", "value": "x"}]},
        {"op": "and", "rules": [{"field": "task.status", "op": "bogus", "value": "x"}]},
        {"op": "and", "rules": [{"field": "task.status", "op": "in", "value": "x"}]},
    ],
)
def test_compile_filter_invalid_raises_422(filter_json):
    with pytest.raises(HTTPException) as exc:
        compile_filter(filter_json)
    assert exc.value.status_code == 422


def test_compile_filter_in_list_cap():
    too_long = {
        "op": "and",
        "rules": [
            {
                "field": "task.status",
                "op": "in",
                "value": [f"s{i}" for i in range(_MAX_IN_VALUES + 1)],
            }
        ],
    }
    with pytest.raises(HTTPException) as exc:
        compile_filter(too_long)
    assert exc.value.status_code == 422

    at_limit = {
        "op": "and",
        "rules": [
            {
                "field": "task.status",
                "op": "in",
                "value": [f"s{i}" for i in range(_MAX_IN_VALUES)],
            }
        ],
    }
    assert isinstance(compile_filter(at_limit), ColumnElement)


def test_validate_filter_rejects_unknown_field():
    with pytest.raises(HTTPException) as exc:
        validate_filter(
            {"op": "and", "rules": [{"field": "nope", "op": "eq", "value": 1}]}
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        "task.status",
        "display_id",
        "file_name",
        "annotation_count",
        "prediction_count",
        "avg_prediction_confidence",
        "unresolved_feedback_count",
        "model_versions",
        "scene_name",
        "scene.frame_index",
        "last_activity_at",
    ],
)
def test_validate_sort_supported_fields(field):
    validate_sort([{"field": field, "direction": "desc"}])


def test_validate_sort_rejects_unknown_field_and_direction():
    with pytest.raises(HTTPException) as exc:
        validate_sort([{"field": "task.raw_sql", "direction": "asc"}])
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        validate_sort([{"field": "task.status", "direction": "sideways"}])
    assert exc.value.status_code == 422


def test_validate_columns_allows_known_and_rejects_unknown():
    validate_columns(["display_id", "status", "assignee", "reviewer", "batch_id"])
    with pytest.raises(HTTPException) as exc:
        validate_columns(["display_id", "secret_column"])
    assert exc.value.status_code == 422
