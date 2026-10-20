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
from app.services.guide_asset_contracts import (
    ALLOWED_GUIDE_ASSET_TYPES,
    MAX_GUIDE_ASSET_SIZE_BYTES,
)
from app.services.project_template import (
    TEMPLATE_PAYLOAD_FIELDS,
    assert_can_create_scope,
    assert_template_creation_scope,
    assert_template_visible,
    can_edit_template,
    dump_project_to_template_payload,
)


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


def derive_tool_unit_for_type_key(type_key: str | None) -> str:
    """旧 type_key → 默认 tool_unit_id 推断 (与 migration 0072 同规则).

    image-seg → region; 其它 → bbox 占位.
    """
    return "region" if type_key == "image-seg" else "bbox"


def coalesce_legacy_into_tool_bindings(
    payload: dict[str, Any],
    existing_tool_bindings: dict[str, Any] | None,
    type_key: str | None,
) -> None:
    """旧客户端只传 classes / classes_config / attribute_schema 时,
    反向派生进 payload["tool_bindings"], 保证 tool_bindings 是单源真值.

    - 若 payload 显式包含 tool_bindings: 跳过 (新客户端直接走)
    - 若 payload 既无 tool_bindings 又无任何 legacy 字段改动: 跳过
    - 否则: 按 type_key 选默认 unit (bbox / region), 把扁平字段合并进该 unit, 其它 unit
      保留 existing_tool_bindings 中的值. classes_config 优先于 classes (前者携带颜色 / 排序);
      仅给 classes list[str] 时只重建 name + order.
    """
    # 空 dict 等价于"未给", 触发反向派生 (模板 fixture 写空 dict 默认值,
    # 创建项目时应走 legacy classes / classes_config 派生路径).
    if payload.get("tool_bindings"):
        return
    has_legacy = any(
        k in payload for k in ("classes", "classes_config", "attribute_schema")
    )
    if not has_legacy:
        return

    unit = derive_tool_unit_for_type_key(type_key)
    merged = dict(existing_tool_bindings or {})
    prev_unit = merged.get(unit) or {}
    prev_classes_list = list(prev_unit.get("classes") or [])

    cfg_map = payload.get("classes_config")
    classes_strs = payload.get("classes")

    # 决定 class 名字列表 (来源优先级: classes list[str] > classes_config keys >
    # existing unit classes); 颜色 / order / alias 从 cfg_map 配套字段查 (若有).
    names: list[str]
    if classes_strs is not None:
        names = list(classes_strs)
    elif cfg_map:  # 非空 dict
        names = list(cfg_map.keys())
    else:
        names = [c.get("name") for c in prev_classes_list if isinstance(c, dict)]

    classes_list: list[dict] = []
    for i, name in enumerate(names):
        if not name:
            continue
        entry: dict = {"name": name}
        cfg = (cfg_map or {}).get(name)
        if isinstance(cfg, dict):
            if cfg.get("color"):
                entry["color"] = cfg["color"]
            if cfg.get("order") is not None:
                entry["order"] = cfg["order"]
            if cfg.get("alias"):
                entry["alias"] = cfg["alias"]
        else:
            # 没 cfg 时, 尝试从 prev_classes_list 取颜色 / alias (PATCH attribute_schema
            # 时保留原配色; 但若用户显式给 classes 改名, 颜色按 name 匹配回查)
            for prev in prev_classes_list:
                if isinstance(prev, dict) and prev.get("name") == name:
                    if prev.get("color"):
                        entry["color"] = prev["color"]
                    if prev.get("order") is not None:
                        entry["order"] = prev["order"]
                    if prev.get("alias"):
                        entry["alias"] = prev["alias"]
                    break
        entry.setdefault("order", i)
        classes_list.append(entry)

    attribute_schema = payload.get("attribute_schema")
    if attribute_schema is None:
        attribute_schema = prev_unit.get("attribute_schema") or {"fields": []}

    merged[unit] = {
        "enabled": True,
        "classes": classes_list,
        "attribute_schema": attribute_schema,
    }
    payload["tool_bindings"] = merged


# 媒体维度 data_type ↔ 兼容 type_key 互推.
# data_type 只到 image/video/lidar 粒度, 无法区分 video-track vs video-mm;
# 派生 type_key 仅在新建项目只给 data_type 时兜底一个默认子类型, 保旧分流不破.
_DATA_TYPE_TO_LEGACY_TYPE_KEY = {
    "image": "image-det",
    "video": "video-track",
    "lidar": "lidar",
}


def data_type_from_type_key(type_key: str | None) -> str:
    """把 type_key 推导到媒体维度 data_type (与 alembic 0082 回填同规则)."""
    if type_key and type_key.startswith("video"):
        return "video"
    if type_key == "lidar":
        return "lidar"
    return "image"


def legacy_type_key_from_data_type(data_type: str | None) -> str:
    """新建项目只给 data_type 时, 派生一个兼容 type_key 默认值."""
    return _DATA_TYPE_TO_LEGACY_TYPE_KEY.get(data_type or "image", "image-det")


