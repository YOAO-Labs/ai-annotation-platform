"""Topology and runtime-snapshot tests (ADR-0050 §12.3).

Covers:
* role-scoped topology (Super Admin full detail vs Project Admin server-trimmed);
* derived pool ``routable_instances`` / ``status`` / ``status_reason_codes``;
* runtime-snapshot freshness envelope (``sources`` / ``partial`` / ``partial_reason``);
* runtime-snapshot Super-Admin-only gating.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from tests.conftest import create_registry_with_pool


_PROJECT_COUNTS_URL = "/api/v1/admin/ml-integrations/primary-pool-project-counts"


@pytest.mark.asyncio
async def test_primary_pool_counts_are_authoritative_and_visibility_scoped(
    httpx_client, db_session, super_admin, project_admin, annotator
) -> None:
    admin, admin_token = super_admin
    manager, manager_token = project_admin
    _, employee_token = annotator
    _, populated_pool = await create_registry_with_pool(db_session, name="bound")
    _, enabled_only_pool = await create_registry_with_pool(
        db_session, name="enabled-only"
    )
    empty_pool = MLBackendServicePool(name="memberless")
    db_session.add(empty_pool)
    await db_session.flush()

    def project(owner, pool=None):
        return Project(
            display_id=f"P-PC-{uuid4().hex[:8]}",
            name="Pool count test",
            type_label="image-det",
            type_key="image-det",
            owner_id=owner.id,
            ml_backend_pool_id=pool.id if pool else None,
        )

    owned = project(manager, empty_pool)
    member = project(admin, populated_pool)
    hidden = project(admin, populated_pool)
    unbound = project(manager)
    db_session.add_all([owned, member, hidden, unbound])
    await db_session.flush()
    db_session.add_all(
        [
            ProjectMember(
                project_id=member.id,
                user_id=manager.id,
                role="viewer",
                assigned_by=admin.id,
            ),
            ProjectMLBackendPool(
                project_id=unbound.id, pool_id=enabled_only_pool.id, enabled=True
            ),
        ]
    )
    await db_session.flush()

    async def read_counts(token):
        return await httpx_client.get(
            _PROJECT_COUNTS_URL, headers={"Authorization": f"Bearer {token}"}
        )

    all_counts = await read_counts(admin_token)
    assert all_counts.status_code == 200, all_counts.text
    assert all_counts.json() == {
        "counts": {str(populated_pool.id): 2, str(empty_pool.id): 1}
    }
    visible_counts = await read_counts(manager_token)
    assert visible_counts.status_code == 200, visible_counts.text
    assert visible_counts.json() == {
        "counts": {str(populated_pool.id): 1, str(empty_pool.id): 1}
    }
    unauthorized = await read_counts(employee_token)
    assert unauthorized.status_code == 403, unauthorized.text
    overview = await httpx_client.get(
        "/api/v1/admin/ml-integrations/overview",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert overview.status_code == 403, overview.text


@pytest.mark.asyncio
async def test_primary_pool_count_read_error_is_not_an_empty_success(
    httpx_client, auth_headers, monkeypatch
) -> None:
    from app.api.v1 import admin_ml_integrations

    async def unavailable(*_args):
        raise SQLAlchemyError("Workspace unavailable")

    monkeypatch.setattr(
        admin_ml_integrations, "count_visible_projects_by_primary_pool", unavailable
    )
    response = await httpx_client.get(_PROJECT_COUNTS_URL, headers=auth_headers)
    assert response.status_code == 503, response.text
    assert "counts" not in response.json()


@pytest.mark.asyncio
async def test_topology_super_admin_sees_full_detail(
    httpx_client, db_session, super_admin
) -> None:
    """Super Admin topology includes health / GPU / state per member + routing_policy."""
    _, token = super_admin
    caps = {"models": [{"id": "det", "task": "detection"}]}
    backend, pool = await create_registry_with_pool(
        db_session,
        name="bk",
        enabled_pool=True,
        health_meta={"capabilities": caps},
        last_checked_at=datetime.now(UTC),
    )
    from yoao_inference.pool_capability import (
        canonicalize_capability,
        capability_fingerprint,
    )

    pool.capability_snapshot = canonicalize_capability(caps)
    pool.capability_fingerprint = capability_fingerprint(caps)
    await db_session.flush()
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/topology",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "topology.v1"
    assert body["router_mode"] == "off"
    pool_entry = next(p for p in body["pools"] if p["name"] == "bk")
    assert pool_entry["member_count"] == 1
    assert pool_entry["routing_policy"] == "smooth_weighted_round_robin"
    assert pool_entry["routable_instances"] == 1
    assert pool_entry["status"] in {"healthy", "degraded"}
    assert isinstance(pool_entry["status_reason_codes"], list)
    member = pool_entry["members"][0]
    assert member["state"] is not None
    assert member["weight"] == 1
    assert "last_checked_at" in member
    assert "gpu_resource_id" in member


@pytest.mark.asyncio
async def test_topology_project_admin_trimmed(
    httpx_client, db_session, project_admin
) -> None:
    """Project Admin topology: server-side projection nulls routing_policy/weight/state/gpu.

    Plan Appendix A.6: role projection must happen server-side, not by frontend
    hiding. The keys remain (so the generated TS type is uniform) but values are None.
    """
    _, token = project_admin
    await create_registry_with_pool(db_session, name="bk")
    await db_session.flush()
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/topology",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "topology.v1"
    if body["pools"]:
        pool_entry = body["pools"][0]
        assert pool_entry["routing_policy"] == "unknown"
        assert "routable_instances" in pool_entry
        assert "status" in pool_entry
        assert pool_entry["status_reason_codes"] == []
        if pool_entry["members"]:
            member = pool_entry["members"][0]
            assert member["state"] is None
            assert member["weight"] is None
            assert member["gpu_resource_id"] is None
            assert member["last_checked_at"] is None
            assert member["traffic_state"] == "active"


@pytest.mark.asyncio
async def test_topology_status_derivation(
    httpx_client, db_session, super_admin
) -> None:
    """Pool status derivation: all disabled → offline; mixed → degraded with reason codes."""
    from app.db.models.ml_backend_pool import MLBackendPoolMember
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin

    _, pool_a = await create_registry_with_pool(db_session, name="pool-a")
    await db_session.flush()
    members_a = (
        (
            await db_session.execute(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id == pool_a.id
                )
            )
        )
        .scalars()
        .all()
    )
    for m in members_a:
        m.traffic_state = "disabled"

    caps = {"models": [{"id": "det", "task": "detection"}]}
    _, pool_b = await create_registry_with_pool(
        db_session,
        name="pool-b",
        enabled_pool=True,
        health_meta={"capabilities": caps},
        last_checked_at=datetime.now(UTC),
    )
    from yoao_inference.pool_capability import (
        canonicalize_capability,
        capability_fingerprint,
    )

    pool_b.capability_snapshot = canonicalize_capability(caps)
    pool_b.capability_fingerprint = capability_fingerprint(caps)
    await db_session.flush()
    members_b = (
        (
            await db_session.execute(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id == pool_b.id
                )
            )
        )
        .scalars()
        .all()
    )
    if members_b:
        members_b[0].traffic_state = "draining"
    reg_b2 = MLBackendRegistry(
        name="pool-b-inst2",
        url="http://pool-b-inst2.test:9999",
        state="connected",
        is_interactive=True,
        source="manual",
        health_meta={"capabilities": caps},
        last_checked_at=datetime.now(UTC),
    )
    db_session.add(reg_b2)
    await db_session.flush()
    db_session.add(
        MLBackendPoolMember(
            pool_id=pool_b.id, registry_id=reg_b2.id, traffic_state="active", weight=1
        )
    )
    await db_session.flush()

    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/topology",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {p["name"]: p for p in body["pools"]}
    assert by_name["pool-a"]["status"] == "offline"
    assert by_name["pool-a"]["routable_instances"] == 0
    assert any("disabled" in r for r in by_name["pool-a"]["status_reason_codes"])
    assert by_name["pool-b"]["status"] == "degraded"
    assert by_name["pool-b"]["routable_instances"] == 1
    assert any("draining" in r for r in by_name["pool-b"]["status_reason_codes"])


@pytest.mark.asyncio
async def test_runtime_snapshot_super_admin_only(
    httpx_client, db_session, super_admin, project_admin
) -> None:
    """runtime-snapshot: Super Admin 200 with freshness envelope, Project Admin 403."""
    await create_registry_with_pool(db_session, name="bk")
    await db_session.flush()
    _, sa_token = super_admin
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/runtime-snapshot",
        headers={"Authorization": f"Bearer {sa_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "runtime_snapshot.v1"
    assert "router_mode" in body
    assert "observed_at" in body
    assert "pools" in body
    assert "sources" in body
    assert "partial" in body
    source_names = [s["name"] for s in body["sources"]]
    assert set(source_names) == {
        "topology",
        "router_ledger",
        "health",
        "gpu",
        "residency",
    }
    gpu_src = next(s for s in body["sources"] if s["name"] == "gpu")
    assert gpu_src["stale"] is True
    assert gpu_src["error"] == "not_bundled_in_v0_23_3"
    residency_src = next(s for s in body["sources"] if s["name"] == "residency")
    assert residency_src["stale"] is True
    assert body["partial"] is True
    assert body["partial_reason"] is not None
    if body["pools"] and body["pools"][0]["members"]:
        m = body["pools"][0]["members"][0]
        assert m["last_selected_at"] is None
        assert m["selection_count_window"] is None
        assert m["rejection_count_window"] is None
        assert m["p95_ms"] is None
        assert m["error_rate"] is None
    _, pa_token = project_admin
    resp = await httpx_client.get(
        "/api/v1/admin/ml-integrations/runtime-snapshot",
        headers={"Authorization": f"Bearer {pa_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_runtime_snapshot_keeps_unknown_inflight_when_owner_unavailable(
    httpx_client, db_session, super_admin, monkeypatch
) -> None:
    from app.services.ml_routing import router
    from app.services.ml_routing.authority import RoutingAuthority
    from yoao_inference_contract.routing import RoutingUnavailable

    async def unavailable(_self, _registry_ids):
        raise RoutingUnavailable()

    class FailingLedger:
        async def healthcheck(self) -> None:
            raise ConnectionError("redis unavailable")

        async def aclose(self) -> None:
            return None

    _, token = super_admin
    await create_registry_with_pool(
        db_session,
        name="ledger-unavailable",
        enabled_pool=True,
        health_meta={"capabilities": {"models": [{"id": "det"}]}},
        last_checked_at=datetime.now(UTC),
    )
    await db_session.flush()
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(router, "make_ledger_from_settings", FailingLedger)
    monkeypatch.setattr(RoutingAuthority, "runtime_snapshot", unavailable)

    response = await httpx_client.get(
        "/api/v1/admin/ml-integrations/runtime-snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    ledger_source = next(
        source for source in body["sources"] if source["name"] == "router_ledger"
    )
    assert ledger_source["stale"] is True
    assert ledger_source["error"] == "routing_owner_unavailable"
    member = next(
        pool["members"][0]
        for pool in body["pools"]
        if pool["name"] == "ledger-unavailable"
    )
    assert member["route_inflight"] is None
    assert member["circuit_open"] is None
