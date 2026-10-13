"""v0.10.13 · E1 · /projects/{id}/guide-assets/* endpoints.

覆盖:
- upload-init 成功签发 URL + key 形如 projects/{id}/guide/{uuid}-{filename}
- 非 owner / 非 super_admin 写入 → 403
- 不支持的 content_type → 400
- upload-complete 成功 append entry 到 project.guide_assets
- upload-complete 校验 storage 上文件存在 (head_object), 不存在 → 400
- DELETE 同步移除 entry + 调 storage.delete_object
- sign-url 对项目可见用户返回签名 URL，非成员或已移除成员 → 404
- key 不属于当前项目 → 404 (防越权)
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from tests.factory import create_membership


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID) -> Project:
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-GA-{uuid.uuid4().hex[:6]}",
        name="guide-asset-test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


def _add_guide_asset(proj: Project) -> str:
    key = f"projects/{proj.id}/guide/{uuid.uuid4()}-show.png"
    proj.guide_assets = [
        {
            "key": key,
            "original_name": "show.png",
            "content_type": "image/png",
            "size": 100,
            "uploaded_at": "2026-05-18T00:00:00+00:00",
        }
    ]
    return key


async def _add_member(
    db: AsyncSession, proj: Project, user_id: uuid.UUID, role: str
) -> ProjectMember:
    return await create_membership(
        db,
        project_id=proj.id,
        user_id=user_id,
        role=role,
        assigned_by=proj.owner_id,
    )


def _patch_storage(monkeypatch, *, upload_present=True, content_length=1024):
    """统一 mock storage_service 三个 IO 方法, 默认 head_object 返回有效尺寸."""
    monkeypatch.setattr(
        "app.api.v1.guide_assets.storage_service.generate_upload_url",
        lambda key, content_type="application/octet-stream", expires_in=900, bucket=None: (
            f"http://storage.local/put/{key}"
        ),
    )
    monkeypatch.setattr(
        "app.api.v1.guide_assets.storage_service.generate_download_url",
        lambda key, expires_in=3600, bucket=None: f"http://storage.local/get/{key}",
    )
    monkeypatch.setattr(
        "app.api.v1.guide_assets.storage_service.verify_upload",
        lambda key, bucket=None: (
            {"ContentLength": content_length} if upload_present else None
        ),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.api.v1.guide_assets.storage_service.delete_object",
        lambda key, bucket=None: deleted.append(key),
    )
    return deleted


async def test_upload_init_signs_url_and_returns_scoped_key(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-init",
        json={"filename": "hint.png", "content_type": "image/png", "size": 1024},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["key"].startswith(f"projects/{proj.id}/guide/")
    assert data["key"].endswith("-hint.png")
    assert data["upload_url"].startswith("http://storage.local/put/")
    assert data["expires_in"] == 900


async def test_upload_init_rejects_disallowed_content_type(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-init",
        json={
            "filename": "boom.exe",
            "content_type": "application/x-msdownload",
            "size": 1024,
        },
        headers=headers,
    )
    assert resp.status_code == 400


async def test_upload_init_forbidden_to_non_owner(
    httpx_client, super_admin, project_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch)
    super_user, _ = super_admin
    pm_user, pm_token = project_admin
    proj = await _seed_project(db_session, super_user.id)
    # Visible work member: a non-owner project administrator is denied management
    # with 403 instead of an invisible-project 404.
    await _add_member(db_session, proj, pm_user.id, "annotator")
    await db_session.commit()

    headers = {"Authorization": f"Bearer {pm_token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-init",
        json={"filename": "x.png", "content_type": "image/png", "size": 100},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


async def test_upload_complete_appends_entry(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch, content_length=2048)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    key = f"projects/{proj.id}/guide/{uuid.uuid4()}-cat.png"
    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-complete",
        json={"key": key, "original_name": "cat.png", "content_type": "image/png"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["key"] == key
    assert data["size"] == 2048
    assert data["original_name"] == "cat.png"

    # v0.10.13 · 通过再 GET /projects/{id} 验证 guide_assets 持久化, 避免在 dependency-
    # override session 上直接 expire+select (与 conftest SAVEPOINT 不兼容).
    detail = await httpx_client.get(f"/api/v1/projects/{proj.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    keys = [e["key"] for e in detail.json()["guide_assets"]]
    assert key in keys


async def test_upload_complete_rejects_missing_storage_object(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch, upload_present=False)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    key = f"projects/{proj.id}/guide/{uuid.uuid4()}-x.png"
    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-complete",
        json={"key": key, "original_name": "x.png", "content_type": "image/png"},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_upload_complete_rejects_foreign_key(
    httpx_client, super_admin, db_session, monkeypatch
):
    """key 不属于该项目 (路径前缀不匹配) → 404, 防越权写入."""
    _patch_storage(monkeypatch)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    other_id = uuid.uuid4()
    await db_session.commit()

    foreign_key = f"projects/{other_id}/guide/{uuid.uuid4()}-x.png"
    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-complete",
        json={
            "key": foreign_key,
            "original_name": "x.png",
            "content_type": "image/png",
        },
        headers=headers,
    )
    assert resp.status_code == 404


async def test_delete_asset_removes_entry_and_calls_storage(
    httpx_client, super_admin, db_session, monkeypatch
):
    deleted = _patch_storage(monkeypatch)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)

    existing_key = f"projects/{proj.id}/guide/{uuid.uuid4()}-old.png"
    proj.guide_assets = [
        {
            "key": existing_key,
            "original_name": "old.png",
            "content_type": "image/png",
            "size": 100,
            "uploaded_at": "2026-05-18T00:00:00+00:00",
        }
    ]
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.request(
        "DELETE",
        f"/api/v1/projects/{proj.id}/guide-assets",
        params={"key": existing_key},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert existing_key in deleted

    detail = await httpx_client.get(f"/api/v1/projects/{proj.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["guide_assets"] == []


@pytest.mark.parametrize("as_super_admin", [False, True])
async def test_sign_url_returns_short_lived_url(
    httpx_client,
    super_admin,
    project_admin,
    db_session,
    monkeypatch,
    as_super_admin,
):
    _patch_storage(monkeypatch)
    user, token = project_admin
    proj = await _seed_project(db_session, user.id)
    key = _add_guide_asset(proj)
    await db_session.commit()

    if as_super_admin:
        _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/guide-assets/sign-url",
        params={"key": key},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["url"].startswith("http://storage.local/get/")
    assert data["expires_in"] == 3600


@pytest.mark.parametrize("role", ["annotator", "reviewer", "viewer"])
async def test_project_members_can_read_guide_images(
    httpx_client,
    project_admin,
    annotator,
    reviewer,
    db_session,
    monkeypatch,
    role,
):
    _patch_storage(monkeypatch)
    owner, _ = project_admin
    user, token = reviewer if role == "reviewer" else annotator
    proj = await _seed_project(db_session, owner.id)
    key = _add_guide_asset(proj)
    await _add_member(db_session, proj, user.id, role)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/guide-assets/sign-url",
        params={"key": key},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": f"http://storage.local/get/{key}", "expires_in": 3600}


async def test_removed_member_cannot_request_new_guide_image_urls(
    httpx_client, project_admin, annotator, db_session, monkeypatch
):
    _patch_storage(monkeypatch)
    owner, _ = project_admin
    user, token = annotator
    proj = await _seed_project(db_session, owner.id)
    key = _add_guide_asset(proj)
    member = await _add_member(db_session, proj, user.id, "annotator")
    await db_session.commit()
    endpoint = f"/api/v1/projects/{proj.id}/guide-assets/sign-url"
    kwargs = {"params": {"key": key}, "headers": {"Authorization": f"Bearer {token}"}}

    allowed = await httpx_client.get(endpoint, **kwargs)
    assert allowed.status_code == 200, allowed.text
    await db_session.delete(member)
    await db_session.commit()
    denied = await httpx_client.get(endpoint, **kwargs)
    assert denied.status_code == 404, denied.text


@pytest.mark.parametrize("as_project_admin", [False, True])
async def test_non_member_cannot_read_guide_images(
    httpx_client,
    super_admin,
    project_admin,
    annotator,
    db_session,
    monkeypatch,
    as_project_admin,
):
    _patch_storage(monkeypatch)
    owner, _ = super_admin
    _, token = project_admin if as_project_admin else annotator
    proj = await _seed_project(db_session, owner.id)
    key = _add_guide_asset(proj)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/guide-assets/sign-url",
        params={"key": key},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("foreign_project", [False, True])
async def test_member_cannot_sign_unregistered_or_foreign_guide_keys(
    httpx_client,
    project_admin,
    annotator,
    db_session,
    monkeypatch,
    foreign_project,
):
    _patch_storage(monkeypatch)
    owner, _ = project_admin
    user, token = annotator
    proj = await _seed_project(db_session, owner.id)
    _add_guide_asset(proj)
    await _add_member(db_session, proj, user.id, "annotator")
    key_project_id = uuid.uuid4() if foreign_project else proj.id
    key = f"projects/{key_project_id}/guide/{uuid.uuid4()}-unknown.png"
    if foreign_project:
        proj.guide_assets = [{**proj.guide_assets[0], "key": key}]
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/guide-assets/sign-url",
        params={"key": key},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("operation", ["upload-init", "upload-complete", "delete"])
async def test_project_member_cannot_modify_guide_assets(
    httpx_client, project_admin, annotator, db_session, monkeypatch, operation
):
    deleted = _patch_storage(monkeypatch)
    owner, _ = project_admin
    user, token = annotator
    proj = await _seed_project(db_session, owner.id)
    key = _add_guide_asset(proj)
    await _add_member(db_session, proj, user.id, "annotator")
    await db_session.commit()
    await db_session.refresh(proj)
    endpoint = f"/api/v1/projects/{proj.id}/guide-assets"
    headers = {"Authorization": f"Bearer {token}"}
    if operation == "delete":
        resp = await httpx_client.delete(endpoint, params={"key": key}, headers=headers)
    else:
        payload = (
            {"filename": "new.png", "content_type": "image/png", "size": 100}
            if operation == "upload-init"
            else {"key": key, "original_name": "show.png", "content_type": "image/png"}
        )
        resp = await httpx_client.post(
            f"{endpoint}/{operation}", json=payload, headers=headers
        )
    assert resp.status_code == 403, resp.text
    assert deleted == []
    detail = await httpx_client.get(f"/api/v1/projects/{proj.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert [asset["key"] for asset in detail.json()["guide_assets"]] == [key]


async def test_patch_project_annotation_guide(httpx_client, super_admin, db_session):
    """v0.10.13 · PATCH /projects/{id} 写入 annotation_guide markdown."""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {"annotation_guide": "# 项目标注指引\n\n请标注**所有**车辆."}
    resp = await httpx_client.patch(
        f"/api/v1/projects/{proj.id}", json=body, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["annotation_guide"].startswith("# 项目标注指引")
