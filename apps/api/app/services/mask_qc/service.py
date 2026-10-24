from __future__ import annotations

import base64
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.mask_qc import MaskQCIssue, MaskQCRun
from app.db.models.project import Project
from app.db.models.task import Task
from aap_workspace.mask_configuration_values import MaskQCConfig
from app.schemas.mask_qc import MaskQCRunRequest
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services.async_job import create_job
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services.mask_qc.config import load_mask_qc_config, mask_qc_config_digest
from app.services.raster_mask_storage import collect_mask_references

MAX_QC_ANNOTATIONS = 1_000
MAX_QC_SAMPLED_FRAMES = 5_000
MAX_QC_TILES = 200_000
MAX_QC_OVERLAP_PAIRS = 100_000


def _effective_current_clause():
    return literal_column(
        """
        NOT EXISTS (
            SELECT 1
            FROM jsonb_each_text(mask_qc_issues.source_versions) AS source(id, version)
            LEFT JOIN annotations AS current_annotation
              ON current_annotation.id = source.id::uuid
            WHERE current_annotation.id IS NULL
               OR current_annotation.is_active IS NOT TRUE
               OR current_annotation.was_cancelled IS TRUE
               OR current_annotation.version <> source.version::integer
        )
        """
    )


class MaskQCError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str | None = None,
        **detail: Any,
    ):
        super().__init__(message if message is not None else reason)
        self.status_code = status_code
        self.detail = {"reason": reason}
        if message is not None:
            self.detail["message"] = message
        self.detail.update(detail)


def canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _geometry_digest(geometry: dict) -> str:
    return canonical_digest(geometry)


def _sampled_frames(
    geometry: dict, config: MaskQCConfig, *, frame_count: int | None = None
) -> int:
    if geometry.get("type") == "raster_mask":
        return 1
    keyframes = geometry.get("keyframes") or []
    if not keyframes:
        return 0
    observed_frames = max(int(item.get("frame_index", 0)) for item in keyframes) + 1
    total_frames = max(observed_frames, int(frame_count or 0))
    return math.ceil(total_frames / config.temporal.sample_step)


def _tile_count(geometry: dict) -> int:
    count = 0
    seen: set[str] = set()
    for reference in collect_mask_references(geometry):
        key = str(reference.get("object_key") or "")
        if key in seen:
            continue
        seen.add(key)
        size = reference.get("size") or [0, 0]
        height, width = int(size[0]), int(size[1])
        count += math.ceil(width / 512) * math.ceil(height / 512)
    return count


def _overlap_pair_upper_bound(
    annotations: list[Annotation],
    *,
    frame_counts: dict[uuid.UUID, int],
    config: MaskQCConfig,
) -> int:
    by_task: dict[uuid.UUID, list[Annotation]] = {}
    for annotation in annotations:
        by_task.setdefault(annotation.task_id, []).append(annotation)
    total = 0
    for task_id, rows in by_task.items():
        pairs = len(rows) * (len(rows) - 1) // 2
        if not pairs:
            continue
        if any(row.geometry.get("type") == "video_track_mask" for row in rows):
            observed = max(
                (
                    _sampled_frames(
                        row.geometry,
                        config,
                        frame_count=frame_counts.get(task_id),
                    )
                    for row in rows
                ),
                default=0,
            )
            total += pairs * observed
        else:
            total += pairs
        if total > MAX_QC_OVERLAP_PAIRS:
            return total
    return total


