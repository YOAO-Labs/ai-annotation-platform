"""Input validation, canonicalization and domain checks for the GPU arbitration ledger.

This is the lowest domain layer above :mod:`gpu_arbitration.ledger.types` (it does not
depend on keys, store or scripts).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from typing import Any

from app.services.gpu_arbitration.ledger.types import (
    _CANONICAL_POSITIVE_INT64_RE,
    _GPU_BACKEND_MEMBERSHIP_STATES,
    _GPUBackendDomains,
    _MAX_GPU_BACKENDS_PER_RESOURCE,
    _MAX_GPU_BACKEND_CONCURRENCY,
    _MAX_POSITIVE_INT64,
    _MAX_REDIS_SAFE_INTEGER,
    _MAX_TTL_MS,
    GPUAllocationState,
    GPUBackendDomainMember,
)


def _validate_nonempty(value: str, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field} must be a non-empty string up to {max_length} chars")
    return value


def _validate_positive_int(value: int, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_REDIS_SAFE_INTEGER
    ):
        raise ValueError(f"{field} must be a positive Redis-safe integer")
    return value


def _validate_redis_safe_int(value: int, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or abs(value) > _MAX_REDIS_SAFE_INTEGER
    ):
        raise ValueError(f"{field} must be a Redis-safe integer")
    return value


def _validate_nonnegative_redis_safe_int(value: int, field: str) -> int:
    value = _validate_redis_safe_int(value, field)
    if value < 0:
        raise ValueError(f"{field} must be a nonnegative Redis-safe integer")
    return value


def _validate_ttl_ms(value: int, field: str) -> int:
    value = _validate_positive_int(value, field)
    if value > _MAX_TTL_MS:
        raise ValueError(f"{field} must be at most {_MAX_TTL_MS} ms")
    return value


def _validate_generation(value: str) -> str:
    if (
        not isinstance(value, str)
        or _CANONICAL_POSITIVE_INT64_RE.fullmatch(value) is None
        or int(value) > _MAX_POSITIVE_INT64
    ):
        raise ValueError("generation must be a canonical positive int64 string")
    return value


def _validate_allocation_generation(
    value: str | None,
    state: GPUAllocationState,
) -> str | None:
    if value is None:
        if state is not GPUAllocationState.UNKNOWN:
            raise ValueError("null generation is only valid for unknown allocations")
        return None
    return _validate_generation(value)


def _validate_membership_epoch(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_POSITIVE_INT64
    ):
        raise ValueError("membership_epoch must be a positive int64")
    return value


def _validate_retirement_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("retirement_id must be a canonical UUID string")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("retirement_id must be a canonical UUID string") from exc
    if value != normalized:
        raise ValueError("retirement_id must be a canonical UUID string")
    return normalized


def _canonical_backend_domains(
    memberships: Sequence[GPUBackendDomainMember],
    *,
    ledger_incarnation: str = "",
) -> _GPUBackendDomains:
    normalized: list[GPUBackendDomainMember] = []
    seen_backend_ids: set[str] = set()
    for member in memberships:
        if not isinstance(member, GPUBackendDomainMember):
            raise ValueError(
                "backend_memberships must contain GPUBackendDomainMember values"
            )
        backend_id = _validate_nonempty(member.backend_id, "backend_id", max_length=128)
        if backend_id in seen_backend_ids:
            raise ValueError("backend_memberships must not contain duplicate backends")
        if (
            not isinstance(member.state, str)
            or member.state not in _GPU_BACKEND_MEMBERSHIP_STATES
        ):
            raise ValueError("backend membership state is invalid")
        seen_backend_ids.add(backend_id)
        normalized.append(
            GPUBackendDomainMember(
                backend_id=backend_id,
                membership_epoch=_validate_membership_epoch(member.membership_epoch),
                state=member.state,
            )
        )
    normalized.sort(key=lambda item: item.backend_id)
    if len(normalized) > _MAX_GPU_BACKENDS_PER_RESOURCE:
        raise ValueError("backend_memberships exceeds the per-resource safety limit")
    backend_ids = tuple(item.backend_id for item in normalized)
    active_backend_ids = tuple(
        item.backend_id for item in normalized if item.state == "active"
    )
    backend_domain_raw = json.dumps(backend_ids, separators=(",", ":"))
    membership_domain_raw = json.dumps(
        [
            {
                "backend_id": item.backend_id,
                "membership_epoch": str(item.membership_epoch),
                "state": item.state,
            }
            for item in normalized
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    active_backend_domain_raw = json.dumps(active_backend_ids, separators=(",", ":"))

    def fingerprint(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    return _GPUBackendDomains(
        backend_domain_raw=backend_domain_raw,
        backend_domain_fingerprint=fingerprint(backend_domain_raw),
        membership_domain_raw=membership_domain_raw,
        membership_domain_fingerprint=fingerprint(membership_domain_raw),
        active_backend_domain_raw=active_backend_domain_raw,
        active_backend_domain_fingerprint=fingerprint(active_backend_domain_raw),
        backend_ids=backend_ids,
        active_backend_ids=active_backend_ids,
        backend_memberships=tuple(normalized),
        ledger_incarnation=ledger_incarnation,
    )


def _decode_tombstone_receipt_target_domains(
    raw_receipt: str | bytes | None,
) -> _GPUBackendDomains | None:
    if isinstance(raw_receipt, bytes):
        try:
            raw_receipt = raw_receipt.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw_receipt, str) or len(raw_receipt) > 65_536:
        return None
    try:
        receipt = json.loads(raw_receipt)
    except (TypeError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("schema") != (
        "gpu-arbiter-tombstone-gc-receipt/v1"
    ):
        return None
    membership_raw = receipt.get("target_membership_domain")
    if not isinstance(membership_raw, str):
        return None
    try:
        membership_document = json.loads(membership_raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(membership_document, list):
        return None
    members: list[GPUBackendDomainMember] = []
    for item in membership_document:
        if not isinstance(item, dict) or set(item) != {
            "backend_id",
            "membership_epoch",
            "state",
        }:
            return None
        epoch_raw = item["membership_epoch"]
        if (
            not isinstance(epoch_raw, str)
            or not epoch_raw.isascii()
            or not epoch_raw.isdecimal()
            or epoch_raw.startswith("0")
        ):
            return None
        try:
            members.append(
                GPUBackendDomainMember(
                    backend_id=item["backend_id"],
                    membership_epoch=int(epoch_raw),
                    state=item["state"],
                )
            )
        except (TypeError, ValueError):
            return None
    try:
        target = _canonical_backend_domains(tuple(members))
    except ValueError:
        return None
    if (
        receipt.get("target_backend_domain") != target.backend_domain_raw
        or receipt.get("target_backend_domain_fingerprint")
        != target.backend_domain_fingerprint
        or receipt.get("target_membership_domain") != target.membership_domain_raw
        or receipt.get("target_membership_domain_fingerprint")
        != target.membership_domain_fingerprint
        or receipt.get("target_active_backend_domain")
        != target.active_backend_domain_raw
        or receipt.get("target_active_backend_domain_fingerprint")
        != target.active_backend_domain_fingerprint
    ):
        return None
    return target


def normalize_gpu_backend_max_concurrency(value: Any, *, default: int = 4) -> int:
    """Normalize the global Redis limit without accepting bool or invalid strings."""

    candidate = default if value is None else value
    if (
        isinstance(candidate, bool)
        or not isinstance(candidate, int)
        or candidate <= 0
        or candidate > _MAX_GPU_BACKEND_CONCURRENCY
    ):
        raise ValueError(
            "max_concurrency must be a positive integer no greater than "
            f"{_MAX_GPU_BACKEND_CONCURRENCY}"
        )
    return candidate
