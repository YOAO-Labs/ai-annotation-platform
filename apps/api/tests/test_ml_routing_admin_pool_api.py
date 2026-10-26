"""v0.23.3 ADR-0050 §12.1 · Super Admin service-pool / member management API tests."""

from __future__ import annotations

import os
import socket
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from aap_inference.health import reconcile_pool_capability
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.pool_capability import capability_fingerprint
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.topology import to_record

from app.config import settings
from app.db.models.audit_log import AuditLog
from app.db.models.ml_backend_pool import MLBackendPoolMember
from app.services.inference_backend_control import BackendControlService
from app.services.inference_health_data import InferenceHealthData
from app.services.inference_topology_data import InferenceTopologyData
from app.services.ml_backend import (
    MLBackendCapabilityRevalidationError,
    MLBackendService,
)
from tests.conftest import create_registry_with_pool
from tests.test_inference_routing_native import _cleanup, _connect, _seed


def _caps(model_id: str = "sam3") -> dict:
    return {
        "capabilities": {"model_ids": [model_id], "supported_trackers": ["sam3_video"]}
    }


async def _reconcile_capability(db_session, registry_id, capabilities) -> None:
    """Seed or invalidate the pool baseline through the Inference health owner port."""
    await reconcile_pool_capability(
        InferenceHealthData(db_session), registry_id, capabilities
    )
    await db_session.flush()


async def _pool_member(db_session, pool_id, registry_id):
    members = await InferenceTopologyData(db_session).pool_members(pool_id)
    return next(member for member in members if member.registry_id == registry_id)


@asynccontextmanager
async def _routing_owner_http(test_engine, monkeypatch):
    from tests.labeling_class_query_helpers import assert_owned_test_database

    await assert_owned_test_database(test_engine)
    dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    if not dsn:
        pytest.skip("requires the verified disposable module-data routing LOGIN")
    runtime = RoutingRuntime(
        dsn,
        health_max_age_seconds=settings.ml_backend_router_health_max_age_seconds,
        lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
    )
    assert await runtime.readiness()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = uuid.uuid4().hex
    server = PipelineHttpServer(
        manager=None,
        data=None,
        control_token=None,
        host="127.0.0.1",
        port=port,
        routing_manager=runtime.manager,
        routing_control_token=token,
    )
    await server.start()
    monkeypatch.setattr(settings, "inference_routing_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(settings, "inference_routing_token", token)
    monkeypatch.setattr(settings, "inference_routing_token_file", "")
    try:
        yield
    finally:
        await server.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "drain", "resume", "remove"])
