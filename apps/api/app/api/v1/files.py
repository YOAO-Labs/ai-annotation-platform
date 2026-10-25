import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_current_user, require_scopes
from app.db.models.user import User
from app.db.models.task import Task
from app.schemas.task import (
    UploadInitRequest,
    UploadInitResponse,
    TaskFileUrlResponse,
    TaskContentIdentity,
)
from app.services.content_snapshot import IMAGE_RESPONSES

router = APIRouter()


@router.post(
    "/upload-init",
    response_model=UploadInitResponse,
    dependencies=[Depends(require_scopes("tasks:write"))],
)
async def upload_init(
    data: UploadInitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services import task_uploads

    try:
        task_id, upload_url = await task_uploads.initialize(
            db, current_user, request, data, _infer_file_type(data.content_type)
        )
        expires_in = int(
            (upload_url.deadline - datetime.now(timezone.utc)).total_seconds()
        )
        if expires_in <= 0:
            from app.services.content_snapshot import ContentError

            raise ContentError("import_storage_expired", 409)
    except Exception as exc:
        raise task_uploads.http_error(exc) from None
    return UploadInitResponse(
        task_id=task_id,
        upload_url=upload_url,
        expires_in=expires_in,
    )


@router.post(
    "/upload-complete/{task_id}",
    dependencies=[Depends(require_scopes("tasks:write"))],
)
async def upload_complete(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services import task_uploads

    try:
        file_type = await task_uploads.complete(db, current_user, request, task_id)
    except Exception as exc:
        raise task_uploads.http_error(exc) from None

    if file_type == "image":
        from app.workers.media import generate_task_thumbnail

        generate_task_thumbnail.delay(str(task_id))

    return {"status": "ok", "task_id": str(task_id)}


@router.get(
    "/tasks/{task_id}/file-url",
    response_model=TaskFileUrlResponse,
    deprecated=True,
    summary="旧原件签名入口（请使用认证下载）",
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def get_file_url(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check current visibility, then reject the retired original signature path."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.api.v1.tasks._shared import _assert_task_visible

    await _assert_task_visible(db, task, current_user)

    raise HTTPException(
        409, detail={"code": "original_requires_authenticated_download"}
    )


@router.get(
    "/tasks/{task_id}/content",
    dependencies=[Depends(require_scopes("tasks:read"))],
    response_class=Response,
    responses=IMAGE_RESPONSES,
)
async def get_verified_task_content(
    task_id: uuid.UUID,
    request: Request,
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.dataset_content import task_snapshot, content_http_error
    from app.services.content_snapshot import SnapshotResponse

    if request.headers.get("range") is not None:
        raise HTTPException(416, "Range is not supported for verified snapshots")
    try:
        snapshot = await task_snapshot(
            db,
            task_id,
            current_user,
            sha256,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(snapshot)


@router.get(
    "/tasks/{task_id}/content-identity",
    response_model=TaskContentIdentity,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def get_task_content_identity(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.dataset_content import public_task_identity, content_http_error

    try:
        return await public_task_identity(
            db,
            task_id,
            current_user,
            request=request,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None


@router.post("/projects/{project_id}/backfill-thumbnails")
async def backfill_project_thumbnails(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.workers.media import backfill_tasks

    task = backfill_tasks.delay(str(project_id), allow_pyramid=False)
    return {"status": "queued", "celery_task_id": task.id}


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
