import json
from uuid import UUID, uuid4

import httpx
import pytest

from yoao.errors import ConflictError
from yoao.models import (
    CapabilityDrift,
    ServicePool,
    ServicePoolRuntimeSnapshot,
    ServicePoolTopology,
)

from .conftest import API

POOL_ID = str(uuid4())
REGISTRY_ID = str(uuid4())


def _pool(**extra) -> dict:
    return {
        "id": POOL_ID,
        "name": "sam-pool",
        "enabled": True,
        "routing_policy": "weighted",
        "legacy_instance_id": REGISTRY_ID,
        "routing_generation": 2,
        "capability_fingerprint": "a" * 64,
        "members": [
            {
                "registry_id": REGISTRY_ID,
                "registry_name": "sam-1",
                "traffic_state": "active",
                "weight": 1,
            }
        ],
        "created_at": "2026-08-14T00:00:00Z",
        "updated_at": "2026-08-14T00:00:00Z",
        **extra,
    }


def test_service_pool_crud(client, respx_mock):
    respx_mock.get(f"{API}/admin/ml-integrations/service-pools").mock(
        return_value=httpx.Response(200, json=[_pool()])
    )
    assert isinstance(client.service_pools.list()[0], ServicePool)

    respx_mock.get(f"{API}/admin/ml-integrations/service-pools/{POOL_ID}").mock(
        return_value=httpx.Response(200, json=_pool())
    )
    assert client.service_pools.get(POOL_ID).name == "sam-pool"

    create = respx_mock.post(f"{API}/admin/ml-integrations/service-pools").mock(
        return_value=httpx.Response(201, json=_pool())
    )
    client.service_pools.create("sam-pool", legacy_instance_id=REGISTRY_ID)
    assert json.loads(create.calls.last.request.content) == {
        "name": "sam-pool",
        "legacy_instance_id": REGISTRY_ID,
    }

    update = respx_mock.patch(
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}"
    ).mock(return_value=httpx.Response(200, json=_pool(name="renamed")))
    assert client.service_pools.update(POOL_ID, name="renamed").name == "renamed"
    assert json.loads(update.calls.last.request.content) == {"name": "renamed"}

    delete = respx_mock.delete(
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}"
    ).mock(return_value=httpx.Response(204))
    assert client.service_pools.delete(POOL_ID) is None
    assert delete.called


def test_service_pool_member_lifecycle(client, respx_mock):
    member_url = (
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}"
    )
    add = respx_mock.put(member_url).mock(
        return_value=httpx.Response(200, json=_pool())
    )
    assert client.service_pools.add_member(POOL_ID, REGISTRY_ID, weight=3).members[
        0
    ].registry_id == UUID(REGISTRY_ID)
    assert json.loads(add.calls.last.request.content) == {"weight": 3}

    drain = respx_mock.post(f"{member_url}/drain").mock(
        return_value=httpx.Response(
            200,
            json=_pool(
                members=[
                    {
                        "registry_id": REGISTRY_ID,
                        "registry_name": "sam-1",
                        "traffic_state": "draining",
                        "weight": 3,
                    }
                ]
            ),
        )
    )
    assert (
        client.service_pools.drain_member(POOL_ID, REGISTRY_ID).members[0].traffic_state
        == "draining"
    )
    assert drain.called

    resume = respx_mock.post(f"{member_url}/resume").mock(
        return_value=httpx.Response(200, json=_pool())
    )
    assert (
        client.service_pools.resume_member(POOL_ID, REGISTRY_ID)
        .members[0]
        .traffic_state
        == "active"
    )
    assert resume.called

    remove = respx_mock.delete(member_url).mock(
        return_value=httpx.Response(200, json=_pool(members=[]))
    )
    assert client.service_pools.remove_member(POOL_ID, REGISTRY_ID).members == []
    assert remove.called


