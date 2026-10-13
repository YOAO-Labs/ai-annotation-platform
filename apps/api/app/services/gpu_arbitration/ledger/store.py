"""Async Redis ledger client for GPU arbitration (ADR-0049).

The store owns one async Redis client created in the current event loop. Callers must
close it before that loop exits; no client or connection pool is kept at module scope.
All scripts touch keys from one physical resource hash slot and never perform network
or database work while the atomic section is running.

This is the orchestration layer: it depends on the ledger primitive submodules and
registers the 15 final Lua scripts from :mod:`gpu_arbitration.ledger.scripts`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services.gpu_arbitration.ledger.keys import GPUArbiterKeys, gpu_arbiter_keys
from app.services.gpu_arbitration.ledger.scripts import (
    _ADMIT_LUA,
    _ARM_EVICTION_CANCEL_LUA,
    _BEGIN_IDLE_EVICTION_LUA,
    _BEGIN_PROOF_RESET_LUA,
    _COLLECT_RETIRED_BACKEND_LUA,
    _COMMIT_PROOF_RESET_LUA,
    _EVOLVE_BACKEND_DOMAINS_LUA,
    _LEASE_LUA,
    _MARK_CARD_NOT_READY_LUA,
    _QUEUE_LUA,
    _RECONCILE_CARD_LUA,
    _SWEEP_LEASES_LUA,
    _TRANSITION_LUA,
    _TRANSITION_OWNER_LUA,
    _VERIFY_TOMBSTONE_GC_LUA,
)
from app.services.gpu_arbitration.ledger.types import (
    _CANONICAL_POSITIVE_INT64_RE,
    _DEFAULT_NAMESPACE,
    _GPUBackendDomains,
    _LEDGER_REVISION_REBASE_THRESHOLD,
    _MAX_GPU_BACKEND_CONCURRENCY,
    _MAX_GPU_BACKENDS_PER_RESOURCE,
    _MAX_GPU_QUEUE_LENGTH,
    _MAX_POSITIVE_INT64,
    _MAX_REDIS_SAFE_INTEGER,
    _NAMESPACE_RE,
    _REDIS_CALL_DEADLINE_SECONDS,
    _REDIS_OPERATION_TIMEOUT_SECONDS,
    _RedisResultT,
    _SHA256_HEX_RE,
    _SNAPSHOT_MAX_ATTEMPTS,
    _TOMBSTONE_GC_RECEIPT_TTL_MS,
    GPU_COLD_ADMISSION_OPERATION,
    GPU_EVICTION_OPERATION,
    GPUAdmissionResult,
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStoreError,
    GPUBackendDomainEvolutionResult,
    GPUBackendDomainMember,
    GPUCardSnapshot,
    GPUEvictionBranchResult,
    GPUIdleEvictionResult,
    GPULeaseMutationResult,
    GPUProofResetCAS,
    GPUProofResetContext,
    GPUQueueResult,
    GPUQueueTicket,
    GPUReconcileLeaseCleanup,
    GPUReconcileResult,
    GPURequestLease,
    GPURequestLeaseState,
    GPUTombstoneGCReceipt,
    GPUTombstoneGCResult,
    GPUTransitionOwnerResult,
    GPUTransitionResult,
)
from app.services.gpu_arbitration.ledger.validation import (
    _canonical_backend_domains,
    _decode_tombstone_receipt_target_domains,
    _validate_allocation_generation,
    _validate_generation,
    _validate_membership_epoch,
    _validate_nonempty,
    _validate_nonnegative_redis_safe_int,
    _validate_positive_int,
    _validate_redis_safe_int,
    _validate_retirement_id,
    _validate_ttl_ms,
    normalize_gpu_backend_max_concurrency,
)

_MAX_RECONCILE_DEADLINE_GRACE_MS = 300_000


class GPUArbiterStore:
    """One event-loop-owned async Redis ledger client."""

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = _DEFAULT_NAMESPACE,
    ) -> None:
        if not isinstance(namespace, str) or _NAMESPACE_RE.fullmatch(namespace) is None:
            raise ValueError("invalid GPU arbiter Redis namespace")
        self._redis = redis
        self.namespace = namespace
        self._closed = False
        self._mark_card_not_ready_script = redis.register_script(
            _MARK_CARD_NOT_READY_LUA
        )
        self._begin_proof_reset_script = redis.register_script(_BEGIN_PROOF_RESET_LUA)
        self._commit_proof_reset_script = redis.register_script(_COMMIT_PROOF_RESET_LUA)
        self._reconcile_card_script = redis.register_script(_RECONCILE_CARD_LUA)
        self._evolve_backend_domains_script = redis.register_script(
            _EVOLVE_BACKEND_DOMAINS_LUA
        )
        self._collect_retired_backend_script = redis.register_script(
            _COLLECT_RETIRED_BACKEND_LUA
        )
        self._verify_tombstone_gc_script = redis.register_script(
            _VERIFY_TOMBSTONE_GC_LUA
        )
        self._admit_script = redis.register_script(_ADMIT_LUA)
        self._lease_script = redis.register_script(_LEASE_LUA)
        self._sweep_leases_script = redis.register_script(_SWEEP_LEASES_LUA)
        self._queue_script = redis.register_script(_QUEUE_LUA)
        self._transition_owner_script = redis.register_script(_TRANSITION_OWNER_LUA)
        self._begin_idle_eviction_script = redis.register_script(
            _BEGIN_IDLE_EVICTION_LUA
        )
        self._arm_eviction_cancel_script = redis.register_script(
            _ARM_EVICTION_CANCEL_LUA
        )
        self._transition_script = redis.register_script(_TRANSITION_LUA)

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        namespace: str = _DEFAULT_NAMESPACE,
    ) -> "GPUArbiterStore":
        client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
            retry_on_timeout=False,
        )
        return cls(client, namespace=namespace)

    async def __aenter__(self) -> "GPUArbiterStore":
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.aclose()
        except GPUArbiterStoreError:
            if exc is None:
                raise

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            async with asyncio.timeout(_REDIS_CALL_DEADLINE_SECONDS):
                await self._redis.aclose()
        except (RedisError, OSError, TimeoutError) as exc:
            raise GPUArbiterStoreError("gpu_arbiter_unavailable") from exc
        self._closed = True

    async def ping(self) -> bool:
        return bool(await self._call(self._redis.ping))

    def _ensure_open(self) -> None:
        if self._closed:
            raise GPUArbiterStoreError("GPU arbiter store is closed")

    async def _call(
        self, operation: Callable[[], Awaitable[_RedisResultT]]
    ) -> _RedisResultT:
        self._ensure_open()
        try:
            async with asyncio.timeout(_REDIS_CALL_DEADLINE_SECONDS):
                return await operation()
        except (RedisError, OSError, TimeoutError) as exc:
            raise GPUArbiterStoreError("gpu_arbiter_unavailable") from exc

    def keys(self, resource_id: str) -> GPUArbiterKeys:
        return gpu_arbiter_keys(resource_id, namespace=self.namespace)

    async def _ledger_domain(self, keys: GPUArbiterKeys) -> _GPUBackendDomains:
        (
            _,
            incarnation,
            backend_raw,
            _,
            membership_raw,
            _,
            active_raw,
            _,
        ) = await self._call(
            lambda: self._redis.hmget(
                keys.card,
                "resource_id",
                "ledger_incarnation",
                "backend_domain",
                "backend_domain_fingerprint",
                "membership_domain",
                "membership_domain_fingerprint",
                "active_backend_domain",
                "active_backend_domain_fingerprint",
            )
        )
        incarnation = incarnation or ""

        def raw_fingerprint(value: str | None) -> tuple[str, str]:
            raw = value or ""
            return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()

        backend_raw, backend_fingerprint = raw_fingerprint(backend_raw)
        membership_raw, membership_fingerprint = raw_fingerprint(membership_raw)
        active_raw, active_fingerprint = raw_fingerprint(active_raw)
        try:
            backend_values = json.loads(backend_raw)
            membership_values = json.loads(membership_raw)
            active_values = json.loads(active_raw)
            if (
                not isinstance(backend_values, list)
                or not isinstance(membership_values, list)
                or not isinstance(active_values, list)
                or len(backend_values) > _MAX_GPU_BACKENDS_PER_RESOURCE
                or len(membership_values) != len(backend_values)
                or backend_values != sorted(set(backend_values))
                or active_values != sorted(set(active_values))
                or json.dumps(backend_values, separators=(",", ":")) != backend_raw
                or json.dumps(active_values, separators=(",", ":")) != active_raw
            ):
                raise ValueError("backend membership domain is invalid")
            memberships = tuple(
                GPUBackendDomainMember(
                    backend_id=item["backend_id"],
                    membership_epoch=int(item["membership_epoch"]),
                    state=item["state"],
                )
                for item in membership_values
                if isinstance(item, dict)
                and set(item) == {"backend_id", "membership_epoch", "state"}
                and isinstance(item.get("membership_epoch"), str)
                and _CANONICAL_POSITIVE_INT64_RE.fullmatch(item["membership_epoch"])
                is not None
                and int(item["membership_epoch"]) <= _MAX_POSITIVE_INT64
            )
            canonical = _canonical_backend_domains(
                memberships, ledger_incarnation=incarnation
            )
            if (
                canonical.backend_domain_raw != backend_raw
                or canonical.membership_domain_raw != membership_raw
                or canonical.active_backend_domain_raw != active_raw
            ):
                raise ValueError("backend membership domain is not canonical")
            return canonical
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _GPUBackendDomains(
                backend_domain_raw=backend_raw,
                backend_domain_fingerprint=backend_fingerprint,
                membership_domain_raw=membership_raw,
                membership_domain_fingerprint=membership_fingerprint,
                active_backend_domain_raw=active_raw,
                active_backend_domain_fingerprint=active_fingerprint,
                backend_ids=(),
                active_backend_ids=(),
                backend_memberships=(),
                ledger_incarnation=incarnation,
            )

    @staticmethod
    def _ledger_keys(keys: GPUArbiterKeys, backend_ids: Sequence[str]) -> list[str]:
        return [
            keys.card,
            keys.allocations,
            keys.queue,
            keys.transition,
            *(
                key
                for backend_id in backend_ids
                for key in (
                    keys.leases(backend_id),
                    keys.backend_queue(backend_id),
                )
            ),
        ]

    async def configure_card(
        self,
        resource_id: str,
        allocatable_mb: int,
        *,
        ready: bool,
    ) -> int:
        if not isinstance(ready, bool):
            raise ValueError("ready must be a boolean")
        if ready:
            raise ValueError("ready cards must be committed by reconcile_card")
        result = await self.mark_card_not_ready(
            resource_id,
            allocatable_mb,
            reason="bootstrap_incomplete",
        )
        if result.reason == "proof_reset_in_progress":
            raise GPUArbiterStoreError("gpu_arbiter_proof_reset_in_progress")
        return result.committed_mb

    async def mark_card_not_ready(
        self,
        resource_id: str,
        allocatable_mb: int,
        *,
        reason: str,
    ) -> GPUReconcileResult:
        keys = self.keys(resource_id)
        allocatable_mb = _validate_positive_int(allocatable_mb, "allocatable_mb")
        _validate_nonempty(reason, "reason", max_length=256)
        raw = await self._call(
            lambda: self._mark_card_not_ready_script(
                keys=[keys.card],
                args=[
                    resource_id,
                    allocatable_mb,
                    reason,
                    hashlib.sha256(b"[]").hexdigest(),
                    uuid.uuid4().hex,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUReconcileResult(
            status=payload["status"],
            ready=False,
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            reason=str(payload.get("reason", reason)),
        )

    async def begin_proof_reset(
        self,
        resource_id: str,
        allocatable_mb: int,
        *,
        expected_ledger_revision: int | None,
        expected_ledger_incarnation: str | None,
        backend_memberships: Sequence[GPUBackendDomainMember],
        reset_id: str,
    ) -> GPUReconcileResult:
        """Freeze one resource before consuming durable reset proof.

        The supplied membership domain must be the durable closed world for this
        resource. Child keys are intentionally preserved until ``commit_proof_reset``
        validates fresh evidence and replaces the ledger atomically.
        """

        keys = self.keys(resource_id)
        allocatable_mb = _validate_positive_int(allocatable_mb, "allocatable_mb")
        _validate_nonempty(reset_id, "reset_id", max_length=256)
        if expected_ledger_revision is not None:
            if (
                isinstance(expected_ledger_revision, bool)
                or not isinstance(expected_ledger_revision, int)
                or expected_ledger_revision <= 0
                or expected_ledger_revision > _MAX_REDIS_SAFE_INTEGER
            ):
                raise ValueError(
                    "expected_ledger_revision must be a positive Redis-safe integer"
                )
        if (expected_ledger_revision is None) != (expected_ledger_incarnation is None):
            raise ValueError(
                "expected ledger revision and incarnation must be supplied together"
            )
        if expected_ledger_incarnation is not None:
            _validate_nonempty(
                expected_ledger_incarnation,
                "expected_ledger_incarnation",
                max_length=128,
            )
        domains = _canonical_backend_domains(backend_memberships)
        begin_document = {
            "active_backend_domain": json.loads(domains.active_backend_domain_raw),
            "allocatable_mb": allocatable_mb,
            "backend_domain": json.loads(domains.backend_domain_raw),
            "expected_ledger_incarnation": expected_ledger_incarnation,
            "expected_ledger_revision": expected_ledger_revision,
            "membership_domain": json.loads(domains.membership_domain_raw),
            "resource_id": resource_id,
        }
        begin_fingerprint = hashlib.sha256(
            json.dumps(begin_document, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        raw = await self._call(
            lambda: self._begin_proof_reset_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    allocatable_mb,
                    expected_ledger_revision or "",
                    expected_ledger_incarnation or "",
                    reset_id,
                    begin_fingerprint,
                    uuid.uuid4().hex,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    *domains.backend_ids,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUReconcileResult(
            status=payload["status"],
            ready=bool(payload.get("ready", False)),
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            purged_leases=int(payload.get("purged_leases", 0)),
            reason=str(payload.get("reason", "")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def prepared_proof_reset(
        self, resource_id: str
    ) -> GPUProofResetContext | None:
        """Return the durable prepared context needed to resume after a restart."""

        keys = self.keys(resource_id)
        card = await self._call(lambda: self._redis.hgetall(keys.card))
        if not card:
            return None
        if card.get("resource_id") != resource_id:
            raise GPUArbiterStoreError("GPU ledger resource identity mismatch")
        proof_state = card.get("proof_reset_state")
        if proof_state is None:
            return None
        if proof_state != "prepared":
            raise GPUArbiterStoreError("GPU proof reset state is invalid")
        try:
            ledger_revision = int(card["ledger_revision"])
            proof_revision = int(card["proof_reset_revision"])
            if ledger_revision != 1 or proof_revision != ledger_revision:
                raise ValueError("proof reset revision is invalid")
            ledger_incarnation = _validate_nonempty(
                card["ledger_incarnation"],
                "ledger_incarnation",
                max_length=128,
            )
            if card["proof_reset_incarnation"] != ledger_incarnation:
                raise ValueError("proof reset incarnation is invalid")
            allocatable_mb = _validate_positive_int(
                int(card["allocatable_mb"]), "allocatable_mb"
            )
            prepared_at_ms = _validate_positive_int(
                int(card["proof_reset_prepared_at_ms"]),
                "proof_reset_prepared_at_ms",
            )
            if int(card["updated_at_ms"]) != prepared_at_ms:
                raise ValueError("proof reset update timestamp is invalid")
            reset_id = _validate_nonempty(
                card["proof_reset_id"], "proof_reset_id", max_length=256
            )
            begin_fingerprint = card["proof_reset_begin_fingerprint"]
            if _SHA256_HEX_RE.fullmatch(begin_fingerprint) is None:
                raise ValueError("proof reset begin fingerprint is invalid")
            if (
                card.get("ledger_version") not in {"2", "3"}
                or card.get("bootstrap_state") != "not_ready"
                or card.get("reconcile_deadline_ms") != "0"
                or card.get("not_ready_reason") != "proof_reset_in_progress"
                or card.get("committed_mb") != "0"
                or card.get("allocation_count") != "0"
                or card.get("lease_counts") != "{}"
                or card.get("card_queue_count") != "0"
                or card.get("backend_queue_counts") != "{}"
                or card.get("transition_mirror") != ""
            ):
                raise ValueError("proof reset card schema is invalid")
            membership_values = json.loads(card["membership_domain"])
            if not isinstance(membership_values, list) or any(
                not isinstance(item, dict)
                or set(item) != {"backend_id", "membership_epoch", "state"}
                or not isinstance(item["membership_epoch"], str)
                or _CANONICAL_POSITIVE_INT64_RE.fullmatch(item["membership_epoch"])
                is None
                or int(item["membership_epoch"]) > _MAX_POSITIVE_INT64
                for item in membership_values
            ):
                raise ValueError("proof reset membership domain is invalid")
            domains = _canonical_backend_domains(
                tuple(
                    GPUBackendDomainMember(
                        backend_id=item["backend_id"],
                        membership_epoch=int(item["membership_epoch"]),
                        state=item["state"],
                    )
                    for item in membership_values
                ),
                ledger_incarnation=ledger_incarnation,
            )
            if (
                card["backend_domain"] != domains.backend_domain_raw
                or card["backend_domain_fingerprint"]
                != domains.backend_domain_fingerprint
                or card["membership_domain"] != domains.membership_domain_raw
                or card["membership_domain_fingerprint"]
                != domains.membership_domain_fingerprint
                or card["active_backend_domain"] != domains.active_backend_domain_raw
                or card["active_backend_domain_fingerprint"]
                != domains.active_backend_domain_fingerprint
            ):
                raise ValueError("proof reset membership domains are invalid")
            return GPUProofResetContext(
                resource_id=resource_id,
                allocatable_mb=allocatable_mb,
                reset_id=reset_id,
                begin_fingerprint=begin_fingerprint,
                ledger_revision=ledger_revision,
                ledger_incarnation=ledger_incarnation,
                prepared_at_ms=prepared_at_ms,
                backend_ids=domains.backend_ids,
                active_backend_ids=domains.active_backend_ids,
                backend_memberships=domains.backend_memberships,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GPUArbiterStoreError("GPU proof reset context decode failed") from exc

    async def proof_reset_cas(self, resource_id: str) -> GPUProofResetCAS | None:
        """Read only the core CAS pair needed to begin a strict proof reset.

        A missing card or a card whose core revision/incarnation is itself corrupt
        returns ``None`` and therefore selects the reset primitive's no-CAS branch.
        Other schema fields are deliberately not trusted here.
        """

        keys = self.keys(resource_id)
        resource, revision_raw, incarnation = await self._call(
            lambda: self._redis.hmget(
                keys.card,
                "resource_id",
                "ledger_revision",
                "ledger_incarnation",
            )
        )
        if resource is None:
            return None
        if resource != resource_id:
            raise GPUArbiterStoreError("GPU ledger resource identity mismatch")
        try:
            revision = int(revision_raw)
            if (
                revision_raw != str(revision)
                or revision <= 0
                or revision > _MAX_REDIS_SAFE_INTEGER
            ):
                return None
            incarnation = _validate_nonempty(
                incarnation,
                "ledger_incarnation",
                max_length=128,
            )
        except (TypeError, ValueError):
            return None
        return GPUProofResetCAS(
            ledger_revision=revision,
            ledger_incarnation=incarnation,
        )

    async def commit_proof_reset(
        self,
        resource_id: str,
        allocatable_mb: int,
        *,
        reset_id: str,
        expected_reset_revision: int,
        expected_reset_incarnation: str,
        backend_memberships: Sequence[GPUBackendDomainMember],
        allocations: Sequence[GPUAllocation],
        ready: bool,
        evidence_deadline_ms: int,
        proof_fingerprint: str,
    ) -> GPUReconcileResult:
        """Replace a prepared ledger with one proof-bound conservative snapshot."""

        keys = self.keys(resource_id)
        allocatable_mb = _validate_positive_int(allocatable_mb, "allocatable_mb")
        _validate_nonempty(reset_id, "reset_id", max_length=256)
        if (
            isinstance(expected_reset_revision, bool)
            or not isinstance(expected_reset_revision, int)
            or expected_reset_revision <= 0
            or expected_reset_revision > _MAX_REDIS_SAFE_INTEGER
        ):
            raise ValueError(
                "expected_reset_revision must be a positive Redis-safe integer"
            )
        _validate_nonempty(
            expected_reset_incarnation,
            "expected_reset_incarnation",
            max_length=128,
        )
        if not isinstance(ready, bool):
            raise ValueError("ready must be a boolean")
        if not ready:
            if type(evidence_deadline_ms) is not int or evidence_deadline_ms != 0:
                raise ValueError(
                    "evidence_deadline_ms must be zero when ready is false"
                )
        else:
            evidence_deadline_ms = _validate_positive_int(
                evidence_deadline_ms, "evidence_deadline_ms"
            )
        if (
            not isinstance(proof_fingerprint, str)
            or _SHA256_HEX_RE.fullmatch(proof_fingerprint) is None
        ):
            raise ValueError(
                "proof_fingerprint must be a 64-character lowercase SHA-256 digest"
            )

        domains = _canonical_backend_domains(backend_memberships)
        if ready and not domains.active_backend_ids:
            raise ValueError("ready proof reset requires an active backend")
        target_allocations: list[dict[str, Any]] = []
        seen_backend_ids: set[str] = set()
        known_backend_ids = set(domains.backend_ids)
        for allocation in allocations:
            if not isinstance(allocation, GPUAllocation):
                raise ValueError(
                    "proof reset allocations must contain GPUAllocation values"
                )
            if allocation.state not in {
                GPUAllocationState.UNKNOWN,
                GPUAllocationState.RESIDENT,
            }:
                raise ValueError("proof reset allocations must be unknown or resident")
            if allocation.state is GPUAllocationState.UNKNOWN and allocation.evictable:
                raise ValueError("proof reset unknown allocations cannot be evictable")
            if allocation.backend_id not in known_backend_ids:
                raise ValueError("proof reset allocation is outside the backend domain")
            if allocation.backend_id in seen_backend_ids:
                raise ValueError("proof reset allocations must be unique by backend")
            seen_backend_ids.add(allocation.backend_id)
            target_allocations.append(self._allocation_to_payload(allocation))
        target_allocations.sort(key=lambda item: item["backend_id"])
        commit_document = {
            "active_backend_domain": json.loads(domains.active_backend_domain_raw),
            "allocatable_mb": allocatable_mb,
            "allocations": target_allocations,
            "backend_domain": json.loads(domains.backend_domain_raw),
            "evidence_deadline_ms": evidence_deadline_ms,
            "expected_reset_incarnation": expected_reset_incarnation,
            "expected_reset_revision": expected_reset_revision,
            "membership_domain": json.loads(domains.membership_domain_raw),
            "proof_fingerprint": proof_fingerprint,
            "ready": ready,
            "reset_id": reset_id,
            "resource_id": resource_id,
        }
        commit_fingerprint = hashlib.sha256(
            json.dumps(commit_document, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        raw = await self._call(
            lambda: self._commit_proof_reset_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    allocatable_mb,
                    reset_id,
                    expected_reset_revision,
                    expected_reset_incarnation,
                    "1" if ready else "0",
                    evidence_deadline_ms,
                    proof_fingerprint,
                    commit_fingerprint,
                    json.dumps(target_allocations, separators=(",", ":")),
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    *domains.backend_ids,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUReconcileResult(
            status=payload["status"],
            ready=bool(payload.get("ready", False)),
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            purged_leases=int(payload.get("purged_leases", 0)),
            reason=str(payload.get("reason", "")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def reconcile_card(
        self,
        resource_id: str,
        allocatable_mb: int,
        *,
        expected_ledger_revision: int | None,
        expected_ledger_incarnation: str | None,
        backend_memberships: Sequence[GPUBackendDomainMember],
        allocations: Sequence[GPUAllocation],
        lease_cleanup: Mapping[str, GPUReconcileLeaseCleanup] | None,
        ready: bool,
        reconcile_deadline_ms: int,
        repair_id: str,
    ) -> GPUReconcileResult:
        """Atomically repair one resource from a durable closed-world domain.

        ``backend_memberships`` must be authoritative and include current members plus
        retained membership tombstones. Active registry rows alone are not sufficient:
        omitting a retired backend can hide a surviving per-backend Redis child
        key after a partial flush. The P3c-2 reconciler is responsible for
        producing that durable domain evidence before enforce can call this
        primitive.
        """
        keys = self.keys(resource_id)
        allocatable_mb = _validate_positive_int(allocatable_mb, "allocatable_mb")
        reconcile_deadline_ms = _validate_positive_int(
            reconcile_deadline_ms, "reconcile_deadline_ms"
        )
        _validate_nonempty(repair_id, "repair_id", max_length=256)
        if not isinstance(ready, bool):
            raise ValueError("ready must be a boolean")
        if expected_ledger_revision is not None:
            if (
                isinstance(expected_ledger_revision, bool)
                or not isinstance(expected_ledger_revision, int)
                or expected_ledger_revision <= 0
                or expected_ledger_revision > _MAX_REDIS_SAFE_INTEGER
            ):
                raise ValueError(
                    "expected_ledger_revision must be a positive Redis-safe integer"
                )
        if (expected_ledger_revision is None) != (expected_ledger_incarnation is None):
            raise ValueError(
                "expected ledger revision and incarnation must be supplied together"
            )
        if expected_ledger_incarnation is not None:
            _validate_nonempty(
                expected_ledger_incarnation,
                "expected_ledger_incarnation",
                max_length=128,
            )

        domains = _canonical_backend_domains(backend_memberships)
        domain = domains.backend_ids
        active_domain = domains.active_backend_ids
        seen_backend_ids = set(domain)
        if ready and not active_domain:
            raise ValueError("ready reconciliation requires an active backend domain")

        allocation_payloads: list[dict[str, Any]] = []
        seen_allocations: set[str] = set()
        for allocation in allocations:
            if not isinstance(allocation, GPUAllocation):
                raise ValueError("allocations must contain GPUAllocation values")
            if allocation.backend_id not in seen_backend_ids:
                raise ValueError("allocation backend is outside backend_ids")
            if allocation.backend_id in seen_allocations:
                raise ValueError("allocations must not contain duplicate backends")
            seen_allocations.add(allocation.backend_id)
            allocation_payloads.append(self._allocation_to_payload(allocation))
        allocation_payloads.sort(key=lambda item: item["backend_id"])

        cleanup_payload: dict[str, dict[str, Any]] = {}
        for backend_id, evidence in (lease_cleanup or {}).items():
            if backend_id not in seen_backend_ids:
                raise ValueError("lease cleanup backend is outside backend_ids")
            if not isinstance(evidence, GPUReconcileLeaseCleanup):
                raise ValueError(
                    "lease_cleanup values must be GPUReconcileLeaseCleanup"
                )
            observed_idle_at_ms = _validate_positive_int(
                evidence.observed_idle_at_ms, "observed_idle_at_ms"
            )
            lease_ids: list[str] = []
            seen_lease_ids: set[str] = set()
            for lease_id in evidence.lease_ids:
                lease_id = _validate_nonempty(lease_id, "lease_id", max_length=256)
                if lease_id in seen_lease_ids:
                    raise ValueError("lease cleanup ids must be unique")
                seen_lease_ids.add(lease_id)
                lease_ids.append(lease_id)
            cleanup_payload[backend_id] = {
                "observed_idle_at_ms": observed_idle_at_ms,
                "lease_ids": sorted(lease_ids),
            }

        repair_document = {
            "resource_id": resource_id,
            "allocatable_mb": allocatable_mb,
            "backend_domain": json.loads(domains.backend_domain_raw),
            "membership_domain": json.loads(domains.membership_domain_raw),
            "active_backend_domain": json.loads(domains.active_backend_domain_raw),
            "allocations": allocation_payloads,
            "lease_cleanup": cleanup_payload,
            "ready": ready,
            "reconcile_deadline_ms": reconcile_deadline_ms,
        }
        canonical_document = json.dumps(
            repair_document,
            sort_keys=True,
            separators=(",", ":"),
        )
        repair_fingerprint = hashlib.sha256(
            canonical_document.encode("utf-8")
        ).hexdigest()
        proposed_incarnation = uuid.uuid4().hex
        raw = await self._call(
            lambda: self._reconcile_card_script(
                keys=[
                    keys.card,
                    keys.allocations,
                    keys.queue,
                    keys.transition,
                    *(
                        key
                        for backend_id in domain
                        for key in (
                            keys.leases(backend_id),
                            keys.backend_queue(backend_id),
                        )
                    ),
                ],
                args=[
                    resource_id,
                    ""
                    if expected_ledger_revision is None
                    else str(expected_ledger_revision),
                    allocatable_mb,
                    "1" if ready else "0",
                    reconcile_deadline_ms,
                    repair_id,
                    json.dumps(
                        allocation_payloads,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        cleanup_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    repair_fingerprint,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    expected_ledger_incarnation or "",
                    proposed_incarnation,
                    *domain,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUReconcileResult(
            status=payload["status"],
            ready=bool(payload.get("ready", False)),
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            purged_leases=int(payload.get("purged_leases", 0)),
            reason=str(payload.get("reason", "")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def evolve_backend_domains(
        self,
        resource_id: str,
        *,
        expected_ledger_revision: int,
        expected_ledger_incarnation: str,
        backend_memberships: Sequence[GPUBackendDomainMember],
        evolution_id: str,
    ) -> GPUBackendDomainEvolutionResult:
        """Atomically expand or change membership state while the card is fail-closed.

        This stage deliberately forbids all-domain shrink. Retiring members remain in
        the bounded key domain until the later proof-backed collection state machine
        can remove their Redis children and durable tombstone together.
        """

        keys = self.keys(resource_id)
        if (
            isinstance(expected_ledger_revision, bool)
            or not isinstance(expected_ledger_revision, int)
            or expected_ledger_revision <= 0
            or expected_ledger_revision > _MAX_REDIS_SAFE_INTEGER
        ):
            raise ValueError(
                "expected_ledger_revision must be a positive Redis-safe integer"
            )
        _validate_nonempty(
            expected_ledger_incarnation,
            "expected_ledger_incarnation",
            max_length=128,
        )
        _validate_nonempty(evolution_id, "evolution_id", max_length=256)
        current = await self._ledger_domain(keys)
        target = _canonical_backend_domains(backend_memberships)
        evolution_document = {
            "resource_id": resource_id,
            "backend_domain": json.loads(target.backend_domain_raw),
            "membership_domain": json.loads(target.membership_domain_raw),
            "active_backend_domain": json.loads(target.active_backend_domain_raw),
        }
        evolution_fingerprint = hashlib.sha256(
            json.dumps(
                evolution_document, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        raw = await self._call(
            lambda: self._evolve_backend_domains_script(
                keys=self._ledger_keys(keys, target.backend_ids),
                args=[
                    resource_id,
                    expected_ledger_revision,
                    expected_ledger_incarnation,
                    evolution_id,
                    evolution_fingerprint,
                    current.backend_domain_raw,
                    current.backend_domain_fingerprint,
                    current.membership_domain_raw,
                    current.membership_domain_fingerprint,
                    current.active_backend_domain_raw,
                    current.active_backend_domain_fingerprint,
                    target.backend_domain_raw,
                    target.backend_domain_fingerprint,
                    target.membership_domain_raw,
                    target.membership_domain_fingerprint,
                    target.active_backend_domain_raw,
                    target.active_backend_domain_fingerprint,
                    *target.backend_ids,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUBackendDomainEvolutionResult(
            status=payload["status"],
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            requested_backend_ids=target.backend_ids,
            requested_active_backend_ids=target.active_backend_ids,
            requested_backend_memberships=target.backend_memberships,
            reason=str(payload.get("reason", "")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def collect_retired_backend(
        self,
        resource_id: str,
        *,
        expected_ledger_revision: int,
        expected_ledger_incarnation: str,
        backend_memberships: Sequence[GPUBackendDomainMember],
        backend_id: str,
        membership_epoch: int,
        retirement_id: str,
        vram_budget_mb: int,
        evidence_deadline_ms: int,
        evidence_fingerprint: str,
        collection_id: str,
    ) -> GPUTombstoneGCResult:
        """Atomically collect one proven-unloaded retiring Redis member.

        This primitive is intentionally separate from ordinary domain evolution:
        it is the only v2 operation that may shrink the all-domain, and it always
        leaves the physical card fail-closed for a later proof-backed repair.
        """

        keys = self.keys(resource_id)
        expected_ledger_revision = _validate_positive_int(
            expected_ledger_revision, "expected_ledger_revision"
        )
        _validate_nonempty(
            expected_ledger_incarnation,
            "expected_ledger_incarnation",
            max_length=128,
        )
        backend_id = _validate_nonempty(backend_id, "backend_id", max_length=128)
        membership_epoch = _validate_membership_epoch(membership_epoch)
        retirement_id = _validate_retirement_id(retirement_id)
        vram_budget_mb = _validate_positive_int(vram_budget_mb, "vram_budget_mb")
        evidence_deadline_ms = _validate_positive_int(
            evidence_deadline_ms, "evidence_deadline_ms"
        )
        if (
            not isinstance(evidence_fingerprint, str)
            or _SHA256_HEX_RE.fullmatch(evidence_fingerprint) is None
        ):
            raise ValueError("evidence_fingerprint must be a SHA-256 hex digest")
        _validate_nonempty(collection_id, "collection_id", max_length=256)

        current = _canonical_backend_domains(backend_memberships)
        matches = [
            item
            for item in current.backend_memberships
            if item.backend_id == backend_id
            and item.membership_epoch == membership_epoch
            and item.state == "retiring"
        ]
        if len(matches) != 1:
            raise ValueError("collection target must be one exact retiring member")
        target = _canonical_backend_domains(
            tuple(
                item
                for item in current.backend_memberships
                if item.backend_id != backend_id
            )
        )
        collection_document = {
            "resource_id": resource_id,
            "backend_id": backend_id,
            "membership_epoch": str(membership_epoch),
            "retirement_id": retirement_id,
            "expected_ledger_revision": expected_ledger_revision,
            "expected_ledger_incarnation": expected_ledger_incarnation,
            "vram_budget_mb": vram_budget_mb,
            "evidence_deadline_ms": evidence_deadline_ms,
            "evidence_fingerprint": evidence_fingerprint,
            "current_membership_domain": json.loads(current.membership_domain_raw),
            "target_membership_domain": json.loads(target.membership_domain_raw),
        }
        collection_fingerprint = hashlib.sha256(
            json.dumps(
                collection_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        raw = await self._call(
            lambda: self._collect_retired_backend_script(
                keys=[
                    *self._ledger_keys(keys, current.backend_ids),
                    keys.tombstone_gc_receipt(
                        backend_id,
                        membership_epoch,
                        retirement_id,
                    ),
                ],
                args=[
                    resource_id,
                    backend_id,
                    membership_epoch,
                    expected_ledger_revision,
                    expected_ledger_incarnation,
                    collection_id,
                    collection_fingerprint,
                    evidence_deadline_ms,
                    vram_budget_mb,
                    current.backend_domain_raw,
                    current.backend_domain_fingerprint,
                    current.membership_domain_raw,
                    current.membership_domain_fingerprint,
                    current.active_backend_domain_raw,
                    current.active_backend_domain_fingerprint,
                    target.backend_domain_raw,
                    target.backend_domain_fingerprint,
                    target.membership_domain_raw,
                    target.membership_domain_fingerprint,
                    target.active_backend_domain_raw,
                    target.active_backend_domain_fingerprint,
                    retirement_id,
                    _TOMBSTONE_GC_RECEIPT_TTL_MS,
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUTombstoneGCResult(
            status=payload["status"],
            ledger_revision=int(payload.get("ledger_revision", 0)),
            ledger_incarnation=str(payload.get("ledger_incarnation", "")),
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            reason=str(payload.get("reason", "")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def verify_tombstone_gc_receipt(
        self,
        resource_id: str,
        *,
        backend_memberships: Sequence[GPUBackendDomainMember],
        backend_id: str,
        membership_epoch: int,
        retirement_id: str,
    ) -> GPUTombstoneGCReceipt | None:
        """Atomically verify a Redis-collected/DB-pending tombstone receipt."""

        keys = self.keys(resource_id)
        backend_id = _validate_nonempty(backend_id, "backend_id", max_length=128)
        membership_epoch = _validate_membership_epoch(membership_epoch)
        retirement_id = _validate_retirement_id(retirement_id)
        current = _canonical_backend_domains(backend_memberships)
        if not any(
            item.backend_id == backend_id
            and item.membership_epoch == membership_epoch
            and item.state == "retiring"
            for item in current.backend_memberships
        ):
            raise ValueError("receipt target must be one exact retiring member")
        receipt_key = keys.tombstone_gc_receipt(
            backend_id,
            membership_epoch,
            retirement_id,
        )
        receipt_raw = await self._call(lambda: self._redis.get(receipt_key))
        target = _decode_tombstone_receipt_target_domains(receipt_raw)
        if target is None:
            return None
        key_backend_ids = tuple(
            sorted(set(current.backend_ids) | set(target.backend_ids))
        )
        key_backend_domain_raw = json.dumps(key_backend_ids, separators=(",", ":"))
        raw = await self._call(
            lambda: self._verify_tombstone_gc_script(
                keys=[
                    *self._ledger_keys(keys, key_backend_ids),
                    receipt_key,
                ],
                args=[
                    resource_id,
                    backend_id,
                    membership_epoch,
                    target.backend_domain_raw,
                    target.backend_domain_fingerprint,
                    target.membership_domain_raw,
                    target.membership_domain_fingerprint,
                    target.active_backend_domain_raw,
                    target.active_backend_domain_fingerprint,
                    retirement_id,
                    current.backend_domain_raw,
                    current.backend_domain_fingerprint,
                    current.membership_domain_raw,
                    current.membership_domain_fingerprint,
                    current.active_backend_domain_raw,
                    current.active_backend_domain_fingerprint,
                    key_backend_domain_raw,
                ],
            )
        )
        payload = self._decode_result(raw)
        if payload["status"] != "verified":
            return None
        fingerprint = str(payload.get("fingerprint", ""))
        if _SHA256_HEX_RE.fullmatch(fingerprint) is None:
            raise GPUArbiterStoreError("GPU tombstone receipt is invalid")
        return GPUTombstoneGCReceipt(
            ledger_revision=int(payload["ledger_revision"]),
            ledger_incarnation=str(payload["ledger_incarnation"]),
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            retirement_id=retirement_id,
            fingerprint=fingerprint,
        )

    async def admit(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        budget_mb: int,
        generation: str,
        eviction_priority: int,
        evictable: bool,
        max_concurrency: int,
        lease_id: str,
        owner_id: str,
        operation: str,
        heartbeat_ttl_ms: int,
        hard_ttl_ms: int,
        reconcile_deadline_grace_ms: int = 0,
        backend_ticket_id: str | None = None,
        card_ticket_id: str | None = None,
        require_resident: bool = False,
        require_cold_owner: bool = False,
    ) -> GPUAdmissionResult:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        _validate_nonempty(lease_id, "lease_id", max_length=256)
        _validate_nonempty(owner_id, "owner_id", max_length=256)
        _validate_nonempty(operation, "operation", max_length=256)
        for ticket_id, field in (
            (backend_ticket_id, "backend_ticket_id"),
            (card_ticket_id, "card_ticket_id"),
        ):
            if ticket_id is not None:
                _validate_nonempty(ticket_id, field, max_length=256)
        budget_mb = _validate_positive_int(budget_mb, "budget_mb")
        membership_epoch = _validate_membership_epoch(membership_epoch)
        generation = _validate_generation(generation)
        if isinstance(eviction_priority, bool) or not isinstance(
            eviction_priority, int
        ):
            raise ValueError("eviction_priority must be an integer")
        max_concurrency = normalize_gpu_backend_max_concurrency(max_concurrency)
        heartbeat_ttl_ms = _validate_ttl_ms(heartbeat_ttl_ms, "heartbeat_ttl_ms")
        hard_ttl_ms = _validate_ttl_ms(hard_ttl_ms, "hard_ttl_ms")
        reconcile_deadline_grace_ms = _validate_nonnegative_redis_safe_int(
            reconcile_deadline_grace_ms,
            "reconcile_deadline_grace_ms",
        )
        if reconcile_deadline_grace_ms > _MAX_RECONCILE_DEADLINE_GRACE_MS:
            raise ValueError(
                "reconcile_deadline_grace_ms must be at most "
                f"{_MAX_RECONCILE_DEADLINE_GRACE_MS}"
            )
        if hard_ttl_ms < heartbeat_ttl_ms:
            raise ValueError("hard_ttl_ms must be >= heartbeat_ttl_ms")

        if not isinstance(evictable, bool):
            raise ValueError("evictable must be a boolean")
        if not isinstance(require_resident, bool):
            raise ValueError("require_resident must be a boolean")
        if not isinstance(require_cold_owner, bool):
            raise ValueError("require_cold_owner must be a boolean")
        if require_resident and require_cold_owner:
            raise ValueError(
                "require_resident and require_cold_owner are mutually exclusive"
            )
        eviction_priority = _validate_redis_safe_int(
            eviction_priority, "eviction_priority"
        )

        domains = await self._ledger_domain(keys)

        raw = await self._call(
            lambda: self._admit_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    backend_id,
                    budget_mb,
                    generation,
                    eviction_priority,
                    "1" if evictable else "0",
                    max_concurrency,
                    lease_id,
                    owner_id,
                    operation,
                    heartbeat_ttl_ms,
                    hard_ttl_ms,
                    backend_ticket_id or "",
                    card_ticket_id or "",
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    str(membership_epoch),
                    "1" if require_resident else "0",
                    "1" if require_cold_owner else "0",
                    reconcile_deadline_grace_ms,
                ],
            )
        )
        payload = self._decode_result(raw)
        state = payload.get("allocation_state")
        return GPUAdmissionResult(
            status=payload["status"],
            reason=str(payload.get("reason", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            lease_count=int(payload.get("lease_count", 0)),
            allocation_state=GPUAllocationState(state) if state else None,
            heartbeat_deadline_ms=self._optional_int(
                payload.get("heartbeat_deadline_ms")
            ),
            hard_deadline_ms=self._optional_int(payload.get("hard_deadline_ms")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def heartbeat_lease(
        self,
        resource_id: str,
        *,
        backend_id: str,
        lease_id: str,
        owner_id: str,
        generation: str,
        heartbeat_ttl_ms: int,
    ) -> GPULeaseMutationResult:
        heartbeat_ttl_ms = _validate_ttl_ms(heartbeat_ttl_ms, "heartbeat_ttl_ms")
        return await self._mutate_lease(
            "heartbeat",
            resource_id,
            backend_id=backend_id,
            lease_id=lease_id,
            owner_id=owner_id,
            generation=generation,
            heartbeat_ttl_ms=heartbeat_ttl_ms,
        )

    async def mark_lease_uncertain(
        self,
        resource_id: str,
        *,
        backend_id: str,
        lease_id: str,
        owner_id: str,
        generation: str,
    ) -> GPULeaseMutationResult:
        return await self._mutate_lease(
            "uncertain",
            resource_id,
            backend_id=backend_id,
            lease_id=lease_id,
            owner_id=owner_id,
            generation=generation,
            heartbeat_ttl_ms=1,
        )

    async def release_lease(
        self,
        resource_id: str,
        *,
        backend_id: str,
        lease_id: str,
        owner_id: str,
        generation: str,
    ) -> GPULeaseMutationResult:
        return await self._mutate_lease(
            "release",
            resource_id,
            backend_id=backend_id,
            lease_id=lease_id,
            owner_id=owner_id,
            generation=generation,
            heartbeat_ttl_ms=1,
        )

    async def _mutate_lease(
        self,
        operation: Literal["heartbeat", "uncertain", "release"],
        resource_id: str,
        *,
        backend_id: str,
        lease_id: str,
        owner_id: str,
        generation: str,
        heartbeat_ttl_ms: int,
    ) -> GPULeaseMutationResult:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        _validate_nonempty(lease_id, "lease_id", max_length=256)
        _validate_nonempty(owner_id, "owner_id", max_length=256)
        generation = _validate_generation(generation)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._lease_script(
                keys=[keys.leases(backend_id), keys.card, keys.allocations],
                args=[
                    operation,
                    lease_id,
                    owner_id,
                    generation,
                    heartbeat_ttl_ms,
                    resource_id,
                    backend_id,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                ],
            )
        )
        payload = self._decode_result(raw)
        lease_state = payload.get("lease_state")
        return GPULeaseMutationResult(
            status=payload["status"],
            lease_state=GPURequestLeaseState(lease_state) if lease_state else None,
            heartbeat_deadline_ms=self._optional_int(
                payload.get("heartbeat_deadline_ms")
            ),
            hard_deadline_ms=self._optional_int(payload.get("hard_deadline_ms")),
        )

    async def sweep_expired_leases(
        self, resource_id: str, *, backend_id: str
    ) -> tuple[int, int]:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._sweep_leases_script(
                keys=[keys.leases(backend_id), keys.card],
                args=[
                    resource_id,
                    backend_id,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                ],
            )
        )
        payload = self._decode_result(raw)
        if payload.get("reason") == "proof_reset_in_progress":
            raise GPUArbiterStoreError("gpu_arbiter_proof_reset_in_progress")
        return int(payload.get("changed", 0)), int(payload.get("total", 0))

    async def enqueue_backend(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        ticket_id: str,
        owner_id: str,
        ttl_ms: int,
        max_queue_length: int = 10_000,
    ) -> GPUQueueResult:
        return await self._queue_operation(
            resource_id,
            "enqueue",
            ticket_id=ticket_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            kind="backend",
            ttl_ms=ttl_ms,
            max_queue_length=max_queue_length,
        )

    async def enqueue_card(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        ticket_id: str,
        owner_id: str,
        ttl_ms: int,
        max_queue_length: int = 10_000,
    ) -> GPUQueueResult:
        return await self._queue_operation(
            resource_id,
            "enqueue",
            ticket_id=ticket_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            kind="card",
            ttl_ms=ttl_ms,
            max_queue_length=max_queue_length,
        )

    async def queue_position(
        self,
        resource_id: str,
        *,
        backend_id: str,
        ticket_id: str,
        card_queue: bool,
    ) -> GPUQueueResult:
        return await self._queue_operation(
            resource_id,
            "position",
            ticket_id=ticket_id,
            backend_id=backend_id,
            membership_epoch=None,
            owner_id="position",
            kind="card" if card_queue else "backend",
            ttl_ms=1,
            max_queue_length=1,
        )

    async def cancel_queue_ticket(
        self,
        resource_id: str,
        *,
        backend_id: str,
        ticket_id: str,
        owner_id: str,
        card_queue: bool,
    ) -> GPUQueueResult:
        return await self._queue_operation(
            resource_id,
            "cancel",
            ticket_id=ticket_id,
            backend_id=backend_id,
            membership_epoch=None,
            owner_id=owner_id,
            kind="card" if card_queue else "backend",
            ttl_ms=1,
            max_queue_length=1,
        )

    async def _queue_operation(
        self,
        resource_id: str,
        operation: Literal["enqueue", "position", "cancel"],
        *,
        ticket_id: str,
        backend_id: str,
        membership_epoch: int | None,
        owner_id: str,
        kind: Literal["backend", "card"],
        ttl_ms: int,
        max_queue_length: int,
    ) -> GPUQueueResult:
        _validate_nonempty(ticket_id, "ticket_id", max_length=256)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        _validate_nonempty(owner_id, "owner_id", max_length=256)
        if operation == "enqueue":
            if membership_epoch is None:
                raise ValueError("enqueue requires membership_epoch")
            membership_epoch = _validate_membership_epoch(membership_epoch)
        ttl_ms = _validate_ttl_ms(ttl_ms, "ttl_ms")
        max_queue_length = _validate_positive_int(max_queue_length, "max_queue_length")
        if max_queue_length > _MAX_GPU_QUEUE_LENGTH:
            raise ValueError(
                f"max_queue_length must be at most {_MAX_GPU_QUEUE_LENGTH}"
            )
        keys = self.keys(resource_id)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._queue_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    operation,
                    ticket_id,
                    backend_id,
                    owner_id,
                    kind,
                    ttl_ms,
                    max_queue_length,
                    resource_id,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    "" if membership_epoch is None else str(membership_epoch),
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUQueueResult(
            status=payload["status"],
            ticket_id=payload.get("ticket_id", ticket_id),
            position=self._optional_int(payload.get("position")),
            expires_at_ms=self._optional_int(payload.get("expires_at_ms")),
        )

    async def acquire_transition_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        owner_id: str,
        generation: str,
        operation: str,
        ttl_ms: int,
        require_idle: bool = False,
    ) -> GPUTransitionOwnerResult:
        if not isinstance(require_idle, bool):
            raise ValueError("require_idle must be a boolean")
        return await self._transition_owner_operation(
            "acquire",
            resource_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            generation=generation,
            operation_name=operation,
            ttl_ms=ttl_ms,
            require_idle=require_idle,
        )

    async def begin_idle_eviction(
        self,
        resource_id: str,
        *,
        requester_backend_id: str,
        requester_membership_epoch: int,
        requester_budget_mb: int,
        requester_eviction_priority: int,
        victim_backend_id: str,
        victim_membership_epoch: int,
        victim_expected_generation: str,
        victim_next_generation: str,
        owner_id: str,
        ttl_ms: int,
        hard_ttl_ms: int,
        allow_busy: bool = False,
        requester_card_ticket_id: str | None = None,
        requester_queue_owner_id: str | None = None,
    ) -> GPUIdleEvictionResult:
        if not isinstance(allow_busy, bool):
            raise ValueError("allow_busy must be a boolean")
        for value, field in (
            (requester_backend_id, "requester_backend_id"),
            (victim_backend_id, "victim_backend_id"),
            (owner_id, "owner_id"),
        ):
            _validate_nonempty(value, field, max_length=256)
        if (requester_card_ticket_id is None) != (requester_queue_owner_id is None):
            raise ValueError(
                "requester_card_ticket_id and requester_queue_owner_id must be paired"
            )
        for value, field in (
            (requester_card_ticket_id, "requester_card_ticket_id"),
            (requester_queue_owner_id, "requester_queue_owner_id"),
        ):
            if value is not None:
                _validate_nonempty(value, field, max_length=256)
        requester_membership_epoch = _validate_membership_epoch(
            requester_membership_epoch
        )
        victim_membership_epoch = _validate_membership_epoch(victim_membership_epoch)
        requester_budget_mb = _validate_positive_int(
            requester_budget_mb, "requester_budget_mb"
        )
        requester_eviction_priority = _validate_redis_safe_int(
            requester_eviction_priority, "requester_eviction_priority"
        )
        victim_expected_generation = _validate_generation(victim_expected_generation)
        victim_next_generation = _validate_generation(victim_next_generation)
        if int(victim_next_generation) <= int(victim_expected_generation):
            raise ValueError("victim_next_generation must increase generation")
        ttl_ms = _validate_ttl_ms(ttl_ms, "ttl_ms")
        hard_ttl_ms = _validate_ttl_ms(hard_ttl_ms, "hard_ttl_ms")
        if hard_ttl_ms < ttl_ms:
            raise ValueError("hard_ttl_ms must be >= ttl_ms")

        keys = self.keys(resource_id)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._begin_idle_eviction_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    requester_backend_id,
                    requester_membership_epoch,
                    requester_budget_mb,
                    requester_eviction_priority,
                    victim_backend_id,
                    victim_membership_epoch,
                    victim_expected_generation,
                    victim_next_generation,
                    owner_id,
                    GPU_EVICTION_OPERATION,
                    ttl_ms,
                    hard_ttl_ms,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    requester_card_ticket_id or "",
                    requester_queue_owner_id or "",
                    "1" if allow_busy else "0",
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUIdleEvictionResult(
            status=payload["status"],
            reason=str(payload.get("reason", "")),
            committed_mb=int(payload.get("committed_mb", 0)),
            shortfall_mb=int(payload.get("shortfall_mb", 0)),
            victim_backend_id=payload.get("victim_backend_id"),
            victim_generation=payload.get("victim_generation"),
            victim_budget_mb=self._optional_int(payload.get("victim_budget_mb")),
            owner_id=payload.get("owner_id"),
            owner_expires_at_ms=self._optional_int(payload.get("owner_expires_at_ms")),
            owner_hard_deadline_ms=self._optional_int(
                payload.get("owner_hard_deadline_ms")
            ),
            retry_at_ms=self._optional_int(payload.get("retry_at_ms")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def arm_eviction_cancel(
        self,
        resource_id: str,
        *,
        backend_id: str,
        expected_generation: str,
        transition_owner_id: str,
    ) -> GPUEvictionBranchResult:
        """Atomically freeze the cancel branch for one busy draining owner."""

        _validate_nonempty(backend_id, "backend_id", max_length=128)
        expected_generation = _validate_generation(expected_generation)
        _validate_nonempty(
            transition_owner_id,
            "transition_owner_id",
            max_length=256,
        )
        keys = self.keys(resource_id)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._arm_eviction_cancel_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    backend_id,
                    expected_generation,
                    transition_owner_id,
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                ],
            )
        )
        payload = self._decode_result(raw)
        branch = payload.get("branch")
        if branch not in {None, "cancel", "unload"}:
            raise GPUArbiterStoreError("GPU eviction branch is invalid")
        state = payload.get("state")
        return GPUEvictionBranchResult(
            status=payload["status"],
            branch=branch,
            state=GPUAllocationState(state) if state else None,
            generation=payload.get("generation"),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def acquire_cold_admission_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        owner_id: str,
        generation: str,
        ttl_ms: int,
    ) -> GPUTransitionOwnerResult:
        return await self.acquire_transition_owner(
            resource_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            generation=generation,
            operation=GPU_COLD_ADMISSION_OPERATION,
            ttl_ms=ttl_ms,
            require_idle=True,
        )

    async def heartbeat_transition_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        owner_id: str,
        generation: str,
        operation: str,
        ttl_ms: int,
    ) -> GPUTransitionOwnerResult:
        return await self._transition_owner_operation(
            "heartbeat",
            resource_id,
            backend_id=backend_id,
            membership_epoch=None,
            owner_id=owner_id,
            generation=generation,
            operation_name=operation,
            ttl_ms=ttl_ms,
            require_idle=False,
        )

    async def revalidate_cold_admission_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        owner_id: str,
        generation: str,
        ttl_ms: int,
    ) -> GPUTransitionOwnerResult:
        return await self._transition_owner_operation(
            "revalidate",
            resource_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            generation=generation,
            operation_name=GPU_COLD_ADMISSION_OPERATION,
            ttl_ms=ttl_ms,
            require_idle=True,
        )

    async def revalidate_transition_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int,
        owner_id: str,
        generation: str,
        operation: str,
        ttl_ms: int,
        require_idle: bool = False,
    ) -> GPUTransitionOwnerResult:
        if not isinstance(require_idle, bool):
            raise ValueError("require_idle must be a boolean")
        return await self._transition_owner_operation(
            "revalidate",
            resource_id,
            backend_id=backend_id,
            membership_epoch=membership_epoch,
            owner_id=owner_id,
            generation=generation,
            operation_name=operation,
            ttl_ms=ttl_ms,
            require_idle=require_idle,
        )

    async def release_transition_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        owner_id: str,
        generation: str,
        operation: str,
    ) -> GPUTransitionOwnerResult:
        return await self._transition_owner_operation(
            "release",
            resource_id,
            backend_id=backend_id,
            membership_epoch=None,
            owner_id=owner_id,
            generation=generation,
            operation_name=operation,
            ttl_ms=1,
            require_idle=False,
        )

    async def release_cold_admission_owner(
        self,
        resource_id: str,
        *,
        backend_id: str,
        owner_id: str,
        generation: str,
    ) -> GPUTransitionOwnerResult:
        return await self.release_transition_owner(
            resource_id,
            backend_id=backend_id,
            owner_id=owner_id,
            generation=generation,
            operation=GPU_COLD_ADMISSION_OPERATION,
        )

    async def _transition_owner_operation(
        self,
        action: Literal["acquire", "revalidate", "heartbeat", "release"],
        resource_id: str,
        *,
        backend_id: str,
        membership_epoch: int | None,
        owner_id: str,
        generation: str,
        operation_name: str,
        ttl_ms: int,
        require_idle: bool,
    ) -> GPUTransitionOwnerResult:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        _validate_nonempty(owner_id, "owner_id", max_length=256)
        _validate_nonempty(operation_name, "operation", max_length=256)
        generation = _validate_generation(generation)
        if action in {"acquire", "revalidate"}:
            if membership_epoch is None:
                raise ValueError(
                    "transition acquire/revalidate requires membership_epoch"
                )
            membership_epoch = _validate_membership_epoch(membership_epoch)
        ttl_ms = _validate_ttl_ms(ttl_ms, "ttl_ms")
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._transition_owner_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    action,
                    resource_id,
                    backend_id,
                    owner_id,
                    generation,
                    operation_name,
                    ttl_ms,
                    "1" if require_idle else "0",
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    "" if membership_epoch is None else str(membership_epoch),
                ],
            )
        )
        payload = self._decode_result(raw)
        return GPUTransitionOwnerResult(
            status=payload["status"],
            owner_id=payload.get("owner_id"),
            generation=payload.get("generation"),
            expires_at_ms=self._optional_int(payload.get("expires_at_ms")),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def transition_allocation(
        self,
        resource_id: str,
        *,
        backend_id: str,
        expected_generation: str,
        target_state: GPUAllocationState,
        next_generation: str | None = None,
        request_lease_id: str | None = None,
        request_owner_id: str | None = None,
        transition_owner_id: str | None = None,
        transition_operation: str | None = None,
    ) -> GPUTransitionResult:
        if target_state is GPUAllocationState.RESIDENT:
            raise ValueError("Resident cold terminal requires finalize_cold_allocation")
        return await self._transition_allocation_operation(
            resource_id,
            backend_id=backend_id,
            expected_generation=expected_generation,
            target_state=target_state,
            next_generation=next_generation,
            request_lease_id=request_lease_id,
            request_owner_id=request_owner_id,
            transition_owner_id=transition_owner_id,
            transition_operation=transition_operation,
            cold_finalize=False,
            target_evictable=False,
            resident_cooldown_ms=0,
            eviction_transition=False,
            expected_source_state=None,
        )

    async def finalize_cold_allocation(
        self,
        resource_id: str,
        *,
        backend_id: str,
        expected_generation: str,
        request_lease_id: str,
        request_owner_id: str,
        target_state: GPUAllocationState,
        target_evictable: bool,
        resident_cooldown_ms: int,
    ) -> GPUTransitionResult:
        if target_state not in {
            GPUAllocationState.RESIDENT,
            GPUAllocationState.UNKNOWN,
            GPUAllocationState.UNLOADED,
            GPUAllocationState.CPU_FALLBACK,
        }:
            raise ValueError("cold terminal target state is invalid")
        if not isinstance(target_evictable, bool):
            raise ValueError("target_evictable must be a boolean")
        if target_evictable is not (target_state is GPUAllocationState.RESIDENT):
            raise ValueError("only a Resident cold terminal may be evictable")
        if target_state is GPUAllocationState.RESIDENT:
            _validate_ttl_ms(resident_cooldown_ms, "resident_cooldown_ms")
        elif resident_cooldown_ms != 0:
            raise ValueError("non-Resident cold terminal cooldown must be zero")
        return await self._transition_allocation_operation(
            resource_id,
            backend_id=backend_id,
            expected_generation=expected_generation,
            target_state=target_state,
            next_generation=None,
            request_lease_id=request_lease_id,
            request_owner_id=request_owner_id,
            transition_owner_id=None,
            transition_operation=None,
            cold_finalize=True,
            target_evictable=target_evictable,
            resident_cooldown_ms=resident_cooldown_ms,
            eviction_transition=False,
            expected_source_state=None,
        )

    async def transition_eviction_allocation(
        self,
        resource_id: str,
        *,
        backend_id: str,
        expected_state: GPUAllocationState,
        expected_generation: str,
        target_state: GPUAllocationState,
        transition_owner_id: str,
        next_generation: str | None = None,
    ) -> GPUTransitionResult:
        allowed = {
            GPUAllocationState.RESIDENT: {GPUAllocationState.DRAINING},
            GPUAllocationState.DRAINING: {
                GPUAllocationState.RESIDENT,
                GPUAllocationState.UNLOADING,
                GPUAllocationState.UNKNOWN,
            },
            GPUAllocationState.UNLOADING: {
                GPUAllocationState.UNLOADED,
                GPUAllocationState.UNKNOWN,
            },
        }
        if target_state not in allowed.get(expected_state, set()):
            raise ValueError("eviction allocation transition is invalid")
        changes_generation = (
            expected_state is GPUAllocationState.RESIDENT
            and target_state is GPUAllocationState.DRAINING
        ) or (
            expected_state is GPUAllocationState.DRAINING
            and target_state is GPUAllocationState.RESIDENT
        )
        if changes_generation and next_generation is None:
            raise ValueError("eviction generation transition requires next_generation")
        if not changes_generation and next_generation is not None:
            raise ValueError("eviction target must not change generation")
        return await self._transition_allocation_operation(
            resource_id,
            backend_id=backend_id,
            expected_generation=expected_generation,
            target_state=target_state,
            next_generation=next_generation,
            request_lease_id=None,
            request_owner_id=None,
            transition_owner_id=transition_owner_id,
            transition_operation=GPU_EVICTION_OPERATION,
            cold_finalize=False,
            target_evictable=False,
            resident_cooldown_ms=0,
            eviction_transition=True,
            expected_source_state=expected_state,
        )

    async def _transition_allocation_operation(
        self,
        resource_id: str,
        *,
        backend_id: str,
        expected_generation: str,
        target_state: GPUAllocationState,
        next_generation: str | None,
        request_lease_id: str | None,
        request_owner_id: str | None,
        transition_owner_id: str | None,
        transition_operation: str | None,
        cold_finalize: bool,
        target_evictable: bool,
        resident_cooldown_ms: int,
        eviction_transition: bool,
        expected_source_state: GPUAllocationState | None,
    ) -> GPUTransitionResult:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        expected_generation = _validate_generation(expected_generation)
        if next_generation is not None:
            next_generation = _validate_generation(next_generation)
        for value, field in (
            (request_lease_id, "request_lease_id"),
            (request_owner_id, "request_owner_id"),
            (transition_owner_id, "transition_owner_id"),
            (transition_operation, "transition_operation"),
        ):
            if value is not None:
                _validate_nonempty(value, field, max_length=256)
        domains = await self._ledger_domain(keys)
        raw = await self._call(
            lambda: self._transition_script(
                keys=self._ledger_keys(keys, domains.backend_ids),
                args=[
                    resource_id,
                    backend_id,
                    expected_generation,
                    target_state.value,
                    next_generation or "",
                    request_lease_id or "",
                    request_owner_id or "",
                    transition_owner_id or "",
                    transition_operation or "",
                    domains.backend_domain_raw,
                    domains.backend_domain_fingerprint,
                    domains.ledger_incarnation,
                    domains.membership_domain_raw,
                    domains.membership_domain_fingerprint,
                    domains.active_backend_domain_raw,
                    domains.active_backend_domain_fingerprint,
                    "1" if cold_finalize else "0",
                    "1" if target_evictable else "0",
                    "1" if eviction_transition else "0",
                    expected_source_state.value if expected_source_state else "",
                    str(resident_cooldown_ms),
                ],
            )
        )
        payload = self._decode_result(raw)
        state = payload.get("state")
        return GPUTransitionResult(
            status=payload["status"],
            state=GPUAllocationState(state) if state else None,
            generation=payload.get("generation"),
            committed_mb=int(payload.get("committed_mb", 0)),
            idempotent=bool(payload.get("idempotent", False)),
        )

    async def snapshot(self, resource_id: str) -> GPUCardSnapshot:
        keys = self.keys(resource_id)
        for _ in range(_SNAPSHOT_MAX_ATTEMPTS):
            card_before = await self._call(lambda: self._redis.hgetall(keys.card))
            if not card_before or card_before.get("resource_id") != resource_id:
                raise GPUArbiterStoreError("gpu_arbiter_not_ready")
            if card_before.get("proof_reset_state") == "prepared":
                raise GPUArbiterStoreError("gpu_arbiter_proof_reset_in_progress")
            try:
                revision_before = int(card_before["ledger_revision"])
                if revision_before <= 0 or revision_before > _MAX_REDIS_SAFE_INTEGER:
                    raise ValueError("ledger revision is invalid")
                incarnation_before = _validate_nonempty(
                    card_before["ledger_incarnation"],
                    "ledger_incarnation",
                    max_length=128,
                )
                if card_before.get("ledger_version") != "3":
                    raise ValueError("ledger schema is not v3")
                domain_raw = card_before["backend_domain"]
                membership_raw = card_before["membership_domain"]
                active_domain_raw = card_before["active_backend_domain"]
                membership_values = json.loads(membership_raw)
                if not isinstance(membership_values, list) or any(
                    not isinstance(item, dict)
                    or set(item) != {"backend_id", "membership_epoch", "state"}
                    or not isinstance(item["membership_epoch"], str)
                    or _CANONICAL_POSITIVE_INT64_RE.fullmatch(item["membership_epoch"])
                    is None
                    or int(item["membership_epoch"]) > _MAX_POSITIVE_INT64
                    for item in membership_values
                ):
                    raise ValueError("membership domain is invalid")
                domains = _canonical_backend_domains(
                    tuple(
                        GPUBackendDomainMember(
                            backend_id=item["backend_id"],
                            membership_epoch=int(item["membership_epoch"]),
                            state=item["state"],
                        )
                        for item in membership_values
                    ),
                    ledger_incarnation=incarnation_before,
                )
                if (
                    domains.backend_domain_raw != domain_raw
                    or domains.membership_domain_raw != membership_raw
                    or domains.active_backend_domain_raw != active_domain_raw
                    or domains.backend_domain_fingerprint
                    != card_before["backend_domain_fingerprint"]
                    or domains.membership_domain_fingerprint
                    != card_before["membership_domain_fingerprint"]
                    or domains.active_backend_domain_fingerprint
                    != card_before["active_backend_domain_fingerprint"]
                ):
                    raise ValueError("backend membership domains are invalid")
                backend_ids = domains.backend_ids
                backend_memberships = {
                    item.backend_id: item for item in domains.backend_memberships
                }
                allocation_count = int(
                    await self._call(lambda: self._redis.hlen(keys.allocations))
                )
                if allocation_count > _MAX_GPU_BACKENDS_PER_RESOURCE:
                    raise ValueError("allocation domain exceeds safety limit")
                allocation_items = await self._call(
                    lambda: self._redis.hgetall(keys.allocations)
                )
                allocations = tuple(
                    sorted(
                        (
                            self._allocation_from_json(
                                raw, expected_backend_id=backend_id
                            )
                            for backend_id, raw in allocation_items.items()
                        ),
                        key=lambda item: item.backend_id,
                    )
                )
                leases: list[GPURequestLease] = []
                lease_counts: dict[str, int] = {}
                card_queue_count = int(
                    await self._call(lambda: self._redis.llen(keys.queue))
                )
                if not 0 <= card_queue_count <= _MAX_GPU_QUEUE_LENGTH:
                    raise ValueError("card queue domain exceeds safety limit")
                backend_queue_counts: dict[str, int] = {}
                total_queue_count = card_queue_count
                for backend_id in backend_ids:
                    queue_count = int(
                        await self._call(
                            lambda backend_id=backend_id: self._redis.llen(
                                keys.backend_queue(backend_id)
                            )
                        )
                    )
                    if not 0 <= queue_count <= _MAX_GPU_QUEUE_LENGTH:
                        raise ValueError("backend queue domain exceeds safety limit")
                    total_queue_count += queue_count
                    if total_queue_count > _MAX_GPU_QUEUE_LENGTH:
                        raise ValueError("queue domain exceeds safety limit")
                    backend_queue_counts[backend_id] = queue_count

                backend_queue_tickets: list[GPUQueueTicket] = []
                seen_ticket_ids: set[str] = set()
                queue_window_changed = False
                for backend_id in backend_ids:
                    lease_count = int(
                        await self._call(
                            lambda backend_id=backend_id: self._redis.hlen(
                                keys.leases(backend_id)
                            )
                        )
                    )
                    if lease_count > _MAX_GPU_BACKEND_CONCURRENCY:
                        raise ValueError("lease domain exceeds safety limit")
                    lease_items = await self._call(
                        lambda backend_id=backend_id: self._redis.hgetall(
                            keys.leases(backend_id)
                        )
                    )
                    lease_counts[backend_id] = len(lease_items)
                    for lease_id, raw in lease_items.items():
                        leases.append(
                            self._lease_from_json(
                                raw,
                                expected_backend_id=backend_id,
                                expected_lease_id=lease_id,
                            )
                        )
                    queue_count = backend_queue_counts[backend_id]
                    backend_queue_raw = (
                        await self._call(
                            lambda backend_id=backend_id, queue_count=queue_count: (
                                self._redis.lrange(
                                    keys.backend_queue(backend_id), 0, queue_count - 1
                                )
                            )
                        )
                        if queue_count
                        else []
                    )
                    if len(backend_queue_raw) != queue_count:
                        queue_window_changed = True
                        break
                    for raw in backend_queue_raw:
                        ticket = self._queue_ticket_from_json(
                            raw,
                            expected_kind="backend",
                            expected_backend_id=backend_id,
                            backend_memberships=backend_memberships,
                        )
                        if ticket.ticket_id in seen_ticket_ids:
                            raise ValueError("queue ticket identity is duplicated")
                        seen_ticket_ids.add(ticket.ticket_id)
                        backend_queue_tickets.append(ticket)
                if queue_window_changed:
                    continue
                card_queue_raw = (
                    await self._call(
                        lambda: self._redis.lrange(keys.queue, 0, card_queue_count - 1)
                    )
                    if card_queue_count
                    else []
                )
                if len(card_queue_raw) != card_queue_count:
                    continue
                card_queue_tickets: list[GPUQueueTicket] = []
                for raw in card_queue_raw:
                    ticket = self._queue_ticket_from_json(
                        raw,
                        expected_kind="card",
                        expected_backend_id=None,
                        backend_memberships=backend_memberships,
                    )
                    if ticket.ticket_id in seen_ticket_ids:
                        raise ValueError("queue ticket identity is duplicated")
                    seen_ticket_ids.add(ticket.ticket_id)
                    card_queue_tickets.append(ticket)
                transition_raw = await self._call(
                    lambda: self._redis.get(keys.transition)
                )
                transition_pttl_ms = int(
                    await self._call(lambda: self._redis.pttl(keys.transition))
                )
                card_after = await self._call(lambda: self._redis.hgetall(keys.card))
                if not card_after or card_after.get("resource_id") != resource_id:
                    raise GPUArbiterStoreError("gpu_arbiter_not_ready")
                if card_after.get("proof_reset_state") == "prepared":
                    raise GPUArbiterStoreError("gpu_arbiter_proof_reset_in_progress")
                incarnation_after = card_after.get("ledger_incarnation")
                if (
                    card_after.get("ledger_revision") != str(revision_before)
                    or incarnation_before != incarnation_after
                ):
                    continue
                revision_after = int(card_after["ledger_revision"])
                card_queue_count_after = int(
                    await self._call(lambda: self._redis.llen(keys.queue))
                )
                backend_queue_counts_after = {
                    backend_id: int(
                        await self._call(
                            lambda backend_id=backend_id: self._redis.llen(
                                keys.backend_queue(backend_id)
                            )
                        )
                    )
                    for backend_id in backend_ids
                }
                if (
                    card_queue_count_after != card_queue_count
                    or backend_queue_counts_after != backend_queue_counts
                ):
                    continue
                leases_by_owner = {
                    (lease.backend_id, lease.lease_id): lease for lease in leases
                }
                for allocation in allocations:
                    if (
                        allocation.generation is None
                        and lease_counts[allocation.backend_id] > 0
                    ):
                        raise ValueError("null-generation allocation has leases")
                    if allocation.state not in {
                        GPUAllocationState.RESERVING,
                        GPUAllocationState.LOADING,
                    }:
                        continue
                    reservation = leases_by_owner.get(
                        (allocation.backend_id, allocation.reservation_lease_id)
                    )
                    if (
                        reservation is None
                        or reservation.owner_id != allocation.reservation_owner_id
                        or reservation.generation != allocation.generation
                    ):
                        raise ValueError("allocation reservation lease mismatch")
                if (
                    card_after.get("backend_domain") != domain_raw
                    or card_after.get("backend_domain_fingerprint")
                    != card_before["backend_domain_fingerprint"]
                    or card_after.get("membership_domain") != membership_raw
                    or card_after.get("membership_domain_fingerprint")
                    != card_before["membership_domain_fingerprint"]
                    or card_after.get("active_backend_domain") != active_domain_raw
                    or card_after.get("active_backend_domain_fingerprint")
                    != card_before["active_backend_domain_fingerprint"]
                    or int(card_after["allocation_count"]) != len(allocations)
                    or json.loads(card_after["lease_counts"]) != lease_counts
                    or int(card_after["card_queue_count"]) != card_queue_count
                    or json.loads(card_after["backend_queue_counts"])
                    != backend_queue_counts
                ):
                    raise GPUArbiterStoreError(
                        "GPU ledger integrity cache drift detected"
                    )
                allocation_backends = {
                    allocation.backend_id for allocation in allocations
                }
                if any(lease.backend_id not in allocation_backends for lease in leases):
                    raise GPUArbiterStoreError("GPU orphan lease detected")

                committed = sum(
                    allocation.budget_mb
                    for allocation in allocations
                    if allocation.counted
                )
                cached_committed = int(card_after["committed_mb"])
                if committed != cached_committed:
                    raise GPUArbiterStoreError("GPU committed cache drift detected")
                allocatable_mb = _validate_positive_int(
                    int(card_after["allocatable_mb"]), "allocatable_mb"
                )
                reconcile_deadline_ms = int(card_after["reconcile_deadline_ms"])
                if (
                    reconcile_deadline_ms < 0
                    or reconcile_deadline_ms > _MAX_REDIS_SAFE_INTEGER
                ):
                    raise ValueError("reconcile deadline is invalid")
                redis_time = await self._call(self._redis.time)
                redis_now_ms = (int(redis_time[0]) * 1000) + (
                    int(redis_time[1]) // 1000
                )
                transition_mirror = card_after["transition_mirror"]
                transition_document: dict[str, Any] | None = None
                if transition_raw is not None:
                    if transition_raw != transition_mirror:
                        raise GPUArbiterStoreError(
                            "GPU transition mirror drift detected"
                        )
                    transition_document = self._transition_from_json(
                        transition_raw,
                        expected_resource_id=resource_id,
                        backend_memberships=backend_memberships,
                    )
                    if "eviction_branch" in transition_document:
                        if transition_pttl_ms != -1:
                            raise GPUArbiterStoreError(
                                "GPU frozen transition expiry is invalid"
                            )
                    elif transition_pttl_ms <= 0:
                        raise GPUArbiterStoreError("GPU transition expiry is invalid")
                elif transition_mirror:
                    mirrored_transition = self._transition_from_json(
                        transition_mirror,
                        expected_resource_id=resource_id,
                        backend_memberships=backend_memberships,
                    )
                    mirrored_expires_at_ms = mirrored_transition["expires_at_ms"]
                    if (
                        "eviction_branch" in mirrored_transition
                        or mirrored_expires_at_ms > redis_now_ms
                    ):
                        raise GPUArbiterStoreError(
                            "GPU transition key missing before expiry"
                        )
                elif transition_pttl_ms != -2:
                    raise GPUArbiterStoreError(
                        "GPU transition key changed during snapshot"
                    )
                return GPUCardSnapshot(
                    resource_id=resource_id,
                    observed_at_ms=redis_now_ms,
                    allocatable_mb=allocatable_mb,
                    ready=(
                        card_after.get("bootstrap_state") == "ready"
                        and bool(domains.active_backend_ids)
                        and revision_after < _LEDGER_REVISION_REBASE_THRESHOLD
                        and reconcile_deadline_ms > redis_now_ms
                        and reconcile_deadline_ms <= redis_now_ms + 300_000
                    ),
                    reconcile_deadline_ms=reconcile_deadline_ms,
                    ledger_revision=revision_after,
                    ledger_incarnation=incarnation_before,
                    committed_mb=committed,
                    backend_ids=domains.backend_ids,
                    active_backend_ids=domains.active_backend_ids,
                    backend_memberships=domains.backend_memberships,
                    allocations=allocations,
                    leases=tuple(sorted(leases, key=lambda item: item.lease_id)),
                    not_ready_reason=(card_after.get("not_ready_reason") or None),
                    card_queue_count=card_queue_count,
                    backend_queue_count=sum(backend_queue_counts.values()),
                    transition_present=transition_raw is not None,
                    card_queue=tuple(card_queue_tickets),
                    backend_queues=tuple(backend_queue_tickets),
                    transition=transition_document,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GPUArbiterStoreError("GPU ledger decode failed") from exc
        raise GPUArbiterStoreError("GPU ledger changed during snapshot")

    async def key_ttls(self, resource_id: str, *, backend_id: str) -> tuple[int, int]:
        keys = self.keys(resource_id)
        _validate_nonempty(backend_id, "backend_id", max_length=128)
        return (
            int(await self._call(lambda: self._redis.ttl(keys.allocations))),
            int(await self._call(lambda: self._redis.ttl(keys.leases(backend_id)))),
        )

    @staticmethod
    def _allocation_to_payload(allocation: GPUAllocation) -> dict[str, Any]:
        backend_id = _validate_nonempty(
            allocation.backend_id, "backend_id", max_length=128
        )
        if not isinstance(allocation.state, GPUAllocationState):
            raise ValueError("allocation state is invalid")
        budget_mb = _validate_positive_int(allocation.budget_mb, "budget_mb")
        generation = _validate_allocation_generation(
            allocation.generation, allocation.state
        )
        eviction_priority = _validate_redis_safe_int(
            allocation.eviction_priority, "eviction_priority"
        )
        if not isinstance(allocation.evictable, bool):
            raise ValueError("allocation evictable must be a boolean")
        if generation is None and allocation.evictable:
            raise ValueError("null-generation unknown allocation cannot be evictable")
        max_concurrency = normalize_gpu_backend_max_concurrency(
            allocation.max_concurrency
        )
        last_used_at_ms = _validate_positive_int(
            allocation.last_used_at_ms, "last_used_at_ms"
        )
        not_evict_before_ms = _validate_nonnegative_redis_safe_int(
            allocation.not_evict_before_ms,
            "not_evict_before_ms",
        )
        if allocation.state is GPUAllocationState.RESIDENT and not_evict_before_ms == 0:
            raise ValueError("Resident allocation cooldown deadline must be positive")
        if (allocation.reservation_lease_id is None) != (
            allocation.reservation_owner_id is None
        ):
            raise ValueError("allocation reservation owner is incomplete")
        reservation_state = allocation.state in {
            GPUAllocationState.RESERVING,
            GPUAllocationState.LOADING,
        }
        if reservation_state != (allocation.reservation_lease_id is not None):
            raise ValueError("allocation reservation owner does not match state")
        payload: dict[str, Any] = {
            "backend_id": backend_id,
            "state": allocation.state.value,
            "budget_mb": budget_mb,
            "generation": generation,
            "eviction_priority": eviction_priority,
            "evictable": allocation.evictable,
            "max_concurrency": max_concurrency,
            "last_used_at_ms": last_used_at_ms,
            "not_evict_before_ms": not_evict_before_ms,
        }
        if allocation.reservation_lease_id is not None:
            payload["reservation_lease_id"] = _validate_nonempty(
                allocation.reservation_lease_id,
                "reservation_lease_id",
                max_length=256,
            )
            payload["reservation_owner_id"] = _validate_nonempty(
                allocation.reservation_owner_id,
                "reservation_owner_id",
                max_length=256,
            )
        return payload

    @staticmethod
    def _decode_result(raw: Any) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GPUArbiterStoreError("invalid Redis script response") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise GPUArbiterStoreError("invalid Redis script payload")
        return payload

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return int(value) if value is not None else None

    @staticmethod
    def _allocation_from_json(raw: str, *, expected_backend_id: str) -> GPUAllocation:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("backend_id") != expected_backend_id
        ):
            raise ValueError("allocation identity mismatch")
        budget_mb = _validate_positive_int(value.get("budget_mb"), "budget_mb")
        priority = value.get("eviction_priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("allocation priority is invalid")
        priority = _validate_redis_safe_int(priority, "eviction_priority")
        evictable = value.get("evictable")
        if not isinstance(evictable, bool):
            raise ValueError("allocation evictable is invalid")
        last_used_at_ms = _validate_positive_int(
            value.get("last_used_at_ms"), "last_used_at_ms"
        )
        not_evict_before_ms = _validate_nonnegative_redis_safe_int(
            value.get("not_evict_before_ms"),
            "not_evict_before_ms",
        )
        if "max_concurrency" not in value:
            raise ValueError("allocation max_concurrency is missing")
        max_concurrency = normalize_gpu_backend_max_concurrency(
            value["max_concurrency"]
        )
        reservation_lease_id = value.get("reservation_lease_id")
        reservation_owner_id = value.get("reservation_owner_id")
        if (reservation_lease_id is None) != (reservation_owner_id is None):
            raise ValueError("allocation reservation owner is incomplete")
        state = GPUAllocationState(value["state"])
        if state is GPUAllocationState.RESIDENT and not_evict_before_ms == 0:
            raise ValueError("Resident allocation cooldown deadline must be positive")
        if value["generation"] is None and evictable:
            raise ValueError("null-generation unknown allocation cannot be evictable")
        has_reservation = reservation_lease_id is not None
        if has_reservation != (
            state in {GPUAllocationState.RESERVING, GPUAllocationState.LOADING}
        ):
            raise ValueError("allocation reservation owner does not match state")
        if reservation_lease_id is not None:
            _validate_nonempty(
                reservation_lease_id, "reservation_lease_id", max_length=256
            )
            _validate_nonempty(
                reservation_owner_id, "reservation_owner_id", max_length=256
            )
        return GPUAllocation(
            backend_id=value["backend_id"],
            state=state,
            budget_mb=budget_mb,
            generation=_validate_allocation_generation(value["generation"], state),
            eviction_priority=priority,
            evictable=evictable,
            max_concurrency=max_concurrency,
            reservation_lease_id=reservation_lease_id,
            reservation_owner_id=reservation_owner_id,
            last_used_at_ms=last_used_at_ms,
            not_evict_before_ms=not_evict_before_ms,
        )

    @staticmethod
    def _lease_from_json(
        raw: str,
        *,
        expected_backend_id: str,
        expected_lease_id: str,
    ) -> GPURequestLease:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("backend_id") != expected_backend_id
            or value.get("lease_id") != expected_lease_id
        ):
            raise ValueError("lease identity mismatch")
        owner_id = _validate_nonempty(value.get("owner_id"), "owner_id", max_length=256)
        operation = _validate_nonempty(
            value.get("operation"), "operation", max_length=256
        )
        created_at_ms = _validate_positive_int(
            value.get("created_at_ms"), "created_at_ms"
        )
        heartbeat_deadline_ms = _validate_positive_int(
            value.get("heartbeat_deadline_ms"), "heartbeat_deadline_ms"
        )
        hard_deadline_ms = _validate_positive_int(
            value.get("hard_deadline_ms"), "hard_deadline_ms"
        )
        if not created_at_ms <= heartbeat_deadline_ms <= hard_deadline_ms:
            raise ValueError("lease deadlines are invalid")
        return GPURequestLease(
            lease_id=value["lease_id"],
            backend_id=value["backend_id"],
            owner_id=owner_id,
            generation=_validate_generation(value["generation"]),
            operation=operation,
            state=GPURequestLeaseState(value["state"]),
            created_at_ms=created_at_ms,
            heartbeat_deadline_ms=heartbeat_deadline_ms,
            hard_deadline_ms=hard_deadline_ms,
        )

    @staticmethod
    def _queue_ticket_from_json(
        raw: str,
        *,
        expected_kind: Literal["backend", "card"],
        expected_backend_id: str | None,
        backend_memberships: Mapping[str, GPUBackendDomainMember],
    ) -> GPUQueueTicket:
        value = json.loads(raw)
        expected_fields = {
            "ticket_id",
            "backend_id",
            "owner_id",
            "kind",
            "membership_epoch",
            "enqueued_at_ms",
            "expires_at_ms",
        }
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ValueError("queue ticket document is invalid")
        backend_id = _validate_nonempty(
            value["backend_id"], "backend_id", max_length=128
        )
        membership = backend_memberships.get(backend_id)
        if membership is None or (
            expected_backend_id is not None and backend_id != expected_backend_id
        ):
            raise ValueError("queue ticket backend identity mismatch")
        if value["kind"] != expected_kind:
            raise ValueError("queue ticket kind mismatch")
        raw_membership_epoch = value["membership_epoch"]
        if (
            not isinstance(raw_membership_epoch, str)
            or _CANONICAL_POSITIVE_INT64_RE.fullmatch(raw_membership_epoch) is None
        ):
            raise ValueError("queue ticket membership epoch is invalid")
        membership_epoch = _validate_membership_epoch(int(raw_membership_epoch))
        membership_epoch_exact = membership_epoch == membership.membership_epoch
        retiring_predecessor = (
            membership.state == "retiring"
            and membership_epoch < membership.membership_epoch
        )
        if not (membership_epoch_exact or retiring_predecessor):
            raise ValueError("queue ticket membership identity mismatch")
        enqueued_at_ms = _validate_positive_int(
            value["enqueued_at_ms"], "enqueued_at_ms"
        )
        expires_at_ms = _validate_positive_int(value["expires_at_ms"], "expires_at_ms")
        if enqueued_at_ms > expires_at_ms:
            raise ValueError("queue ticket deadlines are invalid")
        return GPUQueueTicket(
            ticket_id=_validate_nonempty(
                value["ticket_id"], "ticket_id", max_length=256
            ),
            backend_id=backend_id,
            owner_id=_validate_nonempty(value["owner_id"], "owner_id", max_length=256),
            kind=expected_kind,
            membership_epoch=membership_epoch,
            enqueued_at_ms=enqueued_at_ms,
            expires_at_ms=expires_at_ms,
        )

    @staticmethod
    def _transition_from_json(
        raw: str,
        *,
        expected_resource_id: str,
        backend_memberships: Mapping[str, GPUBackendDomainMember],
    ) -> dict[str, Any]:
        value = json.loads(raw)
        required_fields = {
            "resource_id",
            "backend_id",
            "owner_id",
            "generation",
            "operation",
            "require_idle",
            "created_at_ms",
            "expires_at_ms",
        }
        optional_fields = {
            "hard_deadline_ms",
            "eviction_branch",
            "requester_backend_id",
            "requester_membership_epoch",
            "requester_budget_mb",
            "requester_eviction_priority",
            "requester_card_ticket_id",
            "requester_queue_owner_id",
            "victim_membership_epoch",
            "victim_source_generation",
        }
        if (
            not isinstance(value, dict)
            or not required_fields.issubset(value)
            or not set(value).issubset(required_fields | optional_fields)
            or value["resource_id"] != expected_resource_id
        ):
            raise GPUArbiterStoreError("GPU transition document is invalid")
        backend_id = _validate_nonempty(
            value["backend_id"], "backend_id", max_length=128
        )
        victim_membership = backend_memberships.get(backend_id)
        if victim_membership is None:
            raise ValueError("transition backend identity mismatch")
        _validate_nonempty(value["owner_id"], "owner_id", max_length=256)
        _validate_generation(value["generation"])
        _validate_nonempty(value["operation"], "operation", max_length=256)
        if not isinstance(value["require_idle"], bool):
            raise ValueError("transition require_idle is invalid")
        try:
            created_at_ms = _validate_positive_int(
                value["created_at_ms"], "created_at_ms"
            )
            expires_at_ms = _validate_positive_int(
                value["expires_at_ms"], "expires_at_ms"
            )
        except ValueError as exc:
            raise GPUArbiterStoreError("GPU transition deadline is invalid") from exc
        if created_at_ms > expires_at_ms:
            raise GPUArbiterStoreError("GPU transition deadline is invalid")
        hard_deadline_ms = value.get("hard_deadline_ms")
        if hard_deadline_ms is not None:
            try:
                hard_deadline_ms = _validate_positive_int(
                    hard_deadline_ms, "hard_deadline_ms"
                )
            except ValueError as exc:
                raise GPUArbiterStoreError(
                    "GPU transition deadline is invalid"
                ) from exc
            if expires_at_ms > hard_deadline_ms:
                raise GPUArbiterStoreError("GPU transition deadline is invalid")
        branch = value.get("eviction_branch")
        if "eviction_branch" in value and branch not in {"cancel", "unload"}:
            raise GPUArbiterStoreError("GPU eviction branch is invalid")
        eviction_fields = {
            "requester_backend_id",
            "requester_membership_epoch",
            "requester_budget_mb",
            "requester_eviction_priority",
            "requester_card_ticket_id",
            "requester_queue_owner_id",
            "victim_membership_epoch",
            "victim_source_generation",
        }
        present_eviction_fields = eviction_fields.intersection(value)
        if present_eviction_fields and present_eviction_fields != eviction_fields:
            raise GPUArbiterStoreError("GPU eviction transition is incomplete")
        if (present_eviction_fields or "eviction_branch" in value) and value[
            "operation"
        ] != GPU_EVICTION_OPERATION:
            raise GPUArbiterStoreError("GPU eviction transition operation is invalid")
        requester_backend_id = value.get("requester_backend_id")
        if requester_backend_id is not None:
            _validate_nonempty(
                requester_backend_id, "requester_backend_id", max_length=128
            )
            requester_membership = backend_memberships.get(requester_backend_id)
            if requester_membership is None or requester_backend_id == backend_id:
                raise ValueError("transition requester identity mismatch")
        for field in (
            "requester_membership_epoch",
            "victim_membership_epoch",
            "victim_source_generation",
        ):
            raw_generation = value.get(field)
            if raw_generation is not None:
                _validate_generation(raw_generation)
        if present_eviction_fields:
            if (
                int(value["victim_membership_epoch"])
                != victim_membership.membership_epoch
            ):
                raise ValueError("transition victim membership identity mismatch")
            assert requester_backend_id is not None
            requester_membership = backend_memberships[requester_backend_id]
            if (
                int(value["requester_membership_epoch"])
                != requester_membership.membership_epoch
            ):
                raise ValueError("transition requester membership identity mismatch")
        requester_budget_mb = value.get("requester_budget_mb")
        if requester_budget_mb is not None:
            _validate_positive_int(requester_budget_mb, "requester_budget_mb")
        requester_eviction_priority = value.get("requester_eviction_priority")
        if requester_eviction_priority is not None:
            _validate_redis_safe_int(
                requester_eviction_priority, "requester_eviction_priority"
            )
        requester_card_ticket_id = value.get("requester_card_ticket_id")
        requester_queue_owner_id = value.get("requester_queue_owner_id")
        if (requester_card_ticket_id is None) != (requester_queue_owner_id is None):
            raise ValueError("transition requester queue identity is incomplete")
        if requester_card_ticket_id is not None:
            if not isinstance(requester_card_ticket_id, str) or not isinstance(
                requester_queue_owner_id, str
            ):
                raise ValueError("transition requester queue identity is invalid")
            if bool(requester_card_ticket_id) != bool(requester_queue_owner_id):
                raise ValueError("transition requester queue identity is incomplete")
            if requester_card_ticket_id:
                _validate_nonempty(
                    requester_card_ticket_id,
                    "requester_card_ticket_id",
                    max_length=256,
                )
                _validate_nonempty(
                    requester_queue_owner_id,
                    "requester_queue_owner_id",
                    max_length=256,
                )
        return dict(value)
