"""Task view service and builtin views orchestration (high layer).

Extracted from the legacy ``task_views.py``. The filter/visibility primitives now live
in :mod:`data_management.task_filters` and the pending-count metric expressions in
:mod:`data_management.task_metrics`; this module imports them at the top level (no
function-local imports, no cycle). Builtin view keys live in the primitive schema
module, so direct schema calls do not depend on this module being imported first.
"""

from __future__ import annotations

from app.services.data_management.schema import (
    _track_capable,
    builtin_view_keys,  # noqa: F401
)
from app.services.data_management.filter_tree import (
    iter_filter_rules,
    validate_filter_tree,
)
from app.services.data_management.task_filters import (  # noqa: F401
    _STRING_OPS,
    _NUMERIC_OPS,
    _TASK_FIELD_MAP,
    compile_filter,
    _track_id_expr,
    _compare_scalar,
    _scene_id_sq,
    _scene_frame_sq,
    _scene_name_sq,
    _dataset_id_sq,
    _dataset_file_type_sq,
    _avg_prediction_confidence_sq,
    _unresolved_feedback_count_sq,
    _unresolved_issue_count_sq,
    _comment_count_sq,
    apply_task_visibility,
    visible_tasks_stmt,
)
from app.services.data_management.task_metrics import (  # noqa: F401
    pending_prediction_shapes_expr,
    low_confidence_pending_prediction_shapes_expr,
    pending_tracker_jobs_expr,
)
from app.services.data_management.entity_filters import validate_entity_view  # noqa: F401

import uuid
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Select,
    case,
    func,
    not_,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import DatasetItem
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.project_task_view import ProjectTaskView
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User


DEFAULT_COLUMNS = [
    "display_id",
    "file_name",
    "status",
    "annotation_count",
    "pending_prediction_shape_count",
    "low_confidence_prediction_shape_count",
    "pending_tracker_job_count",
    "unresolved_issue_count",
    "comment_count",
    "annotation_source_counts",
    "track_count",
    "last_activity_at",
]

DEFAULT_VIEWS: list[dict[str, Any]] = [
    {
        "key": "all",
        "name": "全部任务",
        "filter_json": {},
        "sort_json": [{"field": "task.created_at", "direction": "asc"}],
        "columns_json": DEFAULT_COLUMNS,
    },
    {
        "key": "pending",
        "name": "待标注",
        "filter_json": {
            "op": "and",
            "rules": [{"field": "task.status", "op": "in", "value": ["pending"]}],
        },
        "sort_json": [{"field": "task.created_at", "direction": "asc"}],
        "columns_json": DEFAULT_COLUMNS,
    },
    {
        "key": "review",
        "name": "待审核",
        "filter_json": {
            "op": "and",
            "rules": [{"field": "task.status", "op": "in", "value": ["review"]}],
        },
        "sort_json": [{"field": "task.updated_at", "direction": "desc"}],
        "columns_json": DEFAULT_COLUMNS,
    },
    {
        "key": "feedback-open",
        "name": "有未解决问题",
        "filter_json": {
            "op": "and",
            "rules": [{"field": "feedback.unresolved_count", "op": "gt", "value": 0}],
        },
        "sort_json": [{"field": "last_activity_at", "direction": "desc"}],
        "columns_json": DEFAULT_COLUMNS,
    },
    {
        "key": "ai-review",
        "name": "AI 待审",
        "filter_json": {
            "op": "or",
            "rules": [
                {"field": "ai.pending_prediction_shape_count", "op": "gt", "value": 0},
                {"field": "ai.pending_tracker_job_count", "op": "gt", "value": 0},
            ],
        },
        "sort_json": [{"field": "last_activity_at", "direction": "desc"}],
        "columns_json": DEFAULT_COLUMNS,
    },
]


_SORT_FIELD_MAP = {
    **_TASK_FIELD_MAP,
    "task.display_id": Task.display_id,
    "display_id": Task.display_id,
    "file_name": Task.file_name,
    "status": Task.status,
    "annotation_count": Task.total_annotations,
    "prediction_count": Task.total_predictions,
    "task.created_at": Task.created_at,
    "task.updated_at": Task.updated_at,
}


