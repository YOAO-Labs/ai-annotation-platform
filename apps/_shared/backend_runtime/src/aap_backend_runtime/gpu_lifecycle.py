"""Managed GPU lifecycle state domain.

The shared protocol package owns only wire models.  This module deliberately keeps
YOLO's pool, borrower, active-operation, replay, and fencing state local to the single
uvicorn worker that owns the model pool.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    AdmissionTokenError,
    BackendResidency,
    BoundLifecycleIdentity,
    DrainTransitionResponse,
    LifecycleGate,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
    LifecycleState,
    ManagedUnloadResponse,
    PoolResidency,
    WORKLOAD_ADMISSION_SCOPES,
    validate_canonical_positive_int64,
    verify_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from typing import Protocol

class LifecyclePool(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    async def unload_all(self, *, reason: str, force_cleanup: bool) -> int: ...
    async def unload_idle(self, *, idle_before: float) -> int: ...
    async def shutdown(self) -> None: ...

ControlPhase = Literal["normal", "draining", "unloading", "unknown"]
Fingerprint = tuple[Any, ...]


@dataclass(slots=True)
class _ActiveOperation:
    close_requested: bool = False
    pending_futures: set[asyncio.Future[Any]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _JTIRecord:
    fingerprint: Fingerprint
    expires_at: int
    workload: bool


@dataclass(frozen=True, slots=True)
class _DrainRecord:
    generation: str
    owner: str
    operation: str


@dataclass(frozen=True, slots=True)
class _ResumeRecord:
    generation: str
    owner: str
    operation: str


@dataclass(frozen=True, slots=True)
class _ModeRecord:
    control_epoch: str
    gate: LifecycleGate
    owner: str
    operation: str


@dataclass(frozen=True, slots=True)
class _CleanupRecord:
    epoch: str
    owner: str
    operation: str
    task: asyncio.Task[Any]


class WorkloadOperation:
    """One admitted request whose active lifetime may outlive its HTTP coroutine."""

    def __init__(
        self,
        lifecycle: ManagedGpuLifecycle,
        operation_id: str,
        *,
        managed: bool,
        generation: str | None,
    ) -> None:
        self._lifecycle = lifecycle
        self._operation_id = operation_id
        self._pending_futures: set[asyncio.Future[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self.managed = managed
        self.generation = generation

    def track_future(self, future: asyncio.Future[Any] | None) -> None:
        """Keep backend active until a real builder/executor future has finished."""

        if future is None or future.done():
            return
        if self._closed or self._close_task is not None:
            raise RuntimeError("cannot track a future after workload close")
        self._pending_futures.add(future)

    async def close(self) -> None:
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._lifecycle._close_workload(  # noqa: SLF001
                    self._operation_id,
                    self._pending_futures,
                )
            )

        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True

        # Surface an internal close failure before making the operation terminal.
        self._close_task.result()
        self._closed = True
        if cancelled:
            raise asyncio.CancelledError


class ManagedGpuLifecycle:
    """Single-process managed lifecycle guard for a model pool."""

    def __init__(
        self,
        pool: LifecyclePool,
        *,
        verify_keyring: Mapping[str, Ed25519PublicKey],
        evictable_verified: bool = False,
        boot_id: str | None = None,
        expected_registry_id: str | None = None,
        expected_gpu_resource_id: str | None = None,
    ) -> None:
        if bool(expected_registry_id) != bool(expected_gpu_resource_id):
            raise ValueError("both expected lifecycle identities are required")
        self._expected_identity = (BoundLifecycleIdentity(backend_registry_id=expected_registry_id, gpu_resource_id=expected_gpu_resource_id) if expected_registry_id and expected_gpu_resource_id else None)
        self._pool = pool
        self._verify_keyring = dict(verify_keyring)
        self._evictable_verified = evictable_verified
        self._boot_id = boot_id or uuid.uuid4().hex
        self._lock = asyncio.Lock()

        self._gate = LifecycleGate.LEGACY
        self._control_epoch: str | None = None
        self._identity: BoundLifecycleIdentity | None = None
        self._generation: str | None = None
        self._generation_open = False
        self._unmanaged_tainted = False
        self._phase: ControlPhase = "normal"

        self._active: dict[str, _ActiveOperation] = {}
        self._jti_records: dict[str, _JTIRecord] = {}
        self._current_drain: _DrainRecord | None = None
        self._last_resume: _ResumeRecord | None = None
        self._last_mode: _ModeRecord | None = None
        self._last_reset: _CleanupRecord | None = None
        self._last_unload: _CleanupRecord | None = None
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def boot_id(self) -> str:
        return self._boot_id

    async def begin_workload(
        self,
        scope: AdmissionScope,
        *,
        generation_header: str | None,
        token: str | None,
    ) -> WorkloadOperation:
        """Admit a workload and consume its JTI before parsing the business body."""

        if scope not in WORKLOAD_ADMISSION_SCOPES:
            raise ValueError(f"{scope!r} is not a workload scope")

        claims: AdmissionTokenClaims | None = None
        token_error: LifecycleHTTPError | None = None
        generation: str | None = None
        if token is not None or generation_header is not None:
            try:
                generation = self._parse_generation(generation_header)
                claims = self._decode_token(token)
            except LifecycleHTTPError as exc:
                token_error = exc

        async with self._lock:
            self._prune_jtis_locked()
            if self._phase in {"draining", "unloading"}:
                raise LifecycleHTTPError(
                    LifecycleErrorCode.BACKEND_DRAINING,
                    retry_after_s=1,
                )
            if self._phase == "unknown":
                raise LifecycleHTTPError(
                    LifecycleErrorCode.TRANSITION_CONFLICT,
                    message="backend residency is unknown; signed reset is required",
                )

            pool_snapshot = await self._pool.snapshot()
            if self._gate is LifecycleGate.ENFORCE and pool_snapshot["cleanup_failed"]:
                raise LifecycleHTTPError(
                    LifecycleErrorCode.TRANSITION_CONFLICT,
                    message="backend residency is unknown; signed reset is required",
                )

            managed = False
            if self._gate is LifecycleGate.ENFORCE:
                if token is None or generation_header is None:
                    raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
                if token_error is not None:
                    raise token_error
                assert claims is not None and generation is not None
                self._validate_bound_claim_locked(claims, scope)
                self._require_claim_generation(claims, generation)
                self._consume_jti_locked(
                    claims,
                    self._fingerprint("workload", scope, generation, claims),
                    workload=True,
                )
                await self._adopt_or_match_generation_locked(generation)
                operation_id = claims.jti
                managed = True
            else:
                if token is None and generation_header is None:
                    operation_id = f"legacy:{uuid.uuid4().hex}"
                    self._generation = None
                    self._generation_open = False
                    self._unmanaged_tainted = True
                else:
                    if token is None or generation_header is None:
                        raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
                    if token_error is not None:
                        raise token_error
                    assert claims is not None and generation is not None
                    self._validate_bound_claim_locked(claims, scope)
                    self._require_claim_generation(claims, generation)
                    if generation != self._generation or not self._generation_open:
                        raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
                    self._consume_jti_locked(
                        claims,
                        self._fingerprint("workload", scope, generation, claims),
                        workload=True,
                    )
                    operation_id = claims.jti
                    managed = True

            self._active[operation_id] = _ActiveOperation()
            return WorkloadOperation(
                self,
                operation_id,
                managed=managed,
                generation=self._generation if managed else None,
            )

    async def _close_workload(
        self,
        operation_id: str,
        pending_futures: set[asyncio.Future[Any]],
    ) -> None:
        async with self._lock:
            record = self._active.get(operation_id)
            if record is None:
                return
            record.close_requested = True
            record.pending_futures.update(
                future for future in pending_futures if not future.done()
            )
            if not record.pending_futures:
                self._active.pop(operation_id, None)
                return
            for future in record.pending_futures:
                future.add_done_callback(
                    lambda done, op_id=operation_id: self._schedule_future_finished(
                        op_id, done
                    )
                )

    def _schedule_future_finished(
        self,
        operation_id: str,
        future: asyncio.Future[Any],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._future_finished(operation_id, future))

    async def _future_finished(
        self,
        operation_id: str,
        future: asyncio.Future[Any],
    ) -> None:
        async with self._lock:
            record = self._active.get(operation_id)
            if record is None:
                return
            record.pending_futures.discard(future)
            if record.close_requested and not record.pending_futures:
                self._active.pop(operation_id, None)

    async def residency(self) -> BackendResidency:
        _snapshot, residency = await self.snapshot_and_residency()
        return residency

    async def snapshot_and_residency(
        self,
    ) -> tuple[dict[str, Any], BackendResidency]:
        """Return legacy pool and lifecycle views from one ordered lock snapshot."""

        async with self._lock:
            snapshot = await self._pool.snapshot()
            return snapshot, self._build_residency_locked(snapshot)

    async def set_mode(
        self,
        request: LifecycleModeRequest,
        *,
        token: str | None,
    ) -> LifecycleModeResponse:
        claims = self._decode_token(token)
        if claims.scope is not AdmissionScope.MODE:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        if claims.control_epoch != request.control_epoch:
            raise LifecycleHTTPError(
                LifecycleErrorCode.ADMISSION_DENIED,
                message="control epoch does not match request body",
            )
        fingerprint = self._fingerprint(
            "/lifecycle/mode", request.gate.value, request.control_epoch, claims
        )

        async with self._lock:
            self._validate_control_claim_locked(claims, AdmissionScope.MODE)
            if request.gate is LifecycleGate.ENFORCE and not self._evictable_verified:
                raise LifecycleHTTPError(
                    LifecycleErrorCode.TRANSITION_CONFLICT,
                    message="real GPU full-unload verification is required before enforce",
                )
            self._consume_jti_locked(claims, fingerprint, workload=False)
            incoming = int(request.control_epoch)
            current = (
                int(self._control_epoch) if self._control_epoch is not None else None
            )
            if current == incoming:
                expected = _ModeRecord(
                    request.control_epoch,
                    request.gate,
                    claims.owner or "",
                    claims.operation or "",
                )
                if self._last_mode != expected:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                snapshot = await self._pool.snapshot()
                return self._mode_response_locked(snapshot)
            if current is not None and incoming < current:
                raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
            recovering_to_legacy = (
                self._phase == "unknown" and request.gate is LifecycleGate.LEGACY
            )
            if self._phase != "normal" and not recovering_to_legacy:
                raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)

            snapshot = await self._pool.snapshot()
            if self._active or self._pool_is_active(snapshot):
                raise LifecycleHTTPError(LifecycleErrorCode.BACKEND_ACTIVE)
            if request.gate is LifecycleGate.ENFORCE and (
                self._unmanaged_tainted or not self._pool_is_trusted_empty(snapshot)
            ):
                raise LifecycleHTTPError(
                    LifecycleErrorCode.TRANSITION_CONFLICT,
                    message="signed reset and trusted empty residency are required",
                )

            self._bind_identity_locked(claims)
            self._control_epoch = request.control_epoch
            self._gate = request.gate
            self._last_mode = _ModeRecord(
                request.control_epoch,
                request.gate,
                claims.owner or "",
                claims.operation or "",
            )
            return self._mode_response_locked(snapshot)

    async def reset(
        self,
        request: LifecycleResetRequest,
        *,
        token: str | None,
    ) -> LifecycleResetResponse:
        claims = self._decode_token(token)
        if claims.scope is not AdmissionScope.RESET:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        if claims.control_epoch != request.control_epoch:
            raise LifecycleHTTPError(
                LifecycleErrorCode.ADMISSION_DENIED,
                message="control epoch does not match request body",
            )
        fingerprint = self._fingerprint(
            "/lifecycle/reset", request.control_epoch, claims
        )

        async with self._lock:
            self._validate_control_claim_locked(claims, AdmissionScope.RESET)
            self._consume_jti_locked(claims, fingerprint, workload=False)
            incoming = int(request.control_epoch)
            current = (
                int(self._control_epoch) if self._control_epoch is not None else None
            )
            expected = (
                request.control_epoch,
                claims.owner or "",
                claims.operation or "",
            )
            if current == incoming:
                if (
                    self._last_reset is None
                    or (
                        self._last_reset.epoch,
                        self._last_reset.owner,
                        self._last_reset.operation,
                    )
                    != expected
                ):
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                if (
                    self._unmanaged_tainted
                    and self._last_reset.task.done()
                    and self._last_reset.task.exception() is None
                ):
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                task = self._last_reset.task
            else:
                if current is not None and incoming < current:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                if self._gate is not LifecycleGate.LEGACY:
                    raise LifecycleHTTPError(
                        LifecycleErrorCode.TRANSITION_CONFLICT,
                        message="reset is only allowed in the legacy gate",
                    )
                if self._phase in {"draining", "unloading"}:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                snapshot = await self._pool.snapshot()
                if self._active or self._pool_is_active(snapshot):
                    raise LifecycleHTTPError(LifecycleErrorCode.BACKEND_ACTIVE)
                self._bind_identity_locked(claims)
                self._control_epoch = request.control_epoch
                self._phase = "unloading"
                task = asyncio.create_task(self._execute_reset(request.control_epoch))
                task.add_done_callback(self._consume_task_result)
                self._last_reset = _CleanupRecord(
                    request.control_epoch,
                    claims.owner or "",
                    claims.operation or "",
                    task,
                )
        return await asyncio.shield(task)

    async def drain(
        self,
        generation: str,
        *,
        generation_header: str | None,
        token: str | None,
    ) -> DrainTransitionResponse:
        claims, canonical = self._transition_claims(
            AdmissionScope.DRAIN,
            generation,
            generation_header,
            token,
        )
        fingerprint = self._fingerprint("/drain", canonical, claims)
        async with self._lock:
            self._validate_enforced_transition_locked(claims, AdmissionScope.DRAIN)
            self._consume_jti_locked(claims, fingerprint, workload=False)
            if self._generation == canonical:
                expected = _DrainRecord(
                    canonical,
                    claims.owner or "",
                    claims.operation or "",
                )
                if self._phase != "draining" or self._current_drain != expected:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                snapshot = await self._pool.snapshot()
                return self._drain_response_locked(snapshot, draining=True)
            if self._generation is None or int(canonical) < int(self._generation):
                raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
            if self._phase != "normal":
                raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)

            self._generation = canonical
            self._generation_open = False
            self._phase = "draining"
            self._current_drain = _DrainRecord(
                canonical,
                claims.owner or "",
                claims.operation or "",
            )
            self._last_resume = None
            self._last_unload = None
            snapshot = await self._pool.snapshot()
            return self._drain_response_locked(snapshot, draining=True)

    async def cancel_drain(
        self,
        generation: str,
        *,
        generation_header: str | None,
        token: str | None,
    ) -> DrainTransitionResponse:
        claims, canonical = self._transition_claims(
            AdmissionScope.RESUME,
            generation,
            generation_header,
            token,
        )
        fingerprint = self._fingerprint("/drain/cancel", canonical, claims)
        async with self._lock:
            self._validate_enforced_transition_locked(claims, AdmissionScope.RESUME)
            self._consume_jti_locked(claims, fingerprint, workload=False)
            expected_resume = _ResumeRecord(
                canonical,
                claims.owner or "",
                claims.operation or "",
            )
            if self._generation == canonical:
                if self._phase != "normal" or self._last_resume != expected_resume:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                snapshot = await self._pool.snapshot()
                return self._drain_response_locked(snapshot, draining=False)
            if self._generation is None or int(canonical) < int(self._generation):
                raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
            if self._phase != "draining" or self._current_drain is None:
                raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
            if (
                claims.owner != self._current_drain.owner
                or claims.operation != self._current_drain.operation
            ):
                raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)

            self._generation = canonical
            self._generation_open = True
            self._phase = "normal"
            self._last_resume = expected_resume
            self._current_drain = None
            snapshot = await self._pool.snapshot()
            return self._drain_response_locked(snapshot, draining=False)

    async def managed_unload(
        self,
        generation: str,
        *,
        generation_header: str | None,
        token: str | None,
    ) -> ManagedUnloadResponse:
        claims, canonical = self._transition_claims(
            AdmissionScope.UNLOAD,
            generation,
            generation_header,
            token,
        )
        fingerprint = self._fingerprint("/unload", canonical, claims)
        async with self._lock:
            self._validate_enforced_transition_locked(claims, AdmissionScope.UNLOAD)
            self._consume_jti_locked(claims, fingerprint, workload=False)
            if canonical != self._generation:
                raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
            expected = (
                canonical,
                claims.owner or "",
                claims.operation or "",
            )
            if (
                self._last_unload is not None
                and (
                    self._last_unload.epoch,
                    self._last_unload.owner,
                    self._last_unload.operation,
                )
                == expected
            ):
                task = self._last_unload.task
            else:
                if self._phase != "draining" or self._current_drain is None:
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                if (
                    claims.owner != self._current_drain.owner
                    or claims.operation != self._current_drain.operation
                ):
                    raise LifecycleHTTPError(LifecycleErrorCode.TRANSITION_CONFLICT)
                snapshot = await self._pool.snapshot()
                if self._active or self._pool_is_active(snapshot):
                    raise LifecycleHTTPError(LifecycleErrorCode.BACKEND_ACTIVE)
                self._phase = "unloading"
                task = asyncio.create_task(self._execute_managed_unload(canonical))
                task.add_done_callback(self._consume_task_result)
                self._last_unload = _CleanupRecord(
                    canonical,
                    claims.owner or "",
                    claims.operation or "",
                    task,
                )
        return await asyncio.shield(task)

    async def legacy_unload(self) -> dict[str, Any]:
        async with self._lock:
            if self._gate is LifecycleGate.ENFORCE:
                raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
            if self._phase != "normal":
                return {"ok": True, "unloaded": 0}
            snapshot = await self._pool.snapshot()
            if self._active or self._pool_is_active(snapshot):
                return {"ok": True, "unloaded": 0}
            self._phase = "unloading"
            task = asyncio.create_task(self._execute_legacy_unload())
            task.add_done_callback(self._consume_task_result)

        # Client disconnect must not cancel the cleanup owner and strand a legacy-only
        # deployment in unknown without a configured keyring for signed reset.
        return await asyncio.shield(task)

    async def _execute_legacy_unload(self) -> dict[str, Any]:
        try:
            count = await self._pool.unload_all(reason="manual", force_cleanup=True)
        except BaseException:
            async with self._lock:
                self._phase = "unknown"
                self._generation = None
                self._generation_open = False
                self._unmanaged_tainted = True
            raise
        async with self._lock:
            self._phase = "normal"
            self._generation = None
            self._generation_open = False
            self._unmanaged_tainted = True
        return {"ok": True, "unloaded": count}

    async def try_idle_unload(self, *, idle_before: float) -> int:
        async with self._lock:
            if self._phase != "normal" or self._active:
                return 0
            snapshot = await self._pool.snapshot()
            if self._pool_is_active(snapshot) or snapshot["current_size"] == 0:
                return 0
            managed = self._generation is not None and not self._unmanaged_tainted
            self._phase = "unloading"

        try:
            count = await self._pool.unload_idle(idle_before=idle_before)
        except (Exception, asyncio.CancelledError):
            async with self._lock:
                self._phase = "unknown"
                self._generation_open = False
            raise
        async with self._lock:
            self._phase = "normal"
            if count:
                if managed:
                    self._generation_open = False
                else:
                    self._generation = None
                    self._generation_open = False
        return count

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_impl())
            self._shutdown_task.add_done_callback(self._consume_task_result)
        task = self._shutdown_task
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                pass

        shutdown_error: BaseException | None = None
        try:
            task.result()
        except BaseException as exc:
            shutdown_error = exc
        if shutdown_error is not None:
            if cancelled:
                raise asyncio.CancelledError from shutdown_error
            raise shutdown_error
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown_impl(self) -> None:
        async with self._lock:
            self._phase = "unloading"
        while True:
            async with self._lock:
                if not self._active:
                    break
            await asyncio.sleep(0.01)
        try:
            await self._pool.shutdown()
        finally:
            async with self._lock:
                self._phase = "normal"
                self._generation_open = False

    async def _execute_reset(self, control_epoch: str) -> LifecycleResetResponse:
        try:
            count = await self._pool.unload_all(reason="manual", force_cleanup=True)
            snapshot = await self._pool.snapshot()
            if not self._pool_is_trusted_empty(snapshot):
                raise RuntimeError(
                    "full-pool reset did not produce trusted empty state"
                )
        except BaseException as exc:
            async with self._lock:
                self._generation = None
                self._generation_open = False
                self._unmanaged_tainted = True
                self._phase = "unknown"
            raise LifecycleHTTPError(
                LifecycleErrorCode.UNLOAD_FAILED,
                message=str(exc),
            ) from exc

        async with self._lock:
            self._generation = None
            self._generation_open = False
            self._unmanaged_tainted = False
            self._phase = "normal"
            residency = self._build_residency_locked(snapshot)
            return LifecycleResetResponse(
                control_epoch=control_epoch,
                unloaded=True,
                unloaded_count=count,
                residency=residency,
            )

    async def _execute_managed_unload(
        self,
        generation: str,
    ) -> ManagedUnloadResponse:
        try:
            count = await self._pool.unload_all(reason="manual", force_cleanup=True)
            snapshot = await self._pool.snapshot()
            if not self._pool_is_trusted_empty(snapshot):
                raise RuntimeError("managed unload did not produce trusted empty state")
        except BaseException as exc:
            async with self._lock:
                self._generation_open = False
                self._phase = "unknown"
            raise LifecycleHTTPError(
                LifecycleErrorCode.UNLOAD_FAILED,
                message=str(exc),
            ) from exc

        async with self._lock:
            if self._generation != generation:
                self._phase = "unknown"
                raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
            self._generation_open = False
            self._phase = "normal"
            self._current_drain = None
            residency = self._build_residency_locked(snapshot)
            return ManagedUnloadResponse(
                generation=generation,
                unloaded=True,
                unloaded_count=count,
                residency=residency,
            )

    def _build_residency_locked(self, snapshot: dict[str, Any]) -> BackendResidency:
        builders = int(snapshot["builders"])
        borrowers = int(snapshot["borrowers"])
        gpu_loaded = snapshot["gpu_resident"]
        if gpu_loaded is False and (builders or borrowers):
            gpu_loaded = None

        if self._phase == "unknown" or snapshot["cleanup_failed"]:
            state = LifecycleState.UNKNOWN
            gpu_loaded = None
        elif self._phase == "unloading" or snapshot["cleanup_in_progress"]:
            state = LifecycleState.UNLOADING
            gpu_loaded = None
        elif self._phase == "draining":
            state = LifecycleState.DRAINING
        elif builders:
            state = LifecycleState.LOADING
        elif snapshot["current_size"]:
            state = (
                LifecycleState.UNKNOWN
                if gpu_loaded is None
                else LifecycleState.RESIDENT
            )
        elif gpu_loaded is None:
            state = LifecycleState.UNKNOWN
        else:
            state = LifecycleState.UNLOADED

        pool_resident: bool | None
        if snapshot["current_size"] == 0 and builders == 0 and gpu_loaded is False:
            pool_resident = False
        else:
            pool_resident = gpu_loaded
        evictable = (
            self._evictable_verified
            and self._gate is LifecycleGate.ENFORCE
            and self._identity is not None
            and self._generation is not None
            and self._generation_open
            and not self._unmanaged_tainted
            and self._phase == "normal"
            and state is LifecycleState.RESIDENT
            and gpu_loaded is True
        )
        return BackendResidency(
            state=state,
            gpu_loaded=gpu_loaded,
            active_requests=len(self._active),
            builders=builders,
            borrowers=borrowers,
            draining=self._phase == "draining",
            evictable=evictable,
            generation=self._generation,
            pools={
                "models": PoolResidency(
                    resident=pool_resident,
                    device=snapshot["device"],
                    provider=None,
                )
            },
            boot_id=self._boot_id,
            lifecycle_gate=self._gate,
            control_epoch=self._control_epoch,
            identity=self._identity,
        )

    def _mode_response_locked(self, snapshot: dict[str, Any]) -> LifecycleModeResponse:
        assert self._control_epoch is not None
        return LifecycleModeResponse(
            gate=self._gate,
            control_epoch=self._control_epoch,
            residency=self._build_residency_locked(snapshot),
        )

    def _drain_response_locked(
        self,
        snapshot: dict[str, Any],
        *,
        draining: bool,
    ) -> DrainTransitionResponse:
        assert self._generation is not None
        active_requests = len(self._active)
        ready = (
            draining
            and active_requests == 0
            and snapshot["builders"] == 0
            and snapshot["borrowers"] == 0
        )
        return DrainTransitionResponse(
            generation=self._generation,
            draining=draining,
            active_requests=active_requests,
            ready_to_unload=ready,
            residency=self._build_residency_locked(snapshot),
        )

    async def _adopt_or_match_generation_locked(self, generation: str) -> None:
        if generation == self._generation:
            if not self._generation_open:
                raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
            return
        if self._generation is not None and int(generation) <= int(self._generation):
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
        snapshot = await self._pool.snapshot()
        if (
            self._active
            or self._unmanaged_tainted
            or not self._pool_is_trusted_empty(snapshot)
        ):
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_CONFLICT)
        self._generation = generation
        self._generation_open = True

    def _transition_claims(
        self,
        scope: AdmissionScope,
        body_generation: str,
        generation_header: str | None,
        token: str | None,
    ) -> tuple[AdmissionTokenClaims, str]:
        try:
            canonical_body = validate_canonical_positive_int64(body_generation)
        except (TypeError, ValueError) as exc:
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_INVALID) from exc
        canonical_header = self._parse_generation(generation_header)
        claims = self._decode_token(token)
        if canonical_body != canonical_header or claims.generation != canonical_body:
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_MISMATCH)
        if claims.scope is not scope:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return claims, canonical_body

    @staticmethod
    def _parse_generation(value: str | None) -> str:
        try:
            return validate_canonical_positive_int64(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_INVALID) from exc

    def _decode_token(self, token: str | None) -> AdmissionTokenClaims:
        if not self._verify_keyring:
            raise LifecycleHTTPError(
                LifecycleErrorCode.ADMISSION_DENIED,
                message="lifecycle verify keyring is not configured",
            )
        try:
            return verify_admission_token(token or "", keyring=self._verify_keyring)
        except AdmissionTokenError as exc:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc

    def _validate_control_claim_locked(
        self,
        claims: AdmissionTokenClaims,
        scope: AdmissionScope,
    ) -> None:
        if self._expected_identity is None or self._expected_identity != BoundLifecycleIdentity(backend_registry_id=claims.backend_registry_id, gpu_resource_id=claims.gpu_resource_id):
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        if claims.scope is not scope or claims.boot_id != self._boot_id:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        if self._identity is not None and not self._identity_matches(claims):
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)

    def _validate_bound_claim_locked(
        self,
        claims: AdmissionTokenClaims,
        scope: AdmissionScope,
    ) -> None:
        if (
            claims.scope is not scope
            or claims.boot_id != self._boot_id
            or self._identity is None
            or not self._identity_matches(claims)
            or claims.control_epoch != self._control_epoch
        ):
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)

    def _validate_enforced_transition_locked(
        self,
        claims: AdmissionTokenClaims,
        scope: AdmissionScope,
    ) -> None:
        if self._gate is not LifecycleGate.ENFORCE:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        self._validate_bound_claim_locked(claims, scope)

    @staticmethod
    def _require_claim_generation(
        claims: AdmissionTokenClaims,
        generation: str,
    ) -> None:
        if claims.generation != generation:
            raise LifecycleHTTPError(LifecycleErrorCode.GENERATION_MISMATCH)

    def _bind_identity_locked(self, claims: AdmissionTokenClaims) -> None:
        identity = BoundLifecycleIdentity(
            backend_registry_id=claims.backend_registry_id,
            gpu_resource_id=claims.gpu_resource_id,
        )
        if self._identity is None:
            self._identity = identity
        elif self._identity != identity:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)

    def _identity_matches(self, claims: AdmissionTokenClaims) -> bool:
        return self._identity == BoundLifecycleIdentity(
            backend_registry_id=claims.backend_registry_id,
            gpu_resource_id=claims.gpu_resource_id,
        )

    def _consume_jti_locked(
        self,
        claims: AdmissionTokenClaims,
        fingerprint: Fingerprint,
        *,
        workload: bool,
    ) -> None:
        self._prune_jtis_locked()
        previous = self._jti_records.get(claims.jti)
        if previous is not None:
            if workload or previous.workload or previous.fingerprint != fingerprint:
                raise LifecycleHTTPError(
                    LifecycleErrorCode.ADMISSION_DENIED,
                    message="admission token replay was rejected",
                )
            return
        self._jti_records[claims.jti] = _JTIRecord(
            fingerprint=fingerprint,
            expires_at=claims.exp,
            workload=workload,
        )

    def _prune_jtis_locked(self) -> None:
        now = int(time.time())
        active_ids = set(self._active)
        for jti, record in list(self._jti_records.items()):
            if record.expires_at < now and jti not in active_ids:
                self._jti_records.pop(jti, None)

    @staticmethod
    def _fingerprint(*parts: Any) -> Fingerprint:
        normalized: list[Any] = []
        for part in parts:
            if isinstance(part, AdmissionTokenClaims):
                normalized.extend(
                    (
                        part.scope.value,
                        part.backend_registry_id,
                        part.gpu_resource_id,
                        part.boot_id,
                        part.generation,
                        part.control_epoch,
                        part.owner,
                        part.operation,
                    )
                )
            else:
                normalized.append(part)
        return tuple(normalized)

    @staticmethod
    def _pool_is_active(snapshot: dict[str, Any]) -> bool:
        return bool(
            snapshot["builders"]
            or snapshot["borrowers"]
            or snapshot["waiters"]
            or snapshot["cleanup_in_progress"]
        )

    @classmethod
    def _pool_is_trusted_empty(cls, snapshot: dict[str, Any]) -> bool:
        return bool(
            snapshot["current_size"] == 0
            and not cls._pool_is_active(snapshot)
            and not snapshot["cleanup_failed"]
            and snapshot["gpu_resident"] is False
        )

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()


__all__ = ["WorkloadOperation", "ManagedGpuLifecycle"]
