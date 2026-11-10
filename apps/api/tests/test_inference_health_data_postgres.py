"""Real function-only LOGIN executes portable health in the caller's transaction."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from yoao_inference.backend_transport import BackendTransport
from yoao_inference.health import BackendHealthCoordinator
from yoao_inference.health_data_postgres import (
    HealthDataPostgresData,
    HealthDataUnavailable,
)
from yoao_inference.gpu.native import GPUAuthorityPostgresData
from yoao_inference_contract.gpu import GPUAuthorityUnavailable
from yoao_inference_contract.capabilities import CapabilityLimits
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
            await HealthDataPostgresData.bind(
                client, expected_role="inference_health_data_client"
            )
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
            data = await HealthDataPostgresData.bind(
                client, expected_role="inference_health_data_client"
            )

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
                fresh = await HealthDataPostgresData.bind(
                    client, expected_role="inference_health_data_client"
                )
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


@pytest.mark.parametrize("profile", ["health_data", "gpu_authority"])
async def test_independent_function_login_requires_its_exact_expected_role(
    test_engine, profile
):
    from yoao_inference.gpu.native import _AUTHORITY_FUNCTIONS
    from yoao_inference.health_data_postgres import _FUNCTIONS

    await assert_owned_test_database(test_engine)
    role = "fixture_profile_" + uuid.uuid4().hex
    password = uuid.uuid4().hex
    schema, functions = (
        ("aap_inference_health_data_v1", _FUNCTIONS)
        if profile == "health_data"
        else ("aap_inference_gpu_v1", _AUTHORITY_FUNCTIONS)
    )
    grant_functions = ", ".join(
        f"{schema}.{name}({signature})" for name, signature in sorted(functions)
    )
    owner = await _connect(test_engine)
    client = None
    created = False
    database = test_engine.dialect.identifier_preparer.quote_identifier(
        test_engine.url.database
    )
    try:
        await owner.execute(
            f"CREATE ROLE {role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
        )
        created = True
        await owner.execute(f"GRANT CONNECT ON DATABASE {database} TO {role}")
        await owner.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        await owner.execute(f"GRANT EXECUTE ON FUNCTION {grant_functions} TO {role}")
        url = test_engine.url.set(
            drivername="postgresql", username=role, password=password
        ).render_as_string(hide_password=False)
        client = await asyncpg.connect(url, timeout=5, command_timeout=10)
        binding, unavailable = (
            (HealthDataPostgresData, HealthDataUnavailable)
            if profile == "health_data"
            else (GPUAuthorityPostgresData, GPUAuthorityUnavailable)
        )
        async with client.transaction():
            with pytest.raises(unavailable):
                await binding.bind(client, expected_role="another_owner")
            await binding.bind(client, expected_role=role)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await client.fetchval("SELECT count(*) FROM public.ml_backend_registry")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await client.fetchval(
                "SELECT aap_inference_backend_endpoint_v1.backend_endpoint_policy()"
            )
    finally:
        if client is not None:
            await client.close()
        if created:
            await owner.execute(
                f"REVOKE EXECUTE ON FUNCTION {grant_functions} FROM {role}"
            )
            await owner.execute(f"REVOKE CONNECT ON DATABASE {database} FROM {role}")
            await owner.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {role}")
            await owner.execute(f"DROP ROLE {role}")
        await owner.close()