# in 列表元素数上限，防止单请求拖慢 DB。


def builtin_views(
    project_id: uuid.UUID, project: Project | None = None
) -> list[dict[str, Any]]:
    columns = list(DEFAULT_COLUMNS)
    if project is not None and project.scene_mode:
        columns.extend(["scene_name", "frame_index", "scene_total_frames"])
    if project is not None and project.data_type == "video":
        columns.extend(["duration", "frame_count", "keyframe_count"])
    if project is not None and project.data_type == "image":
        columns.append("resolution")
    if project is not None and project.data_type in {
        "lidar",
        "point_cloud",
        "pointcloud",
    }:
        columns.extend(["camera_count", "calibration_issue_count"])
    views = [*DEFAULT_VIEWS]
    if project is not None and project.data_type != "video":
        views = [
            {
                **item,
                "filter_json": (
                    {
                        "op": "and",
                        "rules": [
                            {
                                "field": "ai.pending_prediction_shape_count",
                                "op": "gt",
                                "value": 0,
                            }
                        ],
                    }
                    if item["key"] == "ai-review"
                    else item["filter_json"]
                ),
            }
            for item in views
        ]
    if project is not None:
        required_rules: list[dict[str, Any]] = []
        for unit, binding in (project.tool_bindings or {}).items():
            if not isinstance(binding, dict) or not binding.get("enabled", True):
                continue
            fields = (binding.get("attribute_schema") or {}).get("fields") or []
            for field in fields:
                if (
                    not isinstance(field, dict)
                    or not field.get("required")
                    or not field.get("key")
                ):
                    continue
                object_rules: list[dict[str, Any]] = [
                    {
                        "field": f"annotation.attribute.{unit}.{field['key']}",
                        "op": "missing",
                    }
                ]
                applies_to = field.get("applies_to")
                if isinstance(applies_to, list) and applies_to:
                    object_rules.insert(
                        0,
                        {
                            "field": "annotation.class_name",
                            "op": "in",
                            "value": applies_to,
                        },
                    )
                visible_if = field.get("visible_if")
                if isinstance(visible_if, dict) and visible_if.get("key"):
                    object_rules.insert(
                        0,
                        {
                            "field": f"annotation.attribute.{unit}.{visible_if['key']}",
                            "op": "eq",
                            "value": visible_if.get("equals"),
                        },
                    )
                required_rules.append({"op": "and", "rules": object_rules})
        if required_rules:
            views.append(
                {
                    "key": "missing-required-attributes",
                    "name": "缺少必填属性",
                    "filter_json": {"op": "or", "rules": required_rules},
                    "sort_json": [{"field": "last_activity_at", "direction": "desc"}],
                    "columns_json": DEFAULT_COLUMNS,
                }
            )
        if project.data_type == "video":
            views.append(
                {
                    "key": "tracker-review",
                    "name": "追踪候选待审",
                    "filter_json": {
                        "op": "and",
                        "rules": [
                            {
                                "field": "ai.pending_tracker_job_count",
                                "op": "gt",
                                "value": 0,
                            }
                        ],
                    },
                    "sort_json": [{"field": "last_activity_at", "direction": "desc"}],
                    "columns_json": DEFAULT_COLUMNS,
                }
            )
            if _track_capable(project):
                views.append(
                    {
                        "key": "with-tracks",
                        "name": "含轨迹",
                        "filter_json": {
                            "op": "and",
                            "rules": [
                                {
                                    "field": "annotation.has_track",
                                    "op": "eq",
                                    "value": True,
                                }
                            ],
                        },
                        "sort_json": [
                            {"field": "last_activity_at", "direction": "desc"}
                        ],
                        "columns_json": DEFAULT_COLUMNS,
                    }
                )
        if project.scene_mode:
            views.append(
                {
                    "key": "interpolated",
                    "name": "含插值标注",
                    "filter_json": {
                        "op": "and",
                        "rules": [
                            {
                                "field": "annotation.source",
                                "op": "eq",
                                "value": "interpolated",
                            }
                        ],
                    },
                    "sort_json": [{"field": "scene.frame_index", "direction": "asc"}],
                    "columns_json": DEFAULT_COLUMNS,
                }
            )
    return [
        {
            **item,
            "columns_json": columns,
            "id": None,
            "project_id": project_id,
            "owner_id": None,
            "visibility": "project",
            "builtin": True,
            "created_at": None,
            "updated_at": None,
        }
        for item in views
    ]


