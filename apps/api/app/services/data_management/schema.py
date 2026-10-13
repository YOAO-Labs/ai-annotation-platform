"""Pure schema builder for the Data Manager (primitive layer).

Extracted from the legacy ``data_manager.py``. ``build_data_manager_schema`` and its
helpers depend only on DB models, schemas and config — no views, service or filters —
so ``entity_filters`` can import it without forming a cycle. Builtin task-view keys
are derived directly from project configuration, independent of module import order.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.db.models.project import Project
from app.schemas.data_manager import (
    DataManagerColumnOut,
    DataManagerEntityScope,
    DataManagerFilterFieldOut,
    DataManagerMetricOut,
    DataManagerOptionOut,
    DataManagerProjectKindOut,
    DataManagerSchemaResponse,
    DataManagerToolUnitOut,
)
from app.services.project_kind import project_kind


_TEXT_OPS = ["eq", "ne", "in"]


_NUMBER_OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "in"]


_EXISTS_OPS = ["exists", "eq", "in"]


_ANNOTATION_OPS = ["exists", "eq", "ne", "in"]


_BASE_TASK_VIEW_KEYS = ["all", "pending", "review", "feedback-open", "ai-review"]


_BASE_COLUMNS = [
    ("display_id", "任务", "任务", True, False),
    ("file_name", "文件名", "任务", True, False),
    ("status", "状态", "工作流", True, False),
    ("annotation_count", "标注", "标注", True, False),
    ("pending_prediction_shape_count", "AI 检测待审", "AI 待审", True, True),
    (
        "low_confidence_prediction_shape_count",
        "低置信 AI 待审 (<50%)",
        "AI 待审",
        True,
        True,
    ),
    ("pending_tracker_job_count", "AI 追踪待审", "AI 待审", True, True),
    ("unresolved_issue_count", "未解决问题", "质量", True, True),
    ("comment_count", "评论", "讨论", True, True),
    # Legacy feedback column kept as a compatibility alias of the corrected
    # unresolved-issue count for saved views that still reference it.
    ("unresolved_feedback_count", "反馈（兼容）", "质量", False, True),
    ("annotation_source_counts", "来源", "标注", True, True),
    ("track_count", "轨迹", "标注", True, True),
    ("last_activity_at", "最近活动", "任务", True, True),
    ("assignee", "标注员", "人员", False, False),
    ("reviewer", "审核员", "人员", False, False),
    ("batch_id", "批次", "工作流", False, False),
]


def _option(value: str, label: str | None = None) -> DataManagerOptionOut:
    return DataManagerOptionOut(value=value, label=label or value)


def _enabled_bindings(project: Project) -> list[tuple[str, dict[str, Any]]]:
    bindings = project.tool_bindings or {}
    return [
        (unit, binding)
        for unit, binding in bindings.items()
        if isinstance(binding, dict) and binding.get("enabled", True)
    ]


def _attribute_fields(project: Project) -> list[tuple[str, dict[str, Any]]]:
    fields: list[tuple[str, dict[str, Any]]] = []
    for unit, binding in _enabled_bindings(project):
        schema = binding.get("attribute_schema") or {}
        for field in schema.get("fields") or []:
            if isinstance(field, dict) and field.get("key"):
                fields.append((unit, field))
    return fields


def _class_names(binding: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in binding.get("classes") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _track_capable(project: Project) -> bool:
    kind = project_kind(project)
    if kind.scene_mode:
        return True
    if kind.data_type != "video":
        return False
    return any(
        not isinstance(binding.get("video_modes"), dict)
        or binding["video_modes"].get("track", True)
        for _, binding in _enabled_bindings(project)
    )


def builtin_view_keys(project: Project) -> list[str]:
    """Return builtin task-view keys without importing the high-level views module."""
    keys = list(_BASE_TASK_VIEW_KEYS)
    has_required_attributes = any(
        isinstance(field, dict) and field.get("required") and field.get("key")
        for _, binding in _enabled_bindings(project)
        for field in ((binding.get("attribute_schema") or {}).get("fields") or [])
    )
    if has_required_attributes:
        keys.append("missing-required-attributes")
    if project.data_type == "video":
        keys.append("tracker-review")
        if _track_capable(project):
            keys.append("with-tracks")
    if project.scene_mode:
        keys.append("interpolated")
    return keys


def build_data_manager_schema(
    project: Project, entity_scope: DataManagerEntityScope = "tasks"
) -> DataManagerSchemaResponse:
    kind = project_kind(project)
    bindings = _enabled_bindings(project)
    class_values = sorted(
        {class_name for _, binding in bindings for class_name in _class_names(binding)}
    )
    unit_values = [_option(unit) for unit, _ in bindings]

    fields = [
        DataManagerFilterFieldOut(
            key="task.keyword",
            label="任务或文件",
            group="任务",
            value_type="text",
            operators=["contains"],
        ),
        DataManagerFilterFieldOut(
            key="task.status",
            label="任务状态",
            group="工作流",
            value_type="select",
            operators=_TEXT_OPS,
            options=[
                _option("pending", "待标注"),
                _option("in_progress", "标注中"),
                _option("review", "待审核"),
                _option("rejected", "已退回"),
                _option("completed", "已完成"),
            ],
        ),
        DataManagerFilterFieldOut(
            key="task.assignee",
            label="标注员",
            group="人员",
            value_type="text",
            operators=_TEXT_OPS,
        ),
        DataManagerFilterFieldOut(
            key="task.reviewer",
            label="审核员",
            group="人员",
            value_type="text",
            operators=_TEXT_OPS,
        ),
        DataManagerFilterFieldOut(
            key="task.batch_id",
            label="批次",
            group="工作流",
            value_type="text",
            operators=_TEXT_OPS,
        ),
        DataManagerFilterFieldOut(
            key="annotation.annotation_count",
            label="标注数",
            group="标注",
            value_type="number",
            operators=_NUMBER_OPS,
        ),
        DataManagerFilterFieldOut(
            key="annotation.source",
            label="标注来源",
            group="标注",
            value_type="select",
            operators=_ANNOTATION_OPS,
            options=[
                _option("manual", "人工"),
                _option("prediction_based", "接受 AI"),
                _option("ai_tracker", "AI 追踪"),
                _option("interpolated", "插值"),
            ],
        ),
        DataManagerFilterFieldOut(
            key="annotation.imported",
            label="导入标注",
            group="标注",
            value_type="boolean",
            operators=["eq"],
        ),
        DataManagerFilterFieldOut(
            key="annotation.tool_unit_id",
            label="工具单位",
            group="标注",
            value_type="select",
            operators=_ANNOTATION_OPS,
            options=unit_values,
        ),
        DataManagerFilterFieldOut(
            key="annotation.annotation_type",
            label="几何类型",
            group="标注",
            value_type="text",
            operators=_ANNOTATION_OPS,
        ),
        DataManagerFilterFieldOut(
            key="annotation.class_name",
            label="标注类别",
            group="标注",
            value_type="select",
            operators=_EXISTS_OPS,
            options=[_option(value) for value in class_values],
        ),
        DataManagerFilterFieldOut(
            key="ai.pending_prediction_shape_count",
            label="AI 检测候选待审",
            group="AI 待审",
            value_type="number",
            operators=_NUMBER_OPS,
            expensive=True,
        ),
        DataManagerFilterFieldOut(
            key="ai.low_confidence_prediction_shape_count",
            label="低置信 AI 候选待审 (<50%)",
            group="AI 待审",
            value_type="number",
            operators=_NUMBER_OPS,
            expensive=True,
        ),
        DataManagerFilterFieldOut(
            key="prediction.model_version",
            label="历史预测模型版本",
            group="AI 追溯",
            value_type="text",
            operators=_TEXT_OPS,
            expensive=True,
        ),
        DataManagerFilterFieldOut(
            key="ai.pending_tracker_job_count",
            label="AI 追踪结果待审",
            group="AI 待审",
            value_type="number",
            operators=_NUMBER_OPS,
            expensive=True,
        ),
        DataManagerFilterFieldOut(
            key="issue.unresolved_count",
            label="未解决问题",
            group="质量",
            value_type="number",
            operators=_NUMBER_OPS,
        ),
        DataManagerFilterFieldOut(
            key="discussion.comment_count",
            label="评论",
            group="讨论",
            value_type="number",
            operators=_NUMBER_OPS,
        ),
        DataManagerFilterFieldOut(
            key="feedback.unresolved_count",
            label="未解决反馈（兼容）",
            group="质量",
            value_type="number",
            operators=_NUMBER_OPS,
        ),
        DataManagerFilterFieldOut(
            key="feedback.status",
            label="反馈状态",
            group="质量",
            value_type="select",
            operators=["eq", "in"],
            options=[_option("open", "未解决"), _option("resolved", "已解决")],
        ),
    ]

    if kind.data_type != "video":
        fields = [
            field for field in fields if field.key != "ai.pending_tracker_job_count"
        ]

    if _track_capable(project):
        fields.extend(
            [
                DataManagerFilterFieldOut(
                    key="annotation.has_track",
                    label="含轨迹",
                    group="轨迹",
                    value_type="boolean",
                    operators=["eq"],
                ),
                DataManagerFilterFieldOut(
                    key="annotation.track_id",
                    label="轨迹 ID",
                    group="轨迹",
                    value_type="text",
                    operators=["eq", "in"],
                ),
            ]
        )
    if kind.data_type == "video" and _track_capable(project):
        fields.append(
            DataManagerFilterFieldOut(
                key="keyframe.source",
                label="关键帧来源",
                group="视频",
                value_type="select",
                operators=["eq", "in"],
                options=[
                    _option("manual", "人工"),
                    _option("prediction", "预测"),
                    _option("interpolated", "插值"),
                ],
            )
        )

    if kind.scene_mode:
        fields.extend(
            [
                DataManagerFilterFieldOut(
                    key="scene.scene_name",
                    label="Scene",
                    group="Scene",
                    value_type="text",
                    operators=_TEXT_OPS,
                ),
                DataManagerFilterFieldOut(
                    key="scene.frame_index",
                    label="帧序号",
                    group="Scene",
                    value_type="number",
                    operators=_NUMBER_OPS,
                ),
            ]
        )

    for unit, field in _attribute_fields(project):
        attr_type = str(field.get("type") or "text")
        if attr_type in {"number", "range"}:
            value_type = "number"
            operators = [
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
        elif attr_type == "boolean":
            value_type = "boolean"
            operators = ["eq", "exists", "missing"]
        elif attr_type == "select":
            value_type = "select"
            operators = ["eq", "in", "exists", "missing"]
        elif attr_type == "multiselect":
            value_type = "multiselect"
            operators = ["contains_any", "contains_all", "exists", "missing"]
        else:
            value_type = "text"
            operators = ["eq", "contains", "exists", "missing"]
        options = [
            _option(str(item.get("value")), str(item.get("label") or item.get("value")))
            for item in field.get("options") or []
            if isinstance(item, dict) and item.get("value") is not None
        ]
        fields.append(
            DataManagerFilterFieldOut(
                key=f"annotation.attribute.{unit}.{field['key']}",
                label=str(field.get("label") or field["key"]),
                group=f"属性 · {unit}",
                value_type=value_type,  # type: ignore[arg-type]
                operators=operators,
                options=options,
                expensive=True,
                tool_unit_id=unit,
                attribute_key=str(field["key"]),
            )
        )
        fields.append(
            DataManagerFilterFieldOut(
                key=f"annotation.attribute_origin.{unit}.{field['key']}",
                label=f"{field.get('label') or field['key']}来源",
                group=f"属性来源 · {unit}",
                value_type="select",
                operators=["eq", "in"],
                options=[_option("human", "人工"), _option("ai", "AI")],
                expensive=True,
                tool_unit_id=unit,
                attribute_key=str(field["key"]),
            )
        )

    columns = [
        DataManagerColumnOut(
            key=key,
            label=label,
            group=group,
            default=default,
            expensive=expensive,
        )
        for key, label, group, default, expensive in _BASE_COLUMNS
        if (key != "pending_tracker_job_count" or kind.data_type == "video")
        and (key != "track_count" or _track_capable(project))
    ]
    if kind.data_type == "image":
        columns.append(
            DataManagerColumnOut(
                key="resolution", label="分辨率", group="图像", default=True
            )
        )
    if kind.scene_mode:
        columns.extend(
            [
                DataManagerColumnOut(
                    key="scene_name", label="Scene", group="Scene", default=True
                ),
                DataManagerColumnOut(
                    key="frame_index", label="帧", group="Scene", default=True
                ),
            ]
        )
    if kind.data_type == "video":
        columns.extend(
            [
                DataManagerColumnOut(
                    key="duration", label="时长", group="视频", default=True
                ),
                DataManagerColumnOut(key="fps", label="FPS", group="视频"),
                DataManagerColumnOut(
                    key="frame_count", label="总帧数", group="视频", default=True
                ),
                DataManagerColumnOut(
                    key="keyframe_count",
                    label="关键帧",
                    group="视频",
                    default=_track_capable(project),
                    expensive=True,
                ),
                DataManagerColumnOut(
                    key="outside_range_count",
                    label="不可见区间",
                    group="视频",
                    expensive=True,
                ),
            ]
        )
    if kind.data_type == "lidar":
        columns.extend(
            [
                DataManagerColumnOut(
                    key="camera_count", label="相机路数", group="点云", default=True
                ),
                DataManagerColumnOut(
                    key="calibration_issue_count",
                    label="标定异常",
                    group="点云",
                    default=True,
                    expensive=True,
                ),
            ]
        )
    if kind.scene_mode:
        columns.append(
            DataManagerColumnOut(
                key="scene_total_frames",
                label="Scene 总帧",
                group="Scene",
                default=True,
            )
        )

    available_scopes: list[DataManagerEntityScope] = ["tasks", "objects"]
    if _track_capable(project):
        available_scopes.append("tracks")
    if entity_scope not in available_scopes:
        raise HTTPException(
            status_code=422,
            detail=f"Data Manager scope is not supported: {entity_scope}",
        )

    sort_fields = [
        _option("task.created_at", "创建时间"),
        _option("task.updated_at", "更新时间"),
        _option("task.display_id", "任务编号"),
        _option("annotation_count", "标注数"),
        _option(
            "low_confidence_prediction_shape_count",
            "低置信 AI 待审 (<50%)",
        ),
        _option("unresolved_feedback_count", "未解决问题"),
        _option("unresolved_issue_count", "未解决问题"),
        _option("comment_count", "评论"),
        _option("last_activity_at", "最近活动"),
    ]
    metrics = [
        DataManagerMetricOut(key="tasks", label="任务", group="任务"),
        DataManagerMetricOut(key="annotations", label="标注对象", group="标注"),
        DataManagerMetricOut(key="ai_review", label="AI 待审", group="AI 待审"),
        *(
            [DataManagerMetricOut(key="tracks", label="轨迹", group="轨迹")]
            if _track_capable(project)
            else []
        ),
        DataManagerMetricOut(key="feedback", label="未解决问题", group="质量"),
    ]
    builtin_views = builtin_view_keys(project)

    if entity_scope in {"objects", "tracks"}:
        fields = [
            field.model_copy(update={"operators": _ANNOTATION_OPS})
            if field.key == "annotation.class_name"
            else field
            for field in fields
        ]
        excluded = {
            "annotation.annotation_count",
            "ai.pending_prediction_shape_count",
            "ai.low_confidence_prediction_shape_count",
            "ai.pending_tracker_job_count",
        }
        fields = [field for field in fields if field.key not in excluded]
        if entity_scope == "objects":
            object_columns = [
                ("class_name", "类别", "对象", True, "annotation.class_name"),
                ("source", "来源", "对象", True, "annotation.source"),
                (
                    "tool_geometry",
                    "工具 / 几何",
                    "对象",
                    True,
                    "annotation.annotation_type",
                ),
                ("track_id", "轨迹 ID", "轨迹", True, "annotation.track_id"),
                ("attributes", "属性", "属性", True, None),
                ("task_location", "任务 / 帧", "定位", True, "task.display_id"),
                ("feedback", "未解决问题", "质量", True, "feedback.unresolved_count"),
                ("updated_at", "更新时间", "对象", True, "annotation.updated_at"),
                ("confidence", "置信度", "对象", False, "annotation.confidence"),
                ("created_by", "创建者", "人员", False, None),
                ("annotation_id", "标注 ID", "对象", False, "annotation.id"),
            ]
            columns = [
                DataManagerColumnOut(
                    key=key,
                    label=label,
                    group=group,
                    default=default,
                    expensive=key in {"attributes", "feedback"},
                    sortable=sort_field is not None,
                    sort_field=sort_field,
                )
                for key, label, group, default, sort_field in object_columns
            ]
            sort_fields = [
                _option("annotation.updated_at", "更新时间"),
                _option("annotation.created_at", "创建时间"),
                _option("annotation.class_name", "类别"),
                _option("annotation.source", "来源"),
                _option("annotation.track_id", "轨迹 ID"),
                _option("annotation.confidence", "置信度"),
                _option("task.display_id", "任务编号"),
            ]
            metrics = [
                DataManagerMetricOut(key="objects", label="对象", group="对象"),
                DataManagerMetricOut(key="tasks", label="涉及任务", group="任务"),
                DataManagerMetricOut(key="feedback", label="未解决反馈", group="质量"),
            ]
            builtin_views = ["all", "manual", "accepted-ai", "feedback-open"]
        else:
            track_columns = [
                ("track_id", "轨迹 ID", "轨迹", True, "track.track_id"),
                ("class_name", "类别", "轨迹", True, "track.class_name"),
                ("track_kind", "类型", "轨迹", True, "track.track_kind"),
                ("range", "范围", "时空", True, "track.start_frame"),
                ("coverage", "实例 / 关键帧", "时空", True, "track.occurrence_count"),
                (
                    "visibility",
                    "不可见 / 遮挡",
                    "质量",
                    True,
                    "track.outside_range_count",
                ),
                ("sources", "来源", "来源", True, None),
                ("attributes", "属性", "属性", True, None),
                ("quality", "质量异常", "质量", True, "track.quality_issue_count"),
            ]
            columns = [
                DataManagerColumnOut(
                    key=key,
                    label=label,
                    group=group,
                    default=default,
                    expensive=key in {"visibility", "attributes", "quality"},
                    sortable=sort_field is not None,
                    sort_field=sort_field,
                )
                for key, label, group, default, sort_field in track_columns
            ]
            sort_fields = [
                _option("track.track_id", "轨迹 ID"),
                _option("track.class_name", "类别"),
                _option("track.track_kind", "类型"),
                _option("track.start_frame", "起始帧"),
                _option("track.occurrence_count", "实例 / 关键帧数"),
                _option("track.quality_issue_count", "质量异常"),
            ]
            metrics = [
                DataManagerMetricOut(key="tracks", label="轨迹", group="轨迹"),
                DataManagerMetricOut(key="objects", label="可见实例", group="对象"),
                DataManagerMetricOut(key="quality", label="质量异常", group="质量"),
            ]
            builtin_views = ["all", "manual", "ai-tracker", "interpolated"]

    return DataManagerSchemaResponse(
        entity_scope=entity_scope,
        available_entity_scopes=available_scopes,
        project_kind=DataManagerProjectKindOut(
            data_type=kind.data_type,
            type_key=project.type_key,
            scene_mode=kind.scene_mode,
        ),
        tool_units=[
            DataManagerToolUnitOut(
                id=unit,
                classes=_class_names(binding),
            )
            for unit, binding in bindings
        ],
        filter_fields=fields,
        columns=columns,
        default_columns=[column.key for column in columns if column.default],
        sort_fields=sort_fields,
        metrics=metrics,
        builtin_views=builtin_views,
    )
