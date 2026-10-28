"""Framework-free GPU rollout policy over the fixed Native state capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import uuid

from aap_inference.gpu.native import GPUCallerData
from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from aap_inference_contract.gpu import GPUAuthorityUnavailable

GPURolloutState = Literal[
    "off",
    "promoting",
    "enforcing",
    "demoting",
    "blocked",
]


class GPUArbiterRolloutError(RuntimeError):
    """Base error for durable rollout state operations."""


class GPUArbiterRolloutConflict(GPUArbiterRolloutError):
    """The requested transition conflicts with the durable state."""


class GPUArbiterRolloutUnavailable(GPUArbiterRolloutError):
    """The durable rollout state could not be read safely."""


@dataclass(frozen=True)
class GPUArbiterRolloutSnapshot:
    resource_id: str
    state: GPURolloutState
    effective_mode: GPUArbiterMode
    target_mode: GPUArbiterMode
    transition_id: uuid.UUID | None
    last_transition_id: uuid.UUID | None
    blocker_reason: str | None
    revision: int


@dataclass(frozen=True)
class GPUArbiterRolloutDecision:
    resource_id: str
    state: GPURolloutState | Literal["uninitialized", "disabled"]
    effective_mode: GPUArbiterMode
    dispatch_mode: GPUArbiterMode | None
    blocked_reason: str | None
    revision: int | None

    @property
    def dispatch_blocked(self) -> bool:
        return self.dispatch_mode is None


def _optional_uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("GPU rollout identity is malformed")
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("GPU rollout identity is malformed")
    return parsed


def gpu_arbiter_rollout_snapshot(value: object) -> GPUArbiterRolloutSnapshot:
    if type(value) is not dict or set(value) != {
        "resource_id",
        "state",
        "effective_mode",
        "target_mode",
        "transition_id",
        "last_transition_id",
        "blocker_reason",
        "revision",
    }:
        raise ValueError("GPU rollout snapshot is malformed")
    resource_id = value["resource_id"]
    state = value["state"]
    revision = value["revision"]
    blocker_reason = value["blocker_reason"]
    if (
        type(resource_id) is not str
        or not resource_id
        or state not in {"off", "promoting", "enforcing", "demoting", "blocked"}
        or type(revision) is not int
        or revision < 1
        or (blocker_reason is not None and type(blocker_reason) is not str)
    ):
        raise ValueError("GPU rollout snapshot is malformed")
    return GPUArbiterRolloutSnapshot(
        resource_id=resource_id,
        state=state,
        effective_mode=GPUArbiterMode(value["effective_mode"]),
        target_mode=GPUArbiterMode(value["target_mode"]),
        transition_id=_optional_uuid(value["transition_id"]),
        last_transition_id=_optional_uuid(value["last_transition_id"]),
        blocker_reason=blocker_reason,
        revision=revision,
    )


def _operation_snapshot(result: dict[str, object]) -> GPUArbiterRolloutSnapshot:
    outcome = result.get("outcome")
    if outcome == "conflict":
        reason = result.get("reason")
        if type(reason) is not str or not reason:
            raise GPUArbiterRolloutUnavailable(
                "GPU rollout Native conflict is malformed"
            )
        raise GPUArbiterRolloutConflict(reason)
    if outcome == "unavailable":
        raise GPUArbiterRolloutUnavailable("GPU rollout state is unavailable")
    try:
        return gpu_arbiter_rollout_snapshot(result.get("snapshot"))
    except (TypeError, ValueError, KeyError) as exc:
        raise GPUArbiterRolloutUnavailable("GPU rollout snapshot is malformed") from exc


def classify_gpu_arbiter_rollout(
    resource_id: str,
    desired_mode: GPUArbiterMode,
    rollout: GPUArbiterRolloutSnapshot | None,
    *,
    rollout_enabled: bool,
) -> GPUArbiterRolloutDecision:
    """Resolve reporting and dispatch modes without performing I/O."""

    if not rollout_enabled:
        effective = (
            GPUArbiterMode.OBSERVE
            if desired_mode is GPUArbiterMode.OBSERVE
            else GPUArbiterMode.OFF
        )
        return GPUArbiterRolloutDecision(
            resource_id=resource_id,
            state="disabled",
            effective_mode=effective,
            dispatch_mode=effective,
            blocked_reason=None,
            revision=None,
        )

    if rollout is None:
        if desired_mode is GPUArbiterMode.ENFORCE:
            return GPUArbiterRolloutDecision(
                resource_id=resource_id,
                state="uninitialized",
                effective_mode=GPUArbiterMode.OFF,
                dispatch_mode=None,
                blocked_reason="gpu_rollout_not_initialized",
                revision=None,
            )
        return GPUArbiterRolloutDecision(
            resource_id=resource_id,
            state="uninitialized",
            effective_mode=desired_mode,
            dispatch_mode=desired_mode,
            blocked_reason=None,
            revision=None,
        )

    if rollout.state == "off":
        if desired_mode is GPUArbiterMode.ENFORCE:
            dispatch_mode = None
            blocked_reason = "gpu_rollout_promotion_required"
        else:
            dispatch_mode = desired_mode
            blocked_reason = None
        return GPUArbiterRolloutDecision(
            resource_id=resource_id,
            state=rollout.state,
            effective_mode=GPUArbiterMode.OFF,
            dispatch_mode=dispatch_mode,
            blocked_reason=blocked_reason,
            revision=rollout.revision,
        )

    if rollout.state == "enforcing":
        if desired_mode is GPUArbiterMode.ENFORCE:
            return GPUArbiterRolloutDecision(
                resource_id=resource_id,
                state=rollout.state,
                effective_mode=GPUArbiterMode.ENFORCE,
                dispatch_mode=GPUArbiterMode.ENFORCE,
                blocked_reason=None,
                revision=rollout.revision,
            )
        return GPUArbiterRolloutDecision(
            resource_id=resource_id,
            state=rollout.state,
            effective_mode=GPUArbiterMode.ENFORCE,
            dispatch_mode=None,
            blocked_reason="gpu_rollout_demotion_required",
            revision=rollout.revision,
        )

    reason = (
        rollout.blocker_reason
        if rollout.state == "blocked"
        else f"gpu_rollout_{rollout.state}"
    )
    return GPUArbiterRolloutDecision(
        resource_id=resource_id,
        state=rollout.state,
        effective_mode=rollout.effective_mode,
        dispatch_mode=None,
        blocked_reason=reason,
        revision=rollout.revision,
    )


async def read_gpu_arbiter_rollout(
    data: GPUCallerData,
    resource_id: str,
) -> GPUArbiterRolloutSnapshot | None:
    try:
        result = await data.step("rollout_read", {"resource_id": resource_id})
        if set(result) != {"snapshot"}:
            raise GPUAuthorityUnavailable()
        value = result["snapshot"]
        return None if value is None else gpu_arbiter_rollout_snapshot(value)
    except GPUAuthorityUnavailable as exc:
        raise GPUArbiterRolloutUnavailable(
            f"GPU rollout state unavailable for {resource_id}"
        ) from exc
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            f"GPU rollout state unavailable for {resource_id}"
        ) from exc


async def read_gpu_arbiter_rollouts(
    data: GPUCallerData,
) -> tuple[GPUArbiterRolloutSnapshot, ...]:
    """Read every durable rollout in stable resource order."""

    try:
        result = await data.step("rollout_read_all", {})
        if set(result) != {"snapshots"} or type(result["snapshots"]) is not list:
            raise GPUAuthorityUnavailable()
        snapshots = tuple(
            gpu_arbiter_rollout_snapshot(item) for item in result["snapshots"]
        )
        if tuple(item.resource_id for item in snapshots) != tuple(
            sorted(item.resource_id for item in snapshots)
        ):
            raise GPUAuthorityUnavailable()
        if len({item.resource_id for item in snapshots}) != len(snapshots):
            raise GPUAuthorityUnavailable()
        return snapshots
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            "GPU rollout states could not be listed safely"
        ) from exc


async def resolve_gpu_arbiter_rollout(
    data: GPUCallerData,
    resource_id: str,
    *,
    config: GPUAuthoritySettings,
) -> GPUArbiterRolloutDecision:
    desired = config.gpu_arbiter_desired_mode(resource_id)
    if not config.gpu_arbiter_rollout_enabled:
        return classify_gpu_arbiter_rollout(
            resource_id,
            desired,
            None,
            rollout_enabled=False,
        )
    rollout = await read_gpu_arbiter_rollout(data, resource_id)
    return classify_gpu_arbiter_rollout(
        resource_id,
        desired,
        rollout,
        rollout_enabled=True,
    )


async def gpu_rollout_boundary_active(
    data: GPUCallerData,
    *,
    config: GPUAuthoritySettings,
) -> bool:
    """Whether any durable resource may require managed-wire protection."""

    if not config.gpu_arbiter_rollout_enabled:
        return False
    try:
        result = await data.step("rollout_boundary", {})
        if set(result) != {"active"} or type(result["active"]) is not bool:
            raise GPUAuthorityUnavailable()
        return result["active"]
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            "GPU rollout boundary could not be determined"
        ) from exc


async def begin_gpu_arbiter_rollout(
    data: GPUCallerData,
    resource_id: str,
    target_mode: GPUArbiterMode,
) -> GPUArbiterRolloutSnapshot:
    """Start or replay one exact promotion/demotion transition."""

    try:
        result = await data.step(
            "rollout_begin",
            {
                "resource_id": resource_id,
                "target_mode": target_mode.value,
                "candidate_transition_id": str(uuid.uuid4()),
            },
        )
        return _operation_snapshot(result)
    except (GPUArbiterRolloutConflict, GPUArbiterRolloutUnavailable):
        raise
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            "GPU rollout transition is unavailable"
        ) from exc


async def complete_gpu_arbiter_rollout(
    data: GPUCallerData,
    resource_id: str,
    transition_id: uuid.UUID,
) -> GPUArbiterRolloutSnapshot:
    """Settle an exact transition; response-loss replay is read-only."""

    try:
        return _operation_snapshot(
            await data.step(
                "rollout_complete",
                {"resource_id": resource_id, "transition_id": str(transition_id)},
            )
        )
    except (GPUArbiterRolloutConflict, GPUArbiterRolloutUnavailable):
        raise
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            "GPU rollout transition is unavailable"
        ) from exc


async def block_gpu_arbiter_rollout(
    data: GPUCallerData,
    resource_id: str,
    transition_id: uuid.UUID,
    reason: str,
) -> GPUArbiterRolloutSnapshot:
    """Persist an exact transition failure without relaxing its boundary."""

    normalized_reason = reason.strip()[:256]
    if not normalized_reason:
        raise ValueError("GPU rollout blocker reason must be non-empty")
    try:
        return _operation_snapshot(
            await data.step(
                "rollout_block",
                {
                    "resource_id": resource_id,
                    "transition_id": str(transition_id),
                    "reason": normalized_reason,
                },
            )
        )
    except (GPUArbiterRolloutConflict, GPUArbiterRolloutUnavailable):
        raise
    except Exception as exc:
        raise GPUArbiterRolloutUnavailable(
            "GPU rollout transition is unavailable"
        ) from exc


__all__ = [
    "GPUArbiterRolloutConflict",
    "GPUArbiterRolloutDecision",
    "GPUArbiterRolloutSnapshot",
    "GPUArbiterRolloutUnavailable",
    "begin_gpu_arbiter_rollout",
    "block_gpu_arbiter_rollout",
    "classify_gpu_arbiter_rollout",
    "complete_gpu_arbiter_rollout",
    "gpu_arbiter_rollout_snapshot",
    "gpu_rollout_boundary_active",
    "read_gpu_arbiter_rollout",
    "read_gpu_arbiter_rollouts",
    "resolve_gpu_arbiter_rollout",
]
