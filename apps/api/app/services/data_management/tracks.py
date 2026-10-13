from __future__ import annotations

import base64
import json
import uuid
from collections import Counter, defaultdict
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Integer, String, and_, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.data_manager import (
    DataManagerEntityLocation,
    DataManagerEntityFacets,
    DataManagerEntityQueryRequest,
    DataManagerTrackDetailResponse,
    DataManagerTrackMemberOut,
    DataManagerTrackOut,
    DataManagerTrackQueryResponse,
    DataManagerTrackSourceSummary,
)
from app.services.data_management.cursor import (
    decode_cursor,
    encode_cursor,
    keyset_after,
)
from app.services.data_management.entities import (
    COMPACT_TRACK_TYPES,
    _attribute_origins,
    _clean_attributes,
    task_dataset_item_id_expr,
)
from app.services.data_management.entity_filters import compile_entity_filter
from app.services.project_kind import project_kind
from app.services.data_management.task_filters import visible_tasks_stmt
from app.services.video_tracks import frame_is_outside


def _encode_scene_ref(track_id: str) -> str:
    encoded = base64.urlsafe_b64encode(track_id.encode()).decode().rstrip("=")
    return f"scene:{encoded}"


def _decode_scene_ref(track_ref: str) -> str:
    if not track_ref.startswith("scene:"):
        raise HTTPException(status_code=404, detail="Track not found")
    try:
        raw = track_ref.removeprefix("scene:")
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Track not found") from exc


def _json_array_length(column, key: str):
    value = column[key]
    return case(
        (func.jsonb_typeof(value) == "array", func.jsonb_array_length(value)),
        else_=0,
    )


def _keyframe_frame_expr(mode: str):
    guarded = case(
        (
            func.jsonb_typeof(Annotation.geometry["keyframes"]) == "array",
            Annotation.geometry["keyframes"],
        ),
        else_=literal([], type_=JSONB),
    )
    keyframes = (
        func.jsonb_array_elements(guarded)
        .table_valued("value")
        .alias(f"track_{mode}_keyframes")
    )
    aggregate = func.min if mode == "start" else func.max
    return (
        select(
            aggregate(
                cast(cast(keyframes.c.value, JSONB)["frame_index"].astext, Integer)
            )
        )
        .select_from(keyframes)
        .correlate(Annotation)
        .scalar_subquery()
    )


def _compact_sort(field: str):
    quality_count = case(
        (_json_array_length(Annotation.geometry, "keyframes") == 0, 1), else_=0
    ) + case(
        (
            Annotation.geometry["track_id"].astext.is_distinct_from(
                Annotation.track_id
            ),
            1,
        ),
        else_=0,
    )
    mapping = {
        "track.track_id": Annotation.track_id,
        "track.class_name": Annotation.class_name,
        "track.track_kind": literal("compact_video"),
        "track.start_frame": func.coalesce(_keyframe_frame_expr("start"), -1),
        "track.occurrence_count": _json_array_length(Annotation.geometry, "keyframes"),
        "track.quality_issue_count": quality_count,
        "track.outside_range_count": _json_array_length(Annotation.geometry, "outside"),
    }
    if field not in mapping:
        raise HTTPException(status_code=422, detail=f"Unsupported track sort: {field}")
    return mapping[field]


def _compact_location(row, project: Project, frame: int | None):
    return DataManagerEntityLocation(
        project_id=project.id,
        task_id=row.task_id,
        task_display_id=row.task_display_id,
        batch_id=row.batch_id,
        dataset_item_id=row.dataset_item_id,
        data_type=project.data_type,
        focus_kind="track",
        annotation_id=row.annotation_id,
        track_id=row.track_id,
        video_frame_index=frame,
    )


