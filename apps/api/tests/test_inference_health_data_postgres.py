"""Real function-only LOGIN executes portable health in the caller's transaction."""

from __future__ import annotations

import os
from types import SimpleNamespace

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from aap_inference.backend_transport import BackendTransport
from aap_inference.health import BackendHealthCoordinator
from aap_inference.health_data_postgres import (
    HealthDataPostgresData,
    HealthDataUnavailable,
)
from aap_inference_contract.capabilities import CapabilityLimits
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_health_data_native import (
    _connect,
    _seed_world,
    _cleanup_world,
)
from tests.test_inference_health_legacy import _backend
from tests.test_module_data_roles import _role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _client(test_engine):
    url = make_url(
        _role_url(
            "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL", "inference_health_data_client"
        )
    )
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        timeout=5,
        command_timeout=10,
    )


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("commit", [False, True])
async def test_portable_coordinator_function_login_keeps_outer_commit_and_gpu_proof(
    test_engine, gpu, commit
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed_world(owner)
    client = await _client(test_engine)
    tx = None
    trace = []
    try:
        assert (
            await client.fetchval("SELECT current_user")
            == "inference_health_data_client"
        )
        with pytest.raises(HealthDataUnavailable):
            await HealthDataPostgresData.bind(client)
        with _backend("gpu_echo" if gpu else "cpu_healthy", trace) as origin:
            await owner.execute(
                "UPDATE ml_backend_registry SET url=$2,gpu_resource_id=$3,vram_budget_mb=$4 WHERE id=$1",
                world["registry_id"],
                origin,
                world["gpu_resource_id"] if gpu else None,
                1024 if gpu else None,
            )
            tx = client.transaction()
            await tx.start()
            data = await HealthDataPostgresData.bind(client)

            def make_transport(row):
                return BackendTransport(
                    row,
                    options=SimpleNamespace(ml_health_timeout=2),
                    dispatch_context=None,
                    observe=None,
                )

            coordinator = BackendHealthCoordinator(
                data,
                client_factory=make_transport,
                capability_limits_factory=CapabilityLimits,
            )
            assert await coordinator.check_health(world["registry_id"])
            assert client.is_in_transaction()
            backend = await data.get_backend(world["registry_id"])
            assert backend.state == "connected"
            assert isinstance(backend.health_meta, dict)
            assert ("gpu_arbiter_probe" in backend.health_meta) is gpu
            if gpu:
                proof = backend.health_meta["gpu_arbiter_probe"]
                assert (
                    proof["membership_epoch"] == "1"
                    and proof["membership_state"] == "pending"
                )
                assert proof[
                    "observed_at"
                ] == backend.last_checked_at.isoformat().replace("+00:00", "Z")
            assert (
                await data.load_pool(world["pool_id"], registry_id=world["registry_id"])
            ).capability_fingerprint is not None
            assert (
                await owner.fetchval(
                    "SELECT last_checked_at FROM ml_backend_registry WHERE id=$1",
                    world["registry_id"],
                )
                is None
            )
            if commit:
                await tx.commit()
            else:
                await tx.rollback()
            tx = None
            with pytest.raises(HealthDataUnavailable):
                await data.clock_timestamp()
            replacement = client.transaction()
            await replacement.start()
            try:
                with pytest.raises(HealthDataUnavailable):
                    await data.get_backend(world["registry_id"])
                fresh = await HealthDataPostgresData.bind(client)
                assert await fresh.get_backend(world["registry_id"]) is not None
            finally:
                await replacement.rollback()
            assert (
                await owner.fetchval(
                    "SELECT last_checked_at FROM ml_backend_registry WHERE id=$1",
                    world["registry_id"],
                )
                is not None
            ) is commit
            generation = await owner.fetchval(
                "SELECT routing_generation FROM ml_backend_service_pools WHERE id=$1",
                world["pool_id"],
            )
            assert generation == (2 if commit else 1)
            assert trace == ["http:health", "http:setup"]
    finally:
        if tx is not None:
            await tx.rollback()
        await client.close()
        try:
            await _cleanup_world(owner, world)
        finally:
            await owner.close()


async def test_cached_port_does_not_skip_full_jsonb_materialization(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed_world(owner)
    client = await _client(test_engine)
    tx = client.transaction()
    try:
        await tx.start()
        data = await HealthDataPostgresData.bind(client)
        row = await data.get_backend(world["registry_id"])
        row.url = "http://caller-pending.invalid"
        await owner.execute(
            "UPDATE ml_backend_registry SET health_meta=$2::jsonb WHERE id=$1",
            world["registry_id"],
            '{"n":' + "9" * 5000 + "}",
        )
        with pytest.raises(ValueError):
            await data.get_backend(world["registry_id"])
        assert row.url == "http://caller-pending.invalid"
    finally:
        await tx.rollback()
        await client.close()
        try:
            await _cleanup_world(owner, world)
        finally:
            await owner.close()
