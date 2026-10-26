"""Backend health orchestration over caller-owned data and client ports."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from aap_inference_contract.capabilities import CapabilityLimits
from aap_protocol_v2.lifecycle import managed_lifecycle_capability_sha256

from .backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER


class BackendRow(Protocol):
    id: uuid.UUID
    gpu_resource_id: str | None
    endpoint_sha256: str
    last_checked_at: datetime | None


class MembershipRow(Protocol):
    membership_epoch: int
    state: str


class PoolMemberRow(Protocol):
    id: uuid.UUID
    pool_id: uuid.UUID
    traffic_state: str


class ServicePoolRow(Protocol):
    id: uuid.UUID
    capability_snapshot: dict[str, Any] | None
    capability_fingerprint: str | None
    routing_generation: int
    legacy_instance_id: uuid.UUID | None
    enabled: bool


class BackendHealthData(Protocol):
    """The original caller transaction's operations needed by health checks."""

    async def get_backend(self, registry_id: uuid.UUID) -> BackendRow | None: ...

    async def read_membership(
        self,
        registry_id: uuid.UUID,
        gpu_resource_id: str,
        *,
        for_update: bool = False,
    ) -> MembershipRow | None: ...

    async def clock_timestamp(self) -> datetime | None: ...

    async def lock_current_backend(
        self, registry_id: uuid.UUID
    ) -> BackendRow | None: ...

    async def lock_gpu_resource(self, resource_id: str) -> None: ...

    async def try_membership_promotion(self) -> bool | None: ...

    async def write_health(
        self, registry_id: uuid.UUID, values: dict[str, Any]
    ) -> int | None: ...

    async def refresh_backend(self, backend: BackendRow) -> None: ...

    async def read_member_pool(self, registry_id: uuid.UUID) -> uuid.UUID | None: ...

    async def load_pool_member(
        self, registry_id: uuid.UUID
    ) -> PoolMemberRow | None: ...

    async def load_pool(
        self, pool_id: uuid.UUID, *, registry_id: uuid.UUID
    ) -> ServicePoolRow | None: ...

    async def stage_pool_reconciliation(
        self,
        registry_id: uuid.UUID,
        pool: ServicePoolRow,
        member: PoolMemberRow,
        *,
        operation: Literal["seed", "disable"],
        snapshot: dict[str, Any],
        fingerprint: str,
        expected_generation: int,
        expected_pool_fingerprint: str | None,
        expected_member_state: str,
    ) -> None: ...

    async def flush(self) -> None: ...


class BackendHealthClient(Protocol):
    async def health_meta(
        self, *, gpu_health_challenge: str | None = None
    ) -> tuple[bool, dict[str, Any] | None]: ...

    async def setup(self) -> dict[str, Any] | None: ...


def _proof_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("proof timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def reconcile_pool_capability(
    data: BackendHealthData,
    registry_id: uuid.UUID,
    capabilities: dict[str, Any],
) -> None:
    """Seed a pool snapshot or disable a member whose stable contract drifted."""
    from .pool_capability import canonicalize_capability, capability_fingerprint

    pool_id = await data.read_member_pool(registry_id)
    if pool_id is None:
        return
    pool = await data.load_pool(pool_id, registry_id=registry_id)
    if pool is None:
        return
    member = await data.load_pool_member(registry_id)
    if member is None or member.pool_id != pool_id:
        return
    snapshot = canonicalize_capability(capabilities)
    fingerprint = capability_fingerprint(snapshot)
    if pool.capability_fingerprint is None:
        expected_generation = pool.routing_generation
        expected_pool_fingerprint = pool.capability_fingerprint
        expected_member_state = member.traffic_state
        pool.capability_snapshot = snapshot
        pool.capability_fingerprint = fingerprint
        pool.routing_generation += 1
        await data.stage_pool_reconciliation(
            registry_id,
            pool,
            member,
            operation="seed",
            snapshot=snapshot,
            fingerprint=fingerprint,
            expected_generation=expected_generation,
            expected_pool_fingerprint=expected_pool_fingerprint,
            expected_member_state=expected_member_state,
        )
    elif (
        pool.capability_fingerprint != fingerprint
        and member.traffic_state != "disabled"
    ):
        expected_generation = pool.routing_generation
        expected_pool_fingerprint = pool.capability_fingerprint
        expected_member_state = member.traffic_state
        member.traffic_state = "disabled"
        if pool.legacy_instance_id == registry_id:
            # Off/observe dispatch uses the legacy instance; drift disables the
            # pool until an equivalent legacy is selected explicitly.
            pool.enabled = False
        pool.routing_generation += 1
        await data.stage_pool_reconciliation(
            registry_id,
            pool,
            member,
            operation="disable",
            snapshot=snapshot,
            fingerprint=fingerprint,
            expected_generation=expected_generation,
            expected_pool_fingerprint=expected_pool_fingerprint,
            expected_member_state=expected_member_state,
        )


