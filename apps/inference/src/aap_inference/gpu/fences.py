"""Durable GPU fencing over the fixed caller-transaction Native capability."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
import uuid

from aap_inference.gpu.native import GPUCallerData
from aap_inference.gpu.policy import _MAX_POSITIVE_INT64

GPUReadinessDemoter = Callable[[str], Awaitable[None]]
GPUFenceCounter = Literal["generation", "control_epoch"]


class GPUFenceExhaustedError(RuntimeError):
    """A durable positive-int64 fencing sequence cannot advance safely."""


class GPUFenceMembershipError(RuntimeError):
    """Fence issuance did not match one active durable resource membership."""


@dataclass(frozen=True)
class GPUBackendFenceSnapshot:
    backend_registry_id: uuid.UUID
    generation_high_water: int
    control_epoch_high_water: int
    runtime_epoch_high_water: int
    token_expiry_high_water: datetime | None
    updated_at: datetime


def _aware_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("Native GPU fence timestamp is malformed")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Native GPU fence timestamp is not timezone-aware")
    return parsed


def _parse_nonnegative_int(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_POSITIVE_INT64:
        raise ValueError("Native GPU fence high-water is malformed")
    return value


def _operation_status(result: dict[str, object]) -> str:
    status = result.get("status")
    if type(status) is not str:
        raise GPUFenceMembershipError("GPU Native fence operation returned no status")
    if status == "membership_changed":
        raise GPUFenceMembershipError(
            "GPU membership changed before durable fence update"
        )
    if status == "fence_missing":
        raise GPUFenceMembershipError("durable GPU fence is missing")
    if status == "high_water_exhausted":
        raise GPUFenceExhaustedError(
            "durable GPU fence high-water reached positive int64 maximum"
        )
    return status


def _validate_token_expiry(token_expires_at: datetime) -> None:
    if token_expires_at.tzinfo is None:
        raise ValueError("token_expires_at must be timezone-aware")


async def advance_gpu_backend_fence(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
    counter: GPUFenceCounter,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    token_expires_at: datetime | None = None,
) -> str:
    """Advance and commit one high-water before token issuance or Redis writes."""

    if counter not in {"generation", "control_epoch"}:
        raise ValueError(f"unsupported fence counter: {counter}")
    arguments: dict[str, object] = {
        "backend_registry_id": str(backend_registry_id),
        "counter": counter,
        "gpu_resource_id": gpu_resource_id,
        "membership_epoch": membership_epoch,
        "token_expires_at": None,
    }
    if token_expires_at is not None:
        _validate_token_expiry(token_expires_at)
        arguments["token_expires_at"] = token_expires_at.isoformat()
    result = await data.step("fence_advance", arguments)
    if _operation_status(result) != "advanced":
        raise GPUFenceMembershipError("GPU Native fence update was not applied")
    value = _parse_nonnegative_int(result.get("value"))
    if value < 1:
        raise GPUFenceExhaustedError("GPU fence generation is not positive")
    return str(value)


async def record_gpu_backend_token_expiry(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    token_expires_at: datetime,
) -> datetime:
    """Persist the exact token horizon without advancing any fence counter."""

    _validate_token_expiry(token_expires_at)
    result = await data.step(
        "fence_token_expiry",
        {
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
            "membership_epoch": membership_epoch,
            "token_expires_at": token_expires_at.isoformat(),
        },
    )
    if _operation_status(result) != "recorded":
        raise GPUFenceMembershipError("GPU token horizon was not recorded")
    return _aware_datetime(result.get("token_expires_at"))


async def activate_gpu_backend_membership(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
) -> str:
    """Activate the exact pending membership and advance the runtime epoch."""

    result = await data.step(
        "fence_activate_membership",
        {
            "backend_registry_id": str(backend_registry_id),
            "gpu_resource_id": gpu_resource_id,
            "membership_epoch": membership_epoch,
        },
    )
    if _operation_status(result) != "activated":
        raise GPUFenceMembershipError("GPU membership activation was not applied")
    value = _parse_nonnegative_int(result.get("runtime_epoch_high_water"))
    if value < 1:
        raise GPUFenceExhaustedError("GPU runtime epoch is not positive")
    return str(value)


async def read_gpu_backend_fence(
    data: GPUCallerData,
    backend_registry_id: uuid.UUID,
) -> GPUBackendFenceSnapshot | None:
    result = await data.step(
        "fence_read", {"backend_registry_id": str(backend_registry_id)}
    )
    if set(result) != {"fence"}:
        raise ValueError("GPU Native fence response is malformed")
    value = result["fence"]
    if value is None:
        return None
    expected = {
        "backend_registry_id",
        "generation_high_water",
        "control_epoch_high_water",
        "runtime_epoch_high_water",
        "token_expiry_high_water",
        "updated_at",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("GPU Native fence snapshot is malformed")
    identity = uuid.UUID(value["backend_registry_id"])
    if identity != backend_registry_id:
        raise ValueError("GPU Native returned another backend fence")
    token_expiry = value["token_expiry_high_water"]
    return GPUBackendFenceSnapshot(
        backend_registry_id=identity,
        generation_high_water=_parse_nonnegative_int(value["generation_high_water"]),
        control_epoch_high_water=_parse_nonnegative_int(
            value["control_epoch_high_water"]
        ),
        runtime_epoch_high_water=_parse_nonnegative_int(
            value["runtime_epoch_high_water"]
        ),
        token_expiry_high_water=(
            None if token_expiry is None else _aware_datetime(token_expiry)
        ),
        updated_at=_aware_datetime(value["updated_at"]),
    )


__all__ = [
    "GPUBackendFenceSnapshot",
    "GPUFenceCounter",
    "GPUFenceExhaustedError",
    "GPUFenceMembershipError",
    "GPUReadinessDemoter",
    "activate_gpu_backend_membership",
    "advance_gpu_backend_fence",
    "read_gpu_backend_fence",
    "record_gpu_backend_token_expiry",
]
