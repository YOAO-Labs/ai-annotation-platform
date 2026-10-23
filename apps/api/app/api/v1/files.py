import uuid
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
from app.services.display_id import next_display_id
from app.services.storage import storage_service
from app.services.content_snapshot import IMAGE_RESPONSES

router = APIRouter()


@router.post("/upload-init", response_model=UploadInitResponse)
async def upload_init(
    data: UploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task_id = uuid.uuid4()
    storage_key = f"{data.project_id}/{task_id}/{data.file_name}"

    task = Task(
        id=task_id,
        project_id=data.project_id,
        display_id=await next_display_id(db, "tasks"),
        file_name=data.file_name,
        file_path=storage_key,
        file_type=_infer_file_type(data.content_type),
        status="uploading",
    )
    db.add(task)
    await db.commit()

    upload_url = storage_service.generate_upload_url(storage_key, data.content_type)
    return UploadInitResponse(task_id=task_id, upload_url=upload_url, expires_in=900)


@router.post("/upload-complete/{task_id}")
async def upload_complete(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "uploading":
        raise HTTPException(status_code=400, detail="Task is not in uploading state")

    meta = storage_service.verify_upload(task.file_path)
    if not meta:
        raise HTTPException(status_code=400, detail="File not found in storage")

    task.status = "pending"
    await db.commit()

    if task.file_type == "image":
        from app.workers.media import generate_task_thumbnail

        generate_task_thumbnail.delay(str(task_id))

    return {"status": "ok", "task_id": str(task_id)}


@router.get(
    "/tasks/{task_id}/file-url",
    response_model=TaskFileUrlResponse,
    dependencies=[Depends(require_scopes("tasks:read"))],
)
async def get_file_url(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.api.v1.tasks._shared import _assert_task_visible

    await _assert_task_visible(db, task, current_user)

    from app.services.reference_keys import is_reference

    if is_reference(task.file_path):
        raise HTTPException(409, detail={"code": "reference_requires_verified_content"})

    bucket = (
        storage_service.datasets_bucket
        if task.dataset_item_id or task.file_type == "point_cloud"
        else storage_service.bucket
    )
    url = storage_service.generate_download_url(task.file_path, bucket=bucket)
    return TaskFileUrlResponse(url=url, expires_in=3600)


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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.dataset_content import task_identity, content_http_error

    try:
        asset_id, sha256, size, _, _ = await task_identity(
            db, task_id, current_user, None
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return {
        "asset_id": str(asset_id),
        "sha256": sha256,
        "size_bytes": size,
        "verification_state": "verified",
        "original_availability": "unchecked",
        "preview_availability": "unknown",
    }


@router.post("/projects/{project_id}/backfill-thumbnails")
async def backfill_project_thumbnails(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.workers.media import backfill_tasks

    task = backfill_tasks.delay(str(project_id))
    return {"status": "queued", "celery_task_id": task.id}


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
