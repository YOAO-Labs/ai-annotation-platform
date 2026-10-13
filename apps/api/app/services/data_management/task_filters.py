"""Task filter compiler, sort and visibility primitives (primitive layer).

The filter/visibility helpers shared by entity_filters, entities, tracks and views.
Extracted from the legacy ``task_views.py`` so ``entity_filters`` can depend on a stable
filter interface instead of private ``task_views`` functions. Depends only on DB models,
the scheduler visibility helpers and the sibling task_metrics primitives — no views or
service.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from numbers import Real
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Float,
    Select,
    and_,
    cast,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
)
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.services.data_management.filter_tree import (
    _MAX_IN_VALUES,
    validate_filter_tree,
    validate_in_value,
)
from app.services.data_management.schema import _track_capable
from app.services.data_management.task_metrics import (  # noqa: F401
    low_confidence_pending_prediction_shapes_expr,
    pending_prediction_shapes_expr,
    pending_tracker_jobs_expr,
)
from app.services.project_kind import project_kind
from app.services.project_access import is_privileged_for_project
from app.services.scheduler import task_visibility_clause
from app.services.feedback import unresolved_issue_count_sq
from app.services.task_discussion import task_comment_count_sq

_STRING_OPS = {"eq", "ne", "in"}


_NUMERIC_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}


_DATE_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}


_EXISTS_OPS = {"exists", "eq", "in"}


_TASK_FIELD_MAP = {
    "task.status": Task.status,
    # Filters also run in privileged queries without a TaskBatch join.
    "task.assignee": func.coalesce(
        Task.assignee_id,
        select(TaskBatch.annotator_id)
        .where(TaskBatch.id == Task.batch_id)
        .correlate(Task)
        .scalar_subquery(),
    ),
    "task.reviewer": func.coalesce(
        Task.reviewer_id,
        select(TaskBatch.reviewer_id)
        .where(TaskBatch.id == Task.batch_id)
        .correlate(Task)
        .scalar_subquery(),
    ),
    "task.batch_id": Task.batch_id,
    "task.created_at": Task.created_at,
    "task.updated_at": Task.updated_at,
}


_UUID_FILTER_FIELDS = {
    "task.assignee",
    "task.reviewer",
    "task.batch_id",
    "task.scene_id",
    "scene.scene_id",
    "dataset.dataset_id",
}


_DATETIME_FILTER_FIELDS = {"task.created_at", "task.updated_at"}


def compile_filter(
    filter_json: dict[str, Any],
    project: Project | None = None,
    user: User | None = None,
) -> ColumnElement[bool]:
    validate_filter_tree(filter_json)
    if not filter_json:
        return literal(True)
    return _compile_node(filter_json, project=project, user=user)


def _compile_node(
    node: dict[str, Any],
    project: Project | None = None,
    user: User | None = None,
) -> ColumnElement[bool]:
    if not isinstance(node, dict):
        raise HTTPException(
            status_code=422, detail="Filter group children must be objects"
        )
    if "rules" in node:
        op = node.get("op", "and")
        if op not in {"and", "or"}:
            raise HTTPException(
                status_code=422, detail="Filter group op must be and/or"
            )
        rules = node.get("rules")
        if not isinstance(rules, list):
            raise HTTPException(
                status_code=422, detail="Filter group rules must be a list"
            )
        annotation_rules = (
            [
                rule
                for rule in rules
                if isinstance(rule, dict) and _is_annotation_object_rule(rule)
            ]
            if op == "and"
            else []
        )
        other_rules = [rule for rule in rules if rule not in annotation_rules]
        clauses = [
            _compile_node(rule, project=project, user=user) for rule in other_rules
        ]
        if annotation_rules:
            annotation = aliased(Annotation)
            clauses.append(
                exists().where(
                    annotation.task_id == Task.id,
                    annotation.is_active.is_(True),
                    annotation.was_cancelled.is_(False),
                    *[
                        _compile_annotation_object_condition(
                            annotation,
                            str(rule["field"]),
                            str(rule["op"]),
                            rule.get("value"),
                            project,
                        )
                        for rule in annotation_rules
                    ],
                )
            )
        if not clauses:
            return literal(True)
        return and_(*clauses) if op == "and" else or_(*clauses)

    field = node.get("field")
    op = node.get("op")
    value = node.get("value")
    if not isinstance(field, str) or not isinstance(op, str):
        raise HTTPException(status_code=422, detail="Filter rule needs field and op")
    # 防止单请求用超长 in 列表拖慢 DB（所有走 .in_() 的字段在此统一收口）。
    if op == "in" and isinstance(value, list) and len(value) > _MAX_IN_VALUES:
        validate_in_value(value, max_values=_MAX_IN_VALUES)
    return _compile_rule(field, op, value, project=project, user=user)


def _compile_rule(
    field: str,
    op: str,
    value: Any,
    *,
    project: Project | None = None,
    user: User | None = None,
) -> ColumnElement[bool]:
    if project is not None and not _is_project_filter_field_supported(field, project):
        raise HTTPException(
            status_code=422, detail=f"Unsupported filter field: {field}"
        )
    _validate_rule_value(field, op, value, project)
    value = _coerce_filter_value(field, op, value)
    if field == "task.keyword":
        return _compare_task_keyword(op, value, project)
    if field in _TASK_FIELD_MAP:
        allowed_ops = _DATE_OPS if field.endswith("_at") else _STRING_OPS
        return _compare_column(_TASK_FIELD_MAP[field], op, value, allowed_ops)
    if field in {"task.frame_index", "scene.frame_index"}:
        return _compare_scene_frame(op, value)
    if field in {"task.scene_id", "scene.scene_id"}:
        return _compare_scene_id(op, value)
    if field == "dataset.dataset_id":
        return _compare_dataset_id(op, value)
    if field == "dataset.file_type":
        return _compare_dataset_file_type(op, value)
    if field == "annotation.annotation_count":
        return _compare_column(Task.total_annotations, op, value, _NUMERIC_OPS)
    if field == "annotation.class_name":
        return _compare_annotation_class(op, value)
    if field == "annotation.source":
        return _compare_annotation_column(Annotation.source, op, value)
    if field == "annotation.annotation_type":
        return _compare_annotation_column(Annotation.annotation_type, op, value)
    if field == "annotation.tool_unit_id":
        return _compare_annotation_column(Annotation.tool_unit_id, op, value)
    if field == "annotation.imported":
        return _compare_annotation_imported(op, value)
    if field == "annotation.has_track":
        return _compare_annotation_has_track(op, value)
    if field == "annotation.track_id":
        return _compare_annotation_track_id(op, value)
    if field.startswith("annotation.attribute_origin."):
        return _compare_annotation_attribute_origin(field, op, value, project)
    if field.startswith("annotation.attribute."):
        return _compare_annotation_attribute(field, op, value, project)
    if field == "keyframe.source":
        return _compare_keyframe_source(op, value)
    if field == "prediction.prediction_count":
        return _compare_column(Task.total_predictions, op, value, _NUMERIC_OPS)
    if field == "prediction.model_version":
        return _compare_prediction_field(Prediction.model_version, op, value)
    if field == "prediction.source":
        return _compare_prediction_field(Prediction.source, op, value)
    if field == "prediction.avg_confidence":
        return _compare_scalar(_avg_prediction_confidence_sq(), op, value, _NUMERIC_OPS)
    if field == "feedback.unresolved_count":
        # Compatibility alias: corrected to count unresolved root issues.
        return _compare_scalar(_unresolved_feedback_count_sq(), op, value, _NUMERIC_OPS)
    if field == "issue.unresolved_count":
        return _compare_scalar(_unresolved_issue_count_sq(), op, value, _NUMERIC_OPS)
    if field == "discussion.comment_count":
        return _compare_scalar(_comment_count_sq(), op, value, _NUMERIC_OPS)
    if field == "feedback.kind":
        return _compare_feedback_field(AnnotationFeedback.kind, op, value)
    if field == "feedback.severity":
        return _compare_feedback_field(AnnotationFeedback.severity, op, value)
    if field == "feedback.status":
        return _compare_feedback_status(op, value)
    if field == "scene.scene_name":
        return _compare_scene_name(op, value)
    if field == "ai.pending_prediction_shape_count":
        return _compare_scalar(
            pending_prediction_shapes_expr(), op, value, _NUMERIC_OPS
        )
    if field == "ai.low_confidence_prediction_shape_count":
        return _compare_scalar(
            low_confidence_pending_prediction_shapes_expr(),
            op,
            value,
            _NUMERIC_OPS,
        )
    if field == "ai.pending_tracker_job_count":
        if project is None:
            raise HTTPException(
                status_code=422,
                detail="Project context is required for tracker candidate filters",
            )

        return _compare_scalar(
            pending_tracker_jobs_expr(user, project), op, value, _NUMERIC_OPS
        )
    raise HTTPException(status_code=422, detail=f"Unsupported filter field: {field}")


def _is_annotation_object_rule(node: dict[str, Any]) -> bool:
    field = node.get("field")
    return isinstance(field, str) and (
        (field.startswith("annotation.") and field != "annotation.annotation_count")
        or field == "keyframe.source"
    )


def _is_project_filter_field_supported(field: str, project: Project) -> bool:
    kind = project_kind(project)
    if field.startswith("scene.") and not kind.scene_mode:
        return False
    if field == "ai.pending_tracker_job_count" and kind.data_type != "video":
        return False
    if field == "keyframe.source":
        return kind.data_type == "video" and _track_capable(project)
    if field in {"annotation.has_track", "annotation.track_id"}:
        if kind.scene_mode:
            return True
        if kind.data_type != "video":
            return False
        return _track_capable(project)
    return True


def _invalid_filter_value(detail: str) -> None:
    raise HTTPException(status_code=422, detail=detail)


def _is_number(value: Any) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _parse_datetime_value(field: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid datetime value for {field}"
            ) from exc
    else:
        _invalid_filter_value(f"Filter value for {field} must be datetime text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_datetime_value(field: str, op: str, value: Any) -> None:
    if op not in _DATE_OPS:
        return
    if op in {"eq", "ne"} and value is None:
        return
    if not isinstance(value, (str, datetime, date)):
        _invalid_filter_value(f"Filter value for {field} must be datetime text")
    _parse_datetime_value(field, value)


def _validate_uuid_value(field: str, op: str, value: Any) -> None:
    if op in {"eq", "ne"} and value is None:
        return
    if op == "in":
        if not isinstance(value, list):
            _invalid_filter_value("in value must be a list")
        validate_in_value(value, max_values=_MAX_IN_VALUES)
        values = value
    elif op in {"eq", "ne"}:
        values = [value]
    else:
        return
    for item in values:
        if item is None:
            continue
        if not isinstance(item, (str, uuid.UUID)):
            _invalid_filter_value(f"Filter value for {field} must be a UUID")
        try:
            uuid.UUID(str(item))
        except (AttributeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"Filter value for {field} must be a UUID"
            ) from exc


def _coerce_filter_value(field: str, op: str, value: Any) -> Any:
    if field in _DATETIME_FILTER_FIELDS and op in _DATE_OPS:
        return None if value is None else _parse_datetime_value(field, value)
    if field in _UUID_FILTER_FIELDS:
        if op in {"eq", "ne"}:
            return None if value is None else uuid.UUID(str(value))
        if op == "in":
            return [None if item is None else uuid.UUID(str(item)) for item in value]
    return value


def _validate_text_value(field: str, op: str, value: Any) -> None:
    if op == "in":
        if not isinstance(value, list):
            _invalid_filter_value("in value must be a list")
        validate_in_value(value, max_values=_MAX_IN_VALUES)
        if not all(isinstance(item, str) for item in value):
            _invalid_filter_value(f"Filter value for {field} must contain text")
        return
    if op in {"eq", "ne"} and value is None:
        return
    if op in {"eq", "ne", "contains"} and not isinstance(value, str):
        _invalid_filter_value(f"Filter value for {field} must be text")


def _validate_numeric_value(field: str, op: str, value: Any) -> None:
    if op == "in":
        if not isinstance(value, list):
            _invalid_filter_value("in value must be a list")
        validate_in_value(value, max_values=_MAX_IN_VALUES)
        if not all(_is_number(item) for item in value):
            _invalid_filter_value(f"Filter value for {field} must contain numbers")
        return
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            _invalid_filter_value("between value must have two items")
        if not all(_is_number(item) for item in value):
            _invalid_filter_value(f"Filter value for {field} must contain numbers")
        if value[0] > value[1]:
            _invalid_filter_value("between value must be ordered")
        return
    if op in {"eq", "ne"} and value is None:
        return
    if op in {"eq", "ne", "gt", "gte", "lt", "lte"} and not _is_number(value):
        _invalid_filter_value(f"Filter value for {field} must be a number")


def _validate_boolean_value(field: str, op: str, value: Any) -> None:
    if op == "eq" and not isinstance(value, bool):
        _invalid_filter_value(f"Filter value for {field} must be boolean")


def _validate_rule_value(
    field: str,
    op: str,
    value: Any,
    project: Project | None,
) -> None:
    if op == "in" and isinstance(value, list):
        validate_in_value(value, max_values=_MAX_IN_VALUES)

    if field == "task.keyword":
        _validate_text_value(field, op, value)
        return
    if field in _DATETIME_FILTER_FIELDS:
        _validate_datetime_value(field, op, value)
        return
    if field in _UUID_FILTER_FIELDS:
        _validate_uuid_value(field, op, value)
        return
    if field in _TASK_FIELD_MAP:
        _validate_text_value(field, op, value)
        return
    if field in {
        "task.scene_id",
        "scene.scene_id",
        "dataset.dataset_id",
        "dataset.file_type",
        "annotation.class_name",
        "annotation.source",
        "annotation.annotation_type",
        "annotation.tool_unit_id",
        "annotation.track_id",
        "keyframe.source",
        "prediction.model_version",
        "prediction.source",
        "feedback.kind",
        "feedback.severity",
        "feedback.status",
        "scene.scene_name",
    }:
        _validate_text_value(field, op, value)
        return
    if field in {
        "task.frame_index",
        "scene.frame_index",
        "annotation.annotation_count",
        "prediction.prediction_count",
        "prediction.avg_confidence",
        "feedback.unresolved_count",
        "issue.unresolved_count",
        "discussion.comment_count",
        "ai.pending_prediction_shape_count",
        "ai.low_confidence_prediction_shape_count",
        "ai.pending_tracker_job_count",
    }:
        _validate_numeric_value(field, op, value)
        return
    if field in {"annotation.imported", "annotation.has_track"}:
        _validate_boolean_value(field, op, value)
        return
    if field.startswith("annotation.attribute_origin."):
        _resolve_attribute_field(field, "annotation.attribute_origin.", project)
        _validate_text_value(field, op, value)
        return
    if field.startswith("annotation.attribute."):
        _, _, schema_field = _resolve_attribute_field(
            field, "annotation.attribute.", project
        )
        attr_type = schema_field.get("type", "text")
        if op in {"exists", "missing"}:
            return
        if attr_type in {"number", "range"}:
            _validate_numeric_value(field, op, value)
        elif attr_type == "boolean":
            _validate_boolean_value(field, op, value)
        elif attr_type == "multiselect":
            if op in {"contains_any", "contains_all"}:
                if not isinstance(value, list):
                    _invalid_filter_value(f"Filter value for {field} must be a list")
                if not all(isinstance(item, str) for item in value):
                    _invalid_filter_value(f"Filter value for {field} must contain text")
        else:
            _validate_text_value(field, op, value)


def _compile_annotation_object_condition(
    annotation,
    field: str,
    op: str,
    value: Any,
    project: Project | None,
) -> ColumnElement[bool]:
    if project is not None and not _is_project_filter_field_supported(field, project):
        raise HTTPException(
            status_code=422, detail=f"Unsupported filter field: {field}"
        )
    _validate_rule_value(field, op, value, project)
    if op == "in" and isinstance(value, list):
        validate_in_value(value, max_values=_MAX_IN_VALUES)
    scalar_fields = {
        "annotation.class_name": annotation.class_name,
        "annotation.source": annotation.source,
        "annotation.annotation_type": annotation.annotation_type,
        "annotation.tool_unit_id": annotation.tool_unit_id,
    }
    if field in scalar_fields:
        if op == "exists":
            return scalar_fields[field].is_not(None)
        return _compare_column(scalar_fields[field], op, value, {"eq", "ne", "in"})
    track_id = func.coalesce(
        annotation.track_id, annotation.geometry["track_id"].astext
    )
    if field == "annotation.track_id":
        return _compare_column(track_id, op, value, {"eq", "in"})
    if field == "annotation.has_track":
        if op != "eq" or not isinstance(value, bool):
            raise HTTPException(
                status_code=422,
                detail="annotation.has_track only supports boolean eq",
            )
        return track_id.is_not(None) if value else track_id.is_(None)
    if field == "annotation.imported":
        if op != "eq" or not isinstance(value, bool):
            raise HTTPException(
                status_code=422,
                detail="annotation.imported only supports boolean eq",
            )
        imported = (
            func.coalesce(annotation.attributes["_imported"].astext, "false") == "true"
        )
        return imported if value else not_(imported)
    if field.startswith("annotation.attribute_origin."):
        unit, key, _ = _resolve_attribute_field(
            field, "annotation.attribute_origin.", project
        )
        if op not in {"eq", "in"}:
            raise HTTPException(
                status_code=422, detail="Unsupported attribute origin op"
            )
        origin = func.coalesce(
            annotation.attributes_meta[key]["origin"].astext, "human"
        )
        comparison = (
            origin == value
            if op == "eq"
            else _compare_column(origin, op, value, {"in"})
        )
        return and_(
            annotation.tool_unit_id == unit,
            annotation.attributes.has_key(key),  # noqa: W601
            comparison,
        )
    if field.startswith("annotation.attribute."):
        unit, key, schema_field = _resolve_attribute_field(
            field, "annotation.attribute.", project
        )
        present = annotation.attributes.has_key(key)  # noqa: W601
        if op == "exists":
            condition = present
        elif op == "missing":
            condition = not_(present)
        else:
            text_value = annotation.attributes[key].astext
            attr_type = schema_field.get("type", "text")
            if attr_type in {"number", "range"}:
                number = cast(text_value, Float)
                if op == "between":
                    if not isinstance(value, list) or len(value) != 2:
                        raise HTTPException(
                            status_code=422,
                            detail="between value must have two items",
                        )
                    condition = and_(number >= value[0], number <= value[1])
                else:
                    condition = _compare_column(number, op, value, _NUMERIC_OPS)
            elif attr_type == "boolean":
                if op != "eq" or not isinstance(value, bool):
                    raise HTTPException(
                        status_code=422,
                        detail="boolean attribute only supports eq",
                    )
                condition = text_value == ("true" if value else "false")
            elif attr_type == "multiselect":
                if op not in {"contains_any", "contains_all"} or not isinstance(
                    value, list
                ):
                    raise HTTPException(
                        status_code=422,
                        detail="Invalid multiselect attribute op",
                    )
                contains = [
                    annotation.attributes[key].op("@>")(
                        literal([item], type_=annotation.attributes.type)
                    )
                    for item in value
                ]
                condition = or_(*contains) if op == "contains_any" else and_(*contains)
            elif op == "contains" and attr_type == "text":
                if not isinstance(value, str):
                    raise HTTPException(
                        status_code=422, detail="contains value must be text"
                    )
                condition = text_value.ilike(f"%{value}%")
            else:
                allowed = {"eq", "in"} if attr_type == "select" else {"eq"}
                condition = _compare_column(text_value, op, value, allowed)
            condition = and_(present, condition)
        return and_(annotation.tool_unit_id == unit, condition)
    if field == "keyframe.source":
        if op not in {"eq", "in"}:
            raise HTTPException(
                status_code=422, detail="Unsupported keyframe.source op"
            )
        values = [value] if op == "eq" else value
        if not isinstance(values, list):
            raise HTTPException(status_code=422, detail="in value must be a list")
        return or_(
            *[
                annotation.geometry.op("@>")(
                    literal(
                        {"keyframes": [{"source": source}]},
                        type_=annotation.geometry.type,
                    )
                )
                for source in values
            ]
        )
    raise HTTPException(
        status_code=422, detail=f"Unsupported annotation field: {field}"
    )


def _active_annotation_clause() -> ColumnElement[bool]:
    return and_(
        Annotation.task_id == Task.id,
        Annotation.is_active.is_(True),
        Annotation.was_cancelled.is_(False),
    )


def _compare_task_keyword(
    op: str, value: Any, project: Project | None
) -> ColumnElement[bool]:
    if op != "contains" or not isinstance(value, str):
        raise HTTPException(
            status_code=422, detail="task.keyword only supports contains"
        )
    value = value.strip()
    if not value or len(value) > 200:
        raise HTTPException(
            status_code=422, detail="task.keyword length must be between 1 and 200"
        )
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clauses = [
        Task.display_id.ilike(pattern, escape="\\"),
        Task.file_name.ilike(pattern, escape="\\"),
    ]
    if project is not None and project.scene_mode:
        clauses.append(_scene_name_sq().ilike(pattern, escape="\\"))
    return or_(*clauses)


def _compare_annotation_column(
    column: ColumnElement[Any], op: str, value: Any
) -> ColumnElement[bool]:
    if op not in {"exists", "eq", "ne", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported annotation field op")
    clause = _active_annotation_clause()
    if op == "exists":
        return exists().where(clause, column.is_not(None))
    if op == "eq":
        return exists().where(clause, column == value)
    if op == "ne":
        return exists().where(clause, column != value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(clause, column.in_(value))


def _track_id_expr() -> ColumnElement[str | None]:
    return func.coalesce(Annotation.track_id, Annotation.geometry["track_id"].astext)


def _compare_annotation_imported(op: str, value: Any) -> ColumnElement[bool]:
    if op != "eq" or not isinstance(value, bool):
        raise HTTPException(
            status_code=422, detail="annotation.imported only supports boolean eq"
        )
    imported = (
        func.coalesce(Annotation.attributes["_imported"].astext, "false") == "true"
    )
    return exists().where(
        _active_annotation_clause(), imported if value else not_(imported)
    )


def _compare_annotation_has_track(op: str, value: Any) -> ColumnElement[bool]:
    if op != "eq" or not isinstance(value, bool):
        raise HTTPException(
            status_code=422, detail="annotation.has_track only supports boolean eq"
        )
    has_track = _track_id_expr().is_not(None)
    return exists().where(
        _active_annotation_clause(), has_track if value else not_(has_track)
    )


def _compare_annotation_track_id(op: str, value: Any) -> ColumnElement[bool]:
    if op not in {"eq", "in"}:
        raise HTTPException(
            status_code=422, detail="Unsupported annotation.track_id op"
        )
    track_id = _track_id_expr()
    if op == "eq":
        return exists().where(_active_annotation_clause(), track_id == value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(_active_annotation_clause(), track_id.in_(value))


def _resolve_attribute_field(
    field: str,
    prefix: str,
    project: Project | None,
) -> tuple[str, str, dict[str, Any]]:
    if project is None:
        raise HTTPException(
            status_code=422, detail="Project context is required for attribute filters"
        )
    path = field.removeprefix(prefix)
    if "." not in path:
        raise HTTPException(status_code=422, detail="Invalid attribute field")
    unit, key = path.split(".", 1)
    binding = (project.tool_bindings or {}).get(unit)
    fields = ((binding or {}).get("attribute_schema") or {}).get("fields") or []
    schema_field = next(
        (
            item
            for item in fields
            if isinstance(item, dict) and str(item.get("key")) == key
        ),
        None,
    )
    if (
        not isinstance(binding, dict)
        or not binding.get("enabled", True)
        or not schema_field
    ):
        raise HTTPException(
            status_code=422, detail=f"Unsupported attribute field: {field}"
        )
    return unit, key, schema_field


def _compare_annotation_attribute(
    field: str,
    op: str,
    value: Any,
    project: Project | None,
) -> ColumnElement[bool]:
    unit, key, schema_field = _resolve_attribute_field(
        field, "annotation.attribute.", project
    )
    attr_type = schema_field.get("type", "text")
    present = Annotation.attributes.has_key(key)  # noqa: W601
    base = and_(_active_annotation_clause(), Annotation.tool_unit_id == unit)
    if op == "exists":
        return exists().where(base, present)
    if op == "missing":
        return exists().where(base, not_(present))
    text_value = Annotation.attributes[key].astext
    if attr_type in {"number", "range"}:
        number = cast(text_value, Float)
        if op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise HTTPException(
                    status_code=422, detail="between value must have two items"
                )
            condition = and_(number >= value[0], number <= value[1])
        else:
            condition = _compare_column(number, op, value, _NUMERIC_OPS)
    elif attr_type == "boolean":
        if op != "eq" or not isinstance(value, bool):
            raise HTTPException(
                status_code=422, detail="boolean attribute only supports eq"
            )
        condition = text_value == ("true" if value else "false")
    elif attr_type == "multiselect":
        if op not in {"contains_any", "contains_all"} or not isinstance(value, list):
            raise HTTPException(
                status_code=422, detail="Invalid multiselect attribute op"
            )
        contains = [
            Annotation.attributes[key].op("@>")(
                literal([item], type_=Annotation.attributes.type)
            )
            for item in value
        ]
        condition = or_(*contains) if op == "contains_any" else and_(*contains)
    elif op == "contains" and attr_type == "text":
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail="contains value must be text")
        condition = text_value.ilike(f"%{value}%")
    else:
        allowed = {"eq", "in"} if attr_type == "select" else {"eq"}
        condition = _compare_column(text_value, op, value, allowed)
    return exists().where(base, present, condition)


def _compare_annotation_attribute_origin(
    field: str,
    op: str,
    value: Any,
    project: Project | None,
) -> ColumnElement[bool]:
    unit, key, _ = _resolve_attribute_field(
        field, "annotation.attribute_origin.", project
    )
    if op not in {"eq", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported attribute origin op")
    origin = func.coalesce(Annotation.attributes_meta[key]["origin"].astext, "human")
    base = and_(
        _active_annotation_clause(),
        Annotation.tool_unit_id == unit,
        Annotation.attributes.has_key(key),  # noqa: W601
    )
    if op == "eq":
        return exists().where(base, origin == value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(base, origin.in_(value))


def _compare_annotation_class(op: str, value: Any) -> ColumnElement[bool]:
    if op not in _EXISTS_OPS:
        raise HTTPException(
            status_code=422, detail="Unsupported annotation.class_name op"
        )
    clause = _active_annotation_clause()
    if op == "exists":
        return exists().where(clause)
    if op == "eq":
        return exists().where(clause, Annotation.class_name == value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(clause, Annotation.class_name.in_(value))


def _compare_column(
    column: ColumnElement[Any],
    op: str,
    value: Any,
    allowed_ops: set[str],
) -> ColumnElement[bool]:
    if op not in allowed_ops:
        raise HTTPException(status_code=422, detail=f"Unsupported op for field: {op}")
    if op == "eq":
        return column == value
    if op == "ne":
        return column != value
    if op == "in":
        if not isinstance(value, list):
            raise HTTPException(status_code=422, detail="in value must be a list")
        return column.in_(value)
    if op == "gt":
        return column > value
    if op == "gte":
        return column >= value
    if op == "lt":
        return column < value
    if op == "lte":
        return column <= value
    raise HTTPException(status_code=422, detail=f"Unsupported op: {op}")


def _compare_scalar(
    scalar: ColumnElement[Any],
    op: str,
    value: Any,
    allowed_ops: set[str],
) -> ColumnElement[bool]:
    return _compare_column(scalar, op, value, allowed_ops)


def _compare_keyframe_source(op: str, value: Any) -> ColumnElement[bool]:
    if op not in {"eq", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported keyframe.source op")
    values = [value] if op == "eq" else value
    if not isinstance(values, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    conditions = [
        Annotation.geometry.op("@>")(
            literal({"keyframes": [{"source": source}]}, type_=Annotation.geometry.type)
        )
        for source in values
    ]
    return exists().where(_active_annotation_clause(), or_(*conditions))


def _compare_feedback_status(op: str, value: Any) -> ColumnElement[bool]:
    if op not in {"eq", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported feedback.status op")
    clause = and_(
        AnnotationFeedback.task_id == Task.id,
        AnnotationFeedback.is_active.is_(True),
    )
    if op == "eq":
        return exists().where(clause, AnnotationFeedback.status == value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(clause, AnnotationFeedback.status.in_(value))


def _compare_feedback_field(
    column: ColumnElement[Any], op: str, value: Any
) -> ColumnElement[bool]:
    if op not in {"exists", "eq", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported feedback field op")
    clause = and_(
        AnnotationFeedback.task_id == Task.id,
        AnnotationFeedback.is_active.is_(True),
        AnnotationFeedback.status == "open",
    )
    if op == "exists":
        return exists().where(clause, column.is_not(None))
    if op == "eq":
        return exists().where(clause, column == value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(clause, column.in_(value))


def _compare_prediction_field(
    column: ColumnElement[Any], op: str, value: Any
) -> ColumnElement[bool]:
    if op not in {"exists", "eq", "ne", "in"}:
        raise HTTPException(status_code=422, detail="Unsupported prediction field op")
    clause = Prediction.task_id == Task.id
    if op == "exists":
        return exists().where(clause, column.is_not(None))
    if op == "eq":
        return exists().where(clause, column == value)
    if op == "ne":
        return exists().where(clause, column != value)
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="in value must be a list")
    return exists().where(clause, column.in_(value))


def _compare_scene_id(op: str, value: Any) -> ColumnElement[bool]:
    return _compare_scalar(_scene_id_sq(), op, value, {"eq", "ne", "in"})


def _compare_scene_frame(op: str, value: Any) -> ColumnElement[bool]:
    return _compare_scalar(_scene_frame_sq(), op, value, _NUMERIC_OPS)


def _compare_scene_name(op: str, value: Any) -> ColumnElement[bool]:
    return _compare_scalar(_scene_name_sq(), op, value, _STRING_OPS)


def _compare_dataset_id(op: str, value: Any) -> ColumnElement[bool]:
    return _compare_scalar(_dataset_id_sq(), op, value, {"eq", "ne", "in"})


def _compare_dataset_file_type(op: str, value: Any) -> ColumnElement[bool]:
    return _compare_scalar(_dataset_file_type_sq(), op, value, _STRING_OPS)


def _primary_item_id_sq() -> ColumnElement[uuid.UUID | None]:
    return (
        select(TaskDatasetItemLink.dataset_item_id)
        .where(
            TaskDatasetItemLink.task_id == Task.id,
            TaskDatasetItemLink.role == "primary_lidar",
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _scene_id_sq() -> ColumnElement[uuid.UUID | None]:
    return (
        select(DatasetItem.scene_id)
        .where(
            DatasetItem.id == func.coalesce(Task.dataset_item_id, _primary_item_id_sq())
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _scene_frame_sq() -> ColumnElement[int | None]:
    return (
        select(DatasetItem.frame_index)
        .where(
            DatasetItem.id == func.coalesce(Task.dataset_item_id, _primary_item_id_sq())
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _scene_name_sq() -> ColumnElement[str | None]:
    scene_id = _scene_id_sq()
    return (
        select(Scene.name)
        .where(Scene.id == scene_id)
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _dataset_id_sq() -> ColumnElement[uuid.UUID | None]:
    return (
        select(DatasetItem.dataset_id)
        .where(
            DatasetItem.id == func.coalesce(Task.dataset_item_id, _primary_item_id_sq())
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _dataset_file_type_sq() -> ColumnElement[str | None]:
    return (
        select(DatasetItem.file_type)
        .where(
            DatasetItem.id == func.coalesce(Task.dataset_item_id, _primary_item_id_sq())
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def _avg_prediction_confidence_sq() -> ColumnElement[float | None]:
    return (
        select(func.avg(Prediction.score))
        .where(Prediction.task_id == Task.id)
        .scalar_subquery()
    )


def _unresolved_issue_count_sq() -> ColumnElement[int]:
    # Owned by ``FeedbackService``'s root relation so Data Manager counts match
    # the Workbench issue totals (root issues only; replies never count).
    return unresolved_issue_count_sq()


def _unresolved_feedback_count_sq() -> ColumnElement[int]:
    """Compatibility alias: the legacy feedback column counts unresolved issues."""
    return unresolved_issue_count_sq()


def _comment_count_sq() -> ColumnElement[int]:
    # Owned by the task discussion read model so counts match the Workbench
    # comment-feed total (annotation comments + native task comments).
    return task_comment_count_sq()


def apply_task_visibility(
    stmt: Select,
    user: User,
    project: Project,
    *,
    project_role: str | None = None,
) -> Select:
    """Apply the canonical project task visibility scope to an arbitrary Task query.

    ``project_role`` is the caller's *resolved* membership role.  Callers under
    the project-scoped model must resolve it (for example through
    ``resolve_project_access``) and pass it; when omitted a non-privileged query
    fails closed.  The B3 export worker must pass the same value as the request
    that enqueued the job.
    """
    if not is_privileged_for_project(user, project):
        stmt = stmt.outerjoin(TaskBatch, Task.batch_id == TaskBatch.id).where(
            task_visibility_clause(user, project_role=project_role)
        )
    return stmt


def visible_tasks_stmt(
    project_id: uuid.UUID,
    *,
    user: User,
    project: Project,
    project_role: str | None = None,
) -> Select:
    """Task-id statement visible to ``user``; pass the resolved ``project_role``.

    Contract published for B3 ``workers/export.py``: resolve project access for
    the initiating actor and pass ``project_role=access.project_role`` so the
    worker applies the same predicate as the API request.
    """
    return apply_task_visibility(
        select(Task.id).where(Task.project_id == project_id),
        user,
        project,
        project_role=project_role,
    )
