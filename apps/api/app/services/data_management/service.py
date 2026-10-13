"""DataManagerService (high layer).

Extracted from the legacy ``data_manager.py``. The schema builder now lives in
:mod:`data_management.schema` and the metric expressions in
:mod:`data_management.task_metrics`; this module re-imports them so its public surface
is unchanged.
"""

from __future__ import annotations

from app.services.data_management.schema import (  # noqa: F401
    _TEXT_OPS,
    _NUMBER_OPS,
    _EXISTS_OPS,
    _BASE_COLUMNS,
    _option,
    _enabled_bindings,
    _attribute_fields,
    _class_names,
    _track_capable,
    build_data_manager_schema,
)
from app.services.data_management.task_metrics import (  # noqa: F401
    LOW_CONFIDENCE_THRESHOLD,
    _pending_prediction_shape_rows,
    pending_prediction_shapes_expr,
    low_confidence_pending_prediction_shapes_expr,
    pending_tracker_jobs_expr,
)

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    func,
    literal,
    not_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus
from app.schemas.data_manager import (
    DataManagerAiReviewSummary,
    DataManagerAnnotationSummary,
    DataManagerAttributeSummary,
    DataManagerMatchItem,
    DataManagerMatchesResponse,
    DataManagerScopeSummary,
    DataManagerSummaryResponse,
)
from app.services.project_kind import project_kind
from app.services.prediction import to_internal_shape
from app.services.project_access import is_privileged_for_project
from app.services.feedback import unresolved_issue_count_sq
from app.services.data_management.views import (
    compile_filter,
    visible_tasks_stmt,
)
from app.services.data_management.match_evidence import collect_match_evidence


def _json_scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _attribute_eligibility_clause(
    annotation_base,
    unit: str,
    field: dict[str, Any],
):
    clauses = [annotation_base, Annotation.tool_unit_id == unit]
    applies_to = field.get("applies_to")
    if isinstance(applies_to, list) and applies_to:
        clauses.append(Annotation.class_name.in_([str(item) for item in applies_to]))
    visible_if = field.get("visible_if")
    if isinstance(visible_if, dict) and visible_if.get("key"):
        dependency = str(visible_if["key"])
        clauses.extend(
            [
                Annotation.attributes.has_key(dependency),  # noqa: W601
                Annotation.attributes[dependency].astext
                == _json_scalar_text(visible_if.get("equals")),
            ]
        )
    return and_(*clauses)


class DataManagerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def summary(
        self,
        *,
        project_id,
        filter_json: dict[str, Any],
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerSummaryResponse:
        visible_stmt = visible_tasks_stmt(
            project_id, user=user, project=project, project_role=project_role
        )
        matched_stmt = visible_tasks_stmt(
            project_id, user=user, project=project, project_role=project_role
        ).where(compile_filter(filter_json, project=project, user=user))
        visible_ids = visible_stmt.subquery("dm_visible_tasks")
        matched_ids = matched_stmt.subquery("dm_matched_tasks")

        visible_total = int(
            await self.db.scalar(select(func.count()).select_from(visible_ids)) or 0
        )
        matched_total = int(
            await self.db.scalar(select(func.count()).select_from(matched_ids)) or 0
        )

        status_rows = await self.db.execute(
            select(Task.status, func.count(Task.id))
            .join(matched_ids, matched_ids.c.id == Task.id)
            .group_by(Task.status)
        )
        task_status = {str(status): int(count) for status, count in status_rows}

        annotation_base = and_(
            Annotation.task_id.in_(select(matched_ids.c.id)),
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        annotation_counts = (
            await self.db.execute(
                select(
                    func.count(Annotation.id).label("total"),
                    func.count(Annotation.id)
                    .filter(Annotation.track_id.is_not(None))
                    .label("tracked"),
                    func.count(func.distinct(Annotation.track_id))
                    .filter(Annotation.track_id.is_not(None))
                    .label("distinct_tracks"),
                    func.count(Annotation.id)
                    .filter(
                        Annotation.attributes.op("@>")(
                            literal({"_imported": True}, type_=JSONB)
                        )
                    )
                    .label("imported"),
                ).where(annotation_base)
            )
        ).one()
        annotation_total = int(annotation_counts.total or 0)
        tracked = int(annotation_counts.tracked or 0)
        distinct_tracks = int(annotation_counts.distinct_tracks or 0)
        imported = int(annotation_counts.imported or 0)

        dimension_queries = []
        for dimension, column in (
            ("source", Annotation.source),
            ("class", Annotation.class_name),
            ("tool_unit", Annotation.tool_unit_id),
            ("type", Annotation.annotation_type),
        ):
            dimension_queries.append(
                select(
                    literal(dimension).label("dimension"),
                    cast(column, String).label("value"),
                    func.count(Annotation.id).label("count"),
                )
                .where(annotation_base, column.is_not(None))
                .group_by(column)
            )
        distribution_rows = await self.db.execute(union_all(*dimension_queries))
        distributions: dict[str, dict[str, int]] = {
            "source": {},
            "class": {},
            "tool_unit": {},
            "type": {},
        }
        for dimension, value, count in distribution_rows:
            distributions[str(dimension)][str(value)] = int(count)
        by_source = distributions["source"]
        by_class = distributions["class"]
        by_tool_unit = distributions["tool_unit"]
        by_type = distributions["type"]

        pending_shape_rows = _pending_prediction_shape_rows(
            lambda prediction: prediction.task_id.in_(select(matched_ids.c.id)),
            alias_name="dm_summary_pending",
        ).subquery("dm_summary_pending_rows")
        confidence_bucket = case(
            (pending_shape_rows.c.confidence < 0.25, "lt_025"),
            (pending_shape_rows.c.confidence < 0.5, "025_049"),
            (pending_shape_rows.c.confidence < 0.75, "050_074"),
            else_="gte_075",
        )
        pending_review_rows = await self.db.execute(
            select(
                func.coalesce(pending_shape_rows.c.model_version, "未标记").label(
                    "model_version"
                ),
                confidence_bucket.label("bucket"),
                func.count().label("count"),
            )
            .select_from(pending_shape_rows)
            .group_by(pending_shape_rows.c.model_version, confidence_bucket)
        )
        pending_shapes = 0
        low_confidence_shapes = 0
        by_model_version: dict[str, int] = {}
        confidence_buckets: dict[str, int] = {}
        for model_version, bucket, count in pending_review_rows:
            model_key = str(model_version)
            bucket_key = str(bucket)
            value = int(count)
            pending_shapes += value
            by_model_version[model_key] = by_model_version.get(model_key, 0) + value
            confidence_buckets[bucket_key] = (
                confidence_buckets.get(bucket_key, 0) + value
            )
            if bucket_key in {"lt_025", "025_049"}:
                low_confidence_shapes += value
        pending_tracker_jobs = int(
            await self.db.scalar(
                select(
                    func.coalesce(func.sum(pending_tracker_jobs_expr(user, project)), 0)
                )
                .select_from(Task)
                .join(matched_ids, matched_ids.c.id == Task.id)
            )
            or 0
        )
        unresolved_feedback = int(
            await self.db.scalar(
                select(func.sum(unresolved_issue_count_sq()))
                .select_from(Task)
                .join(matched_ids, matched_ids.c.id == Task.id)
            )
            or 0
        )

        attribute_summaries: list[DataManagerAttributeSummary] = []
        # Attribute schemas are project-bounded (normally a handful of fields),
        # so this loop is bounded by configuration rather than task/annotation
        # cardinality. Each query aggregates the whole matched scope in SQL.
        for unit, field in _attribute_fields(project):
            key = str(field["key"])
            eligible_clause = _attribute_eligibility_clause(
                annotation_base, unit, field
            )
            counts = (
                await self.db.execute(
                    select(
                        func.count(Annotation.id).label("eligible"),
                        func.count(Annotation.id)
                        .filter(Annotation.attributes.has_key(key))  # noqa: W601
                        .label("present"),
                    ).where(eligible_clause)
                )
            ).one()
            eligible = int(counts.eligible or 0)
            present = int(counts.present or 0)
            values: dict[str, int] = {}
            if field.get("type") in {"boolean", "select"}:
                value_expr = Annotation.attributes[key].astext
                rows = await self.db.execute(
                    select(
                        value_expr,
                        func.count(Annotation.id),
                    )
                    .where(
                        eligible_clause,
                        Annotation.attributes.has_key(key),  # noqa: W601
                    )
                    .group_by(value_expr)
                    .limit(50)
                )
                values = {str(value): int(count) for value, count in rows}
            attribute_summaries.append(
                DataManagerAttributeSummary(
                    tool_unit_id=unit,
                    key=key,
                    label=str(field.get("label") or key),
                    eligible=eligible,
                    present=present,
                    missing=max(eligible - present, 0),
                    values=values,
                )
            )

        kind = project_kind(project)
        kind_metrics: dict[str, int | float | None] = {}
        if kind.data_type == "image":
            image_rows = (
                await self.db.execute(
                    select(
                        func.count(DatasetItem.id)
                        .filter(
                            DatasetItem.width.is_not(None),
                            DatasetItem.height.is_not(None),
                        )
                        .label("with_dimensions"),
                        func.count(
                            func.distinct(
                                func.concat(
                                    DatasetItem.width,
                                    "x",
                                    DatasetItem.height,
                                )
                            )
                        )
                        .filter(
                            DatasetItem.width.is_not(None),
                            DatasetItem.height.is_not(None),
                        )
                        .label("distinct_resolutions"),
                    )
                    .select_from(Task)
                    .join(matched_ids, matched_ids.c.id == Task.id)
                    .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                )
            ).one()
            kind_metrics = {
                "images_with_dimensions": int(image_rows.with_dimensions or 0),
                "distinct_resolutions": int(image_rows.distinct_resolutions or 0),
            }
        elif kind.data_type == "video":
            video_meta = DatasetItem.metadata_["video"]
            video_rows = (
                await self.db.execute(
                    select(
                        func.coalesce(
                            func.sum(cast(video_meta["duration_ms"].astext, Integer)),
                            0,
                        ).label("duration_ms"),
                        func.coalesce(
                            func.sum(cast(video_meta["frame_count"].astext, Integer)),
                            0,
                        ).label("frame_count"),
                    )
                    .select_from(Task)
                    .join(matched_ids, matched_ids.c.id == Task.id)
                    .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                )
            ).one()
            keyframes = case(
                (
                    func.jsonb_typeof(Annotation.geometry["keyframes"]) == "array",
                    func.jsonb_array_length(Annotation.geometry["keyframes"]),
                ),
                else_=0,
            )
            outside_ranges = case(
                (
                    func.jsonb_typeof(Annotation.geometry["outside"]) == "array",
                    func.jsonb_array_length(Annotation.geometry["outside"]),
                ),
                else_=0,
            )
            video_annotation_rows = (
                await self.db.execute(
                    select(
                        func.coalesce(func.sum(keyframes), 0).label("keyframes"),
                        func.coalesce(func.sum(outside_ranges), 0).label(
                            "outside_ranges"
                        ),
                    ).where(annotation_base)
                )
            ).one()
            kind_metrics = {
                "duration_ms": int(video_rows.duration_ms or 0),
                "frame_count": int(video_rows.frame_count or 0),
                "keyframes": int(video_annotation_rows.keyframes or 0),
                "outside_ranges": int(video_annotation_rows.outside_ranges or 0),
            }
        elif kind.data_type == "lidar":
            camera_clause = and_(
                TaskDatasetItemLink.task_id.in_(select(matched_ids.c.id)),
                TaskDatasetItemLink.role.like("camera_%"),
            )
            camera_links = int(
                await self.db.scalar(
                    select(func.count(TaskDatasetItemLink.id)).where(camera_clause)
                )
                or 0
            )
            calibration_issues = int(
                await self.db.scalar(
                    select(func.count(TaskDatasetItemLink.id))
                    .join(
                        DatasetItem,
                        DatasetItem.id == TaskDatasetItemLink.dataset_item_id,
                    )
                    .where(
                        camera_clause,
                        not_(
                            DatasetItem.metadata_.has_key("calibration")  # noqa: W601
                        ),
                    )
                )
                or 0
            )
            kind_metrics = {
                "box_3d": by_type.get("box_3d", 0),
                "point_mask_3d": by_type.get("point_mask_3d", 0),
                "camera_links": camera_links,
                "calibration_issues": calibration_issues,
            }
        if kind.scene_mode:
            primary_item_id = (
                select(TaskDatasetItemLink.dataset_item_id)
                .where(
                    TaskDatasetItemLink.task_id == Task.id,
                    TaskDatasetItemLink.role == "primary_lidar",
                )
                .limit(1)
                .correlate(Task)
                .scalar_subquery()
            )
            scene_ids = (
                select(DatasetItem.scene_id)
                .select_from(Task)
                .join(matched_ids, matched_ids.c.id == Task.id)
                .join(
                    DatasetItem,
                    DatasetItem.id
                    == func.coalesce(Task.dataset_item_id, primary_item_id),
                )
                .where(DatasetItem.scene_id.is_not(None))
                .subquery()
            )
            kind_metrics.update(
                {
                    "scenes": int(
                        await self.db.scalar(
                            select(func.count(func.distinct(scene_ids.c.scene_id)))
                        )
                        or 0
                    ),
                    "interpolated_annotations": by_source.get("interpolated", 0),
                }
            )

        return DataManagerSummaryResponse(
            scope=DataManagerScopeSummary(
                visible_task_total=visible_total,
                matched_task_total=matched_total,
            ),
            task_status=task_status,
            annotations=DataManagerAnnotationSummary(
                total=annotation_total,
                single_frame=max(annotation_total - tracked, 0),
                tracked=tracked,
                distinct_tracks=distinct_tracks,
                imported=imported,
                by_source=by_source,
                by_class=by_class,
                by_tool_unit=by_tool_unit,
                by_type=by_type,
            ),
            ai_review=DataManagerAiReviewSummary(
                prediction_shapes=pending_shapes,
                low_confidence_prediction_shapes=low_confidence_shapes,
                tracker_jobs=pending_tracker_jobs,
                confidence_threshold=LOW_CONFIDENCE_THRESHOLD,
                by_model_version=by_model_version,
                confidence_buckets=confidence_buckets,
            ),
            unresolved_feedback=unresolved_feedback,
            attributes=attribute_summaries,
            kind_metrics=kind_metrics,
        )

    async def matches(
        self,
        *,
        project_id,
        task_id,
        filter_json: dict[str, Any],
        limit: int,
        offset: int,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerMatchesResponse:
        visible = await self.db.scalar(
            visible_tasks_stmt(
                project_id, user=user, project=project, project_role=project_role
            ).where(Task.id == task_id)
        )
        if visible is None:
            raise HTTPException(status_code=404, detail="Task not found")
        matched = await self.db.scalar(
            visible_tasks_stmt(
                project_id, user=user, project=project, project_role=project_role
            ).where(
                Task.id == task_id,
                compile_filter(filter_json, project=project, user=user),
            )
        )
        if matched is None:
            return DataManagerMatchesResponse(
                task_id=task_id, items=[], total=0, limit=limit, offset=offset
            )

        evidence = await collect_match_evidence(
            self.db,
            task_id=task_id,
            filter_json=filter_json,
            project=project,
            user=user,
        )

        # Matches are drawn from the exact witness sets in fixed source order.  Each
        # source receives the remaining slice of one global offset/limit window, so
        # prediction geometry is converted only after SQL has selected its page.
        items: list[DataManagerMatchItem] = []
        total = 0
        remaining_offset = offset
        remaining_limit = limit
        annotation_where = (
            Annotation.task_id == task_id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        annotation_total = 0
        if evidence.annotation_condition is not None:
            annotation_where += (evidence.annotation_condition,)
            annotation_total = int(
                await self.db.scalar(
                    select(func.count())
                    .select_from(Annotation)
                    .where(*annotation_where)
                )
                or 0
            )
            if remaining_limit > 0 and remaining_offset < annotation_total:
                annotation_rows = await self.db.execute(
                    select(
                        Annotation.id,
                        Annotation.track_id,
                        Annotation.class_name,
                        Annotation.tool_unit_id,
                        Annotation.annotation_type,
                        Annotation.source,
                        Annotation.attributes,
                        cast(Annotation.geometry["frame_index"].astext, Integer).label(
                            "frame_index"
                        ),
                    )
                    .where(*annotation_where)
                    .order_by(Annotation.created_at, Annotation.id)
                    .offset(remaining_offset)
                    .limit(remaining_limit)
                )
                items.extend(
                    DataManagerMatchItem(
                        entity_kind="annotation",
                        id=row.id,
                        track_id=row.track_id,
                        class_name=row.class_name,
                        tool_unit_id=row.tool_unit_id,
                        annotation_type=row.annotation_type,
                        source=row.source,
                        attributes={
                            key: value
                            for key, value in (row.attributes or {}).items()
                            if not str(key).startswith("_")
                        },
                        frame_index=row.frame_index,
                    )
                    for row in annotation_rows
                )
        total += annotation_total
        remaining_offset = max(remaining_offset - annotation_total, 0)
        remaining_limit = limit - len(items)

        prediction_total = 0
        if evidence.prediction_scope is not None:
            pending = _pending_prediction_shape_rows(
                lambda prediction: prediction.task_id == task_id,
                alias_name="dm_match_pending",
            ).subquery("dm_match_pending_rows")
            prediction_where = [pending.c.task_id == task_id]
            if evidence.prediction_scope == "low":
                prediction_where.append(pending.c.confidence < LOW_CONFIDENCE_THRESHOLD)
            prediction_total = int(
                await self.db.scalar(
                    select(func.count()).select_from(pending).where(*prediction_where)
                )
                or 0
            )
            if remaining_limit > 0 and remaining_offset < prediction_total:
                prediction_rows = await self.db.execute(
                    select(pending)
                    .where(*prediction_where)
                    .order_by(
                        pending.c.prediction_created_at,
                        pending.c.prediction_id,
                        pending.c.shape_index,
                    )
                    .offset(remaining_offset)
                    .limit(remaining_limit)
                )
                for row in prediction_rows.mappings():
                    shape = to_internal_shape(row["shape_value"])
                    geometry = shape.get("geometry") or {}
                    items.append(
                        DataManagerMatchItem(
                            entity_kind="prediction_shape",
                            id=row["prediction_id"],
                            shape_index=row["shape_index"],
                            class_name=shape.get("class_name"),
                            tool_unit_id=row["tool_unit_id"],
                            annotation_type=shape.get("type"),
                            source="prediction_candidate",
                            attributes=shape.get("attributes") or {},
                            frame_index=geometry.get("frame_index"),
                        )
                    )
        total += prediction_total
        remaining_offset = max(remaining_offset - prediction_total, 0)
        remaining_limit = limit - len(items)

        tracker_total = 0
        if evidence.tracker_jobs:
            tracker_results = VideoTrackerJob.staged_result["results"]
            tracker_clause = and_(
                VideoTrackerJob.task_id == task_id,
                VideoTrackerJob.status.in_(
                    [
                        VideoTrackerJobStatus.PENDING_REVIEW.value,
                        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
                        VideoTrackerJobStatus.CANCELLED.value,
                    ]
                ),
                VideoTrackerJob.staged_result.is_not(None),
                case(
                    (
                        func.jsonb_typeof(tracker_results) == "array",
                        func.jsonb_array_length(tracker_results),
                    ),
                    else_=0,
                )
                > 0,
            )
            if not is_privileged_for_project(user, project):
                tracker_clause = and_(
                    tracker_clause, VideoTrackerJob.created_by == user.id
                )
            tracker_total = int(
                await self.db.scalar(
                    select(func.count())
                    .select_from(VideoTrackerJob)
                    .where(tracker_clause)
                )
                or 0
            )
            total += tracker_total

            if remaining_limit > 0 and remaining_offset < tracker_total:
                tracker_rows = await self.db.execute(
                    select(VideoTrackerJob)
                    .where(tracker_clause)
                    .order_by(VideoTrackerJob.created_at, VideoTrackerJob.id)
                    .offset(remaining_offset)
                    .limit(remaining_limit)
                )
                items.extend(
                    DataManagerMatchItem(
                        entity_kind="tracker_job",
                        id=job.id,
                        source="ai_tracker_candidate",
                        frame_index=job.from_frame,
                    )
                    for job in tracker_rows.scalars().all()
                )

        return DataManagerMatchesResponse(
            task_id=task_id,
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