def _compact_from_row(row, project: Project) -> DataManagerTrackOut:
    geometry = row.geometry if isinstance(row.geometry, dict) else {}
    keyframes = [
        item for item in geometry.get("keyframes") or [] if isinstance(item, dict)
    ]
    frames = [
        int(item["frame_index"])
        for item in keyframes
        if isinstance(item.get("frame_index"), int)
    ]
    sources = Counter(str(item.get("source") or "manual") for item in keyframes)
    distinct_frames = len(set(frames))
    start = min(frames) if frames else None
    end = max(frames) if frames else None
    issues: list[str] = []
    if not keyframes:
        issues.append("missing_keyframes")
    if len(frames) != distinct_frames:
        issues.append("duplicate_keyframe")
    if geometry.get("track_id") != row.track_id:
        issues.append("track_identity_mismatch")
    outside = [item for item in geometry.get("outside") or [] if isinstance(item, dict)]
    occluded = sum(1 for item in keyframes if item.get("occluded") is True)
    attributes = _clean_attributes(row.attributes)
    span = end - start + 1 if start is not None and end is not None else None
    return DataManagerTrackOut(
        entity_key=f"track:compact:{row.annotation_id}",
        track_ref=f"compact:{row.annotation_id}",
        track_kind="compact_video",
        track_id=row.track_id,
        compact_annotation_id=row.annotation_id,
        class_name=row.class_name,
        tool_unit_id=row.tool_unit_id,
        annotation_type=row.annotation_type,
        start_frame=start,
        end_frame=end,
        span=span,
        occurrence_count=1,
        distinct_task_count=1,
        distinct_frame_count=distinct_frames,
        missing_frame_count=max((span or 0) - distinct_frames, 0),
        duplicate_frame_count=max(len(frames) - distinct_frames, 0),
        keyframe_count=len(keyframes),
        outside_range_count=len(outside),
        occluded_count=occluded,
        sources=DataManagerTrackSourceSummary(
            annotation_sources={str(row.source): 1},
            keyframe_sources=dict(sources),
        ),
        attributes=attributes,
        attribute_origins=_attribute_origins(row.attributes, row.attributes_meta),
        quality_issues=issues,
        location=_compact_location(row, project, start),
    )


def _scene_sort(field: str, aggregate: dict[str, Any]):
    mapping = {
        "track.track_id": aggregate["track_id"],
        "track.class_name": aggregate["class_name"],
        "track.track_kind": literal("scene"),
        "track.start_frame": aggregate["start_frame"],
        "track.occurrence_count": aggregate["occurrence_count"],
        "track.quality_issue_count": aggregate["quality_issue_count"],
        "track.outside_range_count": literal(0),
    }
    if field not in mapping:
        raise HTTPException(status_code=422, detail=f"Unsupported track sort: {field}")
    return mapping[field]


def _scene_location(row, project: Project):
    return DataManagerEntityLocation(
        project_id=project.id,
        task_id=row.task_id,
        task_display_id=row.task_display_id,
        batch_id=row.batch_id,
        dataset_item_id=row.dataset_item_id,
        data_type=project.data_type,
        focus_kind="track",
        annotation_id=row.annotation_id,
        track_id=row.track_id,
        scene_id=row.scene_id,
        scene_name=row.scene_name,
        scene_frame_index=row.frame_index,
    )


def _scene_member(row, project: Project) -> DataManagerTrackMemberOut:
    return DataManagerTrackMemberOut(
        annotation_id=row.annotation_id,
        task_id=row.task_id,
        task_display_id=row.task_display_id,
        class_name=row.class_name,
        source=row.source,
        frame_index=row.frame_index,
        attributes=_clean_attributes(row.attributes),
        attribute_origins=_attribute_origins(row.attributes, row.attributes_meta),
        location=_scene_location(row, project),
    )


