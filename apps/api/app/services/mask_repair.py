from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJobKind
from app.db.models.dataset import VideoSegment
from app.db.models.mask_qc import MaskQCIssue, MaskQCRun
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.mask_review_scope import MaskReviewScope
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.mask_mutation import (
    MaskExpectedVersion,
    MaskMutationCommitRequest,
    MaskMutationReport,
    MaskMutationScope,
    MaskUpdateMutation,
)
from app.schemas.mask_repair import (
    MaskRepairAction,
    MaskRepairBatchOut,
    MaskRepairDryRunRequest,
    MaskRepairDryRunResponse,
    MaskRepairPlanItem,
    MaskRepairPlanSummary,
)
from app.services import async_job as async_job_svc
from app.services.mask_mutation import MaskMutationService, scope_fingerprint
from app.services.mask_qc.config import load_mask_qc_config, mask_qc_config_digest
from app.services.mask_qc.service import effective_issue_status
from app.services.mask_qc.topology import rle_and_not, rle_or, rle_xor
from app.services.raster_mask_storage import build_rle_reference, load_coco_rle
from app.services.project_access import is_privileged_for_project
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.video_tracks import (
    remove_frame_from_outside_ranges,
    resolve_mask_track_state_at_frame,
)
from app.utils.raster_mask_rle import coco_rle_area


PLAN_TTL = timedelta(minutes=15)
ROLLBACK_TTL = timedelta(days=7)
MAX_SHARD_MUTATIONS = 100

_DETERMINISTIC_KINDS = {
    "delete_small_islands",
    "fill_small_holes",
    "resolve_same_class_overlap",
}
_ISSUE_CODES_BY_KIND = {
    "delete_small_islands": {"small_island"},
    "fill_small_holes": {"small_hole"},
    "resolve_same_class_overlap": {"same_class_overlap"},
    "rerun_local_sam": {
        "near_empty_mask",
        "touches_border",
        "narrow_bridge",
        "boundary_noise",
        "derived_geometry_mismatch",
        "same_class_overlap",
        "cross_class_overlap",
    },
    "rerun_tracker": {"flicker", "drift"},
}


class MaskRepairError(RuntimeError):
    def __init__(
        self, *, status_code: int, reason: str, message: str, **detail: Any
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **detail}


def canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def result_digest(batch: MaskRepairBatch) -> str:
    return canonical_digest(batch.result_json or {})


def batch_out(batch: MaskRepairBatch) -> MaskRepairBatchOut:
    return MaskRepairBatchOut(
        id=batch.id,
        project_id=batch.project_id,
        async_job_id=batch.async_job_id,
        rollback_async_job_id=batch.rollback_async_job_id,
        status=batch.status,
        plan_digest=batch.plan_digest,
        plan=batch.plan_json or {},
        result=batch.result_json or {},
        result_digest=result_digest(batch),
        receipt_expires_at=batch.receipt_expires_at,
        rollback_expires_at=batch.rollback_expires_at,
        created_at=batch.created_at,
        completed_at=batch.completed_at,
        rolled_back_at=batch.rolled_back_at,
    )


def _token_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode()).hexdigest()


def _skip_item(
    action: MaskRepairAction,
    *,
    issue: MaskQCIssue | None,
    code: str,
    detail: str,
) -> dict[str, Any]:
    return MaskRepairPlanItem(
        issue_id=action.issue_id,
        task_id=issue.task_id if issue else None,
        annotation_ids=[issue.annotation_id] if issue else [],
        kind=action.kind,
        frame_index=issue.frame_start if issue else None,
        source_versions=dict(issue.source_versions or {}) if issue else {},
        skip_code=code,
        skip_detail=detail,
    ).model_dump(mode="json")


def _mask_reference(annotation: Annotation, frame_index: int | None) -> dict | None:
    geometry = annotation.geometry or {}
    if geometry.get("type") == "raster_mask":
        reference = geometry.get("mask")
        return dict(reference) if isinstance(reference, dict) else None
    if geometry.get("type") != "video_track_mask" or frame_index is None:
        return None
    resolved = resolve_mask_track_state_at_frame(geometry, frame_index)
    reference = resolved.get("mask")
    return dict(reference) if isinstance(reference, dict) else None


