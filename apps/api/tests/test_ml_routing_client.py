"""Prediction client wiring for service-pool routing."""

from __future__ import annotations

import uuid

import pytest

from app.db.models.ml_backend_registry import (
    MLBackendRegistry,
    ProjectMLBackendPool,
)
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.ml_routing.contracts import (
    RouteLease,
    RouteOutcome,
    RouteSelection,
    RouterMode,
    RoutingError,
)
from app.services.ml_routing.router import MLBackendRouter
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", ["default", "allow", "reject"])
async def test_enforce_prediction_uses_router_selected_instance(
    db_session, super_admin, monkeypatch, dispatch
) -> None:
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    requested, pool = await create_registry_with_pool(
        db_session, name="requested", enabled_pool=True
    )
    selected = MLBackendRegistry(
        name="selected",
        url=f"http://selected-{uuid.uuid4().hex}.test:9999",
        state="connected",
        source="manual",
    )
    db_session.add(selected)
    await db_session.flush()

    # 让第二个物理实例成为同一逻辑池成员。
    from app.db.models.ml_backend_pool import MLBackendPoolMember

    db_session.add(
        MLBackendPoolMember(
            pool_id=pool.id,
            registry_id=selected.id,
            traffic_state="active",
            weight=1,
        )
    )
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()

    lease = RouteLease(
        lease_id="lease-1",
        pool_id=pool.id,
        instance_id=selected.id,
        owner="test",
        operation="predict",
        generation=pool.routing_generation,
        expires_at_ms=9999999999999,
    )
    finished: list[tuple[uuid.UUID, RouteOutcome]] = []

    async def fake_acquire(_self, _pool_id, **_kwargs):
        return RouteSelection(
            lease=lease,
            instance_id=selected.id,
            rejection=None,
        )

    async def fake_finish(_self, route_lease, outcome, duration_ms):
        finished.append((route_lease.instance_id, outcome))
        return True

    monkeypatch.setattr(MLBackendRouter, "acquire", fake_acquire)
    monkeypatch.setattr(MLBackendRouter, "finish", fake_finish)

    used: list[uuid.UUID] = []
    phases: list[str] = []

    class FakeTransport:
        def __init__(self, backend, **_kwargs):
            self.backend_id = backend.id
            used.append(backend.id)

        async def predict(self, tasks, context=None):
            phases.append("predict")
            return [{"tasks": tasks, "context": context}]

    monkeypatch.setattr(
        "app.services.ml_client.MLBackendClient", FakeTransport, raising=True
    )

    class FakeLedger:
        closed = False

        async def cancel(self, route_lease):
            finished.append((route_lease.instance_id, RouteOutcome.CANCEL))
            return True

        async def aclose(self):
            self.closed = True

    ledger = FakeLedger()
    client = RoutedMLBackendClient(
        db_session,
        requested,
        project_id=project.id,
        owner="test",
        operation="predict",
        mode=RouterMode.ENFORCE,
        ledger_factory=lambda: ledger,
    )

    async def before_dispatch(transport):
        assert transport.backend_id == selected.id
        phases.append("check")
        if dispatch == "reject":
            raise ValueError("request authority changed during selection")

    if dispatch == "reject":
        with pytest.raises(ValueError, match="request authority changed"):
            await client.predict([{"id": "task-1"}], before_dispatch=before_dispatch)
    elif dispatch == "allow":
        await client.predict([{"id": "task-1"}], before_dispatch=before_dispatch)
    else:
        await client.predict([{"id": "task-1"}])

    assert used == [selected.id]
    assert client.pool_id == pool.id
    assert client.last_instance_id == selected.id
    assert (
        phases
        == {
            "default": ["predict"],
            "allow": ["check", "predict"],
            "reject": ["check"],
        }[dispatch]
    )
    assert finished == [
        (
            selected.id,
            RouteOutcome.CANCEL if dispatch == "reject" else RouteOutcome.SUCCESS,
        )
    ]
    assert ledger.closed is True


@pytest.mark.asyncio
async def test_enforce_prediction_without_pool_fails_closed(
    db_session, super_admin
) -> None:
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id)
    from app.db.models.ml_backend_registry import MLBackendRegistry

    backend = MLBackendRegistry(
        name="orphan",
        url=f"http://orphan-{uuid.uuid4().hex}.test:9999",
        state="connected",
        source="manual",
    )
    db_session.add(backend)
    await db_session.flush()

    client = RoutedMLBackendClient(
        db_session,
        backend,
        project_id=project.id,
        owner="test",
        operation="predict",
        mode=RouterMode.ENFORCE,
    )

    with pytest.raises(RoutingError) as exc_info:
        await client.predict([{"id": "task-1"}])
    assert exc_info.value.reason.value == "ml_backend_pool_unavailable"
