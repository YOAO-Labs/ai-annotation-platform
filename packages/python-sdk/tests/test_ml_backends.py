from uuid import UUID, uuid4

import httpx
import pytest

from ai_annotation.errors import ConflictError, NotFoundError, PermissionDeniedError
from ai_annotation.models import (
    MLBackend,
    MLBackendHealth,
    MLBackendUnloadResult,
    ProjectMLBackend,
    ProjectServicePool,
)

from .conftest import API

PID = str(uuid4())
BID = str(uuid4())


def _backend(state="connected", **extra) -> dict:
    return {
        "id": BID,
        "project_id": PID,
        "name": "sam2-backend",
        "url": "http://gpu-host:9000",
        "state": state,
        "health_meta": {
            "model_version": "v1.2",
            "gpu_info": {
                "gpu_utilization_percent": 73,
                "memory_used_mb": 8000,
                "memory_total_mb": 24000,
                "gpu_temperature_celsius": 61,
            },
            "cache": {"hit_rate": 0.9},
        },
        "error_message": None,
        "last_checked_at": "2026-06-11T00:00:00Z",
        "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        **extra,
    }


def test_list_ml_backends(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PID}/ml-backends").mock(
        return_value=httpx.Response(200, json=[_backend()])
    )
    backends = client.ml_backends.list(PID)
    assert route.called
    assert len(backends) == 1
    b = backends[0]
    assert isinstance(b, MLBackend)
    assert b.state == "connected"
    assert b.health_meta.model_version == "v1.2"
    assert b.health_meta.gpu_info.gpu_utilization_percent == 73
    assert b.health_meta.cache.hit_rate == 0.9


def test_get_ml_backend(client, respx_mock):
    respx_mock.get(f"{API}/projects/{PID}/ml-backends/{BID}").mock(
        return_value=httpx.Response(
            200, json=_backend(state="error", error_message="down")
        )
    )
    b = client.ml_backends.get(PID, BID)
    assert b.state == "error"
    assert b.error_message == "down"


def test_list_ml_backends_global_project_id_none(client, respx_mock):
    # 全局注册表: 全局/admin 场景 backend 无项目归属, project_id 可缺省/为 None;
    # MLBackend 必须容忍, 不因 project_id 缺失而 validation error。
    payload = _backend()
    payload.pop("project_id")
    respx_mock.get(f"{API}/projects/{PID}/ml-backends").mock(
        return_value=httpx.Response(200, json=[payload])
    )
    backends = client.ml_backends.list(PID)
    assert len(backends) == 1
    assert backends[0].project_id is None


def test_get_ml_backend_404(client, respx_mock):
    respx_mock.get(f"{API}/projects/{PID}/ml-backends/{BID}").mock(
        return_value=httpx.Response(404, json={"detail": "ML Backend not found"})
    )
    with pytest.raises(NotFoundError):
        client.ml_backends.get(PID, BID)


def test_project_backend_availability_enablement_and_health(client, respx_mock):
    backend = _backend()
    available = respx_mock.get(f"{API}/projects/{PID}/ml-backends/available").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"backend": backend, "enabled": True, "default_variants": None}
                ]
            },
        )
    )
    item = client.ml_backends.list_available(PID)[0]
    assert available.called
    assert isinstance(item, ProjectMLBackend)
    assert item.enabled is True

    enablement = respx_mock.put(
        f"{API}/projects/{PID}/ml-backends/{BID}/enablement"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"backend": backend, "enabled": False, "default_variants": None},
        )
    )
    assert client.ml_backends.set_enablement(PID, BID, False).enabled is False
    assert enablement.calls.last.request.content == b'{"enabled":false}'

    respx_mock.post(f"{API}/projects/{PID}/ml-backends/{BID}/health").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "backend_id": BID, "backend_name": "sam2"},
        )
    )
    health = client.ml_backends.check_health(PID, BID)
    assert isinstance(health, MLBackendHealth)
    assert health.status == "ok"


def test_project_pool_availability_and_enablement(client, respx_mock):
    pool_id = str(uuid4())
    pool = {
        "id": pool_id,
        "name": "sam-pool",
        "enabled": True,
        "legacy_instance_id": BID,
        "member_count": 1,
        "routing_generation": 2,
    }
    respx_mock.get(f"{API}/projects/{PID}/ml-backends/pools/available").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"pool": pool, "enabled": False}]},
        )
    )
    item = client.ml_backends.list_available_pools(PID)[0]
    assert isinstance(item, ProjectServicePool)
    assert item.pool.id == UUID(pool_id)

    route = respx_mock.put(
        f"{API}/projects/{PID}/ml-backends/pools/{pool_id}/enablement"
    ).mock(
        return_value=httpx.Response(
            200, json={"pool": pool, "enabled": True, "default_variants": None}
        )
    )
    assert client.ml_backends.set_pool_enablement(PID, pool_id, True).enabled is True
    assert route.calls.last.request.content == b'{"enabled":true}'


