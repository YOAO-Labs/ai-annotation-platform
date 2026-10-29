"""Owner-side retired membership proof and proof-backed tombstone collection."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime
import uuid
from typing import Any, Literal

import structlog

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu.ledger import (
    GPUArbiterStore,
    GPUArbiterStoreError,
    GPUBackendDomainMember,
)
from aap_inference.gpu.native import (
    GPUAuthorityData,
    GPUAuthorityDataProvider,
    GPUCollectorDataProvider,
)
from aap_inference.gpu.policy import _MAX_POSITIVE_INT64
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference.gpu.endpoint_source import OwnedBackendEndpointSource
from aap_inference.gpu.transport import GPUBackendControlTransport

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GPURetiredLiveProof:
    """Owner-private response evidence; never serialize to an ordinary API client."""

    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    challenge: str
    probe_started_at: datetime
    observed_at: datetime
    evidence_deadline_ms: int
    evidence_fingerprint: str
    endpoint_sha256: str
    residency: dict[str, Any]


@dataclass(frozen=True)
class GPURetiredProbeResult:
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    reason: str
    proof: GPURetiredLiveProof | None = dataclass_field(repr=False, compare=False)


@dataclass(frozen=True)
class GPUTombstoneCollectionResult:
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    status: Literal["collected", "blocked", "stale", "error"]
    reason: str
    redis_idempotent: bool = False


def _timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("GPU retired proof timestamp is malformed")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GPU retired proof timestamp is malformed")
    return parsed.astimezone(UTC)


def _parse_backend_domain(value: object) -> tuple[GPUBackendDomainMember, ...]:
    """Decode the collector's ordered JSON domain into ledger members."""

    if type(value) is not list or not value:
        raise RuntimeError("GPU tombstone membership domain is malformed")
    members: list[GPUBackendDomainMember] = []
    previous_backend_id: str | None = None
    for item in value:
        if type(item) is not dict or set(item) != {
            "backend_id",
            "membership_epoch",
            "state",
        }:
            raise RuntimeError("GPU tombstone membership domain is malformed")
        backend_id = item["backend_id"]
        membership_epoch = item["membership_epoch"]
        state = item["state"]
        try:
            canonical_id = str(uuid.UUID(backend_id))
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError("GPU tombstone membership domain is malformed") from None
        if (
            type(backend_id) is not str
            or canonical_id != backend_id
            or type(membership_epoch) is not int
            or not 1 <= membership_epoch <= _MAX_POSITIVE_INT64
            or type(state) is not str
            or state not in {"pending", "active", "retiring"}
            or (previous_backend_id is not None and backend_id <= previous_backend_id)
        ):
            raise RuntimeError("GPU tombstone membership domain is malformed")
        previous_backend_id = backend_id
        members.append(
            GPUBackendDomainMember(
                backend_id=backend_id,
                membership_epoch=membership_epoch,
                state=state,
            )
        )
    return tuple(members)


def _probe_result(
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
    reason: str,
    proof: GPURetiredLiveProof | None = None,
) -> GPURetiredProbeResult:
    return GPURetiredProbeResult(
        backend_id=backend_id,
        resource_id=resource_id,
        membership_epoch=membership_epoch,
        reason=reason,
        proof=proof,
    )


