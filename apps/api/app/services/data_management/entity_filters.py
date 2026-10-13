from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.data_management.filter_tree import (
    iter_filter_rules,
    validate_filter_tree,
    validate_in_value,
)
from app.services.data_management.schema import build_data_manager_schema
from app.services.project_kind import project_kind
from app.services.feedback import annotation_unresolved_issue_count_sq
from app.services.data_management.task_filters import (
    _compile_annotation_object_condition,
    _compare_column,
    _is_annotation_object_rule,
    _MAX_IN_VALUES,
    _validate_rule_value,
    compile_filter,
    visible_tasks_stmt,
)


_NUMERIC_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
_COMPACT_TRACK_TYPES = {
    "video_track_bbox",
    "video_track_polygon",
    "video_track_polyline",
    "video_track_mask",
}


def compile_entity_filter(
    filter_json: dict[str, Any],
    annotation,
    *,
    project: Project,
    user: User | None = None,
) -> ColumnElement[bool]:
    """Compile a Data Manager filter at one concrete annotation grain.

    Annotation clauses are bound to ``annotation`` instead of becoming task-level
    EXISTS predicates. This preserves the same-object invariant for object rows and
    for the member that makes a logical track match.
    """
    validate_filter_tree(filter_json)
    return _compile_entity_node(filter_json, annotation, project=project, user=user)


def _compile_entity_node(
    filter_json: dict[str, Any],
    annotation,
    *,
    project: Project,
    user: User | None = None,
) -> ColumnElement[bool]:
    if not filter_json:
        return literal(True)
    if "rules" in filter_json:
        op = filter_json.get("op", "and")
        if op not in {"and", "or"}:
            raise HTTPException(
                status_code=422, detail="Filter group op must be and/or"
            )
        rules = filter_json.get("rules")
        if not isinstance(rules, list):
            raise HTTPException(
                status_code=422, detail="Filter group rules must be a list"
            )
        clauses = [
            _compile_entity_node(child, annotation, project=project, user=user)
            for child in rules
        ]
        if not clauses:
            return literal(True)
        return and_(*clauses) if op == "and" else or_(*clauses)

    field = filter_json.get("field")
    op = filter_json.get("op")
    value = filter_json.get("value")
    if not isinstance(field, str) or not isinstance(op, str):
        raise HTTPException(status_code=422, detail="Filter rule needs field and op")
    _validate_rule_value(field, op, value, project)
    if _is_annotation_object_rule(filter_json):
        return _compile_annotation_object_condition(
            annotation, field, op, value, project
        )
    if field in {"feedback.unresolved_count", "issue.unresolved_count"}:
        # Corrected to the Workbench root-issue definition restricted to the
        # annotation: replies and non-issue feedback never count.
        count = annotation_unresolved_issue_count_sq(annotation)
        return _compare_column(count, op, value, _NUMERIC_OPS)
    if field == "feedback.status":
        if op not in {"eq", "in"}:
            raise HTTPException(
                status_code=422, detail="Unsupported feedback.status op"
            )
        clause = and_(
            AnnotationFeedback.annotation_id == annotation.id,
            AnnotationFeedback.is_active.is_(True),
        )
        if op == "eq":
            return exists().where(clause, AnnotationFeedback.status == value)
        if not isinstance(value, list):
            raise HTTPException(status_code=422, detail="in value must be a list")
        validate_in_value(value, max_values=_MAX_IN_VALUES)
        return exists().where(clause, AnnotationFeedback.status.in_(value))
    return compile_filter(filter_json, project=project, user=user)


def validate_entity_view(
    *,
    entity_scope: str,
    filter_json: dict[str, Any],
    sort_json: list[dict[str, Any]],
    columns_json: list[str],
    project: Project,
) -> None:
    if entity_scope not in {"objects", "tracks"}:
        raise HTTPException(status_code=422, detail="Unsupported entity scope")
    validate_filter_tree(filter_json)
    schema = build_data_manager_schema(project, entity_scope)  # type: ignore[arg-type]
    allowed_fields = {field.key for field in schema.filter_fields}

    for node in iter_filter_rules(filter_json):
        field = node.get("field")
        if field not in allowed_fields:
            raise HTTPException(
                status_code=422, detail=f"Unsupported filter field: {field}"
            )
        compile_entity_filter(node, Annotation, project=project)
    allowed_sorts = {item.value for item in schema.sort_fields}
    for item in sort_json or []:
        if item.get("field") not in allowed_sorts:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported sort field: {item.get('field')}",
            )
        if item.get("direction", "asc") not in {"asc", "desc"}:
            raise HTTPException(
                status_code=422, detail="Sort direction must be asc or desc"
            )
    allowed_columns = {column.key for column in schema.columns}
    unknown_columns = [
        item for item in columns_json or [] if item not in allowed_columns
    ]
    if unknown_columns:
        raise HTTPException(
            status_code=422, detail=f"Unsupported columns: {unknown_columns}"
        )


