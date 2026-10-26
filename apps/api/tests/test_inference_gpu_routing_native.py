"""Native routing + actual TCP and GPU signatures; no GPU arbiter/hardware claim."""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aap_inference.backend_transport import BackendTransportError
from aap_inference.gpu.contracts import GPUDispatchGrant
from aap_inference.gpu.endpoint_source import GPUOwnedBackendEndpointSource
from aap_inference.owner_transport import BackendTransportGPUControlAdapter
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    LifecycleModeRequest,
    sign_admission_token,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect
from tests.test_inference_routing_owner_integration import _available_port, _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data environment",
    ),
]


async def test_gpu_unload_composes_native_route_and_real_gpu_signature_without_pool_drain(
    test_engine,
    tmp_path,
):
    await assert_owned_test_database(test_engine)
    dsn = os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"]
    registry_id, pool_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    world = {"registry_id": registry_id, "pool_id": pool_id, "member_id": member_id}
    port, route_token, boot = _available_port(), uuid.uuid4().hex, uuid.uuid4().hex
    key = Ed25519PrivateKey.generate()
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if name in os.environ
    }
    environment.update(
        AAP_FIXTURE_VERIFY_PEM=key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode(),
        AAP_FIXTURE_BOOT_ID=boot,
        AAP_FIXTURE_ROUTE_TOKEN=route_token,
        AAP_FIXTURE_REGISTRY_ID=str(registry_id),
        AAP_FIXTURE_PORT=str(port),
    )
    script = Path(__file__).parent / "fixtures/gpu_route_lifecycle_peer.py"
    log = (tmp_path / "gpu-route-peer.log").open("xb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        cwd=tmp_path,
        env=environment,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    owner = await _connect(test_engine)
    receipts = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
        )
    )
    runtime = None
    endpoint_pools: list[asyncpg.Pool] = []
    base = f"http://127.0.0.1:{port}"

    def token(scope, *, generation=None, private_key=key):
        claims = AdmissionTokenClaims(
            backend_registry_id=str(registry_id),
            gpu_resource_id="fixture/GPU-1",
            boot_id=boot,
            control_epoch="1",
            generation=generation,
            scope=scope,
            jti=uuid.uuid4().hex,
            exp=int(time.time()) + 120,
            owner="fixture-owner" if scope is not AdmissionScope.WARMUP else None,
            operation="fixture-eviction"
            if scope is not AdmissionScope.WARMUP
            else None,
        )
        return sign_admission_token(claims, private_key=private_key, kid="fixture")

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as probe:
            async with asyncio.timeout(15):
                while True:
                    assert process.returncode is None, (
                        "owned lifecycle peer exited before readiness"
                    )
                    try:
                        if (await probe.get(base + "/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
            await _seed_world(
                owner,
                registry_id=registry_id,
                pool_id=pool_id,
                member_id=member_id,
                backend_url=base,
                backend_token=route_token,
            )
            runtime = RoutingRuntime(dsn)
            assert await runtime.readiness()
            reply = await runtime.execute(
                make_routing_command(
                    "reconcile",
                    {
                        "registry_id": str(registry_id),
                        "control_id": str(uuid.uuid4()),
                    },
                )
            )
            assert reply.result.phase == "completed"
            await owner.execute(
                "UPDATE public.ml_backend_registry SET gpu_resource_id=$2,"
                "vram_budget_mb=1024 WHERE id=$1",
                registry_id,
                "fixture/GPU-1",
            )
            endpoint_pools.append(
                await asyncpg.create_pool(
                    dsn=os.environ[
                        "AAP_MODULE_DATA_INFERENCE_GPU_HEALTH_ENDPOINT_URL"
                    ].replace("postgresql+asyncpg://", "postgresql://", 1)
                )
            )
            endpoint_pools.append(
                await asyncpg.create_pool(
                    dsn=os.environ[
                        "AAP_MODULE_DATA_INFERENCE_GPU_CONTROL_ENDPOINT_URL"
                    ].replace("postgresql+asyncpg://", "postgresql://", 1)
                )
            )
            endpoint_source = GPUOwnedBackendEndpointSource(
                {
                    "health_observation": endpoint_pools[0],
                    "gpu_control": endpoint_pools[1],
                }
            )
            adapter = BackendTransportGPUControlAdapter(runtime)
            mode_request_id = uuid.uuid4()
            async with adapter.control_binding(
                registry_id,
                request_id=mode_request_id,
                operation="mode",
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    registry_id,
                    request_id=mode_request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                mode = await adapter.lifecycle_mode(
                    endpoint,
                    LifecycleModeRequest(gate="enforce", control_epoch="1"),
                    admission_token=token(AdmissionScope.MODE),
                    request_id=mode_request_id,
                    timeout_seconds=5,
                )
            assert mode.gate.value == "enforce"
            # This signed fixture exercises backend/Native routing only; it does not
            # claim a real GPU-arbiter admission or hardware capacity.
            warm_token = token(AdmissionScope.WARMUP, generation="1")
            response = await probe.post(
                base + "/warmup",
                headers={
                    "Authorization": f"Bearer {route_token}",
                    GPU_GENERATION_HEADER: "1",
                    GPU_ADMISSION_TOKEN_HEADER: warm_token,
                },
            )
            response.raise_for_status()
            assert (await probe.get(base + "/health")).json()["residency"][
                "gpu_loaded"
            ] is True
            drain = GPUDispatchGrant(
                "2",
                token(AdmissionScope.DRAIN, generation="2"),
            )
            drain_request_id = uuid.uuid4()
            async with adapter.control_binding(
                registry_id,
                request_id=drain_request_id,
                operation="drain",
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    registry_id,
                    request_id=drain_request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                drain_reply = await adapter.lifecycle_drain(
                    endpoint,
                    drain,
                    request_id=drain_request_id,
                    timeout_seconds=5,
                )
            assert drain_reply.generation == "2"
            bad = GPUDispatchGrant(
                "2",
                token(
                    AdmissionScope.UNLOAD,
                    generation="2",
                    private_key=Ed25519PrivateKey.generate(),
                ),
            )
            bad_unload_request_id = uuid.uuid4()
            async with adapter.control_binding(
                registry_id,
                request_id=bad_unload_request_id,
                operation="unload",
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    registry_id,
                    request_id=bad_unload_request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                with pytest.raises(BackendTransportError) as caught:
                    await adapter.lifecycle_unload(
                        endpoint,
                        bad,
                        request_id=bad_unload_request_id,
                        timeout_seconds=5,
                    )
            assert caught.value.status_code in {401, 403}
            assert bad.outcome.kind == "response_received"
            state = (await probe.get(base + "/health")).json()
            assert (
                state["successful_unloads"] == 0
                and state["residency"]["gpu_loaded"] is True
            )
            unload = GPUDispatchGrant(
                "2",
                token(AdmissionScope.UNLOAD, generation="2"),
            )
            unload_request_id = uuid.uuid4()
            async with adapter.control_binding(
                registry_id,
                request_id=unload_request_id,
                operation="unload",
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    registry_id,
                    request_id=unload_request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                unload_reply = await adapter.lifecycle_unload(
                    endpoint,
                    unload,
                    request_id=unload_request_id,
                    timeout_seconds=5,
                )
            assert unload_reply.unloaded is True
            state = (await probe.get(base + "/health")).json()
            assert (
                state["successful_unloads"] == 1
                and state["residency"]["gpu_loaded"] is False
            )
            assert (
                await owner.fetchval(
                    "SELECT traffic_state FROM public.ml_backend_pool_members WHERE id=$1",
                    member_id,
                )
                == "active"
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM aap_inference_routing_v1.controls WHERE registry_id=$1 AND kind='unload'",
                    registry_id,
                )
                == 0
            )
            rows = await owner.fetch(
                "SELECT s.id,s.state,s.closed,a.state AS attempt_state,a.status_code FROM aap_inference_routing_v1.scopes s JOIN aap_inference_routing_v1.attempts a ON a.scope_id=s.id WHERE s.registry_id=$1 AND s.operation='gpu:unload' ORDER BY a.created_at",
                registry_id,
            )
            assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
            assert [row["status_code"] for row in rows] == [
                caught.value.status_code,
                200,
            ]
            assert all(
                row["closed"]
                and row["state"] == "finished"
                and row["attempt_state"] == "response"
                for row in rows
            )
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()
        for pool in endpoint_pools:
            await pool.close()
        if runtime is not None:
            await runtime.close()
        try:
            after = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
                list(after - receipts),
            )
            await _cleanup(owner, world)
        finally:
            await owner.close()