def _updated_geometry(
    annotation: Annotation,
    *,
    frame_index: int | None,
    result_reference: dict[str, Any],
) -> dict[str, Any]:
    geometry = deepcopy(annotation.geometry or {})
    if geometry.get("type") == "raster_mask":
        geometry["mask"] = result_reference
        return geometry
    assert frame_index is not None
    previous = next(
        (
            item
            for item in geometry.get("keyframes") or []
            if int(item.get("frame_index", -1)) == frame_index
        ),
        None,
    )
    keyframe: dict[str, Any] = {
        "frame_index": frame_index,
        "mask": result_reference,
        "source": "manual",
        "occluded": bool((previous or {}).get("occluded", False)),
    }
    if isinstance((previous or {}).get("attributes"), dict):
        keyframe["attributes"] = deepcopy(previous["attributes"])
    geometry["keyframes"] = sorted(
        [
            deepcopy(item)
            for item in geometry.get("keyframes") or []
            if int(item.get("frame_index", -1)) != frame_index
        ]
        + [keyframe],
        key=lambda item: int(item["frame_index"]),
    )
    geometry["outside"] = remove_frame_from_outside_ranges(
        geometry.get("outside"), frame_index
    )
    return geometry


async def _video_segment(
    db: AsyncSession,
    *,
    task: Task,
    frame_index: int,
    requested_id: uuid.UUID | None,
) -> VideoSegment | None:
    query = select(VideoSegment).where(
        VideoSegment.dataset_item_id == task.dataset_item_id,
        VideoSegment.start_frame <= frame_index,
        VideoSegment.end_frame >= frame_index,
    )
    if requested_id is not None:
        query = query.where(VideoSegment.id == requested_id)
    return (
        (await db.execute(query.order_by(VideoSegment.segment_index.asc())))
        .scalars()
        .first()
    )


async def _has_current_reviewed_scope(
    db: AsyncSession,
    *,
    annotation: Annotation,
    frame_index: int,
) -> bool:
    return (
        await db.execute(
            select(MaskReviewScope.id).where(
                MaskReviewScope.annotation_id == annotation.id,
                MaskReviewScope.result_annotation_version
                == int(annotation.version or 1),
                MaskReviewScope.frame_start <= frame_index,
                MaskReviewScope.frame_end >= frame_index,
            )
        )
    ).scalar_one_or_none() is not None


async def _issue_versions_current(db: AsyncSession, issue: MaskQCIssue) -> bool:
    source_versions = {
        str(key): int(value) for key, value in issue.source_versions.items()
    }
    if not source_versions:
        return False
    ids = [uuid.UUID(value) for value in source_versions]
    rows = list(
        (
            await db.execute(
                select(Annotation).where(
                    Annotation.id.in_(ids),
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                )
            )
        ).scalars()
    )
    return len(rows) == len(ids) and all(
        source_versions[str(row.id)] == int(row.version or 1) for row in rows
    )


async def _resolve_target(
    db: AsyncSession,
    *,
    issue: MaskQCIssue,
    action: MaskRepairAction,
    frame_index: int | None,
) -> tuple[Annotation | None, dict[str, Any] | None]:
    primary = await db.get(Annotation, issue.annotation_id)
    if action.kind != "resolve_same_class_overlap":
        return primary, None
    ids = sorted(set(issue.related_annotation_ids or []), key=str)
    if issue.annotation_id not in ids:
        ids.append(issue.annotation_id)
    rows = list(
        (
            await db.execute(
                select(Annotation).where(
                    Annotation.id.in_(ids),
                    Annotation.is_active.is_(True),
                    Annotation.was_cancelled.is_(False),
                )
            )
        ).scalars()
    )
    candidates: list[tuple[int, str, Annotation, dict[str, Any]]] = []
    for annotation in rows:
        reference = _mask_reference(annotation, frame_index)
        if reference is None:
            continue
        rle = await load_coco_rle(reference)
        candidates.append((coco_rle_area(rle), str(annotation.id), annotation, rle))
    if len(candidates) < 2:
        return None, None
    _area, _stable_id, target, target_rle = min(candidates)
    return target, target_rle


