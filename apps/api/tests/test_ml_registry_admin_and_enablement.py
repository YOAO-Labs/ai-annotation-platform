"""v0.19.0 ADR-0044 · PR3 端点:
- superadmin 全局注册表 CRUD (/admin/ml-integrations/registry)
- 项目启用勾选清单 (GET /projects/{id}/ml-backends/available)
- 项目启用切换 + 覆盖 (PUT /projects/{id}/ml-backends/{rid}/enablement)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import GPUArbiterMode, settings
from app.db.models.gpu_arbiter_rollout import GPUArbiterRollout
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from app.services.gpu_arbitration.reconciliation import (
    GPUResourceRuntimeObservation,
)
from app.services.gpu_arbitration.rollout_state import (
    begin_gpu_arbiter_rollout,
    block_gpu_arbiter_rollout,
    complete_gpu_arbiter_rollout,
)
from app.services.ml_backend import MLBackendService


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


async def _seed_registry(db: AsyncSession, name: str = "g") -> MLBackendRegistry:
    b = MLBackendRegistry(
        id=uuid.uuid4(),
        name=name,
        url=f"http://reg/{uuid.uuid4().hex[:8]}",
        is_interactive=False,
        state="connected",
        last_checked_at=datetime.now(UTC),
    )
    db.add(b)
    await db.flush()
    # v0.23.3 ADR-0050 · 每 registry 须有 singleton pool (alembic 0132 backfill 语义)。
    await MLBackendService(db)._create_singleton_pool(b)
    return b


async def _allow_managed_destructive_operation(
    db: AsyncSession, registry_id: uuid.UUID, monkeypatch
) -> None:
    from app.services.ml_routing import safety

    class QuiescentLedger:
        async def healthcheck(self) -> None:
            return None

        async def member_inflight(self, _pool_id: str, _registry_id: str) -> int:
            return 0

        async def aclose(self) -> None:
            return None

    svc = MLBackendService(db)
    pool = await svc._pool_for_registry(registry_id)
    await svc.drain_pool_member(pool.id, registry_id)
    await db.commit()
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(safety, "make_ledger_from_settings", QuiescentLedger)


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


@pytest.fixture
async def cleanup_gpu_rollout_rows(
    test_engine: AsyncEngine,
    db_session: AsyncSession,
):
    resource_ids: list[str] = []
    yield resource_ids.append
    if not resource_ids:
        return
    await db_session.rollback()
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "ALTER TABLE gpu_arbiter_rollouts DISABLE TRIGGER "
                "trg_validate_gpu_arbiter_rollout"
            )
        )
        await db.execute(
            delete(GPUArbiterRollout).where(
                GPUArbiterRollout.gpu_resource_id.in_(resource_ids)
            )
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_arbiter_rollouts ENABLE TRIGGER "
                "trg_validate_gpu_arbiter_rollout"
            )
        )


def _runtime_observation(*, ready: bool) -> GPUResourceRuntimeObservation:
    return GPUResourceRuntimeObservation(
        status="ready" if ready else "not_ready",
        reason="" if ready else "backend_gate_unconfirmed",
        ready=ready,
        ledger_revision=7,
        ledger_incarnation="runtime-incarnation",
        reconcile_deadline_ms=0,
        committed_mb=0,
        backend_count=1,
        active_backend_count=1,
        membership_state_counts={"pending": 0, "active": 1, "retiring": 0},
        allocation_state_counts={},
        lease_count=0,
        card_queue_count=0,
        backend_queue_count=0,
        transition_present=False,
        durable_domain_matches=True,
    )


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
    pool = await MLBackendService(db_session)._pool_for_registry(backend.id)
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


@pytest.mark.parametrize("active", [False, True])
async def test_managed_gpu_registry_allows_auth_rotation_without_changing_membership(
    httpx_client, db_session, super_admin, monkeypatch, active
):
    from app.db.models.gpu_backend_membership import GPUBackendMembership

    _, token = super_admin
    resource_id = _configure_gpu(monkeypatch)
    backend = await _seed_registry(db_session, name="managed-auth")
    backend.gpu_resource_id = resource_id
    backend.vram_budget_mb = 18000
    await db_session.flush()
    fence = await db_session.get(GPUBackendFence, backend.id)
    if active:
        fence.runtime_epoch_high_water = 1
        await db_session.flush()
        membership = await db_session.get(
            GPUBackendMembership, (backend.id, resource_id)
        )
        membership.state = "active"
        await db_session.flush()
    fence.runtime_epoch_high_water = 3
    fence.generation_high_water = 7
    fence.control_epoch_high_water = 9
    horizon = datetime.now(UTC) + timedelta(minutes=5)
    fence.token_expiry_high_water = horizon
    await db_session.commit()
    membership = await db_session.get(GPUBackendMembership, (backend.id, resource_id))
    membership_epoch = membership.membership_epoch
    membership_state = membership.state

    response = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}",
        json={
            "auth_method": "token",
            "auth_token": "rotation-test-credential-" + "x" * 32,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["auth_method"] == "token"
    assert "auth_token" not in response.json()
    assert "auth_revision" not in response.json()
    assert response.json()["state"] == "disconnected"
    assert response.json()["health_meta"] is None
    await db_session.refresh(backend)
    await db_session.refresh(membership)
    await db_session.refresh(fence)
    assert backend.auth_revision == 1
    assert backend.last_checked_at is None
    assert membership.membership_epoch == membership_epoch
    assert membership.state == membership_state
    assert (
        fence.runtime_epoch_high_water,
        fence.generation_high_water,
        fence.control_epoch_high_water,
        fence.token_expiry_high_water,
    ) == (3, 7, 9, horizon)


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

    def fail_if_called(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("observe resource endpoint must not read arbiter Redis")

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.GPUArbiterStore.from_url",
        fail_if_called,
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
async def test_gpu_resources_reports_durable_rollout_and_runtime_truth(
    httpx_client,
    db_session,
    test_engine,
    super_admin,
    monkeypatch,
    cleanup_gpu_rollout_rows,
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
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_rollout_enabled",
        rollout_enabled,
    )
    backend = await _seed_registry(db_session, name=f"rollout-{rollout_state}")
    backend.gpu_resource_id = resource_id
    backend.vram_budget_mb = 10000
    await db_session.commit()

    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    cleanup_gpu_rollout_rows(resource_id)
    promotion = await begin_gpu_arbiter_rollout(
        factory,
        resource_id,
        GPUArbiterMode.ENFORCE,
    )
    assert promotion.transition_id is not None
    if rollout_state == "enforcing":
        await complete_gpu_arbiter_rollout(
            factory,
            resource_id,
            promotion.transition_id,
        )
    else:
        await block_gpu_arbiter_rollout(
            factory,
            resource_id,
            promotion.transition_id,
            "backend_gate_unconfirmed",
        )

    class FakeStore:
        async def aclose(self) -> None:
            return None

    async def observe(*_args, **_kwargs):
        return _runtime_observation(ready=runtime_ready)

    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.GPUArbiterStore.from_url",
        lambda *_args, **_kwargs: FakeStore(),
    )
    monkeypatch.setattr(
        "app.api.v1.admin_ml_integrations.observe_gpu_resource_runtime",
        observe,
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
    await _seed_registry(db_session, name="unknown-device")
    explicit_cpu = await _seed_registry(db_session, name="cpu-only")
    explicit_cpu.health_meta = {"compute": {"configured_device": "cpu"}}
    await db_session.commit()

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
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="stale-cpu")
    backend.health_meta = {"compute": {"configured_device": "cpu"}}
    backend.last_checked_at = datetime.now(UTC) - timedelta(minutes=4)
    await db_session.commit()

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
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="malformed-residency")
    backend.health_meta = {
        "compute": [],
        "gpu_info": "unreadable",
        "residency": {},
    }
    await db_session.commit()

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


async def test_delete_registry_cascades_project_binding(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    proj = await _seed_project(db_session, user.id)
    b = await _seed_registry(db_session, name="todel")
    pool = await MLBackendService(db_session)._pool_for_registry(b.id)
    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True)
    )
    proj.ml_backend_pool_id = pool.id
    await _allow_managed_destructive_operation(db_session, b.id, monkeypatch)

    res = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/registry/{b.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 204
    # 全局项已删 + 项目绑定 SET NULL
    assert await MLBackendService(db_session).get(b.id) is None


async def test_registry_unload_does_not_require_project_binding(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="unbound-unload")
    await _allow_managed_destructive_operation(db_session, backend.id, monkeypatch)
    calls: list[uuid.UUID] = []

    async def _unload(_self, registry_id: uuid.UUID):
        calls.append(registry_id)
        return {"unloaded": True, "residency": {"state": "unloaded"}}

    monkeypatch.setattr(MLBackendService, "unload", _unload)

    res = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/registry/{backend.id}/unload",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["unloaded"] is True
    assert calls == [backend.id]


async def test_registry_unload_preserves_gpu_arbiter_error_contract(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    backend = await _seed_registry(db_session, name="arbiter-reject")
    await _allow_managed_destructive_operation(db_session, backend.id, monkeypatch)

    async def _reject(_self, _registry_id: uuid.UUID):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            message="lease full",
            retry_after_s=7,
        )

    monkeypatch.setattr(MLBackendService, "unload", _reject)
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
    on_pool = await MLBackendService(db_session)._pool_for_registry(on.id)
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
