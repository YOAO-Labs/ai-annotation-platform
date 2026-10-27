"""v0.9.3 · /admin/ml-integrations/overview 端到端测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from tests.factory import create_project


@pytest.mark.asyncio
async def test_overview_super_admin_only(httpx_client, super_admin, annotator):
    _, admin_token = super_admin
    _, anno_token = annotator

    # super_admin 200
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/overview",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "storage" in body
    assert "projects" in body
    assert body["total_backends"] == 0

    # annotator 403
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/overview",
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_overview_groups_backends_by_project(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="P1")

    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from tests.conftest import create_registry_with_pool

    b1, b1_pool = await create_registry_with_pool(
        db_session,
        name="b1",
        url="http://x:9000",
        state="connected",
        health_meta={
            "compute": {
                "configured_device": "cuda",
                "effective_provider": "CPUExecutionProvider",
                "cpu_fallback_supported": True,
            }
        },
    )
    b2, b2_pool = await create_registry_with_pool(
        db_session, name="b2", url="http://y:9000", state="disconnected"
    )
    # v0.19.0 ADR-0044 · overview 按项目「已启用」全局 backend 分组。
    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=b1_pool.id, enabled=True)
    )
    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=b2_pool.id, enabled=True)
    )
    await db_session.flush()

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_backends"] == 2
    assert body["connected_backends"] == 1
    assert len(body["projects"]) == 1
    assert body["projects"][0]["project_name"] == "P1"
    assert len(body["projects"][0]["backends"]) == 2
    b1_out = next(
        item for item in body["projects"][0]["backends"] if item["name"] == "b1"
    )
    assert (
        b1_out["health_meta"]["compute"]["effective_provider"] == "CPUExecutionProvider"
    )


@pytest.mark.asyncio
async def test_overview_includes_ai_enabled_project_without_backend(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id, name="AI Empty")
    proj.ai_enabled = True
    await db_session.commit()

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    group = next(p for p in body["projects"] if p["project_id"] == str(proj.id))
    assert group["project_name"] == "AI Empty"
    assert group["backends"] == []
    assert body["total_backends"] == 0


# ── v0.9.6 · /probe + /runtime-hints ──────────────────────────────────


@pytest.mark.asyncio
async def test_probe_returns_ok_for_healthy_backend(httpx_client, super_admin):
    _, token = super_admin

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "gpu_info": {"device_name": "RTX 4060", "memory_used_mb": 1234},
                "cache": {"hit_rate": 0.42},
                "model_version": "grounded-sam2-tiny-large",
            }

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResp()

    with patch("app.api.v1.admin_ml_integrations.httpx.AsyncClient", FakeClient):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/probe",
            json={"url": "http://172.17.0.1:8001"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["model_version"] == "grounded-sam2-tiny-large"
        assert body["gpu_info"]["device_name"] == "RTX 4060"
        assert body["cache"]["hit_rate"] == 0.42


@pytest.mark.asyncio
async def test_probe_returns_error_on_timeout(httpx_client, super_admin):
    _, token = super_admin
    import httpx as _httpx

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            raise _httpx.TimeoutException("timed out")

    with patch("app.api.v1.admin_ml_integrations.httpx.AsyncClient", FakeClient):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/probe",
            json={"url": "http://nope:9999"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert "timed out" in (body["error"] or "")


@pytest.mark.asyncio
async def test_probe_requires_admin(httpx_client, annotator):
    _, anno_token = annotator
    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/probe",
        json={"url": "http://x:8001"},
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_runtime_hints_returns_default_url(
    httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    from app.config import settings

    monkeypatch.setattr(settings, "ml_backend_default_url", "http://172.17.0.1:8001")
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/runtime-hints",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["ml_backend_default_url"] == "http://172.17.0.1:8001"


@pytest.mark.asyncio
async def test_runtime_hints_null_when_not_set(httpx_client, super_admin, monkeypatch):
    _, token = super_admin
    from app.config import settings

    monkeypatch.setattr(settings, "ml_backend_default_url", "")
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/runtime-hints",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["ml_backend_default_url"] is None


# ── v0.10.26 · 容器直连观测 /observe + /observe/smoke-test ──────────────


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx as _h

            raise _h.HTTPStatusError("err", request=None, response=None)


def _fake_client(routes):
    """routes: dict[("get"|"post", path_suffix)] -> _FakeResp。"""

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, **_kw):
            for (m, suffix), resp in routes.items():
                if m == "get" and url.endswith(suffix):
                    return resp
            return _FakeResp(404, {})

        async def post(self, url, **_kw):
            for (m, suffix), resp in routes.items():
                if m == "post" and url.endswith(suffix):
                    return resp
            return _FakeResp(404, {})

    return FakeClient


@pytest.mark.asyncio
async def test_observe_returns_variant_catalog_and_registered_flag(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    from app.config import settings

    monkeypatch.setattr(settings, "ml_backend_observe_urls", ["http://obs1:8001"])

    # 全局注册表里有同 URL 的 backend → observe 应标 registered (来源标签 source)。
    from app.db.models.ml_backend_registry import MLBackendRegistry

    db_session.add(
        MLBackendRegistry(
            name="b", url="http://obs1:8001", state="connected", source="manual"
        )
    )
    await db_session.flush()

    routes = {
        ("get", "/health"): _FakeResp(
            200,
            {
                "ok": True,
                "loaded": False,
                "model_version": "mv",
                "pool": {"cap": 1, "loaded_variants": []},
                "gpu_info": {"memory_used_mb": 1},
                "compute": {
                    "configured_device": "cuda",
                    "effective_device": "cpu",
                    "cpu_fallback_supported": True,
                },
            },
        ),
        ("get", "/setup"): _FakeResp(
            200,
            {
                "supported_variants": [
                    {
                        "key": "series",
                        "title": "Series",
                        "variants": [{"value": "yolo11", "label": "YOLO 11"}],
                    }
                ],
                "params": {
                    "properties": {
                        "sam_variant": {"enum": ["tiny", "large"]},
                        "dino_variant": {"enum": ["T", "B"]},
                    }
                },
            },
        ),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.get(
            "/api/v1/admin/ml-integrations/observe",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["configured_count"] == 1
    t = body["targets"][0]
    assert t["ok"] is True
    assert t["supports_variants"] is True
    assert t["variant_catalog"]["sam_variant"] == ["tiny", "large"]
    assert t["supported_variants"][0]["key"] == "series"
    assert t["compute"] == {
        "configured_device": "cuda",
        "effective_device": "cpu",
        "effective_provider": None,
        "cpu_fallback_supported": True,
    }
    assert t["registered"] is True
    assert "manual" in (t["registered_label"] or "")


@pytest.mark.asyncio
async def test_observe_collects_per_model_variants(
    httpx_client, super_admin, monkeypatch
):
    """v2 backend (rapidocr 等) 把变体挂在 models[].supported_variants 而非顶层时,
    observe 仍应收集到并置 supports_variants=True (否则前端错显「该容器不暴露变体目录」)。"""
    _, token = super_admin
    from app.config import settings

    monkeypatch.setattr(settings, "ml_backend_observe_urls", ["http://ocr:8005"])

    routes = {
        ("get", "/health"): _FakeResp(200, {"ok": True, "loaded": False}),
        ("get", "/setup"): _FakeResp(
            200,
            {
                # 顶层无 supported_variants; 变体按 model 挂载, 跨 model 有重叠 (version/size)
                # 与差异 (lang), 合并后按 key 去重。
                "supported_variants": None,
                "models": [
                    {
                        "id": "ocr-det",
                        "supported_variants": [
                            {
                                "key": "version",
                                "title": "版本",
                                "variants": [{"value": "v5"}],
                            },
                            {
                                "key": "size",
                                "title": "尺寸",
                                "variants": [{"value": "mobile"}],
                            },
                        ],
                    },
                    {
                        "id": "ocr-rec",
                        "supported_variants": [
                            {
                                "key": "version",
                                "title": "版本",
                                "variants": [{"value": "v5"}],
                            },
                            {
                                "key": "size",
                                "title": "尺寸",
                                "variants": [{"value": "mobile"}],
                            },
                            {
                                "key": "lang",
                                "title": "语言",
                                "variants": [{"value": "en"}],
                            },
                        ],
                    },
                ],
            },
        ),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.get(
            "/api/v1/admin/ml-integrations/observe",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    t = res.json()["targets"][0]
    assert t["supports_variants"] is True
    keys = [g["key"] for g in t["supported_variants"]]
    assert keys == ["version", "size", "lang"]  # 去重 + 保序


@pytest.mark.asyncio
async def test_smoke_test_skips_when_pool_already_loaded(httpx_client, super_admin):
    """冲突守护: 池子已有变体常驻时不预热/不卸载, 只确认可加载性。"""
    _, token = super_admin
    routes = {
        ("get", "/health"): _FakeResp(
            200,
            {
                "ok": True,
                "loaded": True,
                "pool": {
                    "loaded_variants": [{"sam_variant": "tiny", "dino_variant": "T"}]
                },
            },
        ),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["skipped"] is True
    assert body["auto_unloaded"] is False


@pytest.mark.asyncio
async def test_smoke_test_skips_via_new_pool_status_loaded_keys(
    httpx_client, super_admin
):
    """v0.14.14 · PoolStatus.loaded_keys 单源时也能识别"已加载", 并解析 key 还原 sam/dino."""
    _, token = super_admin
    routes = {
        ("get", "/health"): _FakeResp(
            200,
            {
                "ok": True,
                "loaded": False,  # 老 loaded 字段为 False, 验证 current_size 触发
                "pool": {
                    "cap": 1,
                    "current_size": 1,
                    "loaded_keys": [
                        {
                            "key": "sam=small/dino=B",
                            "loaded_at": "2026-06-08T00:00:00Z",
                            "last_used_at": "2026-06-08T00:00:01Z",
                            "hit_count": 1,
                        }
                    ],
                    "last_evict": None,
                },
            },
        ),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["skipped"] is True
    # parse 还原后 loaded_variant 仍是 {sam_variant, dino_variant} 形态
    assert body["loaded_variant"] == {"sam_variant": "small", "dino_variant": "B"}


@pytest.mark.asyncio
async def test_smoke_test_generic_variant_is_skipped_until_backend_warm_exists(
    httpx_client, super_admin
):
    _, token = super_admin
    with patch("app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client({})):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "variant": {"series": "yolo11", "size": "n"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["skipped"] is True
    assert body["message"] == "该容器未声明通用 warm 接口"
    assert body["loaded_variant"] == {"series": "yolo11", "size": "n"}


@pytest.mark.asyncio
async def test_smoke_test_warms_and_unloads_empty_pool(httpx_client, super_admin):
    _, token = super_admin
    routes = {
        ("get", "/health"): _FakeResp(
            200, {"ok": True, "loaded": False, "pool": {"loaded_variants": []}}
        ),
        ("post", "/reload"): _FakeResp(
            200,
            {
                "ok": True,
                "loaded": True,
                "reloaded": True,
                "sam_variant": "large",
                "dino_variant": "B",
            },
        ),
        ("post", "/unload"): _FakeResp(200, {"ok": True, "unloaded": True}),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["skipped"] is False
    assert body["reloaded"] is True
    assert body["auto_unloaded"] is True
    assert body["loaded_variant"]["sam_variant"] == "large"


@pytest.mark.asyncio
async def test_unregistered_smoke_test_blocks_raw_load_in_effective_enforce(
    httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.unregistered_gpu_loading_blocked",
        lambda: True,
    )
    routes = {
        ("get", "/health"): _FakeResp(
            200, {"ok": True, "loaded": False, "pool": {"loaded_variants": []}}
        ),
    }

    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://unregistered:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 503
    assert res.json()["detail"] == {
        "error_code": "gpu_config_invalid",
        "message": (
            "effective enforce 下未注册 URL 只能执行只读 health/setup；"
            "请先注册 backend 并完成受管身份绑定"
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health",
    [
        {
            "ok": True,
            "loaded": False,
            "pool": {"loaded_variants": []},
            "residency": {
                "state": "resident",
                "gpu_loaded": True,
                "builders": 0,
                "borrowers": 0,
                "pools": {"video": {"resident": True}},
            },
        },
        {
            "ok": True,
            "loaded": False,
            "pool": {"loaded_variants": []},
            "residency": {
                "state": "unloaded",
                "gpu_loaded": False,
                "builders": 1,
                "borrowers": 0,
                "pools": {"image": {"resident": False}},
            },
        },
        {
            "ok": True,
            "loaded": False,
            "pool": {"loaded_variants": []},
            "video_pool": {
                "current_size": 1,
                "loaded_keys": [{"key": "sam2"}],
                "active_sessions": 1,
            },
        },
        {
            "ok": True,
            "loaded": False,
            "pool": {"loaded_variants": []},
            "video_pool": {"current_size": "unknown", "active_sessions": {}},
        },
    ],
)
async def test_smoke_test_never_warms_when_full_residency_is_not_proven_empty(
    httpx_client, super_admin, health
):
    _, token = super_admin
    routes = {("get", "/health"): _FakeResp(200, health)}
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["skipped"] is True
    assert body["auto_unloaded"] is False
    assert "未执行试启动" in body["message"]


@pytest.mark.asyncio
async def test_registered_smoke_test_uses_unified_ml_client(
    httpx_client, db_session, super_admin, monkeypatch
):
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin
    backend = MLBackendRegistry(
        name="registered-smoke",
        url="http://obs1:8001",
        state="connected",
        auth_method="none",
        extra_params={},
    )
    db_session.add(backend)
    await db_session.commit()
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.unregistered_gpu_loading_blocked",
        lambda: True,
    )
    calls: list[tuple[str, dict]] = []

    async def fake_health_meta(self):
        calls.append(("health", {}))
        return True, {"loaded": False, "pool": {"loaded_variants": []}}

    async def fake_reload(self, **kwargs):
        calls.append(("reload", kwargs))
        return {
            "ok": True,
            "reloaded": True,
            "sam_variant": kwargs["sam_variant"],
            "dino_variant": kwargs["dino_variant"],
        }

    async def fake_unload(self):
        calls.append(("unload", {}))
        return {"ok": True}

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.health_meta",
        fake_health_meta,
    )
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.reload", fake_reload
    )
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.unload", fake_unload
    )
    routes = {
        ("get", "/health"): _FakeResp(
            200, {"ok": True, "loaded": False, "pool": {"loaded_variants": []}}
        ),
    }
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient", _fake_client(routes)
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs1:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200, res.text
    assert calls == [
        ("health", {}),
        ("reload", {"sam_variant": "large", "dino_variant": "B"}),
        ("unload", {}),
    ]
    assert "residency" in res.json()["message"]


@pytest.mark.asyncio
async def test_registered_smoke_test_preserves_gpu_arbiter_error_contract(
    httpx_client, db_session, super_admin, monkeypatch
):
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin
    backend = MLBackendRegistry(
        name="registered-smoke-rejected",
        url="http://obs-rejected:8001",
        state="connected",
        auth_method="none",
        extra_params={},
    )
    db_session.add(backend)
    await db_session.commit()
    unload_calls = 0

    async def fake_health_meta(self):
        return True, {"loaded": False, "pool": {"loaded_variants": []}}

    async def fake_reload(self, **_kwargs):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            message="lease full",
            retry_after_s=7,
        )

    async def fake_unload(self):
        nonlocal unload_calls
        unload_calls += 1
        return {"ok": True}

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.health_meta",
        fake_health_meta,
    )
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.reload", fake_reload
    )
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.MLBackendClient.unload", fake_unload
    )
    with patch(
        "app.api.v1.admin_ml_integrations.httpx.AsyncClient",
        _fake_client({}),
    ):
        response = await httpx_client.post(
            "/api/v1/admin/ml-integrations/observe/smoke-test",
            json={
                "url": "http://obs-rejected:8001",
                "sam_variant": "large",
                "dino_variant": "B",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": {
            "error_code": "gpu_backend_concurrency_saturated",
            "message": "lease full",
        }
    }
    assert response.headers["Retry-After"] == "7"
    assert unload_calls == 0


@pytest.mark.asyncio
async def test_project_admin_all_response_masks_physical_gpu_topology(
    httpx_client, db_session, project_admin
):
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = project_admin
    backend = MLBackendRegistry(
        name="masked-gpu",
        url="http://masked-gpu:9000",
        state="connected",
        gpu_resource_id="node-secret/GPU-secret",
        vram_budget_mb=12_000,
        eviction_priority=7,
        health_meta={
            "capabilities": {"modalities": ["image"]},
            "gpu_info": {"device_uuid": "GPU-secret"},
            "residency": {
                "state": "resident",
                "gpu_loaded": True,
                "identity": {"gpu_resource_id": "node-secret/GPU-secret"},
            },
        },
    )
    db_session.add(backend)
    await db_session.commit()

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    item = next(row for row in res.json()["items"] if row["id"] == str(backend.id))
    assert item["gpu_resource_id"] is None
    assert item["vram_budget_mb"] is None
    assert item["eviction_priority"] is None
    assert item["gpu_config"] is None
    assert item["health_meta"].get("gpu_info") is None
    assert item["health_meta"].get("residency") is None
    assert item["health_meta"]["capabilities"]["modalities"] == ["image"]


@pytest.mark.asyncio
async def test_registry_unload_injects_shadow_factory_and_preserves_numeric_result(
    httpx_client,
    app_module,
    db_session,
    super_admin,
    monkeypatch,
):
    from app.config import GPUArbiterMode, settings
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.deps import get_gpu_shadow_session_factory

    _, token = super_admin
    backend = MLBackendRegistry(
        name="injectable-shadow",
        url="http://injectable-shadow:9000",
        state="connected",
        auth_method="none",
        extra_params={},
        gpu_resource_id="node-a/index:0",
        vram_budget_mb=8_000,
    )
    db_session.add(backend)
    await db_session.commit()
    monkeypatch.setattr(settings, "gpu_arbiter_mode", GPUArbiterMode.OBSERVE)
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        '{"node-a/index:0":{"node_id":"node-a",'
        '"physical_device_token":"index:0","allocatable_mb":20000,'
        '"mode":"observe"}}',
    )

    marker = object()
    observed: list[tuple[str, str, object]] = []

    async def fake_record(backend_id, operation, session_factory):
        observed.append((backend_id, operation, session_factory))

    routes = {("post", "/unload"): _FakeResp(200, {"ok": True, "unloaded": 2})}
    monkeypatch.setattr(
        "app.services.ml_client.record_gpu_shadow_dispatch", fake_record
    )
    app_module.dependency_overrides[get_gpu_shadow_session_factory] = lambda: marker
    try:
        with patch("app.services.ml_client.httpx.AsyncClient", _fake_client(routes)):
            res = await httpx_client.post(
                f"/api/v1/admin/ml-integrations/registry/{backend.id}/unload",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app_module.dependency_overrides.pop(get_gpu_shadow_session_factory, None)

    assert res.status_code == 200, res.text
    assert res.json()["unloaded"] == 2
    assert type(res.json()["unloaded"]) is int
    assert observed == [(str(backend.id), "unload", marker)]


@pytest.mark.asyncio
async def test_smoke_test_requires_super_admin(httpx_client, annotator):
    _, anno_token = annotator
    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/observe/smoke-test",
        json={"url": "http://x:8001"},
        headers={"Authorization": f"Bearer {anno_token}"},
    )
    assert res.status_code == 403