def assert_project_kind_consistent(type_key: str | None, data_type: str | None) -> None:
    """断言 type_key 与 data_type 在媒体维度一致, 不一致抛 422.

    前端用 `type_key === "lidar"` 入 3D Stage, 后端 manifest 用
    `data_type == "lidar"` 放行点云端点; 两侧落库不一致就会出现「前端进了
    3D 台, 后端拒提供点云 manifest」之类的撕裂. 创建 / 更新接口必经此关.

    其中一个缺失时不抛 (上游 cross-fill 会补), 两侧都给且不一致才抛.
    """
    if not type_key or not data_type:
        return
    derived = data_type_from_type_key(type_key)
    if derived != data_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"type_key={type_key!r} implies data_type={derived!r}, "
                f"got data_type={data_type!r} (媒体维度必须一致)"
            ),
        )


def resolve_project_kind(
    type_key: str | None,
    data_type: str | None,
    *,
    inherited_type_key: str | None = None,
    inherited_data_type: str | None = None,
) -> tuple[str, str]:
    """Resolve a create request's project kind before legacy class conversion."""
    type_key = type_key or None
    data_type = data_type or None
    if type_key and data_type:
        assert_project_kind_consistent(type_key, data_type)
    elif type_key:
        data_type = data_type_from_type_key(type_key)
    elif data_type:
        type_key = legacy_type_key_from_data_type(data_type)
    else:
        type_key = inherited_type_key or None
        data_type = inherited_data_type or None
        if type_key and data_type:
            assert_project_kind_consistent(type_key, data_type)
        elif type_key:
            data_type = data_type_from_type_key(type_key)
        elif data_type:
            type_key = legacy_type_key_from_data_type(data_type)
        else:
            data_type = "image"
            type_key = legacy_type_key_from_data_type(data_type)

    assert type_key is not None and data_type is not None
    return type_key, data_type


def lookup_classes_for_tool_unit(
    tool_bindings: dict[str, Any] | None,
    tool_unit_id: str,
) -> list[str]:
    """给定 tool_unit_id, 返回该工具单位下可用的类名集合 (空 list = 未配置, 放行)."""
    if not tool_bindings:
        return []
    binding = tool_bindings.get(tool_unit_id)
    if not isinstance(binding, dict) or not binding.get("enabled"):
        return []
    return [
        cls["name"]
        for cls in (binding.get("classes") or [])
        if isinstance(cls, dict) and cls.get("name")
    ]


def prepare_project_create_payload(
    payload: dict[str, Any],
    *,
    inherited_type_key: str | None = None,
    inherited_data_type: str | None = None,
) -> None:
    """Normalize the request-owned project fields before source/template fallback."""

    from app.services.project_kind import scene_mode_allowed

    payload["type_key"], payload["data_type"] = resolve_project_kind(
        payload.get("type_key"),
        payload.get("data_type"),
        inherited_type_key=inherited_type_key,
        inherited_data_type=inherited_data_type,
    )
    if payload.get("scene_mode"):
        if not scene_mode_allowed(payload.get("data_type")):
            raise HTTPException(
                status_code=422, detail="scene_mode 仅支持 image/lidar 项目"
            )
        payload.setdefault("prefer_same_scene_continuation", True)

    coalesce_legacy_into_tool_bindings(
        payload, existing_tool_bindings=None, type_key=payload.get("type_key")
    )
    for legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(legacy_key, None)


async def assert_project_kind_update_allowed(
    db: AsyncSession, project: Project, payload: dict[str, Any]
) -> None:
    from app.services.project_kind import (
        ProjectKind,
        canonical_media_kind,
        dataset_has_scenes,
        dataset_kind,
        kind_mismatch_detail,
        scene_mode_allowed,
    )

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
    target = ProjectKind(
        data_type=canonical_media_kind(target_data_type),
        scene_mode=target_scene_mode,
    )
    for dataset in rows.scalars().all():
        has_scenes = await dataset_has_scenes(db, dataset.id)
        mismatch = kind_mismatch_detail(
            target, dataset_kind(dataset, has_scenes=has_scenes)
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
    for legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(legacy_key, None)
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
        actor,
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
    for legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(legacy_key, None)

    if source_project_id is not None:
        merged = dump_project_to_template_payload(source_project)
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
    if not can_edit_template(template, actor):
        raise HTTPException(status_code=403, detail="仅模板创建者或超级管理员可编辑")

    new_scope = payload.get("scope", template.scope)
    if new_scope != template.scope:
        assert_can_create_scope(new_scope, actor)
        if new_scope == "organization" and not payload.get(
            "organization_id", template.organization_id
        ):
            raise HTTPException(
                status_code=400,
                detail="organization scope 必须指定 organization_id",
            )

    coalesce_legacy_into_tool_bindings(
        payload, template.tool_bindings, payload.get("type_key") or template.type_key
    )
    for legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(legacy_key, None)
    for key, value in payload.items():
        setattr(template, key, value)
    return template


async def delete_template(
    db: AsyncSession, *, template: ProjectTemplate, actor: User
) -> None:
    await assert_template_visible(db, template, actor)
    if not can_edit_template(template, actor):
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
