"""Owner-side rollout-bound reset/mode control over exact sealed endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
import uuid

from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetRequest,
    LifecycleResetResponse,
)
from aap_inference.gpu.control_preparation import (
    GPUReadinessDemoter,
    GPURolloutControlBlockedError,
    GPURolloutControlPreparation,
    prepare_gpu_backend_rollout_control,
)
from aap_inference.gpu.endpoint_source import OwnedBackendEndpointSource
from aap_inference.gpu.native import GPUAuthorityData
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference.gpu.transport import GPUBackendControlTransport

_GPU_ROLLOUT_CONTROL_TIMEOUT_SECONDS = 8.0
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
    data: GPUAuthorityData,
    signer: GPUAdmissionTokenSigner,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    transition_id: uuid.UUID,
    target_gate: Literal["legacy", "enforce"],
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
            data,
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
    except Exception:
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
        return _result(preparation, status="pending", reason=preparation.reason)

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
    except Exception:
        return _result(
            preparation,
            status="unavailable",
            reason="control_token_signing_failed",
        )
    request_id = uuid.uuid4()
    try:
        async with asyncio.timeout(timeout_seconds):
            route_operation = "reset" if preparation.operation == "reset" else "mode"
            async with backend_transport.control_binding(
                preparation.backend_id,
                request_id=request_id,
                operation=route_operation,
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    preparation.backend_id,
                    request_id=request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                if preparation.operation == "reset":
                    response = await backend_transport.lifecycle_reset(
                        endpoint,
                        LifecycleResetRequest(control_epoch=preparation.control_epoch),
                        admission_token,
                        request_id=request_id,
                        timeout_seconds=timeout_seconds,
                    )
                    _validate_reset_ack(response, preparation)
                else:
                    gate = (
                        "enforce"
                        if preparation.operation == "mode_enforce"
                        else "legacy"
                    )
                    response = await backend_transport.lifecycle_mode(
                        endpoint,
                        LifecycleModeRequest(
                            gate=gate,
                            control_epoch=preparation.control_epoch,
                        ),
                        admission_token,
                        request_id=request_id,
                        timeout_seconds=timeout_seconds,
                    )
                    _validate_mode_ack(response, preparation)
    except TimeoutError:
        return _result(preparation, status="unavailable", reason="control_ack_timeout")
    except Exception:
        return _result(preparation, status="unavailable", reason="control_ack_failed")
    return _result(
        preparation,
        status="issued",
        reason=f"{preparation.operation}_ack_received_awaiting_health",
    )


async def advance_gpu_resource_rollout_control(
    data: GPUAuthorityData,
    gpu_resource_id: str,
    *,
    transition_id: uuid.UUID,
    target_gate: Literal["legacy", "enforce"],
    signer_factory: GPUAdmissionSignerFactory,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    timeout_seconds: float = _GPU_ROLLOUT_CONTROL_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> tuple[GPURolloutControlResult, ...]:
    """Advance all current memberships in stable UUID order, without card overlap."""

    validate_gpu_resource_id(gpu_resource_id)
    result = await data.step(
        "membership_candidates",
        {"gpu_resource_id": gpu_resource_id, "states": ["pending", "active"]},
    )
    if set(result) != {"candidates"} or type(result["candidates"]) is not list:
        raise RuntimeError("GPU membership candidate response is malformed")
    candidates: list[tuple[uuid.UUID, int, str]] = []
    for item in result["candidates"]:
        if type(item) is not dict or set(item) != {
            "backend_id",
            "membership_epoch",
            "state",
        }:
            raise RuntimeError("GPU membership candidate is malformed")
        backend_id = uuid.UUID(item["backend_id"])
        epoch = item["membership_epoch"]
        state = item["state"]
        if type(epoch) is not int or epoch <= 0 or state not in {"pending", "active"}:
            raise RuntimeError("GPU membership candidate is malformed")
        candidates.append((backend_id, epoch, state))
    if tuple(candidates) != tuple(sorted(candidates, key=lambda item: item[0].int)):
        raise RuntimeError("GPU membership candidates are not UUID ordered")
    if not candidates:
        return ()
    pending = tuple(item for item in candidates if item[2] != "active")
    if pending:
        return tuple(
            GPURolloutControlResult(
                backend_id=str(backend_id),
                resource_id=gpu_resource_id,
                membership_epoch=epoch,
                transition_id=str(transition_id),
                operation=None,
                status="blocked",
                reason="membership_not_active",
            )
            for backend_id, epoch, _state in pending
        )
    try:
        signer = signer_factory()
    except Exception:
        return tuple(
            GPURolloutControlResult(
                backend_id=str(backend_id),
                resource_id=gpu_resource_id,
                membership_epoch=epoch,
                transition_id=str(transition_id),
                operation=None,
                status="blocked",
                reason="signer_unavailable",
            )
            for backend_id, epoch, _state in candidates
        )
    results = []
    for backend_id, membership_epoch, _state in candidates:
        results.append(
            await advance_gpu_backend_rollout_control(
                data,
                signer,
                endpoint_source,
                backend_transport,
                backend_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                transition_id=transition_id,
                target_gate=target_gate,
                timeout_seconds=timeout_seconds,
                readiness_demoter=readiness_demoter,
            )
        )
    return tuple(results)


__all__ = [
    "GPUAdmissionSignerFactory",
    "GPURolloutControlResult",
    "advance_gpu_backend_rollout_control",
    "advance_gpu_resource_rollout_control",
]
