"""Boot-scoped signed legacy acknowledgement for durable GPU memberships.

Moved verbatim from the legacy flat module ``gpu_membership_activation.py``. Depends on
contracts, signing, fences, proofs, control_preparation and ml_client; must not depend on
dispatch, retirement or rollout_control.
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
)
from sqlalchemy import select

from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration.signing import GPUAdmissionTokenSigner
from app.services.gpu_arbitration.control_preparation import (
    GPULegacyAckBlockedError,
    GPULegacyAckPreparation,
    GPUReadinessDemoter,
    prepare_gpu_backend_legacy_ack,
)
from app.services.gpu_arbitration.fences import GPUFenceSessionFactory
from app.services.ml_client import MLBackendClient
from app.utils.gpu_resource import validate_gpu_resource_id


_GPU_MODE_ACK_TIMEOUT_SECONDS = 8.0
_GPU_PROMOTION_BARRIER_RETRY_TIMEOUT_SECONDS = 1.0
_GPU_PROMOTION_BARRIER_RETRY_MAX_DELAY_SECONDS = 0.16


class _ModeClient(Protocol):
    async def lifecycle_mode(
        self,
        request: LifecycleModeRequest,
        *,
        admission_token: str,
    ) -> LifecycleModeResponse: ...


GPUAdmissionSignerFactory = Callable[[], GPUAdmissionTokenSigner]
GPUModeClientFactory = Callable[[MLBackendRegistry], _ModeClient]


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


async def promote_gpu_backend_membership(
    session_factory: GPUFenceSessionFactory,
    signer: GPUAdmissionTokenSigner,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    client_factory: GPUModeClientFactory | None = None,
    mode_timeout_seconds: float = _GPU_MODE_ACK_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> GPUMembershipPromotionResult:
    """Activate or recover one member, then obtain a signed legacy-mode ACK."""

    if (
        not isinstance(mode_timeout_seconds, (int, float))
        or isinstance(mode_timeout_seconds, bool)
        or mode_timeout_seconds <= 0
    ):
        raise ValueError("mode_timeout_seconds must be positive")
    loop = asyncio.get_running_loop()
    barrier_retry_deadline = loop.time() + _GPU_PROMOTION_BARRIER_RETRY_TIMEOUT_SECONDS
    barrier_retry_attempt = 0
    while True:
        try:
            preparation = await prepare_gpu_backend_legacy_ack(
                session_factory,
                backend_registry_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                readiness_demoter=readiness_demoter,
            )
            break
        except GPULegacyAckBlockedError as exc:
            remaining = barrier_retry_deadline - loop.time()
            if exc.reason != "gpu_promotion_barrier_busy" or remaining <= 0:
                return GPUMembershipPromotionResult(
                    backend_id=str(backend_registry_id),
                    resource_id=gpu_resource_id,
                    membership_epoch=membership_epoch,
                    status="blocked",
                    reason=exc.reason,
                )
            base_delay = min(
                0.01 * (2 ** min(barrier_retry_attempt, 4)),
                _GPU_PROMOTION_BARRIER_RETRY_MAX_DELAY_SECONDS,
            )
            jitter_nibble = (
                backend_registry_id.int >> ((barrier_retry_attempt % 16) * 4)
            ) & 0xF
            jitter_factor = 0.75 + (jitter_nibble / 30)
            await asyncio.sleep(min(base_delay * jitter_factor, remaining))
            barrier_retry_attempt += 1
        except Exception:  # noqa: BLE001 - durable state may be uncertain; stay off
            return GPUMembershipPromotionResult(
                backend_id=str(backend_registry_id),
                resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                status="unavailable",
                reason="activation_state_unavailable",
            )

    if preparation.action == "acknowledged":
        return _result(
            preparation,
            status="acknowledged",
            reason="legacy_ack_observed",
        )

    assert preparation.token_expires_at is not None
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
        exp=int(preparation.token_expires_at.timestamp()),
        owner=(f"membership:{preparation.backend_id}:{preparation.membership_epoch}"),
        operation=f"mode:legacy:{preparation.control_epoch}",
    )
    try:
        admission_token = signer.sign(claims)
    except Exception:  # noqa: BLE001 - activation and horizon are already durable
        return _result(
            preparation,
            status="active_unacked",
            reason="mode_token_signing_failed",
        )

    try:
        async with asyncio.timeout(mode_timeout_seconds):
            client = (
                client_factory(preparation.backend)
                if client_factory is not None
                else MLBackendClient(
                    preparation.backend, shadow_session_factory=session_factory
                )
            )
            response = await client.lifecycle_mode(
                LifecycleModeRequest(
                    gate="legacy",
                    control_epoch=preparation.control_epoch,
                ),
                admission_token=admission_token,
            )
    except TimeoutError:
        return _result(
            preparation,
            status="active_unacked",
            reason="mode_ack_timeout",
        )
    except Exception:  # noqa: BLE001 - backend rejection remains fail-closed
        return _result(
            preparation,
            status="active_unacked",
            reason="mode_ack_failed",
        )

    try:
        _validate_legacy_mode_ack(response, preparation)
    except (TypeError, ValueError, AttributeError):
        return _result(
            preparation,
            status="active_unacked",
            reason="mode_ack_invalid",
        )
    return _result(
        preparation,
        status="promoted",
        reason="legacy_ack_confirmed",
    )


async def promote_gpu_resource_memberships(
    session_factory: GPUFenceSessionFactory,
    gpu_resource_id: str,
    *,
    signer_factory: GPUAdmissionSignerFactory | None = None,
    client_factory: GPUModeClientFactory | None = None,
    mode_timeout_seconds: float = _GPU_MODE_ACK_TIMEOUT_SECONDS,
    readiness_demoter: GPUReadinessDemoter | None = None,
    pending_only: bool = False,
) -> tuple[GPUMembershipPromotionResult, ...]:
    """Promote current members of one card in stable backend UUID order."""

    validate_gpu_resource_id(gpu_resource_id)
    if not isinstance(pending_only, bool):
        raise ValueError("pending_only must be a boolean")
    states = ("pending",) if pending_only else ("pending", "active")
    async with session_factory() as db:
        candidates = tuple(
            (
                await db.execute(
                    select(
                        GPUBackendMembership.backend_registry_id,
                        GPUBackendMembership.membership_epoch,
                    )
                    .where(
                        GPUBackendMembership.gpu_resource_id == gpu_resource_id,
                        GPUBackendMembership.state.in_(states),
                    )
                    .order_by(GPUBackendMembership.backend_registry_id)
                )
            ).all()
        )
    if not candidates:
        return ()

    signer_builder = signer_factory or GPUAdmissionTokenSigner.from_settings
    try:
        signer = signer_builder()
    except Exception:  # noqa: BLE001 - never activate without a usable signer
        if readiness_demoter is not None:
            try:
                await readiness_demoter(gpu_resource_id)
            except Exception:  # noqa: BLE001 - Redis readiness is not proven revoked
                return tuple(
                    GPUMembershipPromotionResult(
                        backend_id=str(backend_id),
                        resource_id=gpu_resource_id,
                        membership_epoch=membership_epoch,
                        status="unavailable",
                        reason="readiness_demotion_failed",
                    )
                    for backend_id, membership_epoch in candidates
                )
        return tuple(
            GPUMembershipPromotionResult(
                backend_id=str(backend_id),
                resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                status="blocked",
                reason="signer_unavailable",
            )
            for backend_id, membership_epoch in candidates
        )

    results: list[GPUMembershipPromotionResult] = []
    for backend_id, membership_epoch in candidates:
        results.append(
            await promote_gpu_backend_membership(
                session_factory,
                signer,
                backend_id,
                gpu_resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                client_factory=client_factory,
                mode_timeout_seconds=mode_timeout_seconds,
                readiness_demoter=readiness_demoter,
            )
        )
    return tuple(results)


__all__ = [
    "GPUMembershipPromotionResult",
    "promote_gpu_backend_membership",
    "promote_gpu_resource_memberships",
]