@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        (
            "get",
            f"/projects/{PID}/ml-backends/available",
            lambda c: c.ml_backends.list_available(PID),
        ),
        (
            "put",
            f"/projects/{PID}/ml-backends/{BID}/enablement",
            lambda c: c.ml_backends.set_enablement(PID, BID, True),
        ),
        (
            "post",
            f"/projects/{PID}/ml-backends/{BID}/health",
            lambda c: c.ml_backends.check_health(PID, BID),
        ),
        (
            "get",
            f"/projects/{PID}/ml-backends/pools/available",
            lambda c: c.ml_backends.list_available_pools(PID),
        ),
        (
            "put",
            f"/projects/{PID}/ml-backends/pools/pool/enablement",
            lambda c: c.ml_backends.set_pool_enablement(PID, "pool", True),
        ),
    ],
)
def test_project_model_management_maps_permission_error(
    client, respx_mock, method, path, call
):
    getattr(respx_mock, method)(f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        call(client)


def test_registry_crud_health_and_unload(client, respx_mock):
    backend = _backend()
    backend.pop("project_id")
    respx_mock.get(f"{API}/admin/ml-integrations/all").mock(
        return_value=httpx.Response(200, json={"items": [backend]})
    )
    assert isinstance(client.ml_registry.list()[0], MLBackend)

    create = respx_mock.post(f"{API}/admin/ml-integrations/registry").mock(
        return_value=httpx.Response(201, json=backend)
    )
    client.ml_registry.create(name="sam2", url="http://gpu:9000")
    assert (
        create.calls.last.request.content == b'{"name":"sam2","url":"http://gpu:9000"}'
    )

    update = respx_mock.put(f"{API}/admin/ml-integrations/registry/{BID}").mock(
        return_value=httpx.Response(200, json={**backend, "name": "renamed"})
    )
    assert client.ml_registry.update(BID, name="renamed").name == "renamed"
    assert update.calls.last.request.content == b'{"name":"renamed"}'

    health_route = respx_mock.post(
        f"{API}/admin/ml-integrations/registry/{BID}/health"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "backend_id": BID, "backend_name": "renamed"},
        )
    )
    health = client.ml_registry.check_health(BID)
    assert health_route.called and health.status == "ok"

    respx_mock.post(f"{API}/admin/ml-integrations/registry/{BID}/unload").mock(
        return_value=httpx.Response(200, json={"ok": True, "unloaded": True})
    )
    unloaded = client.ml_registry.unload(BID)
    assert isinstance(unloaded, MLBackendUnloadResult)
    assert unloaded.unloaded is True

    delete = respx_mock.delete(f"{API}/admin/ml-integrations/registry/{BID}").mock(
        return_value=httpx.Response(204)
    )
    assert client.ml_registry.delete(BID) is None
    assert delete.called


@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        ("get", "/admin/ml-integrations/all", lambda c: c.ml_registry.list()),
        (
            "post",
            "/admin/ml-integrations/registry",
            lambda c: c.ml_registry.create(name="x", url="http://x"),
        ),
        (
            "put",
            f"/admin/ml-integrations/registry/{BID}",
            lambda c: c.ml_registry.update(BID, name="x"),
        ),
        (
            "delete",
            f"/admin/ml-integrations/registry/{BID}",
            lambda c: c.ml_registry.delete(BID),
        ),
        (
            "post",
            f"/admin/ml-integrations/registry/{BID}/health",
            lambda c: c.ml_registry.check_health(BID),
        ),
        (
            "post",
            f"/admin/ml-integrations/registry/{BID}/unload",
            lambda c: c.ml_registry.unload(BID),
        ),
    ],
)
def test_registry_maps_permission_error(client, respx_mock, method, path, call):
    getattr(respx_mock, method)(f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        call(client)


def test_cancel_job(client, respx_mock):
    route = respx_mock.post(f"{API}/async-jobs/{BID}/cancel").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client.jobs.cancel(BID)
    assert route.called
    assert route.calls.last.request.method == "POST"


def test_cancel_job_conflict_on_terminal(client, respx_mock):
    respx_mock.post(f"{API}/async-jobs/{BID}/cancel").mock(
        return_value=httpx.Response(409, json={"detail": "cannot cancel terminal job"})
    )
    with pytest.raises(ConflictError):
        client.jobs.cancel(BID)
