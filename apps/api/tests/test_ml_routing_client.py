"""Canonical model-execution gateway preserves route identity and settlement."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aap_inference_contract.backend_endpoint import (
    make_backend_endpoint_subject,
)
from aap_inference_contract.execution import FrozenModelOperation
from aap_inference_contract.routing import RoutingScopeView
from app.db.models.ml_backend_pool import MLBackendPoolMember
from app.db.models.ml_backend_registry import (
    MLBackendRegistry,
    ProjectMLBackendPool,
)
from app.services.ml_routing.contracts import (
    RouteLease,
    RouteOutcome,
    RouteSelection,
    RouterMode,
)
from app.services.ml_routing.router import MLBackendRouter
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


def _scope(pool, registry_id, project_id):
    return RoutingScopeView(
        id=uuid.uuid4(),
        pool_id=pool.id,
        registry_id=registry_id,
        project_id=project_id,
        mode="enforce",
        owner="api:test",
        operation="batch_predict",
        generation=pool.routing_generation,
        config_revision=1,
        eligibility_revision=1,
        authority_epoch=uuid.uuid4(),
        state="reserved",
        closed=False,
        heartbeat_deadline_ms=9_999_999_999_999,
        capability="a" * 64,
    )


def _request(project, requested, pool, pinned):
    return FrozenModelOperation(
        request_id=uuid.uuid4(),
        operation="batch_predict",
        task_inputs=({"id": "task-1", "file_path": "input/1"},),
        context=None,
        project_id=project.id,
        requested_registry_id=requested.id,
        requested_pool_id=pool.id,
        endpoint_registry_id=None,
        required_instance_id=pinned,
        route_owner="api:test",
        route_operation="batch_predict",
    )


async def _world(db_session, super_admin):
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
    member = MLBackendPoolMember(
        pool_id=pool.id,
        registry_id=selected.id,
        traffic_state="active",
        weight=1,
    )
    db_session.add(member)
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()
    return project, requested, pool, selected, member


def _gateway_fakes(monkeypatch, scope, *, fail=False):
    from app.services import inference_model_execution as gateway_module

    calls = {"acquire": [], "finish": [], "cancel": [], "execute": [], "closed": 0}
    lease = RouteLease(
        lease_id=str(scope.id),
        pool_id=scope.pool_id,
        instance_id=scope.registry_id,
        owner=scope.owner,
        operation=scope.operation,
        generation=scope.generation,
        expires_at_ms=scope.heartbeat_deadline_ms,
        scope=scope,
    )

    async def acquire(_router, pool_id=None, **kwargs):
        calls["acquire"].append((pool_id, kwargs))
        return RouteSelection(
            lease=lease, instance_id=scope.registry_id, rejection=None
        )

    async def finish(_router, route_lease, outcome, duration_ms):
        calls["finish"].append((route_lease, outcome, duration_ms))
        return True

    async def cancel(_router, route_lease):
        calls["cancel"].append(route_lease)
        return True

    class FakeLedger:
        async def aclose(self):
            calls["closed"] += 1

    class FakeExecutionClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, request):
            calls["execute"].append(request)
            if fail:
                raise RuntimeError("selected execution failed")
            return SimpleNamespace(observation=None, results=[{"task_id": "task-1"}])

        async def aclose(self):
            calls["closed"] += 1

    async def capture(_db, registry_id, *, request_id, purpose, binding=None):
        return make_backend_endpoint_subject(
            registry_id,
            request_id=request_id,
            purpose=purpose,
            binding=binding,
            subject="v1.YQ==",
        )

    monkeypatch.setattr(gateway_module, "_router_mode", lambda: RouterMode.ENFORCE)
    monkeypatch.setattr(gateway_module, "make_ledger_from_settings", FakeLedger)
    monkeypatch.setattr(MLBackendRouter, "acquire", acquire)
    monkeypatch.setattr(MLBackendRouter, "finish", finish)
    monkeypatch.setattr(MLBackendRouter, "cancel", cancel)
    monkeypatch.setattr(gateway_module, "ModelExecutionHttpClient", FakeExecutionClient)
    monkeypatch.setattr(
        "app.services.inference_backend_control.capture_backend_endpoint_subject",
        capture,
    )
    return calls


@pytest.mark.asyncio
async def test_model_gateway_pins_actual_selection_and_finishes_its_scope(
    db_session, super_admin, monkeypatch
):
    project, requested, pool, selected, member = await _world(db_session, super_admin)
    scope = _scope(pool, selected.id, project.id)
    calls = _gateway_fakes(monkeypatch, scope)
    from app.services.inference_model_execution import model_execution_port

    port = model_execution_port(db_session)
    assert not hasattr(port, "last_instance_id")
    result = await port.execute(_request(project, requested, pool, selected.id))

    assert result.results == [{"task_id": "task-1"}]
    pool_id, acquire = calls["acquire"][0]
    assert pool_id == pool.id
    assert acquire["registry_id"] == requested.id
    assert acquire["required_instance_id"] == selected.id
    bound = calls["execute"][0]
    assert bound.endpoint_registry_id == selected.id
    assert bound.selected_pool_id == pool.id
    assert bound.selected_member_id == member.id
    assert bound.topology_generation == pool.routing_generation
    assert bound.route_binding.scope_id == scope.id
    assert bound.endpoint_subject.registry_id == selected.id
    assert len(calls["finish"]) == 1
    assert calls["finish"][0][1] == RouteOutcome.SUCCESS
    assert calls["cancel"] == []
    assert calls["closed"] == 2


@pytest.mark.asyncio
async def test_model_gateway_cancels_route_when_owner_execution_fails(
    db_session, super_admin, monkeypatch
):
    project, requested, pool, selected, _member = await _world(db_session, super_admin)
    scope = _scope(pool, selected.id, project.id)
    calls = _gateway_fakes(monkeypatch, scope, fail=True)
    from app.services.inference_model_execution import model_execution_port

    with pytest.raises(RuntimeError, match="selected execution failed"):
        await model_execution_port(db_session).execute(
            _request(project, requested, pool, selected.id)
        )

    assert calls["finish"] == []
    assert len(calls["cancel"]) == 1
    assert calls["cancel"][0].scope.id == scope.id
    assert calls["closed"] == 2
