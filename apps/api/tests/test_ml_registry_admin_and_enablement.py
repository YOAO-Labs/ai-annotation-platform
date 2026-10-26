"""v0.19.0 ADR-0044 · PR3 端点:
- superadmin 全局注册表 CRUD (/admin/ml-integrations/registry)
- 项目启用勾选清单 (GET /projects/{id}/ml-backends/available)
- 项目启用切换 + 覆盖 (PUT /projects/{id}/ml-backends/{rid}/enablement)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import GPUArbiterMode, settings
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.user import User
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from aap_inference_contract.topology import PoolFacts
from app.services.ml_backend import MLBackendDeleteBlocked, MLBackendService
from app.services.ml_routing.contracts import RoutingCandidate, TrafficState
from app.services.ml_routing.router import make_ledger_from_settings
from tests.conftest import create_registry_with_pool


async def _seed_project(db: AsyncSession, owner_id: uuid.UUID, **overrides) -> Project:
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-RG-{suffix}",
        name=f"rg-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        **overrides,
    )
    db.add(proj)
    await db.flush()
    return proj


async def _singleton_pool(db: AsyncSession, registry_id: uuid.UUID) -> PoolFacts | None:
    """Resolve a registry's singleton service pool through the routing owner."""
    service = MLBackendService(db)
    pool_id = await service.pool_id_for_registry(registry_id)
    if pool_id is None:
        return None
    return await service.get_pool(pool_id)


async def _seed_registry(db: AsyncSession, name: str = "g") -> MLBackendRegistry:
    # v0.23.3 ADR-0050 · 每 registry 须有 singleton pool (alembic 0132 backfill 语义)。
    registry, _pool = await create_registry_with_pool(
        db,
        id=uuid.uuid4(),
        name=name,
        url=f"http://reg/{uuid.uuid4().hex[:8]}",
        is_interactive=False,
        state="connected",
        last_checked_at=datetime.now(UTC),
    )
    return registry


async def _drain_registry_member(
    db: AsyncSession, registry_id: uuid.UUID, monkeypatch
) -> uuid.UUID:
    service = MLBackendService(db)
    pool = await _singleton_pool(db, registry_id)
    assert pool is not None
    await service.drain_pool_member(pool.id, registry_id)
    await db.commit()
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    return pool.id


def _configure_gpu(
    monkeypatch,
    *,
    allocatable_mb: int = 22000,
    global_mode: GPUArbiterMode = GPUArbiterMode.OBSERVE,
    resource_mode: str = "observe",
) -> str:
    resource_id = "node-a/GPU-test"
    monkeypatch.setattr(settings, "gpu_arbiter_mode", global_mode)
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        '{"node-a/GPU-test":{"node_id":"node-a",'
        '"physical_device_token":"GPU-test",'
        f'"allocatable_mb":{allocatable_mb},"mode":"{resource_mode}"}}}}',
    )
    return resource_id


def _gpu_diagnostic(code, level, *, resource_id=None, backend_id=None):
    return {
        "code": code,
        "level": level,
        "message": code,
        "resource_id": resource_id,
        "backend_id": backend_id,
    }


def _gpu_config(
    *,
    status="ok",
    desired_mode="off",
    effective_mode="off",
    diagnostics=(),
    rollout_enabled=False,
    rollout_state="disabled",
    rollout_revision=None,
    resource_claimed_budget_mb=None,
    allocatable_mb=None,
):
    return {
        "status": status,
        "desired_mode": desired_mode,
        "effective_mode": effective_mode,
        "rollout_enabled": rollout_enabled,
        "rollout_state": rollout_state,
        "rollout_revision": rollout_revision,
        "rollout_blocker_reason": None,
        "allocatable_mb": allocatable_mb,
        "resource_claimed_budget_mb": resource_claimed_budget_mb,
        "diagnostics": list(diagnostics),
    }


def _gpu_resource(
    resource_id,
    *,
    allocatable_mb=20000,
    desired_mode="observe",
    effective_mode="observe",
    claimed_budget_mb=0,
    claimed_backend_count=0,
    diagnostics=(),
):
    return {
        "gpu_resource_id": resource_id,
        "node_id": "node-a",
        "physical_device_token": "GPU-0",
        "allocatable_mb": allocatable_mb,
        "configured_mode": desired_mode,
        "desired_mode": desired_mode,
        "effective_mode": effective_mode,
        "claimed_budget_mb": claimed_budget_mb,
        "claimed_backend_count": claimed_backend_count,
        "status": "ok",
        "diagnostics": list(diagnostics),
    }


def _gpu_rollout(
    resource_id,
    *,
    state="disabled",
    effective_mode="off",
    target_mode=None,
    dispatch_blocked=False,
    revision=None,
):
    return {
        "resource_id": resource_id,
        "state": state,
        "effective_mode": effective_mode,
        "target_mode": target_mode,
        "dispatch_blocked": dispatch_blocked,
        "blocked_reason": None,
        "transition_id": None,
        "last_transition_id": None,
        "revision": revision,
    }


