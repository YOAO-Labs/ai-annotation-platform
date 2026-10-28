"""Terminal installation drains retain real owners and cannot reopen admission."""

import asyncio
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from aap_protocol_v2.errors import LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope, AdmissionTokenClaims, LifecycleModeRequest, LifecycleResetRequest,
    sign_admission_token,
)

from aap_backend_runtime import BuildArtifact, ManagedLruPool
from aap_backend_runtime.gpu_lifecycle import ManagedGpuLifecycle


def _pool(build=None, cleanup=None):
    return ManagedLruPool(
        1, build or (lambda key: BuildArtifact(key)), str, cleanup or (lambda: None),
        device_of=lambda _: "cpu", pool_name="models",
    )


def _cpu(pool):
    return ManagedGpuLifecycle(pool, verify_keyring={}, boot_id="boot-1",
                               expected_registry_id="registry-1", terminal_cpu_enabled=True)


def _stop(lifecycle, **overrides):
    args = {"registry_id": "registry-1", "boot_id": "boot-1", "operation_id": "stop-1"}
    args.update(overrides)
    return lifecycle.terminal_drain(**args)


def test_cancelled_caller_waits_for_builder_borrower_and_native_future():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        cleaned = []

        def build(key):
            entered.set()
            release.wait()
            return BuildArtifact(key)

        pool = _pool(build, lambda: cleaned.append(True))
        lifecycle = _cpu(pool)
        operation = await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
        native = asyncio.get_running_loop().create_future()
        operation.track_future(native)
        builder = asyncio.create_task(pool.warmup("model"))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            caller = asyncio.create_task(_stop(lifecycle))
            while lifecycle._terminal_identity is None:
                await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await operation.close()
            retry = asyncio.create_task(_stop(lifecycle))
            with pytest.raises(LifecycleHTTPError):
                await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
            assert not retry.done() and not cleaned
            release.set()
            await builder
            async with pool.borrow("model"):
                native.set_result(None)
                await asyncio.sleep(0.03)
                assert not retry.done() and not cleaned
            proof = await asyncio.wait_for(retry, 2)
            assert proof == {"registry_id": "registry-1", "boot_id": "boot-1", "operation_id": "stop-1",
                             "control_epoch": None, "generation": None, "pool_ids": ["models"], "unloaded": True}
            assert cleaned
            assert await _stop(lifecycle) == proof
            assert (await pool.snapshot())["current_size"] == 0
            with pytest.raises(LifecycleHTTPError):
                await lifecycle.legacy_unload()
            assert (await lifecycle.residency()).draining
        finally:
            release.set()
            await lifecycle.shutdown()
    asyncio.run(scenario())


def test_identity_fences_cannot_be_retargeted_or_enable_gpu_bypass():
    async def scenario():
        pool = _pool()
        lifecycle = _cpu(pool)
        for kwargs in ({"registry_id": "other"}, {"boot_id": "old"}, {"operation_id": ""}):
            with pytest.raises(LifecycleHTTPError):
                await _stop(lifecycle, **kwargs)
        op = await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
        await op.close()
        await _stop(lifecycle)
        for kwargs in ({"operation_id": "another"}, {"registry_id": "other"}, {"boot_id": "new"}):
            with pytest.raises(LifecycleHTTPError):
                await _stop(lifecycle, **kwargs)
        gpu = ManagedGpuLifecycle(_pool(), verify_keyring={}, expected_registry_id="registry-1",
                                  expected_gpu_resource_id="node/GPU-1", terminal_cpu_enabled=True,
                                  boot_id="boot-1")
        with pytest.raises(LifecycleHTTPError):
            await _stop(gpu)
        cold_unknown_device = ManagedGpuLifecycle(_pool(), verify_keyring={}, boot_id="boot-1",
                                                   expected_registry_id="registry-1")
        with pytest.raises(LifecycleHTTPError):
            await _stop(cold_unknown_device)
        await lifecycle.shutdown()
        await gpu.shutdown()
        await cold_unknown_device.shutdown()
    asyncio.run(scenario())

