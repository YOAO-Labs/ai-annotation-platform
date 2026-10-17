import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_current_user, require_project_owner
from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.task import Task
from app.schemas.task import UploadInitRequest, UploadInitResponse, TaskFileUrlResponse
from app.services.display_id import next_display_id
from app.services.project_access import resolve_project_access_by_id
from app.services.storage import storage_service

router = APIRouter()


@router.post("/upload-init", response_model=UploadInitResponse)
async def upload_init(
    data: UploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _project, access = await resolve_project_access_by_id(
        db, user=current_user, project_id=data.project_id
    )
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")

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
    _project, access = await resolve_project_access_by_id(
        db, user=current_user, project_id=task.project_id
    )
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")
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


@router.get("/tasks/{task_id}/file-url", response_model=TaskFileUrlResponse)
async def get_file_url(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.api.v1.tasks import _assert_task_visible

    await _assert_task_visible(db, task, current_user)

    url = storage_service.generate_download_url(task.file_path)
    return TaskFileUrlResponse(url=url, expires_in=3600)


@router.post("/projects/{project_id}/backfill-thumbnails")
async def backfill_project_thumbnails(
    project_id: uuid.UUID,
    _project: Project = Depends(require_project_owner),
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