async def _exact_overlap_pair_count(
    annotations: list[Annotation],
    *,
    frame_counts: dict[uuid.UUID, int],
    config: MaskQCConfig,
) -> int:
    """Run admission-only AABB sweep when the cheap upper bound is too large."""

    from app.services.mask_qc import analyze_rle_topology
    from app.services.raster_mask_storage import load_coco_rle
    from app.services.video_tracks import resolve_mask_track_state_at_frame

    by_task: dict[uuid.UUID, list[Annotation]] = {}
    for annotation in annotations:
        by_task.setdefault(annotation.task_id, []).append(annotation)
    total = 0
    for task_id in sorted(by_task, key=str):
        rows = by_task[task_id]
        references: dict[int | None, list[dict[str, Any]]] = {}
        for annotation in rows:
            geometry = annotation.geometry
            if geometry.get("type") == "raster_mask":
                references.setdefault(None, []).append(geometry["mask"])
                continue
            keyframes = geometry.get("keyframes") or []
            if not keyframes:
                continue
            observed_last = max(int(item["frame_index"]) for item in keyframes)
            last_frame = max(observed_last, frame_counts.get(task_id, 0) - 1)
            for frame_index in range(0, last_frame + 1, config.temporal.sample_step):
                resolved = resolve_mask_track_state_at_frame(geometry, frame_index)
                if resolved.get("mask") is not None and resolved["state"] != "occluded":
                    references.setdefault(frame_index, []).append(resolved["mask"])

        rle_cache: dict[str, dict[str, Any]] = {}
        bbox_cache: dict[str, tuple[int, int, int, int] | None] = {}
        for frame_index in sorted(
            references, key=lambda value: -1 if value is None else value
        ):
            bboxes: list[tuple[int, int, int, int]] = []
            for reference in references[frame_index]:
                digest = str(reference["sha256"])
                if digest not in bbox_cache:
                    key = str(reference["object_key"])
                    if key not in rle_cache:
                        rle_cache[key] = await load_coco_rle(reference)
                    bbox_cache[digest] = analyze_rle_topology(
                        rle_cache[key]
                    ).bbox_pixels
                bbox = bbox_cache[digest]
                if bbox is not None:
                    bboxes.append(bbox)
            bboxes.sort()
            active: list[tuple[int, int, int, int]] = []
            for bbox in bboxes:
                active = [candidate for candidate in active if candidate[2] > bbox[0]]
                for candidate in active:
                    if not (candidate[3] <= bbox[1] or bbox[3] <= candidate[1]):
                        total += 1
                        if total > MAX_QC_OVERLAP_PAIRS:
                            return total
                active.append(bbox)
    return total


async def _selected_annotations(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    request: MaskQCRunRequest,
) -> list[Annotation]:
    query = select(Annotation).where(
        Annotation.project_id == project_id,
        Annotation.is_active.is_(True),
        Annotation.was_cancelled.is_(False),
        Annotation.geometry["type"].astext.in_(("raster_mask", "video_track_mask")),
    )
    if request.scope == "task_ids":
        found_task_ids = set(
            (
                await db.execute(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.id.in_(request.task_ids),
                    )
                )
            ).scalars()
        )
        if found_task_ids != set(request.task_ids):
            raise MaskQCError(
                status_code=404,
                reason="mask_qc_task_not_found",
                message="one or more requested tasks are unavailable",
            )
        query = query.where(Annotation.task_id.in_(request.task_ids))
    elif request.scope == "annotation_ids":
        query = query.where(Annotation.id.in_(request.annotation_ids))
    rows = list(
        (await db.execute(query.order_by(Annotation.task_id, Annotation.id))).scalars()
    )
    if request.scope == "annotation_ids" and len(rows) != len(
        set(request.annotation_ids)
    ):
        raise MaskQCError(
            status_code=404,
            reason="mask_qc_annotation_not_found",
            message="one or more requested Mask annotations are unavailable",
        )
    return rows


async def _task_frame_counts(
    db: AsyncSession, annotations: list[Annotation]
) -> dict[uuid.UUID, int]:
    task_ids = sorted({annotation.task_id for annotation in annotations}, key=str)
    if not task_ids:
        return {}
    rows = (
        await db.execute(
            select(Task.id, Task.dataset_item_id).where(Task.id.in_(task_ids))
        )
    ).all()
    item_ids = tuple(item_id for _task_id, item_id in rows if item_id is not None)
    from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

    summaries = await AssetsTaskMediaSummaryReader(db).current_video_metadata_many(
        item_ids
    )
    counts: dict[uuid.UUID, int] = {}
    for task_id, item_id in rows:
        video = summaries.get(item_id) if item_id is not None else None
        value = video.get("frame_count") if isinstance(video, dict) else None
        if isinstance(value, int) and value > 0:
            counts[task_id] = value
    return counts