def test_capability_drift_preview_and_accept(client, respx_mock):
    preview_url = (
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}/members/"
        f"{REGISTRY_ID}/capability-drift"
    )
    candidate = "b" * 64
    respx_mock.get(preview_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "pool_id": POOL_ID,
                "registry_id": REGISTRY_ID,
                "member_state": "draining",
                "pool_enabled": False,
                "pool_fingerprint": "a" * 64,
                "candidate_fingerprint": candidate,
                "differing_fields": ["models"],
                "has_drift": True,
                "can_accept": True,
                "blocking_members": [],
            },
        )
    )
    preview = client.service_pools.preview_capability_drift(POOL_ID, REGISTRY_ID)
    assert isinstance(preview, CapabilityDrift)
    assert preview.candidate_fingerprint == candidate

    accept = respx_mock.post(f"{preview_url}/accept").mock(
        return_value=httpx.Response(200, json=_pool(capability_fingerprint=candidate))
    )
    pool = client.service_pools.accept_capability_drift(
        POOL_ID, REGISTRY_ID, candidate, enable_pool=False
    )
    assert pool.capability_fingerprint == candidate
    assert json.loads(accept.calls.last.request.content) == {
        "expected_candidate_fingerprint": candidate,
        "enable_pool": False,
    }


def test_topology_and_runtime_snapshot(client, respx_mock):
    respx_mock.get(f"{API}/admin/ml-integrations/topology").mock(
        return_value=httpx.Response(
            200,
            json={
                "schema_version": "topology.v1",
                "generated_at": "2026-08-14T00:00:00Z",
                "router_mode": "observe",
                "pools": [{"id": POOL_ID, "name": "sam-pool"}],
            },
        )
    )
    topology = client.service_pools.topology()
    assert isinstance(topology, ServicePoolTopology)
    assert topology.router_mode == "observe"

    respx_mock.get(f"{API}/admin/ml-integrations/runtime-snapshot").mock(
        return_value=httpx.Response(
            200,
            json={
                "schema_version": "runtime_snapshot.v1",
                "observed_at": "2026-08-14T00:00:00Z",
                "router_mode": "observe",
                "partial": True,
                "partial_reason": "router ledger stale",
                "sources": [],
                "pools": [],
            },
        )
    )
    snapshot = client.service_pools.runtime_snapshot()
    assert isinstance(snapshot, ServicePoolRuntimeSnapshot)
    assert snapshot.partial is True


@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        (
            "get",
            "/admin/ml-integrations/service-pools",
            lambda c: c.service_pools.list(),
        ),
        (
            "post",
            "/admin/ml-integrations/service-pools",
            lambda c: c.service_pools.create("x"),
        ),
        (
            "get",
            f"/admin/ml-integrations/service-pools/{POOL_ID}",
            lambda c: c.service_pools.get(POOL_ID),
        ),
        (
            "patch",
            f"/admin/ml-integrations/service-pools/{POOL_ID}",
            lambda c: c.service_pools.update(POOL_ID, name="x"),
        ),
        (
            "delete",
            f"/admin/ml-integrations/service-pools/{POOL_ID}",
            lambda c: c.service_pools.delete(POOL_ID),
        ),
        (
            "put",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}",
            lambda c: c.service_pools.add_member(POOL_ID, REGISTRY_ID),
        ),
        (
            "delete",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}",
            lambda c: c.service_pools.remove_member(POOL_ID, REGISTRY_ID),
        ),
        (
            "post",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}/drain",
            lambda c: c.service_pools.drain_member(POOL_ID, REGISTRY_ID),
        ),
        (
            "post",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}/resume",
            lambda c: c.service_pools.resume_member(POOL_ID, REGISTRY_ID),
        ),
        (
            "get",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}/capability-drift",
            lambda c: c.service_pools.preview_capability_drift(POOL_ID, REGISTRY_ID),
        ),
        (
            "post",
            f"/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}/capability-drift/accept",
            lambda c: c.service_pools.accept_capability_drift(
                POOL_ID, REGISTRY_ID, "a" * 64
            ),
        ),
        (
            "get",
            "/admin/ml-integrations/topology",
            lambda c: c.service_pools.topology(),
        ),
        (
            "get",
            "/admin/ml-integrations/runtime-snapshot",
            lambda c: c.service_pools.runtime_snapshot(),
        ),
    ],
    ids=(
        None,
        None,
        "get",
        "update",
        "delete",
        "add-member",
        "remove-member",
        "drain-member",
        "resume-member",
        "preview-drift",
        "accept-drift",
        None,
        None,
    ),
)
def test_service_pool_operations_map_conflict(client, respx_mock, method, path, call):
    getattr(respx_mock, method)(f"{API}{path}").mock(
        return_value=httpx.Response(409, json={"detail": "blocked"})
    )
    with pytest.raises(ConflictError):
        call(client)
