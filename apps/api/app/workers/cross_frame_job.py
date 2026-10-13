"""3D Scene 跨帧传播 worker。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.enums import ProjectRole
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.task import Task
from app.db.models.user import User
from app.services import async_job as async_job_svc
from app.services.annotation import AnnotationService
from app.services.async_job_notify import notify_job_terminal
from app.services.cross_frame_job import JOB_KIND, summarize_items
from app.workers.celery_app import celery_app


log = logging.getLogger(__name__)


async def _resolve_task_access(
    db: AsyncSession, task: Task, actor: User, *, lock: bool = False
):
    """Resolve current project access for a task's actual project, fail closed."""

    from app.db.models.project import Project
    from app.services.project_access import resolve_project_access

    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="task_missing")
    try:
        return await resolve_project_access(
            db, user=actor, project=project, lock_membership=lock
        )
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="permission_changed") from exc


async def _review_task_authority(db: AsyncSession, task: Task, actor: User) -> None:
    """Assert current project manager/reviewer authority for review-state work."""

    access = await _resolve_task_access(db, task, actor)
    if access.is_manager or access.project_role == ProjectRole.REVIEWER.value:
        return
    raise HTTPException(status_code=403, detail="permission_changed")


@celery_app.task(bind=True, name="app.workers.cross_frame_job.run_cross_frame_job")
def run_cross_frame_job(self, job_id: str) -> None:
    asyncio.run(
        _run_cross_frame_job(
            job_id=job_id,
            celery_task_id=getattr(self.request, "id", None),
        )
    )


def _preflight_item(target: dict[str, Any]) -> dict[str, Any] | None:
    state = str(target.get("preflight_state") or "")
    if state == "ready":
        return None
    return {
        "frame_index": int(target.get("frame_index") or 0),
        "task_id": None,
        "status": "skipped",
        "created_count": 0,
        "skipped_count": 0,
        "reason": state
        if state in {"missing", "unavailable", "not_editable"}
        else "invalid_target",
    }


def _terminal_item(
    target: dict[str, Any],
    *,
    status: str,
    reason: str | None,
    created_count: int = 0,
    skipped_count: int = 0,
) -> dict[str, Any]:
    return {
        "frame_index": int(target["frame_index"]),
        "task_id": target.get("task_id"),
        "status": status,
        "created_count": created_count,
        "skipped_count": skipped_count,
        "reason": reason,
    }


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
            return detail["reason"][:80]
        if isinstance(detail, str):
            if "跨 scene" in detail:
                return "scene_mismatch"
            if "不存在" in detail or "not found" in detail:
                return "source_or_target_missing"
            if "不支持" in detail:
                return "unsupported_geometry"
    return "propagate_failed"


async def _cancel_requested(db: AsyncSession, job_id: uuid.UUID) -> bool:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return True
    await db.refresh(job)
    return job.status == AsyncJobStatus.CANCELLED.value or bool(
        (job.payload or {}).get("cancel_requested")
    )


async def _persist_progress(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    ordered_items: list[dict[str, Any]],
    total: int,
) -> None:
    result = summarize_items(ordered_items)
    pct = round((len(ordered_items) / max(1, total)) * 100)
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status != AsyncJobStatus.CANCELLED.value:
        job.result = result
        job.progress_pct = max(0, min(100, pct))
    await db.commit()


async def _finish_cancelled(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    result: dict[str, Any],
) -> None:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    job.status = AsyncJobStatus.CANCELLED.value
    job.completed_at = datetime.now(timezone.utc)
    job.progress_pct = 100
    job.result = result
    await notify_job_terminal(db, job_id=job.id)
    await db.commit()


