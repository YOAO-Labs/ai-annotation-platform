from __future__ import annotations

import uuid
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.services.project_access import get_workspace_project
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.dataset import VideoSegment
from app.db.models.task import Task
from app.db.models.video_chapter import VideoChapter
from app.db.models.video_track_quality import (
    VideoTrackQualityIssue,
    VideoTrackQualityRun,
)
from app.schemas.task import VideoMetadata
from app.services import async_job as async_job_svc
from app.services.async_job import create_job
from app.services.mask_formats.image_codecs import rasterize_normalized_polygon
from app.services.mask_qc.service import canonical_digest
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services.quality_job_lifecycle import QualityJobLifecycle
from app.services.mask_qc.topology import compare_rles
from app.services.raster_mask_storage import load_coco_rle
from app.services.video_collaboration import (
    collaboration_config,
    segment_work_bounds_for_task,
)
from app.services.video_context import derive_step
from app.services.video_frame_service import AssetsVideoContextReader
from app.services.video_tracks import resolve_track_at_frame
from app.vendor.trackeval_metrics import evaluate_sequence


TRACK_TYPES = frozenset(
    {
        "video_track_bbox",
        "video_track_polygon",
        "video_track_mask",
        "video_track_polyline",
    }
)


class VideoTrackQualityError(RuntimeError):
    def __init__(self, status_code: int, reason: str, **detail: Any):
        super().__init__(reason)
        self.status_code = status_code
        self.detail = {"reason": reason, **detail}


def geometry_family(geometry: dict) -> str | None:
    return {
        "video_track_bbox": "bbox",
        "video_track_polygon": "polygon",
        "video_track_mask": "mask",
        "video_track_polyline": "polyline",
    }.get(str(geometry.get("type") or ""))