async def probe_retired_gpu_membership(
    data: GPUAuthorityData,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
) -> GPURetiredProbeResult:
    """Obtain fresh challenge-bound evidence for one retiring tombstone."""

    validate_gpu_resource_id(resource_id)
    result = await data.step(
        "retired_probe_subject",
        {
            "backend_id": str(backend_id),
            "resource_id": resource_id,
            "membership_epoch": membership_epoch,
        },
    )
    if set(result) != {"status", "reason", "subject"}:
        raise RuntimeError("GPU retired probe subject response is malformed")
    if result["status"] != "ready":
        reason = result["reason"]
        if type(reason) is not str or not reason:
            raise RuntimeError("GPU retired probe reason is malformed")
        return _probe_result(backend_id, resource_id, membership_epoch, reason)
    subject = result["subject"]
    expected = {"challenge", "probe_started_at"}
    if type(subject) is not dict or set(subject) != expected:
        raise RuntimeError("GPU retired probe subject is malformed")
    challenge = subject["challenge"]
    if type(challenge) is not str or len(challenge) != 64:
        raise RuntimeError("GPU retired probe challenge is malformed")
    request_id = uuid.uuid4()
    endpoint = await endpoint_source.open_owned(
        backend_id,
        request_id=request_id,
        purpose="health_observation",
        binding=None,
    )
    endpoint_sha256 = endpoint.endpoint_sha256
    if type(endpoint_sha256) is not str or len(endpoint_sha256) != 64:
        raise RuntimeError("GPU retired probe endpoint digest is malformed")
    try:
        healthy, metadata = await backend_transport.health_meta(
            endpoint,
            request_id=request_id,
            gpu_health_challenge=challenge,
            timeout_seconds=8.0,
        )
    except Exception:
        return _probe_result(
            backend_id,
            resource_id,
            membership_epoch,
            "live_health_unavailable",
        )
    if not healthy or not isinstance(metadata, dict):
        return _probe_result(
            backend_id,
            resource_id,
            membership_epoch,
            "live_health_unavailable",
        )
    observed_metadata = dict(metadata)
    if observed_metadata.pop(GPU_HEALTH_CHALLENGE_ECHO_MARKER, None) != challenge:
        return _probe_result(
            backend_id,
            resource_id,
            membership_epoch,
            "challenge_echo_missing",
        )
    committed = await data.step(
        "retired_probe_commit",
        {
            "backend_id": str(backend_id),
            "resource_id": resource_id,
            "membership_epoch": membership_epoch,
            "challenge": challenge,
            "probe_started_at": subject["probe_started_at"],
            "endpoint_sha256": endpoint_sha256,
            "residency": observed_metadata.get("residency"),
        },
    )
    status = committed.get("status")
    if status != "live_unloaded":
        reason = committed.get("reason")
        if type(reason) is not str or not reason:
            raise RuntimeError("GPU retired proof rejection is malformed")
        return _probe_result(backend_id, resource_id, membership_epoch, reason)
    proof_value = committed.get("proof")
    if type(proof_value) is not dict or set(proof_value) != {
        "observed_at",
        "evidence_deadline_ms",
        "evidence_fingerprint",
        "residency",
    }:
        raise RuntimeError("GPU retired proof response is malformed")
    observed_at = _timestamp(proof_value["observed_at"])
    evidence_deadline_ms = proof_value["evidence_deadline_ms"]
    fingerprint = proof_value["evidence_fingerprint"]
    residency = proof_value["residency"]
    if (
        type(evidence_deadline_ms) is not int
        or evidence_deadline_ms <= 0
        or type(fingerprint) is not str
        or len(fingerprint) != 64
        or type(residency) is not dict
    ):
        raise RuntimeError("GPU retired proof response is malformed")
    proof = GPURetiredLiveProof(
        backend_id=backend_id,
        resource_id=resource_id,
        membership_epoch=membership_epoch,
        challenge=challenge,
        probe_started_at=_timestamp(subject["probe_started_at"]),
        observed_at=observed_at,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint=fingerprint,
        endpoint_sha256=endpoint_sha256,
        residency=residency,
    )
    return _probe_result(
        backend_id, resource_id, membership_epoch, "live_unloaded", proof
    )


