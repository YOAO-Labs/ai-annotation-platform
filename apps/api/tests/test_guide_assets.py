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
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.guide_object_contract import GuideObjectFailure
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from aap_workspace.guide_asset_policy import MAX_GUIDE_ASSET_SIZE_BYTES
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
    """Mock the narrow guide-object capability instead of API-wide storage."""

    class GuideObjectPort:
        def __init__(self):
            self.deleted: list[str] = []

        async def generate_upload_url(self, project_id, key, *, content_type):
            return f"http://storage.local/put/{key}"

        async def verify_upload(self, project_id, key):
            return content_length if upload_present else None

        async def delete_object(self, project_id, key):
            self.deleted.append(key)

        async def generate_download_url(self, project_id, key):
            return f"http://storage.local/get/{key}"

    port = GuideObjectPort()
    monkeypatch.setattr(
        "app.services.workspace_configuration._guide_objects", lambda: port
    )
    return port


async def test_upload_init_returns_project_scoped_key(
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

    # Read through the public endpoint rather than expiring the shared SAVEPOINT session.
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


async def test_upload_complete_rejects_observed_oversize_object(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch, content_length=MAX_GUIDE_ASSET_SIZE_BYTES + 1)
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    key = f"projects/{proj.id}/guide/{uuid.uuid4()}-large.png"
    response = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/guide-assets/upload-complete",
        json={"key": key, "original_name": "large.png", "content_type": "image/png"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    detail = await httpx_client.get(
        f"/api/v1/projects/{proj.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    assert detail.json()["guide_assets"] == []


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


async def test_delete_asset_removes_registered_metadata(
    httpx_client, super_admin, db_session, monkeypatch
):
    _patch_storage(monkeypatch)
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

    detail = await httpx_client.get(f"/api/v1/projects/{proj.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["guide_assets"] == []


async def test_delete_asset_removes_entry_when_storage_delete_fails(
    httpx_client, super_admin, db_session, monkeypatch
):
    storage = _patch_storage(monkeypatch)
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

    async def fail_delete(project_id: uuid.UUID, key: str):
        raise GuideObjectFailure("storage_error")

    monkeypatch.setattr(storage, "delete_object", fail_delete)
    response = await httpx_client.request(
        "DELETE",
        f"/api/v1/projects/{proj.id}/guide-assets",
        params={"key": existing_key},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    detail = await httpx_client.get(
        f"/api/v1/projects/{proj.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    assert detail.json()["guide_assets"] == []


@pytest.mark.parametrize("as_super_admin", [False, True])
async def test_managers_can_read_registered_guide_images(
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
    storage = _patch_storage(monkeypatch)
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
    assert storage.deleted == []
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


def _inject_change_after_head(monkeypatch, test_engine, mutation, storage):
    boundary = {"verified": False, "changed": False}

    async def verify_upload(project_id: uuid.UUID, key: str):
        boundary["verified"] = True
        return 1024

    def change_before_next_query(
        connection, cursor, statement, parameters, context, executemany
    ):
        if boundary["verified"] and not boundary["changed"]:
            boundary["changed"] = True
            connection.execute(mutation)

    monkeypatch.setattr(storage, "verify_upload", verify_upload)
    event.listen(
        test_engine.sync_engine, "before_cursor_execute", change_before_next_query
    )
    return change_before_next_query


@pytest.mark.parametrize(
    ("change", "expected_status"), [("owner_transferred", 404), ("actor_disabled", 401)]
)
async def test_guide_completion_rechecks_authority_after_storage_head(
    httpx_client,
    db_session,
    test_engine,
    project_admin,
    super_admin,
    monkeypatch,
    change,
    expected_status,
):
    storage = _patch_storage(monkeypatch)
    owner, token = project_admin
    successor, _ = super_admin
    project = await _seed_project(db_session, owner.id)
    await db_session.commit()
    key = f"projects/{project.id}/guide/{uuid.uuid4()}-new.png"
    mutation = (
        update(Project).where(Project.id == project.id).values(owner_id=successor.id)
        if change == "owner_transferred"
        else update(User).where(User.id == owner.id).values(is_active=False)
    )
    listener = _inject_change_after_head(monkeypatch, test_engine, mutation, storage)
    try:
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/guide-assets/upload-complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"key": key, "original_name": "new.png", "content_type": "image/png"},
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)
    assert response.status_code == expected_status
    assets = await db_session.scalar(
        select(Project.guide_assets).where(Project.id == project.id)
    )
    assert assets == []


async def test_guide_completion_preserves_asset_added_during_storage_head(
    httpx_client, db_session, test_engine, super_admin, monkeypatch
):
    storage = _patch_storage(monkeypatch)
    owner, token = super_admin
    project = await _seed_project(db_session, owner.id)
    await db_session.commit()
    concurrent_key = f"projects/{project.id}/guide/{uuid.uuid4()}-concurrent.png"
    entry = {
        "key": concurrent_key,
        "original_name": "concurrent.png",
        "content_type": "image/png",
        "size": 1,
        "uploaded_at": "2026-10-18T00:00:00+00:00",
    }
    key = f"projects/{project.id}/guide/{uuid.uuid4()}-new.png"
    listener = _inject_change_after_head(
        monkeypatch,
        test_engine,
        update(Project).where(Project.id == project.id).values(guide_assets=[entry]),
        storage,
    )
    try:
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/guide-assets/upload-complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"key": key, "original_name": "new.png", "content_type": "image/png"},
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)
    assert response.status_code == 200
    detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    assert {asset["key"] for asset in detail.json()["guide_assets"]} == {
        concurrent_key,
        key,
    }
