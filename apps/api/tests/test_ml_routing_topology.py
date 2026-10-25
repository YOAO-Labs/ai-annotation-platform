"""v0.23.3 ADR-0050 §12.3 + v0.23.4 Appendix A.7 · topology + runtime-snapshot tests.

Covers:
* role-scoped topology (Super Admin full detail vs Project Admin server-trimmed);
* derived pool ``routable_instances`` / ``status`` / ``status_reason_codes``;
* runtime-snapshot freshness envelope (``sources`` / ``partial`` / ``partial_reason``);
* runtime-snapshot Super-Admin-only gating.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import settings
from tests.conftest import create_registry_with_pool


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
    from aap_inference.pool_capability import (
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
    assert body["router_mode"] == "off"  # safe default in tests
    pool_entry = next(p for p in body["pools"] if p["name"] == "bk")
    assert pool_entry["member_count"] == 1
    # Super Admin sees the real routing_policy + derived fields.
    assert pool_entry["routing_policy"] == "smooth_weighted_round_robin"
    assert pool_entry["routable_instances"] == 1
    assert pool_entry["status"] in {"healthy", "degraded"}
    assert isinstance(pool_entry["status_reason_codes"], list)
    member = pool_entry["members"][0]
    # Super Admin sees health + GPU + weight (not projected away).
    assert member["state"] is not None
    assert member["weight"] == 1
    assert (
        "last_checked_at" in member
    )  # key present (value may be None if never probed)
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
        # Project Admin: routing_policy projected to "unknown".
        assert pool_entry["routing_policy"] == "unknown"
        # Project Admin still sees derived availability (display hint).
        assert "routable_instances" in pool_entry
        assert "status" in pool_entry
        assert pool_entry["status_reason_codes"] == []
        if pool_entry["members"]:
            member = pool_entry["members"][0]
            # Server-side projection: keys present, values None.
            assert member["state"] is None
            assert member["weight"] is None
            assert member["gpu_resource_id"] is None
            assert member["last_checked_at"] is None
            # Identity + traffic_state remain (these are not sensitive).
            assert member["traffic_state"] == "active"


@pytest.mark.asyncio
async def test_topology_status_derivation(
    httpx_client, db_session, super_admin
) -> None:
    """Pool status derivation: all disabled → offline; mixed → degraded with reason codes."""
    from app.db.models.ml_backend_pool import MLBackendPoolMember
    from app.db.models.ml_backend_registry import MLBackendRegistry

    _, token = super_admin

    # Pool A: members flipped to disabled → offline.
    _, pool_a = await create_registry_with_pool(db_session, name="pool-a")
    await db_session.flush()
    # The helper creates one active member; flip it to disabled.
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

    # Pool B: 2 members, 1 active + 1 draining → degraded.
    caps = {"models": [{"id": "det", "task": "detection"}]}
    _, pool_b = await create_registry_with_pool(
        db_session,
        name="pool-b",
        enabled_pool=True,
        health_meta={"capabilities": caps},
        last_checked_at=datetime.now(UTC),
    )
    from aap_inference.pool_capability import (
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
    # Add a second active+connected member to pool B.
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
    # Pool A: all disabled → offline + 2_disabled reason.
    assert by_name["pool-a"]["status"] == "offline"
    assert by_name["pool-a"]["routable_instances"] == 0
    assert any("disabled" in r for r in by_name["pool-a"]["status_reason_codes"])
    # Pool B: 1 active + 1 draining → degraded + 1_draining reason + routable=1.
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
    # Super Admin OK.
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
    # Freshness envelope (plan §6.3 / Appendix A.2).
    assert "sources" in body
    assert "partial" in body
    source_names = [s["name"] for s in body["sources"]]
    # All five sources present.
    assert set(source_names) == {
        "topology",
        "router_ledger",
        "health",
        "gpu",
        "residency",
    }
    # gpu + residency honestly marked stale (not bundled in v0.23.3 snapshot).
    gpu_src = next(s for s in body["sources"] if s["name"] == "gpu")
    assert gpu_src["stale"] is True
    assert gpu_src["error"] == "not_bundled_in_v0_23_3"
    residency_src = next(s for s in body["sources"] if s["name"] == "residency")
    assert residency_src["stale"] is True
    # Since gpu/residency are always stale, partial must be True with a reason.
    assert body["partial"] is True
    assert body["partial_reason"] is not None
    # Metrics-driven member fields present but None (plan §4.2).
    if body["pools"] and body["pools"][0]["members"]:
        m = body["pools"][0]["members"][0]
        assert m["last_selected_at"] is None
        assert m["selection_count_window"] is None
        assert m["rejection_count_window"] is None
        assert m["p95_ms"] is None
        assert m["error_rate"] is None
    # Project Admin forbidden.
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
    from aap_inference_contract.routing import RoutingUnavailable

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
