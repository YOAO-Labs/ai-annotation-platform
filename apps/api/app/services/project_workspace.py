"""API-side guide, import, class-rename and adjacent project write helpers."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.services.guide_asset_contracts import (
    ALLOWED_GUIDE_ASSET_TYPES,
    MAX_GUIDE_ASSET_SIZE_BYTES,
)


def set_point_cloud_quality_config(*, project: Project, config: dict[str, Any]) -> None:
    project.point_cloud_quality_config = config


async def clear_primary_backend_binding_for_pool(
    db: AsyncSession, pool_id: UUID
) -> None:
    await db.execute(
        update(Project)
        .where(Project.ml_backend_pool_id == pool_id)
        .values(ml_backend_pool_id=None)
    )


async def find_import_project(db: AsyncSession, display_id: str) -> Project | None:
    return await db.scalar(select(Project).where(Project.display_id == display_id))


async def create_import_project(
    db: AsyncSession,
    *,
    display_id: str,
    name: str,
    owner_id: uuid.UUID,
    tool_bindings: dict | None,
) -> Project:
    project = Project(
        id=uuid.uuid4(),
        display_id=display_id,
        name=name,
        type_label="点云检测",
        type_key="lidar",
        data_type="lidar",
        scene_mode=True,
        prefer_same_scene_continuation=True,
        owner_id=owner_id,
        tool_bindings=tool_bindings or {},
        ai_enabled=False,
    )
    db.add(project)
    await db.flush()
    return project


def rename_class_bindings(
    project: Project,
    *,
    old_name: str,
    new_name: str,
    tool_unit_id: str | None,
) -> None:
    """Rename a class in the selected project tool unit(s), preserving aliases."""
    tool_bindings = dict(project.tool_bindings or {})
    if tool_unit_id is not None:
        if tool_unit_id not in tool_bindings:
            raise HTTPException(
                status_code=404, detail=f"tool_unit_id '{tool_unit_id}' 不存在"
            )
        target_units = [tool_unit_id]
    else:
        target_units = list(tool_bindings.keys())

    found_any = False
    for unit_id in target_units:
        binding = tool_bindings.get(unit_id)
        if not isinstance(binding, dict):
            continue
        classes_list = list(binding.get("classes") or [])
        names = [c.get("name") for c in classes_list if isinstance(c, dict)]
        if old_name not in names:
            continue
        if new_name in names:
            raise HTTPException(
                status_code=409,
                detail=f"类别 '{new_name}' 在工具单位 '{unit_id}' 已存在",
            )
        renamed = []
        for item in classes_list:
            if isinstance(item, dict) and item.get("name") == old_name:
                renamed.append({**item, "name": new_name})
            else:
                renamed.append(item)
        tool_bindings[unit_id] = {**binding, "classes": renamed}
        found_any = True

    if not found_any:
        raise HTTPException(status_code=404, detail=f"类别 '{old_name}' 不存在")

    project.tool_bindings = tool_bindings


_GUIDE_KEY_RE = re.compile(r"^projects/[0-9a-f-]{36}/guide/[0-9a-f-]{36}-[^/]+$")


def _sanitize_guide_asset_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    return name[:200] or "image"


def _assert_guide_asset_key(key: str, project_id: uuid.UUID) -> None:
    if not _GUIDE_KEY_RE.match(key) or not key.startswith(
        f"projects/{project_id}/guide/"
    ):
        raise HTTPException(status_code=404, detail="Guide asset not found")


def new_guide_asset_key(
    project_id: uuid.UUID, *, filename: str, content_type: str, size: int
) -> str:
    if content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type: {content_type}",
        )
    if size > MAX_GUIDE_ASSET_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    safe_name = _sanitize_guide_asset_filename(filename)
    return f"projects/{project_id}/guide/{uuid.uuid4()}-{safe_name}"


def record_guide_asset_upload(
    project: Project,
    *,
    key: str,
    original_name: str,
    content_type: str,
    size: int,
) -> dict[str, str | int]:
    assert_guide_asset_upload_target(project.id, key, content_type)

    if size <= 0 or size > MAX_GUIDE_ASSET_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Invalid upload size")

    entry = {
        "key": key,
        "original_name": _sanitize_guide_asset_filename(original_name),
        "content_type": content_type,
        "size": size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    assets = list(project.guide_assets or [])
    assets.append(entry)
    project.guide_assets = assets
    return entry


def remove_guide_asset(project: Project, key: str) -> dict[str, str]:
    _assert_guide_asset_key(key, project.id)
    existing = list(project.guide_assets or [])
    remaining = [entry for entry in existing if entry.get("key") != key]
    if len(remaining) == len(existing):
        raise HTTPException(status_code=404, detail="Asset not found")
    project.guide_assets = remaining
    return {"deleted": key}


def assert_registered_guide_asset_key(project: Project, key: str) -> None:
    _assert_guide_asset_key(key, project.id)
    keys = {entry.get("key") for entry in (project.guide_assets or [])}
    if key not in keys:
        raise HTTPException(status_code=404, detail="Asset not found")


def assert_guide_asset_upload_target(
    project_id: uuid.UUID, key: str, content_type: str
) -> None:
    _assert_guide_asset_key(key, project_id)
    if content_type not in ALLOWED_GUIDE_ASSET_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported content type")
