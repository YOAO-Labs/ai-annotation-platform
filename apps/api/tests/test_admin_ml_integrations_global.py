"""v0.9.7 · /admin/ml-integrations/all + project create with ml_backend_source_id."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService


def _patch_gpu_authority(monkeypatch) -> None:
    class Authority:
        async def get_diagnostics(self):
            return SimpleNamespace(result={"backends": []})

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.gpu_authority_client",
        lambda: Authority(),
    )


async def _make_backend(db, *, name="b", url="http://h:8000"):
    """v0.19.0 ADR-0044 · backend 已上提为全局注册项 (无 project_id)。
    v0.23.3 ADR-0050 · 同时建 singleton pool (项目创建经 ml_backend_source_id 复用需 pool)。"""
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name=name,
        url=url,
        state="connected",
        is_interactive=False,
        auth_method="none",
        auth_token=None,
        extra_params={},
    )
    db.add(b)
    await db.flush()
    await MLBackendService(db)._create_singleton_pool(b)
    return b


@pytest.mark.asyncio
async def test_list_all_requires_admin(httpx_client, annotator):
    _, token = annotator
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_list_all_lists_registry_rows(
    httpx_client, db_session, super_admin, monkeypatch
):
    """v0.19.0 ADR-0044 · url 全局唯一, /all 直接列全局注册表所有行 (不再按 source project 去重)。"""
    _, token = super_admin
    await _make_backend(db_session, url="http://shared:8000", name="A")
    await _make_backend(db_session, url="http://other:8000", name="C")
    await db_session.commit()
    _patch_gpu_authority(monkeypatch)

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    items = res.json()["items"]
    urls = sorted(it["url"] for it in items)
    assert urls == ["http://other:8000", "http://shared:8000"]
    for it in items:
        # source_project_name 现承载来源标签 (manual/env), source_project_id 置空
        assert it["source_project_name"] == "manual"
        assert it["source_project_id"] == ""


@pytest.mark.asyncio
async def test_create_project_with_backend_source_reuses_registry(
    httpx_client, db_session, super_admin
):
    """v0.19.0 ADR-0044 · 复用已注册 backend 不复制 row, 而是为新项目建启用关联, 共享同一全局 id。"""
    _, token = super_admin
    src = await _make_backend(
        db_session,
        url="http://source:8001",
        name="src-backend",
    )
    await db_session.commit()

    payload = {
        "name": "New Project",
        "type_label": "图像目标检测",
        "type_key": "image-det",
        "ai_enabled": True,
        "ml_backend_source_id": str(src.id),
    }
    res = await httpx_client.post(
        "/api/v1/projects",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # 新项目绑定同一全局 registry id (不再是新 backend id)
    assert body["ml_backend_id"] == str(src.id)
    # 新项目获得一条启用关联
    assert await MLBackendService(db_session).is_enabled(uuid.UUID(body["id"]), src.id)


@pytest.mark.asyncio
async def test_create_project_invalid_backend_source_400(httpx_client, super_admin):
    _, token = super_admin
    payload = {
        "name": "X",
        "type_label": "图像目标检测",
        "type_key": "image-det",
        "ml_backend_source_id": str(uuid.uuid4()),
    }
    res = await httpx_client.post(
        "/api/v1/projects",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