async def _plan_action(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
    action: MaskRepairAction,
    planned_annotations: set[uuid.UUID],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    issue = await db.get(MaskQCIssue, action.issue_id)
    if issue is None or issue.project_id != project.id:
        return _skip_item(
            action,
            issue=None,
            code="issue_not_found",
            detail="Mask QC issue does not exist in this project",
        ), None
    if (
        action.kind not in _ISSUE_CODES_BY_KIND
        or issue.code not in _ISSUE_CODES_BY_KIND[action.kind]
    ):
        return _skip_item(
            action,
            issue=issue,
            code="repair_kind_mismatch",
            detail="repair kind is not compatible with this issue",
        ), None
    if await effective_issue_status(db, issue) != "open":
        return _skip_item(
            action,
            issue=issue,
            code="issue_not_open",
            detail="issue is resolved, ignored, or stale",
        ), None
    if issue.run_id is not None:
        run = await db.get(MaskQCRun, issue.run_id)
        current_config_digest = mask_qc_config_digest(
            load_mask_qc_config(project.mask_qc_config)
        )
        if run is None or run.config_digest != current_config_digest:
            return _skip_item(
                action,
                issue=issue,
                code="blocker_policy_conflict",
                detail="Mask QC policy changed after this issue was produced",
            ), None
    if not await _issue_versions_current(db, issue):
        return _skip_item(
            action,
            issue=issue,
            code="version_conflict",
            detail="one or more issue source annotations changed",
        ), None
    if issue.region_mask_ref is None or issue.region_digest is None:
        return _skip_item(
            action,
            issue=issue,
            code="region_unavailable",
            detail="issue has no exact repair region",
        ), None

    task = await db.get(Task, issue.task_id)
    if task is None or task.project_id != project.id:
        return _skip_item(
            action,
            issue=issue,
            code="task_not_found",
            detail="issue task no longer exists",
        ), None
    if task.status == "completed":
        return _skip_item(
            action,
            issue=issue,
            code="task_locked",
            detail="completed task must be reopened before repair",
        ), None
    try:
        await TaskLockService(db).assert_write_allowed(task.id, actor.id)
    except TaskLockConflictError:
        return _skip_item(
            action,
            issue=issue,
            code="task_lock_conflict",
            detail="task is locked by another user",
        ), None

    frame_index = issue.frame_start
    if issue.frame_start != issue.frame_end:
        frame_index = None
    target, target_rle = await _resolve_target(
        db, issue=issue, action=action, frame_index=frame_index
    )
    if target is None or target.task_id != task.id:
        return _skip_item(
            action,
            issue=issue,
            code="annotation_not_found",
            detail="repair target is no longer available",
        ), None
    if target.id in planned_annotations:
        return _skip_item(
            action,
            issue=issue,
            code="annotation_already_planned",
            detail="one frozen plan may mutate an annotation only once",
        ), None
    if target.is_locked:
        return _skip_item(
            action,
            issue=issue,
            code="annotation_locked",
            detail="annotation is locked",
        ), None

    geometry_type = (target.geometry or {}).get("type")
    is_video = geometry_type == "video_track_mask"
    if is_video and frame_index is None:
        return _skip_item(
            action,
            issue=issue,
            code="single_frame_required",
            detail="video repair requires an exact single-frame issue",
        ), None
    segment: VideoSegment | None = None
    if is_video:
        assert frame_index is not None
        segment = await _video_segment(
            db,
            task=task,
            frame_index=frame_index,
            requested_id=action.segment_id,
        )
        if segment is None:
            return _skip_item(
                action,
                issue=issue,
                code="segment_lock_conflict",
                detail="video frame has no matching editable segment",
            ), None
        now = datetime.now(timezone.utc)
        if not is_privileged_for_project(actor, project) and (
            segment.locked_by != actor.id
            or segment.lock_expires_at is None
            or segment.lock_expires_at <= now
        ):
            return _skip_item(
                action,
                issue=issue,
                code="segment_lock_conflict",
                detail="video segment must be locked by the current reviewer",
            ), None
        if await _has_current_reviewed_scope(
            db, annotation=target, frame_index=frame_index
        ):
            return _skip_item(
                action,
                issue=issue,
                code="reviewed_scope_protected",
                detail="current reviewed region is protected",
            ), None
        resolved = resolve_mask_track_state_at_frame(target.geometry, frame_index)
        is_exact_manual = (
            resolved.get("state") == "exact" and resolved.get("source") == "manual"
        )
        if action.kind in _DETERMINISTIC_KINDS and is_exact_manual:
            return _skip_item(
                action,
                issue=issue,
                code="manual_keyframe_protected",
                detail="manual keyframes require an explicit editor decision",
            ), None
        if action.kind == "rerun_tracker" and not is_exact_manual:
            return _skip_item(
                action,
                issue=issue,
                code="manual_anchor_required",
                detail="Tracker rerun requires an exact manual correction frame",
            ), None

    public_item = MaskRepairPlanItem(
        issue_id=issue.id,
        task_id=task.id,
        annotation_ids=[target.id],
        kind=action.kind,
        frame_index=frame_index,
        source_versions=dict(issue.source_versions or {}),
        candidate_count=1 if action.kind in {"rerun_local_sam", "rerun_tracker"} else 0,
    )
    if action.kind not in _DETERMINISTIC_KINDS:
        if action.kind == "rerun_local_sam" and geometry_type != "raster_mask":
            public_item.skip_code = "sam_image_only"
            public_item.skip_detail = (
                "local SAM batch repair currently requires an image task"
            )
            public_item.candidate_count = 0
            return public_item.model_dump(mode="json"), None
        private = {
            "issue_id": str(issue.id),
            "task_id": str(task.id),
            "annotation_id": str(target.id),
            "kind": action.kind,
            "frame_index": frame_index,
            "source_version": int(target.version or 1),
            "class_name": target.class_name,
            "tool_unit_id": target.tool_unit_id,
            "region_bbox": issue.region_bbox,
            "region_mask_ref": issue.region_mask_ref,
            "action": action.model_dump(mode="json"),
            "status": "pending",
        }
        return public_item.model_dump(mode="json"), private

    source_reference = _mask_reference(target, frame_index)
    if source_reference is None:
        return _skip_item(
            action,
            issue=issue,
            code="mask_unavailable",
            detail="current Mask content is unavailable",
        ), None
    if target_rle is None:
        target_rle = await load_coco_rle(source_reference)
    region_rle = await load_coco_rle(issue.region_mask_ref)
    if action.kind in {"delete_small_islands", "resolve_same_class_overlap"}:
        result_rle = rle_and_not(target_rle, region_rle)
    else:
        result_rle = rle_or(target_rle, region_rle)
    changed_pixels = coco_rle_area(rle_xor(target_rle, result_rle))
    if changed_pixels == 0:
        return _skip_item(
            action,
            issue=issue,
            code="already_repaired",
            detail="repair region does not change current pixels",
        ), None
    if coco_rle_area(result_rle) == 0:
        return _skip_item(
            action,
            issue=issue,
            code="would_empty_mask",
            detail="batch repair never deletes the complete instance",
        ), None

    scope = MaskMutationScope(
        media="video" if is_video else "image",
        frame_index=frame_index if is_video else None,
        segment_id=segment.id if segment is not None else None,
        instance_filter="same_class",
        class_name=target.class_name,
    )
    members = await MaskMutationService(db)._lock_scope(
        task.id,
        scope,
        for_update=False,
    )
    fingerprint = scope_fingerprint(scope, members)
    expected_versions = [
        MaskExpectedVersion(
            annotation_id=annotation.id,
            version=int(annotation.version or 1),
        )
        for annotation in members
    ]
    result_reference = build_rle_reference(result_rle)
    result_geometry = _updated_geometry(
        target,
        frame_index=frame_index,
        result_reference=result_reference,
    )
    payload = MaskMutationCommitRequest(
        idempotency_key=f"mask-repair:{issue.id}",
        operation=action.kind,
        scope=scope,
        scope_fingerprint=fingerprint,
        expected_versions=expected_versions,
        mutations=[
            MaskUpdateMutation(
                kind="update",
                annotation_id=target.id,
                geometry=result_geometry,
            )
        ],
        report=MaskMutationReport(changed_pixels=changed_pixels),
    )
    public_item.changed_pixels = changed_pixels
    public_item.mutation_count = 1
    public_item.scope_fingerprint = fingerprint
    planned_annotations.add(target.id)
    private = {
        "issue_id": str(issue.id),
        "task_id": str(task.id),
        "annotation_id": str(target.id),
        "kind": action.kind,
        "frame_index": frame_index,
        "before_geometry": deepcopy(target.geometry),
        "before_version": int(target.version or 1),
        "result_rle": result_rle,
        "result_reference": result_reference,
        "payload": payload.model_dump(mode="json", exclude_none=True),
        "status": "pending",
    }
    return public_item.model_dump(mode="json"), private


def _build_shards(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_task: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.get("kind") not in _DETERMINISTIC_KINDS:
            continue
        by_task.setdefault(str(item["task_id"]), []).append(index)
    shards: list[dict[str, Any]] = []
    for task_id in sorted(by_task):
        indexes = by_task[task_id]
        for offset in range(0, len(indexes), MAX_SHARD_MUTATIONS):
            shard_indexes = indexes[offset : offset + MAX_SHARD_MUTATIONS]
            shards.append(
                {
                    "id": canonical_digest(
                        {"task_id": task_id, "item_indexes": shard_indexes}
                    )[:24],
                    "task_id": task_id,
                    "item_indexes": shard_indexes,
                    "status": "pending",
                    "operation_ids": [],
                }
            )
    return shards


async def create_repair_plan(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
    request: MaskRepairDryRunRequest,
) -> MaskRepairDryRunResponse:
    public_items: list[dict[str, Any]] = []
    executable_items: list[dict[str, Any]] = []
    planned_annotations: set[uuid.UUID] = set()
    for action in request.actions:
        try:
            public, private = await _plan_action(
                db,
                project=project,
                actor=actor,
                action=action,
                planned_annotations=planned_annotations,
            )
        except (KeyError, TypeError, ValueError) as exc:
            public = _skip_item(
                action,
                issue=await db.get(MaskQCIssue, action.issue_id),
                code="invalid_region",
                detail=str(exc),
            )
            private = None
        public_items.append(public)
        if private is not None:
            executable_items.append(private)

    shards = _build_shards(executable_items)
    summary = MaskRepairPlanSummary(
        action_count=len(request.actions),
        executable_count=len(executable_items),
        skipped_count=sum(item.get("skip_code") is not None for item in public_items),
        mutation_count=sum(
            int(item.get("mutation_count") or 0) for item in public_items
        ),
        candidate_count=sum(
            int(item.get("candidate_count") or 0) for item in public_items
        ),
        changed_pixels=sum(
            int(item.get("changed_pixels") or 0) for item in public_items
        ),
        shard_count=len(shards),
    )
    plan = {
        "schema_version": 1,
        "items": executable_items,
        "public_items": public_items,
        "shards": shards,
        "summary": summary.model_dump(mode="json"),
    }
    digest = canonical_digest(plan)
    receipt = f"mrp_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(timezone.utc) + PLAN_TTL
    batch = MaskRepairBatch(
        project_id=project.id,
        requested_by_id=actor.id,
        token_hash=_token_hash(receipt),
        status="planned",
        plan_digest=digest,
        request_json=request.model_dump(mode="json"),
        plan_json=plan,
        result_json={},
        receipt_expires_at=expires_at,
    )
    db.add(batch)
    await db.flush()
    return MaskRepairDryRunResponse(
        receipt=receipt,
        plan_digest=digest,
        expires_at=expires_at,
        items=[MaskRepairPlanItem.model_validate(item) for item in public_items],
        summary=summary,
    )


async def execute_repair_plan(
    db: AsyncSession,
    *,
    project: Project,
    actor: User,
    receipt: str,
    plan_digest: str,
) -> tuple[MaskRepairBatch, bool]:
    batch = (
        await db.execute(
            select(MaskRepairBatch)
            .where(MaskRepairBatch.token_hash == _token_hash(receipt))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None or batch.project_id != project.id:
        raise MaskRepairError(
            status_code=404,
            reason="repair_plan_not_found",
            message="repair plan was not found",
        )
    if batch.requested_by_id != actor.id:
        raise MaskRepairError(
            status_code=403,
            reason="repair_plan_owner_mismatch",
            message="repair plan belongs to another reviewer",
        )
    if batch.receipt_expires_at <= datetime.now(timezone.utc):
        raise MaskRepairError(
            status_code=410,
            reason="repair_receipt_expired",
            message="repair receipt has expired",
        )
    if (
        batch.plan_digest != plan_digest
        or canonical_digest(batch.plan_json) != plan_digest
    ):
        raise MaskRepairError(
            status_code=409,
            reason="repair_plan_digest_conflict",
            message="repair plan digest does not match",
        )
    if batch.status != "planned":
        if batch.status in {"pending", "running", "completed", "partial"}:
            return batch, False
        raise MaskRepairError(
            status_code=409,
            reason="repair_plan_not_executable",
            message="repair plan is not executable in its current state",
            status=batch.status,
        )
    if not (batch.plan_json or {}).get("items"):
        raise MaskRepairError(
            status_code=422,
            reason="repair_plan_empty",
            message="repair plan contains no executable actions",
        )
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.MASK_REPAIR.value,
        project_id=project.id,
        user_id=actor.id,
        payload={"mask_repair_batch_id": str(batch.id)},
    )
    batch.async_job_id = job.id
    batch.status = "pending"
    await db.flush()
    return batch, True


async def request_repair_rollback(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    actor: User,
    expected_result_digest: str,
) -> MaskRepairBatch:
    batch = (
        await db.execute(
            select(MaskRepairBatch)
            .where(MaskRepairBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        raise MaskRepairError(
            status_code=404,
            reason="repair_batch_not_found",
            message="repair batch was not found",
        )
    if result_digest(batch) != expected_result_digest:
        raise MaskRepairError(
            status_code=409,
            reason="repair_result_digest_conflict",
            message="repair result changed; refresh before rollback",
        )
    now = datetime.now(timezone.utc)
    if batch.status not in {"completed", "partial"}:
        raise MaskRepairError(
            status_code=409,
            reason="repair_batch_not_rollbackable",
            message="repair batch is not rollbackable",
            status=batch.status,
        )
    if batch.rollback_expires_at is None or batch.rollback_expires_at <= now:
        raise MaskRepairError(
            status_code=410,
            reason="repair_rollback_expired",
            message="repair rollback retention has expired",
        )
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.MASK_REPAIR_ROLLBACK.value,
        project_id=batch.project_id,
        user_id=actor.id,
        payload={"mask_repair_batch_id": str(batch.id)},
    )
    batch.rollback_async_job_id = job.id
    batch.status = "rolling_back"
    await db.flush()
    return batch


async def resume_repair_batch(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    actor: User,
) -> MaskRepairBatch:
    batch = (
        await db.execute(
            select(MaskRepairBatch)
            .where(MaskRepairBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        raise MaskRepairError(
            status_code=404,
            reason="repair_batch_not_found",
            message="repair batch was not found",
        )
    if batch.status not in {"failed", "partial"}:
        raise MaskRepairError(
            status_code=409,
            reason="repair_batch_not_resumable",
            message="only failed or partial repair batches can be resumed",
            status=batch.status,
        )
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.MASK_REPAIR.value,
        project_id=batch.project_id,
        user_id=actor.id,
        payload={"mask_repair_batch_id": str(batch.id), "resumed": True},
    )
    batch.async_job_id = job.id
    batch.requested_by_id = actor.id
    batch.status = "pending"
    await db.flush()
    return batch


async def dispatch_repair_batch(batch_id: uuid.UUID, *, rollback: bool = False) -> str:
    try:
        from app.workers.mask_repair import run_mask_repair, rollback_mask_repair

        task = rollback_mask_repair if rollback else run_mask_repair
        result = task.apply_async(args=[str(batch_id)], queue="media")
        return str(result.id)
    except Exception as exc:
        raise MaskRepairError(
            status_code=503,
            reason="repair_dispatch_failed",
            message="Mask repair worker dispatch failed",
            retryable=True,
        ) from exc


def translate_http_exception(exc: HTTPException) -> MaskRepairError:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return MaskRepairError(
        status_code=exc.status_code,
        reason=str(detail.get("reason") or "candidate_dispatch_failed"),
        message=str(detail.get("message") or exc.detail),
    )


__all__ = [
    "MAX_SHARD_MUTATIONS",
    "ROLLBACK_TTL",
    "MaskRepairError",
    "batch_out",
    "canonical_digest",
    "create_repair_plan",
    "dispatch_repair_batch",
    "execute_repair_plan",
    "request_repair_rollback",
    "resume_repair_batch",
    "result_digest",
]