class BackendHealthCoordinator:
    """The complete health and capability-pool write algorithm."""

    def __init__(
        self,
        data: BackendHealthData,
        *,
        client_factory: Callable[[BackendRow], BackendHealthClient],
        capability_limits_factory: Callable[[], CapabilityLimits],
    ) -> None:
        self._data = data
        self._client_factory = client_factory
        self._capability_limits_factory = capability_limits_factory

    async def check_health(
        self,
        registry_id: uuid.UUID,
        *,
        gpu_health_challenge: str | None = None,
    ) -> bool:
        if gpu_health_challenge is not None and (
            not isinstance(gpu_health_challenge, str)
            or re.fullmatch(r"[0-9a-f]{64}", gpu_health_challenge) is None
        ):
            raise ValueError(
                "gpu_health_challenge must be 64 lowercase hexadecimal characters"
            )
        backend = await self._data.get_backend(registry_id)
        if not backend:
            return False
        requested_gpu_resource_id = backend.gpu_resource_id
        requested_membership_epoch: int | None = None
        requested_membership_state: str | None = None
        probe_started_at: datetime | None = None
        if requested_gpu_resource_id is not None:
            requested_membership = await self._data.read_membership(
                registry_id, requested_gpu_resource_id
            )
            if requested_membership is None:
                return False
            requested_membership_epoch = requested_membership.membership_epoch
            requested_membership_state = requested_membership.state
            gpu_health_challenge = gpu_health_challenge or secrets.token_hex(32)
            probe_started_at = await self._data.clock_timestamp()
            if probe_started_at is None:
                return False
        elif gpu_health_challenge is not None:
            raise ValueError("gpu_health_challenge requires a GPU backend membership")

        client = self._client_factory(backend)
        # Fetch /health and its metadata together, preserving the backend's error.
        if gpu_health_challenge is None:
            healthy, meta = await client.health_meta()
        else:
            healthy, meta = await client.health_meta(
                gpu_health_challenge=gpu_health_challenge
            )
        echoed_challenge: str | None = None
        if meta is not None:
            meta = dict(meta)
            echoed = meta.pop(GPU_HEALTH_CHALLENGE_ECHO_MARKER, None)
            if isinstance(echoed, str):
                echoed_challenge = echoed

        # A failed or metadata-free probe invalidates the old device snapshot.
        next_health_meta = None
        next_is_interactive: bool | None = None
        caps: dict[str, Any] | None = None
        if healthy and meta is not None:
            from aap_inference import extract_capabilities

            try:
                setup = await client.setup()
                caps = extract_capabilities(
                    setup, limits=self._capability_limits_factory()
                )
            except Exception:
                caps = None

        # The evidence timestamp must enclose both /health and /setup.
        observed_at = await self._data.clock_timestamp()
        if observed_at is None:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return False
        if probe_started_at is not None and (
            probe_started_at.tzinfo is None
            or probe_started_at.utcoffset() is None
            or observed_at <= probe_started_at
        ):
            return False

        if healthy and meta is not None:
            managed_lifecycle_sha256: str | None = None
            if caps is not None:
                meta = {**meta, "capabilities": caps}
                next_is_interactive = caps["is_interactive"]
                managed_lifecycle = caps.get("managed_lifecycle")
                if managed_lifecycle is not None:
                    managed_lifecycle_sha256 = managed_lifecycle_capability_sha256(
                        managed_lifecycle
                    )
            if (
                gpu_health_challenge is not None
                and echoed_challenge == gpu_health_challenge
                and probe_started_at is not None
                and requested_membership_epoch is not None
                and requested_membership_state is not None
            ):
                meta["gpu_arbiter_probe"] = {
                    "protocol_version": "1",
                    "challenge": gpu_health_challenge,
                    "backend_registry_id": str(registry_id),
                    "gpu_resource_id": requested_gpu_resource_id,
                    "membership_epoch": str(requested_membership_epoch),
                    "membership_state": requested_membership_state,
                    "managed_lifecycle_sha256": managed_lifecycle_sha256,
                    "probe_started_at": _proof_timestamp(probe_started_at),
                    "observed_at": _proof_timestamp(observed_at),
                }
            next_health_meta = meta

        values: dict[str, Any] = {
            "state": "connected" if healthy else "error",
            "last_checked_at": observed_at,
            "health_meta": next_health_meta,
        }
        if next_is_interactive is not None:
            values["is_interactive"] = next_is_interactive

        current_backend = await self._data.lock_current_backend(registry_id)
        if current_backend is None or (
            current_backend.endpoint_sha256 != backend.endpoint_sha256
            or current_backend.gpu_resource_id != requested_gpu_resource_id
        ):
            return False
        if requested_membership_epoch is not None:
            # Keep registry -> resource -> try-global -> membership ordering.
            # Promotion shares the resource -> try-global prefix.
            await self._data.lock_gpu_resource(requested_gpu_resource_id)
            promotion_barrier_acquired = await self._data.try_membership_promotion()
            if promotion_barrier_acquired is not True:
                return False

        # Prevent a slow /setup from overwriting a newer overlapping observation.
        commit_order_floor = probe_started_at or observed_at
        if current_backend.last_checked_at is not None and (
            current_backend.last_checked_at.tzinfo is None
            or current_backend.last_checked_at.utcoffset() is None
            or current_backend.last_checked_at >= commit_order_floor
        ):
            return False
        if requested_membership_epoch is not None:
            current_membership = await self._data.read_membership(
                registry_id, requested_gpu_resource_id, for_update=True
            )
            if (
                current_membership is None
                or current_membership.membership_epoch != requested_membership_epoch
                or current_membership.state != requested_membership_state
            ):
                return False

        rowcount = await self._data.write_health(registry_id, values)
        if rowcount != 1:
            await self._data.refresh_backend(backend)
            return False
        await self._data.refresh_backend(backend)
        if healthy and caps is not None:
            await reconcile_pool_capability(self._data, registry_id, caps)
            await self._data.flush()
        return healthy