async def _boundary(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    task: Task,
    left_segment_id: uuid.UUID,
    right_segment_id: uuid.UUID,
    video_metadata: VideoMetadata | None = None,
) -> tuple[VideoSegment, VideoSegment, list[int]]:
    rows = list(
        (
            await db.execute(
                select(VideoSegment).where(
                    VideoSegment.id.in_([left_segment_id, right_segment_id]),
                    VideoSegment.dataset_item_id == task.dataset_item_id,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    left, right = by_id.get(left_segment_id), by_id.get(right_segment_id)
    if left is None or right is None:
        raise VideoTrackQualityError(404, "video_segment_not_found")
    if right.segment_index != left.segment_index + 1:
        raise VideoTrackQualityError(422, "video_segments_not_adjacent")
    project = await get_workspace_project(db, project_id=task.project_id)
    config = collaboration_config(project)
    if not config.enabled:
        raise VideoTrackQualityError(409, "video_collaboration_disabled")
    if left.status != "completed" or right.status != "completed":
        raise VideoTrackQualityError(
            409,
            "video_boundary_segments_incomplete",
            left_status=left.status,
            right_status=right.status,
        )
    if video_metadata is None:
        video_metadata = (
            await AssetsVideoContextReader(db).from_task(
                actor_id=actor_id,
                task_id=task.id,
                project_id=task.project_id,
                file_type=task.file_type,
                dataset_item_id=task.dataset_item_id,
            )
        ).metadata
    sampling = project.video_sampling or {}
    if (
        video_metadata.frame_count is None
        or video_metadata.frame_count <= 0
        or (
            sampling.get("mode") == "fps"
            and (
                video_metadata.fps is None
                or not math.isfinite(video_metadata.fps)
                or video_metadata.fps <= 0
            )
        )
    ):
        raise VideoTrackQualityError(503, "video_metadata_not_ready")
    left_start, left_end = await segment_work_bounds_for_task(
        db, task=task, segment=left, project=project, video_metadata=video_metadata
    )
    right_start, right_end = await segment_work_bounds_for_task(
        db, task=task, segment=right, project=project, video_metadata=video_metadata
    )
    start, end = max(left_start, right_start), min(left_end, right_end)
    step = derive_step(video_metadata.fps, sampling)
    frames = [frame for frame in range(start, end + 1) if frame % step == 0]
    if not frames:
        raise VideoTrackQualityError(422, "video_boundary_sampling_empty")
    return left, right, frames


async def _annotations(
    db: AsyncSession, task_id: uuid.UUID, segment_ids: list[uuid.UUID]
) -> list[Annotation]:
    return list(
        (
            await db.execute(
                select(Annotation)
                .where(
                    Annotation.task_id == task_id,
                    Annotation.video_segment_id.in_(segment_ids),
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                )
                .order_by(Annotation.video_segment_id, Annotation.id)
            )
        )
        .scalars()
        .all()
    )


def _snapshot(annotations: list[Annotation]) -> list[dict]:
    return [
        {
            "annotation_id": str(row.id),
            "segment_id": str(row.video_segment_id),
            "version": row.version,
            "geometry_digest": canonical_digest(row.geometry),
            "class_name": row.class_name,
            "tool_unit_id": row.tool_unit_id,
            "track_id": row.track_id,
            "geometry_family": geometry_family(row.geometry),
        }
        for row in annotations
    ]


async def create_quality_run(
    db: AsyncSession,
    *,
    task: Task,
    left_segment_id: uuid.UUID,
    right_segment_id: uuid.UUID,
    actor_id: uuid.UUID,
    video_metadata: VideoMetadata | None = None,
) -> tuple[VideoTrackQualityRun, AsyncJob | None, bool]:
    left, right, frames = await _boundary(
        db,
        actor_id=actor_id,
        task=task,
        left_segment_id=left_segment_id,
        right_segment_id=right_segment_id,
        video_metadata=video_metadata,
    )
    annotations = await _annotations(db, task.id, [left.id, right.id])
    snapshot = _snapshot(annotations)
    sampling_digest = canonical_digest(frames)
    input_digest = canonical_digest(
        {"annotations": snapshot, "sampling_digest": sampling_digest}
    )
    existing = (
        await db.execute(
            select(VideoTrackQualityRun)
            .where(
                VideoTrackQualityRun.task_id == task.id,
                VideoTrackQualityRun.left_segment_id == left.id,
                VideoTrackQualityRun.right_segment_id == right.id,
                VideoTrackQualityRun.input_digest == input_digest,
                VideoTrackQualityRun.status.in_(
                    ("pending", "running", "completed", "empty_overlap", "accepted")
                ),
            )
            .order_by(VideoTrackQualityRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, None, False
    celery_task_id = str(uuid.uuid4())
    job = await create_job(
        db,
        kind=AsyncJobKind.VIDEO_TRACK_QUALITY.value,
        user_id=actor_id,
        project_id=task.project_id,
        payload={"task_id": str(task.id), "input_digest": input_digest},
        celery_task_id=celery_task_id,
    )
    run = VideoTrackQualityRun(
        task_id=task.id,
        left_segment_id=left.id,
        right_segment_id=right.id,
        async_job_id=job.id,
        requested_by_id=actor_id,
        input_snapshot=snapshot,
        input_digest=input_digest,
        sampling_digest=sampling_digest,
        metrics={"sampling_frames": frames},
    )
    db.add(run)
    await db.flush()
    return run, job, True


async def current_input_digest(db: AsyncSession, run: VideoTrackQualityRun) -> str:
    annotations = await _annotations(
        db, run.task_id, [run.left_segment_id, run.right_segment_id]
    )
    frames = list((run.metrics or {}).get("sampling_frames") or [])
    return canonical_digest(
        {
            "annotations": _snapshot(annotations),
            "sampling_digest": canonical_digest(frames),
        }
    )


async def refresh_staleness(db: AsyncSession, run: VideoTrackQualityRun) -> bool:
    if run.status in {"pending", "running", "failed", "stale"}:
        return run.status == "stale"
    if await current_input_digest(db, run) == run.input_digest:
        return False
    run.status = "stale"
    run.stale_at = datetime.now(timezone.utc)
    return True


async def dispatch_quality_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    async_job_id: uuid.UUID,
    dispatcher: QualityJobDispatcher,
) -> None:
    run = await db.get(VideoTrackQualityRun, run_id)
    job = await async_job_svc.get_job(db, async_job_id)
    if run is None or job is None or not job.celery_task_id:
        raise VideoTrackQualityError(500, "video_track_quality_dispatch_invalid")
    try:
        dispatcher.dispatch_run(
            "video_track_quality", run.id, execution_id=job.celery_task_id
        )
    except Exception as exc:
        await db.rollback()
        run = await db.get(VideoTrackQualityRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_message = f"{type(exc).__name__}: {exc}"[:4000]
            run.completed_at = datetime.now(timezone.utc)
        await async_job_svc.mark_failed(db, async_job_id, error=str(exc))
        await db.commit()
        raise VideoTrackQualityError(
            503, "video_track_quality_dispatch_failed"
        ) from exc


def _bbox_iou(left: dict, right: dict) -> float:
    lx, ly, lw, lh = (float(left.get(key, 0)) for key in ("x", "y", "w", "h"))
    rx, ry, rw, rh = (float(right.get(key, 0)) for key in ("x", "y", "w", "h"))
    if min(lw, lh, rw, rh) <= 0:
        return 0.0
    intersection = max(0.0, min(lx + lw, rx + rw) - max(lx, rx)) * max(
        0.0, min(ly + lh, ry + rh) - max(ly, ry)
    )
    union = lw * lh + rw * rh - intersection
    return intersection / union if union else 0.0


async def _shape_iou(
    left: Annotation,
    right: Annotation,
    left_shape: dict,
    right_shape: dict,
    *,
    width: int,
    height: int,
    mask_cache: dict[str, dict],
) -> float:
    family = geometry_family(left.geometry)
    if family != geometry_family(right.geometry):
        return 0.0
    if family == "bbox":
        return _bbox_iou(left_shape.get("bbox") or {}, right_shape.get("bbox") or {})
    if family == "polygon":
        left_rle = rasterize_normalized_polygon(
            left_shape.get("points") or [], width=width, height=height
        )
        right_rle = rasterize_normalized_polygon(
            right_shape.get("points") or [], width=width, height=height
        )
    elif family == "mask":

        async def load(reference: dict) -> dict:
            key = canonical_digest(reference)
            if key not in mask_cache:
                mask_cache[key] = await load_coco_rle(reference)
            return mask_cache[key]

        left_rle = await load(left_shape.get("mask") or {})
        right_rle = await load(right_shape.get("mask") or {})
    else:
        return 0.0
    overlap = compare_rles(left_rle, right_rle)
    return (
        overlap.iou_numerator / overlap.iou_denominator
        if overlap.iou_denominator
        else 0.0
    )


def _issue_ranges(events: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        grouped[
            (
                event["code"],
                event.get("left_annotation_id"),
                event.get("right_annotation_id"),
            )
        ].append(event)
    ranges: list[dict] = []
    for (code, left_id, right_id), rows in grouped.items():
        rows.sort(key=lambda row: row["frame"])
        start = end = rows[0]["frame"]
        values = [rows[0].get("iou")]
        for row in rows[1:]:
            if row["frame"] == end + 1:
                end = row["frame"]
                values.append(row.get("iou"))
                continue
            ranges.append(_issue_row(code, left_id, right_id, start, end, values))
            start = end = row["frame"]
            values = [row.get("iou")]
        ranges.append(_issue_row(code, left_id, right_id, start, end, values))
    return ranges


def _issue_row(
    code: str,
    left_id: uuid.UUID | None,
    right_id: uuid.UUID | None,
    start: int,
    end: int,
    values: list[float | None],
) -> dict:
    ious = [float(value) for value in values if value is not None]
    return {
        "left_annotation_id": left_id,
        "right_annotation_id": right_id,
        "code": code,
        "frame_start": start,
        "frame_end": end,
        "metric": {"min_iou": min(ious)} if ious else {},
    }


def _issue_frame_counts(
    issues: list[dict],
    *,
    annotation_id: str | None = None,
    frame_range: tuple[int, int] | None = None,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for issue in issues:
        if annotation_id and annotation_id not in {
            str(issue.get("left_annotation_id")),
            str(issue.get("right_annotation_id")),
        }:
            continue
        start, end = int(issue["frame_start"]), int(issue["frame_end"])
        if frame_range:
            start = max(start, frame_range[0])
            end = min(end, frame_range[1])
        if start <= end:
            counts[str(issue["code"])] += end - start + 1
    return dict(sorted(counts.items()))


def _quality_aggregates(
    *,
    run: VideoTrackQualityRun,
    pairs: list[dict],
    issues: list[dict],
) -> dict[str, list[dict]]:
    fragments = [row for row in run.input_snapshot if row.get("geometry_family")]
    tracks = []
    for fragment in fragments:
        annotation_id = str(fragment["annotation_id"])
        issue_counts = _issue_frame_counts(issues, annotation_id=annotation_id)
        tracks.append(
            {
                **fragment,
                "matched_frames": sum(
                    int(pair["matched_frames"])
                    for pair in pairs
                    if annotation_id
                    in {
                        str(pair["left_annotation_id"]),
                        str(pair["right_annotation_id"]),
                    }
                ),
                "issue_frames": sum(issue_counts.values()),
                "issues": issue_counts,
            }
        )
    segments = [
        {
            "segment_id": str(segment_id),
            "side": side,
            "track_count": sum(
                fragment["segment_id"] == str(segment_id) for fragment in fragments
            ),
            "issue_frames": sum(counts.values()),
            "issues": counts,
        }
        for segment_id, side, counts in (
            (
                run.left_segment_id,
                "left",
                _issue_frame_counts(
                    [issue for issue in issues if issue.get("left_annotation_id")]
                ),
            ),
            (
                run.right_segment_id,
                "right",
                _issue_frame_counts(
                    [issue for issue in issues if issue.get("right_annotation_id")]
                ),
            ),
        )
    ]
    return {"tracks": tracks, "segments": segments}


async def metrics_with_current_chapters(
    db: AsyncSession,
    *,
    task: Task,
    run: VideoTrackQualityRun,
    issues: list[VideoTrackQualityIssue],
) -> dict:
    issue_rows = [
        {
            "code": issue.code,
            "left_annotation_id": issue.left_annotation_id,
            "right_annotation_id": issue.right_annotation_id,
            "frame_start": issue.frame_start,
            "frame_end": issue.frame_end,
        }
        for issue in issues
    ]
    chapters = list(
        (
            await db.execute(
                select(VideoChapter)
                .where(VideoChapter.dataset_item_id == task.dataset_item_id)
                .order_by(VideoChapter.start_frame, VideoChapter.id)
            )
        )
        .scalars()
        .all()
    )
    chapter_rows = []
    for chapter in chapters:
        counts = _issue_frame_counts(
            issue_rows, frame_range=(chapter.start_frame, chapter.end_frame)
        )
        chapter_rows.append(
            {
                "chapter_id": str(chapter.id),
                "title": chapter.title,
                "start_frame": chapter.start_frame,
                "end_frame": chapter.end_frame,
                "issue_frames": sum(counts.values()),
                "issues": counts,
            }
        )
    metrics = dict(run.metrics or {})
    metrics["aggregates"] = {
        **dict(metrics.get("aggregates") or {}),
        "chapters": chapter_rows,
    }
    return metrics


async def evaluate_run(
    db: AsyncSession, run: VideoTrackQualityRun
) -> tuple[dict, list[dict], list[dict]]:
    task = await db.get(Task, run.task_id)
    if task is None:
        raise VideoTrackQualityError(404, "task_not_found")
    if await current_input_digest(db, run) != run.input_digest:
        raise VideoTrackQualityError(409, "video_track_quality_stale")
    annotations = await _annotations(
        db, run.task_id, [run.left_segment_id, run.right_segment_id]
    )
    left = [row for row in annotations if row.video_segment_id == run.left_segment_id]
    right = [row for row in annotations if row.video_segment_id == run.right_segment_id]
    unsupported = [
        row for row in annotations if geometry_family(row.geometry) == "polyline"
    ]
    left = [
        row
        for row in left
        if geometry_family(row.geometry) in {"bbox", "polygon", "mask"}
    ]
    right = [
        row
        for row in right
        if geometry_family(row.geometry) in {"bbox", "polygon", "mask"}
    ]
    frames = [int(value) for value in (run.metrics or {}).get("sampling_frames") or []]
    ctx = await AssetsVideoContextReader(db).from_task_summary(
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    width, height = int(ctx.metadata.width or 1), int(ctx.metadata.height or 1)
    left_ids = {row.id: index for index, row in enumerate(left)}
    right_ids = {row.id: index for index, row in enumerate(right)}
    data: dict[str, Any] = {
        "gt_ids": [],
        "tracker_ids": [],
        "similarity_scores": [],
        "num_gt_ids": len(left),
        "num_tracker_ids": len(right),
    }
    pair_counts: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
    issue_events: list[dict] = []
    mask_cache: dict[str, dict] = {}
    previous_match: dict[uuid.UUID, uuid.UUID] = {}
    previous_visible: set[uuid.UUID] = set()
    seen_matched: set[uuid.UUID] = set()
    for frame in frames:
        visible_left = [
            (row, resolve_track_at_frame(row.geometry, frame)) for row in left
        ]
        visible_right = [
            (row, resolve_track_at_frame(row.geometry, frame)) for row in right
        ]
        visible_left = [
            (row, shape) for row, shape in visible_left if shape is not None
        ]
        visible_right = [
            (row, shape) for row, shape in visible_right if shape is not None
        ]
        matrix = np.zeros((len(visible_left), len(visible_right)), dtype=float)
        for li, (left_row, left_shape) in enumerate(visible_left):
            for ri, (right_row, right_shape) in enumerate(visible_right):
                if (
                    left_row.class_name != right_row.class_name
                    or left_row.tool_unit_id != right_row.tool_unit_id
                    or geometry_family(left_row.geometry)
                    != geometry_family(right_row.geometry)
                ):
                    continue
                matrix[li, ri] = await _shape_iou(
                    left_row,
                    right_row,
                    left_shape,
                    right_shape,
                    width=width,
                    height=height,
                    mask_cache=mask_cache,
                )
                if matrix[li, ri] >= 0.5:
                    pair_counts[(left_row.id, right_row.id)] += 1
        data["gt_ids"].append(
            np.asarray([left_ids[row.id] for row, _ in visible_left], dtype=int)
        )
        data["tracker_ids"].append(
            np.asarray([right_ids[row.id] for row, _ in visible_right], dtype=int)
        )
        data["similarity_scores"].append(matrix)
        rows = cols = np.asarray([], dtype=int)
        if matrix.size:
            rows, cols = linear_sum_assignment(-matrix)
            keep = matrix[rows, cols] >= 0.5
            rejected = [
                (r, c)
                for r, c in zip(rows[~keep], cols[~keep], strict=True)
                if matrix[r, c] > 0
            ]
            for li, ri in rejected:
                issue_events.append(
                    {
                        "code": "geometry_mismatch",
                        "left_annotation_id": visible_left[li][0].id,
                        "right_annotation_id": visible_right[ri][0].id,
                        "frame": frame,
                        "iou": float(matrix[li, ri]),
                    }
                )
            rows, cols = rows[keep], cols[keep]
        matched_left, matched_right = set(rows.tolist()), set(cols.tolist())
        for li, (row, _) in enumerate(visible_left):
            if li not in matched_left:
                issue_events.append(
                    {
                        "code": "false_negative",
                        "left_annotation_id": row.id,
                        "frame": frame,
                    }
                )
        for ri, (row, _) in enumerate(visible_right):
            if ri not in matched_right:
                issue_events.append(
                    {
                        "code": "false_positive",
                        "right_annotation_id": row.id,
                        "frame": frame,
                    }
                )
        current_visible: set[uuid.UUID] = set()
        for li, ri in zip(rows, cols, strict=True):
            left_id, right_id = visible_left[li][0].id, visible_right[ri][0].id
            current_visible.add(left_id)
            if left_id in previous_match and previous_match[left_id] != right_id:
                issue_events.append(
                    {
                        "code": "id_switch",
                        "left_annotation_id": left_id,
                        "right_annotation_id": right_id,
                        "frame": frame,
                    }
                )
            if left_id in seen_matched and left_id not in previous_visible:
                issue_events.append(
                    {
                        "code": "fragmentation",
                        "left_annotation_id": left_id,
                        "right_annotation_id": right_id,
                        "frame": frame,
                    }
                )
            previous_match[left_id] = right_id
            seen_matched.add(left_id)
        previous_visible = current_visible
    for row in unsupported:
        issue_events.append(
            {
                "code": "unsupported_geometry",
                "left_annotation_id": row.id
                if row.video_segment_id == run.left_segment_id
                else None,
                "right_annotation_id": row.id
                if row.video_segment_id == run.right_segment_id
                else None,
                "frame": frames[0],
            }
        )
    metrics = evaluate_sequence(data)
    metrics["sampling_frames"] = frames
    pairs = []
    if pair_counts:
        left_candidates = sorted({left_id for left_id, _ in pair_counts}, key=str)
        right_candidates = sorted({right_id for _, right_id in pair_counts}, key=str)
        score = np.zeros((len(left_candidates), len(right_candidates)))
        for (left_id, right_id), count in pair_counts.items():
            score[left_candidates.index(left_id), right_candidates.index(right_id)] = (
                count
            )
        rows, cols = linear_sum_assignment(-score)
        suggestions = {
            (left_candidates[row], right_candidates[col])
            for row, col in zip(rows, cols, strict=True)
            if score[row, col] > 0
        }
        by_id = {row.id: row for row in annotations}
        for (left_id, right_id), count in sorted(
            pair_counts.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
        ):
            left_row, right_row = by_id[left_id], by_id[right_id]
            suggestion = (
                "same_track"
                if (left_id, right_id) in suggestions
                else "different_track"
            )
            pairs.append(
                {
                    "left_annotation_id": str(left_id),
                    "right_annotation_id": str(right_id),
                    "class_name": left_row.class_name,
                    "tool_unit_id": left_row.tool_unit_id,
                    "geometry_family": geometry_family(left_row.geometry),
                    "matched_frames": count,
                    "suggestion": suggestion,
                    "decision": suggestion,
                }
            )
    issues = _issue_ranges(issue_events)
    metrics["aggregates"] = _quality_aggregates(run=run, pairs=pairs, issues=issues)
    return metrics, pairs, issues


async def replace_issues(
    db: AsyncSession, run: VideoTrackQualityRun, issues: list[dict]
) -> None:
    await db.execute(
        delete(VideoTrackQualityIssue).where(VideoTrackQualityIssue.run_id == run.id)
    )
    for issue in issues:
        db.add(VideoTrackQualityIssue(run_id=run.id, **issue))


class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(self, value: uuid.UUID) -> uuid.UUID:
        self.parent.setdefault(value, value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: uuid.UUID, right: uuid.UUID) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root, key=str)] = min(
                left_root, right_root, key=str
            )


def _decision_rows(run: VideoTrackQualityRun) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    return [
        (
            uuid.UUID(str(pair["left_annotation_id"])),
            uuid.UUID(str(pair["right_annotation_id"])),
            str(pair["decision"]),
        )
        for pair in run.pairs or []
        if pair.get("decision") in {"same_track", "different_track"}
    ]


async def _assert_no_component_collision(
    db: AsyncSession, dsu: _DisjointSet, annotations: dict[uuid.UUID, Annotation]
) -> None:
    by_component_segment: dict[tuple[uuid.UUID, uuid.UUID], list[Annotation]] = (
        defaultdict(list)
    )
    for annotation_id in dsu.parent:
        annotation = annotations.get(annotation_id)
        if annotation is not None and annotation.video_segment_id is not None:
            by_component_segment[
                (dsu.find(annotation_id), annotation.video_segment_id)
            ].append(annotation)
    for (_, segment_id), rows in by_component_segment.items():
        if len(rows) < 2:
            continue
        segment = await db.get(VideoSegment, segment_id)
        if segment is None:
            continue
        for frame in range(segment.start_frame, segment.end_frame + 1):
            visible = [
                str(row.id)
                for row in rows
                if resolve_track_at_frame(row.geometry, frame) is not None
            ]
            if len(visible) > 1:
                raise VideoTrackQualityError(
                    409,
                    "video_track_pair_component_collision",
                    segment_id=str(segment_id),
                    frame_index=frame,
                    annotation_ids=visible,
                )


async def accept_quality_run(
    db: AsyncSession,
    *,
    run: VideoTrackQualityRun,
    actor_id: uuid.UUID,
    input_digest: str,
    decisions: list[dict[str, Any]],
) -> VideoTrackQualityRun:
    if run.status not in {"completed", "accepted"}:
        raise VideoTrackQualityError(
            409, "video_track_quality_not_acceptable", status=run.status
        )
    if input_digest != run.input_digest or await refresh_staleness(db, run):
        raise VideoTrackQualityError(409, "video_track_quality_stale")
    annotations = {
        row.id: row
        for row in await _annotations(
            db, run.task_id, [run.left_segment_id, run.right_segment_id]
        )
    }
    required_pairs = {
        (
            uuid.UUID(str(pair["left_annotation_id"])),
            uuid.UUID(str(pair["right_annotation_id"])),
        )
        for pair in run.pairs or []
    }
    normalized: list[dict[str, str]] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for decision in decisions:
        left_id = uuid.UUID(str(decision["left_annotation_id"]))
        right_id = uuid.UUID(str(decision["right_annotation_id"]))
        pair = (left_id, right_id)
        if pair in seen:
            raise VideoTrackQualityError(422, "video_track_pair_invalid")
        seen.add(pair)
        left, right = annotations.get(left_id), annotations.get(right_id)
        if (
            left is None
            or right is None
            or left.video_segment_id != run.left_segment_id
            or right.video_segment_id != run.right_segment_id
            or left.class_name != right.class_name
            or left.tool_unit_id != right.tool_unit_id
            or geometry_family(left.geometry) != geometry_family(right.geometry)
        ):
            raise VideoTrackQualityError(422, "video_track_pair_incompatible")
        normalized.append(
            {
                "left_annotation_id": str(left_id),
                "right_annotation_id": str(right_id),
                "decision": str(decision["decision"]),
            }
        )
    if not required_pairs.issubset(seen):
        raise VideoTrackQualityError(422, "video_track_pair_decisions_incomplete")

    accepted_runs = list(
        (
            await db.execute(
                select(VideoTrackQualityRun).where(
                    VideoTrackQualityRun.task_id == run.task_id,
                    VideoTrackQualityRun.status == "accepted",
                    VideoTrackQualityRun.id != run.id,
                )
            )
        )
        .scalars()
        .all()
    )
    all_rows = [
        row for accepted in accepted_runs for row in _decision_rows(accepted)
    ] + [
        (
            uuid.UUID(row["left_annotation_id"]),
            uuid.UUID(row["right_annotation_id"]),
            row["decision"],
        )
        for row in normalized
    ]
    dsu = _DisjointSet()
    for left_id, right_id, decision in all_rows:
        if decision == "same_track":
            dsu.union(left_id, right_id)
    conflicts = [
        [str(left_id), str(right_id)]
        for left_id, right_id, decision in all_rows
        if decision == "different_track" and dsu.find(left_id) == dsu.find(right_id)
    ]
    if conflicts:
        raise VideoTrackQualityError(
            409, "video_track_pair_decision_conflict", pairs=conflicts
        )
    task_annotations = list(
        (
            await db.execute(
                select(Annotation).where(
                    Annotation.task_id == run.task_id,
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    await _assert_no_component_collision(
        db, dsu, {row.id: row for row in task_annotations}
    )
    existing_pairs = {
        (str(pair["left_annotation_id"]), str(pair["right_annotation_id"])): pair
        for pair in run.pairs or []
    }
    run.pairs = [
        {
            **existing_pairs.get(
                (row["left_annotation_id"], row["right_annotation_id"]),
                {
                    "left_annotation_id": row["left_annotation_id"],
                    "right_annotation_id": row["right_annotation_id"],
                    "class_name": annotations[
                        uuid.UUID(row["left_annotation_id"])
                    ].class_name,
                    "geometry_family": geometry_family(
                        annotations[uuid.UUID(row["left_annotation_id"])].geometry
                    ),
                    "matched_frames": 0,
                    "suggestion": "different_track",
                },
            ),
            "decision": row["decision"],
        }
        for row in normalized
    ]
    run.status = "accepted"
    run.accepted_by_id = actor_id
    run.accepted_at = datetime.now(timezone.utc)
    return run


async def mark_boundary_runs_stale(db: AsyncSession, segment_id: uuid.UUID) -> None:
    runs = list(
        (
            await db.execute(
                select(VideoTrackQualityRun).where(
                    (VideoTrackQualityRun.left_segment_id == segment_id)
                    | (VideoTrackQualityRun.right_segment_id == segment_id),
                    VideoTrackQualityRun.status.in_(
                        ("completed", "empty_overlap", "accepted")
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    for run in runs:
        run.status = "stale"
        run.stale_at = now


async def execute_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    celery_task_id: str | None,
    job_lifecycle: QualityJobLifecycle,
) -> None:
    run = await db.get(VideoTrackQualityRun, run_id)
    if run is None or run.status != "pending":
        return

    now = datetime.now(timezone.utc)
    run.status = "running"
    run.started_at = now
    run.progress_pct = 5
    if run.async_job_id:
        await job_lifecycle.mark_running(
            run.async_job_id, celery_task_id=celery_task_id
        )
    await db.commit()
    try:
        metrics, pairs, issues = await evaluate_run(db, run)
        await replace_issues(db, run, issues)
        run.metrics = metrics
        run.pairs = pairs
        run.progress_pct = 100
        run.status = (
            "empty_overlap"
            if not pairs
            and not any(issue["code"] == "unsupported_geometry" for issue in issues)
            and not any(int(metrics.get(key, 0)) for key in ("IDTP", "IDFN", "IDFP"))
            else "completed"
        )
        run.completed_at = datetime.now(timezone.utc)
        if run.async_job_id:
            await job_lifecycle.mark_complete(
                run.async_job_id,
                result={"run_id": str(run.id), "status": run.status},
            )
    except VideoTrackQualityError as exc:
        run.status = (
            "stale" if exc.detail["reason"] == "video_track_quality_stale" else "failed"
        )
        run.stale_at = datetime.now(timezone.utc) if run.status == "stale" else None
        run.error_message = str(exc.detail)[:4000]
        run.completed_at = datetime.now(timezone.utc)
        if run.async_job_id:
            await job_lifecycle.mark_failed(run.async_job_id, error=str(exc.detail))
    except Exception as exc:
        run.status = "failed"
        run.error_message = f"{type(exc).__name__}: {exc}"[:4000]
        run.completed_at = datetime.now(timezone.utc)
        if run.async_job_id:
            await job_lifecycle.mark_failed(run.async_job_id, error=run.error_message)
    if run.async_job_id:
        await job_lifecycle.notify_terminal(job_id=run.async_job_id)
    await db.commit()
