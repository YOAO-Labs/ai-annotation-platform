import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_current_user, get_media_job_dispatcher
from app.db.models.user import User
from app.db.models.task import Task
from app.schemas.task import UploadInitRequest, UploadInitResponse, TaskFileUrlResponse
from app.application.legacy_file_upload import (
    LegacyFileUploadWorkflow,
    UploadObjectMissingError,
    UploadTaskNotFoundError,
    UploadTaskNotUploadingError,
)
from app.application.task_workflow_errors import TaskWorkflowError
from app.services import task_access
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.storage import storage_service
from app.services.assets_media import media_gateway, media_http_error
from aap_assets_media.contract import MediaFailure, ReadSourceRequest

router = APIRouter()


@router.post("/upload-init", response_model=UploadInitResponse)
async def upload_init(
    data: UploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await LegacyFileUploadWorkflow(
        db, storage=storage_service
    ).initialize_upload(
        actor_id=current_user.id,
        project_id=data.project_id,
        file_name=data.file_name,
        content_type=data.content_type,
        file_type=_infer_file_type(data.content_type),
    )
    return UploadInitResponse(
        task_id=result.scope.task_id,
        upload_url=result.upload_url,
        expires_in=result.expires_in,
    )


@router.post("/upload-complete/{task_id}")
async def upload_complete(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    media_dispatcher: MediaJobDispatcher = Depends(get_media_job_dispatcher),
):
    workflow = LegacyFileUploadWorkflow(db, storage=storage_service)
    try:
        result = await workflow.complete_upload(
            actor_id=current_user.id,
            task_id=task_id,
            dispatcher=media_dispatcher,
        )
    except UploadTaskNotFoundError:
        raise HTTPException(status_code=404, detail="Task not found")
    except UploadTaskNotUploadingError:
        raise HTTPException(status_code=400, detail="Task is not in uploading state")
    except UploadObjectMissingError:
        raise HTTPException(status_code=400, detail="File not found in storage")
    except TaskWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {"status": "ok", "task_id": str(result.scope.task_id)}


@router.get("/tasks/{task_id}/file-url", response_model=TaskFileUrlResponse)
async def get_file_url(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(Task, task_id, populate_existing=True)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id)

    if task.dataset_item_id is not None:
        try:
            async with media_gateway(db) as gateway:
                reference = await gateway.raw_source_reference(
                    ReadSourceRequest(
                        actor_id=current_user.id,
                        dataset_item_id=task.dataset_item_id,
                        task_id=task.id,
                    )
                )
        except MediaFailure as exc:
            raise media_http_error(exc) from None
        if reference.source.source_drifted:
            raise HTTPException(
                status_code=409, detail="Source changed; refresh the task."
            )
        return TaskFileUrlResponse(
            url=reference.original_url, expires_in=reference.expires_in
        )

    url = storage_service.generate_download_url(task.file_path)
    return TaskFileUrlResponse(url=url, expires_in=3600)


@router.post("/projects/{project_id}/backfill-thumbnails")
async def backfill_project_thumbnails(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    media_dispatcher: MediaJobDispatcher = Depends(get_media_job_dispatcher),
):
    celery_task_id = await LegacyFileUploadWorkflow(
        db, storage=storage_service
    ).queue_thumbnail_backfill(
        actor_id=current_user.id, project_id=project_id, dispatcher=media_dispatcher
    )
    return {"status": "queued", "celery_task_id": celery_task_id}


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