def validate_filter(
    filter_json: dict[str, Any] | None,
    project: Project | None = None,
    user: User | None = None,
) -> None:
    tree = {} if filter_json is None else filter_json
    validate_filter_tree(tree)
    compile_filter(tree, project=project, user=user)


def validate_sort(sort_json: list[dict[str, Any]] | None) -> None:
    for item in sort_json or []:
        field = item.get("field")
        if field not in _SORT_FIELD_MAP and field not in {
            "avg_prediction_confidence",
            "unresolved_feedback_count",
            "unresolved_issue_count",
            "comment_count",
            "model_versions",
            "scene_name",
            "scene.frame_index",
            "last_activity_at",
            "low_confidence_prediction_shape_count",
        }:
            raise HTTPException(
                status_code=422, detail=f"Unsupported sort field: {field}"
            )
        if item.get("direction", "asc") not in {"asc", "desc"}:
            raise HTTPException(
                status_code=422, detail="Sort direction must be asc or desc"
            )


def validate_columns(columns_json: list[str] | None) -> None:
    allowed = set(DEFAULT_COLUMNS) | {
        # Legacy feedback column kept as a compatibility alias of the corrected
        # unresolved-issue count for saved views that still reference it.
        "unresolved_feedback_count",
        "assignee",
        "reviewer",
        "batch_id",
        "created_at",
        "updated_at",
        "prediction_count",
        "avg_prediction_confidence",
        "model_versions",
        "scene_name",
        "frame_index",
        "duration",
        "fps",
        "frame_count",
        "resolution",
        "keyframe_count",
        "outside_range_count",
        "camera_count",
        "calibration_issue_count",
        "scene_total_frames",
    }
    unknown = [item for item in columns_json or [] if item not in allowed]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unsupported columns: {unknown}")


def invalid_filter_fields(filter_json: dict[str, Any], project: Project) -> list[str]:
    try:
        validate_filter_tree(filter_json)
    except HTTPException:
        return ["__filter__"]

    invalid: list[str] = []
    for node in iter_filter_rules(filter_json):
        field = node.get("field")
        try:
            compile_filter(node, project=project)
        except HTTPException:
            invalid.append(str(field or "__filter__"))
    return list(dict.fromkeys(invalid))


def _model_versions_sq() -> ColumnElement[list[str]]:
    return (
        select(
            func.coalesce(func.array_agg(func.distinct(Prediction.model_version)), [])
        )
        .where(Prediction.task_id == Task.id, Prediction.model_version.is_not(None))
        .scalar_subquery()
    )


def _last_activity_at_expr() -> ColumnElement[datetime]:
    return func.greatest(
        Task.updated_at,
        func.coalesce(
            (
                select(func.max(Annotation.updated_at))
                .where(Annotation.task_id == Task.id, Annotation.is_active.is_(True))
                .scalar_subquery()
            ),
            Task.updated_at,
        ),
        func.coalesce(
            (
                select(func.max(AnnotationFeedback.updated_at))
                .where(AnnotationFeedback.task_id == Task.id)
                .scalar_subquery()
            ),
            Task.updated_at,
        ),
        func.coalesce(
            (
                select(func.max(Prediction.created_at))
                .where(Prediction.task_id == Task.id)
                .scalar_subquery()
            ),
            Task.updated_at,
        ),
    )


def _annotation_source_count_sq(source: str) -> ColumnElement[int]:
    return (
        select(func.count(Annotation.id))
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.source == source,
        )
        .scalar_subquery()
    )


