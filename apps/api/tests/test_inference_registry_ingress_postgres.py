"""Run portable Registry ingress over a real function-only caller transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from aap_inference.registry_ingress import sync_environment
from aap_inference.registry_ingress_postgres import RegistryIngressPostgresData
from aap_inference.health_data_postgres import HealthDataUnavailable
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_registry_ingress_legacy import _server
from tests.test_inference_registry_ingress_native import (
    _connect,
    _cleanup_registries,
    _create_registry,
)
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
            "AAP_MODULE_DATA_INFERENCE_REGISTRY_INGRESS_URL",
            "inference_registry_ingress_client",
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


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_actual_http_ingress_keeps_original_transaction_and_minimal_receipt(
    test_engine,
    commit,
    autoflush,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _client(test_engine)
    request_id = uuid.uuid4()
    registry_ids = []
    trace = []
    binding = None
    try:
        assert (
            await client.fetchval("SELECT current_user")
            == "inference_registry_ingress_client"
        )
        with pytest.raises(HealthDataUnavailable):
            await RegistryIngressPostgresData.bind(client)
        with _server("new_ok", trace) as origin:
            command = make_registry_env_sync_request(
                [origin],
                timeout_seconds=2,
                capability_limits=CapabilityLimits(),
                request_id=request_id,
            )
            digest = registry_env_sync_command_sha256(command)

            @asynccontextmanager
            async def data_factory():
                nonlocal binding
                assert trace == ["http:/setup"]
                tx = client.transaction()
                await tx.start()
                try:
                    binding = await RegistryIngressPostgresData.bind(
                        client, autoflush=autoflush
                    )
                    yield binding
                    row = await binding.by_url(origin)
                    registry_ids.append(row.id)
                    assert row.state == "connected"
                    pool = await binding.singleton_pool(row.id)
                    assert pool.enabled is False and pool.capability_fingerprint is None
                    assert (await binding.sync_result(request_id, digest))[
                        "state"
                    ] == "caller_pending"
                    assert (
                        await owner.fetchval(
                            "SELECT count(*) FROM ml_backend_registry WHERE id=$1",
                            row.id,
                        )
                        == 0
                    )
                    assert (
                        await owner.fetchval(
                            "SELECT count(*) FROM aap_inference_registry_ingress_v1.env_sync_receipts WHERE request_id=$1",
                            request_id,
                        )
                        == 0
                    )
                except BaseException:
                    await tx.rollback()
                    raise
                else:
                    await (tx.commit() if commit else tx.rollback())

            result = await sync_environment(
                command.urls,
                timeout_seconds=command.timeout_seconds,
                data_factory=data_factory,
                capability_limits_factory=CapabilityLimits,
                request_id=request_id,
                command_sha256=digest,
            )
            assert result["state"] == "caller_pending"
            assert result["observed_urls"] == 1
            assert not client.is_in_transaction()
            with pytest.raises(HealthDataUnavailable):
                await binding.by_url(origin)
            async with client.transaction():
                with pytest.raises(HealthDataUnavailable):
                    await binding.by_url(origin)
                fresh = await RegistryIngressPostgresData.bind(client)
                receipt = await fresh.sync_result(request_id, digest)
                if commit:
                    assert receipt["state"] == "committed"
                    row = await fresh.by_url(origin)
                    assert row.state == "connected" and row.health_meta["capabilities"]
                    assert set(receipt) == {
                        "schema",
                        "request_id",
                        "command_sha256",
                        "state",
                        "observed_urls",
                    }
                else:
                    assert receipt is None and await fresh.by_url(origin) is None
            assert trace == ["http:/setup"]
    finally:
        await client.close()
        try:
            await _cleanup_registries(owner, registry_ids, [request_id])
        finally:
            await owner.close()


async def test_cached_row_still_materializes_unconsumed_json_and_retains_identity(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    client = await _client(test_engine)
    registry_id = uuid.uuid4()
    origin = f"http://ingress-json-{registry_id}.example.test"
    await _create_registry(owner, registry_id, "JSON", origin, "env")
    try:
        async with client.transaction():
            data = await RegistryIngressPostgresData.bind(client, autoflush=False)
            row = await data.by_url(origin)
            row.name = "caller-pending"
            assert await data.by_url(origin) is row
            assert row.name == "caller-pending"
            await owner.execute(
                "UPDATE ml_backend_registry SET health_meta=$2::jsonb WHERE id=$1",
                registry_id,
                '{"n":' + "9" * 5000 + "}",
            )
            with pytest.raises(ValueError):
                await data.by_url(origin)
            assert row.name == "caller-pending"
    finally:
        await client.close()
        try:
            await _cleanup_registries(owner, [registry_id])
        finally:
            await owner.close()