async def collect_gpu_backend_tombstone(
    collector_data: GPUCollectorDataProvider,
    store: GPUArbiterStore,
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
    *,
    proof: GPURetiredLiveProof | None,
) -> GPUTombstoneCollectionResult:
    """Finalize a receipt or collect with live or owner-verified closure evidence."""

    async with collector_data.transaction() as transaction:
        arguments: dict[str, object] = {
            "phase": "prepare",
            "backend_id": str(backend_id),
            "resource_id": resource_id,
            "membership_epoch": membership_epoch,
            "proof": None,
        }
        if proof is not None:
            arguments["proof"] = {
                "challenge": proof.challenge,
                "probe_started_at": proof.probe_started_at.isoformat(),
                "observed_at": proof.observed_at.isoformat(),
                "evidence_deadline_ms": proof.evidence_deadline_ms,
                "evidence_fingerprint": proof.evidence_fingerprint,
                "endpoint_sha256": proof.endpoint_sha256,
                "residency": proof.residency,
            }
        prepared = await transaction.step("collect_tombstone", arguments)
        status = prepared.get("status")
        if status == "already_absent":
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "collected",
                "tombstone_already_absent",
                True,
            )
        if status in {"blocked", "stale"}:
            reason = prepared.get("reason")
            if type(reason) is not str or not reason:
                raise RuntimeError("GPU tombstone rejection is malformed")
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                status,
                reason,
            )
        if status not in {"receipt_required", "proof_valid", "closure_valid"}:
            raise RuntimeError("GPU tombstone preparation response is malformed")
        domain = _parse_backend_domain(prepared.get("backend_memberships"))
        retirement_id = prepared.get("retirement_id")
        registry_exists = prepared.get("registry_exists")
        if type(retirement_id) is not str or type(registry_exists) is not bool:
            raise RuntimeError("GPU tombstone preparation response is malformed")
        receipt = await store.verify_tombstone_gc_receipt(
            resource_id,
            backend_memberships=domain,
            backend_id=str(backend_id),
            membership_epoch=membership_epoch,
            retirement_id=retirement_id,
        )
        if receipt is not None:
            final = await transaction.step(
                "collect_tombstone",
                {
                    "phase": "commit",
                    "backend_id": str(backend_id),
                    "resource_id": resource_id,
                    "membership_epoch": membership_epoch,
                    "retirement_id": retirement_id,
                    "receipt_fingerprint": receipt.fingerprint,
                    "registry_exists": registry_exists,
                },
            )
            if final.get("status") != "collected":
                raise RuntimeError("GPU tombstone receipt finalization failed")
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "collected",
                "redis_receipt_finalized",
                True,
            )
        if status == "closure_valid":
            evidence_deadline_ms = prepared["evidence_deadline_ms"]
            evidence_fingerprint = prepared["evidence_fingerprint"]
        elif proof is None:
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "blocked",
                "redis_receipt_missing",
            )
        elif status != "proof_valid":
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "blocked",
                "live_proof_invalid",
            )
        else:
            evidence_deadline_ms = proof.evidence_deadline_ms
            evidence_fingerprint = proof.evidence_fingerprint
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
        if snapshot.backend_memberships != domain:
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
            backend_memberships=domain,
            backend_id=str(backend_id),
            membership_epoch=membership_epoch,
            retirement_id=retirement_id,
            vram_budget_mb=prepared["vram_budget_mb"],
            evidence_deadline_ms=evidence_deadline_ms,
            evidence_fingerprint=evidence_fingerprint,
            collection_id=prepared["collection_id"],
        )
        if collection.status != "collected":
            return GPUTombstoneCollectionResult(
                backend_id,
                resource_id,
                membership_epoch,
                "stale" if collection.status == "stale_revision" else "blocked",
                collection.reason or collection.status,
                collection.idempotent,
            )
        receipt = await store.verify_tombstone_gc_receipt(
            resource_id,
            backend_memberships=domain,
            backend_id=str(backend_id),
            membership_epoch=membership_epoch,
            retirement_id=retirement_id,
        )
        if receipt is None:
            raise GPUArbiterStoreError(
                "GPU tombstone receipt disappeared after collection"
            )
        final = await transaction.step(
            "collect_tombstone",
            {
                "phase": "commit",
                "backend_id": str(backend_id),
                "resource_id": resource_id,
                "membership_epoch": membership_epoch,
                "retirement_id": retirement_id,
                "receipt_fingerprint": receipt.fingerprint,
                "registry_exists": registry_exists,
            },
        )
        if final.get("status") != "collected":
            raise RuntimeError("GPU tombstone Native commit failed")
        return GPUTombstoneCollectionResult(
            backend_id,
            resource_id,
            membership_epoch,
            "collected",
            "proof_backed_collection_complete",
            collection.idempotent,
        )


async def retire_gpu_backend_membership(
    data: GPUAuthorityDataProvider,
    collector_data: GPUCollectorDataProvider,
    store: GPUArbiterStore,
    endpoint_source: OwnedBackendEndpointSource,
    backend_transport: GPUBackendControlTransport,
    backend_id: uuid.UUID,
    resource_id: str,
    membership_epoch: int,
) -> GPUTombstoneCollectionResult:
    """Prefer durable installation closure; keep any live probe inside its own UOW."""

    try:
        existing = await collect_gpu_backend_tombstone(
            collector_data, store, backend_id, resource_id, membership_epoch, proof=None
        )
        if existing.reason != "redis_receipt_missing":
            return existing
        async with data.transaction() as transaction:
            probe = await probe_retired_gpu_membership(
                transaction,
                endpoint_source,
                backend_transport,
                backend_id,
                resource_id,
                membership_epoch,
            )
        return await collect_gpu_backend_tombstone(
            collector_data,
            store,
            backend_id,
            resource_id,
            membership_epoch,
            proof=probe.proof,
        )
    except Exception as exc:
        logger.warning("GPU retired tombstone collection failed", exc_info=True)
        return GPUTombstoneCollectionResult(
            backend_id,
            resource_id,
            membership_epoch,
            "error",
            type(exc).__name__,
        )


__all__ = [
    "GPURetiredLiveProof",
    "GPURetiredProbeResult",
    "GPUTombstoneCollectionResult",
    "collect_gpu_backend_tombstone",
    "probe_retired_gpu_membership",
    "retire_gpu_backend_membership",
]
