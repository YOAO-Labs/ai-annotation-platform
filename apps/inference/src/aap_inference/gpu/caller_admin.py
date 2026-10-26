"""Finite Native GPU administration operations borrowed from caller-owned UOWs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from aap_inference.gpu.control_preparation import (
    GPULegacyAckPreparation,
    GPURolloutControlPreparation,
    prepare_gpu_backend_legacy_ack,
    prepare_gpu_backend_rollout_control,
)
from aap_inference.gpu.fences import (
    GPUBackendFenceSnapshot,
    GPUFenceCounter,
    activate_gpu_backend_membership,
    advance_gpu_backend_fence,
    read_gpu_backend_fence,
    record_gpu_backend_token_expiry,
)
from aap_inference.gpu.native import GPUCallerData, GPUCallerPostgresData
from aap_inference.gpu.rollout_state import (
    GPUArbiterRolloutSnapshot,
    begin_gpu_arbiter_rollout,
    block_gpu_arbiter_rollout,
    complete_gpu_arbiter_rollout,
    read_gpu_arbiter_rollout,
    read_gpu_arbiter_rollouts,
)
from aap_inference.gpu.settings import GPUArbiterMode
from aap_inference_contract.gpu import (
    GPUAdminFenceSnapshot,
    GPUAdminMembershipCandidate,
    GPUAdminRolloutSnapshot,
    GPUAuthorityUnavailable,
)


class GPUCallerAdministrativeData:
    """Finite Native GPU actions borrowed from one API-owned transaction."""

    __slots__ = ("_data",)

    def __init__(self, data: GPUCallerData) -> None:
        self._data = data

    async def prepare_legacy_ack(
        self,
        backend_registry_id: uuid.UUID,
        *,
        resource_id: str,
        membership_epoch: int,
        token_ttl_seconds: int = 30,
    ) -> GPULegacyAckPreparation:
        return await prepare_gpu_backend_legacy_ack(
            self._data,
            backend_registry_id,
            gpu_resource_id=resource_id,
            membership_epoch=membership_epoch,
            token_ttl_seconds=token_ttl_seconds,
        )

    async def prepare_rollout_control(
        self,
        backend_registry_id: uuid.UUID,
        *,
        resource_id: str,
        membership_epoch: int,
        transition_id: uuid.UUID,
        target_gate: Literal["legacy", "enforce"],
        token_ttl_seconds: int = 30,
    ) -> GPURolloutControlPreparation:
        return await prepare_gpu_backend_rollout_control(
            self._data,
            backend_registry_id,
            gpu_resource_id=resource_id,
            membership_epoch=membership_epoch,
            transition_id=transition_id,
            target_gate=target_gate,
            token_ttl_seconds=token_ttl_seconds,
        )

    async def membership_candidates(
        self,
        resource_id: str,
        *,
        states: tuple[Literal["pending", "active", "retiring"], ...] = (
            "pending",
            "active",
        ),
    ) -> tuple[GPUAdminMembershipCandidate, ...]:
        if (
            not states
            or len(set(states)) != len(states)
            or any(state not in {"pending", "active", "retiring"} for state in states)
        ):
            raise ValueError("membership states must be a non-empty unique tuple")
        result = await self._data.step(
            "membership_candidates",
            {"gpu_resource_id": resource_id, "states": list(states)},
        )
        if set(result) != {"candidates"} or type(result["candidates"]) is not list:
            raise GPUAuthorityUnavailable()
        candidates: list[GPUAdminMembershipCandidate] = []
        for item in result["candidates"]:
            if type(item) is not dict or set(item) != {
                "backend_id",
                "membership_epoch",
                "state",
            }:
                raise GPUAuthorityUnavailable()
            state = item["state"]
            if type(state) is not str or state not in states:
                raise GPUAuthorityUnavailable()
            backend_id = item["backend_id"]
            membership_epoch = item["membership_epoch"]
            if (
                type(backend_id) is not str
                or type(membership_epoch) is not int
                or not 1 <= membership_epoch <= 9_223_372_036_854_775_807
            ):
                raise GPUAuthorityUnavailable()
            try:
                parsed_id = uuid.UUID(backend_id)
            except ValueError:
                raise GPUAuthorityUnavailable() from None
            if str(parsed_id) != backend_id:
                raise GPUAuthorityUnavailable()
            candidates.append(
                GPUAdminMembershipCandidate(
                    backend_id=parsed_id,
                    membership_epoch=membership_epoch,
                )
            )
        if tuple(item.backend_id.int for item in candidates) != tuple(
            sorted(item.backend_id.int for item in candidates)
        ):
            raise GPUAuthorityUnavailable()
        return tuple(candidates)

    async def membership_max_concurrency(
        self,
        backend_registry_id: uuid.UUID,
    ) -> int | None:
        result = await self._data.step(
            "membership_max_concurrency",
            {"backend_registry_id": str(backend_registry_id)},
        )
        if set(result) != {"max_concurrency"}:
            raise GPUAuthorityUnavailable()
        value = result["max_concurrency"]
        if value is not None and (type(value) is not int or not 1 <= value <= 10_000):
            raise GPUAuthorityUnavailable()
        return value

    async def read_rollout(
        self,
        resource_id: str,
    ) -> GPUAdminRolloutSnapshot | None:
        snapshot = await read_gpu_arbiter_rollout(self._data, resource_id)
        return None if snapshot is None else _rollout_fact(snapshot)

    async def read_rollouts(self) -> tuple[GPUAdminRolloutSnapshot, ...]:
        return tuple(
            _rollout_fact(snapshot)
            for snapshot in await read_gpu_arbiter_rollouts(self._data)
        )

    async def rollout_boundary(self) -> bool:
        result = await self._data.step("rollout_boundary", {})
        if set(result) != {"active"} or type(result["active"]) is not bool:
            raise GPUAuthorityUnavailable()
        return result["active"]

    async def begin_rollout(
        self,
        resource_id: str,
        target_mode: Literal["off", "observe", "enforce"],
    ) -> GPUAdminRolloutSnapshot:
        return _rollout_fact(
            await begin_gpu_arbiter_rollout(
                self._data,
                resource_id,
                GPUArbiterMode(target_mode),
            )
        )

    async def complete_rollout(
        self,
        resource_id: str,
        transition_id: uuid.UUID,
    ) -> GPUAdminRolloutSnapshot:
        return _rollout_fact(
            await complete_gpu_arbiter_rollout(
                self._data,
                resource_id,
                transition_id,
            )
        )

    async def block_rollout(
        self,
        resource_id: str,
        transition_id: uuid.UUID,
        reason: str,
    ) -> GPUAdminRolloutSnapshot:
        return _rollout_fact(
            await block_gpu_arbiter_rollout(
                self._data,
                resource_id,
                transition_id,
                reason,
            )
        )

    async def read_fence(
        self,
        backend_registry_id: uuid.UUID,
    ) -> GPUAdminFenceSnapshot | None:
        fence = await read_gpu_backend_fence(self._data, backend_registry_id)
        return None if fence is None else _fence_fact(fence)

    async def advance_fence(
        self,
        backend_registry_id: uuid.UUID,
        counter: GPUFenceCounter,
        *,
        resource_id: str,
        membership_epoch: int,
        token_expires_at: datetime | None = None,
    ) -> str:
        return await advance_gpu_backend_fence(
            self._data,
            backend_registry_id,
            counter,
            gpu_resource_id=resource_id,
            membership_epoch=membership_epoch,
            token_expires_at=token_expires_at,
        )

    async def activate_membership_fence(
        self,
        backend_registry_id: uuid.UUID,
        *,
        resource_id: str,
        membership_epoch: int,
    ) -> str:
        return await activate_gpu_backend_membership(
            self._data,
            backend_registry_id,
            gpu_resource_id=resource_id,
            membership_epoch=membership_epoch,
        )

    async def record_token_expiry(
        self,
        backend_registry_id: uuid.UUID,
        *,
        resource_id: str,
        membership_epoch: int,
        token_expires_at: datetime,
    ) -> datetime:
        return await record_gpu_backend_token_expiry(
            self._data,
            backend_registry_id,
            gpu_resource_id=resource_id,
            membership_epoch=membership_epoch,
            token_expires_at=token_expires_at,
        )


def _rollout_fact(
    snapshot: GPUArbiterRolloutSnapshot,
) -> GPUAdminRolloutSnapshot:
    return GPUAdminRolloutSnapshot(
        resource_id=snapshot.resource_id,
        state=snapshot.state,
        effective_mode=snapshot.effective_mode.value,
        target_mode=snapshot.target_mode.value,
        transition_id=snapshot.transition_id,
        last_transition_id=snapshot.last_transition_id,
        blocker_reason=snapshot.blocker_reason,
        revision=snapshot.revision,
    )


def _fence_fact(snapshot: GPUBackendFenceSnapshot) -> GPUAdminFenceSnapshot:
    return GPUAdminFenceSnapshot(
        backend_registry_id=snapshot.backend_registry_id,
        generation_high_water=snapshot.generation_high_water,
        control_epoch_high_water=snapshot.control_epoch_high_water,
        runtime_epoch_high_water=snapshot.runtime_epoch_high_water,
        token_expiry_high_water=(
            None
            if snapshot.token_expiry_high_water is None
            else snapshot.token_expiry_high_water.isoformat()
        ),
        updated_at=snapshot.updated_at.isoformat(),
    )


async def bind_gpu_caller_administrative_data(
    connection: Any,
) -> GPUCallerAdministrativeData:
    """Bind finite caller-UOW administration actions to its fixed Native profile."""

    try:
        data = await GPUCallerPostgresData.bind(connection)
    except GPUAuthorityUnavailable:
        raise
    except Exception:
        raise GPUAuthorityUnavailable() from None
    return GPUCallerAdministrativeData(data)
