"""项目级 ML Backend 端点的「项目可见性 / 归属」鉴权收口回归。

背景: backend 全局化 (ADR-0044) 后, /projects/{id}/ml-backends/* 端点原先只用
require_roles 校验「全局角色」, 未校验 URL 里的 project_id 是否对调用者可见/归其所有,
形成跨项目越权面:
  · 读端点 (list/setup/capabilities/interactive): 全局 annotator/reviewer 可读任意项目;
  · 项目启用写端点: 任一 project_admin 可改他人名下项目。
  · 全局注册行写端点 (create/update/unload/...): project_admin 可改全局共享状态。

修复: 读端点叠加 require_project_visible，项目启用叠加
require_project_owner；会新建或修改全局注册行的端点只允许 super_admin。
本模块锁定该行为 (正/反各一)。
"""

from __future__ import annotations

import uuid

from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from tests.conftest import create_registry_with_pool
from tests.factory import create_membership


async def _seed_project(db, owner_id) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-SCOPE-{suffix}",
        name=f"scope-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _seed_backend(db, project_id) -> MLBackendRegistry:
    b, pool = await create_registry_with_pool(
        db,
        name="grounded-sam2",
        url=f"http://example-{uuid.uuid4().hex[:8]}/",
        is_interactive=True,
        state="connected",
    )
    db.add(ProjectMLBackendPool(project_id=project_id, pool_id=pool.id, enabled=True))
    await db.flush()
    return b


# ── 读端点: require_project_visible ──────────────────────────────────


async def test_list_backends_denied_for_non_member_annotator(
    httpx_client, super_admin, annotator, db_session
):
    """非项目成员的 annotator 读他人项目 backend 列表 → 404 (隐藏存在性)。"""
    owner, _ = super_admin
    _, anno_token = annotator
    proj = await _seed_project(db_session, owner.id)
    await _seed_backend(db_session, proj.id)
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/ml-backends",
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert resp.status_code == 404, resp.text


