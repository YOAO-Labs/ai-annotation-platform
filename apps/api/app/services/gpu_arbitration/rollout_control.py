"""Rollout-bound signed reset/mode transport for one physical GPU resource.

Moved verbatim from the legacy flat module ``gpu_rollout_control.py``. Depends on contracts,
signing, rollout_state, fences, proofs, control_preparation and ml_client; must not depend on
dispatch, retirement or membership_activation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
import uuid

from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
)
from sqlalchemy import select

from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration.signing import GPUAdmissionTokenSigner
from app.services.gpu_arbitration.control_preparation import (
    GPUReadinessDemoter,
    GPURolloutControlBlockedError,
    GPURolloutControlPreparation,
    prepare_gpu_backend_rollout_control,
)
from app.services.gpu_arbitration.fences import GPUFenceSessionFactory
from app.services.ml_client import MLBackendClient
from app.utils.gpu_resource import validate_gpu_resource_id


_GPU_ROLLOUT_CONTROL_TIMEOUT_SECONDS = 8.0


class _ControlClient(Protocol):
    async def lifecycle_mode(
        self,
        request: LifecycleModeRequest,
        *,
        admission_token: str,
    ) -> LifecycleModeResponse: ...

    async def lifecycle_reset(
        self,
        request: LifecycleResetRequest,
        *,
        admission_token: str,
    ) -> LifecycleResetResponse: ...


GPUControlClientFactory = Callable[[MLBackendRegistry], _ControlClient]
GPUAdmissionSignerFactory = Callable[[], GPUAdmissionTokenSigner]


@dataclass(frozen=True)
class GPURolloutControlResult:
    backend_id: str
    resource_id: str
    membership_epoch: int
    transition_id: str
    operation: Literal["reset", "mode_enforce", "mode_legacy"] | None
    status: Literal["acknowledged", "issued", "pending", "blocked", "unavailable"]
    reason: str
    control_epoch: str | None = None


def _result(
    preparation: GPURolloutControlPreparation,
    *,
    status: Literal["acknowledged", "issued", "pending", "unavailable"],
    reason: str,
) -> GPURolloutControlResult:
    return GPURolloutControlResult(
        backend_id=str(preparation.backend_id),
        resource_id=preparation.resource_id,
        membership_epoch=preparation.membership_epoch,
        transition_id=str(preparation.transition_id),
        operation=preparation.operation,
        status=status,
        reason=reason,
        control_epoch=preparation.control_epoch,
    )


def _validate_identity(
    preparation: GPURolloutControlPreparation,
    residency,
) -> bool:
    identity = residency.identity
    return bool(
        residency.boot_id == preparation.boot_id
        and identity is not None
        and identity.audience == "aap-gpu-lifecycle"
        and identity.backend_registry_id == str(preparation.backend_id)
        and identity.gpu_resource_id == preparation.resource_id
    )


def _validate_reset_ack(
    response: LifecycleResetResponse,
    preparation: GPURolloutControlPreparation,
) -> None:
    residency = response.residency
    pool_states = tuple(pool.resident for pool in residency.pools.values())
    if (
        response.ok is not True
        or response.control_epoch != preparation.control_epoch
        or response.unloaded is not True
        or residency.lifecycle_gate.value != "legacy"
        or residency.control_epoch != preparation.control_epoch
        or not _validate_identity(preparation, residency)
        or residency.state.value != "unloaded"
        or residency.gpu_loaded is not False
        or residency.generation is not None
        or residency.evictable
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.draining
        or not pool_states
        or any(item is not False for item in pool_states)
    ):
        raise ValueError("reset acknowledgement is not trusted empty and exact")


def _validate_mode_ack(
    response: LifecycleModeResponse,
    preparation: GPURolloutControlPreparation,
) -> None:
    expected_gate = "enforce" if preparation.operation == "mode_enforce" else "legacy"
    residency = response.residency
    pool_states = tuple(pool.resident for pool in residency.pools.values())
    if (
        response.ok is not True
        or response.gate.value != expected_gate
        or response.control_epoch != preparation.control_epoch
        or residency.lifecycle_gate.value != expected_gate
        or residency.control_epoch != preparation.control_epoch
        or not _validate_identity(preparation, residency)
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.draining
        or residency.state.value not in {"unloaded", "resident"}
        or residency.gpu_loaded is None
        or not pool_states
        or any(type(item) is not bool for item in pool_states)
    ):
        raise ValueError("mode acknowledgement is not stable and exact")
    if expected_gate == "enforce" and (
        residency.gpu_loaded is not False
        or any(item is not False for item in pool_states)
        or residency.generation is not None
        or residency.evictable
    ):
        raise ValueError("enforce acknowledgement did not preserve reset-empty state")
    if expected_gate == "legacy" and residency.evictable:
        raise ValueError("legacy acknowledgement remained evictable")
    if residency.gpu_loaded is True and (
        residency.state.value != "resident" or not any(pool_states)
    ):
        raise ValueError("mode acknowledgement has inconsistent residency")


async def advance_gpu_backend_rollout_control(
    session_factory: GPUFenceSessionFactory,
    signer: GPUAdmissionTokenSigner,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    transition_id: uuid.UUID,
    target_gate: Literal["legacy", "enforce"],
    client_factory: GPUControlClientFactory | None = None,
    timeout_seconds: float = _GPU_ROLLOUT_CONTROL_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> GPURolloutControlResult:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    try:
        preparation = await prepare_gpu_backend_rollout_control(
            session_factory,
            backend_registry_id,
            gpu_resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            transition_id=transition_id,
            target_gate=target_gate,
            readiness_demoter=readiness_demoter,
        )
    except GPURolloutControlBlockedError as exc:
        return GPURolloutControlResult(
            backend_id=str(backend_registry_id),
            resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            transition_id=str(transition_id),
            operation=None,
            status="blocked",
            reason=exc.reason,
        )
    except Exception:  # noqa: BLE001 - durable state is uncertain; stay blocked
        return GPURolloutControlResult(
            backend_id=str(backend_registry_id),
            resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            transition_id=str(transition_id),
            operation=None,
            status="unavailable",
            reason="control_state_unavailable",
        )

    if preparation.action == "acknowledged":
        return _result(
            preparation,
            status="acknowledged",
            reason=f"{preparation.operation}_fresh_health_acknowledged",
        )
    if preparation.action == "awaiting_health":
        return _result(
            preparation,
            status="pending",
            reason=preparation.reason,
        )

    scope = (
        AdmissionScope.RESET
        if preparation.operation == "reset"
        else AdmissionScope.MODE
    )
    claims = AdmissionTokenClaims(
        backend_registry_id=str(preparation.backend_id),
        gpu_resource_id=preparation.resource_id,
        boot_id=preparation.boot_id,
        generation=None,
        control_epoch=preparation.control_epoch,
        scope=scope,
        jti=(
            f"rollout:{preparation.transition_id}:{preparation.backend_id}:"
            f"{preparation.operation}:{preparation.control_epoch}"
        ),
        exp=int(preparation.token_expires_at.timestamp()),
        owner=f"rollout:{preparation.transition_id}",
        operation=f"rollout:{preparation.operation}",
    )
    try:
        admission_token = signer.sign(claims)
    except Exception:  # noqa: BLE001 - exact intent remains durably replayable
        return _result(
            preparation,
            status="unavailable",
            reason="control_token_signing_failed",
        )

    try:
        async with asyncio.timeout(timeout_seconds):
            client = (
                client_factory(preparation.backend)
                if client_factory is not None
                else MLBackendClient(
                    preparation.backend, shadow_session_factory=session_factory
                )
            )
            if preparation.operation == "reset":
                response = await client.lifecycle_reset(
                    LifecycleResetRequest(
                        control_epoch=preparation.control_epoch,
                    ),
                    admission_token=admission_token,
                )
                _validate_reset_ack(response, preparation)
            else:
                gate = (
                    "enforce" if preparation.operation == "mode_enforce" else "legacy"
                )
                response = await client.lifecycle_mode(
                    LifecycleModeRequest(
                        gate=gate,
                        control_epoch=preparation.control_epoch,
                    ),
                    admission_token=admission_token,
                )
                _validate_mode_ack(response, preparation)
    except TimeoutError:
        return _result(preparation, status="unavailable", reason="control_ack_timeout")
    except Exception:  # noqa: BLE001 - exact intent remains durably replayable
        return _result(preparation, status="unavailable", reason="control_ack_failed")
    return _result(
        preparation,
        status="issued",
        reason=f"{preparation.operation}_ack_received_awaiting_health",
    )


async def advance_gpu_resource_rollout_control(
    session_factory: GPUFenceSessionFactory,
    gpu_resource_id: str,
    *,
    transition_id: uuid.UUID,
    target_gate: Literal["legacy", "enforce"],
    signer_factory: GPUAdmissionSignerFactory | None = None,
    client_factory: GPUControlClientFactory | None = None,
    timeout_seconds: float = _GPU_ROLLOUT_CONTROL_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> tuple[GPURolloutControlResult, ...]:
    """Advance every current member in stable UUID order without cross-card locks."""

    validate_gpu_resource_id(gpu_resource_id)
    async with session_factory() as db:
        candidates = tuple(
            (
                await db.execute(
                    select(
                        GPUBackendMembership.backend_registry_id,
                        GPUBackendMembership.membership_epoch,
                        GPUBackendMembership.state,
                    )
                    .where(
                        GPUBackendMembership.gpu_resource_id == gpu_resource_id,
                        GPUBackendMembership.state.in_(("pending", "active")),
                    )
                    .order_by(GPUBackendMembership.backend_registry_id)
                )
            ).all()
        )
    if not candidates:
        return ()
    pending = tuple(item for item in candidates if item.state != "active")
    if pending:
        return tuple(
            GPURolloutControlResult(
                backend_id=str(item.backend_registry_id),
                resource_id=gpu_resource_id,
                membership_epoch=item.membership_epoch,
                transition_id=str(transition_id),
                operation=None,
                status="blocked",
                reason="membership_not_active",
            )
            for item in pending
        )

    signer_builder = signer_factory or GPUAdmissionTokenSigner.from_settings
    try:
        signer = signer_builder()
    except Exception:  # noqa: BLE001 - never issue unsigned control operations
        return tuple(
            GPURolloutControlResult(
                backend_id=str(item.backend_registry_id),
                resource_id=gpu_resource_id,
                membership_epoch=item.membership_epoch,
                transition_id=str(transition_id),
                operation=None,
                status="blocked",
                reason="signer_unavailable",
            )
            for item in candidates
        )

    results: list[GPURolloutControlResult] = []
    for item in candidates:
        results.append(
            await advance_gpu_backend_rollout_control(
                session_factory,
                signer,
                item.backend_registry_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=item.membership_epoch,
                transition_id=transition_id,
                target_gate=target_gate,
                client_factory=client_factory,
                timeout_seconds=timeout_seconds,
                readiness_demoter=readiness_demoter,
            )
        )
    return tuple(results)


__all__ = [
    "GPURolloutControlResult",
    "advance_gpu_backend_rollout_control",
    "advance_gpu_resource_rollout_control",
]
