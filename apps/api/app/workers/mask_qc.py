"""Asynchronous Raster / video Mask quality scanning."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.mask_qc import MaskQCIssue, MaskQCRun
from app.schemas.mask_qc import MaskQCConfig
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.mask_qc import (
    SingleFrameThresholds,
    TemporalResolvedFrame,
    analyze_rle_topology,
    compare_rles,
    evaluate_single_frame,
    scan_temporal_frames,
)
from app.services.mask_qc.config import severity_for_rule
from app.services.mask_qc.service import (
    MAX_QC_OVERLAP_PAIRS,
    complete_run_summary,
    issue_dedupe_key,
)
from app.services.raster_mask_storage import load_coco_rle, store_coco_rle
from app.services.video_tracks import resolve_mask_track_state_at_frame
from app.utils.raster_mask_rle import coco_rle_bbox_norm
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

_SEVERITY_RANK = {"blocker": 0, "warning": 1, "info": 2}
_SUGGESTIONS = {
    "empty_mask": "删除空 Mask，或重新标注目标。",
    "near_empty_mask": "确认目标是否真实存在，必要时重新描绘。",
    "touches_border": "确认目标是否被画面边缘截断。",
    "small_island": "检查并删除误画的小连通区域。",
    "small_hole": "检查并填补目标内部的小孔洞。",
    "narrow_bridge": "检查连接不同区域的狭窄像素桥。",
    "boundary_noise": "检查锯齿、毛刺和孤立边界噪声。",
    "derived_geometry_mismatch": "重新生成派生几何。",
    "same_class_overlap": "检查同类别实例是否被重复标注。",
    "cross_class_overlap": "检查不同类别实例的重叠是否合理。",
    "flicker": "检查短暂消失帧并修正轨迹。",
    "drift": "从最近人工关键帧重新传播或局部纠正。",
}


def _region_bbox_xyxy(rle: dict[str, Any]) -> dict[str, float] | None:
    bbox = coco_rle_bbox_norm(rle)
    if not bbox:
        return None
    return {
        "x0": bbox["x"],
        "y0": bbox["y"],
        "x1": bbox["x"] + bbox["w"],
        "y1": bbox["y"] + bbox["h"],
    }


@dataclass(frozen=True)
class _FrameMask:
    annotation_id: uuid.UUID
    annotation_version: int
    task_id: uuid.UUID
    class_name: str
    frame_index: int | None
    state: str
    source: str | None
    rle: dict[str, Any]
    geometry_digest: str
    content_digest: str
    confidence: float | None = None
    correction_lineage: dict[str, Any] | None = None


class _MaskQCCancelled(RuntimeError):
    pass


@celery_app.task(bind=True, name="app.workers.mask_qc.run_mask_qc")
def run_mask_qc(self, run_id: str) -> None:
    asyncio.run(
        _run_mask_qc(
            run_id=run_id,
            celery_task_id=getattr(self.request, "id", None),
        )
    )


def _single_frame_thresholds(config: MaskQCConfig) -> SingleFrameThresholds:
    values = config.single_frame
    return SingleFrameThresholds(
        near_empty_pixels=values.near_empty_pixels,
        small_component_pixels=values.small_component_pixels,
        small_component_ratio_ppm=round(values.small_component_ratio * 1_000_000),
        small_hole_pixels=values.small_hole_pixels,
        narrow_bridge_width=values.narrow_bridge_width,
        boundary_noise_ratio_ppm=round(values.boundary_noise_ratio * 1_000_000),
    )


async def _cancel_requested(db: AsyncSession, run: MaskQCRun) -> bool:
    if run.async_job_id is None:
        return False
    job = await db.get(AsyncJob, run.async_job_id)
    if job is None:
        return True
    await db.refresh(job)
    return job.status == AsyncJobStatus.CANCELLED.value or bool(
        (job.payload or {}).get("cancel_requested")
    )


async def _mark_cancelled(db: AsyncSession, run: MaskQCRun) -> None:
    now = datetime.now(timezone.utc)
    run.status = "cancelled"
    run.completed_at = now
    if run.async_job_id is not None:
        await async_job_svc.mark_cancelled(
            db,
            run.async_job_id,
            result={"run_id": str(run.id), "progress_pct": run.progress_pct},
        )
        await notify_job_terminal(db, job_id=run.async_job_id)


async def _update_progress(
    db: AsyncSession, run: MaskQCRun, *, completed: int, total: int
) -> None:
    pct = min(95, max(1, int(completed * 90 / max(total, 1)) + 5))
    run.progress_pct = pct
    if run.async_job_id is not None:
        await async_job_svc.update_progress(db, run.async_job_id, pct)


async def _load_reference(
    reference: dict[str, Any], cache: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    key = str(reference.get("object_key") or "")
    if not key:
        raise ValueError("Mask QC source reference has no object_key")
    if key not in cache:
        cache[key] = await load_coco_rle(reference)
    return cache[key]


def _source_versions(*frames: _FrameMask) -> dict[str, int]:
    return {
        str(frame.annotation_id): frame.annotation_version
        for frame in sorted(frames, key=lambda item: str(item.annotation_id))
    }


async def _persist_issue(
    db: AsyncSession,
    *,
    run: MaskQCRun,
    primary: _FrameMask,
    related: tuple[_FrameMask, ...],
    code: str,
    severity: str,
    metric: dict[str, Any],
    threshold: dict[str, Any],
    frame_start: int | None,
    frame_end: int | None,
    region_rle: dict[str, Any] | None,
    source: dict[str, Any],
    seen_keys: set[str],
) -> None:
    source_versions = _source_versions(primary, *related)
    region_ref = (
        await store_coco_rle(region_rle, db=db) if region_rle is not None else None
    )
    region_digest = str(region_ref["sha256"]) if region_ref else None
    related_ids = sorted(
        {primary.annotation_id, *(item.annotation_id for item in related)}, key=str
    )
    dedupe_key = issue_dedupe_key(
        code=code,
        annotation_id=primary.annotation_id,
        annotation_version=primary.annotation_version,
        related_versions=source_versions,
        frame_start=frame_start,
        frame_end=frame_end,
        region_digest=region_digest,
    )
    seen_keys.add(dedupe_key)
    values = {
        "id": uuid.uuid4(),
        "run_id": run.id,
        "last_seen_run_id": run.id,
        "project_id": run.project_id,
        "task_id": primary.task_id,
        "annotation_id": primary.annotation_id,
        "annotation_version": primary.annotation_version,
        "related_annotation_ids": related_ids,
        "source_versions": source_versions,
        "code": code,
        "severity": severity,
        "severity_rank": _SEVERITY_RANK[severity],
        # Effective staleness is derived from source_versions in every read and
        # blocking query, so a late worker never needs per-finding DB probes.
        "status": "open",
        "frame_start": frame_start,
        "frame_end": frame_end,
        "metric": metric,
        "threshold": threshold,
        "region_bbox": _region_bbox_xyxy(region_rle) if region_rle else None,
        "region_mask_ref": region_ref,
        "region_digest": region_digest,
        "dedupe_key": dedupe_key,
        "source": source,
        "suggestion": _SUGGESTIONS.get(code),
    }
    statement = insert(MaskQCIssue).values(**values)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        constraint="uq_mask_qc_issues_dedupe",
        set_={
            "severity": excluded.severity,
            "severity_rank": excluded.severity_rank,
            "status": case(
                (excluded.status == "stale", "stale"),
                (
                    MaskQCIssue.status.in_(("resolved", "wont_fix")),
                    MaskQCIssue.status,
                ),
                else_="open",
            ),
            "metric": excluded.metric,
            "threshold": excluded.threshold,
            "region_bbox": excluded.region_bbox,
            "region_mask_ref": excluded.region_mask_ref,
            "region_digest": excluded.region_digest,
            "source": excluded.source,
            "suggestion": excluded.suggestion,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await db.execute(statement)
    if region_ref is not None:
        from app.services.raster_mask_objects import bind_publications

        await bind_publications(db, region_ref)


async def _scan_single_frame(
    db: AsyncSession,
    *,
    run: MaskQCRun,
    config: MaskQCConfig,
    frame: _FrameMask,
    findings_cache: dict[str, tuple],
    seen_keys: set[str],
) -> None:
    findings = findings_cache.get(frame.content_digest)
    if findings is None:
        findings = evaluate_single_frame(
            frame.rle, thresholds=_single_frame_thresholds(config)
        )
        findings_cache[frame.content_digest] = findings
    for finding in findings:
        severity = severity_for_rule(config, finding.code)
        if severity is None:
            continue
        await _persist_issue(
            db,
            run=run,
            primary=frame,
            related=(),
            code=finding.code,
            severity=severity,
            metric=finding.metric,
            threshold=finding.threshold,
            frame_start=frame.frame_index,
            frame_end=frame.frame_index,
            region_rle=finding.region_rle,
            source={
                "state": frame.state,
                "source": frame.source,
                "geometry_digest": frame.geometry_digest,
                "confidence": frame.confidence,
                "correction_lineage": frame.correction_lineage,
            },
            seen_keys=seen_keys,
        )


async def _frames_for_snapshot(
    snapshot: dict[str, Any],
    *,
    config: MaskQCConfig,
    rle_cache: dict[str, dict[str, Any]],
    db: AsyncSession,
    run: MaskQCRun,
    sampled_counter: list[int],
) -> tuple[list[_FrameMask], list[TemporalResolvedFrame]]:
    geometry = snapshot["geometry"]
    common = {
        "annotation_id": uuid.UUID(snapshot["annotation_id"]),
        "annotation_version": int(snapshot["version"]),
        "task_id": uuid.UUID(snapshot["task_id"]),
        "class_name": str(snapshot["class_name"]),
        "geometry_digest": str(snapshot["geometry_digest"]),
    }
    if geometry.get("type") == "raster_mask":
        sampled_counter[0] += 1
        rle = await _load_reference(geometry["mask"], rle_cache)
        return [
            _FrameMask(
                **common,
                frame_index=None,
                state="exact",
                source=str(snapshot.get("source") or "manual"),
                rle=rle,
                content_digest=str(geometry["mask"]["sha256"]),
            )
        ], []

    keyframes = geometry.get("keyframes") or []
    if not keyframes:
        return [], []
    observed_last_frame = max(int(item.get("frame_index", 0)) for item in keyframes)
    frame_count = int(snapshot.get("frame_count") or 0)
    last_frame = max(observed_last_frame, frame_count - 1)
    frames: list[_FrameMask] = []
    temporal: list[TemporalResolvedFrame] = []
    for frame_index in range(0, last_frame + 1, config.temporal.sample_step):
        sampled_counter[0] += 1
        if sampled_counter[0] % 100 == 0 and await _cancel_requested(db, run):
            raise _MaskQCCancelled
        resolved = resolve_mask_track_state_at_frame(geometry, frame_index)
        reference = resolved.get("mask")
        rle = await _load_reference(reference, rle_cache) if reference else None
        temporal.append(
            TemporalResolvedFrame(
                frame_index=frame_index,
                state=resolved["state"],
                source=resolved.get("source"),
                mask=rle,
                resolved_from_frame=resolved.get("resolved_from_frame"),
                confidence=resolved.get("confidence"),
                correction_lineage=resolved.get("correction_lineage"),
            )
        )
        if rle is not None and resolved["state"] != "occluded":
            frames.append(
                _FrameMask(
                    **common,
                    frame_index=frame_index,
                    state=resolved["state"],
                    source=resolved.get("source"),
                    rle=rle,
                    content_digest=str(reference["sha256"]),
                    confidence=resolved.get("confidence"),
                    correction_lineage=resolved.get("correction_lineage"),
                )
            )
    return frames, temporal


async def _scan_temporal(
    db: AsyncSession,
    *,
    run: MaskQCRun,
    config: MaskQCConfig,
    frames: list[_FrameMask],
    resolved: list[TemporalResolvedFrame],
    seen_keys: set[str],
) -> None:
    if not resolved:
        return
    by_index = {frame.frame_index: frame for frame in frames}
    fallback = frames[0] if frames else None
    for finding in scan_temporal_frames(
        resolved,
        flicker_max_frames=config.temporal.flicker_max_frames,
        drift_min_consecutive=config.temporal.drift_min_consecutive,
        centroid_shift_diagonal_ppm=round(
            config.temporal.centroid_shift_diagonal * 1_000_000
        ),
        iou_drop_ppm=round(config.temporal.iou_drop * 1_000_000),
        area_change_ratio_ppm=round(config.temporal.area_change_ratio * 1_000_000),
        component_delta=config.temporal.component_delta,
    ):
        severity = severity_for_rule(config, finding.code)
        primary = by_index.get(finding.frame_end) or fallback
        if severity is None or primary is None:
            continue
        await _persist_issue(
            db,
            run=run,
            primary=primary,
            related=(),
            code=finding.code,
            severity=severity,
            metric={**finding.metric, "anchor_frame": finding.anchor_frame},
            threshold={
                "flicker_max_frames": config.temporal.flicker_max_frames,
                "drift_min_consecutive": config.temporal.drift_min_consecutive,
                "centroid_shift_diagonal_ppm": round(
                    config.temporal.centroid_shift_diagonal * 1_000_000
                ),
            },
            frame_start=finding.frame_start,
            frame_end=finding.frame_end,
            region_rle=None,
            source={
                "source": finding.source,
                "confidence": finding.confidence,
                "correction_lineage": finding.correction_lineage,
                "geometry_digest": primary.geometry_digest,
            },
            seen_keys=seen_keys,
        )


def _aabb_intersects(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


async def _scan_overlaps(
    db: AsyncSession,
    *,
    run: MaskQCRun,
    config: MaskQCConfig,
    frame_groups: dict[int | None, list[_FrameMask]],
    topology_cache: dict[str, tuple[int, int, int, int] | None],
    seen_keys: set[str],
) -> int:
    pair_count = 0
    for frame_index in sorted(
        frame_groups, key=lambda value: -1 if value is None else value
    ):
        entries = []
        for frame in frame_groups[frame_index]:
            if frame.content_digest not in topology_cache:
                topology_cache[frame.content_digest] = analyze_rle_topology(
                    frame.rle
                ).bbox_pixels
            bbox = topology_cache[frame.content_digest]
            if bbox is not None:
                entries.append((bbox, frame))
        entries.sort(key=lambda item: (item[0][0], str(item[1].annotation_id)))
        active: list[tuple[tuple[int, int, int, int], _FrameMask]] = []
        for bbox, right in entries:
            active = [item for item in active if item[0][2] > bbox[0]]
            for left_bbox, left in active:
                if left.annotation_id == right.annotation_id or not _aabb_intersects(
                    left_bbox, bbox
                ):
                    continue
                pair_count += 1
                if pair_count > MAX_QC_OVERLAP_PAIRS:
                    raise ValueError("qc_overlap_pair_budget_exceeded")
                overlap = compare_rles(left.rle, right.rle)
                if overlap.intersection_pixels < config.single_frame.overlap_pixels:
                    continue
                primary, secondary = sorted(
                    (left, right), key=lambda item: str(item.annotation_id)
                )
                code = (
                    "same_class_overlap"
                    if left.class_name == right.class_name
                    else "cross_class_overlap"
                )
                severity = severity_for_rule(config, code)
                if severity is None:
                    continue
                await _persist_issue(
                    db,
                    run=run,
                    primary=primary,
                    related=(secondary,),
                    code=code,
                    severity=severity,
                    metric={
                        "intersection_pixels": overlap.intersection_pixels,
                        "union_pixels": overlap.union_pixels,
                        "left_area_pixels": overlap.left_area_pixels,
                        "right_area_pixels": overlap.right_area_pixels,
                        "iou_numerator": overlap.iou_numerator,
                        "iou_denominator": overlap.iou_denominator,
                    },
                    threshold={"overlap_pixels": config.single_frame.overlap_pixels},
                    frame_start=frame_index,
                    frame_end=frame_index,
                    region_rle=overlap.intersection_rle,
                    source={
                        "annotation_ids": [
                            str(primary.annotation_id),
                            str(secondary.annotation_id),
                        ],
                        "geometry_digests": [
                            primary.geometry_digest,
                            secondary.geometry_digest,
                        ],
                    },
                    seen_keys=seen_keys,
                )
            active.append((bbox, right))
    return pair_count


async def _execute_scan(db: AsyncSession, run: MaskQCRun) -> None:
    config = MaskQCConfig.model_validate(run.config_snapshot)
    snapshots = list(run.source_snapshot or [])
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        by_task[str(snapshot["task_id"])].append(snapshot)

    completed_annotations = 0
    overlap_pairs = 0
    sampled_counter = [0]
    seen_keys: set[str] = set()
    for task_id in sorted(by_task):
        rle_cache: dict[str, dict[str, Any]] = {}
        findings_cache: dict[str, tuple] = {}
        topology_cache: dict[str, tuple[int, int, int, int] | None] = {}
        frame_groups: dict[int | None, list[_FrameMask]] = defaultdict(list)
        for snapshot in sorted(
            by_task[task_id], key=lambda item: str(item["annotation_id"])
        ):
            frames, temporal = await _frames_for_snapshot(
                snapshot,
                config=config,
                rle_cache=rle_cache,
                db=db,
                run=run,
                sampled_counter=sampled_counter,
            )
            for frame in frames:
                await _scan_single_frame(
                    db,
                    run=run,
                    config=config,
                    frame=frame,
                    findings_cache=findings_cache,
                    seen_keys=seen_keys,
                )
                frame_groups[frame.frame_index].append(frame)
            await _scan_temporal(
                db,
                run=run,
                config=config,
                frames=frames,
                resolved=temporal,
                seen_keys=seen_keys,
            )
            completed_annotations += 1
            if completed_annotations % 25 == 0:
                if await _cancel_requested(db, run):
                    await _mark_cancelled(db, run)
                    await db.commit()
                    return
                await _update_progress(
                    db,
                    run,
                    completed=completed_annotations,
                    total=len(snapshots),
                )
                await db.commit()

        overlap_pairs += await _scan_overlaps(
            db,
            run=run,
            config=config,
            frame_groups=frame_groups,
            topology_cache=topology_cache,
            seen_keys=seen_keys,
        )
        await _update_progress(
            db,
            run,
            completed=completed_annotations,
            total=len(snapshots),
        )
        await db.commit()

    if await _cancel_requested(db, run):
        await _mark_cancelled(db, run)
        await db.commit()
        return

    run.summary = {
        **(run.summary or {}),
        "scanned_annotations": completed_annotations,
        "scanned_frames": sampled_counter[0],
        "overlap_pairs": overlap_pairs,
    }
    if seen_keys:
        await db.execute(
            update(MaskQCIssue)
            .where(
                MaskQCIssue.project_id == run.project_id,
                MaskQCIssue.dedupe_key.in_(sorted(seen_keys)),
            )
            .values(last_seen_run_id=run.id)
        )
    run.summary = await complete_run_summary(db, run=run)
    run.status = "completed"
    run.progress_pct = 100
    run.completed_at = datetime.now(timezone.utc)
    if run.async_job_id is not None:
        await async_job_svc.mark_complete(
            db,
            run.async_job_id,
            result={"run_id": str(run.id), **run.summary},
        )
        await notify_job_terminal(db, job_id=run.async_job_id)
    await db.commit()


async def _run_mask_qc(*, run_id: str, celery_task_id: str | None) -> None:
    run_uuid = uuid.UUID(run_id)
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with session_factory() as db:
            run = (
                await db.execute(
                    select(MaskQCRun).where(MaskQCRun.id == run_uuid).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None or run.status != "pending":
                return
            if run.async_job_id is None:
                raise ValueError("Mask QC run has no async job")
            job = await db.get(AsyncJob, run.async_job_id)
            if job is None:
                raise ValueError("Mask QC async job is missing")
            if job.status == AsyncJobStatus.CANCELLED.value:
                await _mark_cancelled(db, run)
                await db.commit()
                return
            run.status = "running"
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.progress_pct = max(run.progress_pct, 1)
            await async_job_svc.mark_running(
                db, run.async_job_id, celery_task_id=celery_task_id
            )
            await async_job_svc.update_progress(db, run.async_job_id, 1)
            await db.commit()

            try:
                await _execute_scan(db, run)
            except _MaskQCCancelled:
                await db.rollback()
                run = await db.get(MaskQCRun, run_uuid)
                if run is not None:
                    await _mark_cancelled(db, run)
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                run = await db.get(MaskQCRun, run_uuid)
                if run is None:
                    raise
                run.status = "failed"
                run.error_message = f"{type(exc).__name__}: {exc}"[:4000]
                run.completed_at = datetime.now(timezone.utc)
                if run.async_job_id is not None:
                    await async_job_svc.mark_failed(
                        db, run.async_job_id, error=run.error_message
                    )
                    await notify_job_terminal(db, job_id=run.async_job_id)
                await db.commit()
                log.exception("Mask QC run failed run=%s", run_uuid)
    finally:
        await engine.dispose()
