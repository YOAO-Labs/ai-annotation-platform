from __future__ import annotations

import uuid
from copy import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import VideoSegment
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_track_quality import VideoTrackQualityRun
from app.services.exporting.video_scope import VideoExportScope, clip_video_geometry
from app.services.video_collaboration import collaboration_enabled
from app.services.video_track_quality import _DisjointSet, refresh_staleness
from app.services.labeling_spatial_quality import quality_rows, spatial_quality_data
from app.services.video_tracks import normalize_outside_ranges


class VideoBoundaryUnreconciledError(RuntimeError):
    def __init__(self, boundaries: list[dict[str, Any]]):
        super().__init__("video_boundary_unreconciled")
        self.detail = {
            "reason": "video_boundary_unreconciled",
            "boundaries": boundaries,
        }


def _core_scope(task: Task, segment: VideoSegment) -> VideoExportScope:
    return VideoExportScope(
        task_id=task.id,
        dataset_item_id=segment.dataset_item_id,
        selection_kind="frames",
        from_frame=segment.start_frame,
        to_frame=segment.end_frame,
    )


def _required_boundaries(
    segments: list[VideoSegment], scope: VideoExportScope | None
) -> list[tuple[VideoSegment, VideoSegment]]:
    selected = segments
    if scope is not None:
        selected = [
            segment
            for segment in segments
            if segment.end_frame >= scope.from_frame
            and segment.start_frame <= scope.to_frame
        ]
    selected_ids = {segment.id for segment in selected}
    return [
        (left, right)
        for left, right in zip(segments, segments[1:], strict=False)
        if left.id in selected_ids
        and right.id in selected_ids
        and (scope is None or scope.from_frame <= right.start_frame <= scope.to_frame)
    ]


async def accepted_boundary_runs(
    db: AsyncSession,
    *,
    task: Task,
    segments: list[VideoSegment],
    scope: VideoExportScope | None,
) -> list[VideoTrackQualityRun]:
    boundaries = _required_boundaries(segments, scope)
    if not boundaries:
        return []
    run_data = await spatial_quality_data(db, "list_video_runs", task_id=task.id)
    rows = quality_rows(VideoTrackQualityRun, run_data.get("rows"))
    by_boundary: dict[tuple[uuid.UUID, uuid.UUID], list[VideoTrackQualityRun]] = {}
    for run in rows:
        by_boundary.setdefault((run.left_segment_id, run.right_segment_id), []).append(
            run
        )
    accepted: list[VideoTrackQualityRun] = []
    missing: list[dict[str, Any]] = []
    for left, right in boundaries:
        candidates = by_boundary.get((left.id, right.id), [])
        run = candidates[0] if candidates else None
        for candidate in candidates:
            await refresh_staleness(db, candidate)
            if candidate.status in {"accepted", "empty_overlap"}:
                run = candidate
                break
        if run is None or run.status not in {"accepted", "empty_overlap"}:
            missing.append(
                {
                    "left_segment_id": str(left.id),
                    "right_segment_id": str(right.id),
                    "run_id": str(run.id) if run else None,
                    "status": run.status if run else "missing",
                }
            )
        else:
            accepted.append(run)
    if missing:
        raise VideoBoundaryUnreconciledError(missing)
    return accepted


def _merge_track_component(rows: list[Any]) -> Any:
    rows.sort(
        key=lambda row: min(
            (
                int(keyframe.get("frame_index", 0))
                for keyframe in (row.geometry or {}).get("keyframes") or []
            ),
            default=0,
        )
    )
    merged = copy(rows[0])
    track_id = min(
        (str(row.track_id) for row in rows if row.track_id),
        default=f"trk_{rows[0].id.hex}",
    )
    keyframes = [
        dict(keyframe)
        for row in rows
        for keyframe in (row.geometry or {}).get("keyframes") or []
    ]
    keyframes.sort(key=lambda keyframe: int(keyframe.get("frame_index", 0)))
    frames = [int(keyframe.get("frame_index", 0)) for keyframe in keyframes]
    if len(frames) != len(set(frames)):
        raise ValueError("canonical video track has duplicate core frame keyframes")
    outside = normalize_outside_ranges(
        [
            dict(range_)
            for row in rows
            for range_ in (row.geometry or {}).get("outside") or []
        ]
    )
    merged.track_id = track_id
    merged.geometry = {
        **dict(rows[0].geometry or {}),
        "track_id": track_id,
        "keyframes": keyframes,
        "outside": outside,
    }
    return merged


