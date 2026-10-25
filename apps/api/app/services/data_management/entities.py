from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Integer, String, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.task import Task
from app.schemas.data_manager import (
    DataManagerEntityLocation,
    DataManagerEntityFacets,
    DataManagerEntityQueryRequest,
    DataManagerObjectDetailResponse,
    DataManagerObjectOut,
    DataManagerObjectQueryResponse,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_read_relations import account_profiles
from app.services.data_management.cursor import (
    decode_cursor,
    encode_cursor,
    keyset_after,
)
from app.services.data_management.entity_filters import compile_entity_filter
from app.services.data_management.task_filters import visible_tasks_stmt
from app.services.feedback import annotation_unresolved_issue_count_sq
from app.services.labeling_core_read_relations import (
    annotation_row,
    task_dataset_item_link_row,
    task_row,
)


COMPACT_TRACK_TYPES = {
    "video_track_bbox",
    "video_track_polygon",
    "video_track_polyline",
    "video_track_mask",
}


def _primary_item_id_sq():
    return (
        select(task_dataset_item_link_row.dataset_item_id)
        .where(
            task_dataset_item_link_row.task_id == task_row.id,
            task_dataset_item_link_row.role == "primary_lidar",
        )
        .limit(1)
        .correlate(Task)
        .scalar_subquery()
    )


def task_dataset_item_id_expr():
    return func.coalesce(task_row.dataset_item_id, _primary_item_id_sq())


def _feedback_count_sq(annotation):
    # Corrected to the Workbench root-issue definition restricted to the
    # annotation: replies and non-issue feedback never count.
    return annotation_unresolved_issue_count_sq(annotation)


def _clean_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in (attributes or {}).items()
        if not str(key).startswith("_")
    }


def _attribute_origins(
    attributes: dict[str, Any] | None, meta: dict[str, Any] | None
) -> dict[str, str]:
    origins: dict[str, str] = {}
    for key in _clean_attributes(attributes):
        raw = (meta or {}).get(key)
        origin = raw.get("origin") if isinstance(raw, dict) else None
        origins[key] = "ai" if origin == "ai" else "human"
    return origins


def _location_from_row(row, project: Project, *, focus_kind: str, annotation_id=None):
    return DataManagerEntityLocation(
        project_id=project.id,
        task_id=row.task_id,
        task_display_id=row.task_display_id,
        batch_id=row.batch_id,
        dataset_item_id=row.dataset_item_id,
        data_type=project.data_type,
        focus_kind=focus_kind,
        annotation_id=annotation_id,
        track_id=row.track_id,
        scene_id=row.scene_id,
        scene_name=row.scene_name,
        scene_frame_index=row.scene_frame_index,
        video_frame_index=row.video_frame_index,
    )


def object_from_row(row, project: Project) -> DataManagerObjectOut:
    attributes = _clean_attributes(row.attributes)
    return DataManagerObjectOut(
        entity_key=f"object:{row.annotation_id}",
        annotation_id=row.annotation_id,
        task_id=row.task_id,
        task_display_id=row.task_display_id,
        file_name=row.file_name,
        batch_id=row.batch_id,
        class_name=row.class_name,
        tool_unit_id=row.tool_unit_id,
        annotation_type=row.annotation_type,
        source=row.source,
        imported=bool((row.attributes or {}).get("_imported", False)),
        confidence=row.confidence,
        track_id=row.track_id,
        parent_prediction_id=row.parent_prediction_id,
        parent_annotation_id=row.parent_annotation_id,
        attributes=attributes,
        attribute_origins=_attribute_origins(row.attributes, row.attributes_meta),
        created_by_id=row.created_by_id,
        created_by_name=row.created_by_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        unresolved_feedback_count=int(row.unresolved_feedback_count or 0),
        location=_location_from_row(
            row, project, focus_kind="annotation", annotation_id=row.annotation_id
        ),
    )