def _track_count_sq() -> ColumnElement[int]:
    return (
        select(func.count(func.distinct(_track_id_expr())))
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            _track_id_expr().is_not(None),
        )
        .scalar_subquery()
    )


def _annotation_json_array_total_sq(key: str) -> ColumnElement[int]:
    value = Annotation.geometry[key]
    count = case(
        (func.jsonb_typeof(value) == "array", func.jsonb_array_length(value)),
        else_=0,
    )
    return (
        select(func.coalesce(func.sum(count), 0))
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .scalar_subquery()
    )


def _camera_count_sq() -> ColumnElement[int]:
    return (
        select(func.count(TaskDatasetItemLink.id))
        .where(
            TaskDatasetItemLink.task_id == Task.id,
            TaskDatasetItemLink.role.like("camera_%"),
        )
        .scalar_subquery()
    )


def _calibration_issue_count_sq() -> ColumnElement[int]:
    return (
        select(func.count(TaskDatasetItemLink.id))
        .join(DatasetItem, DatasetItem.id == TaskDatasetItemLink.dataset_item_id)
        .where(
            TaskDatasetItemLink.task_id == Task.id,
            TaskDatasetItemLink.role.like("camera_%"),
            not_(DatasetItem.metadata_.has_key("calibration")),  # noqa: W601
        )
        .scalar_subquery()
    )


def _scene_total_frames_sq() -> ColumnElement[int | None]:
    scene_id = _scene_id_sq()
    scene_item = aliased(DatasetItem)
    return (
        select(func.count(func.distinct(scene_item.frame_index)))
        .where(
            scene_item.scene_id == scene_id,
            scene_item.frame_index.is_not(None),
        )
        .correlate(Task)
        .scalar_subquery()
    )


def apply_sort(q: Select, sort_json: list[dict[str, Any]] | None) -> Select:
    validate_sort(sort_json)
    sorts = sort_json or [{"field": "task.created_at", "direction": "asc"}]
    order_by = []
    for item in sorts:
        field = item.get("field")
        expr = _sort_expr(field)
        order_by.append(expr.desc() if item.get("direction") == "desc" else expr.asc())
    order_by.append(Task.id.asc())
    return q.order_by(*order_by)


def _sort_expr(field: str) -> ColumnElement[Any]:
    if field in _SORT_FIELD_MAP:
        return _SORT_FIELD_MAP[field]
    if field == "avg_prediction_confidence":
        return _avg_prediction_confidence_sq()
    if field == "unresolved_feedback_count":
        return _unresolved_feedback_count_sq()
    if field == "unresolved_issue_count":
        return _unresolved_issue_count_sq()
    if field == "comment_count":
        return _comment_count_sq()
    if field in {"scene_name", "scene.scene_name"}:
        return _scene_name_sq()
    if field in {"frame_index", "scene.frame_index"}:
        return _scene_frame_sq()
    if field == "last_activity_at":
        return _last_activity_at_expr()
    if field == "model_versions":
        return _model_versions_sq()
    if field == "low_confidence_prediction_shape_count":
        return low_confidence_pending_prediction_shapes_expr()
    raise HTTPException(status_code=422, detail=f"Unsupported sort field: {field}")


