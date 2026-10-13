"""Annotation guide 图片资源端点 (v0.10.13 · E1).

挂载于 /projects/{project_id}/guide-assets/* 之下. 与 datasets items
upload 链路同结构但 storage prefix 独立 (projects/{id}/guide/...), 不污染
dataset_items 表. 读取沿用项目可见性；上传和删除仅限 project owner 或
super_admin (与 ProjectSettings 入口对齐).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.deps import get_db, require_project_owner, require_project_visible
from app.schemas.guide_asset import (
    ALLOWED_GUIDE_ASSET_TYPES,
    MAX_GUIDE_ASSET_SIZE_BYTES,
    GuideAssetEntry,
    GuideAssetSignedUrlResponse,
    GuideAssetUploadInitRequest,
    GuideAssetUploadInitResponse,
)
from app.services.storage import storage_service


router = APIRouter()

# storage key 形如 projects/{project_id}/guide/{uuid}-{safe_filename}
_GUIDE_KEY_RE = re.compile(r"^projects/[0-9a-f-]{36}/guide/[0-9a-f-]{36}-[^/]+$")


def _sanitize_filename(name: str) -> str:
    """去掉路径分隔与控制字符, 保留 ASCII/UTF-8 文件名."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    return name[:200] or "image"


def _build_key(project_id: uuid.UUID, filename: str) -> str:
    safe = _sanitize_filename(filename)
    return f"projects/{project_id}/guide/{uuid.uuid4()}-{safe}"


def _assert_key_belongs_to_project(key: str, project_id: uuid.UUID) -> None:
    if not _GUIDE_KEY_RE.match(key) or not key.startswith(
        f"projects/{project_id}/guide/"
    ):
        raise HTTPException(status_code=404, detail="Guide asset not found")


@router.post(
    "/{project_id}/guide-assets/upload-init",
    response_model=GuideAssetUploadInitResponse,
)
async def guide_asset_upload_init(
    data: GuideAssetUploadInitRequest,
    project: Project = Depends(require_project_owner),
    _: AsyncSession = Depends(get_db),
):
    if data.content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type: {data.content_type}",
        )
    if data.size > MAX_GUIDE_ASSET_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    key = _build_key(project.id, data.filename)
    upload_url = storage_service.generate_upload_url(
        key, content_type=data.content_type, expires_in=900
    )
    return GuideAssetUploadInitResponse(key=key, upload_url=upload_url, expires_in=900)


@router.post(
    "/{project_id}/guide-assets/upload-complete",
    response_model=GuideAssetEntry,
)
async def guide_asset_upload_complete(
    payload: dict,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    key = payload.get("key")
    original_name = payload.get("original_name")
    content_type = payload.get("content_type")
    if not key or not original_name or not content_type:
        raise HTTPException(status_code=400, detail="Missing required fields")
    _assert_key_belongs_to_project(key, project.id)
    if content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported content type")

    meta = storage_service.verify_upload(key)
    if not meta:
        raise HTTPException(status_code=400, detail="File not found in storage")
    size = int(meta.get("ContentLength") or 0)
    if size <= 0 or size > MAX_GUIDE_ASSET_SIZE_BYTES:
        storage_service.delete_object(key)
        raise HTTPException(status_code=400, detail="Invalid upload size")

    entry = {
        "key": key,
        "original_name": _sanitize_filename(original_name),
        "content_type": content_type,
        "size": size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    # JSONB 列必须重新赋值才会触发 dirty (SQLAlchemy 默认不深检测可变 list).
    assets = list(project.guide_assets or [])
    assets.append(entry)
    project.guide_assets = assets
    await db.commit()
    await db.refresh(project)
    return GuideAssetEntry(**entry)


@router.delete("/{project_id}/guide-assets")
async def guide_asset_delete(
    key: str,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    _assert_key_belongs_to_project(key, project.id)
    existing = list(project.guide_assets or [])
    remaining = [e for e in existing if e.get("key") != key]
    if len(remaining) == len(existing):
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        storage_service.delete_object(key)
    except Exception:  # noqa: BLE001 - 即使 storage 删失败也清 db 记录
        pass
    project.guide_assets = remaining
    await db.commit()
    await db.refresh(project)
    return {"deleted": key}


@router.get(
    "/{project_id}/guide-assets/sign-url",
    response_model=GuideAssetSignedUrlResponse,
)
async def guide_asset_sign_url(
    key: str,
    project: Project = Depends(require_project_visible),
    _: AsyncSession = Depends(get_db),
):
    _assert_key_belongs_to_project(key, project.id)
    keys = {e.get("key") for e in (project.guide_assets or [])}
    if key not in keys:
        raise HTTPException(status_code=404, detail="Asset not found")
    url = storage_service.generate_download_url(key, expires_in=3600)
    return GuideAssetSignedUrlResponse(url=url, expires_in=3600)