def _object_sort(field: str):
    mapping = {
        "annotation.id": annotation_row.id,
        "annotation.updated_at": func.coalesce(
            annotation_row.updated_at, literal(datetime(1970, 1, 1))
        ),
        "annotation.created_at": func.coalesce(
            annotation_row.created_at, literal(datetime(1970, 1, 1))
        ),
        "annotation.class_name": annotation_row.class_name,
        "annotation.source": annotation_row.source,
        "annotation.track_id": func.coalesce(annotation_row.track_id, ""),
        "annotation.confidence": func.coalesce(annotation_row.confidence, -1.0),
        "task.display_id": task_row.display_id,
    }
    if field not in mapping:
        raise HTTPException(status_code=422, detail=f"Unsupported object sort: {field}")
    return mapping[field]


def _parse_cursor_value(field: str, value: Any) -> Any:
    if field in {"annotation.created_at", "annotation.updated_at"}:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid Data Manager cursor"
            ) from exc
    return value


def _object_projection(created_by, item, scene):
    video_frame = cast(annotation_row.geometry["frame_index"].astext, Integer)
    return [
        annotation_row.id.label("annotation_id"),
        annotation_row.task_id.label("task_id"),
        task_row.display_id.label("task_display_id"),
        task_row.file_name.label("file_name"),
        task_row.batch_id.label("batch_id"),
        annotation_row.class_name,
        annotation_row.tool_unit_id,
        annotation_row.annotation_type,
        annotation_row.source,
        annotation_row.confidence,
        annotation_row.track_id,
        annotation_row.parent_prediction_id,
        annotation_row.parent_annotation_id,
        annotation_row.attributes,
        annotation_row.attributes_meta,
        annotation_row.user_id.label("created_by_id"),
        created_by.c.name.label("created_by_name"),
        annotation_row.created_at,
        annotation_row.updated_at,
        _feedback_count_sq(Annotation).label("unresolved_feedback_count"),
        item.id.label("dataset_item_id"),
        item.scene_id.label("scene_id"),
        scene.name.label("scene_name"),
        item.frame_index.label("scene_frame_index"),
        video_frame.label("video_frame_index"),
    ]


