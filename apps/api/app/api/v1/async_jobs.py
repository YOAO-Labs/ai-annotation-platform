"""v0.10.16 · 统一异步任务表 API（ROADMAP §1.7）。

GET /async-jobs        列表（仅 owner 可见，super_admin 可见全部）
GET /async-jobs/{id}   详情
POST /async-jobs/{id}/cancel  软取消
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole, ProjectRole
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.project import Project
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.user import User
from app.deps import get_current_user, get_db, assert_request_scopes
from app.services.async_job_terminal import reconcile_domain_terminal
from app.services import dataset_source_access as source_access
from app.services import dataset_quality as quality_svc
from app.services import dataset_quality_review as review_svc
from app.services.project_aggregates import project_scope_clause
from app.schemas.async_job import (
    AsyncJobListResponse,
    AsyncJobOut,
    AsyncJobRetryFailedResponse,
)

router = APIRouter()
log = logging.getLogger(__name__)

# v0.10.51 · batch_predict 支持协作取消；其它长任务保持软取消。
CANCELLABLE_KINDS = {
    "export",
    "sample_evaluation",
    "training_dataset",
    "batch_predict",
    "predictions_import",
    "audit_archive",
    "dataset_import",
    "dataset_scan",
    "dataset_registration",
    "storage_usage",
    "managed_original_verification",
    "dataset_quality",
    "dataset_quality_review",
    "mask_qc",
    "mask_repair",
    "mask_format_import",
    "point_cloud_cross_frame",
    "point_cloud_quality",
}
RETRY_FAILED_KINDS = {"batch_predict"}
#: Export jobs carry a signed download URL in ``result``; every read path must
#: apply the project export capability before exposing them.
EXPORT_JOB_KIND = "export"

AsyncJobStatusParam = Literal["pending", "running", "completed", "failed", "cancelled"]


def _build_async_job_query(
    *,
    current_user: User,
    status: list[AsyncJobStatusParam] | None,
    kind: list[str] | None,
    project_id: uuid.UUID | None,
    search: str | None,
):
    """Build the one scoped async-job relation used by page and count queries."""
    query = select(AsyncJob)

    if current_user.role != PlatformRole.SUPER_ADMIN.value:
        # Restrict to the caller's own jobs *and* a project they may currently
        # access.  Membership/ownership is resolved in SQL (one correlated
        # subquery, no per-job lookup) and a non-administrative owner gets only
        # the membership arm.
        project_scope = project_scope_clause(
            current_user, AsyncJob.project_id, project_roles=()
        )
        # Export rows carry a signed download URL in ``result``.  The list and
        # count endpoints must apply the same export capability as the detail
        # endpoint, otherwise a downgraded account could still obtain the URL.
        export_scope = project_scope_clause(
            current_user,
            AsyncJob.project_id,
            project_roles=(ProjectRole.REVIEWER.value,),
        )
        query = query.where(
            or_(
                source_access.job_scope_clause(current_user),
                quality_svc.job_scope_clause(current_user),
                review_svc.job_scope_clause(current_user),
                and_(
                    AsyncJob.kind.not_in(
                        [
                            "storage_usage",
                            "managed_original_verification",
                            "dataset_scan",
                            "dataset_registration",
                            "dataset_quality",
                            "dataset_quality_review",
                        ]
                    ),
                    AsyncJob.user_id == current_user.id,
                    or_(
                        and_(
                            AsyncJob.kind != EXPORT_JOB_KIND,
                            AsyncJob.project_id.is_(None),
                        ),
                        project_scope,
                    ),
                    or_(
                        AsyncJob.kind != EXPORT_JOB_KIND,
                        and_(AsyncJob.project_id.is_not(None), export_scope),
                    ),
                ),
            )
        )
    if status:
        query = query.where(AsyncJob.status.in_(status))
    if kind:
        query = query.where(AsyncJob.kind.in_(kind))
    if project_id:
        query = query.where(AsyncJob.project_id == project_id)

    search_text = search.strip() if search else ""
    if search_text:
        pattern = f"%{search_text}%"
        query = query.where(
            or_(
                AsyncJob.payload["prompt"].astext.ilike(pattern),
                AsyncJob.payload["batch_display_id"].astext.ilike(pattern),
                AsyncJob.payload["task_display_id"].astext.ilike(pattern),
                AsyncJob.payload["model_key"].astext.ilike(pattern),
                AsyncJob.payload["ml_backend_name"].astext.ilike(pattern),
                AsyncJob.payload["error_type"].astext.ilike(pattern),
            )
        )
    return query


async def _can_access_job(db: AsyncSession, *, job: AsyncJob, user: User) -> bool:
    """Reauthorize a job result against current project access.

    Original job ownership is not sufficient on its own: the account must still
    be able to see the job's project, and the reviewer scope-expansion paths use
    the *current* project role rather than a stale global account role.
    """
    if job.kind in {"storage_usage", "managed_original_verification"}:
        return user.is_active and user.role == PlatformRole.SUPER_ADMIN.value
    if job.kind in {"dataset_quality", "dataset_quality_review"}:
        try:
            actor = await source_access.require_manager(db, user.id)
        except HTTPException:
            return False
        return bool(
            await db.scalar(
                select(AsyncJob.id).where(
                    AsyncJob.id == job.id,
                    (
                        review_svc
                        if job.kind == "dataset_quality_review"
                        else quality_svc
                    ).job_scope_clause(actor),
                )
            )
        )
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return True
    if job.kind in {"dataset_scan", "dataset_registration"}:
        try:
            user = await source_access.require_manager(db, user.id)
        except HTTPException:
            return False
        return bool(
            await db.scalar(
                select(AsyncJob.id).where(
                    AsyncJob.id == job.id, source_access.job_scope_clause(user)
                )
            )
        )
    if job.project_id is None:
        # Annotation export has no legitimate global mode: a malformed/legacy
        # export row without an actual project must not expose its signed URL.
        if job.kind == EXPORT_JOB_KIND:
            return False
        return job.user_id == user.id
    project = await db.get(Project, job.project_id)
    if project is None:
        return False
    from app.services.project_access import ProjectCapability, resolve_project_access

    try:
        access = await resolve_project_access(db, user=user, project=project)
    except HTTPException:
        return False
    if access.is_manager:
        return True
    if (
        job.kind == EXPORT_JOB_KIND
        and ProjectCapability.EXPORT_ANNOTATIONS.value not in access.capabilities
    ):
        # A downgraded account must not retrieve a previously issued export URL.
        return False
    if job.user_id == user.id:
        return True
    if job.kind not in {"mask_qc", "point_cloud_quality"}:
        return False
    if access.project_role != ProjectRole.REVIEWER.value:
        return False
    raw_task_ids = ((job.payload or {}).get("scope") or {}).get("task_ids") or []
    try:
        task_ids = [uuid.UUID(str(value)) for value in raw_task_ids]
    except (TypeError, ValueError):
        return False
    if not task_ids:
        return False
    from app.api.v1.tasks._shared import _visible_task_ids

    return await _visible_task_ids(db, project, user, task_ids) == set(task_ids)


async def _to_async_job_out(
    db: AsyncSession,
    job: AsyncJob,
    project_cache: dict[uuid.UUID, Project | None] | None = None,
) -> AsyncJobOut:
    project: Project | None = None
    if job.project_id is not None:
        if project_cache is None:
            project = await db.get(Project, job.project_id)
        else:
            if job.project_id not in project_cache:
                project_cache[job.project_id] = await db.get(Project, job.project_id)
            project = project_cache[job.project_id]

    payload_project_display_id = None
    if isinstance(job.payload, dict):
        raw_display_id = job.payload.get("project_display_id")
        if isinstance(raw_display_id, str):
            payload_project_display_id = raw_display_id

    return AsyncJobOut.model_validate(job).model_copy(
        update={
            "project_display_id": (
                project.display_id if project else payload_project_display_id
            ),
            "project_name": project.name if project else None,
        }
    )


@router.get("/async-jobs", response_model=AsyncJobListResponse)
async def list_async_jobs(
    request: Request,
    status: list[AsyncJobStatusParam] | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncJobListResponse:
    """v0.10.16 · 当前用户可见 async_jobs 列表（super_admin 可见全部）。

    顺序：created_at DESC。前端铃铛走 polling，默认拉最近 50 行。
    """
    stmt = _build_async_job_query(
        current_user=current_user,
        status=status,
        kind=kind,
        project_id=project_id,
        search=search,
    )
    scopes = getattr(request.state, "api_key_scopes", None)
    if scopes is not None and "*" not in scopes and "datasets:read" not in scopes:
        stmt = stmt.where(
            AsyncJob.kind.not_in(
                [
                    "dataset_scan",
                    "dataset_registration",
                    "dataset_quality",
                    "dataset_quality_review",
                ]
            )
        )
    if scopes is not None and "*" not in scopes:
        stmt = stmt.where(
            AsyncJob.kind.not_in({"storage_usage", "managed_original_verification"})
        )
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())

    stmt = (
        stmt.order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar_one()
    project_cache: dict[uuid.UUID, Project | None] = {}

    return AsyncJobListResponse(
        items=[await _to_async_job_out(db, r, project_cache) for r in rows],
        total=total,
    )


@router.get("/async-jobs/{job_id}", response_model=AsyncJobOut)
async def get_async_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncJobOut:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    if not await _can_access_job(db, job=job, user=current_user):
        raise HTTPException(status_code=403, detail="not your job")
    if job.kind in {
        "dataset_scan",
        "dataset_registration",
        "dataset_quality",
        "dataset_quality_review",
    }:
        assert_request_scopes(request, "datasets:read")
    if job.kind in {"storage_usage", "managed_original_verification"}:
        assert_request_scopes(request, "*")
    return await _to_async_job_out(db, job)


@router.post("/async-jobs/{job_id}/cancel")
async def cancel_async_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """软取消。

    batch_predict 走协作取消：写 cancel_requested 标记并 revoke(terminate=False)，
    worker 在结果提交前或下一题边界落 cancelled 终态；video_tracker 仍走自身取消路径。
    """
    job = await db.get(AsyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    if not await _can_access_job(db, job=job, user=current_user):
        raise HTTPException(status_code=403, detail="not your job")
    if job.kind not in CANCELLABLE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind={job.kind} not cancellable",
        )
    if job.status not in {AsyncJobStatus.PENDING.value, AsyncJobStatus.RUNNING.value}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel terminal job (status={job.status})",
        )

    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    if job.kind == "audit_archive":
        from app.services import audit_archive_reads

        actor, authority = await audit_archive_reads.lock_authority(
            db, current_user, request
        )
        job = await async_job_svc.lock_job(db, job_id)
        if job is None or job.kind != "audit_archive":
            raise HTTPException(404, detail="async_job not found")
        if not await _can_access_job(db, job=job, user=actor):
            raise HTTPException(403, detail="not your job")
        if job.status not in {"pending", "running"}:
            raise HTTPException(409, detail="audit archive is no longer cancellable")
        await audit_archive_reads.reauthorize(db, authority)
        # The source DROP/ready transaction holds this same job lock. Previously
        # committed months remain in result; no physical attempt is deleted.
        await async_job_svc.mark_cancelled(db, job.id)
        await notify_job_terminal(db, job_id=job.id)
        await audit_archive_reads.reauthorize(db, authority)
        await db.commit()
        return {"status": "cancelled", "id": str(job.id)}

    if job.kind == "export":
        # Match the worker's final publication lock: a cancelled job cannot publish.
        job = await db.scalar(
            select(AsyncJob)
            .where(AsyncJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job.status not in {"pending", "running"}:
            raise HTTPException(409, detail="export is no longer cancellable")
        if not await _can_access_job(db, job=job, user=current_user):
            raise HTTPException(403, detail="not your job")
        await async_job_svc.mark_cancelled(db, job.id)
        await db.commit()
        return {"status": "cancelled", "id": str(job.id)}

    if job.kind == "managed_original_verification":
        assert_request_scopes(request, "*")
        from app.services import managed_original_verification, storage_maintenance

        key_id = getattr(request.state, "api_key_id", None)
        _, proof = await storage_maintenance.authority(
            db, current_user, key_id, "*", lock=True
        )
        status = await managed_original_verification.cancel(db, job.id)
        _, current_proof = await storage_maintenance.authority(
            db, current_user, key_id, "*"
        )
        if current_proof != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if status == "cancelled":
            await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        return {"status": status, "id": str(job.id)}

    if job.kind == "storage_usage":
        assert_request_scopes(request, "*")
        from app.services import storage_usage
        from app.services.dataset_content import key_authority

        key_id = getattr(request.state, "api_key_id", None)
        proof = await key_authority(db, current_user, key_id, "*")
        await storage_usage.require_admin(db, current_user.id)
        await storage_usage.cancel(db, job.id)
        await storage_usage.require_admin(db, current_user.id)
        if await key_authority(db, current_user, key_id, "*") != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.commit()
        return {"status": "cancelled", "id": str(job.id)}

    if job.kind in {"dataset_quality", "dataset_quality_review"}:
        assert_request_scopes(request, "datasets:write")
        from app.services.dataset_content import key_authority

        key_id = getattr(request.state, "api_key_id", None)
        proof = await key_authority(db, current_user, key_id, "datasets:write")
        await (
            review_svc if job.kind == "dataset_quality_review" else quality_svc
        ).cancel_job(db, job, current_user)
        if await key_authority(db, current_user, key_id, "datasets:write") != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.commit()
        return {"status": "cancelled", "id": str(job.id)}

    if job.kind in {"dataset_scan", "dataset_registration"}:
        assert_request_scopes(request, "datasets:write")
        from app.services.dataset_content import key_authority

        key_id = getattr(request.state, "api_key_id", None)
        key_proof = await key_authority(db, current_user, key_id, "datasets:write")
        if job.kind == "dataset_scan":
            from app.services.dataset_scans import cancel_job
        else:
            from app.services.dataset_registration import cancel_job

        await cancel_job(db, job.id, current_user)
        if await key_authority(db, current_user, key_id, "datasets:write") != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.commit()
        return {"status": "cancelled", "id": str(job.id)}

    if job.kind == "sample_evaluation":
        from app.api.v1.training_datasets import lock_owner
        from app.db.models.sample_evaluation import SampleEvaluationRun
        from app.services.sample_evaluations import request_cancel

        project = await db.get(Project, job.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        await lock_owner(db, current_user, project)
        run = await db.scalar(
            select(SampleEvaluationRun)
            .where(SampleEvaluationRun.async_job_id == job.id)
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status_code=409, detail="该任务已被新的评估尝试替代")
        await request_cancel(db, run)
        await db.commit()
        return {"status": "cancel_requested", "id": str(job_id)}

    if job.kind == "training_dataset":
        from app.db.models.training_dataset import TrainingDatasetVersion
        from app.services.project_access import resolve_project_access_by_id
        from app.services.project_write_guard import lock_actor_project_authority

        await lock_actor_project_authority(db, current_user.id, job.project_id)
        _, access = await resolve_project_access_by_id(
            db, user=current_user, project_id=job.project_id
        )
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="需要项目管理权限")
        version = (
            await db.execute(
                select(TrainingDatasetVersion)
                .where(TrainingDatasetVersion.async_job_id == job.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        await db.refresh(job)
        if (
            version is None
            or version.status not in {"pending", "running"}
            or job.status not in {"pending", "running"}
        ):
            raise HTTPException(status_code=409, detail="训练数据构建已结束")
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(job.celery_task_id, terminate=False)
            except Exception:
                log.exception("Training dataset revoke failed job=%s", job.id)
        await async_job_svc.mark_cancelled(db, job.id)
        await reconcile_domain_terminal(db, job, cancelled=True)
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        return {"status": "cancelled", "id": str(job_id)}

    if job.kind == "mask_qc":
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(job.celery_task_id, terminate=False)
            except Exception:
                log.exception("mask_qc revoke failed job=%s", job.id)
        await async_job_svc.mark_cancelled(
            db,
            job.id,
            result={"reason": "cancelled_by_user"},
        )
        # Reuse the shared domain terminal rule (pending/running -> cancelled)
        # instead of a second inline copy of the MaskQCRun transition.
        await reconcile_domain_terminal(db, job, cancelled=True)
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        return {"status": "cancelled", "id": str(job_id)}

    if job.kind == "point_cloud_quality":
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(job.celery_task_id, terminate=False)
            except Exception:
                log.exception("point_cloud_quality revoke failed job=%s", job.id)
        await async_job_svc.mark_cancelled(
            db, job.id, result={"reason": "cancelled_by_user"}
        )
        await reconcile_domain_terminal(db, job, cancelled=True)
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        return {"status": "cancelled", "id": str(job_id)}

    if job.kind == "mask_repair":
        # Match the worker's Job -> Batch publication locks. Cancellation must
        # be durable before contacting the broker, including response loss.
        job = await async_job_svc.lock_job(db, job_id)
        if job is None or job.status not in {"pending", "running"}:
            raise HTTPException(409, detail="repair is no longer cancellable")
        if not await _can_access_job(db, job=job, user=current_user):
            raise HTTPException(403, detail="not your job")
        celery_task_id = job.celery_task_id
        if job.status == AsyncJobStatus.PENDING.value:
            await async_job_svc.mark_cancelled(
                db, job.id, result={"reason": "cancelled_by_user"}
            )
            await reconcile_domain_terminal(db, job, cancelled=True)
            await notify_job_terminal(db, job_id=job.id)
            response_status = "cancelled"
        else:
            await async_job_svc.request_cancel(db, job.id)
            response_status = "cancel_requested"
        await db.commit()
        if celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(celery_task_id, terminate=False)
            except Exception:
                log.exception("mask_repair revoke failed job=%s", job_id)
        return {"status": response_status, "id": str(job_id)}

    if job.kind == "mask_format_import":
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(job.celery_task_id, terminate=False)
            except Exception:
                log.exception("mask_format_import revoke failed job=%s", job.id)
        batch = (
            await db.execute(
                select(MaskFormatImport)
                .where(MaskFormatImport.async_job_id == job.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job.status == AsyncJobStatus.PENDING.value:
            await async_job_svc.mark_cancelled(
                db, job.id, result={"reason": "cancelled_by_user"}
            )
            if batch is not None and batch.status == "pending":
                batch.status = "cancelled"
                batch.completed_at = datetime.now(timezone.utc)
            await notify_job_terminal(db, job_id=job.id)
            await db.commit()
            return {"status": "cancelled", "id": str(job_id)}
        await async_job_svc.request_cancel(db, job.id)
        await db.commit()
        return {"status": "cancel_requested", "id": str(job_id)}

    if job.kind == "batch_predict":
        # Use the worker's result/terminal lock, then recheck fresh state. A
        # cancellation that wins this lock prevents the in-flight result write.
        job = await async_job_svc.lock_job(db, job_id)
        if job is None:
            raise HTTPException(404, detail="async_job not found")
        if job.status not in {"pending", "running"}:
            raise HTTPException(409, detail="batch prediction is no longer cancellable")
        if not await _can_access_job(db, job=job, user=current_user):
            raise HTTPException(403, detail="not your job")
        celery_task_id = job.celery_task_id
        total_tasks = _payload_int(job.payload or {}, "total_tasks") or 0
        if job.status == AsyncJobStatus.PENDING.value:
            await async_job_svc.mark_cancelled(
                db,
                job.id,
                result={
                    "success_count": 0,
                    "failed_count": 0,
                    "done_count": 0,
                    "skipped_count": total_tasks,
                    "cancelled_at_index": 0,
                },
            )
            await notify_job_terminal(db, job_id=job.id)
            response_status = "cancelled"
        else:
            await async_job_svc.request_cancel(db, job.id)
            response_status = "cancel_requested"
        await db.commit()
        # Broker I/O must not hold the result lock. The durable cancellation is
        # authoritative even when the best-effort revoke cannot reach Celery.
        if celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(celery_task_id, terminate=False)
            except Exception:
                log.exception("batch_predict revoke failed job=%s", job_id)
        return {"status": response_status, "id": str(job_id)}

    if job.kind == "point_cloud_cross_frame":
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app

                celery_app.control.revoke(job.celery_task_id, terminate=False)
            except Exception:
                log.exception("point_cloud_cross_frame revoke failed job=%s", job.id)
        if job.status == AsyncJobStatus.PENDING.value:
            from app.services.cross_frame_job import summarize_items

            targets = (job.payload or {}).get("targets") or []
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
            await async_job_svc.mark_cancelled(
                db,
                job.id,
                result=summarize_items(items),
            )
            await notify_job_terminal(db, job_id=job.id)
            await db.commit()
            return {"status": "cancelled", "id": str(job_id)}
        await async_job_svc.request_cancel(db, job.id)
        await db.commit()
        return {"status": "cancel_requested", "id": str(job_id)}

    await async_job_svc.mark_cancelled(db, job.id)
    await notify_job_terminal(db, job_id=job.id)
    await db.commit()
    return {"status": "cancelled", "id": str(job_id)}


@router.post(
    "/async-jobs/{job_id}/retry-failed",
    status_code=202,
    response_model=AsyncJobRetryFailedResponse,
)
async def retry_failed_async_job_items(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AsyncJobRetryFailedResponse:
    job = await db.get(AsyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="async_job not found")
    # Retrying re-enters project work, so require current management capability
    # (owner / super-admin) rather than a platform role or job ownership alone.
    if job.project_id is not None:
        project = await db.get(Project, job.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="async_job not found")
        from app.services.project_access import resolve_project_access

        try:
            access = await resolve_project_access(
                db, user=current_user, project=project
            )
        except HTTPException:
            raise HTTPException(status_code=403, detail="not your job")
        if not access.is_manager:
            raise HTTPException(status_code=403, detail="requires project admin")
    elif (
        current_user.role != PlatformRole.SUPER_ADMIN.value
        and job.user_id != current_user.id
    ):
        raise HTTPException(status_code=403, detail="not your job")
    if job.kind not in RETRY_FAILED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind={job.kind} does not support failed item retry",
        )

    failed_ids = _payload_uuid_list(job.result or {}, "failed_prediction_ids")
    if not failed_ids:
        raise HTTPException(
            status_code=409,
            detail="no retryable failed prediction ids recorded for this job",
        )

    from app.api.v1.predictions import MAX_RETRY_COUNT
    from app.services.prediction import claim_failed_prediction_retry
    from app.services.prediction_retries import prepare_retry, release_unpublished_retry

    plans = []
    for failed_id in sorted(failed_ids, key=str):
        claimed = await claim_failed_prediction_retry(db, failed_id, MAX_RETRY_COUNT)
        if claimed is not None:
            plans.append(
                await prepare_retry(
                    db, claimed, current_user, request, project_id=job.project_id
                )
            )
    if not plans:
        raise HTTPException(
            409, detail="no retryable failed predictions remain for this job"
        )
    await db.commit()
    queued = 0
    try:
        for plan in plans:
            plan.publish(current_user.id)
            queued += 1
    except Exception:
        for plan in plans[queued:]:
            await release_unpublished_retry(db, plan)
        await db.commit()
        raise

    return AsyncJobRetryFailedResponse(
        status="queued",
        job_id=job_id,
        queued=queued,
        skipped=len(failed_ids) - queued,
    )


def _payload_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _payload_uuid_list(payload: dict, key: str) -> list[uuid.UUID]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    out: list[uuid.UUID] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            out.append(uuid.UUID(item))
        except ValueError:
            continue
    return out