async def test_member_mutation_projects_cached_pool_after_commit(
    httpx_client, db_session, super_admin, monkeypatch, operation
) -> None:
    _, token = super_admin
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    backend, pool = await create_registry_with_pool(
        db_session,
        name=f"cached-pool-{uuid.uuid4().hex[:8]}",
        health_meta=_caps(),
    )
    await _reconcile_capability(
        db_session, backend.id, backend.health_meta["capabilities"]
    )
    member = await db_session.scalar(
        select(MLBackendPoolMember).where(
            MLBackendPoolMember.pool_id == pool.id,
            MLBackendPoolMember.registry_id == backend.id,
        )
    )
    assert member is not None
    if operation in {"resume", "remove"}:
        member.traffic_state = "draining"
    await db_session.flush()
    await db_session.refresh(pool)
    pool_id, backend_id = pool.id, backend.id
    generation = pool.routing_generation
    previous_updated_at = pool.updated_at
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend_id}"
    if operation == "remove":
        # The savepoint world is intentionally invisible to the out-of-process
        # owner; the real drain refusal is exercised by
        # test_remove_managed_member_requires_real_drain over a committed world.
        from aap_inference.topology import RegistryTopologySnapshot
        from app.services.ml_routing import safety

        async def quiescent(_db, registry_id):
            current = await db_session.scalar(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id == pool_id,
                    MLBackendPoolMember.registry_id == registry_id,
                )
            )
            if current is None:
                return RegistryTopologySnapshot(registry_id, None, None, None, None)
            return RegistryTopologySnapshot(
                registry_id,
                current.id,
                pool_id,
                generation,
                current.traffic_state,
            )

        monkeypatch.setattr(safety, "require_registry_quiescent", quiescent)
    if operation == "upsert":
        response = await httpx_client.put(path, json={"weight": 7}, headers=headers)
        action = "ml_service_pool.member_upserted"
    elif operation == "remove":
        response = await httpx_client.delete(path, headers=headers)
        action = "ml_service_pool.member_removed"
    else:
        response = await httpx_client.post(f"{path}/{operation}", headers=headers)
        action = (
            f"ml_service_pool.member_{'drained' if operation == 'drain' else 'resumed'}"
        )
    assert response.status_code == 200, response.text
    body = response.json()
    # Route responses come from a fresh public pool projection after commit.
    fresh_pool = await MLBackendService(db_session).get_pool(pool_id)
    assert fresh_pool is not None
    assert fresh_pool.updated_at >= previous_updated_at
    assert body["updated_at"] == to_record(fresh_pool)["updated_at"]
    assert body["updated_at"] is not None
    if operation == "remove":
        assert body["members"] == []
        assert body["legacy_instance_id"] is None
        assert body["enabled"] is False
        assert (
            await db_session.scalar(
                select(MLBackendPoolMember.id).where(
                    MLBackendPoolMember.pool_id == pool_id,
                    MLBackendPoolMember.registry_id == backend_id,
                )
            )
            is None
        )
    else:
        assert len(body["members"]) == 1
        assert body["members"][0]["registry_id"] == str(backend_id)
        assert body["members"][0]["weight"] == (7 if operation == "upsert" else 1)
        assert body["members"][0]["traffic_state"] == (
            "draining" if operation == "drain" else "active"
        )
    audits = (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == action, AuditLog.target_id == str(pool_id)
            )
        )
    ).all()
    assert len(audits) == 1
    if operation == "remove":
        repeated = await httpx_client.delete(path, headers=headers)
        assert repeated.status_code == 404
        await db_session.refresh(pool)
        assert pool.routing_generation == generation + 1
        assert (
            len(
                (
                    await db_session.scalars(
                        select(AuditLog).where(
                            AuditLog.action == action,
                            AuditLog.target_id == str(pool_id),
                        )
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_create_pool_returns_disabled_empty(
    httpx_client, db_session, super_admin
) -> None:
    _, token = super_admin
    resp = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "new-pool"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "new-pool"
    assert body["enabled"] is False
    assert body["members"] == []
    assert body["legacy_instance_id"] is None


@pytest.mark.asyncio
async def test_add_member_seeds_fingerprint_and_legacy(
    httpx_client, db_session, super_admin
) -> None:
    """First member seeds pool capability_fingerprint + legacy_instance_id (§7.3)."""
    _, token = super_admin
    # Bare registry WITHOUT a singleton pool (admin pool member mgmt starts from scratch).
    from app.db.models.ml_backend_registry import MLBackendRegistry

    backend = MLBackendRegistry(
        name="bk",
        url=f"http://bk-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps(),
        source="manual",
    )
    db_session.add(backend)
    await db_session.flush()

    # Create empty pool.
    resp = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "sam3-pool"},
        headers={"Authorization": f"Bearer {token}"},
    )
    pool_id = resp.json()["id"]

    # Add member.
    resp = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}",
        json={"weight": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["registry_id"] == str(backend.id)
    assert body["legacy_instance_id"] == str(backend.id)
    assert body["capability_fingerprint"] is not None


@pytest.mark.asyncio
async def test_add_member_capability_mismatch_409(
    httpx_client, db_session, super_admin
) -> None:
    """Adding a member with a divergent fingerprint → 409 capability_mismatch (D3)."""
    _, token = super_admin
    # Two registries with DIFFERENT capabilities.
    from app.db.models.ml_backend_registry import MLBackendRegistry

    b1 = MLBackendRegistry(
        name="sam3-a",
        url=f"http://s3a-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps("sam3"),
        source="manual",
    )
    b2 = MLBackendRegistry(
        name="yolo-b",
        url=f"http://ylb-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta={"capabilities": {"model_ids": ["yolo"], "task": "detect"}},
        source="manual",
    )
    db_session.add_all([b1, b2])
    await db_session.flush()

    # Pool seeded with sam3.
    resp = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "sam3-pool"},
        headers={"Authorization": f"Bearer {token}"},
    )
    pool_id = resp.json()["id"]
    await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{b1.id}",
        json={"weight": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Add yolo (different fingerprint) → 409.
    resp = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{b2.id}",
        json={"weight": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "ml_backend_pool_capability_mismatch"


@pytest.mark.asyncio
async def test_add_member_without_capability_cannot_join_seeded_pool(
    httpx_client, db_session, super_admin
) -> None:
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    seeded = MLBackendRegistry(
        name="seeded",
        url=f"http://seeded-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps(),
        source="manual",
    )
    unknown = MLBackendRegistry(
        name="unknown",
        url=f"http://unknown-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=None,
        source="manual",
    )
    db_session.add_all([seeded, unknown])
    await db_session.flush()
    created = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "capability-required"},
        headers=headers,
    )
    pool_id = created.json()["id"]
    await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{seeded.id}",
        json={"weight": 1},
        headers=headers,
    )

    response = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{unknown.id}",
        json={"weight": 1},
        headers=headers,
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"]["error_code"] == "ml_backend_pool_capability_mismatch"
    )


