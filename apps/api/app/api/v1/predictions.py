"""v0.8.6 F6 · 失败预测管理与重试。

仅 super_admin / project_admin 可访问。

端点：
- ``GET /admin/failed-predictions?page&page_size&include_dismissed`` 列表（分页）
- ``POST /admin/failed-predictions/{id}/retry`` 异步重试，返回 202 Accepted；
  WebSocket 推 ``failed_prediction.retry.{started,succeeded,failed}`` 进度事件。
- ``POST /admin/failed-predictions/{id}/dismiss`` v0.8.8 · 永久放弃（soft-delete）。
- ``POST /admin/failed-predictions/{id}/restore`` v0.8.8 · 误操作恢复。

软上限 max=3 由本路由层判断（HTTP 409）。dismiss 后即使 retry_count < max 也不再
出现在默认列表（前端 toggle "显示已放弃" 才出）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_current_user,
    get_db,
    require_project_owner,
    require_roles,
    require_scopes,
)
from app.db.enums import UserRole
from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.project_access import resolve_project_access
from app.schemas.aap_json import AAPImportErrorEntry, AAPImportResult
from app.schemas.prediction import (
    PredictionPurgeCounts,
    PredictionPurgeRequest,
    PredictionPurgeResponse,
)
from app.config import settings
from app.services.audit import AuditAction, AuditService
from app.services.pipeline_validation import resolve_preannotate_queue
from app.services.prediction import (
    claim_failed_prediction_retry,
    release_failed_prediction_retry_claim,
)


router = APIRouter()

MAX_RETRY_COUNT = 3
_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


async def _assert_can_manage_failed_prediction(
    db: AsyncSession, fp: FailedPrediction, user: User
) -> None:
    """Failed-prediction retry/dismiss/restore management is project-bound.

    A platform project administrator retains management only over projects they
    own; a super administrator manages every project.  A membership in another
    owner's project grants no management.
    """

    if user.role == UserRole.SUPER_ADMIN.value:
        return
    project = await db.get(Project, fp.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    access = await resolve_project_access(db, user=user, project=project)
    if access.is_manager:
        return
    raise HTTPException(
        status_code=403, detail="仅项目负责人或超级管理员可管理失败预测"
    )


class FailedPredictionItem(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID | None
    task_display_id: str | None = None
    project_id: uuid.UUID
    project_name: str | None = None
    ml_backend_id: uuid.UUID | None
    backend_name: str | None = None
    model_version: str | None
    error_type: str
    message: str
    retry_count: int
    last_retry_at: datetime | None
    # v0.8.8 · 永久放弃时间戳；非空表示已被 admin dismiss
    dismissed_at: datetime | None = None
    created_at: datetime


class FailedPredictionList(BaseModel):
    items: list[FailedPredictionItem]
    total: int
    page: int
    page_size: int


class RetryResponse(BaseModel):
    status: str
    failed_id: uuid.UUID


@router.get(
    "/admin/failed-predictions",
    response_model=FailedPredictionList,
    dependencies=[Depends(require_scopes("predictions:read"))],
)
async def list_failed_predictions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_dismissed: bool = Query(
        False,
        description="v0.8.8 · true 时同时返回已 dismiss 的失败预测；默认隐藏",
    ),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    base_filter = (
        FailedPrediction.dismissed_at.is_(None) if not include_dismissed else None
    )

    count_q = select(func.count()).select_from(FailedPrediction)
    rows_q = (
        select(FailedPrediction, Task, Project, MLBackend)
        .outerjoin(Task, Task.id == FailedPrediction.task_id)
        .outerjoin(Project, Project.id == FailedPrediction.project_id)
        .outerjoin(MLBackend, MLBackend.id == FailedPrediction.ml_backend_id)
        .order_by(desc(FailedPrediction.created_at))
    )
    if actor.role == UserRole.PROJECT_ADMIN.value:
        # A platform project administrator's global list covers only their own
        # projects; an explicit other-owner project cannot be enumerated here.
        owned_project_ids = select(Project.id).where(Project.owner_id == actor.id)
        count_q = count_q.where(FailedPrediction.project_id.in_(owned_project_ids))
        rows_q = rows_q.where(FailedPrediction.project_id.in_(owned_project_ids))
    if base_filter is not None:
        count_q = count_q.where(base_filter)
        rows_q = rows_q.where(base_filter)
    total = (await db.execute(count_q)).scalar() or 0

    rows = (
        await db.execute(rows_q.offset((page - 1) * page_size).limit(page_size))
    ).all()

    items: list[FailedPredictionItem] = []
    for fp, t, p, b in rows:
        items.append(
            FailedPredictionItem(
                id=fp.id,
                task_id=fp.task_id,
                task_display_id=t.display_id if t else None,
                project_id=fp.project_id,
                project_name=p.name if p else None,
                ml_backend_id=fp.ml_backend_id,
                backend_name=b.name if b else None,
                model_version=fp.model_version,
                error_type=fp.error_type,
                message=fp.message,
                retry_count=fp.retry_count or 0,
                last_retry_at=fp.last_retry_at,
                dismissed_at=fp.dismissed_at,
                created_at=fp.created_at,
            )
        )
    return FailedPredictionList(
        items=items, total=int(total), page=page, page_size=page_size
    )


@router.post(
    "/admin/failed-predictions/{failed_id}/retry",
    status_code=202,
    response_model=RetryResponse,
)
async def retry_failed_prediction(
    failed_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
) -> Any:
    fp = await db.get(FailedPrediction, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await _assert_can_manage_failed_prediction(db, fp, current_user)
    if fp.dismissed_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Failed prediction is dismissed; restore it before retry",
        )
    if (fp.retry_count or 0) >= MAX_RETRY_COUNT:
        raise HTTPException(
            status_code=409,
            detail=f"Max retries ({MAX_RETRY_COUNT}) exceeded",
        )
    fp = await claim_failed_prediction_retry(db, failed_id, MAX_RETRY_COUNT)
    if fp is None:
        raise HTTPException(status_code=409, detail="Retry already queued")

    # 投递 Celery task，前端通过 ws 推送获得进度
    from app.workers.predictions_retry import retry_failed_prediction as task_fn

    # v0.19.5 · 设备感知路由对齐: 原始失败的 backend 自报 device=cpu 时, 重试也落 ml.cpu 队列,
    # 而不是按 task_routes 静态路由到 ml(gpu) 占 GPU 并发槽。无自报 → 保守落 gpu(与现状等价)。
    device: str | None = None
    if fp.ml_backend_id is not None:
        backend = await db.get(MLBackend, fp.ml_backend_id)
        if backend and isinstance(backend.health_meta, dict):
            rp = (backend.health_meta.get("capabilities") or {}).get(
                "resource_profile"
            ) or {}
            device = rp.get("device")
    queue = resolve_preannotate_queue(
        [device],
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    await db.commit()
    try:
        task_fn.apply_async(args=[str(failed_id), str(current_user.id)], queue=queue)
    except Exception:
        await release_failed_prediction_retry_claim(db, failed_id)
        await db.commit()
        raise
    return RetryResponse(status="queued", failed_id=failed_id)


class DismissResponse(BaseModel):
    status: str
    failed_id: uuid.UUID
    dismissed_at: datetime | None = None


@router.post(
    "/admin/failed-predictions/{failed_id}/dismiss",
    response_model=DismissResponse,
)
async def dismiss_failed_prediction(
    failed_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
) -> Any:
    """v0.8.8 · 永久放弃失败预测（soft-delete）。

    审计：``failed_prediction.dismissed``。重复调用幂等——已 dismiss 的行
    不更新 dismissed_at。
    """
    fp = await db.get(FailedPrediction, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await _assert_can_manage_failed_prediction(db, fp, current_user)
    if fp.dismissed_at is None:
        fp.dismissed_at = datetime.now(timezone.utc)
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.FAILED_PREDICTION_DISMISSED,
            target_type="failed_prediction",
            target_id=str(failed_id),
            request=request,
            detail={
                "project_id": str(fp.project_id),
                "error_type": fp.error_type,
                "retry_count": fp.retry_count or 0,
            },
        )
        await db.commit()
    return DismissResponse(
        status="dismissed",
        failed_id=failed_id,
        dismissed_at=fp.dismissed_at,
    )


@router.post(
    "/admin/failed-predictions/{failed_id}/restore",
    response_model=DismissResponse,
)
async def restore_failed_prediction(
    failed_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
) -> Any:
    """v0.8.8 · 误操作恢复：清空 ``dismissed_at``，让该行重回默认列表。"""
    fp = await db.get(FailedPrediction, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await _assert_can_manage_failed_prediction(db, fp, current_user)
    if fp.dismissed_at is not None:
        fp.dismissed_at = None
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.FAILED_PREDICTION_RESTORED,
            target_type="failed_prediction",
            target_id=str(failed_id),
            request=request,
            detail={
                "project_id": str(fp.project_id),
                "error_type": fp.error_type,
            },
        )
        await db.commit()
    return DismissResponse(
        status="restored",
        failed_id=failed_id,
        dismissed_at=None,
    )


# ── v0.10.15 · 外部预测导入 (COCO / AAP JSON) ─────────────────────────


@router.post(
    "/projects/{project_id}/predictions/import",
    response_model=AAPImportResult,
)
async def import_predictions(
    request: Request,
    files: list[UploadFile] = File(..., alias="file"),
    format: str = Query("aap_json", pattern="^(aap_json|coco|yolo)$"),
    yolo_variant: str = Query("det", pattern="^(det|obb|seg)$"),
    dry_run: bool = Query(False),
    model_version: str | None = Form(None),
    overwrite_existing: bool = Form(True),
    image_width: int | None = Form(None),
    image_height: int | None = Form(None),
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AAPImportResult:
    """外部模型预测结果导入端点 (v0.10.15).

    支持 COCO Detection、YOLO zip 与平台原生 AAP JSON v1.0 三种 input format. 写入的
    Prediction 行 source='external_import', ml_backend_id=NULL.
    """

    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from app.services.predictions_import import (
        import_aap_json,
        import_coco,
        import_yolo,
    )

    raw_files = [(upload.filename or "file", await upload.read()) for upload in files]
    if not raw_files:
        raise HTTPException(status_code=422, detail="file is required")

    image_size_hint: tuple[int, int] | None = None
    if format == "coco":
        if (image_width is None) != (image_height is None):
            raise HTTPException(
                status_code=422,
                detail="image_width and image_height must be provided together",
            )
        if image_width is not None and image_height is not None:
            if image_width <= 0 or image_height <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="image_width and image_height must be positive",
                )
            image_size_hint = (image_width, image_height)

    # v0.10.16 · async_jobs 双写（同步端点；dry_run 不记录）
    aj_id: uuid.UUID | None = None
    if not dry_run:
        try:
            aj = await async_job_svc.create_job(
                db,
                kind="predictions_import",
                project_id=project.id,
                user_id=current_user.id,
                payload={
                    "format": format,
                    "size_bytes": sum(len(raw) for _, raw in raw_files),
                    "file_count": len(raw_files),
                    "file_names": [name for name, _ in raw_files],
                    "project_display_id": project.display_id,
                    "overwrite_existing": overwrite_existing,
                    "model_version_fallback": model_version,
                    "image_size_hint": image_size_hint,
                    "yolo_variant": yolo_variant if format == "yolo" else None,
                },
            )
            await async_job_svc.mark_running(db, aj.id)
            await db.commit()
            aj_id = aj.id
        except Exception:
            await db.rollback()
            aj_id = None

    try:
        result = AAPImportResult(dry_run=dry_run)
        purged_tasks: set[uuid.UUID] = set()
        for filename, raw in raw_files:
            try:
                if format == "aap_json":
                    part = await import_aap_json(
                        db,
                        project.id,
                        raw,
                        model_version_fallback=model_version,
                        overwrite_existing=overwrite_existing,
                        dry_run=dry_run,
                        purged_tasks=purged_tasks,
                    )
                elif format == "coco":
                    part = await import_coco(
                        db,
                        project.id,
                        raw,
                        model_version_fallback=model_version,
                        overwrite_existing=overwrite_existing,
                        dry_run=dry_run,
                        image_size_hint=image_size_hint,
                        purged_tasks=purged_tasks,
                    )
                else:
                    part = await import_yolo(
                        db,
                        project.id,
                        raw,
                        yolo_variant=yolo_variant,
                        model_version_fallback=model_version,
                        overwrite_existing=overwrite_existing,
                        dry_run=dry_run,
                        purged_tasks=purged_tasks,
                    )
            except ValueError as exc:
                if len(raw_files) == 1:
                    raise
                result.skipped += 1
                result.errors.append(
                    AAPImportErrorEntry(
                        task_match={"file_name": filename},
                        reason=str(exc),
                    )
                )
                continue

            result.imported += part.imported
            result.skipped += part.skipped
            if len(raw_files) > 1:
                result.errors.extend(
                    AAPImportErrorEntry(
                        task_match=err.task_match,
                        reason=f"{filename}: {err.reason}",
                    )
                    for err in part.errors
                )
            else:
                result.errors.extend(part.errors)
    except ValueError as exc:
        if aj_id is not None:
            try:
                await async_job_svc.mark_failed(db, aj_id, error=str(exc))
                await notify_job_terminal(db, job_id=aj_id)
                await db.commit()
            except Exception:
                await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        if aj_id is not None:
            try:
                await async_job_svc.mark_failed(db, aj_id, error=str(exc))
                await notify_job_terminal(db, job_id=aj_id)
                await db.commit()
            except Exception:
                await db.rollback()
        raise

    if not dry_run:
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.PREDICTIONS_IMPORT,
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={
                "format": format,
                "project_display_id": project.display_id,
                "file_count": len(raw_files),
                "imported": result.imported,
                "skipped": result.skipped,
                "error_count": len(result.errors),
                "model_version_fallback": model_version,
                "overwrite_existing": overwrite_existing,
                "image_size_hint": image_size_hint,
                "yolo_variant": yolo_variant if format == "yolo" else None,
            },
        )
        if aj_id is not None:
            try:
                await async_job_svc.mark_complete(
                    db,
                    aj_id,
                    result={
                        "imported": result.imported,
                        "skipped": result.skipped,
                        "error_count": len(result.errors),
                    },
                )
                await notify_job_terminal(db, job_id=aj_id)
            except Exception:
                pass
        await db.commit()

    return result


@router.post(
    "/projects/{project_id}/predictions/purge",
    response_model=PredictionPurgeResponse,
)
async def purge_predictions(
    payload: PredictionPurgeRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PredictionPurgeResponse:
    """按来源清理项目预测候选。dry_run=true 仅返回将删除的计数。"""

    from app.services.predictions_import import _purge_predictions

    counts = await _purge_predictions(
        db,
        project_id=project.id,
        task_ids=payload.task_ids,
        source_scope=payload.source_scope,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run:
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.PREDICTIONS_PURGE,
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={
                "project_display_id": project.display_id,
                "source_scope": payload.source_scope,
                "task_ids": [str(task_id) for task_id in payload.task_ids]
                if payload.task_ids is not None
                else None,
                "counts": counts,
            },
        )
        await db.commit()

    return PredictionPurgeResponse(
        source_scope=payload.source_scope,
        task_ids=payload.task_ids,
        dry_run=payload.dry_run,
        counts=PredictionPurgeCounts(**counts),
    )
