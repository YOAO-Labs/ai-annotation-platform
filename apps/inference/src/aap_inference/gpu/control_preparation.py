"""Typed GPU control preparations returned by the closed Native capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
import uuid

from aap_inference.gpu.native import GPUCallerData
from aap_inference.gpu.policy import _HEALTH_EVIDENCE_MAX_AGE
from aap_inference.gpu.resource import validate_gpu_resource_id
from aap_inference_contract.gpu import GPUAuthorityUnavailable

GPU_LEGACY_MODE_TOKEN_TTL_SECONDS = 30
_PROOF_RESET_MAX_WINDOW = timedelta(minutes=5)
_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807

GPUReadinessDemoter = Callable[[str], Awaitable[None]]
GPURolloutControlOperation = Literal["reset", "mode_enforce", "mode_legacy"]


class GPULegacyAckBlockedError(RuntimeError):
    """Cached evidence cannot authorize a legacy-mode membership handshake."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GPURolloutControlBlockedError(RuntimeError):
    """Fresh durable/runtime evidence cannot advance one rollout control step."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GPULegacyAckPreparation:
    action: Literal["issue", "acknowledged"]
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    boot_id: str
    runtime_epoch: str
    control_epoch: str
    token_expires_at: datetime | None
    proof_ready: bool


@dataclass(frozen=True)
class GPURolloutControlPreparation:
    action: Literal["issue", "acknowledged", "awaiting_health"]
    operation: GPURolloutControlOperation
    target_gate: Literal["legacy", "enforce"]
    backend_id: uuid.UUID
    resource_id: str
    membership_epoch: int
    transition_id: uuid.UUID
    boot_id: str
    control_epoch: str
    token_expires_at: datetime
    reason: str


def _timestamp(value: object, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if type(value) is not str:
        raise GPUAuthorityUnavailable()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GPUAuthorityUnavailable() from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise GPUAuthorityUnavailable()
    return result.astimezone(UTC)


def _preparation(result: object) -> dict[str, object]:
    """Decode one closed preparation reply, validating the blocked shape first."""

    if type(result) is not dict:
        raise GPUAuthorityUnavailable()
    if set(result) == {"status", "reason"}:
        if result.get("status") != "blocked":
            raise GPUAuthorityUnavailable()
        reason = result.get("reason")
        if type(reason) is not str or not reason:
            raise GPUAuthorityUnavailable()
        raise ValueError(reason)
    if set(result) != {"status", "preparation"} or result.get("status") != "ok":
        raise GPUAuthorityUnavailable()
    value = result.get("preparation")
    if type(value) is not dict:
        raise GPUAuthorityUnavailable()
    return value


def _legacy_ack_preparation(value: object) -> GPULegacyAckPreparation:
    if type(value) is not dict or set(value) != {
        "action",
        "backend_id",
        "resource_id",
        "membership_epoch",
        "boot_id",
        "runtime_epoch",
        "control_epoch",
        "token_expires_at",
        "proof_ready",
    }:
        raise GPUAuthorityUnavailable()
    try:
        action = value["action"]
        backend_id = uuid.UUID(value["backend_id"])
        membership_epoch = value["membership_epoch"]
        if (
            action not in {"issue", "acknowledged"}
            or str(backend_id) != value["backend_id"]
            or type(membership_epoch) is not int
            or not 1 <= membership_epoch <= _MAX_POSITIVE_INT64
            or type(value["proof_ready"]) is not bool
        ):
            raise ValueError
        for key in ("resource_id", "boot_id", "runtime_epoch", "control_epoch"):
            if type(value[key]) is not str or not value[key]:
                raise ValueError
        return GPULegacyAckPreparation(
            action=action,
            backend_id=backend_id,
            resource_id=value["resource_id"],
            membership_epoch=membership_epoch,
            boot_id=value["boot_id"],
            runtime_epoch=value["runtime_epoch"],
            control_epoch=value["control_epoch"],
            token_expires_at=_timestamp(value["token_expires_at"], optional=True),
            proof_ready=value["proof_ready"],
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


def _rollout_preparation(value: object) -> GPURolloutControlPreparation:
    if type(value) is not dict or set(value) != {
        "action",
        "operation",
        "target_gate",
        "backend_id",
        "resource_id",
        "membership_epoch",
        "transition_id",
        "boot_id",
        "control_epoch",
        "token_expires_at",
        "reason",
    }:
        raise GPUAuthorityUnavailable()
    try:
        action = value["action"]
        operation = value["operation"]
        target_gate = value["target_gate"]
        backend_id = uuid.UUID(value["backend_id"])
        transition_id = uuid.UUID(value["transition_id"])
        membership_epoch = value["membership_epoch"]
        if (
            action not in {"issue", "acknowledged", "awaiting_health"}
            or operation not in {"reset", "mode_enforce", "mode_legacy"}
            or target_gate not in {"legacy", "enforce"}
            or str(backend_id) != value["backend_id"]
            or str(transition_id) != value["transition_id"]
            or type(membership_epoch) is not int
            or not 1 <= membership_epoch <= _MAX_POSITIVE_INT64
        ):
            raise ValueError
        for key in ("resource_id", "boot_id", "control_epoch", "reason"):
            if type(value[key]) is not str or not value[key]:
                raise ValueError
        return GPURolloutControlPreparation(
            action=action,
            operation=operation,
            target_gate=target_gate,
            backend_id=backend_id,
            resource_id=value["resource_id"],
            membership_epoch=membership_epoch,
            transition_id=transition_id,
            boot_id=value["boot_id"],
            control_epoch=value["control_epoch"],
            token_expires_at=_timestamp(value["token_expires_at"]),
            reason=value["reason"],
        )
    except GPUAuthorityUnavailable:
        raise
    except (TypeError, ValueError, KeyError):
        raise GPUAuthorityUnavailable() from None


@asynccontextmanager
async def _preparation_transaction(data: GPUCallerData) -> AsyncIterator[GPUCallerData]:
    """Hold one unit of work across the preparing step and the readiness demotion.

    A provider port owns and commits its own short transaction, so the durable
    authority change stays uncommitted - with its resource advisory lock and row
    locks retained - until the demoter confirms the fail-closed barrier; a blocked
    reply or a failed demotion rolls the Native writes back. A port already borrowed
    from a caller keeps the caller's transaction and never opens a nested one.
    """

    opener = getattr(data, "transaction", None)
    if not callable(opener):
        yield data
        return
    async with opener() as borrowed:
        yield borrowed


async def prepare_gpu_backend_legacy_ack(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    token_ttl_seconds: int = GPU_LEGACY_MODE_TOKEN_TTL_SECONDS,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> GPULegacyAckPreparation:
    validate_gpu_resource_id(gpu_resource_id)
    if (
        not isinstance(membership_epoch, int)
        or isinstance(membership_epoch, bool)
        or not 1 <= membership_epoch <= _MAX_POSITIVE_INT64
    ):
        raise ValueError("membership_epoch must be a positive int64")
    if (
        not isinstance(token_ttl_seconds, int)
        or isinstance(token_ttl_seconds, bool)
        or not 1 <= token_ttl_seconds <= 300
    ):
        raise ValueError("token_ttl_seconds must be between 1 and 300")
    if evidence_ttl <= timedelta(0) or evidence_ttl > _PROOF_RESET_MAX_WINDOW:
        raise ValueError(
            "evidence_ttl must be positive and no greater than five minutes"
        )
    async with _preparation_transaction(data) as borrowed:
        result = await borrowed.step(
            "prepare_legacy_ack",
            {
                "backend_registry_id": str(backend_registry_id),
                "gpu_resource_id": gpu_resource_id,
                "membership_epoch": membership_epoch,
                "token_ttl_seconds": token_ttl_seconds,
                "evidence_ttl_seconds": evidence_ttl.total_seconds(),
            },
        )
        try:
            preparation = _legacy_ack_preparation(_preparation(result))
        except ValueError as exc:
            if readiness_demoter is not None:
                await readiness_demoter(gpu_resource_id)
            raise GPULegacyAckBlockedError(str(exc)) from None
        if (
            preparation.backend_id != backend_registry_id
            or preparation.resource_id != gpu_resource_id
        ):
            raise GPUAuthorityUnavailable()
        if not preparation.proof_ready:
            # Activating a member or advancing its control epoch/token horizon must
            # never become durable while cached readiness still advertises this card.
            if readiness_demoter is None:
                raise GPULegacyAckBlockedError("readiness_demotion_unavailable")
            await readiness_demoter(gpu_resource_id)
        return preparation


async def prepare_gpu_backend_rollout_control(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    transition_id: uuid.UUID,
    target_gate: Literal["legacy", "enforce"],
    token_ttl_seconds: int = GPU_LEGACY_MODE_TOKEN_TTL_SECONDS,
    evidence_ttl: timedelta = _HEALTH_EVIDENCE_MAX_AGE,
    readiness_demoter: GPUReadinessDemoter | None = None,
) -> GPURolloutControlPreparation:
    validate_gpu_resource_id(gpu_resource_id)
    if not isinstance(transition_id, uuid.UUID):
        raise ValueError("transition_id must be a UUID")
    if target_gate not in {"legacy", "enforce"}:
        raise ValueError("target_gate must be legacy or enforce")
    if (
        not isinstance(membership_epoch, int)
        or isinstance(membership_epoch, bool)
        or not 1 <= membership_epoch <= _MAX_POSITIVE_INT64
    ):
        raise ValueError("membership_epoch must be a positive int64")
    if (
        not isinstance(token_ttl_seconds, int)
        or isinstance(token_ttl_seconds, bool)
        or not 1 <= token_ttl_seconds <= 300
    ):
        raise ValueError("token_ttl_seconds must be between 1 and 300")
    if evidence_ttl <= timedelta(0) or evidence_ttl > _PROOF_RESET_MAX_WINDOW:
        raise ValueError(
            "evidence_ttl must be positive and no greater than five minutes"
        )
    async with _preparation_transaction(data) as borrowed:
        result = await borrowed.step(
            "prepare_rollout_control",
            {
                "backend_registry_id": str(backend_registry_id),
                "gpu_resource_id": gpu_resource_id,
                "membership_epoch": membership_epoch,
                "transition_id": str(transition_id),
                "target_gate": target_gate,
                "token_ttl_seconds": token_ttl_seconds,
                "evidence_ttl_seconds": evidence_ttl.total_seconds(),
            },
        )
        try:
            preparation = _rollout_preparation(_preparation(result))
        except ValueError as exc:
            if readiness_demoter is not None:
                await readiness_demoter(gpu_resource_id)
            raise GPURolloutControlBlockedError(str(exc)) from None
        if (
            preparation.backend_id != backend_registry_id
            or preparation.resource_id != gpu_resource_id
            or preparation.transition_id != transition_id
            or preparation.target_gate != target_gate
        ):
            raise GPUAuthorityUnavailable()
        if (
            preparation.action == "issue"
            and preparation.reason != "control_intent_replay"
        ):
            # A fresh reset/mode intent advances the control epoch and token horizon;
            # cached readiness must be revoked before that change becomes durable.
            if readiness_demoter is None:
                raise GPURolloutControlBlockedError("readiness_demotion_unavailable")
            await readiness_demoter(gpu_resource_id)
        return preparation


__all__ = [
    "GPULegacyAckBlockedError",
    "GPULegacyAckPreparation",
    "GPURolloutControlBlockedError",
    "GPURolloutControlOperation",
    "GPURolloutControlPreparation",
    "GPUReadinessDemoter",
    "GPU_LEGACY_MODE_TOKEN_TTL_SECONDS",
    "prepare_gpu_backend_legacy_ack",
    "prepare_gpu_backend_rollout_control",
]
