"""v0.10.11 · POST /projects 加 source_project_id 支持 (从已有项目复制配置).

覆盖:
- 仅给 source_project_id, 不带其它字段 — 配置字段全部从源克隆.
- source + 显式 name/ai_enabled — 显式字段覆盖源.
- 源项目带 ml_backend_id — 自动复制 backend (无需 ml_backend_source_id).
- 调用者对源项目无 view 权限 — 404 (与 assert_project_visible 语义一致).
- 不带 source_project_id — 原路径回归.
- 复制是深拷贝, 修改新项目的 JSONB 字段不污染源.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.user import User


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID, **overrides) -> Project:
    suffix = uuid.uuid4().hex[:8]
    defaults = dict(
        id=uuid.uuid4(),
        display_id=f"P-CL-{suffix}",
        name=f"src-{suffix}",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
    )
    defaults.update(overrides)
    proj = Project(**defaults)
    db.add(proj)
    await db.flush()
    return proj


async def _seed_backend(
    db: AsyncSession, project_id: uuid.UUID, name: str = "src-backend"
) -> MLBackendRegistry:
    # v0.19.0 ADR-0044 · backend 已上提为全局注册项 (无 project_id); url 全局唯一。
    # v0.23.3 ADR-0050 · 同时建 singleton pool (克隆 / 绑定经 pool 层)。
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name=name,
        url=f"http://example.test/{uuid.uuid4().hex[:8]}",
        is_interactive=True,
        state="connected",
        auth_method="bearer",
        auth_token="tok-secret",
        extra_params={"variant": "tiny"},
    )
    db.add(b)
    await db.flush()
    from app.services.ml_backend import MLBackendService

    await MLBackendService(db)._create_singleton_pool(b)
    return b


async def test_clone_copies_all_cloneable_fields(httpx_client, super_admin, db_session):
    user, token = super_admin
    src = await _seed_project(
        db_session,
        user.id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": "car", "color": "#ff0000", "order": 0, "alias": "car"},
                    {"name": "pedestrian", "color": "#00ff00", "order": 1},
                ],
                "attribute_schema": {
                    "fields": [
                        {
                            "key": "occluded",
                            "label": "遮挡",
                            "type": "boolean",
                            "required": False,
                        }
                    ]
                },
            }
        },
        ai_enabled=True,
        box_threshold=0.4,
        text_threshold=0.3,
        sampling="random",
        maximum_annotations=3,
        show_overlap_first=True,
        iou_dedup_threshold=0.85,
        rendering_config={"smoothImage": False, "controlPointsSize": 8},
        label_config={"choice": "single"},
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "克隆出来的项目",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 显式字段保留
    assert data["name"] == "克隆出来的项目"
    # 配置字段从源克隆
    assert data["classes"] == ["car", "pedestrian"]
    assert data["classes_config"]["car"]["color"] == "#ff0000"
    assert data["attribute_schema"]["fields"][0]["key"] == "occluded"
    assert data["ai_enabled"] is True
    assert data["box_threshold"] == pytest.approx(0.4, rel=1e-5)
    assert data["text_threshold"] == pytest.approx(0.3, rel=1e-5)
    assert data["sampling"] == "random"
    assert data["maximum_annotations"] == 3
    assert data["show_overlap_first"] is True
    assert data["iou_dedup_threshold"] == pytest.approx(0.85, rel=1e-5)
    assert data["rendering_config"]["smoothImage"] is False
    assert data["rendering_config"]["controlPointsSize"] == 8
    # 不复制运行时数据
    assert data["total_tasks"] == 0
    assert data["completed_tasks"] == 0
    # 新项目独立 id
    assert data["id"] != str(src.id)
    # 新项目独立 display_id
    assert data["display_id"] != src.display_id


async def test_clone_explicit_field_overrides_source(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    src = await _seed_project(
        db_session,
        user.id,
        ai_enabled=True,
        box_threshold=0.4,
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "覆盖测试",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
        # 显式 override
        "ai_enabled": False,
        "box_threshold": 0.6,
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 显式字段优先
    assert data["ai_enabled"] is False
    assert data["box_threshold"] == pytest.approx(0.6, rel=1e-5)


async def test_clone_auto_derives_ml_backend_from_source(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    src = await _seed_project(db_session, user.id, ai_enabled=True)
    backend = await _seed_backend(db_session, src.id, name="auto-clone-backend")
    # v0.23.3: 项目主绑定存 pool id (backend 经 _seed_backend 已有 singleton pool)。
    from app.services.ml_backend import MLBackendService

    src_pool = await MLBackendService(db_session)._pool_for_registry(backend.id)
    src.ml_backend_pool_id = src_pool.id if src_pool else None
    await db_session.flush()
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "自动 backend 复制",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # v0.19.0 ADR-0044 · clone 复用同一全局 registry id (不再复制新 backend row)
    assert data["ml_backend_id"] == str(backend.id)
    # 新项目获得一条启用关联
    from app.services.ml_backend import MLBackendService

    assert await MLBackendService(db_session).is_enabled(
        uuid.UUID(data["id"]), backend.id
    )


async def test_clone_without_view_permission_returns_404(
    httpx_client, project_admin, db_session
):
    """project_admin 复制别人 (非 self 所有) 的源项目 -> 404 (隐藏存在性)."""
    pm_user, pm_token = project_admin

    # 另一个 super_admin 拥有源项目 (project_admin 看不到)
    from app.core.security import hash_password

    other_owner = User(
        id=uuid.uuid4(),
        email=f"other-owner-{uuid.uuid4().hex[:6]}@test.local",
        name="other-owner",
        password_hash=hash_password("Test1234"),
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
    )
    db_session.add(other_owner)
    await db_session.flush()

    src = await _seed_project(db_session, other_owner.id)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {pm_token}"}
    body = {
        "name": "越权复制",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    # assert_project_visible 隐藏存在性, 返回 404 (project_admin 只能看 owner=self)
    assert resp.status_code == 404, resp.text


async def test_create_project_without_source_unchanged(httpx_client, super_admin):
    """回归: 不带 source_project_id 时, 走原路径, 不报错."""
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "普通新建",
        "type_label": "图像-检测",
        "type_key": "image-det",
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "普通新建"
    assert data["classes"] == []
    assert data["ai_enabled"] is False


async def test_clone_skips_annotation_guide_by_default(
    httpx_client, super_admin, db_session
):
    """v0.10.13 · E1 · annotation_guide / guide_assets 不在默认克隆白名单."""
    user, token = super_admin
    src = await _seed_project(
        db_session,
        user.id,
        annotation_guide="# 源项目指引\n请标注所有车辆.",
        guide_assets=[
            {
                "key": f"projects/{uuid.uuid4()}/guide/aaa-img.png",
                "original_name": "img.png",
                "content_type": "image/png",
                "size": 1024,
                "uploaded_at": "2026-05-18T00:00:00+00:00",
            }
        ],
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "默认不带 guide",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["annotation_guide"] is None
    assert data["guide_assets"] == []


async def test_clone_with_copy_annotation_guide_flag(
    httpx_client, super_admin, db_session
):
    """v0.10.13 · E1 · copy_annotation_guide=true 时同时复制 guide + assets."""
    user, token = super_admin
    asset_entry = {
        "key": f"projects/{uuid.uuid4()}/guide/bbb-pic.jpg",
        "original_name": "pic.jpg",
        "content_type": "image/jpeg",
        "size": 2048,
        "uploaded_at": "2026-05-18T01:00:00+00:00",
    }
    src = await _seed_project(
        db_session,
        user.id,
        annotation_guide="# 源指引",
        guide_assets=[asset_entry],
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "带 guide",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
        "copy_annotation_guide": True,
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["annotation_guide"] == "# 源指引"
    assert len(data["guide_assets"]) == 1
    assert data["guide_assets"][0]["key"] == asset_entry["key"]


async def test_copy_annotation_guide_without_source_returns_400(
    httpx_client, super_admin
):
    """v0.10.13 · E1 · copy_annotation_guide 必须配合 source_project_id."""
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "孤儿 copy flag",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "copy_annotation_guide": True,
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 400, resp.text


async def test_apply_template_with_explicit_field_override(
    httpx_client, super_admin, db_session
):
    """v0.10.14 · E2 · template_id 路径: 显式给字段优先于模板载荷."""
    from app.db.models.project_template import ProjectTemplate

    user, token = super_admin
    t = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=f"PT-TX-{uuid.uuid4().hex[:6]}",
        name="模板",
        type_label="图像-检测",
        type_key="image-det",
        tool_bindings={
            "bbox": {"enabled": True, "classes": [{"name": "car", "order": 0}]}
        },
        ai_enabled=True,
        scope="public",
        created_by=user.id,
    )
    db_session.add(t)
    await db_session.flush()
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "应用模板带覆盖",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "template_id": str(t.id),
        # 显式覆盖
        "ai_enabled": False,
        "classes": ["pedestrian"],
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ai_enabled"] is False
    assert data["classes"] == ["pedestrian"]


async def test_apply_nonexistent_template_404(httpx_client, super_admin):
    """v0.10.14 · E2 · template_id 指向不存在记录 → 404."""
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "幽灵模板",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "template_id": str(uuid.uuid4()),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 404, resp.text


async def test_clone_jsonb_is_deep_copied(httpx_client, super_admin, db_session):
    """修改新项目的 tool_bindings 不应该污染源项目 (避免共享 JSONB 引用)."""
    user, token = super_admin
    src = await _seed_project(
        db_session,
        user.id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "a", "color": "#111111", "order": 0}],
            }
        },
    )
    await db_session.commit()
    src_id = src.id

    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "深拷贝校验",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src_id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=headers)
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    # 走 PATCH 改新项目 classes_config
    patch = await httpx_client.patch(
        f"/api/v1/projects/{new_id}",
        json={"classes_config": {"a": {"color": "#999999", "order": 0}}},
        headers=headers,
    )
    assert patch.status_code == 200

    # 重新读源项目, color 应保持 #111111
    db_session.expire_all()
    refreshed = await db_session.get(Project, src_id)
    assert refreshed is not None
    assert refreshed.tool_bindings["bbox"]["classes"][0]["color"] == "#111111"
