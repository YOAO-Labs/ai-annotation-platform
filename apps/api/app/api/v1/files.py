import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_current_user, require_scopes
from app.schemas.task import (
    UploadInitRequest,
    UploadInitResponse,
    TaskFileUrlResponse,
    TaskContentIdentity,
)
from app.services import task_access
from app.schemas.task import TaskImageBackfillResponse
from app.services.identity_contracts import IdentityActorFacts
from app.services.content_snapshot import VERIFIED_IMAGE_RESPONSES
from app.db.models.user import User

from app.services.labeling_core_read_relations import get_core_row, task_row

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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await get_core_row(db, task_row, task_id, populate_existing=True)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id)

    raise HTTPException(
        409, detail={"code": "original_requires_authenticated_download"}
    )


@router.get(
    "/tasks/{task_id}/content",
    dependencies=[Depends(require_scopes("tasks:read"))],
    response_class=Response,
    responses=VERIFIED_IMAGE_RESPONSES,
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

    try:
        snapshot = await task_snapshot(
            db,
            task_id,
            current_user,
            sha256,
            api_key_id=getattr(request.state, "api_key_id", None),
            request=request,
            require_range_support=request.headers.get("range") is not None,
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(
        snapshot,
        range_header=request.headers.get("range"),
        if_range=request.headers.get("if-range"),
    )


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


@router.post(
    "/projects/{project_id}/backfill-thumbnails",
    response_model=TaskImageBackfillResponse,
    status_code=202,
    responses={
        202: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the accepted image backfill.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def backfill_project_thumbnails(
    project_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    from app.application import project_thumbnail_backfill
    from app.application.task_workflow_errors import TaskWorkflowError

    operation_id = idempotency_key or uuid.uuid4()
    try:
        accepted = await project_thumbnail_backfill.accept_thumbnail_backfill(
            db,
            actor_id=current_user.id,
            project_id=project_id,
            operation_id=operation_id,
        )
    except TaskWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if accepted.spec.selection.accepted_before is None:
        raise HTTPException(status_code=502, detail="Invalid image backfill receipt")
    response.headers["Idempotency-Key"] = str(operation_id)
    return TaskImageBackfillResponse(
        project_id=project_id,
        operation_id=operation_id,
        job_id=accepted.work.job_id,
        purpose="thumbnail",
        status=accepted.work.status,
        accepted_before=accepted.spec.selection.accepted_before,
    )


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
