"""Managed SAM3 lifecycle fencing, replay, and cleanup contract tests."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from aap_protocol_v2.errors import LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    LifecycleModeRequest,
    LifecycleResetRequest,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.modules.setdefault(
    "torch", MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False)))
)


class _Model:
    names = {0: "object"}
    device = "cuda:0"


def _domain(*, free_gpu_memory=None):
    from gpu_lifecycle import Sam3GpuLifecycle
    from aap_backend_runtime import BuildArtifact, ManagedLruPool
    from pool_domain import Sam3Pools

    private_key = Ed25519PrivateKey.generate()
    pool = ManagedLruPool(
        2,
        lambda _key: BuildArtifact(_Model()),
        str,
        free_gpu_memory or (lambda: None),
        build_timeout=1.0,
    )
    multiplex = ManagedLruPool(
        1,
        lambda _key: BuildArtifact(_Model()),
        str,
        lambda: None,
        build_timeout=1.0,
    )
    pvs = ManagedLruPool(
        1,
        lambda _key: BuildArtifact(_Model()),
        str,
        lambda: None,
        build_timeout=1.0,
    )
    lifecycle = Sam3GpuLifecycle(
        Sam3Pools(pool, multiplex, pvs),
        verify_keyring={"current": private_key.public_key()},
        evictable_verified=True,
        boot_id="boot-1",
    )
    return lifecycle, pool, private_key


def _token(
    lifecycle,
    private_key: Ed25519PrivateKey,
    scope: AdmissionScope,
    *,
    generation: str | None = None,
    control_epoch: str = "1",
    jti: str,
    owner: str = "platform-1",
    operation: str = "operation-1",
    boot_id: str | None = None,
) -> str:
    control = scope in {
        AdmissionScope.DRAIN,
        AdmissionScope.UNLOAD,
        AdmissionScope.RESUME,
        AdmissionScope.MODE,
        AdmissionScope.RESET,
    }
    claims = AdmissionTokenClaims(
        backend_registry_id="backend-1",
        gpu_resource_id="node-a/GPU-1",
        boot_id=boot_id or lifecycle.boot_id,
        generation=generation,
        control_epoch=control_epoch,
        scope=scope,
        jti=jti,
        exp=int(time.time()) + 60,
        owner=owner if control else None,
        operation=operation if control else None,
    )
    return sign_admission_token(claims, private_key=private_key, kid="current")


async def _enforce(lifecycle, private_key, *, epoch: str = "1"):
    return await lifecycle.set_mode(
        LifecycleModeRequest(gate="enforce", control_epoch=epoch),
        token=_token(
            lifecycle,
            private_key,
            AdmissionScope.MODE,
            control_epoch=epoch,
            jti=f"mode-{epoch}",
            operation=f"mode-{epoch}",
        ),
    )


@pytest.mark.asyncio
async def test_fresh_boot_reports_trusted_legacy_empty_residency() -> None:
    lifecycle, _pool, _key = _domain()

    residency = await lifecycle.residency()

    assert residency.state.value == "unloaded"
    assert residency.gpu_loaded is False
    assert residency.lifecycle_gate.value == "legacy"
    assert residency.generation is None
    assert residency.identity is None
    assert residency.pools["image"].resident is False


@pytest.mark.asyncio
async def test_legacy_workload_requires_signed_reset_before_enforce() -> None:
    lifecycle, _pool, key = _domain()
    operation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header=None,
        token=None,
    )
    await operation.close()

    with pytest.raises(LifecycleHTTPError) as mode_error:
        await _enforce(lifecycle, key)
    assert mode_error.value.detail["error_code"] == "gpu_transition_conflict"

    reset = await lifecycle.reset(
        LifecycleResetRequest(control_epoch="1"),
        token=_token(
            lifecycle,
            key,
            AdmissionScope.RESET,
            control_epoch="1",
            jti="reset-1",
            operation="reset-1",
        ),
    )
    assert reset.unloaded is True
    assert reset.residency.generation is None
    assert reset.residency.gpu_loaded is False

    mode = await _enforce(lifecycle, key, epoch="2")
    assert mode.gate.value == "enforce"
    assert mode.residency.identity is not None


@pytest.mark.parametrize(
    ("generation_header", "token"),
    [
        ("1", None),
        (None, "invalid-token"),
        ("1", "invalid-token"),
    ],
)
@pytest.mark.asyncio
async def test_legacy_workload_rejects_partial_or_invalid_managed_headers(
    generation_header: str | None,
    token: str | None,
) -> None:
    lifecycle, _pool, _key = _domain()

    with pytest.raises(LifecycleHTTPError) as error:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header=generation_header,
            token=token,
        )

    assert error.value.status_code == 403
    assert (await lifecycle.residency()).active_requests == 0


@pytest.mark.asyncio
async def test_legacy_workload_rejects_valid_managed_headers_without_open_generation() -> (
    None
):
    lifecycle, _pool, key = _domain()

    with pytest.raises(LifecycleHTTPError) as error:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="1",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.PREDICT,
                generation="1",
                jti="legacy-managed",
            ),
        )

    assert error.value.status_code == 403
    assert (await lifecycle.residency()).active_requests == 0


@pytest.mark.asyncio
async def test_enforce_consumes_workload_jti_and_rejects_wrong_scope_or_boot() -> None:
    lifecycle, _pool, key = _domain()
    await _enforce(lifecycle, key)
    token = _token(
        lifecycle,
        key,
        AdmissionScope.PREDICT,
        generation="1",
        jti="lease-1",
    )
    operation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header="1",
        token=token,
    )
    assert (await lifecycle.residency()).active_requests == 1

    with pytest.raises(LifecycleHTTPError) as replay:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="1",
            token=token,
        )
    assert replay.value.status_code == 403
    await operation.close()

    wrong_scope = _token(
        lifecycle,
        key,
        AdmissionScope.WARMUP,
        generation="1",
        jti="lease-wrong-scope",
    )
    with pytest.raises(LifecycleHTTPError) as scope_error:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="1",
            token=wrong_scope,
        )
    assert scope_error.value.status_code == 403

    old_boot = _token(
        lifecycle,
        key,
        AdmissionScope.PREDICT,
        generation="1",
        jti="lease-old-boot",
        boot_id="old-boot",
    )
    with pytest.raises(LifecycleHTTPError) as boot_error:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="1",
            token=old_boot,
        )
    assert boot_error.value.status_code == 403


@pytest.mark.asyncio
async def test_drain_blocks_new_work_and_unload_waits_for_active() -> None:
    lifecycle, _pool, key = _domain()
    await _enforce(lifecycle, key)
    active = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header="1",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.PREDICT,
            generation="1",
            jti="lease-active",
        ),
    )
    drain = await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="drain-1",
        ),
    )
    assert drain.active_requests == 1
    assert drain.ready_to_unload is False

    with pytest.raises(LifecycleHTTPError) as draining:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.PREDICT,
                generation="2",
                jti="lease-blocked",
            ),
        )
    assert draining.value.status_code == 503

    unload_token = _token(
        lifecycle,
        key,
        AdmissionScope.UNLOAD,
        generation="2",
        jti="unload-1",
    )
    with pytest.raises(LifecycleHTTPError) as busy:
        await lifecycle.managed_unload(
            "2",
            generation_header="2",
            token=unload_token,
        )
    assert busy.value.detail["error_code"] == "gpu_backend_active"

    await active.close()
    retry = await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="drain-2",
        ),
    )
    assert retry.ready_to_unload is True
    unloaded = await lifecycle.managed_unload(
        "2",
        generation_header="2",
        token=unload_token,
    )
    assert unloaded.unloaded is True


@pytest.mark.asyncio
async def test_managed_unload_keeps_generation_tombstone_until_new_allocation() -> None:
    lifecycle, pool, key = _domain()
    await _enforce(lifecycle, key)
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP,
        generation_header="1",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.WARMUP,
            generation="1",
            jti="warmup-1",
        ),
    )
    await pool.warmup("sam3")
    await operation.close()
    assert (await lifecycle.residency()).evictable is True

    await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="drain-1",
        ),
    )
    unloaded = await lifecycle.managed_unload(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.UNLOAD,
            generation="2",
            jti="unload-1",
        ),
    )
    assert unloaded.residency.generation == "2"
    assert unloaded.residency.gpu_loaded is False

    with pytest.raises(LifecycleHTTPError) as tombstone:
        await lifecycle.begin_workload(
            AdmissionScope.PREDICT,
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.PREDICT,
                generation="2",
                jti="lease-same-generation",
            ),
        )
    assert tombstone.value.detail["error_code"] == "gpu_generation_conflict"

    new_allocation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header="3",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.PREDICT,
            generation="3",
            jti="lease-new-generation",
        ),
    )
    assert new_allocation.generation == "3"
    await new_allocation.close()


@pytest.mark.asyncio
async def test_cancel_uses_new_generation_and_fences_late_unload() -> None:
    lifecycle, _pool, key = _domain()
    await _enforce(lifecycle, key)
    initial = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header="1",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.PREDICT,
            generation="1",
            jti="lease-1",
        ),
    )
    await initial.close()
    await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="drain-1",
            owner="owner-1",
            operation="drain-operation",
        ),
    )
    with pytest.raises(LifecycleHTTPError) as wrong_operation:
        await lifecycle.cancel_drain(
            "3",
            generation_header="3",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.RESUME,
                generation="3",
                jti="resume-wrong-operation",
                owner="owner-1",
                operation="resume-operation",
            ),
        )
    assert wrong_operation.value.detail["error_code"] == "gpu_transition_conflict"
    still_draining = await lifecycle.residency()
    assert still_draining.draining is True
    assert still_draining.generation == "2"

    resume_token = _token(
        lifecycle,
        key,
        AdmissionScope.RESUME,
        generation="3",
        jti="resume-1",
        owner="owner-1",
        operation="drain-operation",
    )
    resumed = await lifecycle.cancel_drain(
        "3",
        generation_header="3",
        token=resume_token,
    )
    assert resumed.draining is False
    assert resumed.generation == "3"
    assert (
        await lifecycle.cancel_drain(
            "3",
            generation_header="3",
            token=resume_token,
        )
    ) == resumed

    with pytest.raises(LifecycleHTTPError) as stale:
        await lifecycle.managed_unload(
            "2",
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.UNLOAD,
                generation="2",
                jti="late-unload",
                owner="owner-1",
                operation="drain-operation",
            ),
        )
    assert stale.value.detail["error_code"] == "gpu_generation_conflict"


@pytest.mark.asyncio
async def test_transition_jti_cannot_be_reused_across_routes() -> None:
    lifecycle, _pool, key = _domain()
    await _enforce(lifecycle, key)
    operation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header="1",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.PREDICT,
            generation="1",
            jti="lease-1",
        ),
    )
    await operation.close()
    await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="shared-jti",
        ),
    )

    with pytest.raises(LifecycleHTTPError) as replay:
        await lifecycle.managed_unload(
            "2",
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.UNLOAD,
                generation="2",
                jti="shared-jti",
            ),
        )
    assert replay.value.status_code == 403


@pytest.mark.asyncio
async def test_mode_is_idempotent_only_for_same_gate_owner_and_operation() -> None:
    lifecycle, _pool, key = _domain()
    await _enforce(lifecycle, key)
    retry = await lifecycle.set_mode(
        LifecycleModeRequest(gate="enforce", control_epoch="1"),
        token=_token(
            lifecycle,
            key,
            AdmissionScope.MODE,
            control_epoch="1",
            jti="mode-retry",
            operation="mode-1",
        ),
    )
    assert retry.gate.value == "enforce"

    with pytest.raises(LifecycleHTTPError) as conflict:
        await lifecycle.set_mode(
            LifecycleModeRequest(gate="legacy", control_epoch="1"),
            token=_token(
                lifecycle,
                key,
                AdmissionScope.MODE,
                control_epoch="1",
                jti="mode-conflict",
                operation="mode-conflict",
            ),
        )
    assert conflict.value.detail["error_code"] == "gpu_transition_conflict"


@pytest.mark.asyncio
async def test_tracked_future_keeps_active_after_http_operation_closes() -> None:
    lifecycle, _pool, _key = _domain()
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP,
        generation_header=None,
        token=None,
    )
    future = asyncio.get_running_loop().create_future()
    operation.track_future(future)
    await operation.close()
    assert (await lifecycle.residency()).active_requests == 1

    future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert (await lifecycle.residency()).active_requests == 0


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_operation_and_pool_borrower() -> None:
    lifecycle, pool, _key = _domain()
    operation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header=None,
        token=None,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use_model() -> None:
        async with pool.borrow("sam3"):
            entered.set()
            await release.wait()

    borrower = asyncio.create_task(use_model())
    await entered.wait()
    shutdown = asyncio.create_task(lifecycle.shutdown())
    await asyncio.sleep(0.02)
    residency = await lifecycle.residency()
    assert not shutdown.done()
    assert residency.state.value == "unloading"
    assert residency.active_requests == 1
    assert residency.borrowers == 1

    release.set()
    await borrower
    await operation.close()
    await asyncio.wait_for(shutdown, timeout=1.0)
    residency = await lifecycle.residency()
    assert residency.state.value == "unloaded"
    assert residency.gpu_loaded is False
    assert residency.active_requests == 0
    assert residency.borrowers == 0


@pytest.mark.asyncio
async def test_cancelled_shutdown_restores_state_after_full_cleanup() -> None:
    lifecycle, pool, _key = _domain()
    operation = await lifecycle.begin_workload(
        AdmissionScope.PREDICT,
        generation_header=None,
        token=None,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use_model() -> None:
        async with pool.borrow("sam3"):
            entered.set()
            await release.wait()

    borrower = asyncio.create_task(use_model())
    await entered.wait()
    shutdown = asyncio.create_task(lifecycle.shutdown())
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0.02)
    shutdown.cancel()
    assert not shutdown.done()
    assert (await lifecycle.residency()).state.value == "unloading"

    release.set()
    await borrower
    await operation.close()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(shutdown), timeout=1.0)
    residency = await lifecycle.residency()
    assert residency.state.value == "unloaded"
    assert residency.gpu_loaded is False
    assert residency.active_requests == 0
    assert residency.borrowers == 0


@pytest.mark.asyncio
async def test_cancelled_close_still_releases_active_operation() -> None:
    lifecycle, _pool, _key = _domain()
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP,
        generation_header=None,
        token=None,
    )

    await lifecycle._lock.acquire()  # noqa: SLF001 - force cancellation at close lock
    close_task = asyncio.create_task(operation.close())
    await asyncio.sleep(0)
    close_task.cancel()
    await asyncio.sleep(0)
    assert len(lifecycle._active) == 1  # noqa: SLF001

    lifecycle._lock.release()  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert (await lifecycle.residency()).active_requests == 0


@pytest.mark.asyncio
async def test_cancelled_legacy_unload_owner_recovers_without_keyring() -> None:
    from gpu_lifecycle import Sam3GpuLifecycle
    from aap_backend_runtime import BuildArtifact, ManagedLruPool
    from pool_domain import Sam3Pools

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    def cleanup() -> None:
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=1.0)

    pool = ManagedLruPool(
        1,
        lambda _key: BuildArtifact(_Model()),
        str,
        cleanup,
        build_timeout=1.0,
    )
    multiplex = ManagedLruPool(
        1,
        lambda _key: BuildArtifact(_Model()),
        str,
        lambda: None,
    )
    pvs = ManagedLruPool(
        1,
        lambda _key: BuildArtifact(_Model()),
        str,
        lambda: None,
    )
    lifecycle = Sam3GpuLifecycle(
        Sam3Pools(pool, multiplex, pvs),
        verify_keyring={},
        boot_id="boot-legacy",
    )
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP,
        generation_header=None,
        token=None,
    )
    await pool.warmup("sam3")
    await operation.close()

    unload = asyncio.create_task(lifecycle.legacy_unload())
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(cleanup_started.wait), timeout=1.0
        )
        unload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await unload
        assert (await lifecycle.residency()).state.value == "unloading"

        allow_cleanup.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            residency = await lifecycle.residency()
            if residency.state.value == "unloaded":
                break
            await asyncio.sleep(0.01)
        assert residency.gpu_loaded is False

        next_operation = await lifecycle.begin_workload(
            AdmissionScope.WARMUP,
            generation_header=None,
            token=None,
        )
        await next_operation.close()
    finally:
        allow_cleanup.set()


@pytest.mark.asyncio
async def test_failed_cleanup_stays_unknown_is_idempotent_and_reset_recovers() -> None:
    fail_cleanup = True
    cleanup_calls = 0

    def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if fail_cleanup:
            raise RuntimeError("CUDA cleanup failed")

    lifecycle, pool, key = _domain(free_gpu_memory=cleanup)
    await _enforce(lifecycle, key)
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP,
        generation_header="1",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.WARMUP,
            generation="1",
            jti="warmup-1",
        ),
    )
    await pool.warmup("sam3")
    await operation.close()
    await lifecycle.drain(
        "2",
        generation_header="2",
        token=_token(
            lifecycle,
            key,
            AdmissionScope.DRAIN,
            generation="2",
            jti="drain-1",
        ),
    )

    with pytest.raises(LifecycleHTTPError) as failed:
        await lifecycle.managed_unload(
            "2",
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.UNLOAD,
                generation="2",
                jti="unload-1",
            ),
        )
    assert failed.value.detail["error_code"] == "gpu_unload_failed"
    assert (await lifecycle.residency()).state.value == "unknown"

    with pytest.raises(LifecycleHTTPError):
        await lifecycle.managed_unload(
            "2",
            generation_header="2",
            token=_token(
                lifecycle,
                key,
                AdmissionScope.UNLOAD,
                generation="2",
                jti="unload-retry",
            ),
        )
    assert cleanup_calls == 1

    await lifecycle.set_mode(
        LifecycleModeRequest(gate="legacy", control_epoch="2"),
        token=_token(
            lifecycle,
            key,
            AdmissionScope.MODE,
            control_epoch="2",
            jti="mode-legacy",
            operation="mode-legacy",
        ),
    )
    fail_cleanup = False
    recovered = await lifecycle.reset(
        LifecycleResetRequest(control_epoch="3"),
        token=_token(
            lifecycle,
            key,
            AdmissionScope.RESET,
            control_epoch="3",
            jti="reset-recover",
            operation="reset-recover",
        ),
    )
    assert recovered.residency.state.value == "unloaded"
    assert recovered.residency.gpu_loaded is False