@pytest.mark.asyncio
async def test_drain_then_resume_member(httpx_client, db_session, super_admin) -> None:
    """drain → traffic_state=draining; resume → back to active (§10.3)."""
    _, token = super_admin
    from app.db.models.ml_backend_registry import MLBackendRegistry

    backend = MLBackendRegistry(
        name="bk",
        url=f"http://bk-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps(),
        source="manual",
    )
    db_session.add(backend)
    await db_session.flush()

    resp = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "p"},
        headers={"Authorization": f"Bearer {token}"},
    )
    pool_id = resp.json()["id"]
    await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}",
        json={"weight": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Drain.
    resp = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}/drain",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["members"][0]["traffic_state"] == "draining"
    # Resume.
    resp = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}/resume",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["members"][0]["traffic_state"] == "active"


@pytest.mark.asyncio
async def test_enable_pool_without_legacy_rejected(
    httpx_client, db_session, super_admin
) -> None:
    """PATCH enabled=true on an empty pool (no legacy) → 409 (D15)."""
    _, token = super_admin
    resp = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "empty"},
        headers={"Authorization": f"Bearer {token}"},
    )
    pool_id = resp.json()["id"]
    resp = await httpx_client.patch(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_service_pools(httpx_client, db_session, super_admin) -> None:
    _, token = super_admin
    await create_registry_with_pool(db_session, name="bk1")
    await create_registry_with_pool(db_session, name="bk2")
    await db_session.flush()
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/service-pools",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()
    names = {it["name"] for it in items}
    assert "bk1" in names and "bk2" in names


@pytest.mark.asyncio
async def test_non_admin_forbidden(httpx_client, db_session, annotator) -> None:
    """Non-super-admin gets 403 on pool management."""
    _, token = annotator
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/service-pools",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    resp = await httpx_client.get(
        f"/api/v1/admin/ml-integrations/service-pools/{uuid.uuid4()}/members/{uuid.uuid4()}/capability-drift",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_member_is_idempotent_upsert_and_preserves_traffic_state(
    httpx_client, db_session, super_admin
) -> None:
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    backend = MLBackendRegistry(
        name="upsert-member",
        url=f"http://upsert-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps(),
        source="manual",
    )
    db_session.add(backend)
    await db_session.flush()
    created = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "upsert-pool"},
        headers=headers,
    )
    pool_id = uuid.UUID(created.json()["id"])
    first = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}",
        json={"weight": 1},
        headers=headers,
    )
    first_generation = first.json()["routing_generation"]
    member = await db_session.scalar(
        select(MLBackendPoolMember).where(
            MLBackendPoolMember.pool_id == pool_id,
            MLBackendPoolMember.registry_id == backend.id,
        )
    )
    member_id = member.id

    await httpx_client.post(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}/drain",
        headers=headers,
    )
    updated = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}",
        json={"weight": 7},
        headers=headers,
    )
    body = updated.json()
    assert body["members"][0]["weight"] == 7
    assert body["members"][0]["traffic_state"] == "draining"
    assert body["routing_generation"] == first_generation + 2
    await db_session.refresh(member)
    assert member.id == member_id

    repeated = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_id}/members/{backend.id}",
        json={"weight": 7},
        headers=headers,
    )
    assert repeated.json()["routing_generation"] == body["routing_generation"]