def _gpu_admin_authority(
    monkeypatch,
    *,
    resources=(),
    rollouts=(),
    diagnostics=(),
    backends=(),
    runtime=None,
):
    runtime = runtime or {
        "status": "ready",
        "reason": "",
        "ready": True,
        "ledger_revision": 7,
        "ledger_incarnation": "runtime-incarnation",
        "reconcile_deadline_ms": 0,
        "committed_mb": 0,
        "backend_count": 1,
        "active_backend_count": 1,
        "membership_state_counts": {"pending": 0, "active": 1, "retiring": 0},
        "allocation_state_counts": {},
        "lease_count": 0,
        "card_queue_count": 0,
        "backend_queue_count": 0,
        "transition_present": False,
        "durable_domain_matches": True,
    }
    result = {
        "resources": list(resources),
        "rollouts": list(rollouts),
        "diagnostics": list(diagnostics),
        "backends": list(backends),
    }

    class RuntimeReply:
        def model_dump(self, **_kwargs):
            return runtime

    class Authority:
        async def get_diagnostics(self):
            return SimpleNamespace(result=result)

        async def observe_resource(self, resource_id, *, allocatable_mb):
            assert any(
                item["gpu_resource_id"] == resource_id for item in result["resources"]
            )
            assert allocatable_mb > 0
            return RuntimeReply()

    authority = Authority()
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.gpu_authority_client",
        lambda: authority,
    )
    return authority


# ── admin 全局 CRUD ──────────────────────────────────────────────────────