def _scene_from_members(
    track_id: str,
    members: list[Any],
    project: Project,
    visible_frames_by_scene: dict[Any, set[int]] | None = None,
    scene_track: SceneTrack | None = None,
    presence_intervals: list[SceneTrackInterval] | None = None,
) -> DataManagerTrackOut:
    members = sorted(
        members,
        key=lambda row: (
            str(row.scene_id or ""),
            row.frame_index if row.frame_index is not None else -1,
            str(row.annotation_id),
        ),
    )
    first = members[0]
    frames = [int(row.frame_index) for row in members if row.frame_index is not None]
    frame_keys = {
        (row.scene_id, int(row.frame_index))
        for row in members
        if row.frame_index is not None
    }
    distinct_frames = len(frame_keys)
    class_counts = Counter(row.class_name for row in members)
    unit_counts = Counter(row.tool_unit_id for row in members)
    type_counts = Counter(row.annotation_type for row in members)
    source_counts = Counter(row.source for row in members)
    scene_ids = {row.scene_id for row in members if row.scene_id is not None}
    declared_intervals = sorted(
        presence_intervals or [], key=lambda row: row.start_frame
    )
    start = (
        declared_intervals[0].start_frame
        if declared_intervals
        else min(frames)
        if frames and len(scene_ids) <= 1
        else None
    )
    finite_ends = [
        row.end_frame for row in declared_intervals if row.end_frame is not None
    ]
    if declared_intervals:
        end = max(finite_ends) if len(finite_ends) == len(declared_intervals) else None
    else:
        end = max(frames) if frames and len(scene_ids) <= 1 else None
    span = end - start + 1 if start is not None and end is not None else None
    attrs = [_clean_attributes(row.attributes) for row in members]
    common_attributes: dict[str, Any] = {}
    all_keys = {key for item in attrs for key in item}
    inconsistent_attributes = False
    for key in all_keys:
        # dict/list attribute values (e.g. imported JSON vs. editor round-trip)
        # can be equal but have different key order; repr() would treat them
        # as distinct. json.dumps(sort_keys=True) normalizes key order so
        # equal values compare equal regardless of origin. default=str covers
        # any value that isn't JSON-serializable on its own.
        values = {
            json.dumps(item.get(key), sort_keys=True, ensure_ascii=False, default=str)
            for item in attrs
        }
        if len(values) == 1:
            common_attributes[key] = attrs[0].get(key)
        else:
            inconsistent_attributes = True
    issues: list[str] = []
    if len(class_counts) > 1:
        issues.append("inconsistent_class")
    if inconsistent_attributes:
        issues.append("inconsistent_attributes")
    if len(scene_ids) > 1:
        issues.append("multiple_scenes")
    if scene_track is not None and any(
        row.class_name != scene_track.class_name for row in members
    ):
        issues.append("track_identity_mismatch")
    if declared_intervals:
        outside_interval = any(
            row.frame_index is not None
            and not any(
                interval.start_frame <= row.frame_index
                and (
                    interval.end_frame is None or row.frame_index <= interval.end_frame
                )
                for interval in declared_intervals
            )
            for row in members
        )
        if outside_interval:
            issues.append("member_outside_interval")
    duplicates = max(len(members) - distinct_frames, 0)
    if duplicates:
        issues.append("duplicate_frame")
    origins = _attribute_origins(first.attributes, first.attributes_meta)
    expected_visible_frames = 0
    if visible_frames_by_scene and declared_intervals:
        scene_id = next(iter(scene_ids), None)
        expected_visible_frames = sum(
            1
            for frame in visible_frames_by_scene.get(scene_id, set())
            if any(
                interval.start_frame <= frame
                and (interval.end_frame is None or frame <= interval.end_frame)
                for interval in declared_intervals
            )
        )
    elif visible_frames_by_scene and start is not None and end is not None:
        scene_id = next(iter(scene_ids), None)
        expected_visible_frames = sum(
            1
            for frame in visible_frames_by_scene.get(scene_id, set())
            if start <= frame <= end
        )
    return DataManagerTrackOut(
        entity_key=f"track:{_encode_scene_ref(track_id)}",
        track_ref=_encode_scene_ref(track_id),
        track_kind="scene",
        track_id=track_id,
        class_name=(
            scene_track.class_name
            if scene_track is not None
            else class_counts.most_common(1)[0][0]
            if class_counts
            else None
        ),
        tool_unit_id=unit_counts.most_common(1)[0][0] if unit_counts else None,
        annotation_type=type_counts.most_common(1)[0][0] if type_counts else None,
        start_frame=start,
        end_frame=end,
        span=span,
        occurrence_count=len(members),
        distinct_task_count=len({row.task_id for row in members}),
        distinct_frame_count=distinct_frames,
        missing_frame_count=max(expected_visible_frames - distinct_frames, 0),
        duplicate_frame_count=duplicates,
        keyframe_count=sum(
            1 for row in members if getattr(row, "temporal_role", None) == "keyframe"
        ),
        sources=DataManagerTrackSourceSummary(
            annotation_sources={
                str(key): int(value) for key, value in source_counts.items()
            }
        ),
        attributes=common_attributes,
        attribute_origins=origins,
        quality_issues=issues,
        location=_scene_location(first, project),
    )


