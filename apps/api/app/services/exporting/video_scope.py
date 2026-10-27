from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.schemas.export import (
    VideoExportFrameSelection,
    VideoExportScopeRequest,
    VideoExportSegmentSelection,
)
from app.services.labeling_core_read_relations import (
    get_core_row,
    task_row,
    video_segment_row,
)
from app.services.video_tracks import normalize_outside_ranges, resolve_track_at_frame
from app.services import original_sources
from app.services.content_snapshot import ContentError

MAX_SCOPE_SEGMENTS = 100_000
MAX_SCOPE_LINKS = 50_000


@dataclass(frozen=True)
class VideoExportSegment:
    id: uuid.UUID
    segment_index: int
    start_frame: int
    end_frame: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "segment_index": self.segment_index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
        }


@dataclass(frozen=True)
class VideoExportScope:
    task_id: uuid.UUID
    dataset_item_id: uuid.UUID | None
    selection_kind: Literal["segments", "frames"]
    from_frame: int
    to_frame: int
    segments: tuple[VideoExportSegment, ...] = ()

    def contains(self, frame_index: int) -> bool:
        return self.from_frame <= int(frame_index) <= self.to_frame

    def filter_frames(self, frame_indexes: list[int]) -> list[int]:
        return [frame for frame in frame_indexes if self.contains(frame)]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": str(self.task_id),
            "dataset_item_id": str(self.dataset_item_id)
            if self.dataset_item_id
            else None,
            "selection_kind": self.selection_kind,
            "from_frame": self.from_frame,
            "to_frame": self.to_frame,
        }
        if self.segments:
            payload["segments"] = [segment.as_dict() for segment in self.segments]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> VideoExportScope | None:
        if not payload:
            return None
        return cls(
            task_id=uuid.UUID(str(payload["task_id"])),
            dataset_item_id=uuid.UUID(str(payload["dataset_item_id"]))
            if payload["dataset_item_id"]
            else None,
            selection_kind=cast(
                Literal["segments", "frames"], str(payload["selection_kind"])
            ),
            from_frame=int(payload["from_frame"]),
            to_frame=int(payload["to_frame"]),
            segments=tuple(
                VideoExportSegment(
                    id=uuid.UUID(str(segment["id"])),
                    segment_index=int(segment["segment_index"]),
                    start_frame=int(segment["start_frame"]),
                    end_frame=int(segment["end_frame"]),
                )
                for segment in payload.get("segments") or []
            ),
        )


async def normalize_video_export_scope(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    project: Project,
    request: VideoExportScopeRequest | None,
    batch_id: uuid.UUID | None = None,
) -> VideoExportScope | None:
    if request is None:
        return None
    if project.data_type != "video":
        raise ValueError("video export scope is only supported for video projects")

    task = await get_core_row(db, task_row, request.task_id)
    if task is None or task.project_id != project.id:
        raise ValueError("video export task does not belong to this project")
    if batch_id is not None and task.batch_id != batch_id:
        raise ValueError("video export task does not belong to this batch")

    if task.file_type != "video":
        raise ValueError("video export task is not a video")
    try:
        relation = await original_sources.resolve_task_relation(
            db, task.id, max_links=MAX_SCOPE_LINKS
        )
    except ContentError:
        raise ValueError("video export source relation is unavailable") from None
    item_id = relation.item_id
    # Source extent is checked against the completely verified video's actual
    # probe receipt by the Worker, before any frame or track expansion.

    selection = request.selection
    if isinstance(selection, VideoExportFrameSelection):
        return VideoExportScope(
            task_id=task.id,
            dataset_item_id=item_id,
            selection_kind="frames",
            from_frame=selection.from_frame,
            to_frame=selection.to_frame,
        )

    assert isinstance(selection, VideoExportSegmentSelection)
    if item_id is None:
        raise ValueError("video export segments require a dataset item")
    endpoints = (
        await db.execute(
            select(
                video_segment_row.id,
                video_segment_row.segment_index,
                video_segment_row.start_frame,
                video_segment_row.end_frame,
            ).where(
                video_segment_row.id.in_(
                    [selection.start_segment_id, selection.end_segment_id]
                ),
                video_segment_row.dataset_item_id == item_id,
            )
        )
    ).all()
    by_id = {segment.id: segment for segment in endpoints}
    start = by_id.get(selection.start_segment_id)
    end = by_id.get(selection.end_segment_id)
    if start is None or end is None:
        raise ValueError("video export segment does not belong to this task")
    if start.segment_index > end.segment_index:
        raise ValueError("video export start segment must not follow end segment")

    span = end.segment_index - start.segment_index + 1
    if span > MAX_SCOPE_SEGMENTS:
        raise ValueError("video export segment range exceeds its budget")
    rows = (
        await db.execute(
            select(
                video_segment_row.id,
                video_segment_row.segment_index,
                video_segment_row.start_frame,
                video_segment_row.end_frame,
            )
            .where(
                video_segment_row.dataset_item_id == item_id,
                video_segment_row.segment_index >= start.segment_index,
                video_segment_row.segment_index <= end.segment_index,
            )
            .order_by(video_segment_row.segment_index)
            .limit(MAX_SCOPE_SEGMENTS + 1)
        )
    ).all()
    if len(rows) != span:
        raise ValueError("video export segment range is not contiguous")
    segments = tuple(
        VideoExportSegment(
            id=row.id,
            segment_index=row.segment_index,
            start_frame=row.start_frame,
            end_frame=row.end_frame,
        )
        for row in rows
    )
    from_frame = min(segment.start_frame for segment in segments)
    to_frame = max(segment.end_frame for segment in segments)
    if from_frame < 0 or to_frame < from_frame:
        raise ValueError("invalid video export segment range")
    return VideoExportScope(
        task_id=task.id,
        dataset_item_id=item_id,
        selection_kind="segments",
        from_frame=from_frame,
        to_frame=to_frame,
        segments=segments,
    )