async def execute_cross_frame_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    celery_task_id: str | None = None,
) -> None:
    await async_job_svc.mark_running(db, job_id, celery_task_id=celery_task_id)
    await db.commit()
    job = await db.get(AsyncJob, job_id)
    if job is None or job.kind != JOB_KIND:
        return
    payload = job.payload or {}
    targets = payload.get("targets")
    sources = payload.get("sources")
    if not isinstance(targets, list) or not isinstance(sources, list):
        raise ValueError("invalid cross-frame job snapshot")
    source_task_id = uuid.UUID(str(payload["source_task_id"]))
    source_ids = [uuid.UUID(str(source["annotation_id"])) for source in sources]
    expected_versions = {
        uuid.UUID(str(source["annotation_id"])): int(source["version"])
        for source in sources
    }
    actor = await db.get(User, job.user_id) if job.user_id else None
    if actor is None or not actor.is_active:
        raise ValueError("cross-frame job actor not found")

    completed_by_frame: dict[int, dict[str, Any]] = {}
    for target in targets:
        item = _preflight_item(target)
        if item is not None:
            completed_by_frame[int(target["frame_index"])] = item
    ordered_items = [
        completed_by_frame[int(target["frame_index"])]
        for target in targets
        if int(target["frame_index"]) in completed_by_frame
    ]
    if ordered_items:
        await _persist_progress(
            db, job_id=job_id, ordered_items=ordered_items, total=len(targets)
        )

    source_stale = False
    # Stable actor id: a per-frame rollback expires the ORM instance, so the
    # bounded account lock must use a captured id rather than actor.id.
    actor_user_id = actor.id
    for target_index, target in enumerate(targets):
        frame_index = int(target["frame_index"])
        if frame_index in completed_by_frame:
            continue
        if await _cancel_requested(db, job_id):
            for remaining in targets[target_index:]:
                remaining_frame = int(remaining["frame_index"])
                if remaining_frame in completed_by_frame:
                    continue
                completed_by_frame[remaining_frame] = _terminal_item(
                    remaining,
                    status="cancelled",
                    reason="cancelled_by_user",
                )
            final_items = [
                completed_by_frame[int(row["frame_index"])] for row in targets
            ]
            await _finish_cancelled(
                db,
                job_id=job_id,
                result=summarize_items(final_items),
            )
            return
        if source_stale:
            completed_by_frame[frame_index] = _terminal_item(
                target, status="stale", reason="source_version_changed"
            )
            ordered_items = [
                completed_by_frame[int(row["frame_index"])]
                for row in targets
                if int(row["frame_index"]) in completed_by_frame
            ]
            await _persist_progress(
                db, job_id=job_id, ordered_items=ordered_items, total=len(targets)
            )
            continue

        target_task_id = uuid.UUID(str(target["task_id"]))
        try:
            # 每帧进入写事务前恢复 actor：上一帧 rollback 会过期
            # Session 内对象，同时这也让运行中被停用的账号不再继续写入。
            # Account-first: bounded FOR SHARE NOWAIT on the acting account
            # before membership/Task locks so a concurrent global disable cannot
            # race the frame commit.  A busy account rolls back to a terminal
            # item via the outer handler instead of a wait cycle.
            from app.services.project_write_guard import (
                lock_actor_account,
                lock_actor_scope,
            )

            try:
                await lock_actor_account(db, actor_user_id)
            except HTTPException as exc:
                raise RuntimeError("permission_changed") from exc
            await db.refresh(actor)
            if not actor.is_active:
                raise RuntimeError("permission_changed")
            lock_ids = sorted({source_task_id, target_task_id}, key=str)
            # Account/membership-before-resource: bounded share lock the actor's
            # membership for each involved project before the Task locks, so the
            # final-write guard's membership re-read cannot wait after a Task.

            member_project_ids = set(
                (
                    await db.execute(
                        select(Task.project_id).where(Task.id.in_(lock_ids))
                    )
                ).scalars()
            )
            for member_project_id in member_project_ids:
                await lock_actor_scope(db, actor.id, member_project_id)
            locked_tasks = list(
                (
                    await db.execute(
                        select(Task)
                        .where(Task.id.in_(lock_ids))
                        .order_by(Task.id)
                        .with_for_update(nowait=True)
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            tasks_by_id = {row.id: row for row in locked_tasks}
            source_task = tasks_by_id.get(source_task_id)
            target_task = tasks_by_id.get(target_task_id)
            if source_task is None or target_task is None:
                raise RuntimeError("task_missing")
            try:
                from app.api.v1.tasks._shared import _assert_task_visible

                await _assert_task_visible(db, source_task, actor)
                await _assert_task_visible(db, target_task, actor)
            except HTTPException as exc:
                raise RuntimeError("permission_changed") from exc
            if source_task.status == "completed":
                raise RuntimeError("source_task_locked")
            if source_task.status == "review":
                # Reading a review-state source still needs review authority.
                try:
                    await _review_task_authority(db, source_task, actor)
                except HTTPException as exc:
                    raise RuntimeError("source_task_locked") from exc
            if target_task.status == "completed":
                raise RuntimeError("target_task_locked")
            # Phase-aware write: pending/in-progress requires the effective
            # annotator (or manager); review additionally validates the frozen
            # contributor evidence.  Reviewer/viewer cannot write annotation
            # work.
            try:
                from app.services.project_write_guard import (
                    assert_phase_write_allowed,
                )

                await assert_phase_write_allowed(
                    db, target_task, actor, action="propagate"
                )
            except HTTPException as exc:
                raise RuntimeError("target_task_locked") from exc

            source_rows = list(
                (
                    await db.execute(
                        select(Annotation)
                        .where(Annotation.id.in_(source_ids))
                        .order_by(Annotation.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            source_by_id = {row.id: row for row in source_rows}
            stale_ids = [
                source_id
                for source_id in source_ids
                if source_id not in source_by_id
                or not source_by_id[source_id].is_active
                or source_by_id[source_id].task_id != source_task_id
                or (source_by_id[source_id].geometry or {}).get("type") != "box_3d"
                or int(source_by_id[source_id].version or 1)
                != expected_versions[source_id]
            ]
            if stale_ids:
                source_stale = True
                await db.rollback()
                completed_by_frame[frame_index] = _terminal_item(
                    target, status="stale", reason="source_version_changed"
                )
            else:
                track_ids = [row.track_id for row in source_rows if row.track_id]
                existing_tracks = set()
                if track_ids:
                    existing_tracks = set(
                        (
                            await db.execute(
                                select(Annotation.track_id)
                                .where(Annotation.task_id == target_task_id)
                                .where(Annotation.is_active.is_(True))
                                .where(Annotation.geometry["type"].astext == "box_3d")
                                .where(Annotation.track_id.in_(track_ids))
                            )
                        ).scalars()
                    )
                propagate_ids = [
                    source_id
                    for source_id in source_ids
                    if source_by_id[source_id].track_id not in existing_tracks
                ]
                skipped_count = len(source_ids) - len(propagate_ids)
                if not propagate_ids:
                    await db.commit()
                    completed_by_frame[frame_index] = _terminal_item(
                        target,
                        status="skipped",
                        reason="all_tracks_exist",
                        skipped_count=skipped_count,
                    )
                else:
                    results, _ = await AnnotationService(db).propagate_batch(
                        source_task_id=source_task_id,
                        target_task_id=target_task_id,
                        annotation_ids=propagate_ids,
                        user_id=actor.id,
                    )
                    for source_id in source_ids:
                        expected_versions[source_id] = int(
                            source_by_id[source_id].version or 1
                        )
                    await db.commit()
                    completed_by_frame[frame_index] = _terminal_item(
                        target,
                        status="success",
                        reason=None,
                        created_count=len(results),
                        skipped_count=skipped_count,
                    )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            reason = (
                str(exc)
                if str(exc)
                in {
                    "task_missing",
                    "source_task_locked",
                    "target_task_locked",
                    "permission_changed",
                }
                else _failure_reason(exc)
            )
            log.exception(
                "cross-frame target failed job=%s frame=%s", job_id, frame_index
            )
            completed_by_frame[frame_index] = _terminal_item(
                target, status="failed", reason=reason
            )

        ordered_items = [
            completed_by_frame[int(row["frame_index"])]
            for row in targets
            if int(row["frame_index"]) in completed_by_frame
        ]
        await _persist_progress(
            db, job_id=job_id, ordered_items=ordered_items, total=len(targets)
        )

    final_items = [completed_by_frame[int(target["frame_index"])] for target in targets]
    result = summarize_items(final_items)
    await async_job_svc.update_progress(db, job_id, 100)
    if result["failed_count"] or result["stale_count"]:
        await async_job_svc.mark_failed(
            db,
            job_id,
            error="one or more cross-frame targets require attention",
            result=result,
        )
    else:
        await async_job_svc.mark_complete(db, job_id, result=result)
    await notify_job_terminal(db, job_id=job_id)
    await db.commit()


async def _run_cross_frame_job(*, job_id: str, celery_task_id: str | None) -> None:
    job_uuid = uuid.UUID(job_id)
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            try:
                await execute_cross_frame_job(
                    db, job_id=job_uuid, celery_task_id=celery_task_id
                )
            except Exception:  # noqa: BLE001
                await db.rollback()
                log.exception("cross-frame worker failed job=%s", job_uuid)
                await async_job_svc.mark_failed(
                    db, job_uuid, error="cross-frame worker failed"
                )
                await notify_job_terminal(db, job_id=job_uuid)
                await db.commit()
                raise
    finally:
        await engine.dispose()