def invalid_entity_filter_fields(
    filter_json: dict[str, Any], entity_scope: str, project: Project
) -> list[str]:
    try:
        validate_filter_tree(filter_json)
    except HTTPException:
        return ["__filter__"]

    schema = build_data_manager_schema(project, entity_scope)  # type: ignore[arg-type]
    allowed_fields = {field.key for field in schema.filter_fields}
    invalid: list[str] = []
    for node in iter_filter_rules(filter_json):
        field = node.get("field")
        try:
            if field not in allowed_fields:
                raise HTTPException(
                    status_code=422, detail=f"Unsupported filter field: {field}"
                )
            compile_entity_filter(node, Annotation, project=project)
        except HTTPException:
            invalid.append(str(field or "__filter__"))
    return list(dict.fromkeys(invalid))


def builtin_entity_views(project_id, entity_scope: str) -> list[dict[str, Any]]:
    if entity_scope == "objects":
        specs = [
            ("all", "全部对象", {}),
            (
                "manual",
                "人工标注",
                {"field": "annotation.source", "op": "eq", "value": "manual"},
            ),
            (
                "accepted-ai",
                "接受 AI",
                {
                    "field": "annotation.source",
                    "op": "eq",
                    "value": "prediction_based",
                },
            ),
            (
                "feedback-open",
                "有未解决反馈",
                {"field": "feedback.unresolved_count", "op": "gt", "value": 0},
            ),
        ]
        columns = [
            "class_name",
            "source",
            "tool_geometry",
            "track_id",
            "attributes",
            "task_location",
            "feedback",
            "updated_at",
        ]
        sort = [{"field": "annotation.updated_at", "direction": "desc"}]
    elif entity_scope == "tracks":
        specs = [
            ("all", "全部轨迹", {}),
            (
                "manual",
                "人工轨迹",
                {"field": "annotation.source", "op": "eq", "value": "manual"},
            ),
            (
                "ai-tracker",
                "AI 追踪",
                {"field": "annotation.source", "op": "eq", "value": "ai_tracker"},
            ),
            (
                "interpolated",
                "含插值",
                {
                    "field": "annotation.source",
                    "op": "eq",
                    "value": "interpolated",
                },
            ),
        ]
        columns = [
            "track_id",
            "class_name",
            "track_kind",
            "range",
            "coverage",
            "visibility",
            "sources",
            "attributes",
            "quality",
        ]
        sort = [{"field": "track.track_id", "direction": "asc"}]
    else:
        return []
    return [
        {
            "id": None,
            "key": key,
            "project_id": project_id,
            "owner_id": None,
            "name": name,
            "visibility": "project",
            "entity_scope": entity_scope,
            "filter_json": filter_json,
            "sort_json": sort,
            "columns_json": columns,
            "builtin": True,
            "created_at": None,
            "updated_at": None,
        }
        for key, name, filter_json in specs
    ]


async def count_entity_filters(
    db: AsyncSession,
    *,
    project_id,
    entity_scope: str,
    filters: list[dict[str, Any]],
    user: User,
    project: Project,
    project_role: str | None = None,
) -> list[int]:
    if not filters:
        return []
    visible = visible_tasks_stmt(
        project_id, user=user, project=project, project_role=project_role
    ).subquery(f"dm_{entity_scope}_view_visible")
    conditions = [
        compile_entity_filter(item, Annotation, project=project, user=user)
        for item in filters
    ]
    active = and_(
        Annotation.project_id == project_id,
        Annotation.is_active.is_(True),
        Annotation.was_cancelled.is_(False),
    )
    if entity_scope == "objects":
        base = and_(active, Annotation.annotation_type.not_in(_COMPACT_TRACK_TYPES))
        columns = [
            func.count(Annotation.id).filter(condition).label(f"c{index}")
            for index, condition in enumerate(conditions)
        ]
    elif entity_scope == "tracks":
        kind = project_kind(project)
        if kind.data_type == "video" and not kind.scene_mode:
            base = and_(
                active,
                Annotation.track_id.is_not(None),
                Annotation.annotation_type.in_(_COMPACT_TRACK_TYPES),
            )
            columns = [
                func.count(Annotation.id).filter(condition).label(f"c{index}")
                for index, condition in enumerate(conditions)
            ]
        else:
            base = and_(
                active,
                Annotation.track_id.is_not(None),
                Annotation.annotation_type.not_in(_COMPACT_TRACK_TYPES),
            )
            columns = [
                func.count(func.distinct(Annotation.track_id))
                .filter(condition)
                .label(f"c{index}")
                for index, condition in enumerate(conditions)
            ]
    else:
        raise HTTPException(status_code=422, detail="Unsupported entity scope")
    row = (
        await db.execute(
            select(*columns)
            .select_from(Annotation)
            .join(visible, visible.c.id == Annotation.task_id)
            .join(Task, Task.id == Annotation.task_id)
            .where(base)
        )
    ).one()
    return [int(value or 0) for value in row]