def _clip_outside_ranges(
    geometry: dict[str, Any], from_frame: int, to_frame: int
) -> list[dict[str, Any]]:
    clipped = []
    for range_ in normalize_outside_ranges(geometry.get("outside") or []):
        start = max(from_frame, int(range_["from"]))
        end = min(to_frame, int(range_["to"]))
        if start <= end:
            clipped.append({**range_, "from": start, "to": end})
    return normalize_outside_ranges(clipped)


def _visible_runs(
    from_frame: int, to_frame: int, outside_ranges: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    covered = sorted(
        (int(range_["from"]), int(range_["to"])) for range_ in outside_ranges
    )
    merged: list[list[int]] = []
    for start, end in covered:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    runs: list[tuple[int, int]] = []
    cursor = from_frame
    for start, end in merged:
        if cursor < start:
            runs.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= to_frame:
        runs.append((cursor, to_frame))
    return runs


def _boundary_keyframe(geometry: dict[str, Any], frame_index: int) -> dict | None:
    resolved = resolve_track_at_frame(geometry, frame_index)
    if resolved is None:
        return None
    row: dict[str, Any] = {
        "frame_index": frame_index,
        "source": resolved.get("source", "manual"),
        "occluded": bool(resolved.get("occluded", False)),
    }
    for field in ("bbox", "points", "mask", "attributes"):
        if field in resolved:
            row[field] = deepcopy(resolved[field])
    if geometry.get("type") != "video_track_mask":
        row["source"] = "interpolated"
    elif row["source"] not in {"manual", "prediction"}:
        row["source"] = "manual"
    return row


def clip_video_geometry(
    geometry: dict[str, Any] | None,
    scope: VideoExportScope | None,
) -> dict[str, Any] | None:
    if not isinstance(geometry, dict):
        return None
    if scope is None:
        return deepcopy(geometry)

    geometry_type = str(geometry.get("type") or "")
    if geometry_type.startswith("video_track_"):
        original_keyframes = [
            deepcopy(keyframe)
            for keyframe in geometry.get("keyframes") or []
            if isinstance(keyframe, dict)
            and scope.contains(int(keyframe.get("frame_index", -1)))
        ]
        outside = _clip_outside_ranges(geometry, scope.from_frame, scope.to_frame)
        boundaries: dict[int, dict[str, Any]] = {}
        for start, end in _visible_runs(scope.from_frame, scope.to_frame, outside):
            for frame_index in {start, end}:
                boundary = _boundary_keyframe(geometry, frame_index)
                if boundary is not None:
                    boundaries[frame_index] = boundary
        for keyframe in original_keyframes:
            boundaries[int(keyframe.get("frame_index", 0))] = keyframe
        if not boundaries:
            return None
        return {
            **deepcopy(geometry),
            "keyframes": [boundaries[index] for index in sorted(boundaries)],
            "outside": outside,
        }

    if "frame_index" in geometry:
        return (
            deepcopy(geometry) if scope.contains(int(geometry["frame_index"])) else None
        )
    return deepcopy(geometry)