def _source_snapshot(
    annotations: list[Annotation], *, frame_counts: dict[uuid.UUID, int]
) -> list[dict[str, Any]]:
    return [
        {
            "annotation_id": str(annotation.id),
            "task_id": str(annotation.task_id),
            "version": annotation.version,
            "class_name": annotation.class_name,
            "source": annotation.source,
            "track_id": annotation.track_id,
            "geometry_digest": _geometry_digest(annotation.geometry),
            "geometry": annotation.geometry,
            "frame_count": frame_counts.get(annotation.task_id),
        }
        for annotation in annotations
    ]


def _task_digests(snapshot: list[dict[str, Any]]) -> dict[str, str]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for item in snapshot:
        by_task.setdefault(item["task_id"], []).append(item)
    return {
        task_id: canonical_digest(items) for task_id, items in sorted(by_task.items())
    }


async def create_mask_qc_run(
    db: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    request: MaskQCRunRequest,
) -> tuple[MaskQCRun, AsyncJob | None, bool]:
    config = load_mask_qc_config(project.mask_qc_config)
    if not config.enabled:
        raise MaskQCError(
            status_code=409,
            reason="mask_qc_disabled",
            message="Mask QC is disabled for this project",
        )
    annotations = await _selected_annotations(
        db, project_id=project.id, request=request
    )
    if not annotations:
        raise MaskQCError(
            status_code=422,
            reason="mask_qc_scope_empty",
            message="the selected scope has no active Raster Mask annotations",
        )
    if len(annotations) > MAX_QC_ANNOTATIONS:
        raise MaskQCError(
            status_code=422,
            reason="mask_qc_annotation_budget_exceeded",
            message="Mask QC annotation budget exceeded",
            actual=len(annotations),
            limit=MAX_QC_ANNOTATIONS,
        )
    current_versions = {str(item.id): item.version for item in annotations}
    unexpected = sorted(set(request.expected_versions) - set(current_versions))
    conflicts = {
        annotation_id: {
            "expected": expected,
            "actual": current_versions.get(annotation_id),
        }
        for annotation_id, expected in request.expected_versions.items()
        if current_versions.get(annotation_id) != expected
    }
    if unexpected or conflicts:
        raise MaskQCError(
            status_code=409,
            reason="mask_qc_source_version_conflict",
            message="Mask QC source versions changed",
            conflicts=conflicts,
            outside_scope=unexpected,
        )
    frame_counts = await _task_frame_counts(db, annotations)
    sampled_frames = sum(
        _sampled_frames(
            annotation.geometry,
            config,
            frame_count=frame_counts.get(annotation.task_id),
        )
        for annotation in annotations
    )
    tile_count = sum(_tile_count(annotation.geometry) for annotation in annotations)
    if sampled_frames > MAX_QC_SAMPLED_FRAMES:
        raise MaskQCError(
            status_code=422,
            reason="mask_qc_frame_budget_exceeded",
            message="Mask QC sampled-frame budget exceeded",
            actual=sampled_frames,
            limit=MAX_QC_SAMPLED_FRAMES,
        )
    if tile_count > MAX_QC_TILES:
        raise MaskQCError(
            status_code=422,
            reason="mask_qc_tile_budget_exceeded",
            message="Mask QC tile budget exceeded",
            actual=tile_count,
            limit=MAX_QC_TILES,
        )
    if (
        _overlap_pair_upper_bound(
            annotations,
            frame_counts=frame_counts,
            config=config,
        )
        > MAX_QC_OVERLAP_PAIRS
        and await _exact_overlap_pair_count(
            annotations,
            frame_counts=frame_counts,
            config=config,
        )
        > MAX_QC_OVERLAP_PAIRS
    ):
        raise MaskQCError(
            status_code=422,
            reason="qc_overlap_pair_budget_exceeded",
            message="Mask QC overlap-pair budget exceeded",
            limit=MAX_QC_OVERLAP_PAIRS,
        )
    snapshot = _source_snapshot(annotations, frame_counts=frame_counts)
    source_digest = canonical_digest(snapshot)
    config_digest = mask_qc_config_digest(config)
    selected_task_ids = sorted({item["task_id"] for item in snapshot})
    selected_annotation_ids = sorted(item["annotation_id"] for item in snapshot)
    scope_json = {
        "scope": request.scope,
        "task_ids": (
            sorted(str(value) for value in request.task_ids)
            if request.scope == "task_ids"
            else selected_task_ids
        ),
        "annotation_ids": (
            sorted(str(value) for value in request.annotation_ids)
            if request.scope == "annotation_ids"
            else selected_annotation_ids
        ),
        "expected_versions": current_versions,
    }
    singleflight_key = canonical_digest(
        {
            "scope": scope_json,
            "config_digest": config_digest,
            "source_snapshot_digest": source_digest,
        }
    )
    existing = (
        await db.execute(
            select(MaskQCRun).where(
                MaskQCRun.project_id == project.id,
                MaskQCRun.singleflight_key == singleflight_key,
                MaskQCRun.status.in_(("pending", "running")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, None, False

    celery_task_id = str(uuid.uuid4())
    try:
        async with db.begin_nested():
            job = await create_job(
                db,
                kind=AsyncJobKind.MASK_QC.value,
                user_id=actor_id,
                project_id=project.id,
                payload={
                    "scope": scope_json,
                    "source_snapshot_digest": source_digest,
                },
                celery_task_id=celery_task_id,
            )
            run = MaskQCRun(
                project_id=project.id,
                async_job_id=job.id,
                requested_by_id=actor_id,
                status="pending",
                progress_pct=0,
                scope_json=scope_json,
                config_revision=config.config_revision,
                config_digest=config_digest,
                config_snapshot=config.model_dump(mode="json"),
                source_snapshot=snapshot,
                source_snapshot_digest=source_digest,
                task_snapshot_digests=_task_digests(snapshot),
                singleflight_key=singleflight_key,
                summary={
                    "annotation_count": len(annotations),
                    "sampled_frames": sampled_frames,
                    "estimated_tiles": tile_count,
                },
            )
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(MaskQCRun).where(
                    MaskQCRun.project_id == project.id,
                    MaskQCRun.singleflight_key == singleflight_key,
                    MaskQCRun.status.in_(("pending", "running")),
                )
            )
        ).scalar_one()
        return existing, None, False
    return run, job, True


def issue_dedupe_key(
    *,
    code: str,
    annotation_id: uuid.UUID,
    annotation_version: int,
    related_versions: dict[str, int],
    frame_start: int | None,
    frame_end: int | None,
    region_digest: str | None,
) -> str:
    return canonical_digest(
        {
            "code": code,
            "primary": [str(annotation_id), annotation_version],
            "related": sorted(related_versions.items()),
            "frame": [frame_start, frame_end],
            "region_digest": region_digest,
        }
    )


async def effective_issue_status(db: AsyncSession, issue: MaskQCIssue) -> str:
    return (await effective_issue_statuses(db, [issue]))[issue.id]


async def decide_issue(
    db: AsyncSession,
    *,
    issue: MaskQCIssue,
    expected_task_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: str,
) -> None:
    """Apply a manual decision to the freshly locked, current Mask QC issue."""

    if (
        issue.task_id != expected_task_id
        or await effective_issue_status(db, issue) == "stale"
    ):
        raise MaskQCError(
            status_code=409,
            reason="mask_qc_issue_stale",
        )
    issue.status = status
    if status == "open":
        issue.resolved_by_id = None
        issue.resolved_at = None
    else:
        issue.resolved_by_id = actor_id
        issue.resolved_at = datetime.now(timezone.utc)


async def effective_issue_statuses(
    db: AsyncSession, issues: list[MaskQCIssue]
) -> dict[uuid.UUID, str]:
    """Resolve stale state for an issue page with one annotation query."""

    source_versions_by_issue = {
        issue.id: issue.source_versions
        or {str(issue.annotation_id): issue.annotation_version}
        for issue in issues
    }
    ids = {
        uuid.UUID(annotation_id)
        for source_versions in source_versions_by_issue.values()
        for annotation_id in source_versions
    }
    if ids:
        rows = (
            await db.execute(
                select(
                    Annotation.id,
                    Annotation.version,
                    Annotation.is_active,
                    Annotation.was_cancelled,
                ).where(Annotation.id.in_(ids))
            )
        ).all()
    else:
        rows = []
    current = {
        str(annotation_id): version
        for annotation_id, version, is_active, was_cancelled in rows
        if is_active and not was_cancelled
    }
    return {
        issue.id: (
            "stale"
            if issue.status == "stale"
            or any(
                current.get(annotation_id) != version
                for annotation_id, version in source_versions_by_issue[issue.id].items()
            )
            else issue.status
        )
        for issue in issues
    }


def encode_issue_cursor(issue: MaskQCIssue) -> str:
    raw = json.dumps(
        [issue.severity_rank, issue.created_at.isoformat(), str(issue.id)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_issue_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        rank, created_at, issue_id = json.loads(
            base64.urlsafe_b64decode(cursor.encode()).decode()
        )
        return int(rank), datetime.fromisoformat(created_at), uuid.UUID(issue_id)
    except Exception as exc:
        raise MaskQCError(
            status_code=422,
            reason="invalid_mask_qc_cursor",
            message="Mask QC issue cursor is invalid",
        ) from exc


async def list_issues(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    code: str | None = None,
    task_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID | None = None,
    frame: int | None = None,
    allowed_task_ids: set[uuid.UUID] | None = None,
) -> tuple[list[MaskQCIssue], str | None]:
    query = select(MaskQCIssue).where(MaskQCIssue.project_id == project_id)
    if status:
        effective_current = _effective_current_clause()
        if status == "stale":
            query = query.where(or_(MaskQCIssue.status == "stale", ~effective_current))
        else:
            query = query.where(MaskQCIssue.status == status, effective_current)
    if severity:
        query = query.where(MaskQCIssue.severity == severity)
    if code:
        query = query.where(MaskQCIssue.code == code)
    if task_id:
        query = query.where(MaskQCIssue.task_id == task_id)
    if annotation_id:
        query = query.where(MaskQCIssue.annotation_id == annotation_id)
    if frame is not None:
        query = query.where(
            MaskQCIssue.frame_start <= frame,
            MaskQCIssue.frame_end >= frame,
        )
    if allowed_task_ids is not None:
        query = query.where(MaskQCIssue.task_id.in_(allowed_task_ids))
    if cursor:
        rank, created_at, issue_id = decode_issue_cursor(cursor)
        query = query.where(
            or_(
                MaskQCIssue.severity_rank > rank,
                and_(
                    MaskQCIssue.severity_rank == rank,
                    MaskQCIssue.created_at < created_at,
                ),
                and_(
                    MaskQCIssue.severity_rank == rank,
                    MaskQCIssue.created_at == created_at,
                    MaskQCIssue.id < issue_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                query.order_by(
                    MaskQCIssue.severity_rank,
                    MaskQCIssue.created_at.desc(),
                    MaskQCIssue.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    next_cursor = encode_issue_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def qc_digest_for_issues(
    db: AsyncSession, *, run: MaskQCRun, task_id: uuid.UUID
) -> str:
    issues = list(
        (
            await db.execute(
                select(MaskQCIssue).where(
                    MaskQCIssue.task_id == task_id,
                    MaskQCIssue.last_seen_run_id == run.id,
                )
            )
        ).scalars()
    )
    statuses = await effective_issue_statuses(db, issues)
    evidence = []
    for issue in issues:
        evidence.append(
            [
                str(issue.id),
                issue.dedupe_key,
                issue.severity,
                statuses[issue.id],
            ]
        )
    return canonical_digest(
        {
            "config_digest": run.config_digest,
            "task_source_snapshot_digest": (run.task_snapshot_digests or {}).get(
                str(task_id)
            ),
            "issues": sorted(evidence),
        }
    )


async def current_task_source_snapshot(
    db: AsyncSession, *, task_id: uuid.UUID
) -> tuple[list[dict[str, Any]], str | None]:
    """Return the current Mask source snapshot and its deterministic digest."""

    annotations = list(
        (
            await db.execute(
                select(Annotation)
                .where(
                    Annotation.task_id == task_id,
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                    Annotation.geometry["type"].astext.in_(
                        ("raster_mask", "video_track_mask")
                    ),
                )
                .order_by(Annotation.id)
            )
        ).scalars()
    )
    if not annotations:
        return [], None
    frame_counts = await _task_frame_counts(db, annotations)
    snapshot = _source_snapshot(annotations, frame_counts=frame_counts)
    return snapshot, canonical_digest(snapshot)


async def latest_task_run(
    db: AsyncSession, *, project_id: uuid.UUID, task_id: uuid.UUID
) -> MaskQCRun | None:
    """Find the newest run whose frozen scope contains the task."""

    return (
        await db.execute(
            select(MaskQCRun)
            .where(
                MaskQCRun.project_id == project_id,
                MaskQCRun.task_snapshot_digests.op("?")(str(task_id)),
            )
            .order_by(MaskQCRun.created_at.desc(), MaskQCRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def task_qc_summary(
    db: AsyncSession, *, task: Task, project: Project
) -> dict[str, Any]:
    """Build the task summary from current versions, never from stale job payloads."""

    _snapshot, current_digest = await current_task_source_snapshot(db, task_id=task.id)
    config = load_mask_qc_config(project.mask_qc_config)
    if current_digest is None or not config.enabled:
        return {
            "task_id": task.id,
            "run_id": None,
            "qc_digest": None,
            "source_snapshot_digest": current_digest,
            "status": "not_applicable",
            "progress_pct": 0,
            "counts": {},
            "blocking": config.blocking,
        }

    run = await latest_task_run(db, project_id=project.id, task_id=task.id)
    if run is None:
        return {
            "task_id": task.id,
            "run_id": None,
            "qc_digest": None,
            "source_snapshot_digest": current_digest,
            "status": "stale",
            "progress_pct": 0,
            "counts": {},
            "blocking": config.blocking,
        }

    frozen_digest = (run.task_snapshot_digests or {}).get(str(task.id))
    current = (
        frozen_digest == current_digest
        and run.config_digest == mask_qc_config_digest(config)
    )
    counts: dict[str, int] = {}
    issue_rows = list(
        (
            await db.execute(
                select(MaskQCIssue).where(
                    MaskQCIssue.task_id == task.id,
                    MaskQCIssue.last_seen_run_id == run.id,
                )
            )
        ).scalars()
    )
    statuses = await effective_issue_statuses(db, issue_rows)
    for issue in issue_rows:
        status = statuses[issue.id]
        counts[status] = counts.get(status, 0) + 1
        if status == "open":
            counts[issue.severity] = counts.get(issue.severity, 0) + 1

    qc_digest = await qc_digest_for_issues(db, run=run, task_id=task.id)
    return {
        "task_id": task.id,
        "run_id": run.id,
        "qc_digest": qc_digest,
        "source_snapshot_digest": frozen_digest,
        "status": run.status if current else "stale",
        "progress_pct": run.progress_pct,
        "counts": counts,
        "blocking": config.blocking,
    }


async def current_completed_task_run(
    db: AsyncSession, *, task: Task, project: Project
) -> tuple[MaskQCRun | None, str | None]:
    """Return completed evidence matching current sources and current config."""

    _snapshot, source_digest = await current_task_source_snapshot(db, task_id=task.id)
    if source_digest is None:
        return None, None
    config_digest = mask_qc_config_digest(load_mask_qc_config(project.mask_qc_config))
    run = (
        await db.execute(
            select(MaskQCRun)
            .where(
                MaskQCRun.project_id == project.id,
                MaskQCRun.status == "completed",
                MaskQCRun.config_digest == config_digest,
                MaskQCRun.task_snapshot_digests[str(task.id)].astext == source_digest,
            )
            .order_by(MaskQCRun.completed_at.desc(), MaskQCRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return run, source_digest


async def current_open_task_issues(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    severity: str | None = None,
    issue_ids: set[uuid.UUID] | None = None,
) -> list[MaskQCIssue]:
    query = select(MaskQCIssue).where(
        MaskQCIssue.task_id == task_id,
        MaskQCIssue.status == "open",
        _effective_current_clause(),
    )
    if severity is not None:
        query = query.where(MaskQCIssue.severity == severity)
    if issue_ids is not None:
        query = query.where(MaskQCIssue.id.in_(issue_ids))
    return list((await db.execute(query.order_by(MaskQCIssue.id))).scalars())


async def complete_run_summary(db: AsyncSession, *, run: MaskQCRun) -> dict[str, Any]:
    """Aggregate persisted issue counts without loading RLE payloads."""

    effective_status = case(
        (
            or_(
                MaskQCIssue.status == "stale",
                ~_effective_current_clause(),
            ),
            "stale",
        ),
        else_=MaskQCIssue.status,
    ).label("effective_status")
    rows = (
        await db.execute(
            select(
                MaskQCIssue.severity,
                effective_status,
                func.count(MaskQCIssue.id),
            )
            .where(MaskQCIssue.last_seen_run_id == run.id)
            .group_by(MaskQCIssue.severity, effective_status)
        )
    ).all()
    summary = dict(run.summary or {})
    summary["issue_count"] = sum(int(count) for _, _, count in rows)
    summary["issues_by_severity"] = {
        severity: sum(int(count) for sev, _, count in rows if sev == severity)
        for severity in ("blocker", "warning", "info")
    }
    summary["issues_by_status"] = {
        status: sum(int(count) for _, row_status, count in rows if row_status == status)
        for status in ("open", "resolved", "wont_fix", "stale")
    }
    return summary


async def fail_mask_qc_run(
    db: AsyncSession,
    *,
    run: MaskQCRun,
    error: str,
) -> None:
    """Move the domain run and generic job to failed in one transaction."""

    now = datetime.now(timezone.utc)
    if run.status not in {"completed", "cancelled"}:
        run.status = "failed"
        run.error_message = error[:4000]
        run.completed_at = now
    if run.async_job_id is not None:
        await async_job_svc.mark_failed(db, run.async_job_id, error=error)
        await notify_job_terminal(db, job_id=run.async_job_id)


async def dispatch_mask_qc_run(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    async_job_id: uuid.UUID,
    dispatcher: QualityJobDispatcher,
) -> None:
    """Dispatch only after the caller committed the paired run/job rows."""

    run = await db.get(MaskQCRun, run_id)
    job = await async_job_svc.get_job(db, async_job_id)
    if run is None or job is None or not job.celery_task_id:
        raise MaskQCError(
            status_code=500,
            reason="mask_qc_dispatch_contract_invalid",
            message="Mask QC dispatch contract is incomplete",
        )
    try:
        dispatcher.dispatch_run("mask_qc", run.id, execution_id=job.celery_task_id)
    except Exception as exc:
        await db.rollback()
        run = (
            await db.execute(
                select(MaskQCRun).where(MaskQCRun.id == run_id).with_for_update()
            )
        ).scalar_one()
        error = f"Mask QC dispatch failed: {type(exc).__name__}: {exc}"
        await fail_mask_qc_run(db, run=run, error=error)
        await db.commit()
        raise MaskQCError(
            status_code=503,
            reason="mask_qc_dispatch_failed",
            message="Mask QC could not be queued",
        ) from exc


async def reconcile_job_terminal(db: AsyncSession, terminal: JobTerminal) -> None:
    """Reconcile the QC ledger without committing the caller's transaction."""
    run = (
        await db.execute(
            select(MaskQCRun)
            .where(MaskQCRun.async_job_id == terminal.job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.status not in {"pending", "running"}:
        return
    if terminal.cancelled:
        run.status = "cancelled"
    else:
        run.status = "failed"
        run.error_message = (terminal.error or "")[:4000]
    run.completed_at = datetime.now(timezone.utc)


async def resolve_repair_issue(
    db: AsyncSession,
    *,
    issue_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Resolve a QC issue atomically with its accepted repair mutation."""
    issue = await db.get(MaskQCIssue, issue_id)
    if issue is None:
        return
    issue.status = "resolved"
    issue.resolved_by_id = actor_id
    issue.resolved_at = datetime.now(timezone.utc)
