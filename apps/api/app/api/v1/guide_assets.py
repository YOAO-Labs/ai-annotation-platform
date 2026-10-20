"""Annotation guide image resource endpoints.

挂载于 /projects/{project_id}/guide-assets/* 之下. 与 datasets items
upload 链路同结构但 storage prefix 独立 (projects/{id}/guide/...), 不污染
dataset_items 表. 读取沿用项目可见性；上传和删除仅限 project owner 或
super_admin (与 ProjectSettings 入口对齐).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.deps import (
    get_current_user,
    get_db,
    require_project_owner,
    require_project_visible,
)
from app.schemas.guide_asset import (
    GuideAssetEntry,
    GuideAssetSignedUrlResponse,
    GuideAssetUploadInitRequest,
    GuideAssetUploadInitResponse,
)
from app.application.project_guide_assets import ProjectGuideAssetWorkflow
from app.services.storage import storage_service
from app.services.identity_contracts import IdentityActorFacts


router = APIRouter()


@router.post(
    "/{project_id}/guide-assets/upload-init",
    response_model=GuideAssetUploadInitResponse,
)
async def guide_asset_upload_init(
    data: GuideAssetUploadInitRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    upload = await ProjectGuideAssetWorkflow(
        db, storage=storage_service
    ).initialize_upload(
        actor_id=current_user.id,
        project_id=project.id,
        filename=data.filename,
        content_type=data.content_type,
        size=data.size,
    )
    return GuideAssetUploadInitResponse(**upload)


@router.post(
    "/{project_id}/guide-assets/upload-complete",
    response_model=GuideAssetEntry,
)
async def guide_asset_upload_complete(
    payload: dict,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    key = payload.get("key")
    original_name = payload.get("original_name")
    content_type = payload.get("content_type")
    if not key or not original_name or not content_type:
        raise HTTPException(status_code=400, detail="Missing required fields")

    entry = await ProjectGuideAssetWorkflow(
        db, storage=storage_service
    ).complete_upload(
        actor_id=current_user.id,
        project_id=project.id,
        key=key,
        original_name=original_name,
        content_type=content_type,
    )
    await db.commit()
    await db.refresh(project)
    return GuideAssetEntry(**entry)


@router.delete("/{project_id}/guide-assets")
async def guide_asset_delete(
    key: str,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    result = await ProjectGuideAssetWorkflow(db, storage=storage_service).delete_asset(
        actor_id=current_user.id, project_id=project.id, key=key
    )
    await db.commit()
    await db.refresh(project)
    return result


@router.get(
    "/{project_id}/guide-assets/sign-url",
    response_model=GuideAssetSignedUrlResponse,
)
async def guide_asset_sign_url(
    key: str,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    signed_url = await ProjectGuideAssetWorkflow(
        db, storage=storage_service
    ).sign_asset(actor_id=current_user.id, project_id=project.id, key=key)
    return GuideAssetSignedUrlResponse(**signed_url)