class DataManagerTrackService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _visible(
        self,
        project_id: uuid.UUID,
        user: User,
        project: Project,
        name: str,
        *,
        project_role: str | None = None,
    ):
        return visible_tasks_stmt(
            project_id, user=user, project=project, project_role=project_role
        ).subquery(name)

    def _active(self, project_id: uuid.UUID):
        return and_(
            Annotation.project_id == project_id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
            Annotation.track_id.is_not(None),
        )

    async def query(
        self,
        *,
        project_id: uuid.UUID,
        payload: DataManagerEntityQueryRequest,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerTrackQueryResponse:
        kind = project_kind(project)
        if not (kind.scene_mode or kind.data_type == "video"):
            raise HTTPException(status_code=422, detail="Track lens is not supported")
        if kind.data_type == "video" and not kind.scene_mode:
            return await self._query_compact(
                project_id=project_id,
                payload=payload,
                user=user,
                project=project,
                project_role=project_role,
            )
        return await self._query_scene(
            project_id=project_id,
            payload=payload,
            user=user,
            project=project,
            project_role=project_role,
        )

    async def _query_compact(
        self, *, project_id, payload, user, project, project_role=None
    ):
        sort = (payload.sort_json or [{"field": "track.track_id", "direction": "asc"}])[
            0
        ]
        field = str(sort.get("field") or "track.track_id")
        direction = str(sort.get("direction") or "asc")
        if direction not in {"asc", "desc"}:
            raise HTTPException(
                status_code=422, detail="Sort direction must be asc or desc"
            )
        visible = self._visible(
            project_id, user, project, "dm_compact_visible", project_role=project_role
        )
        condition = compile_entity_filter(
            payload.filter_json, Annotation, project=project, user=user
        )
        base = (
            select(Annotation.id)
            .select_from(Annotation)
            .join(visible, visible.c.id == Annotation.task_id)
            .join(Task, Task.id == Annotation.task_id)
            .where(
                self._active(project_id),
                Annotation.annotation_type.in_(COMPACT_TRACK_TYPES),
                condition,
            )
            .subquery("dm_compact_matches")
        )
        total = int(await self.db.scalar(select(func.count()).select_from(base)) or 0)
        task_total = int(
            await self.db.scalar(
                select(func.count(func.distinct(Annotation.task_id)))
                .select_from(Annotation)
                .join(base, base.c.id == Annotation.id)
            )
            or 0
        )
        distributions: dict[str, dict[str, int]] = defaultdict(dict)
        for dimension, column in (
            ("class", Annotation.class_name),
            ("source", Annotation.source),
            ("tool_unit", Annotation.tool_unit_id),
            ("type", Annotation.annotation_type),
        ):
            rows = await self.db.execute(
                select(cast(column, String), func.count(Annotation.id))
                .select_from(Annotation)
                .join(base, base.c.id == Annotation.id)
                .group_by(column)
            )
            distributions[dimension] = {str(value): int(count) for value, count in rows}
        quality_row = (
            await self.db.execute(
                select(
                    func.count(Annotation.id)
                    .filter(_json_array_length(Annotation.geometry, "keyframes") == 0)
                    .label("missing_keyframes"),
                    func.count(Annotation.id)
                    .filter(
                        Annotation.geometry["track_id"].astext.is_distinct_from(
                            Annotation.track_id
                        )
                    )
                    .label("track_identity_mismatch"),
                )
                .select_from(Annotation)
                .join(base, base.c.id == Annotation.id)
            )
        ).one()
        item = aliased(DatasetItem)
        sort_expr = _compact_sort(field)
        q = (
            select(
                Annotation.id.label("annotation_id"),
                Annotation.task_id,
                Task.display_id.label("task_display_id"),
                Task.batch_id,
                Task.file_name,
                item.id.label("dataset_item_id"),
                Annotation.track_id,
                Annotation.class_name,
                Annotation.tool_unit_id,
                Annotation.annotation_type,
                Annotation.source,
                Annotation.attributes,
                Annotation.attributes_meta,
                Annotation.geometry,
                sort_expr.label("_sort_value"),
            )
            .select_from(Annotation)
            .join(base, base.c.id == Annotation.id)
            .join(Task, Task.id == Annotation.task_id)
            .outerjoin(item, item.id == task_dataset_item_id_expr())
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
                    Annotation.id,
                    direction=direction,
                    value=value,
                    tie=tie_id,
                )
            )
        order = sort_expr.desc() if direction == "desc" else sort_expr.asc()
        rows = (
            await self.db.execute(
                q.order_by(order, Annotation.id.asc()).limit(payload.limit + 1)
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
        return DataManagerTrackQueryResponse(
            items=[_compact_from_row(row, project) for row in page_rows],
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
                by_quality={
                    key: int(value or 0)
                    for key, value in {
                        "missing_keyframes": quality_row.missing_keyframes,
                        "track_identity_mismatch": quality_row.track_identity_mismatch,
                    }.items()
                    if value
                },
            ),
        )

    def _scene_member_query(self, *, project_id, user, project, project_role=None):
        visible = self._visible(
            project_id, user, project, "dm_scene_visible", project_role=project_role
        )
        item = aliased(DatasetItem)
        scene = aliased(Scene)
        return (
            select(
                Annotation.id.label("annotation_id"),
                Annotation.task_id,
                Task.display_id.label("task_display_id"),
                Task.batch_id,
                Task.file_name,
                item.id.label("dataset_item_id"),
                item.scene_id,
                scene.name.label("scene_name"),
                item.frame_index,
                Annotation.track_id,
                Annotation.scene_track_id,
                Annotation.class_name,
                Annotation.tool_unit_id,
                Annotation.annotation_type,
                Annotation.source,
                Annotation.temporal_role,
                Annotation.attributes,
                Annotation.attributes_meta,
            )
            .select_from(Annotation)
            .join(visible, visible.c.id == Annotation.task_id)
            .join(Task, Task.id == Annotation.task_id)
            .outerjoin(item, item.id == task_dataset_item_id_expr())
            .outerjoin(scene, scene.id == item.scene_id)
            .where(
                self._active(project_id),
                Annotation.annotation_type.not_in(COMPACT_TRACK_TYPES),
            )
        )

    async def _query_scene(
        self, *, project_id, payload, user, project, project_role=None
    ):
        sort = (payload.sort_json or [{"field": "track.track_id", "direction": "asc"}])[
            0
        ]
        field = str(sort.get("field") or "track.track_id")
        direction = str(sort.get("direction") or "asc")
        if direction not in {"asc", "desc"}:
            raise HTTPException(
                status_code=422, detail="Sort direction must be asc or desc"
            )
        visible = self._visible(
            project_id,
            user,
            project,
            "dm_scene_match_visible",
            project_role=project_role,
        )
        condition = compile_entity_filter(
            payload.filter_json, Annotation, project=project, user=user
        )
        matched_keys = (
            select(Annotation.track_id.label("track_id"))
            .select_from(Annotation)
            .join(visible, visible.c.id == Annotation.task_id)
            .join(Task, Task.id == Annotation.task_id)
            .where(
                self._active(project_id),
                Annotation.annotation_type.not_in(COMPACT_TRACK_TYPES),
                condition,
            )
            .distinct()
            .subquery("dm_scene_matched_tracks")
        )
        total = int(
            await self.db.scalar(select(func.count()).select_from(matched_keys)) or 0
        )

        all_members = self._scene_member_query(
            project_id=project_id, user=user, project=project, project_role=project_role
        ).subquery("dm_scene_track_members")
        class_count = func.count(func.distinct(all_members.c.class_name))
        scene_count = func.count(func.distinct(all_members.c.scene_id))
        occurrence_count = func.count(all_members.c.annotation_id)
        frame_identity = func.concat(
            cast(all_members.c.scene_id, String), ":", all_members.c.frame_index
        )
        distinct_frames = func.count(func.distinct(frame_identity))
        quality_count = (
            case((class_count > 1, 1), else_=0)
            + case((scene_count > 1, 1), else_=0)
            + case((occurrence_count > distinct_frames, 1), else_=0)
        )
        aggregate = {
            "track_id": all_members.c.track_id,
            "class_name": func.min(all_members.c.class_name),
            "start_frame": func.coalesce(func.min(all_members.c.frame_index), -1),
            "occurrence_count": occurrence_count,
            "quality_issue_count": quality_count,
        }
        sort_expr = _scene_sort(field, aggregate)
        task_total = int(
            await self.db.scalar(
                select(func.count(func.distinct(all_members.c.task_id)))
                .select_from(all_members)
                .join(matched_keys, matched_keys.c.track_id == all_members.c.track_id)
            )
            or 0
        )
        distributions: dict[str, dict[str, int]] = defaultdict(dict)
        for dimension, column in (
            ("class", all_members.c.class_name),
            ("source", all_members.c.source),
            ("tool_unit", all_members.c.tool_unit_id),
            ("type", all_members.c.annotation_type),
        ):
            rows = await self.db.execute(
                select(
                    cast(column, String),
                    func.count(func.distinct(all_members.c.track_id)),
                )
                .select_from(all_members)
                .join(matched_keys, matched_keys.c.track_id == all_members.c.track_id)
                .group_by(column)
            )
            distributions[dimension] = {str(value): int(count) for value, count in rows}
        quality_groups = (
            select(
                all_members.c.track_id,
                (class_count > 1).label("inconsistent_class"),
                (scene_count > 1).label("multiple_scenes"),
                (occurrence_count > distinct_frames).label("duplicate_frame"),
            )
            .select_from(all_members)
            .join(matched_keys, matched_keys.c.track_id == all_members.c.track_id)
            .group_by(all_members.c.track_id)
            .subquery("dm_scene_track_quality")
        )
        quality_row = (
            await self.db.execute(
                select(
                    func.count()
                    .filter(quality_groups.c.inconsistent_class)
                    .label("inconsistent_class"),
                    func.count()
                    .filter(quality_groups.c.multiple_scenes)
                    .label("multiple_scenes"),
                    func.count()
                    .filter(quality_groups.c.duplicate_frame)
                    .label("duplicate_frame"),
                ).select_from(quality_groups)
            )
        ).one()
        q = (
            select(
                all_members.c.track_id,
                sort_expr.label("_sort_value"),
            )
            .join(matched_keys, matched_keys.c.track_id == all_members.c.track_id)
            .group_by(all_members.c.track_id)
        )
        if payload.cursor:
            value, tie = decode_cursor(payload.cursor, field=field, direction=direction)
            q = q.having(
                keyset_after(
                    sort_expr,
                    all_members.c.track_id,
                    direction=direction,
                    value=value,
                    tie=tie,
                )
            )
        order = sort_expr.desc() if direction == "desc" else sort_expr.asc()
        page_keys = (
            await self.db.execute(
                q.order_by(order, all_members.c.track_id.asc()).limit(payload.limit + 1)
            )
        ).all()
        has_more = len(page_keys) > payload.limit
        page_keys = page_keys[: payload.limit]
        ids = [row.track_id for row in page_keys]
        member_rows = []
        if ids:
            member_rows = (
                await self.db.execute(
                    self._scene_member_query(
                        project_id=project_id,
                        user=user,
                        project=project,
                        project_role=project_role,
                    ).where(Annotation.track_id.in_(ids))
                )
            ).all()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in member_rows:
            grouped[str(row.track_id)].append(row)
        linked_track_ids = {
            row.scene_track_id for row in member_rows if row.scene_track_id is not None
        }
        scene_tracks = (
            list(
                (
                    await self.db.execute(
                        select(SceneTrack).where(SceneTrack.id.in_(linked_track_ids))
                    )
                ).scalars()
            )
            if linked_track_ids
            else []
        )
        scene_track_by_external_id = {row.track_id: row for row in scene_tracks}
        intervals = (
            list(
                (
                    await self.db.execute(
                        select(SceneTrackInterval)
                        .where(SceneTrackInterval.scene_track_id.in_(linked_track_ids))
                        .order_by(
                            SceneTrackInterval.scene_track_id,
                            SceneTrackInterval.start_frame,
                        )
                    )
                ).scalars()
            )
            if linked_track_ids
            else []
        )
        intervals_by_track_id: dict[uuid.UUID, list[SceneTrackInterval]] = defaultdict(
            list
        )
        for interval in intervals:
            intervals_by_track_id[interval.scene_track_id].append(interval)
        next_cursor = None
        if has_more and page_keys:
            last = page_keys[-1]
            next_cursor = encode_cursor(
                field=field,
                direction=direction,
                value=last._sort_value,
                tie=str(last.track_id),
            )
        visible_frames = await self._visible_scene_frames(
            project_id=project_id,
            user=user,
            project=project,
            project_role=project_role,
            scene_ids={row.scene_id for row in member_rows if row.scene_id is not None},
        )
        return DataManagerTrackQueryResponse(
            items=[
                _scene_from_members(
                    str(track_id),
                    grouped[str(track_id)],
                    project,
                    visible_frames,
                    scene_track=scene_track_by_external_id.get(str(track_id)),
                    presence_intervals=intervals_by_track_id.get(
                        scene_track_by_external_id[str(track_id)].id, []
                    )
                    if str(track_id) in scene_track_by_external_id
                    else [],
                )
                for track_id in ids
                if grouped[str(track_id)]
            ],
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
                by_quality={
                    key: int(value or 0)
                    for key, value in {
                        "inconsistent_class": quality_row.inconsistent_class,
                        "multiple_scenes": quality_row.multiple_scenes,
                        "duplicate_frame": quality_row.duplicate_frame,
                    }.items()
                    if value
                },
            ),
        )

    async def detail(
        self,
        *,
        project_id: uuid.UUID,
        track_ref: str,
        user: User,
        project: Project,
        project_role: str | None = None,
    ) -> DataManagerTrackDetailResponse:
        if track_ref.startswith("compact:"):
            try:
                annotation_id = uuid.UUID(track_ref.removeprefix("compact:"))
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Track not found") from exc
            row = await self._compact_detail_row(
                project_id=project_id,
                annotation_id=annotation_id,
                user=user,
                project=project,
                project_role=project_role,
            )
            track = _compact_from_row(row, project)
            geometry = row.geometry if isinstance(row.geometry, dict) else {}
            members = [
                DataManagerTrackMemberOut(
                    annotation_id=row.annotation_id,
                    task_id=row.task_id,
                    task_display_id=row.task_display_id,
                    class_name=row.class_name,
                    source=row.source,
                    frame_index=keyframe.get("frame_index"),
                    keyframe_source=str(keyframe.get("source") or "manual"),
                    occluded=bool(keyframe.get("occluded", False)),
                    outside=frame_is_outside(
                        geometry, int(keyframe.get("frame_index", 0))
                    ),
                    attributes=keyframe.get("attributes") or {},
                    location=_compact_location(
                        row, project, int(keyframe.get("frame_index", 0))
                    ),
                )
                for keyframe in geometry.get("keyframes") or []
                if isinstance(keyframe, dict)
            ]
            return DataManagerTrackDetailResponse(track=track, members=members)

        track_id = _decode_scene_ref(track_ref)
        rows = (
            await self.db.execute(
                self._scene_member_query(
                    project_id=project_id,
                    user=user,
                    project=project,
                    project_role=project_role,
                ).where(Annotation.track_id == track_id)
            )
        ).all()
        if not rows:
            raise HTTPException(status_code=404, detail="Track not found")
        visible_frames = await self._visible_scene_frames(
            project_id=project_id,
            user=user,
            project=project,
            project_role=project_role,
            scene_ids={row.scene_id for row in rows if row.scene_id is not None},
        )
        linked_id = next(
            (row.scene_track_id for row in rows if row.scene_track_id is not None),
            None,
        )
        scene_track = await self.db.get(SceneTrack, linked_id) if linked_id else None
        presence_intervals = (
            list(
                (
                    await self.db.execute(
                        select(SceneTrackInterval)
                        .where(SceneTrackInterval.scene_track_id == linked_id)
                        .order_by(SceneTrackInterval.start_frame)
                    )
                ).scalars()
            )
            if linked_id
            else []
        )
        track = _scene_from_members(
            track_id,
            rows,
            project,
            visible_frames,
            scene_track=scene_track,
            presence_intervals=presence_intervals,
        )
        return DataManagerTrackDetailResponse(
            track=track,
            members=[
                _scene_member(row, project)
                for row in sorted(
                    rows,
                    key=lambda item: (
                        str(item.scene_id or ""),
                        item.frame_index if item.frame_index is not None else -1,
                        str(item.annotation_id),
                    ),
                )
            ],
        )

    async def _visible_scene_frames(
        self, *, project_id, user, project, project_role=None, scene_ids: set[Any]
    ) -> dict[Any, set[int]]:
        if not scene_ids:
            return {}
        visible = self._visible(
            project_id,
            user,
            project,
            "dm_scene_frame_visible",
            project_role=project_role,
        )
        item = aliased(DatasetItem)
        rows = await self.db.execute(
            select(item.scene_id, item.frame_index)
            .select_from(Task)
            .join(visible, visible.c.id == Task.id)
            .join(item, item.id == task_dataset_item_id_expr())
            .where(
                item.scene_id.in_(scene_ids),
                item.frame_index.is_not(None),
            )
            .distinct()
        )
        result: dict[Any, set[int]] = defaultdict(set)
        for scene_id, frame_index in rows:
            result[scene_id].add(int(frame_index))
        return result

    async def _compact_detail_row(
        self, *, project_id, annotation_id, user, project, project_role=None
    ):
        visible = self._visible(
            project_id,
            user,
            project,
            "dm_compact_detail_visible",
            project_role=project_role,
        )
        item = aliased(DatasetItem)
        row = (
            await self.db.execute(
                select(
                    Annotation.id.label("annotation_id"),
                    Annotation.task_id,
                    Task.display_id.label("task_display_id"),
                    Task.batch_id,
                    item.id.label("dataset_item_id"),
                    Annotation.track_id,
                    Annotation.class_name,
                    Annotation.tool_unit_id,
                    Annotation.annotation_type,
                    Annotation.source,
                    Annotation.attributes,
                    Annotation.attributes_meta,
                    Annotation.geometry,
                )
                .select_from(Annotation)
                .join(visible, visible.c.id == Annotation.task_id)
                .join(Task, Task.id == Annotation.task_id)
                .outerjoin(item, item.id == task_dataset_item_id_expr())
                .where(
                    self._active(project_id),
                    Annotation.id == annotation_id,
                    Annotation.annotation_type.in_(COMPACT_TRACK_TYPES),
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Track not found")
        return row
