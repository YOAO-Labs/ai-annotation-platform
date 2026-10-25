"""3D Scene 跨帧任务的快照、单飞与结果对账 helper。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.services.labeling_core_read_relations import annotation_row


CONTRACT_VERSION = 1
JOB_KIND = "point_cloud_cross_frame"
ACTIVE_STATUSES = ("pending", "running")
RETRYABLE_ITEM_STATUSES = frozenset({"failed", "stale"})
MAX_SOURCE_ANNOTATIONS = 500


async def load_source_annotations(
    db: AsyncSession,
    *,
    source_task_id: uuid.UUID,
    scope: str,
    annotation_ids: list[uuid.UUID],
) -> list[Annotation]:
    stmt = (
        select(annotation_row)
        .where(annotation_row.task_id == source_task_id)
        .where(annotation_row.is_active.is_(True))
        .where(annotation_row.geometry["type"].astext == "box_3d")
    )
    if scope == "selected":
        stmt = stmt.where(annotation_row.id.in_(annotation_ids))
    rows = list(
        (
            await db.execute(
                stmt.order_by(annotation_row.created_at, annotation_row.id).limit(
                    MAX_SOURCE_ANNOTATIONS + 1
                )
            )
        ).scalars()
    )
    if len(rows) > MAX_SOURCE_ANNOTATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"cross-frame job cannot exceed {MAX_SOURCE_ANNOTATIONS} source annotations",
        )
    if scope == "selected":
        by_id = {row.id: row for row in rows}
        missing = [
            str(annotation_id)
            for annotation_id in annotation_ids
            if annotation_id not in by_id
        ]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"box_3d annotations not found in source task: {', '.join(missing)}",
            )
        rows = [by_id[annotation_id] for annotation_id in annotation_ids]
    if not rows:
        raise HTTPException(
            status_code=422, detail="source task has no active box_3d annotations"
        )
    return rows


def snapshot_sources(rows: Iterable[Annotation]) -> list[dict[str, Any]]:
    return [
        {
            "annotation_id": str(row.id),
            "version": int(row.version or 1),
            "track_id": row.track_id,
        }
        for row in rows
    ]


def singleflight_key(payload: dict[str, Any]) -> str:
    frozen = {
        key: payload.get(key)
        for key in (
            "contract_version",
            "source_task_id",
            "scene_id",
            "operation",
            "scope",
            "direction",
            "start_frame",
            "end_frame",
            "conflict_policy",
            "sources",
            "targets",
            "parent_job_id",
        )
    }
    encoded = json.dumps(
        frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "stale": 0,
        "cancelled": 0,
    }
    created_annotation_count = 0
    for item in items:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
        created_annotation_count += int(item.get("created_count") or 0)
    return {
        "contract_version": CONTRACT_VERSION,
        "total_count": len(items),
        "success_count": counts["success"],
        "skipped_count": counts["skipped"],
        "failed_count": counts["failed"],
        "stale_count": counts["stale"],
        "cancelled_count": counts["cancelled"],
        "created_annotation_count": created_annotation_count,
        "items": items,
    }


def cancelled_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Retain each frozen target in the result of a pending cancellation."""
    targets = payload.get("targets") or []
    items = [
        {
            "frame_index": int(target.get("frame_index") or 0),
            "task_id": target.get("task_id"),
            "status": "cancelled",
            "created_count": 0,
            "skipped_count": 0,
            "reason": "cancelled_by_user",
        }
        for target in targets
        if isinstance(target, dict)
    ]
    return summarize_items(items)


def retryable_targets(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = result.get("items")
    if not isinstance(items, list):
        return []
    targets: list[dict[str, Any]] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("status") not in RETRYABLE_ITEM_STATUSES
        ):
            continue
        task_id = item.get("task_id")
        frame_index = item.get("frame_index")
        if not isinstance(task_id, str) or not isinstance(frame_index, int):
            continue
        try:
            uuid.UUID(task_id)
        except ValueError:
            continue
        targets.append(
            {
                "frame_index": frame_index,
                "task_id": task_id,
                "preflight_state": "ready",
            }
        )
    return targets
