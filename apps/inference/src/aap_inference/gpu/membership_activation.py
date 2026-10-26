"""Owner-side boot-scoped signed legacy acknowledgements for GPU memberships."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    LifecycleModeRequest,
    LifecycleModeResponse,
)
from aap_inference.gpu.backend_dispatch import parse_gpu_shadow_backend_facts
from aap_inference.gpu.control_preparation import (
    GPULegacyAckBlockedError,
    GPULegacyAckPreparation,
    GPUReadinessDemoter,
    prepare_gpu_backend_legacy_ack,
)
from aap_inference.gpu.endpoint_identity import canonical_gpu_backend_endpoint
from aap_inference.gpu.endpoint_source import OwnedBackendEndpointSource
from aap_inference.gpu.native import GPUAuthorityData
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference.gpu.transport import GPUBackendControlTransport

_GPU_MODE_ACK_TIMEOUT_SECONDS = 8.0
_GPU_PROMOTION_BARRIER_RETRY_TIMEOUT_SECONDS = 1.0
_GPU_PROMOTION_BARRIER_RETRY_MAX_DELAY_SECONDS = 0.16
GPUAdmissionSignerFactory = Callable[[], GPUAdmissionTokenSigner]


@dataclass(frozen=True)
class GPUMembershipPromotionResult:
    backend_id: str
    resource_id: str
    membership_epoch: int
    status: Literal[
        "promoted",
        "acknowledged",
        "blocked",
        "active_unacked",
        "unavailable",
    ]
    reason: str
    runtime_epoch: str | None = None
    control_epoch: str | None = None
    requires_proof_reset: bool = True


def _result(
    preparation: GPULegacyAckPreparation,
    *,
    status: Literal["promoted", "acknowledged", "active_unacked"],
    reason: str,
) -> GPUMembershipPromotionResult:
    return GPUMembershipPromotionResult(
        backend_id=str(preparation.backend_id),
        resource_id=preparation.resource_id,
        membership_epoch=preparation.membership_epoch,
        status=status,
        reason=reason,
        runtime_epoch=preparation.runtime_epoch,
        control_epoch=preparation.control_epoch,
        requires_proof_reset=(status != "acknowledged" or not preparation.proof_ready),
    )


def _validate_legacy_mode_ack(
    response: LifecycleModeResponse,
    preparation: GPULegacyAckPreparation,
) -> None:
    residency = response.residency
    identity = residency.identity
    pool_states = tuple(pool.resident for pool in residency.pools.values())
    if (
        response.ok is not True
        or response.gate.value != "legacy"
        or response.control_epoch != preparation.control_epoch
        or residency.boot_id != preparation.boot_id
        or identity is None
        or identity.audience != "aap-gpu-lifecycle"
        or identity.backend_registry_id != str(preparation.backend_id)
        or identity.gpu_resource_id != preparation.resource_id
        or residency.generation is not None
        or residency.evictable
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.draining
        or residency.state.value not in {"unloaded", "resident"}
        or residency.gpu_loaded is None
        or not pool_states
        or any(type(item) is not bool for item in pool_states)
    ):
        raise ValueError("legacy mode acknowledgement is not stable and exact")
    if residency.gpu_loaded is True and (
        residency.state.value != "resident" or not any(pool_states)
    ):
        raise ValueError("legacy mode acknowledgement has inconsistent residency")


def _stored_boot_id(health_meta: object) -> str | None:
    if type(health_meta) is not dict:
        return None
    residency = health_meta.get("residency")
    if type(residency) is not dict:
        return None
    boot_id = residency.get("boot_id")
    return boot_id if type(boot_id) is str and boot_id else None


async def _lifecycle_alias_reason(
    data: GPUAuthorityData,
    endpoint_source: OwnedBackendEndpointSource,
    backend_id: uuid.UUID,
    resource_id: str,
) -> str | None:
    """Compare private endpoint identities and persisted boot IDs before activation."""

    result = await data.step(
        "shadow_snapshot",
        {"resource_id": None, "backend_id": None},
    )
    facts = parse_gpu_shadow_backend_facts(result)
    rows = [
        (item.backend_id, item.gpu_resource_id, item.health_meta)
        for item in facts
        if item.gpu_resource_id is not None
    ]
    if not any(
        row_id == backend_id and row_resource_id == resource_id
        for row_id, row_resource_id, _health_meta in rows
    ):
        raise RuntimeError("GPU lifecycle alias target is absent")

    target_row = next(row for row in rows if row[0] == backend_id)
    target_boot_id = _stored_boot_id(target_row[2])
    if target_boot_id is not None and any(
        row_id != backend_id and _stored_boot_id(health_meta) == target_boot_id
        for row_id, _resource_id, health_meta in rows
    ):
        return "lifecycle_boot_id_aliased"

    identities: dict[uuid.UUID, str] = {}
    for row_id, _resource_id, _health_meta in rows:
        endpoint = await endpoint_source.open_owned(
            row_id,
            request_id=uuid.uuid4(),
            purpose="health_observation",
            binding=None,
        )
        identities[row_id] = canonical_gpu_backend_endpoint(endpoint.url)
    target_identity = identities[backend_id]
    if any(
        row_id != backend_id and identity == target_identity
        for row_id, identity in identities.items()
    ):
        return "lifecycle_endpoint_aliased"
    return None


async def promote_gpu_backend_membership(
    data: GPUAuthorityData,
    signer: GPUAdmissionTokenSigner,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    mode_timeout_seconds: float = _GPU_MODE_ACK_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> GPUMembershipPromotionResult:
    """Activate/recover one member and obtain a real signed legacy-mode ACK."""

    if (
        not isinstance(mode_timeout_seconds, (int, float))
        or isinstance(mode_timeout_seconds, bool)
        or mode_timeout_seconds <= 0
    ):
        raise ValueError("mode_timeout_seconds must be positive")
    try:
        alias_reason = await _lifecycle_alias_reason(
            data,
            endpoint_source,
            backend_registry_id,
            gpu_resource_id,
        )
    except Exception:
        if readiness_demoter is not None:
            try:
                await readiness_demoter(gpu_resource_id)
            except Exception:
                return GPUMembershipPromotionResult(
                    backend_id=str(backend_registry_id),
                    resource_id=gpu_resource_id,
                    membership_epoch=membership_epoch,
                    status="unavailable",
                    reason="readiness_demotion_failed",
                )
        return GPUMembershipPromotionResult(
            backend_id=str(backend_registry_id),
            resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            status="unavailable",
            reason="lifecycle_alias_check_unavailable",
        )
    if alias_reason is not None:
        if readiness_demoter is not None:
            try:
                await readiness_demoter(gpu_resource_id)
            except Exception:
                return GPUMembershipPromotionResult(
                    backend_id=str(backend_registry_id),
                    resource_id=gpu_resource_id,
                    membership_epoch=membership_epoch,
                    status="unavailable",
                    reason="readiness_demotion_failed",
                )
        return GPUMembershipPromotionResult(
            backend_id=str(backend_registry_id),
            resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            status="blocked",
            reason=alias_reason,
        )
    try:
        preparation = await prepare_gpu_backend_legacy_ack(
            data,
            backend_registry_id,
            gpu_resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            readiness_demoter=readiness_demoter,
        )
    except GPULegacyAckBlockedError as exc:
        return GPUMembershipPromotionResult(
            backend_id=str(backend_registry_id),
            resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            status="blocked",
            reason=exc.reason,
        )
    if preparation.action == "acknowledged":
        return _result(
            preparation,
            status="acknowledged",
            reason="legacy_mode_already_acknowledged",
        )
    token_expires_at = preparation.token_expires_at
    if token_expires_at is None:
        return _result(
            preparation,
            status="active_unacked",
            reason="control_token_horizon_missing",
        )
    claims = AdmissionTokenClaims(
        backend_registry_id=str(preparation.backend_id),
        gpu_resource_id=preparation.resource_id,
        boot_id=preparation.boot_id,
        generation=None,
        control_epoch=preparation.control_epoch,
        scope=AdmissionScope.MODE,
        jti=(
            f"mode:{preparation.backend_id}:{preparation.membership_epoch}:"
            f"{preparation.control_epoch}:legacy"
        ),
        exp=int(token_expires_at.timestamp()),
        owner=f"membership:{preparation.backend_id}:{preparation.membership_epoch}",
        operation=f"mode:legacy:{preparation.control_epoch}",
    )
    try:
        admission_token = signer.sign(claims)
        request_id = uuid.uuid4()
        async with asyncio.timeout(mode_timeout_seconds):
            async with backend_transport.control_binding(
                preparation.backend_id,
                request_id=request_id,
                operation="mode",
            ) as binding:
                endpoint = await endpoint_source.open_owned(
                    preparation.backend_id,
                    request_id=request_id,
                    purpose="gpu_control",
                    binding=binding,
                )
                response = await backend_transport.lifecycle_mode(
                    endpoint,
                    LifecycleModeRequest(
                        gate="legacy",
                        control_epoch=preparation.control_epoch,
                    ),
                    admission_token,
                    request_id=request_id,
                    timeout_seconds=mode_timeout_seconds,
                )
    except TimeoutError:
        return _result(preparation, status="active_unacked", reason="mode_ack_timeout")
    except Exception:
        return _result(preparation, status="active_unacked", reason="mode_ack_failed")

    try:
        _validate_legacy_mode_ack(response, preparation)
    except (TypeError, ValueError, AttributeError):
        return _result(preparation, status="active_unacked", reason="mode_ack_invalid")
    return _result(preparation, status="promoted", reason="legacy_ack_confirmed")


async def promote_gpu_resource_memberships(
    data: GPUAuthorityData,
    gpu_resource_id: str,
    *,
    signer_factory: GPUAdmissionSignerFactory,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    mode_timeout_seconds: float = _GPU_MODE_ACK_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
    pending_only: bool = False,
) -> tuple[GPUMembershipPromotionResult, ...]:
    """Promote current members in stable backend UUID order."""

    validate_gpu_resource_id(gpu_resource_id)
    if not isinstance(pending_only, bool):
        raise ValueError("pending_only must be a boolean")
    result = await data.step(
        "membership_candidates",
        {
            "gpu_resource_id": gpu_resource_id,
            "states": ["pending"] if pending_only else ["pending", "active"],
        },
    )
    if set(result) != {"candidates"} or type(result["candidates"]) is not list:
        raise RuntimeError("GPU membership candidate response is malformed")
    candidates: list[tuple[uuid.UUID, int]] = []
    for item in result["candidates"]:
        if type(item) is not dict or set(item) != {
            "backend_id",
            "membership_epoch",
            "state",
        }:
            raise RuntimeError("GPU membership candidate is malformed")
        backend_id_value = item["backend_id"]
        epoch = item["membership_epoch"]
        state = item["state"]
        if (
            type(backend_id_value) is not str
            or type(epoch) is not int
            or not 1 <= epoch <= 9_223_372_036_854_775_807
            or type(state) is not str
            or state not in ({"pending"} if pending_only else {"pending", "active"})
        ):
            raise RuntimeError("GPU membership candidate is malformed")
        try:
            backend_id = uuid.UUID(backend_id_value)
        except ValueError:
            raise RuntimeError("GPU membership candidate is malformed") from None
        if str(backend_id) != backend_id_value:
            raise RuntimeError("GPU membership candidate is malformed")
        candidates.append((backend_id, epoch))
    if tuple(candidates) != tuple(sorted(candidates, key=lambda item: item[0].int)):
        raise RuntimeError("GPU membership candidates are not UUID ordered")
    if not candidates:
        return ()

    try:
        signer = signer_factory()
    except Exception:
        if readiness_demoter is not None:
            try:
                await readiness_demoter(gpu_resource_id)
            except Exception:
                return tuple(
                    GPUMembershipPromotionResult(
                        backend_id=str(backend_id),
                        resource_id=gpu_resource_id,
                        membership_epoch=epoch,
                        status="unavailable",
                        reason="readiness_demotion_failed",
                    )
                    for backend_id, epoch in candidates
                )
        return tuple(
            GPUMembershipPromotionResult(
                backend_id=str(backend_id),
                resource_id=gpu_resource_id,
                membership_epoch=epoch,
                status="blocked",
                reason="signer_unavailable",
            )
            for backend_id, epoch in candidates
        )

    results = []
    for backend_id, membership_epoch in candidates:
        try:
            result = await promote_gpu_backend_membership(
                data,
                signer,
                endpoint_source,
                backend_transport,
                backend_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                mode_timeout_seconds=mode_timeout_seconds,
                readiness_demoter=readiness_demoter,
            )
        except Exception:
            result = GPUMembershipPromotionResult(
                backend_id=str(backend_id),
                resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                status="unavailable",
                reason="membership_promotion_unavailable",
            )
        results.append(result)
    return tuple(results)


__all__ = [
    "GPUAdmissionSignerFactory",
    "GPUMembershipPromotionResult",
    "promote_gpu_backend_membership",
    "promote_gpu_resource_memberships",
]