class TaskViewService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_views(
        self,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        entity_scope: str = "tasks",
    ) -> list[ProjectTaskView]:
        rows = await self.db.execute(
            select(ProjectTaskView)
            .where(
                ProjectTaskView.project_id == project_id,
                ProjectTaskView.entity_scope == entity_scope,
                or_(
                    ProjectTaskView.visibility == "project",
                    ProjectTaskView.owner_id == user_id,
                ),
            )
            .order_by(ProjectTaskView.visibility.desc(), ProjectTaskView.name.asc())
        )
        return list(rows.scalars().all())

    async def get_view(
        self,
        project_id: uuid.UUID,
        view_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ProjectTaskView:
        view = await self.db.get(ProjectTaskView, view_id)
        if (
            view is None
            or view.project_id != project_id
            or (view.visibility == "private" and view.owner_id != user_id)
        ):
            raise HTTPException(status_code=404, detail="Task view not found")
        return view

    async def create_view(
        self,
        *,
        project_id: uuid.UUID,
        owner_id: uuid.UUID,
        name: str,
        visibility: str,
        filter_json: dict[str, Any],
        sort_json: list[dict[str, Any]],
        columns_json: list[str],
        entity_scope: str = "tasks",
        project: Project | None = None,
    ) -> ProjectTaskView:
        _validate_payload(
            filter_json,
            sort_json,
            columns_json,
            entity_scope=entity_scope,
            project=project,
        )
        view = ProjectTaskView(
            project_id=project_id,
            owner_id=owner_id,
            name=name,
            visibility=visibility,
            entity_scope=entity_scope,
            filter_json=filter_json,
            sort_json=sort_json,
            columns_json=columns_json,
        )
        self.db.add(view)
        await self.db.flush()
        await self.db.refresh(view)
        return view

    async def update_view(
        self,
        view: ProjectTaskView,
        *,
        name: str | None = None,
        visibility: str | None = None,
        filter_json: dict[str, Any] | None = None,
        sort_json: list[dict[str, Any]] | None = None,
        columns_json: list[str] | None = None,
        project: Project | None = None,
    ) -> ProjectTaskView:
        next_filter = view.filter_json if filter_json is None else filter_json
        next_sort = view.sort_json if sort_json is None else sort_json
        next_columns = view.columns_json if columns_json is None else columns_json
        _validate_payload(
            next_filter,
            next_sort,
            next_columns,
            entity_scope=view.entity_scope,
            project=project,
        )
        if name is not None:
            view.name = name
        if visibility is not None:
            view.visibility = visibility
        if filter_json is not None:
            view.filter_json = filter_json
        if sort_json is not None:
            view.sort_json = sort_json
        if columns_json is not None:
            view.columns_json = columns_json
        await self.db.flush()
        await self.db.refresh(view)
        return view

    async def delete_view(self, view: ProjectTaskView) -> None:
        await self.db.delete(view)
        await self.db.flush()

    def _apply_visibility(
        self,
        stmt: Select,
        user: User,
        project: Project,
        *,
        project_role: str | None = None,
    ) -> Select:
        # 与 tasks.list_tasks 对齐: 非特权用户 (annotator/reviewer/viewer) 通过 Data
        # Manager 查询时只能看自己任务级/批次级可见性范围内的任务, 不能看到全项目任务。
        # 显式分派的 unbatched task 也必须保留在这个授权关系中。
        # ``project_role`` 是调用方解析出的成员职责; 缺失时非特权查询 fail closed。
        return apply_task_visibility(stmt, user, project, project_role=project_role)

    async def count_for_filter(
        self,
        project_id: uuid.UUID,
        filter_json: dict[str, Any],
        *,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> int:
        clause = compile_filter(filter_json, project=project, user=user)
        stmt = (
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == project_id, clause)
        )
        stmt = self._apply_visibility(stmt, user, project, project_role=project_role)
        total = await self.db.scalar(stmt)
        return int(total or 0)

    async def count_for_filters(
        self,
        project_id: uuid.UUID,
        filters: list[dict[str, Any]],
        *,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> list[int]:
        """一次扫描算出多个 filter 的计数 (内置 + 已保存视图)，避免 N+1 往返。"""
        if not filters:
            return []
        cols = [
            func.count()
            .filter(compile_filter(f, project=project, user=user))
            .label(f"c{i}")
            for i, f in enumerate(filters)
        ]
        stmt = select(*cols).select_from(Task).where(Task.project_id == project_id)
        stmt = self._apply_visibility(stmt, user, project, project_role=project_role)
        row = (await self.db.execute(stmt)).one()
        return [int(v or 0) for v in row]

    async def query_tasks(
        self,
        *,
        project_id: uuid.UUID,
        filter_json: dict[str, Any],
        sort_json: list[dict[str, Any]],
        columns_json: list[str],
        limit: int,
        offset: int,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> tuple[list[Any], int]:
        clause = compile_filter(filter_json, project=project, user=user)
        base = Task.project_id == project_id
        count_stmt = self._apply_visibility(
            select(func.count()).select_from(Task).where(base, clause),
            user,
            project,
            project_role=project_role,
        )
        total = await self.db.scalar(count_stmt)
        requested = set(columns_json)
        if not requested:
            # Empty columns is the legacy API shape. Keep legacy projections for
            # older clients while explicit columns opt into selective work.
            requested = set(DEFAULT_COLUMNS) | {
                "avg_prediction_confidence",
                "model_versions",
                "scene_name",
                "frame_index",
            }
        requested.update(item.get("field", "") for item in sort_json or [])
        projection: list[Any] = [
            Task,
            _TASK_FIELD_MAP["task.assignee"].label("effective_assignee_id"),
            _TASK_FIELD_MAP["task.reviewer"].label("effective_reviewer_id"),
        ]
        if "avg_prediction_confidence" in requested:
            projection.append(
                _avg_prediction_confidence_sq().label("avg_prediction_confidence")
            )
        if (
            "unresolved_issue_count" in requested
            or "unresolved_feedback_count" in requested
        ):
            # The legacy feedback column is a compatibility alias of the
            # corrected issue count; serialize both from one projection.
            projection.append(
                _unresolved_issue_count_sq().label("unresolved_issue_count")
            )
        if "comment_count" in requested:
            projection.append(_comment_count_sq().label("comment_count"))
        if "model_versions" in requested:
            projection.append(_model_versions_sq().label("model_versions"))
        if "scene_name" in requested or "scene.scene_name" in requested:
            projection.append(_scene_name_sq().label("scene_name"))
        if "frame_index" in requested or "scene.frame_index" in requested:
            projection.append(_scene_frame_sq().label("frame_index"))
        if "last_activity_at" in requested:
            projection.append(_last_activity_at_expr().label("last_activity_at"))
        if "annotation_source_counts" in requested:
            for source in ("manual", "prediction_based", "ai_tracker", "interpolated"):
                projection.append(
                    _annotation_source_count_sq(source).label(f"source_{source}_count")
                )
        if "track_count" in requested:
            projection.append(_track_count_sq().label("track_count"))
        if "pending_prediction_shape_count" in requested:
            projection.append(
                pending_prediction_shapes_expr().label("pending_prediction_shape_count")
            )
        if "low_confidence_prediction_shape_count" in requested:
            projection.append(
                low_confidence_pending_prediction_shapes_expr().label(
                    "low_confidence_prediction_shape_count"
                )
            )
        if "pending_tracker_job_count" in requested:
            projection.append(
                pending_tracker_jobs_expr(user, project).label(
                    "pending_tracker_job_count"
                )
            )
        if "keyframe_count" in requested:
            projection.append(
                _annotation_json_array_total_sq("keyframes").label("keyframe_count")
            )
        if "outside_range_count" in requested:
            projection.append(
                _annotation_json_array_total_sq("outside").label("outside_range_count")
            )
        if "camera_count" in requested:
            projection.append(_camera_count_sq().label("camera_count"))
        if "calibration_issue_count" in requested:
            projection.append(
                _calibration_issue_count_sq().label("calibration_issue_count")
            )
        if "scene_total_frames" in requested:
            projection.append(_scene_total_frames_sq().label("scene_total_frames"))
        q = select(*projection).where(base, clause)
        q = self._apply_visibility(q, user, project, project_role=project_role)
        q = apply_sort(q, sort_json).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).all()
        return rows, int(total or 0)


def _validate_payload(
    filter_json: dict[str, Any],
    sort_json: list[dict[str, Any]],
    columns_json: list[str],
    *,
    entity_scope: str = "tasks",
    project: Project | None = None,
) -> None:
    if entity_scope != "tasks":
        if project is None:
            raise HTTPException(
                status_code=422,
                detail="Project context is required for entity views",
            )

        validate_entity_view(
            entity_scope=entity_scope,
            filter_json=filter_json,
            sort_json=sort_json,
            columns_json=columns_json,
            project=project,
        )
        return
    validate_filter(filter_json, project=project)
    validate_sort(sort_json)
    validate_columns(columns_json)
