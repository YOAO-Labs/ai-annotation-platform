"""POST /api/v1/projects/{pid}/ml-backends/{bid}/warmup 代理端点测试.

只测路由层: 调度 MLBackendService.warmup() 时是否原样转发 body, 是否对 backend 上游
HTTP 错误做 4xx/5xx 透传 / 502 兜底.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


pytestmark = pytest.mark.asyncio


async def _make_backend(
    db_session, project_id: uuid.UUID, name: str = "yolo"
) -> MLBackendRegistry:
    """建全局注册项 + 为项目启用 (ADR-0044)。url 全局唯一。"""
    b, pool = await create_registry_with_pool(
        db_session,
        name=name,
        url=f"http://yolo-{uuid.uuid4().hex[:8]}:8003",
        state="connected",
        is_interactive=False,
        auth_method="none",
        extra_params={},
        health_meta={},
    )
    db_session.add(
        ProjectMLBackendPool(project_id=project_id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    return b


async def test_warmup_forwards_body_and_returns_backend_response(
    httpx_client, auth_headers, db_session, super_admin, monkeypatch
):
    """body 原样转发到 backend; 响应原样返回."""
    from types import SimpleNamespace

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="P-Warm")
    backend = await _make_backend(db_session, proj.id)

    mock_warmup = AsyncMock(
        return_value={
            "ok": True,
            "model_load_ms": 4500,
            "cache_hit": False,
            "evicted": None,
        }
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(warmup=mock_warmup),
    )

    body = {"task": "detection", "variants": {"series": "yolo11", "size": "s"}}
    r = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/warmup",
        json=body,
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cache_hit"] is False
    assert data["model_load_ms"] == 4500
    mock_warmup.assert_awaited_once_with(backend.id, body)


async def test_warmup_returns_404_when_backend_missing(
    httpx_client, auth_headers, db_session, super_admin, monkeypatch
):
    from types import SimpleNamespace

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="P-NoBackend")

    mock_warmup = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(warmup=mock_warmup),
    )
    fake_bid = uuid.uuid4()
    r = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{fake_bid}/warmup",
        json={},
        headers=auth_headers,
    )
    assert r.status_code == 404
    mock_warmup.assert_not_awaited()


async def test_warmup_propagates_upstream_4xx(
    httpx_client, auth_headers, db_session, super_admin, monkeypatch
):
    """backend 上游返回 400 INVALID_VARIANT 时, 路由透传 status_code=400."""
    from types import SimpleNamespace
    from yoao_inference_contract.backend_control import BackendControlFailure
    from app.services.inference_backend_control import BackendControlOperationError

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="P-BadVar")
    backend = await _make_backend(db_session, proj.id)

    err = BackendControlOperationError(
        BackendControlFailure(
            code="backend_http_error",
            status_code=400,
            detail='{"code": "INVALID_VARIANT", "message": "no weight"}',
        )
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(warmup=AsyncMock(side_effect=err)),
    )
    r = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/warmup",
        json={"task": "detection", "variants": {"series": "yolov99", "size": "z"}},
        headers=auth_headers,
    )
    assert r.status_code == 400


async def test_warmup_returns_502_on_connection_error(
    httpx_client, auth_headers, db_session, super_admin, monkeypatch
):
    """backend 不可达 (httpx.ConnectError) 时, 路由 502."""
    from types import SimpleNamespace
    from yoao_inference_contract.backend_control import BackendControlFailure
    from app.services.inference_backend_control import BackendControlOperationError

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="P-Unreach")
    backend = await _make_backend(db_session, proj.id)

    err = BackendControlOperationError(
        BackendControlFailure(
            code="backend_unavailable",
            status_code=502,
            detail="connection refused",
        )
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(warmup=AsyncMock(side_effect=err)),
    )
    r = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/warmup",
        json={},
        headers=auth_headers,
    )
    assert r.status_code == 502