@pytest.mark.asyncio
async def test_member_cannot_be_owned_by_two_pools(
    httpx_client, db_session, super_admin
) -> None:
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    backend = MLBackendRegistry(
        name="single-owner",
        url=f"http://single-owner-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps(),
        source="manual",
    )
    db_session.add(backend)
    await db_session.flush()
    pool_ids = []
    for name in ("owner-a", "owner-b"):
        response = await httpx_client.post(
            "/api/v1/admin/ml-integrations/service-pools",
            json={"name": name},
            headers=headers,
        )
        pool_ids.append(response.json()["id"])
    await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_ids[0]}/members/{backend.id}",
        json={"weight": 1},
        headers=headers,
    )

    response = await httpx_client.put(
        f"/api/v1/admin/ml-integrations/service-pools/{pool_ids[1]}/members/{backend.id}",
        json={"weight": 1},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "ml_backend_pool_member_conflict"
    assert response.json()["detail"]["current_pool_id"] == pool_ids[0]


@pytest.mark.asyncio
async def test_create_pool_rejects_unowned_legacy_instance_id(
    httpx_client, db_session, super_admin
) -> None:
    _, token = super_admin
    response = await httpx_client.post(
        "/api/v1/admin/ml-integrations/service-pools",
        json={"name": "invalid-legacy", "legacy_instance_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_singleton_capability_is_seeded_then_drift_disables_legacy(
    db_session,
) -> None:
    svc = MLBackendService(db_session)
    backend = await svc.create_registry(
        "capability-seed",
        f"http://capability-seed-{uuid.uuid4().hex[:6]}:9",
    )
    pool = await InferenceTopologyData(db_session).pool_for_registry(backend.id)
    assert pool is not None
    assert pool.capability_fingerprint is None

    await _reconcile_capability(
        db_session,
        backend.id,
        {"models": [{"id": "sam3", "task": "segment", "modalities": ["image"]}]},
    )
    await db_session.flush()
    pool = await InferenceTopologyData(db_session).pool_for_registry(backend.id)
    assert pool is not None
    assert pool.capability_fingerprint is not None
    await svc.update_pool(pool.id, enabled=True)

    await _reconcile_capability(
        db_session,
        backend.id,
        {"models": [{"id": "yolo", "task": "detect", "modalities": ["image"]}]},
    )
    pool = await InferenceTopologyData(db_session).pool_for_registry(backend.id)
    assert pool is not None
    member = await _pool_member(db_session, pool.id, backend.id)
    assert member.traffic_state == "disabled"
    assert pool.enabled is False


async def _create_disabled_drift(db_session):
    backend, pool = await create_registry_with_pool(
        db_session,
        name=f"drift-{uuid.uuid4().hex[:6]}",
        state="connected",
        enabled_pool=True,
        health_meta=_caps("sam3"),
    )
    await _reconcile_capability(
        db_session, backend.id, backend.health_meta["capabilities"]
    )
    backend.health_meta = _caps("sam3-next")
    await _reconcile_capability(
        db_session, backend.id, backend.health_meta["capabilities"]
    )
    facts = await MLBackendService(db_session).get_pool(pool.id)
    assert facts is not None
    member = await _pool_member(db_session, facts.id, backend.id)
    assert member.traffic_state == "disabled"
    assert facts.enabled is False
    return backend, facts, member


@pytest.mark.asyncio
async def test_capability_drift_preview_and_accept_restores_singleton(
    db_session,
) -> None:
    backend, pool, _member = await _create_disabled_drift(db_session)
    svc = MLBackendService(db_session)

    preview = await svc.preview_capability_drift(pool.id, backend.id)
    assert preview is not None
    assert preview.has_drift is True
    assert preview.can_accept is True
    assert preview.member_state == "disabled"
    assert preview.differing_fields == ("model_ids",)
    assert preview.blocking_member_ids == ()
    generation = pool.routing_generation

    accepted = await svc.accept_capability_drift(
        pool.id,
        backend.id,
        expected_candidate_fingerprint=preview.candidate_fingerprint,
        enable_pool=True,
    )
    assert accepted is not None
    accepted_pool, result = accepted
    assert result.old_fingerprint == preview.pool_fingerprint
    assert result.new_fingerprint == preview.candidate_fingerprint
    assert result.differing_fields == ("model_ids",)
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "active"
    )
    assert accepted_pool.enabled is True
    assert accepted_pool.routing_generation == generation + 1
    assert accepted_pool.capability_fingerprint == capability_fingerprint(
        backend.health_meta["capabilities"]
    )


@pytest.mark.asyncio
async def test_capability_drift_accept_rejects_stale_review(
    db_session,
) -> None:
    backend, pool, _member = await _create_disabled_drift(db_session)
    generation = pool.routing_generation

    with pytest.raises(MLBackendCapabilityRevalidationError) as caught:
        await MLBackendService(db_session).accept_capability_drift(
            pool.id,
            backend.id,
            expected_candidate_fingerprint="0" * 64,
            enable_pool=True,
        )

    assert caught.value.error_code == "ml_backend_capability_changed_during_review"
    fresh_pool = await MLBackendService(db_session).get_pool(pool.id)
    assert fresh_pool is not None
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "disabled"
    )
    assert fresh_pool.enabled is False
    assert fresh_pool.routing_generation == generation


