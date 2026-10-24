"""Authorized native-preview requests and purpose-specific trusted CPU protocol."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.api.v1.training_datasets import lock_owner
from app.api.v1.training_runs import executor_auth
from app.db.models.project import Project
from app.db.models.training_preview import TrainingPreview
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_project_owner, require_scopes
from app.schemas.training_preview import (
    PreviewCapabilities,
    PreviewClaim,
    PreviewFinish,
    PreviewUploads,
    TrainingPreviewCreate,
    TrainingPreviewOut,
)
from app.schemas.training_run import ExecutorFence
from app.services import training_previews as service
from app.services.training_dataset_analysis import stream_media

router = APIRouter(
    prefix="/projects/{project_id}/training-previews",
    dependencies=[Depends(require_scopes("datasets:read", "annotations:read"))],
)
executor_router = APIRouter(
    prefix="/internal/training/previews", dependencies=[Depends(executor_auth)]
)


async def load(db, project_id, preview_id, *, lock=False):
    statement = select(TrainingPreview).where(
        TrainingPreview.id == preview_id, TrainingPreview.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update(read=lock == "share").execution_options(
            populate_existing=True
        )
    preview = await db.scalar(statement)
    if preview is None:
        raise HTTPException(status_code=404, detail="原生预览不存在或已清理")
    return preview


@router.get("/capabilities", response_model=PreviewCapabilities)
async def capabilities(
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    return await service.capabilities(db)


@router.post(
    "",
    response_model=TrainingPreviewOut,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def create(
    body: TrainingPreviewCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_owner(db, user, project)
    preview = await service.create(
        db, project, user, body, getattr(request.state, "api_key_id", None)
    )
    result = await service.output(preview)
    await db.commit()
    return result


@router.post(
    "/{preview_id}/storage-pin",
    response_model=TrainingPreviewOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def pin_legacy_preview_storage(
    preview_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.training_auxiliary_storage_adoption import pin

    preview = await pin(
        db,
        "training-preview",
        preview_id,
        user,
        project,
        getattr(request.state, "api_key_id", None),
    )
    return await service.output(preview, include_report=False)


@router.get("/{preview_id}", response_model=TrainingPreviewOut)
async def get(
    preview_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    preview = await load(db, project.id, preview_id, lock="share")
    result = await service.output(preview)
    await db.commit()
    return result


@router.post(
    "/{preview_id}/cancel",
    response_model=TrainingPreviewOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def cancel(
    preview_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    preview = await load(db, project.id, preview_id, lock=True)
    await service.cancel(db, preview)
    result = await service.output(preview)
    await db.commit()
    return result


@router.delete(
    "/{preview_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def expire(
    preview_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    preview = await load(db, project.id, preview_id, lock=True)
    preview.expires_at = service.now()
    await service.cancel(db, preview)
    await db.commit()


@router.get("/{preview_id}/images/{index}", response_class=StreamingResponse)
async def image(
    preview_id: uuid.UUID,
    index: int,
    project: Project = Depends(require_project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await lock_owner(db, user, project)
    preview = await load(db, project.id, preview_id, lock="share")
    if preview.expires_at <= service.now():
        raise HTTPException(status_code=410, detail="原生预览已过期，请重新生成")
    if preview.status != "succeeded" or not 0 <= index < preview.spec["count"]:
        raise HTTPException(status_code=409, detail="原生预览尚未完成或图片不存在")
    source = None
    try:
        report = await asyncio.to_thread(service.read_report, preview)
        output = next(item for item in report.outputs if item.index == index)
        source = await asyncio.to_thread(service.read_image, preview, output)
        await db.commit()
        return StreamingResponse(
            stream_media(source),
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(source.close),
        )
    except BaseException as exc:
        if source is not None:
            source.close()
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, Exception):
            raise HTTPException(
                status_code=422, detail="原生预览图片缺失、损坏或身份不符"
            ) from exc
        raise


@executor_router.post("/claim")
async def claim(body: PreviewClaim, db: AsyncSession = Depends(get_db)):
    result = await service.claim(db, body)
    await db.commit()
    return {"assignment": result}


@executor_router.post("/{preview_id}/heartbeat")
async def heartbeat(
    preview_id: uuid.UUID, body: ExecutorFence, db: AsyncSession = Depends(get_db)
):
    result = await service.heartbeat(db, preview_id, body)
    await db.commit()
    return result


@executor_router.post("/{preview_id}/uploads")
async def uploads(
    preview_id: uuid.UUID, body: PreviewUploads, db: AsyncSession = Depends(get_db)
):
    result = await service.uploads(db, preview_id, body)
    await db.commit()
    return result


@executor_router.post("/{preview_id}/finish")
async def finish(
    preview_id: uuid.UUID, body: PreviewFinish, db: AsyncSession = Depends(get_db)
):
    preview = await service.finish(db, preview_id, body)
    await db.commit()
    return {"id": str(preview.id), "status": preview.status}