async def test_list_backends_ok_for_member_annotator(
    httpx_client, super_admin, annotator, db_session
):
    """项目成员的 annotator 可读本项目 backend 列表 → 200 (未被过度收紧)。"""
    owner, _ = super_admin
    anno, anno_token = annotator
    proj = await _seed_project(db_session, owner.id)
    await _seed_backend(db_session, proj.id)
    await create_membership(
        db_session,
        project_id=proj.id,
        user_id=anno.id,
        role="annotator",
        assigned_by=owner.id,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/ml-backends",
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_project_backend_list_keeps_malformed_health_meta_observable(
    httpx_client, super_admin, db_session
):
    """第三方历史坏 health JSON 不能让项目 backend 列表整体 500。"""
    owner, token = super_admin
    proj = await _seed_project(db_session, owner.id)
    backend = await _seed_backend(db_session, proj.id)
    backend.health_meta = {
        "compute": [],
        "gpu_info": "unreadable",
        "residency": {},
    }
    await db_session.commit()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/ml-backends",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["health_meta"]["residency"] == {}


# ── 写端点: require_project_owner ────────────────────────────────────


async def test_create_backend_denied_for_non_owner_project_admin(
    httpx_client, super_admin, project_admin, db_session
):
    """非 owner 的 project_admin 给他人项目加 backend → 403。"""
    owner, _ = super_admin
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, owner.id)
    # Visible member with a work role: a non-owner project administrator receives
    # that membership's capabilities, never management, so the denial is 403.
    await create_membership(
        db_session,
        project_id=proj.id,
        user_id=pm.id,
        role="annotator",
        assigned_by=owner.id,
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends",
        json={"name": "sam3", "url": "http://sam3-scope/", "is_interactive": True},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_create_backend_denied_for_owning_project_admin(
    httpx_client, project_admin, db_session
):
    """create 会写全局注册行，项目 owner 也不能执行。"""
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, pm.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends",
        json={"name": "sam3", "url": "http://sam3-own/", "is_interactive": True},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403, resp.text


# ── 共享全局态运维端点: unload/reload 收口到 super_admin ────────────────────
#
# 评审 #17 · unload/reload 操作的是「全局 backend 显存驻留」(一物理 backend 被多项目共用),
# 项目 owner 也能借此驱逐/换掉其他项目正在用的权重。故这类破坏性驻留操作从 project_owner
# 收口到 super_admin; 构造性的 warmup 刻意保留在 project_owner (项目预标/交互推理前置)。
# 正 (授权角色通过闸) / 反 (未授权角色被拒) 各一。授权方向 monkeypatch 掉 service 的
# backend HTTP 调用, 只锁鉴权闸, 不打真实网络。


async def test_unload_denied_for_owning_project_admin(
    httpx_client, project_admin, db_session
):
    """卸载作用于全局显存驻留 → 即便是项目 owner 的 project_admin 也 403 (仅 super_admin)。"""
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, pm.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/unload",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_unload_allowed_for_super_admin(
    httpx_client, super_admin, db_session, monkeypatch
):
    """super_admin 通过鉴权闸 (service 已 mock, backend HTTP 不打真网) → 200。"""
    owner, admin_token = super_admin
    proj = await _seed_project(db_session, owner.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def _fake_unload(self, registry_id):
        return {"ok": True, "unloaded": True, "loaded": False}

    async def _allow_quiescent(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.ml_routing.safety.require_registry_quiescent",
        _allow_quiescent,
    )
    monkeypatch.setattr("app.services.ml_backend.MLBackendService.unload", _fake_unload)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/unload",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text


async def test_reload_denied_for_owning_project_admin(
    httpx_client, project_admin, db_session
):
    """重载会改写全局常驻变体 → 项目 owner 的 project_admin 也 403 (仅 super_admin)。"""
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, pm.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/reload",
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_reload_allowed_for_super_admin(
    httpx_client, super_admin, db_session, monkeypatch
):
    """super_admin 通过鉴权闸 → 200。"""
    owner, admin_token = super_admin
    proj = await _seed_project(db_session, owner.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def _fake_reload(
        self, registry_id, sam_variant=None, dino_variant=None, task_type=None
    ):
        return {"ok": True, "reloaded": True}

    monkeypatch.setattr("app.services.ml_backend.MLBackendService.reload", _fake_reload)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/reload",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text


async def test_residency_routes_preserve_gpu_arbiter_error_contract(
    httpx_client, super_admin, db_session, monkeypatch
):
    owner, admin_token = super_admin
    proj = await _seed_project(db_session, owner.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def _reject(*_args, **_kwargs):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            message="lease full",
            retry_after_s=7,
        )

    async def _allow_quiescent(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.ml_routing.safety.require_registry_quiescent",
        _allow_quiescent,
    )
    for method in ("unload", "reload", "warmup"):
        monkeypatch.setattr(
            f"app.services.ml_backend.MLBackendService.{method}",
            _reject,
        )

    for operation in ("unload", "reload", "warmup"):
        response = await httpx_client.post(
            f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/{operation}",
            json={} if operation != "unload" else None,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 503, response.text
        assert response.json() == {
            "detail": {
                "error_code": "gpu_backend_concurrency_saturated",
                "message": "lease full",
            }
        }
        assert response.headers["Retry-After"] == "7"


# ── warmup: 刻意保留 project_owner (未随 unload/reload 收口) ─────────────────


async def test_warmup_denied_for_non_owner_project_admin(
    httpx_client, super_admin, project_admin, db_session
):
    """非 owner 的 project_admin 预热他人项目 backend → 403 (owner 闸仍生效)。"""
    owner, _ = super_admin
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, owner.id)
    await create_membership(
        db_session,
        project_id=proj.id,
        user_id=pm.id,
        role="annotator",
        assigned_by=owner.id,
    )
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/warmup",
        json={},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_warmup_allowed_for_owning_project_admin(
    httpx_client, project_admin, db_session, monkeypatch
):
    """owner project_admin 可预热自己项目的 backend → 200 (证明未被收口到 super_admin)。"""
    pm, pm_token = project_admin
    proj = await _seed_project(db_session, pm.id)
    backend = await _seed_backend(db_session, proj.id)
    await db_session.commit()

    async def _fake_warmup(self, registry_id, body):
        return {"ok": True, "cache_hit": False}

    monkeypatch.setattr("app.services.ml_backend.MLBackendService.warmup", _fake_warmup)
    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/warmup",
        json={},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 200, resp.text