def test_registry_only_configuration_never_authorizes_signed_gpu_mode():
    async def scenario():
        key = Ed25519PrivateKey.generate()
        lifecycle = ManagedGpuLifecycle(_pool(), verify_keyring={"k": key.public_key()},
            evictable_verified=True, boot_id="boot-1", expected_registry_id="registry-1",
            terminal_cpu_enabled=True)
        claims = AdmissionTokenClaims(backend_registry_id="registry-1",
            gpu_resource_id="node/GPU-1", boot_id="boot-1", control_epoch="1",
            scope=AdmissionScope.MODE, jti="mode", exp=int(time.time()) + 60,
            owner="owner", operation="mode")
        signed = sign_admission_token(claims, private_key=key, kid="k")
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.set_mode(LifecycleModeRequest(gate="enforce", control_epoch="1"),
                                     token=signed)
        await lifecycle.shutdown()
    asyncio.run(scenario())


def test_signed_gpu_terminal_fence_rejects_reactivation_and_wrong_identity():
    async def scenario():
        key = Ed25519PrivateKey.generate()
        lifecycle = ManagedGpuLifecycle(_pool(), verify_keyring={"k": key.public_key()},
            evictable_verified=True, boot_id="boot-1", expected_registry_id="registry-1",
            expected_gpu_resource_id="node/GPU-1")

        def token(scope, jti, generation=None, operation="stop-1", registry="registry-1"):
            claims = AdmissionTokenClaims(backend_registry_id=registry, gpu_resource_id="node/GPU-1",
                boot_id="boot-1", generation=generation, control_epoch="1", scope=scope,
                jti=jti, exp=int(time.time()) + 60,
                owner=None if scope == AdmissionScope.PREDICT else "owner",
                operation=None if scope == AdmissionScope.PREDICT else operation)
            return sign_admission_token(claims, private_key=key, kid="k")

        await lifecycle.set_mode(LifecycleModeRequest(gate="enforce", control_epoch="1"),
                                 token=token(AdmissionScope.MODE, "mode"))
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.terminal_drain_signed("1", generation_header="1",
                token=token(AdmissionScope.DRAIN, "wrong", "1", registry="other"))
        active = await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header="1",
            token=token(AdmissionScope.PREDICT, "work", "1"))
        signed = token(AdmissionScope.DRAIN, "terminal", "2")
        stopping = asyncio.create_task(lifecycle.terminal_drain_signed("2", generation_header="2", token=signed))
        while lifecycle._terminal_identity is None:
            await asyncio.sleep(0)
        assert not stopping.done()
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.cancel_drain("3", generation_header="3",
                token=token(AdmissionScope.RESUME, "resume", "3"))
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.set_mode(LifecycleModeRequest(gate="legacy", control_epoch="1"),
                                     token=token(AdmissionScope.MODE, "reactivate"))
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.reset(LifecycleResetRequest(control_epoch="1"),
                                  token=token(AdmissionScope.RESET, "reset"))
        await active.close()
        proof = await stopping
        assert proof["generation"] == "2" and proof["pool_ids"] == ["models"]
        assert await lifecycle.terminal_drain_signed("2", generation_header="2", token=signed) == proof
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.terminal_drain_signed("2", generation_header="2",
                token=token(AdmissionScope.DRAIN, "other", "2", operation="other"))
        await lifecycle.shutdown()
    asyncio.run(scenario())

def test_signed_terminal_first_claim_drains_legacy_resident_and_borrower():
    async def scenario():
        key = Ed25519PrivateKey.generate()
        pool = _pool()
        lifecycle = ManagedGpuLifecycle(pool, verify_keyring={"k": key.public_key()},
            evictable_verified=False, boot_id="boot-1", expected_registry_id="registry-1",
            expected_gpu_resource_id="node/GPU-1")
        await pool.warmup("model")
        operation = await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
        def signed(registry):
            claims = AdmissionTokenClaims(backend_registry_id=registry, gpu_resource_id="node/GPU-1",
                boot_id="boot-1", generation="1", control_epoch="1", scope=AdmissionScope.DRAIN,
                jti="terminal", exp=int(time.time()) + 60, owner="owner", operation="stop-1")
            return sign_admission_token(claims, private_key=key, kid="k")
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.terminal_drain_signed("1", generation_header="1", token=signed("other"))
        async with pool.borrow("model"):
            stopping = asyncio.create_task(lifecycle.terminal_drain_signed(
                "1", generation_header="1", token=signed("registry-1")))
            while lifecycle._terminal_identity is None:
                await asyncio.sleep(0)
            with pytest.raises(LifecycleHTTPError):
                await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
            await operation.close()
            assert not stopping.done()
        proof = await stopping
        assert proof["unloaded"] and proof["control_epoch"] == "1"
        residency = await lifecycle.residency()
        assert residency.draining and not residency.gpu_loaded
        assert residency.identity.backend_registry_id == "registry-1"
        await lifecycle.shutdown()
    asyncio.run(scenario())



