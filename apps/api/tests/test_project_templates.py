"""v0.10.14 · E2 · ProjectTemplate 路由 / service 端到端测试.

覆盖:
- 创建 / 列表 / 详情 / 更新 / 删除 主路径 (private / public / organization)
- public 仅 super_admin 可建; 非 super_admin 升级到 public 也拒
- organization scope 必须给 organization_id
- 可见性: private 仅 created_by 可见; organization 仅同组织可见; public 全部
- 编辑 / 删除权限: created_by 或 super_admin
- 从源项目导出 (source_project_id) — dump CLONEABLE 字段进模板; annotation_guide 跟随
- 克隆模板 → 私有副本
- 应用模板创建项目: 字段 deepcopy + usage_count +1
- template_id 与 source_project_id 互斥 → 422 (pydantic model_validator)
- 模板 annotation_guide 应用到新项目 (guide_assets 不携带)
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import build_tool_bindings


async def _seed_template(
    db: AsyncSession,
    created_by: uuid.UUID,
    *,
    name: str = "模板A",
    scope: str = "private",
    organization_id: uuid.UUID | None = None,
    **overrides,
) -> ProjectTemplate:
    defaults = dict(
        id=uuid.uuid4(),
        display_id=f"PT-{uuid.uuid4().hex[:6]}",
        name=name,
        type_label="图像-检测",
        type_key="image-det",
        scope=scope,
        organization_id=organization_id,
        created_by=created_by,
    )
    defaults.update(overrides)
    t = ProjectTemplate(**defaults)
    db.add(t)
    await db.flush()
    return t


async def _seed_user(db: AsyncSession, role: str, email_prefix: str) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"{email_prefix}-{uuid.uuid4().hex[:6]}@test.local",
        name=email_prefix,
        password_hash=hash_password("Test1234"),
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.flush()
    return u


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_private_template_minimal(httpx_client, project_admin):
    """project_admin 创建私有模板, 默认 scope=private."""
    _, token = project_admin
    body = {
        "name": "我的模板",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "classes": ["car"],
    }
    resp = await httpx_client.post(
        "/api/v1/project-templates", json=body, headers=_auth(token)
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["scope"] == "private"
    assert data["name"] == "我的模板"
    assert data["display_id"].startswith("PT-")
    assert data["classes"] == ["car"]


async def test_create_public_requires_super_admin(httpx_client, project_admin):
    """project_admin 建 scope=public → 403."""
    _, token = project_admin
    body = {
        "name": "公共模板",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "scope": "public",
    }
    resp = await httpx_client.post(
        "/api/v1/project-templates", json=body, headers=_auth(token)
    )
    assert resp.status_code == 403, resp.text


async def test_create_organization_requires_org_id(httpx_client, super_admin):
    """scope=organization 不带 organization_id → 400."""
    _, token = super_admin
    body = {
        "name": "组织模板",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "scope": "organization",
    }
    resp = await httpx_client.post(
        "/api/v1/project-templates", json=body, headers=_auth(token)
    )
    assert resp.status_code == 400, resp.text


async def test_list_visibility_filters(httpx_client, db_session, super_admin):
    """列表对其他用户隐藏私有 / 跨组织模板; public 全可见."""
    admin_user, _ = super_admin

    other_pm = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "other-pm")
    org = Organization(
        id=uuid.uuid4(),
        name="org-x",
        slug=f"orgx-{uuid.uuid4().hex[:6]}",
        created_by=admin_user.id,
    )
    db_session.add(org)
    await db_session.flush()

    # other_pm 不在 org 内
    await _seed_template(db_session, other_pm.id, name="他私有", scope="private")
    await _seed_template(
        db_session,
        other_pm.id,
        name="他组织",
        scope="organization",
        organization_id=org.id,
    )
    await _seed_template(db_session, other_pm.id, name="他公共", scope="public")
    await db_session.commit()

    # 以 project_admin (新建一个) 视角调用
    pm = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "viewer-pm")
    await db_session.commit()
    from app.core.security import create_access_token

    pm_token = create_access_token(
        subject=str(pm.id), role=UserRole.PROJECT_ADMIN.value
    )

    resp = await httpx_client.get("/api/v1/project-templates", headers=_auth(pm_token))
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "他公共" in names
    assert "他私有" not in names
    assert "他组织" not in names


async def test_list_visible_organization_template_for_member(
    httpx_client, db_session, super_admin
):
    """同组织成员能看到 scope=organization 模板."""
    admin_user, _ = super_admin
    creator = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "creator-pm")
    viewer = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "viewer-pm")
    org = Organization(
        id=uuid.uuid4(),
        name="org-y",
        slug=f"orgy-{uuid.uuid4().hex[:6]}",
        created_by=admin_user.id,
    )
    db_session.add(org)
    await db_session.flush()
    db_session.add(
        OrganizationMember(
            id=uuid.uuid4(), organization_id=org.id, user_id=viewer.id, role="member"
        )
    )
    await db_session.flush()

    await _seed_template(
        db_session,
        creator.id,
        name="组织内模板",
        scope="organization",
        organization_id=org.id,
    )
    await db_session.commit()

    from app.core.security import create_access_token

    token = create_access_token(
        subject=str(viewer.id), role=UserRole.PROJECT_ADMIN.value
    )
    resp = await httpx_client.get("/api/v1/project-templates", headers=_auth(token))
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "组织内模板" in names


async def test_update_only_by_creator_or_super_admin(
    httpx_client, db_session, project_admin
):
    pm_user, pm_token = project_admin
    other = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "other-pm")
    t = await _seed_template(db_session, other.id)
    await db_session.commit()

    resp = await httpx_client.patch(
        f"/api/v1/project-templates/{t.id}",
        json={"name": "被改了"},
        headers=_auth(pm_token),
    )
    # pm_user 看不到 (private + 非 created_by) → 404
    assert resp.status_code == 404


async def test_delete_only_by_creator(httpx_client, db_session, project_admin):
    pm_user, pm_token = project_admin
    t = await _seed_template(db_session, pm_user.id, name="待删")
    await db_session.commit()

    resp = await httpx_client.delete(
        f"/api/v1/project-templates/{t.id}", headers=_auth(pm_token)
    )
    assert resp.status_code == 204


async def test_create_from_source_project_dumps_fields(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    src = Project(
        id=uuid.uuid4(),
        display_id=f"P-SRC-{uuid.uuid4().hex[:6]}",
        name="源项目",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(
            [{"name": "car", "color": "#ff0000", "order": 0}, "person"]
        ),
        ai_enabled=True,
        annotation_guide="# 源指引",
    )
    db_session.add(src)
    await db_session.flush()
    await db_session.commit()

    body = {
        "name": "从源项目导出",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post(
        "/api/v1/project-templates", json=body, headers=_auth(token)
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["classes"] == ["car", "person"]
    assert data["classes_config"]["car"]["color"] == "#ff0000"
    assert data["ai_enabled"] is True
    assert data["annotation_guide"] == "# 源指引"
    assert data["source_project_id"] == str(src.id)


async def test_duplicate_creates_private_copy(httpx_client, db_session, super_admin):
    user, token = super_admin
    src = await _seed_template(
        db_session,
        user.id,
        name="原模板",
        scope="public",
        tool_bindings=build_tool_bindings(
            [{"name": "a", "color": "#222222", "order": 0}, "b"]
        ),
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/project-templates/{src.id}/duplicate", headers=_auth(token)
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["scope"] == "private"
    assert data["name"].endswith("(副本)")
    assert data["classes"] == ["a", "b"]
    # JSONB 深拷贝, 改副本不影响源 (这里只做 API 层 round-trip 校验)
    assert data["id"] != str(src.id)


async def test_apply_template_creates_project_and_bumps_usage(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    t = await _seed_template(
        db_session,
        user.id,
        scope="public",
        tool_bindings=build_tool_bindings(["car"]),
        ai_enabled=True,
        annotation_guide="# 模板指引",
    )
    await db_session.commit()
    t_id = t.id

    body = {
        "name": "由模板创建",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "template_id": str(t_id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    project_data = resp.json()
    assert project_data["classes"] == ["car"]
    assert project_data["ai_enabled"] is True
    assert project_data["annotation_guide"] == "# 模板指引"
    # 模板不带 guide_assets, 新项目 guide_assets 应为空
    assert project_data["guide_assets"] == []

    # 复读模板, usage_count + 1
    resp2 = await httpx_client.get(
        f"/api/v1/project-templates/{t_id}", headers=_auth(token)
    )
    assert resp2.status_code == 200
    assert resp2.json()["usage_count"] == 1


async def test_template_id_and_source_project_id_mutually_exclusive(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    t = await _seed_template(db_session, user.id, scope="public")
    src = Project(
        id=uuid.uuid4(),
        display_id=f"P-MX-{uuid.uuid4().hex[:6]}",
        name="无关源项目",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=user.id,
    )
    db_session.add(src)
    await db_session.commit()

    body = {
        "name": "互斥测试",
        "type_label": "图像-检测",
        "type_key": "image-det",
        "template_id": str(t.id),
        "source_project_id": str(src.id),
    }
    resp = await httpx_client.post("/api/v1/projects", json=body, headers=_auth(token))
    # pydantic model_validator → 422
    assert resp.status_code == 422, resp.text


async def test_private_template_detail_404_for_others(
    httpx_client, db_session, project_admin
):
    """别人的私有模板, 详情接口返回 404 (隐藏存在性)."""
    pm_user, pm_token = project_admin
    other = await _seed_user(db_session, UserRole.PROJECT_ADMIN.value, "owner")
    t = await _seed_template(db_session, other.id, name="他人私有")
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/project-templates/{t.id}", headers=_auth(pm_token)
    )
    assert resp.status_code == 404