async def test_create_registry_superadmin(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/registry",
        json={"name": "alpha", "url": "http://alpha:8000", "is_interactive": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "alpha"
    assert body["url"] == "http://alpha:8000"
    assert body["project_id"] is None
    assert body["gpu_resource_id"] is None
    assert body["vram_budget_mb"] is None
    assert body["eviction_priority"] == 0


async def test_create_registry_round_trips_gpu_claim(
    httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch)

    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/registry",
        json={
            "name": "gpu-alpha",
            "url": "http://gpu-alpha:8000",
            "gpu_resource_id": resource_id,
            "vram_budget_mb": 18000,
            "eviction_priority": 7,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 201, res.text
    assert res.json()["gpu_resource_id"] == resource_id
    assert res.json()["vram_budget_mb"] == 18000
    assert res.json()["eviction_priority"] == 7


async def test_create_registry_rejects_unknown_or_oversized_gpu_claim(
    httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch, allocatable_mb=10000)

    for claim in (
        {"gpu_resource_id": "missing/GPU-x", "vram_budget_mb": 9000},
        {"gpu_resource_id": resource_id, "vram_budget_mb": 10001},
    ):
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/registry",
            json={
                "name": f"bad-{uuid.uuid4()}",
                "url": f"http://{uuid.uuid4()}:8000",
                **claim,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["error_code"] == "gpu_config_invalid"
        assert res.json()["detail"]["diagnostics"][0]["level"] == "blocker"


async def test_create_registry_duplicate_url_409(httpx_client, db_session, super_admin):
    _, token = super_admin
    existing = await _seed_registry(db_session, name="dup")
    await db_session.commit()
    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/registry",
        json={"name": "dup2", "url": existing.url},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error_code"] == "ml_backend_url_conflict"


async def test_registry_schema_gpu_errors_use_stable_422_envelope(
    httpx_client, super_admin
):
    _, token = super_admin
    invalid_claims = (
        {"gpu_resource_id": "node-a/GPU-test"},
        {"gpu_resource_id": "node-a/GPU-test", "vram_budget_mb": "1000"},
        {"gpu_resource_id": "node-a/cuda:0", "vram_budget_mb": 1000},
        {"eviction_priority": None},
    )

    for claim in invalid_claims:
        res = await httpx_client.post(
            "/api/v1/admin/ml-integrations/registry",
            json={
                "name": f"invalid-{uuid.uuid4()}",
                "url": f"http://invalid-{uuid.uuid4()}:8000",
                **claim,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["error_code"] == "gpu_config_invalid"
        assert res.json()["detail"]["diagnostics"][0]["level"] == "blocker"


async def test_registry_update_null_priority_uses_stable_422_envelope(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="null-priority")
    await db_session.commit()

    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        json={"eviction_priority": None},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text
    assert res.json()["detail"]["error_code"] == "gpu_config_invalid"
    assert res.json()["detail"]["diagnostics"][0]["field"] == "eviction_priority"


async def test_registry_non_gpu_schema_error_keeps_fastapi_validation_envelope(
    httpx_client, super_admin
):
    _, token = super_admin

    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/registry",
        json={"url": "http://missing-name:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422, res.text
    assert isinstance(res.json()["detail"], list)
    assert res.json()["detail"][0]["loc"][-1] == "name"


async def test_create_registry_requires_superadmin(httpx_client, project_admin):
    _, token = project_admin
    res = await httpx_client.post(
        "/api/v1/admin/ml-integrations/registry",
        json={"name": "x", "url": "http://x:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


async def test_project_registry_payload_cannot_set_global_gpu_claim(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await _seed_project(db_session, user.id)
    await db_session.commit()

    res = await httpx_client.post(
        f"/api/v1/projects/{project.id}/ml-backends",
        json={
            "name": "smuggled-claim",
            "url": "http://smuggled-claim:8000",
            "gpu_resource_id": "node-a/GPU-test",
            "vram_budget_mb": 1000,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 201, res.text
    assert res.json()["gpu_resource_id"] is None
    assert res.json()["vram_budget_mb"] is None


async def test_project_admin_cannot_create_or_update_global_registry_row(
    httpx_client, db_session, project_admin
):
    user, token = project_admin
    project = await _seed_project(db_session, user.id)
    backend = await _seed_registry(db_session, name="shared-global")
    pool = await _singleton_pool(db_session, backend.id)
    db_session.add(
        ProjectMLBackendPool(
            project_id=project.id,
            pool_id=pool.id,
            enabled=True,
        )
    )
    await db_session.commit()

    create = await httpx_client.post(
        f"/api/v1/projects/{project.id}/ml-backends",
        json={"name": "forbidden", "url": "http://forbidden:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )
    update = await httpx_client.put(
        f"/api/v1/projects/{project.id}/ml-backends/{backend.id}",
        json={"url": "http://hijacked:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert create.status_code == 403
    assert update.status_code == 403
    await db_session.refresh(backend)
    assert backend.url != "http://hijacked:8000"


async def test_update_registry(httpx_client, db_session, super_admin):
    _, token = super_admin
    b = await _seed_registry(db_session, name="old")
    await db_session.commit()
    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{b.id}",
        json={"name": "renamed", "is_interactive": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "renamed"
    assert res.json()["is_interactive"] is True


async def test_update_registry_merges_and_clears_gpu_claim(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch)
    b = await _seed_registry(db_session, name="claimed")
    b.gpu_resource_id = resource_id
    b.vram_budget_mb = 18000
    b.eviction_priority = 1
    await db_session.commit()

    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{b.id}",
        json={"vram_budget_mb": 17000, "eviction_priority": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["gpu_resource_id"] == resource_id
    assert res.json()["vram_budget_mb"] == 17000
    assert res.json()["eviction_priority"] == 2

    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{b.id}",
        json={"gpu_resource_id": None, "vram_budget_mb": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["gpu_resource_id"] is None
    assert res.json()["vram_budget_mb"] is None


async def test_update_registry_duplicate_url_is_structured_409(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    first = await _seed_registry(db_session, name="first")
    second = await _seed_registry(db_session, name="second")
    await db_session.commit()

    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{second.id}",
        json={"url": first.url},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error_code"] == "ml_backend_url_conflict"


async def test_managed_gpu_registry_mutation_requires_retirement(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch)
    backend = await _seed_registry(db_session, name="managed")
    backend.gpu_resource_id = resource_id
    backend.vram_budget_mb = 18000
    await db_session.flush()
    fence = await db_session.get(GPUBackendFence, backend.id)
    assert fence is not None
    fence.runtime_epoch_high_water = 1
    await db_session.commit()

    update_response = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        json={"url": "http://managed-replacement:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )
    delete_response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert update_response.status_code == 409, update_response.text
    assert (
        update_response.json()["detail"]["error_code"]
        == "gpu_backend_retirement_required"
    )
    assert delete_response.status_code == 409, delete_response.text
    assert (
        delete_response.json()["detail"]["error_code"]
        == "gpu_backend_retirement_required"
    )


async def test_update_registry_url_invalidates_old_health_evidence(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="old-endpoint")
    backend.health_meta = {
        "compute": {"configured_device": "cpu"},
        "gpu_info": {"device_uuid": "GPU-old"},
    }
    await db_session.commit()

    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        json={"url": "http://new-endpoint:8000"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["state"] == "disconnected"
    assert res.json()["health_meta"] is None
    await db_session.refresh(backend)
    assert backend.last_checked_at is None


async def test_gpu_resources_reports_per_card_oversubscription_warning(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch, allocatable_mb=20000)
    first = await _seed_registry(db_session, name="gpu-a")
    second = await _seed_registry(db_session, name="gpu-b")
    for backend in (first, second):
        backend.gpu_resource_id = resource_id
        backend.vram_budget_mb = 12000
    await db_session.commit()

    diagnostic = _gpu_diagnostic(
        "gpu_resource_oversubscribed", "warning", resource_id=resource_id
    )
    _gpu_admin_authority(
        monkeypatch,
        resources=[
            _gpu_resource(
                resource_id,
                allocatable_mb=20000,
                desired_mode="observe",
                effective_mode="observe",
                claimed_budget_mb=24000,
                claimed_backend_count=2,
                diagnostics=[diagnostic],
            )
        ],
        rollouts=[_gpu_rollout(resource_id)],
        runtime={
            "status": "disabled",
            "reason": "gpu_arbiter_disabled",
            "ready": False,
            "ledger_revision": None,
            "ledger_incarnation": None,
            "reconcile_deadline_ms": None,
            "committed_mb": None,
            "backend_count": 2,
            "active_backend_count": 0,
            "membership_state_counts": {
                "pending": 2,
                "active": 0,
                "retiring": 0,
            },
            "allocation_state_counts": {},
            "lease_count": None,
            "card_queue_count": None,
            "backend_queue_count": None,
            "transition_present": None,
            "durable_domain_matches": None,
        },
    )

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/gpu-resources",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    resource = res.json()["resources"][0]
    assert res.json()["global_desired_mode"] == "observe"
    assert res.json()["runtime_ready"] is True
    assert res.json()["observe_runtime_ready"] is True
    assert res.json()["enforce_runtime_ready"] is False
    assert resource["gpu_resource_id"] == resource_id
    assert resource["desired_mode"] == "observe"
    assert resource["effective_mode"] == "observe"
    assert resource["claimed_budget_mb"] == 24000
    assert resource["claimed_backend_count"] == 2
    assert resource["status"] == "warning"
    assert resource["diagnostics"][0]["code"] == "gpu_resource_oversubscribed"
    assert resource["runtime"]["status"] == "disabled"
    assert resource["runtime"]["membership_state_counts"] == {
        "pending": 2,
        "active": 0,
        "retiring": 0,
    }


async def test_gpu_resources_requires_superadmin(httpx_client, project_admin):
    _, token = project_admin

    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/gpu-resources",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 403


async def test_enforce_desired_mode_reports_disabled_rollout_latch(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(
        monkeypatch,
        global_mode=GPUArbiterMode.ENFORCE,
        resource_mode="enforce",
    )
    backend = await _seed_registry(db_session, name="enforce-pending")
    backend.gpu_resource_id = resource_id
    backend.vram_budget_mb = 10000
    await db_session.commit()

    diagnostic = _gpu_diagnostic(
        "gpu_rollout_disabled", "blocker", resource_id=resource_id
    )
    _gpu_admin_authority(
        monkeypatch,
        resources=[
            _gpu_resource(
                resource_id,
                desired_mode="enforce",
                effective_mode="off",
                claimed_budget_mb=10000,
                claimed_backend_count=1,
                diagnostics=[diagnostic],
            )
        ],
        rollouts=[
            _gpu_rollout(
                resource_id,
                state="disabled",
                effective_mode="off",
                target_mode="enforce",
                dispatch_blocked=True,
            )
        ],
        backends=[
            {
                "backend_id": str(backend.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    desired_mode="enforce",
                    effective_mode="off",
                    diagnostics=[diagnostic],
                    resource_claimed_budget_mb=10000,
                    allocatable_mb=22000,
                ),
            }
        ],
        runtime={
            "status": "disabled",
            "reason": "gpu_arbiter_disabled",
            "ready": False,
            "ledger_revision": None,
            "ledger_incarnation": None,
            "reconcile_deadline_ms": None,
            "committed_mb": None,
            "backend_count": 1,
            "active_backend_count": 0,
            "membership_state_counts": {
                "pending": 1,
                "active": 0,
                "retiring": 0,
            },
            "allocation_state_counts": {},
            "lease_count": None,
            "card_queue_count": None,
            "backend_queue_count": None,
            "transition_present": None,
            "durable_domain_matches": None,
        },
    )

    resources = await httpx_client.get(
        "/api/v1/admin/ml-integrations/gpu-resources",
        headers={"Authorization": f"Bearer {token}"},
    )
    backends = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resources.status_code == 200, resources.text
    resource = resources.json()["resources"][0]
    assert resources.json()["rollout_enabled"] is False
    assert resource["desired_mode"] == "enforce"
    assert resource["effective_mode"] == "off"
    assert resource["rollout"]["state"] == "disabled"
    assert resource["rollout"]["effective_mode"] == "off"
    assert resource["status"] == "blocker"
    assert any(
        item["code"] == "gpu_rollout_disabled" for item in resource["diagnostics"]
    )

    item = next(row for row in backends.json()["items"] if row["id"] == str(backend.id))
    assert item["gpu_config"]["desired_mode"] == "enforce"
    assert item["gpu_config"]["effective_mode"] == "off"
    assert item["gpu_config"]["rollout_enabled"] is False
    assert item["gpu_config"]["rollout_state"] == "disabled"
    assert item["gpu_config"]["status"] == "blocker"


@pytest.mark.parametrize(
    (
        "rollout_state",
        "rollout_enabled",
        "runtime_ready",
        "expected_effective",
        "expected_code",
    ),
    (
        ("enforcing", True, True, "enforce", None),
        ("blocked", True, False, "off", "gpu_rollout_not_ready"),
        ("enforcing", False, False, "off", "gpu_rollout_active_while_disabled"),
    ),
)
async def test_gpu_resources_projects_rollout_and_runtime_facts(
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
    rollout_state: str,
    rollout_enabled: bool,
    runtime_ready: bool,
    expected_effective: str,
    expected_code: str | None,
) -> None:
    _, token = super_admin
    resource_id = _configure_gpu(
        monkeypatch,
        global_mode=GPUArbiterMode.ENFORCE,
        resource_mode="enforce",
    )
    monkeypatch.setattr(settings, "gpu_arbiter_rollout_enabled", rollout_enabled)
    backend = await _seed_registry(db_session, name=f"rollout-{rollout_state}")
    backend.gpu_resource_id = resource_id
    backend.vram_budget_mb = 10000
    await db_session.commit()

    diagnostics = (
        [
            _gpu_diagnostic(
                expected_code,
                "blocker",
                resource_id=resource_id,
                backend_id=backend.id,
            )
        ]
        if expected_code is not None
        else []
    )
    _gpu_admin_authority(
        monkeypatch,
        resources=[
            _gpu_resource(
                resource_id,
                desired_mode="enforce",
                effective_mode=expected_effective,
                claimed_budget_mb=10000,
                claimed_backend_count=1,
                diagnostics=diagnostics,
            )
        ],
        rollouts=[
            _gpu_rollout(
                resource_id,
                state=rollout_state,
                effective_mode=expected_effective,
                target_mode="enforce",
                dispatch_blocked=not runtime_ready,
                revision=7,
            )
        ],
        backends=[
            {
                "backend_id": str(backend.id),
                "gpu_config": _gpu_config(
                    status="blocker" if expected_code else "ok",
                    desired_mode="enforce",
                    effective_mode=expected_effective,
                    diagnostics=diagnostics,
                    rollout_enabled=rollout_enabled,
                    rollout_state=rollout_state,
                    rollout_revision=7,
                    resource_claimed_budget_mb=10000,
                    allocatable_mb=22000,
                ),
            }
        ],
        runtime={
            "status": "ready" if runtime_ready else "not_ready",
            "reason": "" if runtime_ready else "backend_gate_unconfirmed",
            "ready": runtime_ready,
            "ledger_revision": 7,
            "ledger_incarnation": "runtime-incarnation",
            "reconcile_deadline_ms": 0,
            "committed_mb": 0,
            "backend_count": 1,
            "active_backend_count": 1,
            "membership_state_counts": {
                "pending": 0,
                "active": 1,
                "retiring": 0,
            },
            "allocation_state_counts": {},
            "lease_count": 0,
            "card_queue_count": 0,
            "backend_queue_count": 0,
            "transition_present": False,
            "durable_domain_matches": True,
        },
    )

    resources = await httpx_client.get(
        "/api/v1/admin/ml-integrations/gpu-resources",
        headers={"Authorization": f"Bearer {token}"},
    )
    backends = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resources.status_code == 200, resources.text
    assert backends.status_code == 200, backends.text
    body = resources.json()
    resource = body["resources"][0]
    assert body["rollout_enabled"] is rollout_enabled
    assert body["runtime_ready"] is runtime_ready
    assert body["enforce_runtime_ready"] is runtime_ready
    assert resource["effective_mode"] == expected_effective
    assert resource["rollout"]["state"] == rollout_state
    assert resource["rollout"]["revision"] is not None
    assert resource["runtime"]["ready"] is runtime_ready
    if expected_code is None:
        assert resource["status"] != "blocker"
    else:
        assert expected_code in {
            diagnostic["code"] for diagnostic in resource["diagnostics"]
        }

    item = next(row for row in backends.json()["items"] if row["id"] == str(backend.id))
    gpu_config = item["gpu_config"]
    assert gpu_config["effective_mode"] == expected_effective
    assert gpu_config["rollout_enabled"] is rollout_enabled
    assert gpu_config["rollout_state"] == rollout_state
    assert gpu_config["rollout_revision"] is not None
    if expected_code is not None:
        assert expected_code in {
            diagnostic["code"] for diagnostic in gpu_config["diagnostics"]
        }


async def test_global_list_exposes_missing_claim_and_identity_blockers(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch)
    unclaimed = await _seed_registry(db_session, name="unclaimed-gpu")
    unclaimed.health_meta = {
        "compute": {"configured_device": "cuda"},
        "residency": {"state": "unloaded", "gpu_loaded": False},
    }
    mismatch = await _seed_registry(db_session, name="wrong-card")
    mismatch.gpu_resource_id = resource_id
    mismatch.vram_budget_mb = 10000
    mismatch.health_meta = {
        "gpu_info": {"device_uuid": "GPU-other"},
        "compute": {"configured_device": "cuda"},
    }
    unknown = await _seed_registry(db_session, name="unknown-device")
    explicit_cpu = await _seed_registry(db_session, name="cpu-only")
    explicit_cpu.health_meta = {"compute": {"configured_device": "cpu"}}
    await db_session.commit()

    _gpu_admin_authority(
        monkeypatch,
        backends=[
            {
                "backend_id": str(unclaimed.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    desired_mode="observe",
                    effective_mode="observe",
                    diagnostics=[
                        _gpu_diagnostic(
                            "gpu_claim_missing", "blocker", backend_id=unclaimed.id
                        )
                    ],
                ),
            },
            {
                "backend_id": str(mismatch.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    desired_mode="observe",
                    effective_mode="observe",
                    diagnostics=[
                        _gpu_diagnostic(
                            "gpu_identity_mismatch",
                            "blocker",
                            resource_id=resource_id,
                            backend_id=mismatch.id,
                        )
                    ],
                ),
            },
            {
                "backend_id": str(unknown.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    desired_mode="observe",
                    effective_mode="observe",
                    diagnostics=[
                        _gpu_diagnostic(
                            "gpu_claim_unknown", "blocker", backend_id=unknown.id
                        )
                    ],
                ),
            },
            {
                "backend_id": str(explicit_cpu.id),
                "gpu_config": _gpu_config(
                    status="info",
                    diagnostics=[
                        _gpu_diagnostic(
                            "explicit_cpu_backend", "info", backend_id=explicit_cpu.id
                        )
                    ],
                ),
            },
        ],
    )
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    by_name = {item["name"]: item for item in res.json()["items"]}
    assert by_name["unclaimed-gpu"]["gpu_config"]["status"] == "blocker"
    assert (
        by_name["unclaimed-gpu"]["gpu_config"]["diagnostics"][0]["code"]
        == "gpu_claim_missing"
    )
    assert by_name["wrong-card"]["gpu_config"]["status"] == "blocker"
    assert (
        by_name["wrong-card"]["gpu_config"]["diagnostics"][0]["code"]
        == "gpu_identity_mismatch"
    )
    assert by_name["unknown-device"]["gpu_config"]["status"] == "blocker"
    assert (
        by_name["unknown-device"]["gpu_config"]["diagnostics"][0]["code"]
        == "gpu_claim_unknown"
    )
    assert by_name["cpu-only"]["gpu_config"]["status"] == "info"
    assert (
        by_name["cpu-only"]["gpu_config"]["diagnostics"][0]["code"]
        == "explicit_cpu_backend"
    )


async def test_global_list_does_not_trust_stale_cpu_health_snapshot(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="stale-cpu")
    backend.health_meta = {"compute": {"configured_device": "cpu"}}
    backend.last_checked_at = datetime.now(UTC) - timedelta(minutes=4)
    await db_session.commit()

    _gpu_admin_authority(
        monkeypatch,
        backends=[
            {
                "backend_id": str(backend.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    diagnostics=[
                        _gpu_diagnostic(
                            "gpu_claim_unknown", "blocker", backend_id=backend.id
                        )
                    ],
                ),
            }
        ],
    )
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    item = next(row for row in res.json()["items"] if row["id"] == str(backend.id))
    assert item["gpu_config"]["status"] == "blocker"
    assert item["gpu_config"]["diagnostics"][0]["code"] == "gpu_claim_unknown"


async def test_gpu_resources_surfaces_invalid_env_config_without_startup_failure(
    httpx_client, super_admin, monkeypatch
):
    _, token = super_admin
    monkeypatch.setattr(settings, "gpu_arbiter_resources_json", "{not-json")

    _gpu_admin_authority(
        monkeypatch,
        diagnostics=[_gpu_diagnostic("gpu_resources_config_invalid", "blocker")],
    )
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/gpu-resources",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["runtime_ready"] is False
    assert res.json()["resources"] == []
    assert res.json()["diagnostics"][0]["code"] == "gpu_resources_config_invalid"
    assert res.json()["diagnostics"][0]["level"] == "blocker"


async def test_global_list_keeps_malformed_legacy_residency_observable(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="malformed-residency")
    backend.health_meta = {
        "compute": [],
        "gpu_info": "unreadable",
        "residency": {},
    }
    await db_session.commit()

    _gpu_admin_authority(
        monkeypatch,
        backends=[
            {
                "backend_id": str(backend.id),
                "gpu_config": _gpu_config(
                    status="blocker",
                    diagnostics=[
                        _gpu_diagnostic(
                            "gpu_claim_unknown", "blocker", backend_id=backend.id
                        )
                    ],
                ),
            }
        ],
    )
    res = await httpx_client.get(
        "/api/v1/admin/ml-integrations/all",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    item = next(row for row in res.json()["items"] if row["id"] == str(backend.id))
    assert item["health_meta"]["residency"] == {}
    assert item["gpu_config"]["status"] == "blocker"
    assert item["gpu_config"]["diagnostics"][0]["code"] == "gpu_claim_unknown"


async def test_update_registry_404(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{uuid.uuid4()}",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


async def test_delete_registry_clears_all_matching_primary_bindings_only(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="todel")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    unrelated_backend = await _seed_registry(db_session, name="unrelated")
    unrelated_pool = await _singleton_pool(db_session, unrelated_backend.id)
    assert unrelated_pool is not None

    affected = [await _seed_project(db_session, user.id) for _ in range(2)]
    unrelated = await _seed_project(db_session, user.id)
    for project in affected:
        project.ml_backend_pool_id = pool.id
    unrelated.ml_backend_pool_id = unrelated_pool.id
    db_session.add(
        ProjectMLBackendPool(project_id=affected[0].id, pool_id=pool.id, enabled=True)
    )

    await _drain_registry_member(db_session, backend.id, monkeypatch)
    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert await MLBackendService(db_session).get_registry(backend.id) is None
    assert (
        await MLBackendService(db_session).get_registry(unrelated_backend.id)
        is not None
    )
    rows = (
        await db_session.execute(
            select(Project.id, Project.ml_backend_pool_id).where(
                Project.id.in_([project.id for project in (*affected, unrelated)])
            )
        )
    ).all()
    assert dict(rows) == {
        affected[0].id: None,
        affected[1].id: None,
        unrelated.id: unrelated_pool.id,
    }
    enabled_binding = await db_session.scalar(
        select(ProjectMLBackendPool).where(
            ProjectMLBackendPool.project_id == affected[0].id,
            ProjectMLBackendPool.pool_id == pool.id,
        )
    )
    assert enabled_binding is not None


async def test_delete_missing_registry_preserves_unrelated_binding(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="still-present")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    await db_session.commit()

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert await MLBackendService(db_session).get_registry(backend.id) is not None
    assert (
        await db_session.scalar(
            select(Project.ml_backend_pool_id).where(Project.id == project.id)
        )
        == pool.id
    )


async def test_delete_gpu_fenced_registry_preserves_primary_binding(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="gpu-fenced")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    db_session.add(
        GPUBackendFence(backend_registry_id=backend.id, runtime_epoch_high_water=1)
    )
    await db_session.commit()

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == (
        "gpu_backend_retirement_required"
    )
    assert await MLBackendService(db_session).get_registry(backend.id) is not None
    assert (
        await db_session.scalar(
            select(Project.ml_backend_pool_id).where(Project.id == project.id)
        )
        == pool.id
    )


async def test_delete_registry_with_running_prediction_preserves_binding(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="busy-prediction")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    db_session.add(
        AsyncJob(
            kind="batch_predict",
            project_id=project.id,
            user_id=user.id,
            status=AsyncJobStatus.RUNNING.value,
            payload={"ml_backend_id": str(backend.id)},
        )
    )
    await db_session.commit()

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert await MLBackendService(db_session).get_registry(backend.id) is not None
    assert (
        await db_session.scalar(
            select(Project.ml_backend_pool_id).where(Project.id == project.id)
        )
        == pool.id
    )


@pytest.mark.parametrize("autoflush", [True, False])
async def test_registry_running_work_capability_preserves_caller_autoflush(
    db_session, autoflush
):
    backend = await _seed_registry(db_session, name=f"pending-count-{autoflush}")
    pending = AsyncJob(
        kind="batch_predict",
        status=AsyncJobStatus.RUNNING.value,
        payload={"ml_backend_id": str(backend.id)},
    )
    db_session.add(pending)
    service = MLBackendService(db_session)
    if autoflush:
        with pytest.raises(MLBackendDeleteBlocked) as blocked:
            await service.validate_delete(backend.id)
        assert blocked.value.running_jobs == 1
    else:
        with db_session.no_autoflush:
            assert await service.validate_delete(backend.id) is True
        await db_session.flush()
        with pytest.raises(MLBackendDeleteBlocked) as blocked:
            await service.validate_delete(backend.id)
        assert blocked.value.running_jobs == 1
    await db_session.rollback()


async def test_delete_registry_requires_member_draining(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="not-draining")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    await db_session.commit()
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "ml_backend_member_draining"
    assert await MLBackendService(db_session).get_registry(backend.id) is not None
    assert (
        await db_session.scalar(
            select(Project.ml_backend_pool_id).where(Project.id == project.id)
        )
        == pool.id
    )


async def test_delete_registry_rejects_live_redis_route_lease(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="live-route-lease")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    ledger = make_ledger_from_settings()
    lease = None
    try:
        # Exercise the real isolated Redis ledger without contacting any model/GPU endpoint.
        lease, rejection = await ledger.acquire(
            str(pool.id),
            pool.routing_generation,
            [
                RoutingCandidate(
                    instance_id=backend.id,
                    weight=1,
                    max_concurrency=1,
                    fingerprint_ok=True,
                    health_fresh=True,
                    traffic_state=TrafficState.ACTIVE,
                )
            ],
            owner=f"test:{uuid.uuid4()}",
            operation="registry-delete-guard",
        )
        assert lease is not None
        assert rejection is None

        await _drain_registry_member(db_session, backend.id, monkeypatch)
        response = await httpx_client.delete(
            f"/api/v1/admin/ml-integrations/registry/{backend.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == (
            "ml_backend_member_not_quiescent"
        )
        assert await MLBackendService(db_session).get_registry(backend.id) is not None
        assert (
            await db_session.scalar(
                select(Project.ml_backend_pool_id).where(Project.id == project.id)
            )
            == pool.id
        )
    finally:
        if lease is not None:
            await ledger.cancel(lease)
        await ledger.aclose()


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("role", "project_admin", 403),
        ("is_active", False, 401),
    ],
)
async def test_delete_registry_rechecks_revoked_super_admin_authority(
    field,
    value,
    expected_status,
    httpx_client,
    db_session,
    super_admin,
    monkeypatch,
):
    user, token = super_admin
    backend = await _seed_registry(db_session, name="revoked-admin")
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    project = await _seed_project(db_session, user.id, ml_backend_pool_id=pool.id)
    await _drain_registry_member(db_session, backend.id, monkeypatch)
    await db_session.execute(
        update(User)
        .where(User.id == user.id)
        .values(**{field: value})
        .execution_options(synchronize_session=False)
    )
    if field == "role":
        assert user.role == "super_admin"
    else:
        assert user.is_active is True

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == expected_status
    assert await MLBackendService(db_session).get_registry(backend.id) is not None
    assert (
        await db_session.scalar(
            select(Project.ml_backend_pool_id).where(Project.id == project.id)
        )
        == pool.id
    )


async def test_delete_registry_commit_failure_rolls_back_registry_and_binding(
    test_engine: AsyncEngine, app_module, monkeypatch
):
    from app.core.security import create_access_token
    from app.deps import get_db

    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    suffix = uuid.uuid4().hex[:8]
    actor_id = uuid.uuid4()
    backend_id: uuid.UUID
    project_id: uuid.UUID
    pool_id: uuid.UUID
    trigger_name = f"fail_primary_clear_{suffix}"
    function_name = f"fail_primary_clear_fn_{suffix}"
    token = create_access_token(subject=str(actor_id), role="super_admin")

    async with sessions() as db:
        async with db.begin():
            actor = User(
                id=actor_id,
                email=f"registry-delete-{suffix}@test.local",
                name="Registry Delete",
                password_hash="test-only",
                role="super_admin",
                is_active=True,
            )
            db.add(actor)
            await db.flush()
            backend = await _seed_registry(db, name=f"commit-failure-{suffix}")
            backend_id = backend.id
            pool = await _singleton_pool(db, backend.id)
            assert pool is not None
            pool_id = pool.id
            project = await _seed_project(db, actor.id, ml_backend_pool_id=pool.id)
            project_id = project.id
            await MLBackendService(db).drain_pool_member(pool.id, backend.id)

    async with sessions() as db:
        async with db.begin():
            await db.execute(
                text(
                    f"""
                    CREATE FUNCTION {function_name}() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.id = '{project_id}'::uuid
                           AND OLD.ml_backend_pool_id = '{pool_id}'::uuid
                           AND NEW.ml_backend_pool_id IS NULL THEN
                            RAISE EXCEPTION 'primary binding commit failure';
                        END IF;
                        RETURN NEW;
                    END;
                    $$
                    """
                )
            )
            await db.execute(
                text(
                    f"""
                    CREATE CONSTRAINT TRIGGER {trigger_name}
                    AFTER UPDATE ON projects
                    DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION {function_name}()
                    """
                )
            )

    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    previous_db_dependency = app_module.dependency_overrides.get(get_db)

    async def request_db():
        async with sessions() as db:
            yield db

    app_module.dependency_overrides[get_db] = request_db
    try:
        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.delete(
                f"/api/v1/admin/ml-integrations/registry/{backend_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 500
        async with sessions() as db:
            assert await db.get(MLBackendRegistry, backend_id) is not None
            assert (
                await db.scalar(
                    select(Project.ml_backend_pool_id).where(Project.id == project_id)
                )
                == pool_id
            )
    finally:
        if previous_db_dependency is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_dependency
        async with sessions() as db:
            async with db.begin():
                await db.execute(
                    text(f"DROP TRIGGER IF EXISTS {trigger_name} ON projects")
                )
                await db.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
                project = await db.get(Project, project_id)
                assert project is not None
                await db.delete(project)
                await MLBackendService(db).delete_with_topology(backend_id)
                await db.execute(
                    delete(MLBackendServicePool).where(
                        MLBackendServicePool.id == pool_id
                    )
                )
                actor = await db.get(User, actor_id)
                assert actor is not None
                await db.delete(actor)


async def test_registry_unload_preserves_gpu_arbiter_error_contract(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="arbiter-reject")
    await _drain_registry_member(db_session, backend.id, monkeypatch)

    async def _reject(_registry_id: uuid.UUID, *, expected_topology=None):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            message="lease full",
            retry_after_s=7,
        )

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.backend_control_service",
        lambda _db: SimpleNamespace(unload=_reject),
    )
    response = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}/unload",
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


# ── 项目启用勾选清单 ──────────────────────────────────────────────────────


async def test_available_lists_all_with_enabled_flag(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    on = await _seed_registry(db_session, name="on")
    off = await _seed_registry(db_session, name="off")
    on_pool = await _singleton_pool(db_session, on.id)
    db_session.add(
        ProjectMLBackendPool(
            project_id=proj.id,
            pool_id=on_pool.id,
            enabled=True,
            default_variants={"sam_variant": "large"},
        )
    )
    await db_session.commit()

    res = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/ml-backends/available",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    by_id = {it["backend"]["id"]: it for it in res.json()["items"]}
    assert by_id[str(on.id)]["enabled"] is True
    assert by_id[str(on.id)]["default_variants"] == {"sam_variant": "large"}
    assert by_id[str(off.id)]["enabled"] is False
    assert by_id[str(off.id)]["default_variants"] is None


async def test_enablement_toggle_and_override(httpx_client, db_session, super_admin):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    b = await _seed_registry(db_session, name="tog")
    await db_session.commit()

    # 启用 + 写变体覆盖
    res = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/{b.id}/enablement",
        json={"enabled": True, "default_variants": {"sam_variant": "base"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enabled"] is True
    assert body["default_variants"] == {"sam_variant": "base"}

    # 停用 (覆盖缺省不动)
    res = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/{b.id}/enablement",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert res.json()["default_variants"] == {"sam_variant": "base"}


async def test_enablement_unknown_backend_404(httpx_client, db_session, super_admin):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    await db_session.commit()
    res = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/{uuid.uuid4()}/enablement",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404
