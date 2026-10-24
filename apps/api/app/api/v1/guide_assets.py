"""Authenticated guide-image bytes and independent project references."""

from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.user import User
from app.deps import (
    get_current_user,
    get_db,
    require_project_owner,
    require_project_visible,
)
from app.schemas.guide_asset import (
    ALLOWED_GUIDE_ASSET_TYPES,
    GuideAssetEntry,
    GuideAssetUploadInitRequest,
    GuideAssetUploadInitResponse,
    GuideAssetSignedUrlResponse,
)
from app.services import guide_asset_storage as storage, guide_asset_uploads as uploads

router = APIRouter()


def _upgrade(project_id):
    raise HTTPException(
        409,
        {
            "code": "guide_asset_authenticated_transfer_required",
            "upload_path": f"/api/v1/projects/{project_id}/guide-assets/upload",
            "content_path": f"/api/v1/projects/{project_id}/guide-assets/content",
        },
    )


@router.post(
    "/{project_id}/guide-assets/upload",
    response_model=GuideAssetEntry,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in sorted(ALLOWED_GUIDE_ASSET_TYPES)
            },
        }
    },
)
async def guide_asset_upload(
    project_id: uuid.UUID,
    request: Request,
    file_name: str = Query(..., min_length=1, max_length=255),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if request.headers.get("content-encoding", "identity").lower() not in {
        "",
        "identity",
    }:
        raise HTTPException(415, {"code": "guide_asset_content_encoding"})
    try:
        file_name = storage.safe_file_name(file_name)
        mime_type = storage.normalize_mime_type(request.headers.get("content-type"))
    except storage.GuideAssetStorageError as exc:
        raise uploads.http_error(exc) from None
    proof = await uploads.capture_upload(db, actor, request, project_id)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > storage.MAX_BYTES:
            raise HTTPException(413, {"code": "guide_asset_too_large"})
        data.extend(chunk)
    return await uploads.upload(
        db, proof, project_id, bytes(data), mime_type, file_name
    )


@router.get("/{project_id}/guide-assets/content", response_class=Response)
async def guide_asset_content(
    project_id: uuid.UUID,
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    receipt, data = await uploads.read(db, actor, request, project_id, key)
    return Response(
        data,
        media_type=receipt.mime_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(receipt.file_name, safe=""),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{project_id}/guide-assets")
async def guide_asset_delete(
    project_id: uuid.UUID,
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await uploads.unlink(db, actor, request, project_id, key)


@router.post(
    "/{project_id}/guide-assets/upload-init",
    response_model=GuideAssetUploadInitResponse,
    deprecated=True,
)
async def guide_asset_upload_init(
    data: GuideAssetUploadInitRequest,
    project: Project = Depends(require_project_owner),
):
    if data.content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(400, "Unsupported content type")
    _upgrade(project.id)


@router.post(
    "/{project_id}/guide-assets/upload-complete",
    response_model=GuideAssetEntry,
    deprecated=True,
)
async def guide_asset_upload_complete(
    payload: dict,
    project: Project = Depends(require_project_owner),
):
    if any(
        not payload.get(field) for field in ("key", "original_name", "content_type")
    ):
        raise HTTPException(400, "Missing required fields")
    if payload["content_type"] not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(400, "Unsupported content type")
    _upgrade(project.id)


@router.get(
    "/{project_id}/guide-assets/sign-url",
    response_model=GuideAssetSignedUrlResponse,
    deprecated=True,
)
async def guide_asset_sign_url(
    key: str,
    project: Project = Depends(require_project_visible),
):
    if key not in {entry.get("key") for entry in (project.guide_assets or [])}:
        raise HTTPException(404, "Asset not found")
    _upgrade(project.id)
