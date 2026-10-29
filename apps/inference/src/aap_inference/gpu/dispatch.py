"""Resident and cold GPU dispatch authority.

Portable orchestration for GPU admission, lifecycle eviction, and terminal proof.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    BackendResidency,
    DrainTransitionResponse,
    LifecycleState,
    ManagedUnloadResponse,
)

from aap_inference.gpu.endpoint_source import OwnedBackendEndpointSource
from aap_inference.gpu.settings import GPUAuthoritySettings as Settings
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchContextFactory,
    GPUDispatchGrant,
    GPUDispatchRequest,
)
from aap_inference.gpu.native import GPUAuthorityDataProvider
from aap_inference.gpu.transport import GPUBackendControlTransport
from aap_inference.gpu.proofs import (
    GPUBusyEvictionRuntimeSubjectError,
    GPUColdRuntimeSubject,
    GPUColdRuntimeSubjectError,
    GPUColdTerminalCommitResult,
    GPUEvictionCommitResult,
    GPUEvictionDrainHealth,
    GPUIdleEvictionRuntimeSubjectError,
    GPUPreparedColdRuntimeSubject,
    GPUPreparedEvictionCancelRuntimeSubject,
    GPUPreparedIdleEvictionRuntimeSubject,
    GPUResidentRuntimeSubject,
    GPUResidentRuntimeSubjectError,
    commit_gpu_cold_terminal_from_health,
    commit_gpu_eviction_cancel_from_health,
    commit_gpu_eviction_phase_from_health,
    prepare_gpu_cold_runtime_generation,
    prepare_gpu_eviction_cancel_runtime_generation,
    prepare_gpu_idle_eviction_runtime_generation,
    read_gpu_busy_eviction_runtime_subject,
    read_gpu_cold_runtime_subject,
    read_gpu_eviction_drain_health,
    read_gpu_idle_eviction_runtime_subject,
    read_gpu_resident_runtime_subject,
    record_gpu_resident_runtime_token_expiry,
)
from aap_inference.gpu.ledger import (
    GPU_EVICTION_OPERATION,
    GPUAdmissionResult,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUArbiterStoreError,
    GPUCardSnapshot,
    GPUQueueResult,
)
from aap_inference.gpu.policy import _HEALTH_EVIDENCE_MAX_AGE


logger = structlog.get_logger(__name__)

_GPU_RUNTIME_HEARTBEAT_TTL_MS = 15_000
_GPU_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 5.0
_GPU_RUNTIME_HARD_DEADLINE_GRACE_SECONDS = 30
_GPU_RUNTIME_RECONCILE_DEADLINE_GRACE_MS = int(
    _HEALTH_EVIDENCE_MAX_AGE.total_seconds() * 1000
)
_GPU_RUNTIME_MAX_RECONCILE_HORIZON_MS = 300_000
_GPU_RUNTIME_MIN_RECONCILE_GRACE_MS = 120_000
_GPU_RUNTIME_MIN_TOKEN_WINDOW_SECONDS = 5
_GPU_RUNTIME_RETRY_AFTER_SECONDS = 1
_GPU_RUNTIME_COLD_INTENT_TTL_MS = 30_000
_GPU_RUNTIME_MAX_EVICTION_ATTEMPTS = 128
_GPU_RUNTIME_QUEUE_POLL_INTERVAL_SECONDS = 0.05
_GPU_RUNTIME_MAX_ADMISSION_TIMEOUT_SECONDS = 3600
_GPU_RUNTIME_MAX_RESIDENCY_COOLDOWN_SECONDS = 3600
_GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS = 6.0
_GPU_RUNTIME_EVICTION_CANCEL_HORIZON_SECONDS = 30.0
_GPU_RUNTIME_NO_SAFE_IDLE_VICTIM = "No safe idle GPU victim can satisfy capacity"
_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807
_MAX_REDIS_TTL_MS = 2_147_483_647
_GPU_RUNTIME_SCOPES = {
    "predict": AdmissionScope.PREDICT,
    "predict_interactive": AdmissionScope.PREDICT,
    "warmup": AdmissionScope.WARMUP,
    "reload": AdmissionScope.RELOAD,
}

GPUArbiterStoreFactory = Callable[[], GPUArbiterStore]
GPUAdmissionSignerFactory = Callable[[], GPUAdmissionTokenSigner]
GPUHealthRefresher = Callable[[uuid.UUID, str], Awaitable[bool]]


class GPUDispatchClock(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


GPURuntimeLeaseSubject = GPUResidentRuntimeSubject | GPUPreparedColdRuntimeSubject


class _GPUVictimCooldownActive(RuntimeError):
    def __init__(self, retry_at_ms: int) -> None:
        self.retry_at_ms = retry_at_ms
        super().__init__("GPU victim residency cooldown is active")


def _dispatch_error(
    code: GPUArbiterErrorCode,
    message: str,
    *,
    retry_after_s: int | None = None,
) -> GPUArbiterDispatchError:
    return GPUArbiterDispatchError(
        code,
        message=message,
        retry_after_s=retry_after_s,
    )


def _validate_runtime_request(request: GPUDispatchRequest) -> None:
    expected_scope = _GPU_RUNTIME_SCOPES.get(request.operation)
    if expected_scope is None:
        raise _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU control dispatch authority is not ready",
        )
    if request.scope is not expected_scope:
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU dispatch operation and admission scope do not match",
        )


def _map_admission_rejection(result: GPUAdmissionResult) -> GPUArbiterDispatchError:
    if result.status in {"concurrency_saturated", "concurrency_queued"}:
        return _dispatch_error(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            "GPU backend concurrency is saturated",
            retry_after_s=_GPU_RUNTIME_RETRY_AFTER_SECONDS,
        )
    if result.status == "card_queued":
        return _capacity_unavailable("GPU card admission queue is no longer available")
    if result.status == "capacity_unavailable":
        return _dispatch_error(
            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
            "GPU resident capacity is unavailable",
        )
    if result.status in {
        "not_ready",
        "transition_in_progress",
        "stale_generation",
        "config_mismatch",
    }:
        return _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU resident runtime is not ready",
        )
    return _dispatch_error(
        GPUArbiterErrorCode.UNAVAILABLE,
        "GPU arbiter admission failed",
    )


def _map_queue_rejection(
    result: GPUQueueResult,
    *,
    card_queue: bool,
) -> GPUArbiterDispatchError:
    if result.status == "full":
        if card_queue:
            return _capacity_unavailable("GPU card admission queue is full")
        return _dispatch_error(
            GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
            "GPU backend admission queue is full",
            retry_after_s=_GPU_RUNTIME_RETRY_AFTER_SECONDS,
        )
    if result.status in {"not_ready", "config_mismatch"}:
        return _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU admission queue is not ready",
        )
    return _dispatch_error(
        GPUArbiterErrorCode.UNAVAILABLE,
        "GPU admission queue is unavailable",
    )


async def _enqueue_fifo_ticket(
    store: GPUArbiterStore,
    clock: GPUDispatchClock,
    resource_id: str,
    *,
    backend_id: str,
    membership_epoch: int,
    ticket_id: str,
    owner_id: str,
    deadline: float,
    card_queue: bool,
) -> GPUQueueResult:
    method = store.enqueue_card if card_queue else store.enqueue_backend

    def kwargs() -> dict:
        remaining_ms = min(
            _GPU_RUNTIME_MAX_ADMISSION_TIMEOUT_SECONDS * 1000,
            math.ceil((deadline - clock.monotonic()) * 1000),
        )
        if remaining_ms <= 0:
            raise _queue_timeout(card_queue=card_queue)
        return {
            "backend_id": backend_id,
            "membership_epoch": membership_epoch,
            "ticket_id": ticket_id,
            "owner_id": owner_id,
            "ttl_ms": remaining_ms,
        }

    try:
        result = await method(resource_id, **kwargs())
    except GPUArbiterDispatchError:
        raise
    except Exception:
        try:
            result = await method(resource_id, **kwargs())
        except GPUArbiterDispatchError:
            raise
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission queue result is uncertain",
            ) from exc
    if result.status != "queued":
        raise _map_queue_rejection(result, card_queue=card_queue)
    if (
        result.ticket_id != ticket_id
        or result.position is None
        or result.position <= 0
        or result.expires_at_ms is None
        or result.expires_at_ms <= 0
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU admission queue returned an invalid ticket",
        )
    return result


async def _read_fifo_position(
    store: GPUArbiterStore,
    resource_id: str,
    *,
    backend_id: str,
    ticket_id: str,
    card_queue: bool,
) -> GPUQueueResult:
    kwargs = {
        "backend_id": backend_id,
        "ticket_id": ticket_id,
        "card_queue": card_queue,
    }
    try:
        return await store.queue_position(resource_id, **kwargs)
    except Exception:
        try:
            return await store.queue_position(resource_id, **kwargs)
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission queue position is unavailable",
            ) from exc


async def _cancel_fifo_ticket(
    store: GPUArbiterStore,
    resource_id: str,
    *,
    backend_id: str,
    ticket_id: str,
    owner_id: str,
    card_queue: bool,
) -> None:
    kwargs = {
        "backend_id": backend_id,
        "ticket_id": ticket_id,
        "owner_id": owner_id,
        "card_queue": card_queue,
    }
    try:
        try:
            result = await store.cancel_queue_ticket(resource_id, **kwargs)
        except Exception:
            result = await store.cancel_queue_ticket(resource_id, **kwargs)
        if result.status not in {"cancelled", "missing"}:
            logger.warning(
                "gpu_admission_queue_cleanup_rejected",
                gpu_arbiter={
                    "backend_id": backend_id,
                    "resource_id": resource_id,
                    "ticket_id": ticket_id,
                    "queue": "card" if card_queue else "backend",
                    "status": result.status,
                },
            )
    except Exception:  # noqa: BLE001 - ticket TTL remains the bounded fallback
        logger.warning(
            "gpu_admission_queue_cleanup_failed",
            gpu_arbiter={
                "backend_id": backend_id,
                "resource_id": resource_id,
                "ticket_id": ticket_id,
                "queue": "card" if card_queue else "backend",
            },
            exc_info=True,
        )


def _queue_remaining_seconds(
    clock: GPUDispatchClock,
    *,
    deadline: float,
    expires_at_ms: int,
) -> float:
    return min(
        deadline - clock.monotonic(),
        (expires_at_ms - int(clock.time() * 1000)) / 1000,
    )


def _queue_timeout(*, card_queue: bool) -> GPUArbiterDispatchError:
    if card_queue:
        return _capacity_unavailable("GPU card admission queue timed out")
    return _dispatch_error(
        GPUArbiterErrorCode.BACKEND_CONCURRENCY_SATURATED,
        "GPU backend admission queue timed out",
        retry_after_s=_GPU_RUNTIME_RETRY_AFTER_SECONDS,
    )


def _queue_remaining_ttl_ms(
    *,
    clock: GPUDispatchClock,
    deadline: float,
    expires_at_ms: int,
) -> int:
    remaining_ms = int(
        _queue_remaining_seconds(
            clock,
            deadline=deadline,
            expires_at_ms=expires_at_ms,
        )
        * 1000
    )
    if remaining_ms <= 0:
        raise _queue_timeout(card_queue=True)
    return remaining_ms


def _eviction_work_remaining_ttl_ms(
    *,
    clock: GPUDispatchClock,
    deadline: float,
    expires_at_ms: int,
) -> int:
    remaining_ms = _queue_remaining_ttl_ms(
        clock=clock,
        deadline=deadline,
        expires_at_ms=expires_at_ms,
    ) - math.ceil(_GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS * 1000)
    if remaining_ms <= 0:
        raise _queue_timeout(card_queue=True)
    return remaining_ms


@asynccontextmanager
async def _bounded_card_admission_step(
    *,
    clock: GPUDispatchClock,
    deadline: float,
    expires_at_ms: int,
    reserve_seconds: float = 0,
):
    remaining = (
        _queue_remaining_seconds(
            clock,
            deadline=deadline,
            expires_at_ms=expires_at_ms,
        )
        - reserve_seconds
    )
    if remaining <= 0:
        raise _queue_timeout(card_queue=True)
    timeout = asyncio.timeout(remaining)
    try:
        async with timeout:
            yield
    except TimeoutError as exc:
        if timeout.expired():
            raise _queue_timeout(card_queue=True) from exc
        raise


async def _wait_for_card_fifo_head(
    store: GPUArbiterStore,
    clock: GPUDispatchClock,
    resource_id: str,
    *,
    backend_id: str,
    ticket: GPUQueueResult,
    deadline: float,
    poll_interval_seconds: float,
) -> None:
    position = ticket.position
    assert ticket.expires_at_ms is not None
    while position != 1:
        remaining = _queue_remaining_seconds(
            clock,
            deadline=deadline,
            expires_at_ms=ticket.expires_at_ms,
        )
        if remaining <= 0:
            raise _capacity_unavailable("GPU card admission queue timed out")
        await clock.sleep(min(poll_interval_seconds, remaining))
        if (
            _queue_remaining_seconds(
                clock,
                deadline=deadline,
                expires_at_ms=ticket.expires_at_ms,
            )
            <= 0
        ):
            raise _capacity_unavailable("GPU card admission queue timed out")
        current = await _read_fifo_position(
            store,
            resource_id,
            backend_id=backend_id,
            ticket_id=ticket.ticket_id,
            card_queue=True,
        )
        if current.status == "missing":
            raise _capacity_unavailable("GPU card admission queue timed out")
        if current.status != "queued":
            raise _map_queue_rejection(current, card_queue=True)
        if (
            current.ticket_id != ticket.ticket_id
            or current.position is None
            or current.position <= 0
        ):
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission queue returned an invalid ticket",
            )
        position = current.position
    if (
        _queue_remaining_seconds(
            clock,
            deadline=deadline,
            expires_at_ms=ticket.expires_at_ms,
        )
        <= 0
    ):
        raise _capacity_unavailable("GPU card admission queue timed out")


async def _call_admit_exact(
    store: GPUArbiterStore,
    resource_id: str,
    **kwargs,
) -> GPUAdmissionResult:
    try:
        return await store.admit(resource_id, **kwargs)
    except Exception:
        try:
            result = await store.admit(resource_id, **kwargs)
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission result is uncertain",
            ) from exc
        if not result.admitted:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission result is uncertain",
            )
        return result


async def _admit_resident_with_fifo(
    store: GPUArbiterStore,
    clock: GPUDispatchClock,
    resource_id: str,
    *,
    backend_id: str,
    membership_epoch: int,
    owner_id: str,
    admission_kwargs: dict,
    timeout_ms: int,
    poll_interval_seconds: float,
) -> GPUAdmissionResult:
    admission = await _call_admit_exact(
        store,
        resource_id,
        **admission_kwargs,
    )
    if admission.status not in {"concurrency_saturated", "concurrency_queued"}:
        return admission

    ticket_id = f"backend:{uuid.uuid4()}"
    ticket_may_exist = True
    deadline = clock.monotonic() + timeout_ms / 1000
    try:
        ticket = await _enqueue_fifo_ticket(
            store,
            clock,
            resource_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            ticket_id=ticket_id,
            owner_id=owner_id,
            deadline=deadline,
            card_queue=False,
        )
        assert ticket.expires_at_ms is not None
        while True:
            remaining = _queue_remaining_seconds(
                clock,
                deadline=deadline,
                expires_at_ms=ticket.expires_at_ms,
            )
            if remaining <= 0:
                return admission
            admission = await _call_admit_exact(
                store,
                resource_id,
                **admission_kwargs,
                backend_ticket_id=ticket_id,
            )
            if admission.admitted:
                ticket_may_exist = False
                return admission
            if admission.status not in {
                "concurrency_saturated",
                "concurrency_queued",
            }:
                return admission
            remaining = _queue_remaining_seconds(
                clock,
                deadline=deadline,
                expires_at_ms=ticket.expires_at_ms,
            )
            if remaining <= 0:
                return admission
            await clock.sleep(min(poll_interval_seconds, remaining))
    finally:
        if ticket_may_exist:
            await _await_cancellation_safe(
                _cancel_fifo_ticket(
                    store,
                    resource_id,
                    backend_id=backend_id,
                    ticket_id=ticket_id,
                    owner_id=owner_id,
                    card_queue=False,
                )
            )


async def _read_runtime_subject(
    data_provider: GPUAuthorityDataProvider,
    request: GPUDispatchRequest,
) -> GPUResidentRuntimeSubject:
    async with data_provider.transaction() as data:
        return await read_gpu_resident_runtime_subject(
            data,
            backend_id=request.backend_id,
            gpu_resource_id=request.gpu_resource_id,
        )


async def _read_cold_runtime_subject(
    data_provider: GPUAuthorityDataProvider,
    request: GPUDispatchRequest,
    *,
    expected_challenge: str | None = None,
) -> GPUColdRuntimeSubject:
    async with data_provider.transaction() as data:
        return await read_gpu_cold_runtime_subject(
            data,
            backend_id=request.backend_id,
            gpu_resource_id=request.gpu_resource_id,
            expected_challenge=expected_challenge,
        )


async def _heartbeat_runtime_lease(
    store: GPUArbiterStore,
    subject: GPURuntimeLeaseSubject,
    clock: GPUDispatchClock,
    *,
    lease_id: str,
    owner_id: str,
    heartbeat_ttl_ms: int,
    heartbeat_interval_seconds: float,
    hard_deadline_ms: int,
) -> None:
    while True:
        remaining_ms = hard_deadline_ms - int(clock.time() * 1000)
        if remaining_ms <= 0:
            return
        await clock.sleep(min(heartbeat_interval_seconds, remaining_ms / 1000))
        if int(clock.time() * 1000) >= hard_deadline_ms:
            return
        result = await store.heartbeat_lease(
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            lease_id=lease_id,
            owner_id=owner_id,
            generation=subject.generation,
            heartbeat_ttl_ms=heartbeat_ttl_ms,
        )
        if result.status != "heartbeated":
            raise GPUArbiterStoreError(
                f"GPU runtime lease heartbeat failed: {result.status}"
            )


async def _stop_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - cleanup remains conservative
        logger.warning("gpu_runtime_lease_heartbeat_failed", exc_info=True)


async def _cleanup_runtime_lease(
    store: GPUArbiterStore,
    subject: GPURuntimeLeaseSubject,
    *,
    lease_id: str,
    owner_id: str,
    action: str | None,
    heartbeat_task: asyncio.Task[None] | None,
) -> None:
    await _stop_heartbeat(heartbeat_task)
    if action is not None:
        try:
            if action == "release":
                result = await store.release_lease(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=subject.generation,
                )
                expected_status = "released"
            else:
                result = await store.mark_lease_uncertain(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=subject.generation,
                )
                expected_status = "uncertain"
            if result.status not in {expected_status, "missing"}:
                logger.warning(
                    "gpu_runtime_lease_cleanup_rejected",
                    gpu_arbiter={
                        "backend_id": str(subject.backend_registry_id),
                        "resource_id": subject.gpu_resource_id,
                        "lease_id": lease_id,
                        "action": action,
                        "status": result.status,
                    },
                )
        except Exception:  # noqa: BLE001 - stale lease is the conservative fallback
            logger.warning(
                "gpu_runtime_lease_cleanup_failed",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "lease_id": lease_id,
                    "action": action,
                },
                exc_info=True,
            )
    try:
        await store.aclose()
    except Exception:  # noqa: BLE001 - do not replace the business outcome
        logger.warning("gpu_runtime_store_close_failed", exc_info=True)


async def _await_cancellation_safe(cleanup: Awaitable[None]) -> None:
    """Finish cleanup even if the caller is cancelled repeatedly."""

    task = asyncio.create_task(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if not task.cancelled():
        task.result()
    if cancelled:
        raise asyncio.CancelledError


def _cold_generation_candidate(subject: GPUColdRuntimeSubject) -> str:
    if subject.generation_high_water >= _MAX_POSITIVE_INT64:
        raise _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU runtime generation is exhausted",
        )
    return str(subject.generation_high_water + 1)


def _runtime_hard_ttl_ms(
    config: Settings,
    *,
    heartbeat_ttl_ms: int,
) -> int:
    timeout = config.ml_predict_timeout
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU runtime timeout is invalid",
        )
    hard_ttl_ms = (timeout + _GPU_RUNTIME_HARD_DEADLINE_GRACE_SECONDS) * 1000
    if (
        hard_ttl_ms <= 0
        or hard_ttl_ms > _MAX_REDIS_TTL_MS
        or hard_ttl_ms < heartbeat_ttl_ms
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU runtime hard TTL is invalid",
        )
    return hard_ttl_ms


def _runtime_reconcile_deadline_grace_ms(hard_ttl_ms: int) -> int:
    available_grace_ms = _GPU_RUNTIME_MAX_RECONCILE_HORIZON_MS - hard_ttl_ms
    if available_grace_ms < _GPU_RUNTIME_MIN_RECONCILE_GRACE_MS:
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU runtime timeout leaves no safe reconcile proof window",
        )
    return min(_GPU_RUNTIME_RECONCILE_DEADLINE_GRACE_MS, available_grace_ms)


def _admission_timeout_ms(
    config: Settings,
    *,
    override_seconds: float | None,
) -> int:
    timeout = (
        config.gpu_arbiter_admission_timeout_seconds
        if override_seconds is None
        else override_seconds
    )
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _GPU_RUNTIME_MAX_ADMISSION_TIMEOUT_SECONDS
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU admission timeout is invalid",
        )
    timeout_ms = max(1, int(timeout * 1000))
    if timeout_ms > _MAX_REDIS_TTL_MS:
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU admission timeout is invalid",
        )
    return timeout_ms


def _residency_cooldown_ms(
    config: Settings,
    *,
    override_seconds: float | None,
) -> int:
    cooldown = (
        config.gpu_arbiter_residency_cooldown_seconds
        if override_seconds is None
        else override_seconds
    )
    if (
        not isinstance(cooldown, (int, float))
        or isinstance(cooldown, bool)
        or not math.isfinite(cooldown)
        or cooldown <= 0
        or cooldown > _GPU_RUNTIME_MAX_RESIDENCY_COOLDOWN_SECONDS
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.CONFIG_INVALID,
            "GPU residency cooldown is invalid",
        )
    return max(1, int(cooldown * 1000))


def _map_cold_owner_rejection(status: str) -> GPUArbiterDispatchError:
    if status in {"busy", "active_leases"}:
        return _dispatch_error(
            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
            "GPU cold admission is busy",
        )
    if status in {
        "missing",
        "owner_mismatch",
        "operation_mismatch",
        "not_ready",
        "config_mismatch",
        "stale_generation",
        "invalid_transition",
    }:
        return _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU cold runtime is not ready",
        )
    return _dispatch_error(
        GPUArbiterErrorCode.UNAVAILABLE,
        "GPU cold admission owner failed",
    )


async def _release_cold_intent(
    store: GPUArbiterStore,
    subject: GPUColdRuntimeSubject,
    *,
    owner_id: str,
    generation: str,
) -> None:
    try:
        result = await store.release_cold_admission_owner(
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            owner_id=owner_id,
            generation=generation,
        )
        if result.status not in {"released", "missing"}:
            logger.warning(
                "gpu_cold_intent_cleanup_rejected",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "owner_id": owner_id,
                    "generation": generation,
                    "status": result.status,
                },
            )
    except Exception:  # noqa: BLE001 - TTL remains the conservative fallback
        logger.warning(
            "gpu_cold_intent_cleanup_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "owner_id": owner_id,
                "generation": generation,
            },
            exc_info=True,
        )


async def _refresh_cold_terminal_health(
    health_refresher: GPUHealthRefresher,
    subject: GPUPreparedColdRuntimeSubject,
) -> str:
    challenge = secrets.token_hex(32)
    try:
        await health_refresher(
            subject.backend_registry_id,
            challenge,
        )
    except Exception:  # noqa: BLE001 - the locked classifier falls back to Unknown
        logger.warning(
            "gpu_cold_terminal_health_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
            },
            exc_info=True,
        )
    return challenge


async def _finalize_cold_runtime(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    subject: GPUColdRuntimeSubject,
    *,
    health_refresher: GPUHealthRefresher,
    generation: str,
    lease_id: str,
    owner_id: str,
    intent_may_exist: bool,
    lease_may_exist: bool,
    grant_exposed: bool,
    request_not_sent: bool,
    response_received: bool,
    prepared_subject: GPUPreparedColdRuntimeSubject | None,
    heartbeat_task: asyncio.Task[None] | None,
    card_ticket_id: str,
    card_ticket_may_exist: bool,
    resident_cooldown_ms: int,
) -> None:
    if lease_may_exist:
        target_state = GPUAllocationState.UNLOADED
        terminal_confirmed = False
        terminal_result: GPUColdTerminalCommitResult | None = None
        if grant_exposed and not request_not_sent and prepared_subject is not None:
            challenge = None
            if response_received:
                challenge = await _refresh_cold_terminal_health(
                    health_refresher,
                    prepared_subject,
                )
            try:
                terminal_result = await commit_gpu_cold_terminal_from_health(
                    data_provider,
                    store,
                    prepared_subject,
                    challenge=challenge,
                    lease_id=lease_id,
                    owner_id=owner_id,
                    resident_cooldown_ms=resident_cooldown_ms,
                )
                target_state = terminal_result.state
                terminal_confirmed = terminal_result.status == "finalized"
                if not terminal_confirmed:
                    logger.warning(
                        "gpu_cold_terminal_commit_rejected",
                        gpu_arbiter={
                            "backend_id": str(subject.backend_registry_id),
                            "resource_id": subject.gpu_resource_id,
                            "lease_id": lease_id,
                            "generation": generation,
                            "status": terminal_result.status,
                            "reason": terminal_result.reason,
                        },
                    )
            except Exception:  # noqa: BLE001 - uncertain lease remains conservative
                logger.warning(
                    "gpu_cold_terminal_commit_failed",
                    gpu_arbiter={
                        "backend_id": str(subject.backend_registry_id),
                        "resource_id": subject.gpu_resource_id,
                        "lease_id": lease_id,
                        "generation": generation,
                    },
                    exc_info=True,
                )
        else:
            target_state = (
                GPUAllocationState.UNKNOWN
                if grant_exposed and not request_not_sent
                else GPUAllocationState.UNLOADED
            )
            try:
                transition = await store.finalize_cold_allocation(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    expected_generation=generation,
                    request_lease_id=lease_id,
                    request_owner_id=owner_id,
                    target_state=target_state,
                    target_evictable=False,
                    resident_cooldown_ms=0,
                )
                terminal_confirmed = transition.status == "transitioned"
                if not terminal_confirmed:
                    logger.warning(
                        "gpu_cold_allocation_cleanup_rejected",
                        gpu_arbiter={
                            "backend_id": str(subject.backend_registry_id),
                            "resource_id": subject.gpu_resource_id,
                            "lease_id": lease_id,
                            "generation": generation,
                            "target_state": target_state.value,
                            "status": transition.status,
                        },
                    )
            except Exception:  # noqa: BLE001 - uncertain lease remains conservative
                logger.warning(
                    "gpu_cold_allocation_cleanup_failed",
                    gpu_arbiter={
                        "backend_id": str(subject.backend_registry_id),
                        "resource_id": subject.gpu_resource_id,
                        "lease_id": lease_id,
                        "generation": generation,
                        "target_state": target_state.value,
                    },
                    exc_info=True,
                )
        await _stop_heartbeat(heartbeat_task)
        try:
            if terminal_confirmed and target_state is not GPUAllocationState.UNKNOWN:
                cleanup = await store.release_lease(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=generation,
                )
                expected_status = "released"
            else:
                cleanup = await store.mark_lease_uncertain(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=generation,
                )
                expected_status = "uncertain"
            if cleanup.status not in {expected_status, "missing"}:
                logger.warning(
                    "gpu_cold_lease_cleanup_rejected",
                    gpu_arbiter={
                        "backend_id": str(subject.backend_registry_id),
                        "resource_id": subject.gpu_resource_id,
                        "lease_id": lease_id,
                        "generation": generation,
                        "status": cleanup.status,
                    },
                )
        except Exception:  # noqa: BLE001 - repair owns conservative residue
            logger.warning(
                "gpu_cold_lease_cleanup_failed",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "lease_id": lease_id,
                    "generation": generation,
                },
                exc_info=True,
            )
    if intent_may_exist:
        await _release_cold_intent(
            store,
            subject,
            owner_id=owner_id,
            generation=generation,
        )
    if card_ticket_may_exist:
        await _cancel_fifo_ticket(
            store,
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            ticket_id=card_ticket_id,
            owner_id=owner_id,
            card_queue=True,
        )
    try:
        await store.aclose()
    except Exception:  # noqa: BLE001 - do not replace the business outcome
        logger.warning("gpu_runtime_store_close_failed", exc_info=True)


def _capacity_unavailable(message: str) -> GPUArbiterDispatchError:
    return _dispatch_error(
        GPUArbiterErrorCode.CAPACITY_UNAVAILABLE,
        message,
        retry_after_s=_GPU_RUNTIME_RETRY_AFTER_SECONDS,
    )


def _eviction_victim_hint(
    snapshot: GPUCardSnapshot,
    requester: GPUColdRuntimeSubject,
    *,
    allow_busy: bool,
) -> GPUAllocation | None:
    if not snapshot.ready:
        raise _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU card ledger is not ready",
        )
    requester_allocation = next(
        (
            allocation
            for allocation in snapshot.allocations
            if allocation.backend_id == str(requester.backend_registry_id)
        ),
        None,
    )
    if requester_allocation is not None and requester_allocation.state not in {
        GPUAllocationState.UNLOADED,
        GPUAllocationState.CPU_FALLBACK,
    }:
        raise _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU cold runtime already has an active allocation",
        )
    free_mb = snapshot.allocatable_mb - snapshot.committed_mb
    if requester.budget_mb <= free_mb:
        return None
    if requester.budget_mb > snapshot.allocatable_mb:
        raise _capacity_unavailable("GPU request exceeds allocatable card memory")
    if snapshot.transition_present:
        raise _capacity_unavailable("GPU card transition is already in progress")

    leased_backends = {lease.backend_id for lease in snapshot.leases}
    candidates = sorted(
        (
            allocation
            for allocation in snapshot.allocations
            if allocation.backend_id != str(requester.backend_registry_id)
            and allocation.backend_id in snapshot.active_backend_ids
            and allocation.state is GPUAllocationState.RESIDENT
            and allocation.evictable
            and allocation.generation is not None
            and allocation.eviction_priority <= requester.eviction_priority
            and (allow_busy or allocation.backend_id not in leased_backends)
        ),
        key=lambda allocation: (
            allocation.eviction_priority,
            allocation.last_used_at_ms,
            allocation.backend_id,
        ),
    )
    shortfall_mb = requester.budget_mb - free_mb
    if sum(item.budget_mb for item in candidates) < shortfall_mb:
        raise _capacity_unavailable(_GPU_RUNTIME_NO_SAFE_IDLE_VICTIM)
    ready = tuple(
        allocation
        for allocation in candidates
        if allocation.not_evict_before_ms <= snapshot.observed_at_ms
    )
    if sum(item.budget_mb for item in ready) >= shortfall_mb:
        return ready[0]

    available_mb = 0
    retry_at_ms = snapshot.observed_at_ms
    for allocation in sorted(
        candidates,
        key=lambda item: (
            item.not_evict_before_ms,
            item.eviction_priority,
            item.last_used_at_ms,
            item.backend_id,
        ),
    ):
        available_mb += allocation.budget_mb
        retry_at_ms = max(retry_at_ms, allocation.not_evict_before_ms)
        if available_mb >= shortfall_mb:
            raise _GPUVictimCooldownActive(retry_at_ms)
    raise _capacity_unavailable(_GPU_RUNTIME_NO_SAFE_IDLE_VICTIM)


def _idle_victim_hint(
    snapshot: GPUCardSnapshot,
    requester: GPUColdRuntimeSubject,
) -> GPUAllocation | None:
    return _eviction_victim_hint(snapshot, requester, allow_busy=False)


async def _heartbeat_eviction_owner(
    store: GPUArbiterStore,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    clock: GPUDispatchClock,
    *,
    owner_id: str,
    heartbeat_ttl_ms: int,
    heartbeat_interval_seconds: float,
    hard_deadline_ms: int,
) -> None:
    while True:
        remaining_ms = hard_deadline_ms - int(clock.time() * 1000)
        if remaining_ms <= 0:
            raise GPUArbiterStoreError("GPU eviction owner hard deadline reached")
        await clock.sleep(min(heartbeat_interval_seconds, remaining_ms / 1000))
        if int(clock.time() * 1000) >= hard_deadline_ms:
            raise GPUArbiterStoreError("GPU eviction owner hard deadline reached")
        result = await store.heartbeat_transition_owner(
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            owner_id=owner_id,
            generation=subject.generation,
            operation=GPU_EVICTION_OPERATION,
            ttl_ms=heartbeat_ttl_ms,
        )
        if result.status != "renewed":
            raise GPUArbiterStoreError(
                f"GPU eviction owner heartbeat failed: {result.status}"
            )


def _raise_if_heartbeat_failed(task: asyncio.Task[None] | None) -> None:
    if task is not None and task.done():
        task.result()


async def _stop_eviction_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - terminal CAS remains authoritative
        logger.warning("gpu_eviction_owner_heartbeat_failed", exc_info=True)


async def _release_eviction_owner(
    store: GPUArbiterStore,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    owner_id: str,
) -> None:
    try:
        release_kwargs = {
            "backend_id": str(subject.backend_registry_id),
            "owner_id": owner_id,
            "generation": subject.generation,
            "operation": GPU_EVICTION_OPERATION,
        }
        try:
            released = await store.release_transition_owner(
                subject.gpu_resource_id,
                **release_kwargs,
            )
        except Exception:
            released = await store.release_transition_owner(
                subject.gpu_resource_id,
                **release_kwargs,
            )
        if released.status not in {"released", "missing"}:
            logger.warning(
                "gpu_eviction_owner_release_rejected",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "generation": subject.generation,
                    "owner_id": owner_id,
                    "status": released.status,
                },
            )
    except Exception:  # noqa: BLE001 - owner TTL is the conservative fallback
        logger.warning(
            "gpu_eviction_owner_release_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
                "owner_id": owner_id,
            },
            exc_info=True,
        )


async def _refresh_eviction_health(
    health_refresher: GPUHealthRefresher,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    phase: str,
) -> str | None:
    challenge = secrets.token_hex(32)
    try:
        if await health_refresher(subject.backend_registry_id, challenge):
            return challenge
    except Exception:  # noqa: BLE001 - phase commit converges to Unknown
        logger.warning(
            "gpu_eviction_health_refresh_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
                "phase": phase,
            },
            exc_info=True,
        )
    return None


def _eviction_ack_identity_matches(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    generation: str,
    residency: BackendResidency,
) -> bool:
    identity = residency.identity
    return bool(
        generation == subject.generation
        and residency.generation == subject.generation
        and residency.boot_id == subject.boot_id
        and residency.control_epoch == subject.control_epoch
        and identity is not None
        and identity.backend_registry_id == str(subject.backend_registry_id)
        and identity.gpu_resource_id == subject.gpu_resource_id
    )


def _drain_ack_ready(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    response: DrainTransitionResponse,
) -> bool:
    return bool(
        response.draining
        and response.ready_to_unload
        and response.active_requests == 0
        and _eviction_ack_identity_matches(
            subject,
            generation=response.generation,
            residency=response.residency,
        )
    )


def _busy_drain_ack_matches(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    response: DrainTransitionResponse,
) -> bool:
    residency = response.residency
    pool_residencies = tuple(
        residency.pools[pool_id].resident for pool_id in sorted(residency.pools)
    )
    ready = (
        residency.active_requests == 0
        and residency.builders == 0
        and residency.borrowers == 0
    )
    return bool(
        response.ok is True
        and response.draining
        and response.active_requests == residency.active_requests
        and response.ready_to_unload is ready
        and residency.state is LifecycleState.DRAINING
        and residency.gpu_loaded is True
        and residency.draining
        and not residency.evictable
        and _eviction_ack_identity_matches(
            subject,
            generation=response.generation,
            residency=residency,
        )
        and tuple(sorted(residency.pools)) == subject.pool_ids
        and all(item is not None for item in pool_residencies)
        and any(item is True for item in pool_residencies)
    )


def _unload_ack_matches(
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    response: ManagedUnloadResponse,
) -> bool:
    return bool(
        response.unloaded
        and _eviction_ack_identity_matches(
            subject,
            generation=response.generation,
            residency=response.residency,
        )
    )


def _eviction_cancel_ack_matches(
    subject: GPUPreparedEvictionCancelRuntimeSubject,
    response: DrainTransitionResponse,
) -> bool:
    residency = response.residency
    identity = residency.identity
    pool_residencies = tuple(
        residency.pools[pool_id].resident for pool_id in sorted(residency.pools)
    )
    return bool(
        response.ok is True
        and response.generation == subject.generation
        and not response.draining
        and not response.ready_to_unload
        and residency.state is LifecycleState.RESIDENT
        and residency.gpu_loaded is True
        and not residency.draining
        and residency.evictable
        and residency.generation == subject.generation
        and residency.boot_id == subject.boot_id
        and residency.control_epoch == subject.control_epoch
        and identity is not None
        and identity.backend_registry_id == str(subject.backend_registry_id)
        and identity.gpu_resource_id == subject.gpu_resource_id
        and tuple(sorted(residency.pools)) == subject.pool_ids
        and all(item is not None for item in pool_residencies)
        and any(item is True for item in pool_residencies)
    )


async def _call_eviction_lifecycle(
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    backend_id: uuid.UUID,
    operation: Literal["drain", "cancel_drain", "unload"],
    *,
    clock: GPUDispatchClock,
    generation: str,
    admission_token: str,
    hard_deadline_ms: int,
) -> object | None:
    if operation not in {"drain", "cancel_drain", "unload"}:
        raise ValueError("GPU eviction lifecycle operation is invalid")
    for attempt in range(2):
        remaining_seconds = (hard_deadline_ms - int(clock.time() * 1000)) / 1000
        if remaining_seconds <= 0:
            return None
        # Each control send owns a fresh durable routing attempt, including retries.
        request_id = uuid.uuid4()
        grant = GPUDispatchGrant(
            generation=generation,
            admission_token=admission_token,
        )
        try:
            async with asyncio.timeout(remaining_seconds):
                async with backend_transport.control_binding(
                    backend_id,
                    request_id=request_id,
                    operation=operation,
                ) as binding:
                    endpoint = await endpoint_source.open_owned(
                        backend_id,
                        request_id=request_id,
                        purpose="gpu_control",
                        binding=binding,
                    )
                    if operation == "drain":
                        return await backend_transport.lifecycle_drain(
                            endpoint,
                            grant,
                            request_id=request_id,
                            timeout_seconds=remaining_seconds,
                        )
                    if operation == "cancel_drain":
                        return await backend_transport.lifecycle_cancel_drain(
                            endpoint,
                            grant,
                            request_id=request_id,
                            timeout_seconds=remaining_seconds,
                        )
                    return await backend_transport.lifecycle_unload(
                        endpoint,
                        grant,
                        request_id=request_id,
                        timeout_seconds=remaining_seconds,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - outcome decides exact retry eligibility
            response_received = (
                grant.outcome is not None and grant.outcome.kind == "response_received"
            )
            if response_received:
                return None
            if attempt == 0:
                continue
    return None


def _sign_eviction_grants(
    signer: GPUAdmissionTokenSigner,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    clock: GPUDispatchClock,
    owner_id: str,
    owner_hard_deadline_ms: int,
) -> tuple[str, str]:
    token_exp = min(
        int(subject.token_expires_at.timestamp()),
        owner_hard_deadline_ms // 1000,
    )
    if (
        token_exp - max(int(clock.time()), int(subject.db_now.timestamp()))
        < _GPU_RUNTIME_MIN_TOKEN_WINDOW_SECONDS
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU eviction owner has no safe token delivery window",
        )
    common = {
        "backend_registry_id": str(subject.backend_registry_id),
        "gpu_resource_id": subject.gpu_resource_id,
        "boot_id": subject.boot_id,
        "generation": subject.generation,
        "control_epoch": subject.control_epoch,
        "exp": token_exp,
        "owner": owner_id,
        "operation": GPU_EVICTION_OPERATION,
    }
    drain_token = signer.sign(
        AdmissionTokenClaims(
            **common,
            scope=AdmissionScope.DRAIN,
            jti=f"transition:{uuid.uuid4()}",
        )
    )
    unload_token = signer.sign(
        AdmissionTokenClaims(
            **common,
            scope=AdmissionScope.UNLOAD,
            jti=f"transition:{uuid.uuid4()}",
        )
    )
    return drain_token, unload_token


def _sign_eviction_cancel_grant(
    signer: GPUAdmissionTokenSigner,
    subject: GPUPreparedEvictionCancelRuntimeSubject,
    clock: GPUDispatchClock,
) -> str:
    if subject.operation != GPU_EVICTION_OPERATION:
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU eviction cancel operation is invalid",
        )
    token_exp = min(
        int(subject.token_expires_at.timestamp()),
        subject.owner_hard_deadline_ms // 1000,
    )
    if (
        token_exp - max(int(clock.time()), int(subject.db_now.timestamp()))
        < _GPU_RUNTIME_MIN_TOKEN_WINDOW_SECONDS
    ):
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU eviction cancel owner has no safe token delivery window",
        )
    return signer.sign(
        AdmissionTokenClaims(
            backend_registry_id=str(subject.backend_registry_id),
            gpu_resource_id=subject.gpu_resource_id,
            boot_id=subject.boot_id,
            generation=subject.generation,
            control_epoch=subject.control_epoch,
            scope=AdmissionScope.RESUME,
            jti=subject.jti,
            exp=token_exp,
            owner=subject.owner_id,
            operation=subject.operation,
        )
    )


async def _read_busy_eviction_drain_health(
    data_provider: GPUAuthorityDataProvider,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    challenge: str,
) -> GPUEvictionDrainHealth:
    async with data_provider.transaction() as data:
        return await read_gpu_eviction_drain_health(
            data,
            subject,
            challenge=challenge,
        )


async def _wait_for_busy_eviction_ready(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    clock: GPUDispatchClock,
    *,
    health_refresher: GPUHealthRefresher,
    heartbeat_task: asyncio.Task[None] | None,
    queue_deadline: float,
    ticket_expires_at_ms: int,
    work_hard_deadline_ms: int,
    poll_interval_seconds: float,
) -> str:
    while True:
        _raise_if_heartbeat_failed(heartbeat_task)
        queue_remaining_ms = _eviction_work_remaining_ttl_ms(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=ticket_expires_at_ms,
        )
        work_remaining_ms = work_hard_deadline_ms - int(clock.time() * 1000)
        if work_remaining_ms <= 0:
            raise _capacity_unavailable("GPU busy eviction work deadline reached")
        remaining_ms = min(queue_remaining_ms, work_remaining_ms)
        try:
            async with asyncio.timeout(remaining_ms / 1000):
                try:
                    snapshot = await store.snapshot(subject.gpu_resource_id)
                except Exception as exc:
                    raise _dispatch_error(
                        GPUArbiterErrorCode.UNAVAILABLE,
                        "GPU busy eviction ledger is unavailable",
                    ) from exc
                allocation = next(
                    (
                        item
                        for item in snapshot.allocations
                        if item.backend_id == str(subject.backend_registry_id)
                    ),
                    None,
                )
                if (
                    not snapshot.transition_present
                    or allocation is None
                    or allocation.state is not GPUAllocationState.DRAINING
                    or allocation.generation != subject.generation
                ):
                    raise _capacity_unavailable("GPU busy victim drain state changed")
                redis_ready = not any(
                    lease.backend_id == str(subject.backend_registry_id)
                    for lease in snapshot.leases
                )
                challenge = await _refresh_eviction_health(
                    health_refresher,
                    subject,
                    phase="busy_wait",
                )
                if challenge is None:
                    raise _capacity_unavailable("GPU busy victim health is unavailable")
                try:
                    health = await _read_busy_eviction_drain_health(
                        data_provider,
                        subject,
                        challenge=challenge,
                    )
                except Exception as exc:
                    raise _dispatch_error(
                        GPUArbiterErrorCode.UNAVAILABLE,
                        "GPU busy victim drain proof is unavailable",
                    ) from exc
                if health.status == "uncertain":
                    raise _capacity_unavailable(
                        "GPU busy victim drain proof is uncertain"
                    )
                if redis_ready and health.status == "ready_to_unload":
                    return challenge
                await clock.sleep(
                    min(
                        poll_interval_seconds,
                        remaining_ms / 1000,
                    )
                )
        except TimeoutError as exc:
            raise _capacity_unavailable(
                "GPU busy eviction work deadline reached"
            ) from exc


async def _cancel_busy_eviction(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    store: GPUArbiterStore,
    signer: GPUAdmissionTokenSigner,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    owner_id: str,
    owner_hard_deadline_ms: int,
) -> tuple[GPUEvictionCommitResult | None, str | None]:
    prepare_kwargs = {
        "owner_id": owner_id,
        "owner_hard_deadline_ms": owner_hard_deadline_ms,
        "token_expires_at": datetime.fromtimestamp(
            owner_hard_deadline_ms / 1000,
            UTC,
        ),
    }
    try:
        try:
            cancel_subject = await prepare_gpu_eviction_cancel_runtime_generation(
                data_provider,
                subject,
                **prepare_kwargs,
            )
        except Exception:
            cancel_subject = await prepare_gpu_eviction_cancel_runtime_generation(
                data_provider,
                subject,
                **prepare_kwargs,
            )
        cancel_token = _sign_eviction_cancel_grant(signer, cancel_subject, clock)
    except Exception:  # noqa: BLE001 - open owner expires into proof reset
        logger.warning(
            "gpu_eviction_cancel_prepare_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
                "owner_id": owner_id,
            },
            exc_info=True,
        )
        return None, None

    arm_kwargs = {
        "backend_id": str(subject.backend_registry_id),
        "expected_generation": subject.generation,
        "transition_owner_id": owner_id,
    }
    try:
        try:
            armed = await store.arm_eviction_cancel(
                subject.gpu_resource_id,
                **arm_kwargs,
            )
        except Exception:
            armed = await store.arm_eviction_cancel(
                subject.gpu_resource_id,
                **arm_kwargs,
            )
    except Exception:  # noqa: BLE001 - uncertain branch forbids RESUME
        logger.warning(
            "gpu_eviction_cancel_arm_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
                "owner_id": owner_id,
            },
            exc_info=True,
        )
        return None, None
    if armed.status != "armed" or armed.branch != "cancel":
        if armed.status == "branch_conflict" and armed.branch == "unload":
            return None, "unload"
        logger.warning(
            "gpu_eviction_cancel_arm_rejected",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "generation": subject.generation,
                "owner_id": owner_id,
                "status": armed.status,
                "branch": armed.branch,
            },
        )
        return None, None

    cancel_response = await _call_eviction_lifecycle(
        endpoint_source,
        backend_transport,
        subject.backend_registry_id,
        "cancel_drain",
        clock=clock,
        generation=cancel_subject.generation,
        admission_token=cancel_token,
        hard_deadline_ms=owner_hard_deadline_ms,
    )
    ack_confirmed = isinstance(
        cancel_response,
        DrainTransitionResponse,
    ) and _eviction_cancel_ack_matches(cancel_subject, cancel_response)
    challenge = None
    if ack_confirmed:
        challenge = await _refresh_eviction_health(
            health_refresher,
            subject,
            phase="cancel",
        )
    try:
        try:
            result = await commit_gpu_eviction_cancel_from_health(
                data_provider,
                store,
                cancel_subject,
                ack_confirmed=ack_confirmed,
                challenge=challenge,
            )
        except Exception:
            result = await commit_gpu_eviction_cancel_from_health(
                data_provider,
                store,
                cancel_subject,
                ack_confirmed=ack_confirmed,
                challenge=challenge,
            )
    except Exception:  # noqa: BLE001 - frozen branch awaits proof reset
        logger.warning(
            "gpu_eviction_cancel_commit_failed",
            gpu_arbiter={
                "backend_id": str(subject.backend_registry_id),
                "resource_id": subject.gpu_resource_id,
                "drain_generation": subject.generation,
                "cancel_generation": cancel_subject.generation,
                "owner_id": owner_id,
            },
            exc_info=True,
        )
        return None, "cancel"
    return result, "cancel"


async def _finish_busy_eviction_transition(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    store: GPUArbiterStore,
    signer: GPUAdmissionTokenSigner,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    owner_id: str,
    owner_hard_deadline_ms: int,
    phase: str,
    terminal_result: GPUEvictionCommitResult | None,
    replay_challenge: str | None,
    heartbeat_task: asyncio.Task[None] | None,
) -> None:
    if phase == "unload" or (
        terminal_result is not None
        and terminal_result.status == "finalized"
        and terminal_result.state is GPUAllocationState.UNLOADING
    ):
        await _finish_eviction_transition(
            data_provider,
            store,
            subject,
            owner_id=owner_id,
            phase="unload",
            terminal_result=terminal_result,
            replay_challenge=replay_challenge,
            heartbeat_task=heartbeat_task,
        )
        return
    if (
        terminal_result is not None
        and terminal_result.status == "finalized"
        and terminal_result.state
        in {
            GPUAllocationState.RESIDENT,
            GPUAllocationState.UNKNOWN,
            GPUAllocationState.UNLOADED,
        }
    ):
        await _stop_eviction_heartbeat(heartbeat_task)
        await _release_eviction_owner(store, subject, owner_id=owner_id)
        return

    if replay_challenge is not None:
        try:
            replayed = await commit_gpu_eviction_phase_from_health(
                data_provider,
                store,
                subject,
                phase="drain",
                challenge=replay_challenge,
                owner_id=owner_id,
            )
            if (
                replayed.status == "finalized"
                and replayed.state is GPUAllocationState.UNLOADING
            ):
                await _finish_eviction_transition(
                    data_provider,
                    store,
                    subject,
                    owner_id=owner_id,
                    phase="unload",
                    terminal_result=replayed,
                    replay_challenge=None,
                    heartbeat_task=heartbeat_task,
                )
                return
        except Exception:  # noqa: BLE001 - cancel branch remains the fallback
            logger.warning(
                "gpu_busy_eviction_drain_replay_failed",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "generation": subject.generation,
                    "owner_id": owner_id,
                },
                exc_info=True,
            )

    cancel_result, branch = await _cancel_busy_eviction(
        data_provider,
        endpoint_source,
        backend_transport,
        store,
        signer,
        subject,
        health_refresher=health_refresher,
        clock=clock,
        owner_id=owner_id,
        owner_hard_deadline_ms=owner_hard_deadline_ms,
    )
    if branch == "unload":
        await _finish_eviction_transition(
            data_provider,
            store,
            subject,
            owner_id=owner_id,
            phase="unload",
            terminal_result=None,
            replay_challenge=None,
            heartbeat_task=heartbeat_task,
        )
        return
    await _stop_eviction_heartbeat(heartbeat_task)
    if (
        cancel_result is not None
        and cancel_result.status == "finalized"
        and cancel_result.state
        in {GPUAllocationState.RESIDENT, GPUAllocationState.UNKNOWN}
    ):
        await _release_eviction_owner(store, subject, owner_id=owner_id)


async def _finish_eviction_transition(
    data_provider: GPUAuthorityDataProvider,
    store: GPUArbiterStore,
    subject: GPUPreparedIdleEvictionRuntimeSubject,
    *,
    owner_id: str,
    phase: str,
    terminal_result: GPUEvictionCommitResult | None,
    replay_challenge: str | None,
    heartbeat_task: asyncio.Task[None] | None,
) -> None:
    confirmed_terminal = bool(
        terminal_result is not None
        and terminal_result.status == "finalized"
        and terminal_result.state
        in {GPUAllocationState.UNLOADED, GPUAllocationState.UNKNOWN}
    )
    cleanup_phase = phase
    if terminal_result is None and replay_challenge is not None:
        try:
            terminal_result = await commit_gpu_eviction_phase_from_health(
                data_provider,
                store,
                subject,
                phase="unload" if phase == "unload" else "drain",
                challenge=replay_challenge,
                owner_id=owner_id,
            )
            confirmed_terminal = bool(
                terminal_result.status == "finalized"
                and terminal_result.state
                in {GPUAllocationState.UNLOADED, GPUAllocationState.UNKNOWN}
            )
            if (
                terminal_result.status == "finalized"
                and terminal_result.state is GPUAllocationState.UNLOADING
            ):
                cleanup_phase = "unload"
        except Exception:  # noqa: BLE001 - fall back to conservative Unknown
            logger.warning(
                "gpu_eviction_terminal_replay_failed",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "generation": subject.generation,
                    "phase": phase,
                },
                exc_info=True,
            )
    if not confirmed_terminal:
        try:
            terminal_result = await commit_gpu_eviction_phase_from_health(
                data_provider,
                store,
                subject,
                phase="unload" if cleanup_phase == "unload" else "drain",
                challenge=None,
                owner_id=owner_id,
            )
            confirmed_terminal = bool(
                terminal_result.status == "finalized"
                and terminal_result.state is GPUAllocationState.UNKNOWN
            )
        except Exception:  # noqa: BLE001 - repair owns conservative residue
            logger.warning(
                "gpu_eviction_terminal_cleanup_failed",
                gpu_arbiter={
                    "backend_id": str(subject.backend_registry_id),
                    "resource_id": subject.gpu_resource_id,
                    "generation": subject.generation,
                    "phase": phase,
                },
                exc_info=True,
            )
    await _stop_eviction_heartbeat(heartbeat_task)
    if confirmed_terminal:
        await _release_eviction_owner(
            store,
            subject,
            owner_id=owner_id,
        )


async def _evict_one_idle_victim(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    store: GPUArbiterStore,
    signer: GPUAdmissionTokenSigner,
    requester: GPUColdRuntimeSubject,
    victim: GPUAllocation,
    *,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    hard_ttl_ms: int,
    heartbeat_interval_seconds: float,
    queue_deadline: float,
    ticket_expires_at_ms: int,
    requester_card_ticket_id: str | None = None,
    requester_queue_owner_id: str | None = None,
    allow_busy: bool = False,
    poll_interval_seconds: float = _GPU_RUNTIME_QUEUE_POLL_INTERVAL_SECONDS,
) -> str:
    initial_queue_work_ttl_ms = _eviction_work_remaining_ttl_ms(
        clock=clock,
        deadline=queue_deadline,
        expires_at_ms=ticket_expires_at_ms,
    )
    cancel_horizon_ms = (
        math.ceil(_GPU_RUNTIME_EVICTION_CANCEL_HORIZON_SECONDS * 1000)
        if allow_busy
        else 0
    )
    initial_work_ttl_ms = min(
        hard_ttl_ms,
        initial_queue_work_ttl_ms,
        _MAX_REDIS_TTL_MS - cancel_horizon_ms,
    )
    challenge = secrets.token_hex(32)
    try:
        victim_id = uuid.UUID(victim.backend_id)
        refreshed = await health_refresher(victim_id, challenge)
    except Exception as exc:
        raise _capacity_unavailable("GPU idle victim health is unavailable") from exc
    if not refreshed:
        raise _capacity_unavailable("GPU idle victim health is unavailable")
    try:
        async with data_provider.transaction() as data:
            if allow_busy:
                idle_subject = await read_gpu_busy_eviction_runtime_subject(
                    data,
                    backend_id=victim.backend_id,
                    gpu_resource_id=requester.gpu_resource_id,
                    expected_generation=victim.generation or "",
                    challenge=challenge,
                )
            else:
                idle_subject = await read_gpu_idle_eviction_runtime_subject(
                    data,
                    backend_id=victim.backend_id,
                    gpu_resource_id=requester.gpu_resource_id,
                    expected_generation=victim.generation or "",
                    challenge=challenge,
                )
        _eviction_work_remaining_ttl_ms(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=ticket_expires_at_ms,
        )
        prepared = await prepare_gpu_idle_eviction_runtime_generation(
            data_provider,
            idle_subject,
            token_expires_at=idle_subject.db_now
            + timedelta(
                milliseconds=(initial_work_ttl_ms if allow_busy else hard_ttl_ms)
            ),
        )
    except (
        GPUBusyEvictionRuntimeSubjectError,
        GPUIdleEvictionRuntimeSubjectError,
    ) as exc:
        raise _capacity_unavailable("GPU idle victim changed before eviction") from exc
    except Exception as exc:
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU idle victim generation is unavailable",
        ) from exc

    owner_id = f"evict:{uuid.uuid4()}"
    owner_may_exist = False
    heartbeat_task: asyncio.Task[None] | None = None
    terminal_result: GPUEvictionCommitResult | None = None
    replay_challenge: str | None = None
    owner_hard_deadline_ms: int | None = None
    work_hard_deadline_ms: int | None = None
    phase = "drain"
    try:
        if allow_busy:
            work_ttl_ms = min(
                initial_work_ttl_ms,
                _eviction_work_remaining_ttl_ms(
                    clock=clock,
                    deadline=queue_deadline,
                    expires_at_ms=ticket_expires_at_ms,
                ),
            )
            owner_hard_ttl_ms = work_ttl_ms + cancel_horizon_ms
        else:
            owner_hard_ttl_ms = min(
                hard_ttl_ms,
                _queue_remaining_ttl_ms(
                    clock=clock,
                    deadline=queue_deadline,
                    expires_at_ms=ticket_expires_at_ms,
                ),
            )
            work_ttl_ms = owner_hard_ttl_ms
        owner_ttl_ms = min(_GPU_RUNTIME_COLD_INTENT_TTL_MS, owner_hard_ttl_ms)
        begin_kwargs = {
            "requester_backend_id": str(requester.backend_registry_id),
            "requester_membership_epoch": requester.membership_epoch,
            "requester_budget_mb": requester.budget_mb,
            "requester_eviction_priority": requester.eviction_priority,
            "victim_backend_id": str(prepared.backend_registry_id),
            "victim_membership_epoch": prepared.membership_epoch,
            "victim_expected_generation": prepared.source_generation,
            "victim_next_generation": prepared.generation,
            "owner_id": owner_id,
            "ttl_ms": owner_ttl_ms,
            "hard_ttl_ms": owner_hard_ttl_ms,
            "requester_card_ticket_id": requester_card_ticket_id,
            "requester_queue_owner_id": requester_queue_owner_id,
            "allow_busy": allow_busy,
        }
        begin_uncertain = False
        owner_may_exist = True
        try:
            selected = await store.begin_idle_eviction(
                requester.gpu_resource_id,
                **begin_kwargs,
            )
        except Exception:
            begin_uncertain = True
            selected = await store.begin_idle_eviction(
                requester.gpu_resource_id,
                **begin_kwargs,
            )
        if selected.status != "selected":
            owner_may_exist = begin_uncertain
            if selected.status == "capacity_available":
                return "capacity_available"
            if selected.status == "stale_selection":
                return "stale"
            if selected.status in {
                "capacity_unavailable",
                "card_queued",
                "cooldown_active",
                "victim_busy",
                "transition_in_progress",
            }:
                raise _capacity_unavailable("GPU idle victim is no longer available")
            if selected.status in {"not_ready", "config_mismatch"}:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU eviction selection is not ready",
                )
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU eviction selection failed",
            )
        if (
            selected.victim_backend_id != str(prepared.backend_registry_id)
            or selected.victim_generation != prepared.generation
            or selected.victim_budget_mb != prepared.budget_mb
            or selected.owner_id != owner_id
            or selected.owner_expires_at_ms is None
            or selected.owner_hard_deadline_ms is None
            or selected.owner_expires_at_ms > selected.owner_hard_deadline_ms
        ):
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU eviction selection receipt is invalid",
            )
        owner_hard_deadline_ms = selected.owner_hard_deadline_ms
        work_hard_deadline_ms = owner_hard_deadline_ms - cancel_horizon_ms
        if work_hard_deadline_ms <= int(clock.time() * 1000):
            raise _capacity_unavailable("GPU eviction work deadline reached")

        try:
            drain_token, unload_token = _sign_eviction_grants(
                signer,
                prepared,
                clock=clock,
                owner_id=owner_id,
                owner_hard_deadline_ms=work_hard_deadline_ms,
            )
        except GPUArbiterDispatchError:
            raise
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU eviction token signing failed",
            ) from exc
        first_heartbeat = await store.heartbeat_transition_owner(
            prepared.gpu_resource_id,
            backend_id=str(prepared.backend_registry_id),
            owner_id=owner_id,
            generation=prepared.generation,
            operation=GPU_EVICTION_OPERATION,
            ttl_ms=owner_ttl_ms,
        )
        if first_heartbeat.status != "renewed":
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU eviction owner heartbeat failed",
            )
        heartbeat_task = asyncio.create_task(
            _heartbeat_eviction_owner(
                store,
                prepared,
                clock,
                owner_id=owner_id,
                heartbeat_ttl_ms=owner_ttl_ms,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                hard_deadline_ms=selected.owner_hard_deadline_ms,
            ),
            name=f"gpu-eviction-heartbeat:{owner_id}",
        )
        drain_response = await _call_eviction_lifecycle(
            endpoint_source,
            backend_transport,
            prepared.backend_registry_id,
            "drain",
            clock=clock,
            generation=prepared.generation,
            admission_token=drain_token,
            hard_deadline_ms=work_hard_deadline_ms,
        )
        drain_ack_matches = isinstance(
            drain_response,
            DrainTransitionResponse,
        ) and (
            _busy_drain_ack_matches(prepared, drain_response)
            if allow_busy
            else _drain_ack_ready(prepared, drain_response)
        )
        if not drain_ack_matches:
            if not allow_busy:
                terminal_result = await commit_gpu_eviction_phase_from_health(
                    data_provider,
                    store,
                    prepared,
                    phase="drain",
                    challenge=None,
                    owner_id=owner_id,
                )
            raise _capacity_unavailable("GPU idle victim did not acknowledge drain")

        if allow_busy:
            drain_challenge = await _wait_for_busy_eviction_ready(
                data_provider,
                store,
                prepared,
                clock,
                health_refresher=health_refresher,
                heartbeat_task=heartbeat_task,
                queue_deadline=queue_deadline,
                ticket_expires_at_ms=ticket_expires_at_ms,
                work_hard_deadline_ms=work_hard_deadline_ms,
                poll_interval_seconds=poll_interval_seconds,
            )
        else:
            drain_challenge = await _refresh_eviction_health(
                health_refresher,
                prepared,
                phase="drain",
            )
        if allow_busy and int(clock.time() * 1000) >= work_hard_deadline_ms:
            raise _capacity_unavailable("GPU busy eviction work deadline reached")
        replay_challenge = drain_challenge
        terminal_result = await commit_gpu_eviction_phase_from_health(
            data_provider,
            store,
            prepared,
            phase="drain",
            challenge=drain_challenge,
            owner_id=owner_id,
        )
        if (
            terminal_result.status != "finalized"
            or terminal_result.state is not GPUAllocationState.UNLOADING
        ):
            raise _capacity_unavailable("GPU idle victim drain proof was rejected")

        phase = "unload"
        terminal_result = None
        replay_challenge = None
        _raise_if_heartbeat_failed(heartbeat_task)
        unload_response = await _call_eviction_lifecycle(
            endpoint_source,
            backend_transport,
            prepared.backend_registry_id,
            "unload",
            clock=clock,
            generation=prepared.generation,
            admission_token=unload_token,
            hard_deadline_ms=work_hard_deadline_ms,
        )
        unload_ack_matches = isinstance(
            unload_response,
            ManagedUnloadResponse,
        ) and _unload_ack_matches(prepared, unload_response)
        unload_challenge = await _refresh_eviction_health(
            health_refresher,
            prepared,
            phase="unload",
        )
        replay_challenge = unload_challenge
        terminal_result = await commit_gpu_eviction_phase_from_health(
            data_provider,
            store,
            prepared,
            phase="unload",
            challenge=unload_challenge,
            owner_id=owner_id,
        )
        if (
            terminal_result.status != "finalized"
            or terminal_result.state is not GPUAllocationState.UNLOADED
        ):
            raise _capacity_unavailable("GPU idle victim unload proof was rejected")
        if not unload_ack_matches:
            logger.info(
                "gpu_eviction_unload_confirmed_by_health",
                gpu_arbiter={
                    "backend_id": str(prepared.backend_registry_id),
                    "resource_id": prepared.gpu_resource_id,
                    "generation": prepared.generation,
                },
            )
        return "unloaded"
    except GPUArbiterDispatchError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU idle victim eviction is unavailable",
        ) from exc
    finally:
        if owner_may_exist:
            if allow_busy and owner_hard_deadline_ms is not None:
                await _await_cancellation_safe(
                    _finish_busy_eviction_transition(
                        data_provider,
                        endpoint_source,
                        backend_transport,
                        store,
                        signer,
                        prepared,
                        health_refresher=health_refresher,
                        clock=clock,
                        owner_id=owner_id,
                        owner_hard_deadline_ms=owner_hard_deadline_ms,
                        phase=phase,
                        terminal_result=terminal_result,
                        replay_challenge=replay_challenge,
                        heartbeat_task=heartbeat_task,
                    )
                )
            elif allow_busy:
                await _await_cancellation_safe(_stop_eviction_heartbeat(heartbeat_task))
            else:
                await _await_cancellation_safe(
                    _finish_eviction_transition(
                        data_provider,
                        store,
                        prepared,
                        owner_id=owner_id,
                        phase=phase,
                        terminal_result=terminal_result,
                        replay_challenge=replay_challenge,
                        heartbeat_task=heartbeat_task,
                    )
                )


async def _ensure_cold_capacity(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    store: GPUArbiterStore,
    signer: GPUAdmissionTokenSigner,
    requester: GPUColdRuntimeSubject,
    *,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    hard_ttl_ms: int,
    heartbeat_interval_seconds: float,
    queue_deadline: float,
    ticket_expires_at_ms: int,
    requester_card_ticket_id: str,
    requester_queue_owner_id: str,
    queue_poll_interval_seconds: float = _GPU_RUNTIME_QUEUE_POLL_INTERVAL_SECONDS,
) -> bool:
    changed = False
    for _ in range(_GPU_RUNTIME_MAX_EVICTION_ATTEMPTS):
        _queue_remaining_ttl_ms(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=ticket_expires_at_ms,
        )
        try:
            snapshot = await store.snapshot(requester.gpu_resource_id)
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU card snapshot is unavailable",
            ) from exc
        allow_busy = False
        try:
            try:
                victim = _idle_victim_hint(snapshot, requester)
            except GPUArbiterDispatchError as exc:
                if (
                    exc.error_code != GPUArbiterErrorCode.CAPACITY_UNAVAILABLE.value
                    or not isinstance(exc.detail, dict)
                    or exc.detail.get("message") != _GPU_RUNTIME_NO_SAFE_IDLE_VICTIM
                ):
                    raise
                victim = _eviction_victim_hint(
                    snapshot,
                    requester,
                    allow_busy=True,
                )
                allow_busy = True
        except _GPUVictimCooldownActive as exc:
            cooldown_wait_seconds = max(
                0.0,
                (exc.retry_at_ms - snapshot.observed_at_ms) / 1000,
            )
            if cooldown_wait_seconds <= 0:
                continue
            async with _bounded_card_admission_step(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=ticket_expires_at_ms,
            ):
                await clock.sleep(cooldown_wait_seconds)
            continue
        if victim is None:
            return changed
        _eviction_work_remaining_ttl_ms(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=ticket_expires_at_ms,
        )
        async with _bounded_card_admission_step(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=ticket_expires_at_ms,
            reserve_seconds=_GPU_RUNTIME_EVICTION_CLEANUP_RESERVE_SECONDS,
        ):
            outcome = await _evict_one_idle_victim(
                data_provider,
                endpoint_source,
                backend_transport,
                store,
                signer,
                requester,
                victim,
                clock=clock,
                health_refresher=health_refresher,
                hard_ttl_ms=hard_ttl_ms,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                queue_deadline=queue_deadline,
                ticket_expires_at_ms=ticket_expires_at_ms,
                requester_card_ticket_id=requester_card_ticket_id,
                requester_queue_owner_id=requester_queue_owner_id,
                allow_busy=allow_busy,
                poll_interval_seconds=queue_poll_interval_seconds,
            )
        if outcome == "unloaded":
            changed = True
        elif outcome == "capacity_available":
            return changed
    raise _dispatch_error(
        GPUArbiterErrorCode.NOT_READY,
        "GPU eviction selection did not converge",
    )


@asynccontextmanager
async def _dispatch_cold_runtime(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    request: GPUDispatchRequest,
    subject: GPUColdRuntimeSubject,
    *,
    config: Settings,
    store_factory: GPUArbiterStoreFactory,
    signer_factory: GPUAdmissionSignerFactory,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    heartbeat_ttl_ms: int,
    heartbeat_interval_seconds: float,
    admission_timeout_seconds: float | None,
    residency_cooldown_seconds: float | None,
    queue_poll_interval_seconds: float,
):
    generation = _cold_generation_candidate(subject)
    hard_ttl_ms = _runtime_hard_ttl_ms(
        config,
        heartbeat_ttl_ms=heartbeat_ttl_ms,
    )
    reconcile_deadline_grace_ms = _runtime_reconcile_deadline_grace_ms(hard_ttl_ms)
    admission_timeout_ms = _admission_timeout_ms(
        config,
        override_seconds=admission_timeout_seconds,
    )
    resident_cooldown_ms = _residency_cooldown_ms(
        config,
        override_seconds=residency_cooldown_seconds,
    )
    try:
        signer = signer_factory()
    except Exception as exc:
        raise _dispatch_error(
            GPUArbiterErrorCode.NOT_READY,
            "GPU admission signer is not ready",
        ) from exc

    try:
        store = store_factory()
    except Exception as exc:
        raise _dispatch_error(
            GPUArbiterErrorCode.UNAVAILABLE,
            "GPU arbiter store is unavailable",
        ) from exc

    lease_id = f"workload:{uuid.uuid4()}"
    owner_id = f"dispatch:{uuid.uuid4()}"
    card_ticket_id = f"card:{uuid.uuid4()}"
    ticket_membership_epoch = subject.membership_epoch
    intent_may_exist = False
    lease_may_exist = False
    card_ticket_may_exist = False
    grant_exposed = False
    grant: GPUDispatchGrant | None = None
    prepared_subject: GPUPreparedColdRuntimeSubject | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        try:
            if not await store.ping():
                raise GPUArbiterStoreError("GPU arbiter ping failed")
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU arbiter store is unavailable",
            ) from exc

        card_ticket_may_exist = True
        queue_deadline = clock.monotonic() + admission_timeout_ms / 1000
        card_ticket = await _enqueue_fifo_ticket(
            store,
            clock,
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            membership_epoch=subject.membership_epoch,
            ticket_id=card_ticket_id,
            owner_id=owner_id,
            deadline=queue_deadline,
            card_queue=True,
        )
        await _wait_for_card_fifo_head(
            store,
            clock,
            subject.gpu_resource_id,
            backend_id=str(subject.backend_registry_id),
            ticket=card_ticket,
            deadline=queue_deadline,
            poll_interval_seconds=queue_poll_interval_seconds,
        )
        assert card_ticket.expires_at_ms is not None
        try:
            async with _bounded_card_admission_step(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ):
                subject = await _read_cold_runtime_subject(
                    data_provider,
                    request,
                )
        except GPUArbiterDispatchError:
            raise
        except GPUColdRuntimeSubjectError as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.NOT_READY,
                "GPU cold runtime changed while queued",
            ) from exc
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold runtime is unavailable while queued",
            ) from exc
        if subject.membership_epoch != ticket_membership_epoch:
            raise _dispatch_error(
                GPUArbiterErrorCode.NOT_READY,
                "GPU cold runtime membership changed while queued",
            )
        generation = _cold_generation_candidate(subject)

        async with _bounded_card_admission_step(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=card_ticket.expires_at_ms,
        ):
            capacity_changed = await _ensure_cold_capacity(
                data_provider,
                endpoint_source,
                backend_transport,
                store,
                signer,
                subject,
                clock=clock,
                health_refresher=health_refresher,
                hard_ttl_ms=hard_ttl_ms,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                queue_deadline=queue_deadline,
                ticket_expires_at_ms=card_ticket.expires_at_ms,
                requester_card_ticket_id=card_ticket_id,
                requester_queue_owner_id=owner_id,
                queue_poll_interval_seconds=queue_poll_interval_seconds,
            )
        if capacity_changed:
            challenge = secrets.token_hex(32)
            try:
                async with _bounded_card_admission_step(
                    clock=clock,
                    deadline=queue_deadline,
                    expires_at_ms=card_ticket.expires_at_ms,
                ):
                    refreshed = await health_refresher(
                        subject.backend_registry_id,
                        challenge,
                    )
            except GPUArbiterDispatchError:
                raise
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU cold runtime health is unavailable after eviction",
                ) from exc
            if not refreshed:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU cold runtime is not ready after eviction",
                )
            try:
                async with _bounded_card_admission_step(
                    clock=clock,
                    deadline=queue_deadline,
                    expires_at_ms=card_ticket.expires_at_ms,
                ):
                    subject = await _read_cold_runtime_subject(
                        data_provider,
                        request,
                        expected_challenge=challenge,
                    )
            except GPUArbiterDispatchError:
                raise
            except GPUColdRuntimeSubjectError as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU cold runtime changed during eviction",
                ) from exc
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU cold runtime is unavailable after eviction",
                ) from exc
            if subject.membership_epoch != ticket_membership_epoch:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU cold runtime membership changed during eviction",
                )
            generation = _cold_generation_candidate(subject)

        cold_owner_ttl_ms = min(
            _GPU_RUNTIME_COLD_INTENT_TTL_MS,
            _queue_remaining_ttl_ms(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ),
        )
        intent_may_exist = True
        try:
            async with _bounded_card_admission_step(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ):
                owner = await store.acquire_cold_admission_owner(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    membership_epoch=subject.membership_epoch,
                    owner_id=owner_id,
                    generation=generation,
                    ttl_ms=cold_owner_ttl_ms,
                )
        except GPUArbiterDispatchError:
            raise
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold admission owner is unavailable",
            ) from exc
        if owner.status != "acquired":
            intent_may_exist = False
            raise _map_cold_owner_rejection(owner.status)

        try:
            async with _bounded_card_admission_step(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ):
                prepared = await prepare_gpu_cold_runtime_generation(
                    data_provider,
                    subject,
                    token_expires_at=subject.db_now
                    + timedelta(milliseconds=hard_ttl_ms),
                )
            prepared_subject = prepared
        except GPUArbiterDispatchError:
            raise
        except GPUColdRuntimeSubjectError as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.NOT_READY,
                "GPU cold runtime changed before generation issuance",
            ) from exc
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold runtime generation is unavailable",
            ) from exc
        if prepared.generation != generation:
            raise _dispatch_error(
                GPUArbiterErrorCode.NOT_READY,
                "GPU cold runtime generation changed",
            )

        cold_owner_ttl_ms = min(
            _GPU_RUNTIME_COLD_INTENT_TTL_MS,
            _queue_remaining_ttl_ms(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ),
        )
        try:
            async with _bounded_card_admission_step(
                clock=clock,
                deadline=queue_deadline,
                expires_at_ms=card_ticket.expires_at_ms,
            ):
                revalidated = await store.revalidate_cold_admission_owner(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    membership_epoch=subject.membership_epoch,
                    owner_id=owner_id,
                    generation=generation,
                    ttl_ms=cold_owner_ttl_ms,
                )
        except GPUArbiterDispatchError:
            raise
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold admission owner revalidation is unavailable",
            ) from exc
        if revalidated.status != "renewed":
            raise _map_cold_owner_rejection(revalidated.status)

        lease_may_exist = True
        admission_kwargs = {
            "backend_id": str(subject.backend_registry_id),
            "membership_epoch": subject.membership_epoch,
            "budget_mb": subject.budget_mb,
            "generation": generation,
            "eviction_priority": subject.eviction_priority,
            "evictable": False,
            "max_concurrency": subject.max_concurrency,
            "lease_id": lease_id,
            "owner_id": owner_id,
            "operation": request.operation,
            "heartbeat_ttl_ms": heartbeat_ttl_ms,
            "hard_ttl_ms": hard_ttl_ms,
            "reconcile_deadline_grace_ms": reconcile_deadline_grace_ms,
            "card_ticket_id": card_ticket_id,
            "require_cold_owner": True,
        }
        async with _bounded_card_admission_step(
            clock=clock,
            deadline=queue_deadline,
            expires_at_ms=card_ticket.expires_at_ms,
        ):
            admission = await _call_admit_exact(
                store,
                subject.gpu_resource_id,
                **admission_kwargs,
            )
        if not admission.admitted:
            lease_may_exist = False
            raise _map_admission_rejection(admission)
        card_ticket_may_exist = False
        intent_may_exist = False
        if (
            admission.allocation_state is not GPUAllocationState.RESERVING
            or admission.lease_count != 1
            or admission.heartbeat_deadline_ms is None
            or admission.hard_deadline_ms is None
            or admission.heartbeat_deadline_ms > admission.hard_deadline_ms
        ):
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU arbiter returned an invalid cold reservation",
            )

        token_exp = min(
            int(prepared.token_expires_at.timestamp()),
            admission.hard_deadline_ms // 1000,
        )
        if (
            token_exp - max(int(clock.time()), int(prepared.db_now.timestamp()))
            < _GPU_RUNTIME_MIN_TOKEN_WINDOW_SECONDS
        ):
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU runtime lease has no safe token delivery window",
            )
        try:
            admission_token = signer.sign(
                AdmissionTokenClaims(
                    backend_registry_id=str(prepared.backend_registry_id),
                    gpu_resource_id=prepared.gpu_resource_id,
                    boot_id=prepared.boot_id,
                    generation=prepared.generation,
                    control_epoch=prepared.control_epoch,
                    scope=request.scope,
                    jti=lease_id,
                    exp=token_exp,
                )
            )
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU admission token signing failed",
            ) from exc
        try:
            grant = GPUDispatchGrant(
                generation=prepared.generation,
                admission_token=admission_token,
            )
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU dispatch grant is invalid",
            ) from exc
        try:
            first_heartbeat = await store.heartbeat_lease(
                prepared.gpu_resource_id,
                backend_id=str(prepared.backend_registry_id),
                lease_id=lease_id,
                owner_id=owner_id,
                generation=prepared.generation,
                heartbeat_ttl_ms=heartbeat_ttl_ms,
            )
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU runtime lease heartbeat is unavailable",
            ) from exc
        if first_heartbeat.status != "heartbeated":
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU runtime lease heartbeat failed",
            )
        try:
            loading = await store.transition_allocation(
                prepared.gpu_resource_id,
                backend_id=str(prepared.backend_registry_id),
                expected_generation=prepared.generation,
                target_state=GPUAllocationState.LOADING,
                request_lease_id=lease_id,
                request_owner_id=owner_id,
            )
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold loading transition is unavailable",
            ) from exc
        if loading.status != "transitioned":
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU cold loading transition failed",
            )
        heartbeat_task = asyncio.create_task(
            _heartbeat_runtime_lease(
                store,
                prepared,
                clock,
                lease_id=lease_id,
                owner_id=owner_id,
                heartbeat_ttl_ms=heartbeat_ttl_ms,
                heartbeat_interval_seconds=float(heartbeat_interval_seconds),
                hard_deadline_ms=admission.hard_deadline_ms,
            ),
            name=f"gpu-runtime-heartbeat:{lease_id}",
        )
        grant_exposed = True
        yield grant
    finally:
        await _await_cancellation_safe(
            _finalize_cold_runtime(
                data_provider,
                store,
                subject,
                generation=generation,
                lease_id=lease_id,
                owner_id=owner_id,
                intent_may_exist=intent_may_exist,
                health_refresher=health_refresher,
                lease_may_exist=lease_may_exist,
                grant_exposed=grant_exposed,
                request_not_sent=(
                    grant is not None
                    and grant.outcome is not None
                    and grant.outcome.kind == "not_sent"
                ),
                response_received=(
                    grant is not None
                    and grant.outcome is not None
                    and grant.outcome.kind == "response_received"
                ),
                prepared_subject=prepared_subject,
                heartbeat_task=heartbeat_task,
                card_ticket_id=card_ticket_id,
                card_ticket_may_exist=card_ticket_may_exist,
                resident_cooldown_ms=resident_cooldown_ms,
            )
        )


def build_gpu_dispatch_context_factory(
    data_provider: GPUAuthorityDataProvider,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    *,
    config: Settings,
    store_factory: GPUArbiterStoreFactory,
    signer_factory: GPUAdmissionSignerFactory,
    health_refresher: GPUHealthRefresher,
    clock: GPUDispatchClock,
    heartbeat_ttl_ms: int = _GPU_RUNTIME_HEARTBEAT_TTL_MS,
    heartbeat_interval_seconds: float = _GPU_RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    admission_timeout_seconds: float | None = None,
    residency_cooldown_seconds: float | None = None,
    queue_poll_interval_seconds: float = _GPU_RUNTIME_QUEUE_POLL_INTERVAL_SECONDS,
) -> GPUDispatchContextFactory:
    """Build a lazy per-dispatch authority without opening DB, Redis, or secrets."""

    if (
        not isinstance(heartbeat_ttl_ms, int)
        or isinstance(heartbeat_ttl_ms, bool)
        or heartbeat_ttl_ms <= 0
    ):
        raise ValueError("heartbeat_ttl_ms must be a positive integer")
    if (
        not isinstance(heartbeat_interval_seconds, (int, float))
        or isinstance(heartbeat_interval_seconds, bool)
        or heartbeat_interval_seconds <= 0
        or heartbeat_interval_seconds * 1000 >= heartbeat_ttl_ms
    ):
        raise ValueError("heartbeat interval must be positive and shorter than TTL")
    if admission_timeout_seconds is not None and (
        not isinstance(admission_timeout_seconds, (int, float))
        or isinstance(admission_timeout_seconds, bool)
        or not math.isfinite(admission_timeout_seconds)
        or admission_timeout_seconds <= 0
        or admission_timeout_seconds > _GPU_RUNTIME_MAX_ADMISSION_TIMEOUT_SECONDS
    ):
        raise ValueError("admission_timeout_seconds must be positive and at most 3600")
    if residency_cooldown_seconds is not None and (
        not isinstance(residency_cooldown_seconds, (int, float))
        or isinstance(residency_cooldown_seconds, bool)
        or not math.isfinite(residency_cooldown_seconds)
        or residency_cooldown_seconds <= 0
        or residency_cooldown_seconds > _GPU_RUNTIME_MAX_RESIDENCY_COOLDOWN_SECONDS
    ):
        raise ValueError("residency_cooldown_seconds must be positive and at most 3600")
    if (
        not isinstance(queue_poll_interval_seconds, (int, float))
        or isinstance(queue_poll_interval_seconds, bool)
        or not math.isfinite(queue_poll_interval_seconds)
        or queue_poll_interval_seconds <= 0
    ):
        raise ValueError("queue_poll_interval_seconds must be positive")

    @asynccontextmanager
    async def dispatch(request: GPUDispatchRequest):
        _validate_runtime_request(request)
        try:
            subject = await _read_runtime_subject(data_provider, request)
        except GPUResidentRuntimeSubjectError as exc:
            logger.info(
                "gpu_runtime_subject_not_ready",
                gpu_arbiter={
                    "backend_id": request.backend_id,
                    "resource_id": request.gpu_resource_id,
                    "reason": exc.reason,
                },
            )
            try:
                cold_subject = await _read_cold_runtime_subject(
                    data_provider,
                    request,
                )
            except GPUColdRuntimeSubjectError as cold_exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU runtime subject is not ready",
                ) from cold_exc
            except Exception as cold_exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU cold runtime subject is unavailable",
                ) from cold_exc
            async with _dispatch_cold_runtime(
                data_provider,
                endpoint_source,
                backend_transport,
                request,
                cold_subject,
                config=config,
                store_factory=store_factory,
                signer_factory=signer_factory,
                health_refresher=health_refresher,
                clock=clock,
                heartbeat_ttl_ms=heartbeat_ttl_ms,
                heartbeat_interval_seconds=float(heartbeat_interval_seconds),
                admission_timeout_seconds=admission_timeout_seconds,
                residency_cooldown_seconds=residency_cooldown_seconds,
                queue_poll_interval_seconds=float(queue_poll_interval_seconds),
            ) as grant:
                yield grant
            return
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU runtime subject is unavailable",
            ) from exc

        admission_timeout_ms = _admission_timeout_ms(
            config,
            override_seconds=admission_timeout_seconds,
        )

        try:
            signer = signer_factory()
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.NOT_READY,
                "GPU admission signer is not ready",
            ) from exc

        try:
            store = store_factory()
        except Exception as exc:
            raise _dispatch_error(
                GPUArbiterErrorCode.UNAVAILABLE,
                "GPU arbiter store is unavailable",
            ) from exc

        lease_id = f"workload:{uuid.uuid4()}"
        owner_id = f"dispatch:{uuid.uuid4()}"
        lease_may_exist = False
        grant_exposed = False
        grant: GPUDispatchGrant | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            hard_ttl_ms = _runtime_hard_ttl_ms(
                config,
                heartbeat_ttl_ms=heartbeat_ttl_ms,
            )
            reconcile_deadline_grace_ms = _runtime_reconcile_deadline_grace_ms(
                hard_ttl_ms
            )
            lease_may_exist = True
            admission_kwargs = {
                "backend_id": str(subject.backend_registry_id),
                "membership_epoch": subject.membership_epoch,
                "budget_mb": subject.budget_mb,
                "generation": subject.generation,
                "eviction_priority": subject.eviction_priority,
                "evictable": False,
                "max_concurrency": subject.max_concurrency,
                "lease_id": lease_id,
                "owner_id": owner_id,
                "operation": request.operation,
                "heartbeat_ttl_ms": heartbeat_ttl_ms,
                "hard_ttl_ms": hard_ttl_ms,
                "reconcile_deadline_grace_ms": reconcile_deadline_grace_ms,
                "require_resident": True,
            }
            try:
                admission = await _admit_resident_with_fifo(
                    store,
                    clock,
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    membership_epoch=subject.membership_epoch,
                    owner_id=owner_id,
                    admission_kwargs=admission_kwargs,
                    timeout_ms=admission_timeout_ms,
                    poll_interval_seconds=float(queue_poll_interval_seconds),
                )
            except GPUArbiterDispatchError:
                raise
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU arbiter admission is unavailable",
                ) from exc
            if not admission.admitted:
                lease_may_exist = False
                raise _map_admission_rejection(admission)
            if (
                admission.allocation_state is not GPUAllocationState.RESIDENT
                or admission.heartbeat_deadline_ms is None
                or admission.hard_deadline_ms is None
                or admission.heartbeat_deadline_ms > admission.hard_deadline_ms
            ):
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU arbiter returned an invalid Resident grant",
                )

            token_exp = admission.hard_deadline_ms // 1000
            if (
                token_exp - int(subject.db_now.timestamp())
                < _GPU_RUNTIME_MIN_TOKEN_WINDOW_SECONDS
            ):
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU runtime lease has no safe token delivery window",
                )
            token_expires_at = datetime.fromtimestamp(token_exp, tz=UTC)
            try:
                await record_gpu_resident_runtime_token_expiry(
                    data_provider,
                    subject,
                    token_expires_at=token_expires_at,
                )
            except GPUResidentRuntimeSubjectError as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.NOT_READY,
                    "GPU resident runtime changed before token issuance",
                ) from exc
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU token horizon is unavailable",
                ) from exc

            try:
                claims = AdmissionTokenClaims(
                    backend_registry_id=str(subject.backend_registry_id),
                    gpu_resource_id=subject.gpu_resource_id,
                    boot_id=subject.boot_id,
                    generation=subject.generation,
                    control_epoch=subject.control_epoch,
                    scope=request.scope,
                    jti=lease_id,
                    exp=token_exp,
                )
                admission_token = signer.sign(claims)
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU admission token signing failed",
                ) from exc
            try:
                first_heartbeat = await store.heartbeat_lease(
                    subject.gpu_resource_id,
                    backend_id=str(subject.backend_registry_id),
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=subject.generation,
                    heartbeat_ttl_ms=heartbeat_ttl_ms,
                )
            except Exception as exc:
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU runtime lease heartbeat is unavailable",
                ) from exc
            if first_heartbeat.status != "heartbeated":
                raise _dispatch_error(
                    GPUArbiterErrorCode.UNAVAILABLE,
                    "GPU runtime lease heartbeat failed",
                )
            heartbeat_task = asyncio.create_task(
                _heartbeat_runtime_lease(
                    store,
                    subject,
                    clock,
                    lease_id=lease_id,
                    owner_id=owner_id,
                    heartbeat_ttl_ms=heartbeat_ttl_ms,
                    heartbeat_interval_seconds=float(heartbeat_interval_seconds),
                    hard_deadline_ms=admission.hard_deadline_ms,
                ),
                name=f"gpu-runtime-heartbeat:{lease_id}",
            )
            grant = GPUDispatchGrant(
                generation=subject.generation,
                admission_token=admission_token,
            )
            grant_exposed = True
            yield grant
        finally:
            action: str | None = None
            if lease_may_exist:
                action = "release"
                if grant_exposed and (
                    grant is None
                    or grant.outcome is None
                    or grant.outcome.kind not in {"response_received", "not_sent"}
                ):
                    action = "uncertain"
            await _await_cancellation_safe(
                _cleanup_runtime_lease(
                    store,
                    subject,
                    lease_id=lease_id,
                    owner_id=owner_id,
                    action=action,
                    heartbeat_task=heartbeat_task,
                )
            )

    return dispatch


__all__ = ["build_gpu_dispatch_context_factory"]