def test_failed_terminal_cleanup_never_authorizes_stop_or_reopens():
    async def scenario():
        def fail():
            raise RuntimeError("cleanup failed")
        lifecycle = _cpu(_pool(cleanup=fail))
        for _ in range(2):
            with pytest.raises(LifecycleHTTPError):
                await _stop(lifecycle)
        with pytest.raises(LifecycleHTTPError):
            await lifecycle.begin_workload(AdmissionScope.PREDICT, generation_header=None, token=None)
    asyncio.run(scenario())


@pytest.mark.parametrize("qualified", [False, True])
def test_managed_startup_rejects_workloads_until_signed_enforcement(qualified):
    async def scenario():
        builds = []
        pool = _pool(lambda key: (builds.append(key), BuildArtifact(key))[1])
        key = Ed25519PrivateKey.generate()
        lifecycle = ManagedGpuLifecycle(
            pool, verify_keyring={"k": key.public_key()}, evictable_verified=qualified,
            boot_id="boot-1", expected_registry_id="registry-1",
            expected_gpu_resource_id="node/GPU-1", allow_legacy_workloads=False,
        )

        def token(scope, jti, generation=None):
            control = scope in {AdmissionScope.RESET, AdmissionScope.MODE}
            claims = AdmissionTokenClaims(
                backend_registry_id="registry-1", gpu_resource_id="node/GPU-1",
                boot_id="boot-1", control_epoch="1", generation=generation,
                scope=scope, jti=jti, exp=int(time.time()) + 60,
                owner="owner" if control else None, operation=jti if control else None,
            )
            return sign_admission_token(claims, private_key=key, kid="k")

        async def work(scope, generation, permit):
            operation = await lifecycle.begin_workload(
                scope, generation_header=generation, token=permit,
            )
            try:
                if scope == AdmissionScope.WARMUP:
                    await pool.warmup("model")
                else:
                    async with pool.borrow("model"):
                        pass
            finally:
                await operation.close()

        for scope in (AdmissionScope.WARMUP, AdmissionScope.PREDICT):
            for generation, permit in (
                (None, None), ("1", "invalid"),
                ("1", token(scope, f"early-{scope.value}", "1")),
            ):
                with pytest.raises(LifecycleHTTPError) as error:
                    await work(scope, generation, permit)
                assert error.value.detail["error_code"] == "gpu_admission_denied"
        assert not builds
        assert (await pool.snapshot())["current_size"] == 0
        assert (await lifecycle.residency()).active_requests == 0

        await lifecycle.reset(LifecycleResetRequest(control_epoch="1"),
                              token=token(AdmissionScope.RESET, "reset"))
        if not qualified:
            with pytest.raises(LifecycleHTTPError):
                await lifecycle.set_mode(LifecycleModeRequest(gate="enforce", control_epoch="1"),
                                         token=token(AdmissionScope.MODE, "mode"))
            with pytest.raises(LifecycleHTTPError):
                await work(AdmissionScope.WARMUP, None, None)
            assert not builds
        else:
            await lifecycle.set_mode(LifecycleModeRequest(gate="enforce", control_epoch="1"),
                                     token=token(AdmissionScope.MODE, "mode"))
            for generation, permit in ((None, None), ("1", "invalid")):
                with pytest.raises(LifecycleHTTPError):
                    await work(AdmissionScope.WARMUP, generation, permit)
            assert not builds
            await work(AdmissionScope.WARMUP, "1", token(AdmissionScope.WARMUP, "warm", "1"))
            await work(AdmissionScope.PREDICT, "1", token(AdmissionScope.PREDICT, "predict", "1"))
            assert builds == ["model"]
        await lifecycle.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("policy", [None, True])
def test_legacy_default_and_explicit_local_policy_still_allow_workloads(policy):
    async def scenario():
        builds = []
        pool = _pool(lambda key: (builds.append(key), BuildArtifact(key))[1])
        options = {} if policy is None else {"allow_legacy_workloads": policy}
        lifecycle = ManagedGpuLifecycle(pool, verify_keyring={}, **options)
        operation = await lifecycle.begin_workload(
            AdmissionScope.PREDICT, generation_header=None, token=None,
        )
        assert not operation.managed
        async with pool.borrow("model"):
            pass
        await operation.close()
        assert builds == ["model"]
        await lifecycle.shutdown()
    asyncio.run(scenario())
