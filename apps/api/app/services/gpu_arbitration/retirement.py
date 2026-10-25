"""Retired live proof probe and tombstone GC collection.

Moved verbatim from the legacy aggregate ``gpu_arbiter.py``. This module owns the
retired-backend live-health probe, the tombstone collection receipt, and the
tombstone deletion path. It is the only GPU domain module (besides dispatch,
membership, rollout-control) allowed to depend on ``ml_client``.

Depends on proofs, fences, ledger, ml_client, config DB models and SQLAlchemy.
Must not depend on dispatch, membership_activation or rollout_control.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import secrets
import uuid
from typing import Any, Literal

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration.fences import GPUFenceSessionFactory
from app.services.gpu_arbitration.ledger import (
    GPUArbiterStore,
    GPUArbiterStoreError,
)
from app.services.gpu_arbitration.policy import _HEALTH_EVIDENCE_MAX_AGE
from app.services.gpu_arbitration.proofs import (
    _GPUProofInvalid,
    _canonical_proof_timestamp,
    _datetime_to_epoch_ms,
    _gpu_domain_members,
    _lock_gpu_resource_proof_domain,
    _optional_datetime_document,
    _parse_gpu_proof_residency,
)
from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from app.services.ml_client import MLBackendClient
from app.utils.gpu_resource import validate_gpu_resource_id


logger = structlog.get_logger(__name__)

_GPU_HEALTH_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class GPURetiredLiveProof:
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    challenge: str
    probe_started_at: datetime
    observed_at: datetime
    evidence_deadline_ms: int
    evidence_fingerprint: str
    registry_url: str
    registry_auth_method: str
    registry_auth_token: str | None = dataclass_field(repr=False)
    residency: dict[str, Any]


@dataclass(frozen=True)
class GPURetiredProbeResult:
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    reason: str
    proof: GPURetiredLiveProof | None = None


@dataclass(frozen=True)
class GPUTombstoneCollectionResult:
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    status: Literal["collected", "blocked", "stale", "error"]
    reason: str
    redis_idempotent: bool = False


def _retired_proof_fingerprint(
    membership: GPUBackendMembership,
    *,
    challenge: str,
    probe_started_at: datetime,
    observed_at: datetime,
    evidence_deadline_ms: int,
    registry_url: str,
    registry_auth_method: str,
    registry_auth_token: str | None,
    residency: Mapping[str, Any],
) -> str:
    document = {
        "schema": "gpu-arbiter-retired-proof/v1",
        "membership": {
            "backend_id": str(membership.backend_registry_id),
            "resource_id": membership.gpu_resource_id,
            "membership_epoch": str(membership.membership_epoch),
            "retirement_id": str(membership.retirement_id),
            "state": membership.state,
            "vram_budget_mb": membership.vram_budget_mb,
            "retired_generation_high_water": (membership.retired_generation_high_water),
            "retired_control_epoch_high_water": (
                membership.retired_control_epoch_high_water
            ),
            "retired_runtime_epoch_high_water": (
                membership.retired_runtime_epoch_high_water
            ),
            "retired_token_expiry_high_water": _optional_datetime_document(
                membership.retired_token_expiry_high_water
            ),
        },
        "probe": {
            "challenge": challenge,
            "probe_started_at": _canonical_proof_timestamp(probe_started_at),
            "observed_at": _canonical_proof_timestamp(observed_at),
            "evidence_deadline_ms": evidence_deadline_ms,
        },
        "route": {
            "url": registry_url,
            "auth_method": registry_auth_method,
            "auth_token_sha256": hashlib.sha256(
                (registry_auth_token or "").encode("utf-8")
            ).hexdigest(),
        },
        "residency": residency,
    }
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_retired_live_unloaded(
    membership: GPUBackendMembership,
    *,
    probe_started_at: datetime,
    observed_at: datetime,
    residency_document: Any,
) -> dict[str, Any]:
    if membership.state != "retiring":
        raise _GPUProofInvalid("membership_not_retiring")
    if membership.retirement_id is None:
        raise _GPUProofInvalid("retirement_identity_missing")
    if (
        probe_started_at.tzinfo is None
        or probe_started_at.utcoffset() is None
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise _GPUProofInvalid("probe_timestamp_invalid")
    horizon = membership.retired_token_expiry_high_water
    if horizon is not None:
        if horizon.tzinfo is None or horizon.utcoffset() is None:
            raise _GPUProofInvalid("retired_token_horizon_invalid")
        if probe_started_at.astimezone(UTC) <= horizon.astimezone(UTC):
            raise _GPUProofInvalid("probe_not_after_retired_token_horizon")
    if probe_started_at >= observed_at:
        raise _GPUProofInvalid("probe_clock_order_invalid")
    residency = _parse_gpu_proof_residency(residency_document)
    identity = residency.identity
    if identity is None:
        raise _GPUProofInvalid("residency_identity_missing")
    if (
        identity["backend_registry_id"] != str(membership.backend_registry_id)
        or identity["gpu_resource_id"] != membership.gpu_resource_id
    ):
        raise _GPUProofInvalid("residency_identity_mismatch")
    if (
        residency.state not in {"unloaded", "resident"}
        or residency.gpu_loaded is not False
        or residency.active_requests != 0
        or residency.builders != 0
        or residency.borrowers != 0
        or residency.draining
        or residency.evictable
        or any(item is not False for item in residency.pool_residencies)
    ):
        raise _GPUProofInvalid("residency_not_stably_unloaded")
    if residency.generation is not None and (
        membership.retired_generation_high_water is None
        or int(residency.generation) > membership.retired_generation_high_water
    ):
        raise _GPUProofInvalid("residency_generation_ahead")
    if residency.control_epoch is not None and (
        membership.retired_control_epoch_high_water is None
        or int(residency.control_epoch) > membership.retired_control_epoch_high_water
    ):
        raise _GPUProofInvalid("residency_control_epoch_ahead")
    return residency.raw


async def probe_retired_gpu_membership(
    session_factory: GPUFenceSessionFactory,
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
    *,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
) -> GPURetiredProbeResult:
    """Obtain fresh challenge-bound evidence for one retiring tombstone."""

    validate_gpu_resource_id(resource_id)
    if evidence_ttl <= timedelta(0) or evidence_ttl > _HEALTH_EVIDENCE_MAX_AGE:
        raise ValueError("retired GPU evidence TTL must be within three minutes")
    async with session_factory() as db:
        membership = await db.scalar(
            select(GPUBackendMembership).where(
                GPUBackendMembership.backend_registry_id == backend_id,
                GPUBackendMembership.gpu_resource_id == resource_id,
                GPUBackendMembership.membership_epoch == membership_epoch,
                GPUBackendMembership.state == "retiring",
            )
        )
        registry = await db.get(MLBackendRegistry, backend_id)
        probe_started_at = await db.scalar(select(func.clock_timestamp()))
    if membership is None:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "tombstone_missing"
        )
    if registry is None:
        return GPURetiredProbeResult(
            backend_id,
            resource_id,
            membership_epoch,
            "registry_missing_for_live_gc",
        )
    if probe_started_at is None:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "probe_clock_missing"
        )
    if probe_started_at.tzinfo is None or probe_started_at.utcoffset() is None:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "probe_timestamp_invalid"
        )
    horizon = membership.retired_token_expiry_high_water
    if horizon is not None:
        if horizon.tzinfo is None or horizon.utcoffset() is None:
            return GPURetiredProbeResult(
                backend_id,
                resource_id,
                membership_epoch,
                "retired_token_horizon_invalid",
            )
        if probe_started_at.astimezone(UTC) <= horizon.astimezone(UTC):
            return GPURetiredProbeResult(
                backend_id,
                resource_id,
                membership_epoch,
                "waiting_token_horizon",
            )

    challenge = secrets.token_hex(32)

    healthy, meta = await MLBackendClient(registry).health_meta(
        gpu_health_challenge=challenge
    )
    async with session_factory() as db:
        observed_at = await db.scalar(select(func.clock_timestamp()))
    if observed_at is None:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "probe_clock_missing"
        )
    if not healthy or type(meta) is not dict:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "live_health_unavailable"
        )
    meta = dict(meta)
    if meta.pop(GPU_HEALTH_CHALLENGE_ECHO_MARKER, None) != challenge:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, "challenge_echo_missing"
        )
    try:
        residency = _validate_retired_live_unloaded(
            membership,
            probe_started_at=probe_started_at,
            observed_at=observed_at,
            residency_document=meta.get("residency"),
        )
    except _GPUProofInvalid as exc:
        return GPURetiredProbeResult(
            backend_id, resource_id, membership_epoch, exc.reason
        )
    evidence_deadline_ms = _datetime_to_epoch_ms(observed_at + evidence_ttl)
    fingerprint = _retired_proof_fingerprint(
        membership,
        challenge=challenge,
        probe_started_at=probe_started_at,
        observed_at=observed_at,
        evidence_deadline_ms=evidence_deadline_ms,
        registry_url=registry.url,
        registry_auth_method=registry.auth_method,
        registry_auth_token=registry.auth_token,
        residency=residency,
    )
    proof = GPURetiredLiveProof(
        backend_id=backend_id,
        resource_id=resource_id,
        membership_epoch=membership_epoch,
        challenge=challenge,
        probe_started_at=probe_started_at,
        observed_at=observed_at,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint=fingerprint,
        registry_url=registry.url,
        registry_auth_method=registry.auth_method,
        registry_auth_token=registry.auth_token,
        residency=residency,
    )
    return GPURetiredProbeResult(
        backend_id, resource_id, membership_epoch, "live_unloaded", proof
    )


async def _delete_gpu_tombstone_from_receipt(
    db: AsyncSession,
    membership: GPUBackendMembership,
    *,
    receipt_fingerprint: str,
    registry_exists: bool,
) -> None:
    if membership.retirement_id is None:
        raise RuntimeError("GPU tombstone retirement identity is missing")
    receipt = json.dumps(
        {
            "backend_id": str(membership.backend_registry_id),
            "resource_id": membership.gpu_resource_id,
            "membership_epoch": str(membership.membership_epoch),
            "retirement_id": str(membership.retirement_id),
            "fingerprint": receipt_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    await db.execute(
        text("SELECT set_config('app.gpu_tombstone_gc_receipt', :receipt, true)"),
        {"receipt": receipt},
    )
    result = await db.execute(
        delete(GPUBackendMembership).where(
            GPUBackendMembership.backend_registry_id == membership.backend_registry_id,
            GPUBackendMembership.gpu_resource_id == membership.gpu_resource_id,
            GPUBackendMembership.membership_epoch == membership.membership_epoch,
            GPUBackendMembership.state == "retiring",
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("GPU tombstone changed before collection")
    remaining = await db.scalar(
        select(func.count())
        .select_from(GPUBackendMembership)
        .where(
            GPUBackendMembership.backend_registry_id == membership.backend_registry_id
        )
    )
    if not registry_exists and remaining == 0:
        await db.execute(
            delete(GPUBackendFence).where(
                GPUBackendFence.backend_registry_id == membership.backend_registry_id
            )
        )


async def collect_gpu_backend_tombstone(
    session_factory: GPUFenceSessionFactory,
    store: GPUArbiterStore,
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
    *,
    proof: GPURetiredLiveProof | None,
) -> GPUTombstoneCollectionResult:
    """Finalize an existing Redis receipt or consume new live proof in two stages."""

    async with session_factory() as db:
        async with db.begin():
            locked = await _lock_gpu_resource_proof_domain(db, resource_id)
            membership = next(
                (
                    item
                    for item in locked.memberships
                    if item.backend_registry_id == backend_id
                    and item.membership_epoch == membership_epoch
                    and item.state == "retiring"
                ),
                None,
            )
            if membership is None:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "collected",
                    "tombstone_already_absent",
                    True,
                )
            durable_domain = _gpu_domain_members(locked.memberships)
            receipt = await store.verify_tombstone_gc_receipt(
                resource_id,
                backend_memberships=durable_domain,
                backend_id=str(backend_id),
                membership_epoch=membership_epoch,
                retirement_id=str(membership.retirement_id),
            )
            if receipt is not None:
                await _delete_gpu_tombstone_from_receipt(
                    db,
                    membership,
                    receipt_fingerprint=receipt.fingerprint,
                    registry_exists=backend_id in locked.registries,
                )
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "collected",
                    "redis_receipt_finalized",
                    True,
                )
            if proof is None:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "redis_receipt_missing",
                )
            registry = locked.registries.get(backend_id)
            if (
                proof.backend_id != backend_id
                or proof.resource_id != resource_id
                or proof.membership_epoch != membership_epoch
            ):
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "live_proof_membership_mismatch",
                )
            if _GPU_HEALTH_CHALLENGE_RE.fullmatch(proof.challenge) is None:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "live_proof_challenge_invalid",
                )
            if registry is None:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "registry_missing_for_live_gc",
                )
            if (
                registry.url != proof.registry_url
                or registry.auth_method != proof.registry_auth_method
                or registry.auth_token != proof.registry_auth_token
            ):
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "stale",
                    "live_probe_route_changed",
                )
            try:
                residency = _validate_retired_live_unloaded(
                    membership,
                    probe_started_at=proof.probe_started_at,
                    observed_at=proof.observed_at,
                    residency_document=proof.residency,
                )
            except _GPUProofInvalid as exc:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    exc.reason,
                )
            observed_at_ms = _datetime_to_epoch_ms(proof.observed_at)
            maximum_deadline_ms = _datetime_to_epoch_ms(
                proof.observed_at + _HEALTH_EVIDENCE_MAX_AGE
            )
            if not (observed_at_ms < proof.evidence_deadline_ms <= maximum_deadline_ms):
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "live_proof_deadline_invalid",
                )
            if (
                locked.db_now < proof.observed_at
                or locked.db_now - proof.observed_at > _HEALTH_EVIDENCE_MAX_AGE
                or _datetime_to_epoch_ms(locked.db_now) >= proof.evidence_deadline_ms
            ):
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "stale",
                    "live_proof_expired",
                )
            fingerprint = _retired_proof_fingerprint(
                membership,
                challenge=proof.challenge,
                probe_started_at=proof.probe_started_at,
                observed_at=proof.observed_at,
                evidence_deadline_ms=proof.evidence_deadline_ms,
                registry_url=proof.registry_url,
                registry_auth_method=proof.registry_auth_method,
                registry_auth_token=proof.registry_auth_token,
                residency=residency,
            )
            if fingerprint != proof.evidence_fingerprint:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    "live_proof_fingerprint_mismatch",
                )
            try:
                snapshot = await store.snapshot(resource_id)
            except GPUArbiterStoreError as exc:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "blocked",
                    str(exc),
                )
            if snapshot.backend_memberships != durable_domain:
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    "stale",
                    "membership_domain_changed",
                )
            collection = await store.collect_retired_backend(
                resource_id,
                expected_ledger_revision=snapshot.ledger_revision,
                expected_ledger_incarnation=snapshot.ledger_incarnation,
                backend_memberships=durable_domain,
                backend_id=str(backend_id),
                membership_epoch=membership_epoch,
                retirement_id=str(membership.retirement_id),
                vram_budget_mb=membership.vram_budget_mb,
                evidence_deadline_ms=proof.evidence_deadline_ms,
                evidence_fingerprint=proof.evidence_fingerprint,
                collection_id=hashlib.sha256(
                    (
                        proof.evidence_fingerprint
                        + ":"
                        + str(backend_id)
                        + ":"
                        + str(membership_epoch)
                        + ":"
                        + str(membership.retirement_id)
                    ).encode("utf-8")
                ).hexdigest(),
            )
            if collection.status != "collected":
                return GPUTombstoneCollectionResult(
                    backend_id,
                    resource_id,
                    membership_epoch,
                    ("stale" if collection.status == "stale_revision" else "blocked"),
                    collection.reason or collection.status,
                    collection.idempotent,
                )
            receipt = await store.verify_tombstone_gc_receipt(
                resource_id,
                backend_memberships=durable_domain,
                backend_id=str(backend_id),
                membership_epoch=membership_epoch,
                retirement_id=str(membership.retirement_id),
            )
            if receipt is None:
                raise GPUArbiterStoreError(
                    "GPU tombstone receipt disappeared after collection"
                )
            await _delete_gpu_tombstone_from_receipt(
                db,
                membership,
                receipt_fingerprint=receipt.fingerprint,
                registry_exists=True,
            )
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "collected",
                "proof_backed_collection_complete",
                collection.idempotent,
            )
