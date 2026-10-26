"""Actual diagnostic endpoint counts survive loss of optional Redis hints."""

import os
import socket
import uuid

import pytest

from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime
from app.config import settings
from app.services.ml_routing import router
from app.services.ml_routing.authority import RoutingAuthority
from app.services.ml_routing.ledger import RoutingLedger
from aap_inference_contract.routing import RoutingUnavailable
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import (
    _cleanup,
    _connect,
    _seed,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


class _UnavailableCircuit:
    async def healthcheck(self):
        raise ConnectionError("injected circuit store outage")

    async def aclose(self):
        pass


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_runtime_endpoint_retains_active_scope_after_redis_reset(
    test_engine, httpx_client, super_admin, monkeypatch, mode
):
    await assert_owned_test_database(test_engine)
    routing_dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert routing_dsn, "module-data verifier must provide the restricted routing LOGIN"
    owner = await _connect(test_engine)
    world = await _seed(owner)
    owner_runtime = RoutingRuntime(routing_dsn)
    assert await owner_runtime.readiness()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    token = f"routing-owner-{uuid.uuid4().hex}"
    owner_server = PipelineHttpServer(
        manager=None,
        data=None,
        control_token=None,
        host="127.0.0.1",
        port=port,
        routing_manager=owner_runtime.manager,
        routing_control_token=token,
    )
    await owner_server.start()
    for name, value in {
        "inference_routing_url": f"http://127.0.0.1:{port}",
        "inference_routing_token": token,
        "inference_routing_token_file": "",
        "ml_backend_router_mode": mode,
    }.items():
        monkeypatch.setattr(settings, name, value)
    before = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
            "FROM aap_inference_routing_v1.command_receipts"
        )
    )
    namespace = f"routing-diagnostics-test:{uuid.uuid4()}"
    ledger = RoutingLedger.from_url(settings.redis_url, namespace=namespace)

    def factory():
        return RoutingLedger.from_url(settings.redis_url, namespace=namespace)

    monkeypatch.setattr(router, "make_ledger_from_settings", factory)
    authority = RoutingAuthority()
    scope = None
    try:
        scope = await authority.acquire(
            registry_id=world["registry_id"], owner="diagnostics-acceptance"
        )
        key = (
            f"{namespace}:pool:{world['pool_id']}:member:{world['registry_id']}:leases"
        )
        await ledger._redis.zadd(key, {str(uuid.uuid4()): 9999999999999})
        headers = {"Authorization": f"Bearer {super_admin[1]}"}

        async def snapshot():
            response = await httpx_client.get(
                "/api/v1/admin/ml-integrations/runtime-snapshot", headers=headers
            )
            assert response.status_code == 200, response.text
            body = response.json()
            member = next(
                member
                for pool in body["pools"]
                for member in pool["members"]
                if member["registry_id"] == str(world["registry_id"])
            )
            source = next(
                item for item in body["sources"] if item["name"] == "router_ledger"
            )
            return member, source

        member, source = await snapshot()
        assert member["route_inflight"] == 1
        assert source["stale"] is False
        await ledger._redis.delete(key)
        assert await ledger._redis.zcard(key) == 0
        member, source = await snapshot()
        assert member["route_inflight"] == 1
        assert source["stale"] is False

        monkeypatch.setattr(router, "make_ledger_from_settings", _UnavailableCircuit)
        member, source = await snapshot()
        assert member["route_inflight"] == 1
        assert member["circuit_open"] is None
        assert source["stale"] is False

        async def unavailable(_self, _registry_ids):
            raise RoutingUnavailable()

        monkeypatch.setattr(RoutingAuthority, "runtime_snapshot", unavailable)
        member, source = await snapshot()
        assert member["route_inflight"] is None
        assert source["stale"] is True
        assert source["error"] == "routing_owner_unavailable"
    finally:
        try:
            if scope is not None:
                await authority.close(scope, "cancel")
            keys = [key async for key in ledger._redis.scan_iter(f"{namespace}:*")]
            if keys:
                await ledger._redis.delete(*keys)
            await ledger.aclose()
            after = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
                    "FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts "
                "WHERE id=ANY($1::uuid[])",
                list(after - before),
            )
            await _cleanup(owner, world)
        finally:
            try:
                await owner_server.close()
            finally:
                try:
                    await owner_runtime.close()
                finally:
                    await owner.close()