def merge_canonical_annotations(
    annotations: list[Any], runs: list[VideoTrackQualityRun]
) -> list[Any]:
    dsu = _DisjointSet()
    by_id = {row.id: row for row in annotations}
    for run in runs:
        for pair in run.pairs or []:
            if pair.get("decision") != "same_track":
                continue
            left_id = uuid.UUID(str(pair["left_annotation_id"]))
            right_id = uuid.UUID(str(pair["right_annotation_id"]))
            if left_id in by_id and right_id in by_id:
                dsu.union(left_id, right_id)
    components: dict[uuid.UUID, list[Any]] = {}
    for row in annotations:
        root = dsu.find(row.id) if row.id in dsu.parent else row.id
        components.setdefault(root, []).append(row)
    return [
        _merge_track_component(rows)
        if len(rows) > 1
        and all(
            str((row.geometry or {}).get("type") or "").startswith("video_track_")
            for row in rows
        )
        else rows[0]
        for rows in components.values()
    ]


async def canonical_task_annotations(
    db: AsyncSession,
    *,
    project: Project,
    task: Task,
    annotations: list[Any],
    scope: VideoExportScope | None,
) -> list[Any]:
    if not collaboration_enabled(project):
        if scope is None:
            return annotations
        scoped: list[Any] = []
        for annotation in annotations:
            geometry = clip_video_geometry(annotation.geometry, scope)
            if geometry is not None:
                row = copy(annotation)
                row.geometry = geometry
                scoped.append(row)
        return scoped
    segments = list(
        (
            await db.execute(
                select(VideoSegment)
                .where(VideoSegment.dataset_item_id == task.dataset_item_id)
                .order_by(VideoSegment.segment_index)
            )
        )
        .scalars()
        .all()
    )
    by_id = {segment.id: segment for segment in segments}
    clipped: list[Any] = []
    for annotation in annotations:
        segment = by_id.get(annotation.video_segment_id)
        if segment is None:
            raise ValueError("collaborative video annotation has no valid segment")
        geometry = clip_video_geometry(annotation.geometry, _core_scope(task, segment))
        if geometry is None:
            continue
        row = copy(annotation)
        row.geometry = geometry
        clipped.append(row)
    runs = await accepted_boundary_runs(db, task=task, segments=segments, scope=scope)
    merged = merge_canonical_annotations(clipped, runs)
    if scope is None:
        return merged
    scoped: list[Any] = []
    for row in merged:
        geometry = clip_video_geometry(row.geometry, scope)
        if geometry is not None:
            row.geometry = geometry
            scoped.append(row)
    return scoped


async def assert_task_boundaries_reconciled(
    db: AsyncSession, *, project: Project, task: Task
) -> None:
    if not collaboration_enabled(project):
        return
    segments = list(
        (
            await db.execute(
                select(VideoSegment)
                .where(VideoSegment.dataset_item_id == task.dataset_item_id)
                .order_by(VideoSegment.segment_index)
            )
        )
        .scalars()
        .all()
    )
    await accepted_boundary_runs(db, task=task, segments=segments, scope=None)


async def assert_export_boundaries_reconciled(
    db: AsyncSession,
    *,
    project: Project,
    batch_id: uuid.UUID | None,
    scope: VideoExportScope | None,
) -> None:
    if not collaboration_enabled(project):
        return
    query = select(Task).where(Task.project_id == project.id)
    if batch_id is not None:
        query = query.where(Task.batch_id == batch_id)
    if scope is not None:
        query = query.where(Task.id == scope.task_id)
    tasks = list((await db.execute(query)).scalars().all())
    for task in tasks:
        segments = list(
            (
                await db.execute(
                    select(VideoSegment)
                    .where(VideoSegment.dataset_item_id == task.dataset_item_id)
                    .order_by(VideoSegment.segment_index)
                )
            )
            .scalars()
            .all()
        )
        await accepted_boundary_runs(
            db,
            task=task,
            segments=segments,
            scope=scope if scope is not None and scope.task_id == task.id else None,
        )