@pytest.mark.asyncio
async def test_capability_drift_accept_can_keep_pool_disabled(db_session) -> None:
    backend, pool, _member = await _create_disabled_drift(db_session)
    preview = await MLBackendService(db_session).preview_capability_drift(
        pool.id, backend.id
    )
    assert preview is not None

    accepted = await MLBackendService(db_session).accept_capability_drift(
        pool.id,
        backend.id,
        expected_candidate_fingerprint=preview.candidate_fingerprint,
        enable_pool=False,
    )
    assert accepted is not None
    accepted_pool, _result = accepted
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "active"
    )
    assert accepted_pool.enabled is False


@pytest.mark.asyncio
async def test_disabled_member_can_revalidate_after_capability_rolls_back(
    db_session,
) -> None:
    backend, pool, _member = await _create_disabled_drift(db_session)
    backend.health_meta = _caps("sam3")
    generation = pool.routing_generation

    preview = await MLBackendService(db_session).preview_capability_drift(
        pool.id, backend.id
    )
    assert preview is not None
    assert preview.has_drift is False
    assert preview.can_accept is True
    assert preview.differing_fields == ()

    accepted = await MLBackendService(db_session).accept_capability_drift(
        pool.id,
        backend.id,
        expected_candidate_fingerprint=preview.candidate_fingerprint,
        enable_pool=True,
    )
    assert accepted is not None
    accepted_pool, result = accepted
    assert result.old_fingerprint == result.new_fingerprint
    assert result.differing_fields == ()
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "active"
    )
    assert accepted_pool.enabled is True
    assert accepted_pool.routing_generation == generation + 1


@pytest.mark.asyncio
async def test_capability_drift_accept_rejects_incompatible_active_member(
    db_session,
) -> None:
    from app.db.models.ml_backend_registry import MLBackendRegistry

    backend, pool, _member = await _create_disabled_drift(db_session)
    peer = MLBackendRegistry(
        name="old-contract-peer",
        url=f"http://old-contract-{uuid.uuid4().hex[:6]}:9",
        state="connected",
        health_meta=_caps("sam3"),
        source="manual",
    )
    db_session.add(peer)
    await db_session.flush()
    peer_member = MLBackendPoolMember(
        pool_id=pool.id,
        registry_id=peer.id,
        traffic_state="active",
        weight=1,
    )
    db_session.add(peer_member)
    await db_session.flush()

    preview = await MLBackendService(db_session).preview_capability_drift(
        pool.id, backend.id
    )
    assert preview is not None
    assert preview.can_accept is False
    assert preview.blocking_member_ids == (peer.id,)

    with pytest.raises(MLBackendCapabilityRevalidationError) as caught:
        await MLBackendService(db_session).accept_capability_drift(
            pool.id,
            backend.id,
            expected_candidate_fingerprint=preview.candidate_fingerprint,
            enable_pool=True,
        )
    assert caught.value.error_code == "ml_backend_pool_capability_blocked_by_members"
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "disabled"
    )

    peer_member.traffic_state = "disabled"
    await db_session.flush()
    preview = await MLBackendService(db_session).preview_capability_drift(
        pool.id, backend.id
    )
    assert preview is not None
    assert preview.can_accept is True
    accepted = await MLBackendService(db_session).accept_capability_drift(
        pool.id,
        backend.id,
        expected_candidate_fingerprint=preview.candidate_fingerprint,
        enable_pool=True,
    )
    assert accepted is not None
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "active"
    )
    assert (await _pool_member(db_session, pool.id, peer.id)).traffic_state == (
        "disabled"
    )


