"""Project workspace commands and project/template write helpers."""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.dataset import Dataset, ProjectDataset
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from aap_workspace.configuration_policy import (
    TEMPLATE_PAYLOAD_FIELDS,
    assert_template_creation_scope,
    can_edit_template,
    coalesce_legacy_into_tool_bindings,
    dataset_kind,
    discard_legacy_project_configuration_fields,
    dump_project_to_template_payload,
    kind_mismatch_detail,
    project_kind,
    resolve_project_kind,
    scene_mode_allowed,
)
from app.services.guide_asset_contracts import (
    ALLOWED_GUIDE_ASSET_TYPES,
    MAX_GUIDE_ASSET_SIZE_BYTES,
)
from app.services.project_kind import dataset_has_scenes
from app.services.project_template import assert_template_visible


def _configuration_actor(actor: User) -> dict[str, Any]:
    role = getattr(actor.role, "value", actor.role)
    return {"id": actor.id, "role": role}


def set_primary_backend_binding(*, project: Project, pool_id: UUID | None) -> None:
    project.ml_backend_pool_id = pool_id


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


async def assert_project_kind_update_allowed(
    db: AsyncSession, project: Project, payload: dict[str, Any]
) -> None:
    if "scene_mode" in payload and payload["scene_mode"] != project.scene_mode:
        if (project.total_tasks or 0) > 0:
            raise HTTPException(
                status_code=422,
                detail="已建 task 的项目不可切换 scene 模式,请先解绑数据集",
            )

    target_data_type = payload.get("data_type", project.data_type)
    target_scene_mode = bool(payload.get("scene_mode", project.scene_mode))
    if target_scene_mode and not scene_mode_allowed(target_data_type):
        raise HTTPException(
            status_code=422, detail="scene_mode 仅支持 image/lidar 项目"
        )

    if "data_type" not in payload and "scene_mode" not in payload:
        return

    rows = await db.execute(
        select(Dataset)
        .join(ProjectDataset, ProjectDataset.dataset_id == Dataset.id)
        .where(ProjectDataset.project_id == project.id)
    )
    target = project_kind(target_data_type, target_scene_mode)
    for dataset in rows.scalars().all():
        has_scenes = await dataset_has_scenes(db, dataset.id)
        mismatch = kind_mismatch_detail(
            target, dataset_kind(dataset.data_type, has_scenes)
        )
        if mismatch:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"已有数据集 {dataset.display_id} 与目标项目 kind 不匹配: "
                    f"{mismatch}; 请先解绑数据集"
                ),
            )


async def create_project_record(
    db: AsyncSession, *, owner_id: uuid.UUID, payload: dict[str, Any]
) -> Project:
    from app.services.display_id import next_display_id

    project = Project(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "projects"),
        owner_id=owner_id,
        **payload,
    )
    db.add(project)
    await db.flush()
    return project


def apply_project_update(project: Project, payload: dict[str, Any]) -> None:
    coalesce_legacy_into_tool_bindings(
        payload,
        existing_tool_bindings=project.tool_bindings,
        type_key=payload.get("type_key") or project.type_key,
    )
    discard_legacy_project_configuration_fields(payload)
    for key, value in payload.items():
        setattr(project, key, value)


async def create_template(
    db: AsyncSession,
    *,
    actor: User,
    payload: dict[str, Any],
    source_project: Any | None = None,
) -> ProjectTemplate:
    """Create a template from request data and an optional visible project."""

    from app.services.display_id import next_display_id

    if actor.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可创建模板"
        )

    assert_template_creation_scope(
        payload.get("scope", "private"),
        payload.get("organization_id"),
        _configuration_actor(actor),
    )

    source_project_id = payload.pop("source_project_id", None)
    payload["type_key"], payload["data_type"] = resolve_project_kind(
        payload.get("type_key"),
        payload.get("data_type"),
        inherited_type_key=(
            source_project.type_key if source_project is not None else None
        ),
        inherited_data_type=(
            source_project.data_type if source_project is not None else None
        ),
    )
    coalesce_legacy_into_tool_bindings(payload, None, payload["type_key"])
    discard_legacy_project_configuration_fields(payload)

    if source_project_id is not None:
        source_fields = {
            field: getattr(source_project, field) for field in TEMPLATE_PAYLOAD_FIELDS
        }
        source_fields["annotation_guide"] = source_project.annotation_guide
        merged = dump_project_to_template_payload(source_fields)
        for key, value in merged.items():
            payload.setdefault(key, value)
        payload.setdefault("name", f"{source_project.name} 模板")
        payload.setdefault("type_label", source_project.type_label)

    if (
        not payload.get("name")
        or not payload.get("type_label")
        or not payload.get("type_key")
    ):
        raise HTTPException(
            status_code=400,
            detail="name / type_label / type_key 必填 (或通过 source_project_id 兜底)",
        )

    template = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "project_templates"),
        created_by=actor.id,
        source_project_id=source_project_id,
        **payload,
    )
    db.add(template)
    return template


async def update_template(
    db: AsyncSession,
    *,
    template: ProjectTemplate,
    actor: User,
    payload: dict[str, Any],
) -> ProjectTemplate:

    await assert_template_visible(db, template, actor)
    if not can_edit_template(
        {"created_by": template.created_by},
        _configuration_actor(actor),
    ):
        raise HTTPException(status_code=403, detail="仅模板创建者或超级管理员可编辑")

    new_scope = payload.get("scope", template.scope)
    if new_scope != template.scope:
        assert_template_creation_scope(
            new_scope,
            payload.get("organization_id", template.organization_id),
            _configuration_actor(actor),
        )

    coalesce_legacy_into_tool_bindings(
        payload, template.tool_bindings, payload.get("type_key") or template.type_key
    )
    discard_legacy_project_configuration_fields(payload)
    for key, value in payload.items():
        setattr(template, key, value)
    return template


async def delete_template(
    db: AsyncSession, *, template: ProjectTemplate, actor: User
) -> None:
    await assert_template_visible(db, template, actor)
    if not can_edit_template(
        {"created_by": template.created_by},
        _configuration_actor(actor),
    ):
        raise HTTPException(status_code=403, detail="仅模板创建者或超级管理员可删除")
    await db.delete(template)


async def duplicate_template(
    db: AsyncSession,
    *,
    source: ProjectTemplate,
    actor: User,
) -> ProjectTemplate:
    from app.services.display_id import next_display_id

    if actor.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可克隆模板"
        )
    await assert_template_visible(db, source, actor)

    payload = {field: getattr(source, field) for field in TEMPLATE_PAYLOAD_FIELDS}
    for key, value in list(payload.items()):
        if isinstance(value, (dict, list)):
            payload[key] = copy.deepcopy(value)

    duplicate = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "project_templates"),
        name=f"{source.name} (副本)",
        description=source.description,
        annotation_guide=source.annotation_guide,
        scope="private",
        organization_id=None,
        created_by=actor.id,
        source_project_id=source.source_project_id,
        **payload,
    )
    db.add(duplicate)
    return duplicate


def increment_template_usage(template: ProjectTemplate) -> None:
    template.usage_count = (template.usage_count or 0) + 1
