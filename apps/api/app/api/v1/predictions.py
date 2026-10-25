"""失败预测管理与重试入口。

角色入口由路由校验；项目管理权限、重试限额、占用提交和派发补偿由领域与工作流负责。
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
from sqlalchemy.orm import aliased

from app.deps import (
    get_current_user,
    get_current_user_id,
    get_db,
    get_prediction_retry_port,
    require_project_owner,
    require_roles,
    require_scopes,
)
from app.db.enums import UserRole
from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
from app.db.models.project import Project
from app.schemas.aap_json import AAPImportErrorEntry, AAPImportResult
from app.schemas.prediction import (
    PredictionPurgeCounts,
    PredictionPurgeRequest,
    PredictionPurgeResponse,
)
from app.services.audit import AuditAction, AuditService
from app.application.async_job_contracts import (
    AsyncJobOperationFailure,
    PredictionRetryPort,
)
from app.services.prediction import (
    PredictionService,
    assert_can_manage_failed_prediction,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_read_relations import project_facts, project_rows
from app.services.labeling_core_read_relations import (
    failed_prediction_row,
    get_core_row,
    task_row,
)


router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


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
    actor: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
):
    base_filter = (
        failed_prediction_row.dismissed_at.is_(None) if not include_dismissed else None
    )

    count_q = select(func.count()).select_from(failed_prediction_row)
    project_row = aliased(Project, project_rows(), adapt_on_names=True)
    rows_q = (
        select(failed_prediction_row, task_row, project_row, MLBackend)
        .outerjoin(task_row, task_row.id == failed_prediction_row.task_id)
        .outerjoin(project_row, project_row.id == failed_prediction_row.project_id)
        .outerjoin(MLBackend, MLBackend.id == failed_prediction_row.ml_backend_id)
        .order_by(desc(failed_prediction_row.created_at))
    )
    if actor.role == UserRole.PROJECT_ADMIN.value:
        # A platform project administrator's global list covers only their own
        # projects; an explicit other-owner project cannot be enumerated here.
        owner_facts = project_facts()
        owned_project_ids = select(owner_facts.c.id).where(
            owner_facts.c.owner_id == actor.id
        )
        count_q = count_q.where(failed_prediction_row.project_id.in_(owned_project_ids))
        rows_q = rows_q.where(failed_prediction_row.project_id.in_(owned_project_ids))
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
    dependencies=[Depends(require_roles(*_MANAGERS))],
)
async def retry_failed_prediction(
    failed_id: uuid.UUID,
    actor_id: uuid.UUID = Depends(get_current_user_id),
    retry_port: PredictionRetryPort = Depends(get_prediction_retry_port),
) -> Any:
    try:
        await retry_port.retry_prediction(failed_id=failed_id, actor_id=actor_id)
    except AsyncJobOperationFailure as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=dict(exc.headers) if exc.headers is not None else None,
        ) from exc
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
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
) -> Any:
    """v0.8.8 · 永久放弃失败预测（soft-delete）。

    审计：``failed_prediction.dismissed``。重复调用幂等——已 dismiss 的行
    不更新 dismissed_at。
    """
    fp = await get_core_row(db, failed_prediction_row, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await assert_can_manage_failed_prediction(db, fp, current_user.id)
    if await PredictionService(db).dismiss_failed_prediction(fp):
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
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
) -> Any:
    """v0.8.8 · 误操作恢复：清空 ``dismissed_at``，让该行重回默认列表。"""
    fp = await get_core_row(db, failed_prediction_row, failed_id)
    if not fp:
        raise HTTPException(status_code=404, detail="Failed prediction not found")
    await assert_can_manage_failed_prediction(db, fp, current_user.id)
    if await PredictionService(db).restore_failed_prediction(fp):
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
                        actor_id=current_user.id,
                        job_id=aj_id,
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
                        actor_id=current_user.id,
                        job_id=aj_id,
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
                        actor_id=current_user.id,
                        job_id=aj_id,
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
