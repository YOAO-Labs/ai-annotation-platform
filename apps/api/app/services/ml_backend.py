from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from aap_protocol_v2.backend_auth import managed_provider
from aap_protocol_v2.lifecycle import managed_lifecycle_capability_sha256
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.policy import validate_gpu_claim
from app.services.ml_client import (
    GPU_HEALTH_CHALLENGE_ECHO_MARKER,
    MLBackendClient,
)


class MLBackendDeleteBlocked(Exception):
    """B-28 · ml_backend 上仍有 running prediction job，拒绝删除。"""

    def __init__(self, running_jobs: int) -> None:
        super().__init__(f"ml backend has {running_jobs} running prediction job(s)")
        self.running_jobs = running_jobs


class MLBackendURLConflict(Exception):
    """A different global registry row already owns the normalized URL."""

    def __init__(self, backend_name: str) -> None:
        super().__init__(f"ML backend URL already registered by {backend_name}")
        self.backend_name = backend_name


class MLBackendPoolMemberConflict(Exception):
    """A registry is already owned by another service pool."""

    def __init__(self, pool_id: uuid.UUID) -> None:
        super().__init__(f"registry already belongs to service pool {pool_id}")
        self.pool_id = pool_id


class MLBackendPoolCapabilityUnavailable(Exception):
    """A multi-member pool has no trustworthy capability baseline yet."""