class DataManagerObjectService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _base(
        self,
        project_id: uuid.UUID,
        user: IdentityActorFacts,
        project: Project,
        *,
        project_role: str | None = None,
    ):
        visible = visible_tasks_stmt(
            project_id, user=user, project=project, project_role=project_role
        ).subquery("dm_object_visible_tasks")
        return (
            select(annotation_row.id)
            .select_from(annotation_row)
            .join(visible, visible.c.id == annotation_row.task_id)
            .join(task_row, task_row.id == annotation_row.task_id)
            .where(
                annotation_row.project_id == project_id,
                annotation_row.is_active.is_(True),
                annotation_row.was_cancelled.is_(False),
                annotation_row.annotation_type.not_in(COMPACT_TRACK_TYPES),
            )
        )

    async def query(
        self,
        *,
        project_id: uuid.UUID,
        payload: DataManagerEntityQueryRequest,
        user: IdentityActorFacts,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerObjectQueryResponse:
        sort = (
            payload.sort_json
            or [{"field": "annotation.updated_at", "direction": "desc"}]
        )[0]
        field = str(sort.get("field") or "annotation.updated_at")
        direction = str(sort.get("direction") or "desc")
        if direction not in {"asc", "desc"}:
            raise HTTPException(
                status_code=422, detail="Sort direction must be asc or desc"
            )
        sort_expr = _object_sort(field)
        condition = compile_entity_filter(
            payload.filter_json, Annotation, project=project, user=user
        )
        base = self._base(project_id, user, project, project_role=project_role).where(
            condition
        )
        total = int(
            await self.db.scalar(select(func.count()).select_from(base.subquery())) or 0
        )
        matches_for_facets = base.subquery("dm_object_facet_matches")
        task_total = int(
            await self.db.scalar(
                select(func.count(func.distinct(annotation_row.task_id)))
                .select_from(annotation_row)
                .join(matches_for_facets, matches_for_facets.c.id == annotation_row.id)
            )
            or 0
        )
        facet_queries = []
        for dimension, column in (
            ("class", annotation_row.class_name),
            ("source", annotation_row.source),
            ("tool_unit", annotation_row.tool_unit_id),
            ("type", annotation_row.annotation_type),
        ):
            facet_queries.append(
                select(
                    literal(dimension).label("dimension"),
                    cast(column, String).label("value"),
                    func.count(annotation_row.id).label("count"),
                )
                .select_from(annotation_row)
                .join(matches_for_facets, matches_for_facets.c.id == annotation_row.id)
                .group_by(column)
            )
        facet_rows = await self.db.execute(union_all(*facet_queries))
        distributions: dict[str, dict[str, int]] = defaultdict(dict)
        for dimension, value, count in facet_rows:
            distributions[str(dimension)][str(value)] = int(count)

        created_by = account_profiles.alias("created_by_account_profile")
        item = aliased(DatasetItem)
        scene = aliased(Scene)
        matches = base.subquery("dm_object_matches")
        q = (
            select(
                *_object_projection(created_by, item, scene),
                sort_expr.label("_sort_value"),
            )
            .select_from(annotation_row)
            .join(matches, matches.c.id == annotation_row.id)
            .join(task_row, task_row.id == annotation_row.task_id)
            .outerjoin(created_by, created_by.c.id == annotation_row.user_id)
            .outerjoin(item, item.id == task_dataset_item_id_expr())
            .outerjoin(scene, scene.id == item.scene_id)
        )
        if payload.cursor:
            value, tie = decode_cursor(payload.cursor, field=field, direction=direction)
            try:
                tie_id = uuid.UUID(tie)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid Data Manager cursor"
                ) from exc
            q = q.where(
                keyset_after(
                    sort_expr,
                    annotation_row.id,
                    direction=direction,
                    value=_parse_cursor_value(field, value),
                    tie=tie_id,
                )
            )
        order = sort_expr.desc() if direction == "desc" else sort_expr.asc()
        rows = (
            await self.db.execute(
                q.order_by(order, annotation_row.id.asc()).limit(payload.limit + 1)
            )
        ).all()
        has_more = len(rows) > payload.limit
        page_rows = rows[: payload.limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_cursor(
                field=field,
                direction=direction,
                value=last._sort_value,
                tie=str(last.annotation_id),
            )
        return DataManagerObjectQueryResponse(
            items=[object_from_row(row, project) for row in page_rows],
            total=total,
            limit=payload.limit,
            next_cursor=next_cursor,
            facets=DataManagerEntityFacets(
                matched_total=total,
                task_total=task_total,
                by_class=distributions["class"],
                by_source=distributions["source"],
                by_tool_unit=distributions["tool_unit"],
                by_type=distributions["type"],
            ),
        )

    async def detail(
        self,
        *,
        project_id: uuid.UUID,
        annotation_id: uuid.UUID,
        user: IdentityActorFacts,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerObjectDetailResponse:
        created_by = account_profiles.alias("created_by_account_profile")
        item = aliased(DatasetItem)
        scene = aliased(Scene)
        base = self._base(project_id, user, project, project_role=project_role).where(
            annotation_row.id == annotation_id
        )
        match = base.subquery("dm_object_detail")
        row = (
            await self.db.execute(
                select(*_object_projection(created_by, item, scene))
                .select_from(annotation_row)
                .join(match, match.c.id == annotation_row.id)
                .join(task_row, task_row.id == annotation_row.task_id)
                .outerjoin(created_by, created_by.c.id == annotation_row.user_id)
                .outerjoin(item, item.id == task_dataset_item_id_expr())
                .outerjoin(scene, scene.id == item.scene_id)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Object not found")
        return DataManagerObjectDetailResponse(item=object_from_row(row, project))

    async def location(self, **kwargs) -> DataManagerEntityLocation:
        detail = await self.detail(**kwargs)
        return detail.item.location
