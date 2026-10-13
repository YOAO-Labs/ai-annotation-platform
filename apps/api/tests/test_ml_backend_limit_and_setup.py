"""v0.10.1 · M1 Capability 协商基础设施 (v0.19.0 ADR-0044 数据模型迁移).

覆盖:
- POST /projects/{id}/ml-backends: 全局注册项按 url 复用/新建 + 为项目启用。
  · 不再有 max_ml_backends_per_project 上限, 同项目可添加多个 backend 都成功。
  · 同一 url 第二次添加复用同一 registry id。
- GET /projects/{id}/ml-backends/{bid}/setup 代理 backend /setup; 30s 进程内 TTL 缓存命中
  第二次不再调下游.
- 404: backend 未在本项目启用 (跨项目) 时不串台.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.api.v1 import ml_backends as ml_backends_route
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from tests.conftest import create_registry_with_pool


async def _seed_project(db, owner_id) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-LIM-{suffix}",
        name=f"lim-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _seed_backend(db, project_id, name="grounded-sam2") -> MLBackendRegistry:
    """建全局注册项 + 为项目启用 (ADR-0044)。url 全局唯一, 每次生成不同 url。"""
    b, pool = await create_registry_with_pool(
        db,
        name=name,
        url=f"http://example-{uuid.uuid4().hex[:8]}/",
        is_interactive=True,
        state="connected",
    )
    db.add(ProjectMLBackendPool(project_id=project_id, pool_id=pool.id, enabled=True))
    await db.flush()
    return b


@pytest.fixture(autouse=True)
def _clear_setup_cache():
    ml_backends_route._setup_cache.clear()
    yield
    ml_backends_route._setup_cache.clear()


async def test_create_multiple_ml_backends_no_limit(
    httpx_client, super_admin, db_session
):
    """ADR-0044 · 不再有单项目 backend 上限: 同项目连加多个 backend 都成功。"""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await _seed_backend(db_session, proj.id, name="grounded-sam2")
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    for i in range(3):
        resp = await httpx_client.post(
            f"/api/v1/projects/{proj.id}/ml-backends",
            json={
                "name": f"sam3-{i}",
                "url": f"http://sam3-{i}/",
                "is_interactive": True,
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text


async def test_create_ml_backend_reuses_registry_by_url(
    httpx_client, super_admin, db_session
):
    """ADR-0044 · 同一 url 第二次添加复用同一全局 registry id (不新建)。"""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    body = {"name": "sam3", "url": "http://sam3-reuse/", "is_interactive": True}
    r1 = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends", json=body, headers=headers
    )
    r2 = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends", json=body, headers=headers
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]


async def test_create_ml_backend_activates_singleton_pool(
    httpx_client, super_admin, db_session
):
    """registry 兼容入口首次启用时同步激活 singleton pool，真实路由才可达。"""
    from sqlalchemy import select

    from app.db.models.ml_backend_pool import (
        MLBackendPoolMember,
        MLBackendServicePool,
    )

    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends",
        json={
            "name": "sam3-routable",
            "url": "http://sam3-routable/",
            "is_interactive": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text

    pool = await db_session.scalar(
        select(MLBackendServicePool)
        .join(
            MLBackendPoolMember,
            MLBackendPoolMember.pool_id == MLBackendServicePool.id,
        )
        .where(MLBackendPoolMember.registry_id == uuid.UUID(resp.json()["id"]))
    )
    assert pool is not None
    assert pool.enabled is True
    assert pool.routing_generation == 2


async def test_project_out_drops_ml_backend_limit(
    httpx_client, super_admin, db_session
):
    """ADR-0044 · 上限彻底退役, ProjectOut 不再透出 ml_backend_limit 字段。"""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "ml_backend_limit" not in resp.json()


async def test_setup_proxy_returns_capability_and_caches(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    capability = {
        "name": "grounded-sam2",
        "version": "0.10.1",
        "model_version": "grounded-sam2-dinoT-sam2.1tiny",
        "supported_prompts": ["point", "bbox", "text"],
        "supported_text_outputs": ["box", "mask", "both"],
        "params": {"type": "object", "properties": {}},
    }
    call_count = {"n": 0}

    async def fake_setup(self):
        call_count["n"] += 1
        return capability

    with patch("app.services.ml_client.MLBackendClient.setup", new=fake_setup):
        url = f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/setup"
        headers = {"Authorization": f"Bearer {token}"}
        r1 = await httpx_client.get(url, headers=headers)
        r2 = await httpx_client.get(url, headers=headers)

    assert r1.status_code == 200, r1.text
    assert r1.json() == capability
    assert r2.json() == capability
    # 30s TTL → 第二次走缓存
    assert call_count["n"] == 1


async def test_setup_proxy_404_on_cross_project_backend(
    httpx_client, super_admin, db_session
):
    """ADR-0044 · backend 只对 B 项目启用, 用 A 项目路径访问 → 404 (未启用)。"""
    user, token = super_admin
    proj_a = await _seed_project(db_session, user.id)
    proj_b = await _seed_project(db_session, user.id)
    backend_b = await _seed_backend(db_session, proj_b.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj_a.id}/ml-backends/{backend_b.id}/setup",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_setup_proxy_502_when_backend_unreachable(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(self):
        raise RuntimeError("connection refused")

    with patch("app.services.ml_client.MLBackendClient.setup", new=fake_setup):
        resp = await httpx_client.get(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/setup",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 502


# ─── v0.14.9 · 能力声明协议 v2 — /capabilities 端点 ──────────────────────────

# 含 models[] 的 /setup 回放样本: OCR + 检测双模型目录。
_SETUP_WITH_MODELS = {
    "name": "multi-model-backend",
    "infra": "paddle",
    "models": [
        {
            "id": "pp-ocrv4",
            "display_name": "PP-OCRv4",
            "task": "ocr",
            "supported_text_outputs": ["text"],
            "output_attribute_types": ["text", "language"],
        },
        {
            "id": "yolo-det",
            "display_name": "YOLO Detector",
            "task": "detection",
            "infra": "onnx",
            "supported_geometric_outputs": ["bbox"],
        },
    ],
}


async def test_capabilities_returns_derived_models_snapshot(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def fake_setup(self):
        return _SETUP_WITH_MODELS

    with patch("app.services.ml_client.MLBackendClient.setup", new=fake_setup):
        resp = await httpx_client.get(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # backend 默认 infra
    assert body["infra"] == "paddle"
    # models[] 派生出两条, id / task / infra 各就位
    models = {m["id"]: m for m in body["models"]}
    assert set(models) == {"pp-ocrv4", "yolo-det"}
    assert models["pp-ocrv4"]["task"] == "ocr"
    assert models["pp-ocrv4"]["output_attribute_types"] == ["text", "language"]
    # detection 条目显式覆盖 infra
    assert models["yolo-det"]["infra"] == "onnx"
    # 模态派生 (纯 image 双模型)
    assert body["modalities"] == ["image"]


async def test_capabilities_refresh_invalidates_cache(
    httpx_client, super_admin, db_session
):
    """refresh 先 pop setup 缓存再重探: 两次结果不同时 refresh 拿到新值。"""
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    state = {"infra": "pytorch"}

    async def fake_setup(self):
        return {"infra": state["infra"], "models": []}

    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.services.ml_client.MLBackendClient.setup", new=fake_setup):
        # 先 GET 一次填充缓存
        r1 = await httpx_client.get(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/capabilities",
            headers=headers,
        )
        assert r1.json()["infra"] == "pytorch"
        # backend 升级换 infra
        state["infra"] = "tensorrt"
        # 不刷新时 GET 仍走 30s 缓存 → 旧值
        r2 = await httpx_client.get(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/capabilities",
            headers=headers,
        )
        assert r2.json()["infra"] == "pytorch"
        # refresh 先 invalidate 缓存再重探 → 新值
        r3 = await httpx_client.post(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/capabilities/refresh",
            headers=headers,
        )
    assert r3.status_code == 200, r3.text
    assert r3.json()["infra"] == "tensorrt"


async def test_capabilities_404_on_cross_project_backend(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    proj_a = await _seed_project(db_session, user.id)
    proj_b = await _seed_project(db_session, user.id)
    backend_b = await _seed_backend(db_session, proj_b.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj_a.id}/ml-backends/{backend_b.id}/capabilities",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