class MLBackendCapabilityRevalidationError(Exception):
    """A capability drift cannot be accepted without violating pool safety."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        blocking_member_ids: tuple[uuid.UUID, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.blocking_member_ids = blocking_member_ids


@dataclass(frozen=True)
class CapabilityDriftPreview:
    pool_id: uuid.UUID
    registry_id: uuid.UUID
    member_state: str
    pool_enabled: bool
    pool_fingerprint: str | None
    candidate_fingerprint: str | None
    differing_fields: tuple[str, ...]
    has_drift: bool
    can_accept: bool
    blocking_member_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class CapabilityDriftAcceptance:
    old_fingerprint: str
    new_fingerprint: str
    differing_fields: tuple[str, ...]
    pool_enabled: bool


class GPUBackendManagedMutationBlocked(Exception):
    """A backend that entered a managed runtime must use managed retirement."""


def _gpu_membership_constraint_name(exc: IntegrityError) -> str | None:
    for source in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if source is None:
            continue
        name = getattr(source, "constraint_name", None)
        if isinstance(name, str):
            return name
        diag = getattr(source, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


def _raise_managed_mutation_for_integrity(exc: IntegrityError) -> None:
    constraint_name = _gpu_membership_constraint_name(exc)
    if constraint_name is not None and constraint_name.startswith(
        "ck_gpu_backend_membership_"
    ):
        raise GPUBackendManagedMutationBlocked(
            "managed GPU backend requires retirement before mutation"
        ) from exc
    raise exc


_REGISTRY_MUTABLE_FIELDS = {
    "name",
    "url",
    "is_interactive",
    "auth_method",
    "auth_token",
    "extra_params",
    "gpu_resource_id",
    "vram_budget_mb",
    "eviction_priority",
}


def _proof_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("proof timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MLBackendService:
    """v0.19.0 ADR-0044 · backend 上提为全局注册表(MLBackendRegistry) + 项目启用关联。
    v0.23.3 ADR-0050 · 项目启用关联迁移到服务池层 (ProjectMLBackendPool): 项目不再
    绑定单个物理实例, 而是绑定一个服务池; off mode 下每 pool 是 singleton, 经
    legacy_instance_id 解析回原 registry 实例, 行为与 v0.23.2 一致。

    本服务既管全局注册项(superadmin)，也管项目级启用/覆盖。`get` 按 registry id
    返回全局注册项; 项目内「可用 backend」走 `list_enabled_for_project` /
    `is_enabled` (读 ProjectMLBackendPool.enabled=true, 经 pool member 解析回 registry)。
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        shadow_session_factory: GPUShadowSessionFactory | None = None,
        dispatch_context_factory: GPUDispatchContextFactory | None = None,
    ) -> None:
        self.db = db
        self.shadow_session_factory = shadow_session_factory
        self.dispatch_context_factory = dispatch_context_factory

    # ── 全局注册表 ───────────────────────────────────────────────────────────
    async def create_registry(
        self, name: str, url: str, source: str = "manual", **kwargs
    ) -> MLBackendRegistry:
        unknown = set(kwargs) - (_REGISTRY_MUTABLE_FIELDS - {"name", "url"})
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")
        validate_gpu_claim(
            kwargs.get("gpu_resource_id"),
            kwargs.get("vram_budget_mb"),
            extra_params=kwargs.get("extra_params"),
        )
        env_managed_default = False
        if "auth_method" not in kwargs:
            from app.services.managed_inference_auth import provider_for_url

            provider = provider_for_url(url)
            if provider:
                kwargs["auth_method"] = f"managed:{provider}"
                env_managed_default = source == "env"
        if managed_provider(kwargs.get("auth_method")) is not None:
            if env_managed_default:
                from aap_protocol_v2.managed_credential import ensure_credential
                from app.services.managed_inference_auth import credential_directory

                kwargs["auth_token"] = ensure_credential(credential_directory(provider))
            else:
                from app.services.managed_inference_auth import initialize_managed_auth

                kwargs["auth_token"] = await initialize_managed_auth(
                    url, kwargs.get("auth_token"), kwargs["auth_method"]
                )
        row = MLBackendRegistry(
            id=uuid.uuid4(), name=name, url=url.rstrip("/"), source=source, **kwargs
        )
        self.db.add(row)
        await self.db.flush()
        # v0.23.3 ADR-0050 · 每 registry 创建即生成 singleton pool + active 成员。
        # off mode 下项目经 pool.legacy_instance_id 解析回此 registry, 行为不变;
        # 管理员后续可向同一 pool 加等价副本实现负载均衡。
        await self._create_singleton_pool(row)
        return row

    async def _create_singleton_pool(
        self, registry: MLBackendRegistry
    ) -> MLBackendServicePool:
        """为 registry 创建 singleton 服务池 (name 取 registry 名, legacy 指向它)。

        幂等: 若 registry 已有 pool (经 member 反查), 直接返回既有 pool。
        env auto-upsert 与重启不会重复创建 (uq_ml_backend_pool_members_registry)。
        """
        existing = await self.db.execute(
            select(MLBackendServicePool)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.pool_id == MLBackendServicePool.id,
            )
            .where(MLBackendPoolMember.registry_id == registry.id)
        )
        pool = existing.scalars().first()
        if pool is not None:
            return pool
        raw_caps = (registry.health_meta or {}).get("capabilities")
        capability_snapshot = None
        capability_fp = None
        if isinstance(raw_caps, dict):
            from app.services.ml_routing.capability import (
                canonicalize_capability,
                capability_fingerprint,
            )

            capability_snapshot = canonicalize_capability(raw_caps)
            capability_fp = capability_fingerprint(capability_snapshot)
        pool = MLBackendServicePool(
            id=uuid.uuid4(),
            name=registry.name,
            enabled=False,
            routing_policy="smooth_weighted_round_robin",
            legacy_instance_id=registry.id,
            routing_generation=1,
            capability_fingerprint=capability_fp,
            capability_snapshot=capability_snapshot,
        )
        self.db.add(pool)
        await self.db.flush()
        member = MLBackendPoolMember(
            id=uuid.uuid4(),
            pool_id=pool.id,
            registry_id=registry.id,
            traffic_state="active",
            weight=1,
        )
        self.db.add(member)
        await self.db.flush()
        return pool

    # ── v0.23.3 ADR-0050 §12.1 · Super Admin pool/member management ──────────
    async def create_pool(
        self, name: str, *, legacy_instance_id: uuid.UUID | None = None
    ) -> MLBackendServicePool:
        """Create a disabled empty pool (§12.1). Adding the first member sets legacy_instance_id
        + computes capability fingerprint; only then can the pool be enabled (D15)."""
        if legacy_instance_id is not None:
            raise ValueError(
                "legacy_instance_id cannot be set without membership; create the pool then add the member"
            )
        pool = MLBackendServicePool(
            id=uuid.uuid4(),
            name=name,
            enabled=False,
            routing_policy="smooth_weighted_round_robin",
            legacy_instance_id=None,
            routing_generation=1,
        )
        self.db.add(pool)
        await self.db.flush()
        return pool

    async def get_pool(self, pool_id: uuid.UUID) -> MLBackendServicePool | None:
        return await self.db.get(MLBackendServicePool, pool_id)

    async def _get_pool_for_update(
        self, pool_id: uuid.UUID, *, nowait: bool = False
    ) -> MLBackendServicePool | None:
        return await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update(nowait=nowait)
        )

    async def list_pools(self) -> list[MLBackendServicePool]:
        result = await self.db.execute(
            select(MLBackendServicePool).order_by(
                MLBackendServicePool.created_at.desc()
            )
        )
        return list(result.scalars().all())

    async def update_pool(
        self,
        pool_id: uuid.UUID,
        *,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> MLBackendServicePool | None:
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            return None
        changed = False
        if name is not None and pool.name != name:
            pool.name = name
            changed = True
        if enabled is not None:
            if enabled and pool.legacy_instance_id is None:
                raise ValueError(
                    "cannot enable a pool with no legacy instance; add a member first (D15)"
                )
            if enabled and pool.capability_fingerprint is None:
                raise ValueError(
                    "cannot enable a pool before a member capability snapshot is available"
                )
            if enabled:
                member = await self.db.scalar(
                    select(MLBackendPoolMember).where(
                        MLBackendPoolMember.pool_id == pool.id,
                        MLBackendPoolMember.registry_id == pool.legacy_instance_id,
                        MLBackendPoolMember.traffic_state == "active",
                    )
                )
                if member is None:
                    raise ValueError(
                        "legacy instance must be an active member of the pool"
                    )
            if pool.enabled != enabled:
                pool.enabled = enabled
                changed = True
        if changed:
            pool.routing_generation += 1
        await self.db.flush()
        return pool

    async def delete_pool(self, pool_id: uuid.UUID) -> bool:
        """Delete a pool. Members cascade; legacy_instance_id FK is RESTRICT so the
        legacy registry must be removed from membership first (or pool disabled + cleared)."""
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            return False
        member = await self.db.scalar(
            select(MLBackendPoolMember.id).where(MLBackendPoolMember.pool_id == pool_id)
        )
        if member is not None:
            raise ValueError(
                "remove every service-pool member before deleting the pool"
            )
        await self.db.delete(pool)
        await self.db.flush()
        return True

    async def add_pool_member(
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        weight: int = 1,
        capability_snapshot: dict | None = None,
    ) -> tuple[MLBackendPoolMember, MLBackendServicePool]:
        """Add a registry as a pool member. Validates capability fingerprint exact match
        against the pool snapshot (D3); first member seeds the pool fingerprint + legacy.

        Returns (member, pool). Raises CapabilityMismatch on fingerprint divergence.
        """
        from app.services.ml_routing.capability import (
            canonicalize_capability,
            capability_fingerprint,
            diff_capabilities,
        )
        from app.services.ml_routing.contracts import CapabilityMismatchError

        pool = await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )
        if pool is None:
            raise ValueError(f"pool {pool_id} not found")
        registry = await self.get(registry_id)
        if registry is None:
            raise ValueError(f"registry {registry_id} not found")
        owned_member = await self.db.scalar(
            select(MLBackendPoolMember)
            .where(MLBackendPoolMember.registry_id == registry_id)
            .with_for_update()
        )
        if owned_member is not None and owned_member.pool_id != pool_id:
            raise MLBackendPoolMemberConflict(owned_member.pool_id)

        # Capability fingerprint check (D3): exact match required for active routing.
        candidate_caps = (
            (registry.health_meta or {}).get("capabilities")
            if registry.health_meta
            else None
        )
        candidate_snapshot = (
            canonicalize_capability(candidate_caps) if candidate_caps else None
        )
        candidate_fp = (
            capability_fingerprint(candidate_snapshot) if candidate_snapshot else None
        )
        if pool.capability_fingerprint is not None:
            mismatch = diff_capabilities(pool.capability_snapshot, candidate_snapshot)
            if mismatch is not None:
                raise CapabilityMismatchError(mismatch)
        if owned_member is not None:
            member = owned_member
            if pool.capability_fingerprint is None and candidate_fp is not None:
                pool.capability_fingerprint = candidate_fp
                pool.capability_snapshot = candidate_snapshot
                pool.routing_generation += 1
            if member.weight != weight:
                member.weight = weight
                pool.routing_generation += 1
            await self.db.flush()
            return member, pool
        if pool.capability_fingerprint is None:
            existing_member_id = await self.db.scalar(
                select(MLBackendPoolMember.id).where(
                    MLBackendPoolMember.pool_id == pool_id
                )
            )
            if existing_member_id is not None:
                raise MLBackendPoolCapabilityUnavailable(
                    "service pool capability baseline is unavailable; refresh the existing member before adding replicas"
                )
        member = MLBackendPoolMember(
            id=uuid.uuid4(),
            pool_id=pool_id,
            registry_id=registry_id,
            traffic_state="active",
            weight=weight,
        )
        self.db.add(member)
        # First member seeds the pool fingerprint + legacy_instance_id (§7.3).
        if pool.capability_fingerprint is None and candidate_fp is not None:
            pool.capability_fingerprint = candidate_fp
            pool.capability_snapshot = candidate_snapshot
        if pool.legacy_instance_id is None:
            pool.legacy_instance_id = registry_id
        pool.routing_generation += 1
        await self.db.flush()
        return member, pool

    async def _reconcile_pool_capability(
        self, registry_id: uuid.UUID, capabilities: dict
    ) -> None:
        """Seed a pool snapshot or disable a member whose stable contract drifted."""
        from app.services.ml_routing.capability import (
            canonicalize_capability,
            capability_fingerprint,
        )

        member = await self.db.scalar(
            select(MLBackendPoolMember)
            .where(MLBackendPoolMember.registry_id == registry_id)
            .with_for_update()
        )
        if member is None:
            return
        pool = await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == member.pool_id)
            .with_for_update()
        )
        if pool is None:
            return
        snapshot = canonicalize_capability(capabilities)
        fingerprint = capability_fingerprint(snapshot)
        if pool.capability_fingerprint is None:
            pool.capability_snapshot = snapshot
            pool.capability_fingerprint = fingerprint
            pool.routing_generation += 1
        elif (
            pool.capability_fingerprint != fingerprint
            and member.traffic_state != "disabled"
        ):
            member.traffic_state = "disabled"
            if pool.legacy_instance_id == registry_id:
                # off/observe dispatch always uses the legacy instance.  Once its
                # contract drifts, disable the pool until an equivalent legacy is
                # selected explicitly; never bypass the disabled member state.
                pool.enabled = False
            pool.routing_generation += 1

    @staticmethod
    def _registry_capability_snapshot(
        registry: MLBackendRegistry,
    ) -> tuple[dict | None, str | None]:
        from app.services.ml_routing.capability import (
            canonicalize_capability,
            capability_fingerprint,
        )

        raw = (registry.health_meta or {}).get("capabilities")
        if not isinstance(raw, dict):
            return None, None
        snapshot = canonicalize_capability(raw)
        return snapshot, capability_fingerprint(snapshot)

    @staticmethod
    def _capability_blocking_members(
        rows: list[tuple[MLBackendPoolMember, MLBackendRegistry]],
        *,
        target_registry_id: uuid.UUID,
        candidate_fingerprint: str,
    ) -> tuple[uuid.UUID, ...]:
        blocking: list[uuid.UUID] = []
        for member, registry in rows:
            if registry.id == target_registry_id or member.traffic_state == "disabled":
                continue
            _, member_fingerprint = MLBackendService._registry_capability_snapshot(
                registry
            )
            if member_fingerprint != candidate_fingerprint:
                blocking.append(registry.id)
        return tuple(sorted(blocking, key=str))

    async def preview_capability_drift(
        self, pool_id: uuid.UUID, registry_id: uuid.UUID
    ) -> CapabilityDriftPreview | None:
        from app.services.ml_routing.capability import diff_capabilities

        pool = await self.db.get(MLBackendServicePool, pool_id)
        if pool is None:
            return None
        result = await self.db.execute(
            select(MLBackendPoolMember, MLBackendRegistry)
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == MLBackendPoolMember.registry_id,
            )
            .where(MLBackendPoolMember.pool_id == pool_id)
        )
        rows = list(result.tuples().all())
        target = next(
            (
                (member, registry)
                for member, registry in rows
                if registry.id == registry_id
            ),
            None,
        )
        if target is None:
            return None
        member, registry = target
        candidate_snapshot, candidate_fingerprint = self._registry_capability_snapshot(
            registry
        )
        mismatch = (
            diff_capabilities(pool.capability_snapshot, candidate_snapshot)
            if pool.capability_snapshot is not None and candidate_snapshot is not None
            else None
        )
        differing_fields = mismatch.differing_fields if mismatch is not None else ()
        has_drift = (
            pool.capability_fingerprint is not None
            and candidate_fingerprint is not None
            and pool.capability_fingerprint != candidate_fingerprint
        )
        blocking_member_ids = (
            self._capability_blocking_members(
                rows,
                target_registry_id=registry_id,
                candidate_fingerprint=candidate_fingerprint,
            )
            if candidate_fingerprint is not None
            else ()
        )
        return CapabilityDriftPreview(
            pool_id=pool.id,
            registry_id=registry.id,
            member_state=member.traffic_state,
            pool_enabled=pool.enabled,
            pool_fingerprint=pool.capability_fingerprint,
            candidate_fingerprint=candidate_fingerprint,
            differing_fields=differing_fields,
            has_drift=has_drift,
            can_accept=(
                registry.state == "connected"
                and member.traffic_state == "disabled"
                and pool.capability_fingerprint is not None
                and candidate_fingerprint is not None
                and not blocking_member_ids
            ),
            blocking_member_ids=blocking_member_ids,
        )

    async def accept_capability_drift(
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        expected_candidate_fingerprint: str,
        enable_pool: bool,
    ) -> tuple[MLBackendServicePool, CapabilityDriftAcceptance] | None:
        """Accept a reviewed capability contract and restore one disabled member.

        The caller must perform a fresh health probe first. This method locks the
        complete pool topology and verifies the reviewed fingerprint again before
        atomically replacing the baseline and restoring routing.
        """
        from app.services.ml_routing.capability import diff_capabilities

        pool = await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )
        if pool is None:
            return None
        result = await self.db.execute(
            select(MLBackendPoolMember, MLBackendRegistry)
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == MLBackendPoolMember.registry_id,
            )
            .where(MLBackendPoolMember.pool_id == pool_id)
            .with_for_update()
        )
        rows = list(result.tuples().all())
        target = next(
            (
                (member, registry)
                for member, registry in rows
                if registry.id == registry_id
            ),
            None,
        )
        if target is None:
            return None
        member, registry = target
        if registry.state != "connected":
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_capability_revalidation_failed",
                "backend must be connected before accepting capability drift",
            )
        candidate_snapshot, candidate_fingerprint = self._registry_capability_snapshot(
            registry
        )
        if candidate_snapshot is None or candidate_fingerprint is None:
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_capability_snapshot_unavailable",
                "backend capability snapshot is unavailable",
            )
        if candidate_fingerprint != expected_candidate_fingerprint:
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_capability_changed_during_review",
                "backend capability changed after review; refresh the preview",
            )
        if member.traffic_state != "disabled":
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_capability_revalidation_not_required",
                "only a disabled member can accept capability drift",
            )
        if pool.capability_snapshot is None or pool.capability_fingerprint is None:
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_capability_snapshot_unavailable",
                "service pool capability baseline is unavailable",
            )
        mismatch = diff_capabilities(pool.capability_snapshot, candidate_snapshot)
        blocking_member_ids = self._capability_blocking_members(
            rows,
            target_registry_id=registry_id,
            candidate_fingerprint=candidate_fingerprint,
        )
        if blocking_member_ids:
            raise MLBackendCapabilityRevalidationError(
                "ml_backend_pool_capability_blocked_by_members",
                "active or draining pool members do not match the reviewed capability",
                blocking_member_ids=blocking_member_ids,
            )

        old_fingerprint = pool.capability_fingerprint
        if mismatch is not None:
            pool.capability_snapshot = candidate_snapshot
            pool.capability_fingerprint = candidate_fingerprint
        member.traffic_state = "active"
        if enable_pool:
            legacy_member = next(
                (
                    pool_member
                    for pool_member, pool_registry in rows
                    if pool_registry.id == pool.legacy_instance_id
                ),
                None,
            )
            if legacy_member is None or (
                legacy_member is not member and legacy_member.traffic_state != "active"
            ):
                raise MLBackendCapabilityRevalidationError(
                    "ml_backend_capability_revalidation_failed",
                    "legacy instance must be an active member before enabling the pool",
                )
            pool.enabled = True
        pool.routing_generation += 1
        await self.db.flush()
        return pool, CapabilityDriftAcceptance(
            old_fingerprint=old_fingerprint,
            new_fingerprint=candidate_fingerprint,
            differing_fields=mismatch.differing_fields if mismatch is not None else (),
            pool_enabled=pool.enabled,
        )

    async def remove_pool_member(
        self, pool_id: uuid.UUID, registry_id: uuid.UUID
    ) -> bool:
        """Remove a member. If it's the legacy_instance_id, clear that pointer + disable
        pool (D5/D15: non-empty enabled pool must have legacy member)."""
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            return False
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
        )
        member = result.scalar_one_or_none()
        if member is None:
            return False
        await self.db.delete(member)
        if pool.legacy_instance_id == registry_id:
            pool.legacy_instance_id = None
            pool.enabled = False
        pool.routing_generation += 1
        await self.db.flush()
        return True

    async def drain_pool_member(
        self, pool_id: uuid.UUID, registry_id: uuid.UUID
    ) -> MLBackendPoolMember | None:
        """Set member traffic_state=draining (no new leases; keeps existing)."""
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
        )
        member = result.scalar_one_or_none()
        if member is None:
            return None
        pool = await self._get_pool_for_update(pool_id)
        changed = member.traffic_state != "draining"
        if changed:
            member.traffic_state = "draining"
        if pool is not None and changed:
            pool.routing_generation += 1
        await self.db.flush()
        return member

    async def resume_pool_member(
        self, pool_id: uuid.UUID, registry_id: uuid.UUID
    ) -> MLBackendPoolMember | None:
        """Resume a draining member back to active. Disabled members need re-validation."""
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
        )
        member = result.scalar_one_or_none()
        if member is None:
            return None
        if member.traffic_state == "disabled":
            raise ValueError(
                "disabled member needs capability re-validation before resume"
            )
        changed = member.traffic_state != "active"
        if changed:
            member.traffic_state = "active"
        pool = await self._get_pool_for_update(pool_id)
        if pool is not None and changed:
            pool.routing_generation += 1
        await self.db.flush()
        return member

    async def get(self, registry_id: uuid.UUID) -> MLBackendRegistry | None:
        result = await self.db.execute(
            select(MLBackendRegistry).where(MLBackendRegistry.id == registry_id)
        )
        return result.scalar_one_or_none()

    async def get_by_url(self, url: str) -> MLBackendRegistry | None:
        result = await self.db.execute(
            select(MLBackendRegistry).where(MLBackendRegistry.url == url.rstrip("/"))
        )
        return result.scalar_one_or_none()

    async def list_registry(self) -> list[MLBackendRegistry]:
        result = await self.db.execute(
            select(MLBackendRegistry).order_by(MLBackendRegistry.created_at.desc())
        )
        return list(result.scalars().all())

    async def update(
        self, registry_id: uuid.UUID, *, preserve_manual_auth: bool = False, **kwargs
    ) -> MLBackendRegistry | None:
        row = await self.get(registry_id)
        if not row:
            return None
        if (
            preserve_manual_auth
            and row.auth_method != "none"
            and managed_provider(row.auth_method) is None
        ):
            raise HTTPException(
                409,
                detail={
                    "error_code": "manual_auth_preserved",
                    "message": "实例已改为手动认证，已保留该配置；请刷新后查看。",
                },
            )
        requested_identity = (row.url, row.auth_revision)
        unknown = set(kwargs) - _REGISTRY_MUTABLE_FIELDS
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")

        if "url" in kwargs:
            url = kwargs["url"]
            if not isinstance(url, str) or not url:
                raise ValueError("url must not be null or empty")
            normalized_url = url.rstrip("/")
            conflict = await self.get_by_url(normalized_url)
            if conflict is not None and conflict.id != row.id:
                raise MLBackendURLConflict(conflict.name)
            kwargs["url"] = normalized_url

        next_method = kwargs.get("auth_method", row.auth_method)
        if managed_provider(next_method) is not None and (
            "auth_method" in kwargs or "url" in kwargs or "auth_token" in kwargs
        ):
            from app.services.managed_inference_auth import initialize_managed_auth

            kwargs["auth_token"] = await initialize_managed_auth(
                kwargs.get("url", row.url), kwargs.get("auth_token"), next_method
            )

        # Do not hold a row lock across the authentication handshake. Recheck the
        # final identity under the same lock used by concurrent configuration writes.
        row = await self.db.scalar(
            select(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        if (row.url, row.auth_revision) != requested_identity:
            raise HTTPException(409, "服务地址或认证已被其他操作修改，请刷新后重试。")
        if (
            managed_provider(row.auth_method) is not None
            and managed_provider(next_method) is None
        ):
            if next_method == "token" and not kwargs.get("auth_token"):
                raise HTTPException(
                    422, "改用手动 Token 时必须填写服务端令牌；系统托管凭据不能沿用。"
                )
            if next_method != "token":
                kwargs["auth_token"] = None

        protected_values = {
            "url": row.url,
            "extra_params": row.extra_params,
            "gpu_resource_id": row.gpu_resource_id,
            "vram_budget_mb": row.vram_budget_mb,
            "eviction_priority": row.eviction_priority,
        }
        protected_change = any(
            field in kwargs and kwargs[field] != previous
            for field, previous in protected_values.items()
        )
        auth_change = any(
            field in kwargs and kwargs[field] != getattr(row, field)
            for field in ("auth_method", "auth_token")
        )
        if protected_change:
            fence = await self.db.get(GPUBackendFence, registry_id)
            if fence is not None and fence.runtime_epoch_high_water > 0:
                raise GPUBackendManagedMutationBlocked(
                    "managed GPU backend requires retirement before mutation"
                )

        next_resource_id = kwargs.get("gpu_resource_id", row.gpu_resource_id)
        next_budget_mb = kwargs.get("vram_budget_mb", row.vram_budget_mb)
        next_extra_params = kwargs.get("extra_params", row.extra_params)
        validate_gpu_claim(
            next_resource_id,
            next_budget_mb,
            extra_params=next_extra_params,
        )
        if "eviction_priority" in kwargs and kwargs["eviction_priority"] is None:
            raise ValueError("eviction_priority must not be null")

        for key, value in kwargs.items():
            setattr(row, key, value)
        if protected_change or auth_change:
            # Any membership/config epoch change invalidates the cached residency
            # evidence. Authentication rotation invalidates observations without
            # changing GPU membership or its independent control credentials.
            row.state = "disconnected"
            row.health_meta = None
            row.last_checked_at = None
            row.error_message = None
        try:
            await self.db.flush()
        except IntegrityError as exc:
            _raise_managed_mutation_for_integrity(exc)
        if auth_change:
            await self.db.refresh(row)
        return row

    async def _count_running_predictions(self, registry_id: uuid.UUID) -> int:
        """Legacy deletion guard only; never an allocation or active-lease truth."""

        running = await self.db.execute(
            select(AsyncJob.id).where(
                AsyncJob.kind == "batch_predict",
                AsyncJob.payload["ml_backend_id"].astext == str(registry_id),
                AsyncJob.status == AsyncJobStatus.RUNNING.value,
            )
        )
        return len(list(running.scalars().all()))

    async def validate_delete(self, registry_id: uuid.UUID) -> bool:
        """Run non-mutating legacy job and managed-GPU deletion guards."""
        if await self.get(registry_id) is None:
            return False
        from app.db.models.sample_evaluation import SampleEvaluationRun
        from app.db.models.trained_model import ModelDeployment

        if await self.db.scalar(
            select(SampleEvaluationRun.id)
            .join(
                ModelDeployment, ModelDeployment.id == SampleEvaluationRun.deployment_id
            )
            .where(ModelDeployment.registry_id == registry_id)
            .limit(1)
        ):
            raise HTTPException(
                status_code=409, detail="推理实例被冻结样本评估引用，请先结束并删除评估"
            )
        running_jobs = await self._count_running_predictions(registry_id)
        if running_jobs:
            raise MLBackendDeleteBlocked(running_jobs)
        fence = await self.db.get(GPUBackendFence, registry_id)
        if fence is not None and fence.runtime_epoch_high_water > 0:
            raise GPUBackendManagedMutationBlocked(
                "managed GPU backend requires retirement before delete"
            )
        return True

    async def delete(self, registry_id: uuid.UUID) -> bool:
        row = await self.get(registry_id)
        if not row:
            return False
        await self.validate_delete(registry_id)
        # 级联: 解绑 projects.ml_backend_pool_id (SET NULL 语义);
        # project_ml_backend_pool 关联由 FK ondelete=CASCADE 自动清。
        # 历史 prediction.ml_backend_id / ml_backend_pool_id 同样 SET NULL。
        # v0.23.3 ADR-0050 §5.2: 删除 registry 前必须先清理服务池层 ——
        # pool.legacy_instance_id (FK RESTRICT) 与 pool_member.registry_id (FK RESTRICT)
        # 都引用此 registry。off mode singleton 下: 删 member → 若该 registry 是某 pool
        # 的 legacy_instance_id 且 pool 无其它成员, 把 pool 置 disabled + 清 legacy 指针
        # (空 pool 只能 disabled) → 再删 registry 行。多成员 pool 的 legacy 接替留给
        # 显式 pool 管理 API (v0.23.4), 本删除路径只处理 singleton / 无 legacy 场景。
        pool = await self._pool_for_registry(registry_id)
        if pool is not None:
            bound_projects = await self.db.execute(
                select(Project).where(Project.ml_backend_pool_id == pool.id)
            )
            for project in bound_projects.scalars():
                project.ml_backend_pool_id = None
            # 删该 registry 在所有 pool 的成员关系 (singleton 下只有一条)。
            members = await self.db.execute(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            for member in members.scalars():
                await self.db.delete(member)
            await self.db.flush()
            # 若该 registry 是某 pool 的 legacy_instance_id: 该 pool 失去 legacy 实例。
            # 按 ADR-0050 D15 非空 enabled pool 必须有 legacy 成员; 这里 registry 正被删,
            # pool 必然变为空 (singleton) 或需新 legacy (多成员, 不在本路径处理)。
            # 把 pool enabled=false 并清 legacy_instance_id 满足 CHECK 约束。
            legacy_pools = await self.db.execute(
                select(MLBackendServicePool).where(
                    MLBackendServicePool.legacy_instance_id == registry_id
                )
            )
            for lp in legacy_pools.scalars():
                lp.enabled = False
                lp.legacy_instance_id = None
                lp.routing_generation += 1
            await self.db.flush()
        await self.db.delete(row)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            _raise_managed_mutation_for_integrity(exc)
        return True

    # ── 项目启用关联 (v0.23.3 ADR-0050 · 经服务池层) ─────────────────────────
    # 项目绑定改为 pool 维度: project_ml_backend_pool.pool_id → service pool。
    # off mode 下每 pool 是 singleton, legacy_instance_id 指向唯一 registry 实例,
    # 行为与 v0.23.2 一致。调用方 (路由 / worker) 仍按 registry id 调本组方法:
    # 内部 _pool_for_registry 把 registry id 解析到其所属 singleton pool。
    # router (P3) 接线后, 推理路径改走 router.acquire(pool_id); 本组方法继续服务
    # lifecycle / 项目设置勾选清单 (这些是实例级, 不经 router)。

    async def _pool_for_registry(
        self, registry_id: uuid.UUID
    ) -> MLBackendServicePool | None:
        """registry id → 其所属 service pool (singleton backfill 后每 registry 恰一 pool)。"""
        result = await self.db.execute(
            select(MLBackendServicePool)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.pool_id == MLBackendServicePool.id,
            )
            .where(MLBackendPoolMember.registry_id == registry_id)
        )
        return result.scalars().first()

    async def pool_id_for_registry(self, registry_id: uuid.UUID) -> uuid.UUID | None:
        """Resolve the singleton pool id owning a registry instance.

        Public accessor for call sites that carry a registry id (off/observe dispatch)
        and need to record the requested pool id on Prediction / FailedPrediction /
        AsyncJob results (ADR-0050 §5.4 dual-ID). Returns None if the registry has no
        pool (e.g. pre-backfill, or lifecycle-only instances).
        """
        pool = await self._pool_for_registry(registry_id)
        return pool.id if pool is not None else None

    async def list_enabled_for_project(
        self, project_id: uuid.UUID
    ) -> list[MLBackendRegistry]:
        """该项目已启用的全局 backend (registry 行)。预标 / DAG 下游 / 门控读此集合。

        v0.23.3: 读 project_ml_backend_pool.enabled → 经 pool member 解析回 registry
        实例 (off mode singleton, 每启用 pool 恰一 registry)。"""
        result = await self.db.execute(
            select(MLBackendRegistry)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.registry_id == MLBackendRegistry.id,
            )
            .join(
                ProjectMLBackendPool,
                ProjectMLBackendPool.pool_id == MLBackendPoolMember.pool_id,
            )
            .where(
                ProjectMLBackendPool.project_id == project_id,
                ProjectMLBackendPool.enabled.is_(True),
            )
            .order_by(MLBackendRegistry.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_available_for_project(
        self, project_id: uuid.UUID
    ) -> list[tuple[MLBackendRegistry, ProjectMLBackendPool | None]]:
        """全部全局 backend + 本项目关联 (None=未建关联即未启用)。项目设置勾选清单读此。

        v0.23.3: registry 行经其 singleton pool 关联回项目 (LEFT JOIN 保证未启用 /
        从未关联过的全局项也出现在清单里, 供勾选启用)。"""
        result = await self.db.execute(
            select(MLBackendRegistry, ProjectMLBackendPool)
            .outerjoin(
                MLBackendPoolMember,
                MLBackendPoolMember.registry_id == MLBackendRegistry.id,
            )
            .outerjoin(
                ProjectMLBackendPool,
                (ProjectMLBackendPool.pool_id == MLBackendPoolMember.pool_id)
                & (ProjectMLBackendPool.project_id == project_id),
            )
            .order_by(MLBackendRegistry.created_at.desc())
        )
        return [(reg, assoc) for reg, assoc in result.all()]

    async def list_pools_available_for_project(
        self, project_id: uuid.UUID
    ) -> list[tuple[MLBackendServicePool, int, ProjectMLBackendPool | None]]:
        """全部服务池 + 成员数 + 本项目启用关联 (None=未建关联即未启用)。

        v0.23.3 ADR-0050 §12.2 · 项目服务池可用清单。off/observe 下每池是 singleton;
        完整池管理 UI 留给 v0.23.4。LEFT JOIN 保证未启用 / 从未关联过的池也出现。
        """
        # 成员计数子查询
        member_count_subq = (
            select(
                MLBackendPoolMember.pool_id,
                func.count(MLBackendPoolMember.id).label("n"),
            )
            .group_by(MLBackendPoolMember.pool_id)
            .subquery()
        )
        result = await self.db.execute(
            select(MLBackendServicePool, member_count_subq.c.n, ProjectMLBackendPool)
            .outerjoin(
                member_count_subq,
                member_count_subq.c.pool_id == MLBackendServicePool.id,
            )
            .outerjoin(
                ProjectMLBackendPool,
                (ProjectMLBackendPool.pool_id == MLBackendServicePool.id)
                & (ProjectMLBackendPool.project_id == project_id),
            )
            .order_by(MLBackendServicePool.created_at.desc())
        )
        return [(pool, int(count or 0), assoc) for pool, count, assoc in result.all()]

    async def set_pool_enabled(
        self,
        project_id: uuid.UUID,
        pool_id: uuid.UUID,
        enabled: bool,
        **overrides,
    ) -> ProjectMLBackendPool:
        """切换项目对某服务池的启用 + 写项目级变体覆盖 (pool 级, ADR-0050 §12.2)。

        与 set_enabled (registry-level) 对称, 但直接操作 pool id。pool 必须存在。
        """
        pool = await self.db.get(MLBackendServicePool, pool_id)
        if pool is None:
            raise ValueError(f"service pool {pool_id} not found")
        result = await self.db.execute(
            select(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project_id,
                ProjectMLBackendPool.pool_id == pool_id,
            )
        )
        assoc = result.scalar_one_or_none()
        if assoc is None:
            assoc = ProjectMLBackendPool(
                id=uuid.uuid4(),
                project_id=project_id,
                pool_id=pool_id,
                enabled=enabled,
            )
            self.db.add(assoc)
        else:
            assoc.enabled = enabled
        if enabled and not pool.enabled:
            # A freshly registered singleton starts globally disabled. The first
            # project enablement must make the logical route reachable; otherwise
            # off/observe dispatch is rejected despite the project association.
            pool.enabled = True
            pool.routing_generation += 1
        for key in ("default_variants",):
            if key in overrides:
                setattr(assoc, key, overrides[key])
        await self.db.flush()
        return assoc

    async def get_assoc(
        self, project_id: uuid.UUID, registry_id: uuid.UUID
    ) -> ProjectMLBackendPool | None:
        """项目 × registry 的启用关联行 (经 registry 的 singleton pool 解析)。"""
        pool = await self._pool_for_registry(registry_id)
        if pool is None:
            return None
        result = await self.db.execute(
            select(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project_id,
                ProjectMLBackendPool.pool_id == pool.id,
            )
        )
        return result.scalar_one_or_none()

    async def is_enabled(self, project_id: uuid.UUID, registry_id: uuid.UUID) -> bool:
        assoc = await self.get_assoc(project_id, registry_id)
        return bool(assoc and assoc.enabled)

    async def set_enabled(
        self,
        project_id: uuid.UUID,
        registry_id: uuid.UUID,
        enabled: bool,
        *,
        nowait: bool = False,
        **overrides,
    ) -> ProjectMLBackendPool:
        """切换项目启用 + 写项目级变体覆盖 (default_variants, pool 级)。

        v0.23.3: registry_id 经 _pool_for_registry 解析到 singleton pool,
        关联行写 pool_id。registry 必须 singleton-backfill 过 (有 pool)。"""
        pool = await self._pool_for_registry(registry_id)
        if pool is None:
            raise ValueError(
                f"registry {registry_id} has no service pool; "
                "singleton backfill (alembic 0132) must run first"
            )
        pool = await self._get_pool_for_update(pool.id, nowait=nowait)
        assert pool is not None
        result = await self.db.execute(
            select(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project_id,
                ProjectMLBackendPool.pool_id == pool.id,
            )
        )
        assoc = result.scalar_one_or_none()
        if assoc is None:
            assoc = ProjectMLBackendPool(
                id=uuid.uuid4(),
                project_id=project_id,
                pool_id=pool.id,
                enabled=enabled,
            )
            self.db.add(assoc)
        else:
            assoc.enabled = enabled
        if enabled and not pool.enabled:
            # registry 兼容端点仍是项目添加 backend 的主入口。新建 singleton pool
            # 初始为 disabled；首次项目启用必须同时激活逻辑池，否则 router 在所有
            # mode 下都会以 POOL_NOT_ENABLED 拒绝真实推理。
            pool.enabled = True
            pool.routing_generation += 1
        for key in ("default_variants",):
            if key in overrides:
                setattr(assoc, key, overrides[key])
        await self.db.flush()
        return assoc

    # ── client 操作 (按 registry id) ─────────────────────────────────────────
    async def unload(self, registry_id: uuid.UUID) -> dict | None:
        backend = await self.get(registry_id)
        if not backend:
            return None
        # 远程调用前释放只读事务连接，避免 shadow 短会话与请求会话互相挤占池。
        await self.db.commit()
        return await MLBackendClient(
            backend,
            shadow_session_factory=self.shadow_session_factory,
            dispatch_context_factory=self.dispatch_context_factory,
        ).unload()

    async def reload(
        self,
        registry_id: uuid.UUID,
        sam_variant: str | None = None,
        dino_variant: str | None = None,
        modality: str | None = None,
        task_type: str | None = None,
    ) -> dict | None:
        backend = await self.get(registry_id)
        if not backend:
            return None
        await self.db.commit()
        return await MLBackendClient(
            backend,
            shadow_session_factory=self.shadow_session_factory,
            dispatch_context_factory=self.dispatch_context_factory,
        ).reload(
            sam_variant=sam_variant,
            dino_variant=dino_variant,
            modality=modality,
            task_type=task_type,
        )

    async def warmup(self, registry_id: uuid.UUID, body: dict) -> dict | None:
        """协议 §4.4 · 转发 /warmup. body 原样上抛 backend, 各 backend schema 不同."""
        backend = await self.get(registry_id)
        if not backend:
            return None
        await self.db.commit()
        return await MLBackendClient(
            backend,
            shadow_session_factory=self.shadow_session_factory,
            dispatch_context_factory=self.dispatch_context_factory,
        ).warmup(body)

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
        backend = await self.get(registry_id)
        if not backend:
            return False
        requested_url = backend.url
        requested_auth_method = backend.auth_method
        requested_auth_token = backend.auth_token
        requested_auth_revision = backend.auth_revision
        requested_gpu_resource_id = backend.gpu_resource_id
        requested_membership_epoch: int | None = None
        requested_membership_state: str | None = None
        probe_started_at: datetime | None = None
        if requested_gpu_resource_id is not None:
            requested_membership = (
                await self.db.execute(
                    select(
                        GPUBackendMembership.membership_epoch,
                        GPUBackendMembership.state,
                    ).where(
                        GPUBackendMembership.backend_registry_id == registry_id,
                        GPUBackendMembership.gpu_resource_id
                        == requested_gpu_resource_id,
                        GPUBackendMembership.state.in_(("pending", "active")),
                    )
                )
            ).one_or_none()
            if requested_membership is None:
                return False
            requested_membership_epoch = requested_membership.membership_epoch
            requested_membership_state = requested_membership.state
            gpu_health_challenge = gpu_health_challenge or secrets.token_hex(32)
            probe_started_at = await self.db.scalar(select(func.clock_timestamp()))
            if probe_started_at is None:
                return False
        elif gpu_health_challenge is not None:
            raise ValueError("gpu_health_challenge requires a GPU backend membership")
        client = MLBackendClient(backend)
        # v0.9.6 · 用 health_meta 一次性拉 ok + meta, 把深度指标缓存到全局注册行
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
        # A failed or metadata-free probe invalidates the previous device/identity
        # snapshot.  Keeping it would let stale CPU/UUID evidence pass GPU diagnostics.
        next_health_meta = None
        next_is_interactive: bool | None = None
        caps: dict | None = None
        if healthy and meta is not None:
            # v0.10.37 · 顺带探 /setup, 把能力快照落进 health_meta["capabilities"]。
            from app.services.ml_capabilities import extract_capabilities

            try:
                setup_payload = await client.setup()
                caps = extract_capabilities(setup_payload)
            except Exception:
                caps = None
            if (
                caps is not None
                and setup_payload.get("model_metadata_endpoint") is True
            ):
                metadata = await client.model_metadata()
                if metadata is not None:
                    meta = {**meta, "model_metadata": metadata}

        # GPU proof binds the complete /health + /setup observation window.  A
        # timestamp captured before setup could authorize a capability that was
        # never observed within the challenge's evidence interval.
        observed_at = await self.db.scalar(select(func.clock_timestamp()))
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
                # is_interactive 改派生对账: 以 /setup 自报为真值
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

        values: dict = {
            "state": "connected" if healthy else "error",
            "last_checked_at": observed_at,
            "health_meta": next_health_meta,
        }
        if next_is_interactive is not None:
            values["is_interactive"] = next_is_interactive
        current_backend = await self.db.scalar(
            select(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current_backend is None or (
            current_backend.url != requested_url
            or current_backend.auth_method != requested_auth_method
            or current_backend.auth_token != requested_auth_token
            or current_backend.auth_revision != requested_auth_revision
            or current_backend.gpu_resource_id != requested_gpu_resource_id
        ):
            return False
        if requested_membership_epoch is not None:
            # Keep registry -> resource -> try-global -> membership ordering.
            # Promotion uses the same resource -> try-global prefix; neither side
            # waits on the global barrier while holding a card lock.
            await self.db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
                ),
                {"resource_id": requested_gpu_resource_id},
            )
            promotion_barrier_acquired = await self.db.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended('aap:gpu-membership-promotion', 0))"
                )
            )
            if promotion_barrier_acquired is not True:
                return False
        # A slow /setup must not let an older /health response borrow a later
        # completion timestamp and overwrite a concurrently committed snapshot.
        # For GPU probes, first-committer-wins across overlapping observation
        # windows; a later periodic probe will refresh any conservatively dropped
        # evidence.  Non-GPU checks retain monotonic completion ordering.
        commit_order_floor = probe_started_at or observed_at
        if current_backend.last_checked_at is not None and (
            current_backend.last_checked_at.tzinfo is None
            or current_backend.last_checked_at.utcoffset() is None
            or current_backend.last_checked_at >= commit_order_floor
        ):
            return False
        if requested_membership_epoch is not None:
            current_membership = (
                await self.db.execute(
                    select(
                        GPUBackendMembership.membership_epoch,
                        GPUBackendMembership.state,
                    )
                    .where(
                        GPUBackendMembership.backend_registry_id == registry_id,
                        GPUBackendMembership.gpu_resource_id
                        == requested_gpu_resource_id,
                        GPUBackendMembership.state.in_(("pending", "active")),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if (
                current_membership is None
                or current_membership.membership_epoch != requested_membership_epoch
                or current_membership.state != requested_membership_state
            ):
                return False
        result = await self.db.execute(
            update(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        # The post-probe row lock and membership recheck form the commit-time fence
        # without holding a DB lock across the backend network call.
        if result.rowcount != 1:
            await self.db.refresh(backend)
            return False
        await self.db.refresh(backend)
        if healthy and caps is not None:
            await self._reconcile_pool_capability(registry_id, caps)
            await self.db.flush()
        return healthy

    async def get_interactive_backend(
        self, project_id: uuid.UUID
    ) -> MLBackendRegistry | None:
        """该项目已启用且 is_interactive 的 connected backend。

        v0.23.3: 经 project_ml_backend_pool → pool member 解析回 registry 实例。"""
        result = await self.db.execute(
            select(MLBackendRegistry)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.registry_id == MLBackendRegistry.id,
            )
            .join(
                ProjectMLBackendPool,
                ProjectMLBackendPool.pool_id == MLBackendPoolMember.pool_id,
            )
            .where(
                ProjectMLBackendPool.project_id == project_id,
                ProjectMLBackendPool.enabled.is_(True),
                MLBackendRegistry.is_interactive.is_(True),
                MLBackendRegistry.state == "connected",
            )
        )
        return result.scalars().first()

    async def get_project_backend(
        self, project_id: uuid.UUID
    ) -> MLBackendRegistry | None:
        """优先返回 project.ml_backend_pool_id 显式绑定(且项目已启用)，否则 fallback 交互式。

        v0.23.3: 项目主绑定存 pool id; 经 _pool_for_registry 解析 pool 的 legacy
        instance (off mode singleton, 行为与 v0.23.2 一致)。"""
        proj = await self.db.get(Project, project_id)
        if proj is not None and proj.ml_backend_pool_id is not None:
            pool = await self.db.get(MLBackendServicePool, proj.ml_backend_pool_id)
            if pool is not None and pool.legacy_instance_id is not None:
                if await self.is_enabled(project_id, pool.legacy_instance_id):
                    backend = await self.get(pool.legacy_instance_id)
                    if backend is not None:
                        return backend
        return await self.get_interactive_backend(project_id)

    async def get_tracker_backend(
        self, project_id: uuid.UUID, model_key: str
    ) -> MLBackendRegistry | None:
        return await self.get_tracker_backend_for_capabilities(project_id, [model_key])

    async def get_tracker_backend_for_capabilities(
        self, project_id: uuid.UUID, model_keys: list[str]
    ) -> MLBackendRegistry | None:
        """选择同时支持全部 tracker 能力的已连接项目后端。

        绑定优先（项目显式绑定的 backend 若支持全部 tracker 则用它），否则取首个 connected 的。
        与 `get_project_backend` 的区别：后者只按「单一绑定 / 交互 fallback」选、不看 tracker——
        一项目同时启用多个 tracker-capable backend 时（如 grounded-sam2[sam2_video] +
        sam3-backend[sam3_video]），会把所有 tracker（含 sam3_video）都发给绑定那个，导致
        sam3_video 静默落到 sam2。本方法按 `health_meta.capabilities.supported_trackers`
        路由，消除该错配。

        返回 None：没有已启用且 connected 的 backend 同时声明全部能力。组合追踪因此不会把
        发现与传播错误拼到两个不同后端，也不会把任务排队后才因断连失败。
        """

        required = set(model_keys)
        if not required:
            return None

        def _supports(backend: MLBackendRegistry) -> bool:
            caps = (backend.health_meta or {}).get("capabilities") or {}
            return required.issubset(set(caps.get("supported_trackers") or []))

        supporting = [
            b
            for b in await self.list_enabled_for_project(project_id)
            if b.state == "connected" and _supports(b)
        ]
        if not supporting:
            return None
        proj = await self.db.get(Project, project_id)
        # v0.23.3: 项目主绑定存 pool id; 经 pool 的 legacy instance 解析回 registry。
        bound_id: uuid.UUID | None = None
        if proj is not None and proj.ml_backend_pool_id is not None:
            pool = await self.db.get(MLBackendServicePool, proj.ml_backend_pool_id)
            bound_id = pool.legacy_instance_id if pool is not None else None
        if bound_id is not None:
            for b in supporting:
                if b.id == bound_id:
                    return b
        return supporting[0]