@pytest.mark.asyncio
async def test_capability_drift_api_accepts_after_fresh_probe_and_audits(
    httpx_client, db_session, super_admin, monkeypatch
) -> None:
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    backend, pool, _ = await _create_disabled_drift(db_session)
    await db_session.commit()

    async def healthy(_self, registry_id, **_kwargs):
        assert registry_id == backend.id
        return True

    monkeypatch.setattr(BackendControlService, "check_health", healthy)
    preview_response = await httpx_client.get(
        f"/api/v1/admin/ml-integrations/service-pools/{pool.id}/members/{backend.id}/capability-drift",
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["can_accept"] is True
    assert preview["differing_fields"] == ["model_ids"]

    response = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/service-pools/{pool.id}/members/{backend.id}/capability-drift/accept",
        json={
            "expected_candidate_fingerprint": preview["candidate_fingerprint"],
            "enable_pool": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["members"][0]["traffic_state"] == "active"

    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "ml_service_pool.capability_drift_accepted",
            AuditLog.target_id == str(pool.id),
        )
    )
    assert audit is not None
    assert audit.detail_json["registry_id"] == str(backend.id)
    assert audit.detail_json["new_fingerprint"] == preview["candidate_fingerprint"]
    assert audit.detail_json["differing_fields"] == ["model_ids"]


@pytest.mark.asyncio
async def test_capability_drift_api_rejects_failed_fresh_probe(
    httpx_client, db_session, super_admin, monkeypatch
) -> None:
    _, token = super_admin
    backend, pool, _member = await _create_disabled_drift(db_session)
    preview = await MLBackendService(db_session).preview_capability_drift(
        pool.id, backend.id
    )
    await db_session.commit()
    assert preview is not None

    async def unhealthy(_self, _registry_id, **_kwargs):
        return False

    monkeypatch.setattr(BackendControlService, "check_health", unhealthy)
    response = await httpx_client.post(
        f"/api/v1/admin/ml-integrations/service-pools/{pool.id}/members/{backend.id}/capability-drift/accept",
        json={
            "expected_candidate_fingerprint": preview.candidate_fingerprint,
            "enable_pool": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert (
        response.json()["detail"]["error_code"]
        == "ml_backend_capability_revalidation_failed"
    )
    fresh_pool = await MLBackendService(db_session).get_pool(pool.id)
    assert fresh_pool is not None
    assert (await _pool_member(db_session, pool.id, backend.id)).traffic_state == (
        "disabled"
    )
    assert fresh_pool.enabled is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verified disposable module-data routing LOGIN",
)
async def test_remove_managed_member_requires_real_drain(
    test_engine, httpx_client, super_admin, monkeypatch
) -> None:
    _, token = super_admin
    owner = await _connect(test_engine)
    world = await _seed(owner)
    try:
        monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
        async with _routing_owner_http(test_engine, monkeypatch):
            response = await httpx_client.delete(
                f"/api/v1/admin/ml-integrations/service-pools/{world['pool_id']}"
                f"/members/{world['registry_id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "ml_backend_member_draining"
    finally:
        await _cleanup(owner, world)
        await owner.close()


@pytest.mark.asyncio
async def test_remove_fails_closed_when_routing_owner_is_unavailable(
    httpx_client, db_session, super_admin, monkeypatch
) -> None:
    from app.services.ml_routing import safety

    class UnavailableAuthority:
        async def quiescence(self, _registry_id):
            raise ConnectionError("routing owner unavailable")

    _, token = super_admin
    backend, pool = await create_registry_with_pool(
        db_session,
        name="remove-ledger-guard",
        state="connected",
        health_meta=_caps(),
    )
    member = await db_session.scalar(
        select(MLBackendPoolMember).where(MLBackendPoolMember.registry_id == backend.id)
    )
    member.traffic_state = "draining"
    await db_session.commit()
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(safety, "RoutingAuthority", UnavailableAuthority)

    response = await httpx_client.delete(
        f"/api/v1/admin/ml-integrations/service-pools/{pool.id}/members/{backend.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "ml_backend_router_unavailable"
