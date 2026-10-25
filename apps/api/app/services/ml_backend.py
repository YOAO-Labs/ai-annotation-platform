from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.services.project_access import get_workspace_project
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.policy import validate_gpu_claim
from app.services.ml_client import MLBackendClient
from aap_inference.topology import RegistryDeletion, RegistryTopologySnapshot


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

_REGISTRY_EVALUATION_REFERENCE = text(
    "SELECT aap_learning_v1.registry_evaluation_reference_exists(CAST(:registry_id AS uuid))"
)
_RUNNING_REGISTRY_PREDICTIONS = text(
    "SELECT aap_jobs_v1.count_running_registry_predictions(CAST(:registry_id AS uuid))"
)


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
            from aap_inference.pool_capability import (
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
        self, pool_id: uuid.UUID
    ) -> MLBackendServicePool | None:
        return await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
        from app.services.workspace_backend_bindings import (
            clear_primary_backend_binding_for_pool,
        )

        await clear_primary_backend_binding_for_pool(self.db, pool_id)
        await self.db.delete(pool)
        await self.db.flush()
        return True

    async def _registry_for_pool_change(self, registry_id: uuid.UUID):
        return await self.db.scalar(
            select(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )

    async def _lock_registry_reference(self, registry_id: uuid.UUID):
        return await self.db.scalar(
            select(MLBackendRegistry.id)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update(read=True, key_share=True)
        )

    @staticmethod
    def _check_topology(expected, registry_id, member, pool):
        if expected is None:
            return
        current = RegistryTopologySnapshot(
            registry_id=registry_id,
            member_id=member.id if member is not None else None,
            pool_id=member.pool_id if member is not None else None,
            routing_generation=pool.routing_generation
            if member is not None and pool is not None
            else None,
            traffic_state=member.traffic_state if member is not None else None,
        )
        if current != expected:
            from app.services.ml_routing.contracts import MLBackendQuiescenceError

            raise MLBackendQuiescenceError(
                "ml_backend_topology_changed",
                "service-pool topology changed after quiescence check; drain again and retry",
            )

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
        from aap_inference.pool_capability import (
            canonicalize_capability,
            capability_fingerprint,
            diff_capabilities,
        )
        from app.services.ml_routing.contracts import CapabilityMismatchError

        registry = await self._registry_for_pool_change(registry_id)
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            raise ValueError(f"pool {pool_id} not found")
        if registry is None:
            raise ValueError(f"registry {registry_id} not found")
        owned_member = await self.db.scalar(
            select(MLBackendPoolMember)
            .where(MLBackendPoolMember.registry_id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
        from aap_inference.health import reconcile_pool_capability
        from app.services.inference_health_data import InferenceHealthData

        await reconcile_pool_capability(
            InferenceHealthData(self.db),
            registry_id,
            capabilities,
        )

    @staticmethod
    def _registry_capability_snapshot(
        registry: MLBackendRegistry,
    ) -> tuple[dict | None, str | None]:
        from aap_inference.pool_capability import (
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
        from aap_inference.pool_capability import diff_capabilities

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
        from aap_inference.pool_capability import diff_capabilities

        target_registry = await self._registry_for_pool_change(registry_id)
        pool = await self._get_pool_for_update(pool_id)
        if pool is None or target_registry is None:
            return None
        result = await self.db.execute(
            select(MLBackendPoolMember, MLBackendRegistry)
            .join(
                MLBackendRegistry,
                MLBackendRegistry.id == MLBackendPoolMember.registry_id,
            )
            .where(MLBackendPoolMember.pool_id == pool_id)
            .with_for_update(of=MLBackendPoolMember)
            .execution_options(populate_existing=True)
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
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        expected_topology: RegistryTopologySnapshot | None = None,
    ) -> bool:
        """Remove a member. If it's the legacy_instance_id, clear that pointer + disable
        pool (D5/D15: non-empty enabled pool must have legacy member)."""
        if await self._lock_registry_reference(registry_id) is None:
            self._check_topology(expected_topology, registry_id, None, None)
            return False
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            self._check_topology(expected_topology, registry_id, None, None)
            return False
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        member = result.scalar_one_or_none()
        if member is None:
            self._check_topology(expected_topology, registry_id, None, None)
            return False
        self._check_topology(expected_topology, registry_id, member, pool)
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
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            return None
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        member = result.scalar_one_or_none()
        if member is None:
            return None
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
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            return None
        result = await self.db.execute(
            select(MLBackendPoolMember)
            .where(
                MLBackendPoolMember.pool_id == pool_id,
                MLBackendPoolMember.registry_id == registry_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
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
        self, registry_id: uuid.UUID, **kwargs
    ) -> MLBackendRegistry | None:
        row = await self.get(registry_id)
        if not row:
            return None
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

        protected_values = {
            "url": row.url,
            "auth_method": row.auth_method,
            "auth_token": row.auth_token,
            "extra_params": row.extra_params,
            "gpu_resource_id": row.gpu_resource_id,
            "vram_budget_mb": row.vram_budget_mb,
            "eviction_priority": row.eviction_priority,
        }
        protected_change = any(
            field in kwargs and kwargs[field] != previous
            for field, previous in protected_values.items()
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
        if protected_change:
            # Any membership/config epoch change invalidates the cached residency
            # evidence, even when the endpoint and physical resource stay the same.
            row.state = "disconnected"
            row.health_meta = None
            row.last_checked_at = None
            row.error_message = None
        try:
            await self.db.flush()
        except IntegrityError as exc:
            _raise_managed_mutation_for_integrity(exc)
        return row

    async def _count_running_predictions(self, registry_id: uuid.UUID) -> int:
        """Legacy deletion guard only; never an allocation or active-lease truth."""
        if self.db.autoflush:
            await self.db.flush()
        running = await self.db.scalar(
            _RUNNING_REGISTRY_PREDICTIONS, {"registry_id": registry_id}
        )
        return int(running)

    async def validate_delete(self, registry_id: uuid.UUID) -> bool:
        """Run non-mutating legacy job and managed-GPU deletion guards."""
        if await self.get(registry_id) is None:
            return False
        if await self.db.scalar(
            _REGISTRY_EVALUATION_REFERENCE, {"registry_id": registry_id}
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
        return (await self.delete_with_topology(registry_id)).deleted

    async def delete_with_topology(
        self,
        registry_id: uuid.UUID,
        *,
        expected_topology: RegistryTopologySnapshot | None = None,
    ) -> RegistryDeletion:
        row = await self.db.scalar(
            select(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            self._check_topology(expected_topology, registry_id, None, None)
            return RegistryDeletion(False)
        await self.validate_delete(registry_id)
        pool_id = await self.db.scalar(
            select(MLBackendPoolMember.pool_id).where(
                MLBackendPoolMember.registry_id == registry_id
            )
        )
        pools = list(
            (
                await self.db.execute(
                    select(MLBackendServicePool)
                    .where(
                        or_(
                            MLBackendServicePool.id == pool_id,
                            MLBackendServicePool.legacy_instance_id == registry_id,
                        )
                    )
                    .order_by(MLBackendServicePool.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        member = await self.db.scalar(
            select(MLBackendPoolMember)
            .where(MLBackendPoolMember.registry_id == registry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        owning_pool = next((pool for pool in pools if pool.id == pool_id), None)
        self._check_topology(expected_topology, registry_id, member, owning_pool)
        if member is not None and member.pool_id != pool_id:
            from app.services.ml_routing.contracts import MLBackendQuiescenceError

            raise MLBackendQuiescenceError(
                "ml_backend_topology_changed",
                "service-pool membership changed during deletion",
            )
        if member is not None:
            await self.db.delete(member)
            await self.db.flush()
        for pool in pools:
            if pool.legacy_instance_id == registry_id:
                pool.enabled = False
                pool.legacy_instance_id = None
                pool.routing_generation += 1
            elif member is not None and pool.id == pool_id:
                pool.routing_generation += 1
        if pools:
            await self.db.flush()
        await self.db.delete(row)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            _raise_managed_mutation_for_integrity(exc)
        return RegistryDeletion(True, pool_id)

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
        from app.services.workspace_backend_bindings import (
            lock_backend_project_reference,
        )

        await lock_backend_project_reference(self.db, project_id)
        pool = await self._get_pool_for_update(pool_id)
        if pool is None:
            raise ValueError(f"service pool {pool_id} not found")
        if enabled and (not pool.enabled or pool.legacy_instance_id is None):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "ml_backend_pool_not_enabled",
                    "message": "pool is disabled or has no legacy instance; cannot enable for project",
                },
            )
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
        **overrides,
    ) -> ProjectMLBackendPool:
        """切换项目启用 + 写项目级变体覆盖 (default_variants, pool 级)。

        v0.23.3: registry_id 经 _pool_for_registry 解析到 singleton pool,
        关联行写 pool_id。registry 必须 singleton-backfill 过 (有 pool)。"""
        from app.services.workspace_backend_bindings import (
            lock_backend_project_reference,
        )

        await lock_backend_project_reference(self.db, project_id)
        registry = await self._registry_for_pool_change(registry_id)
        pool = await self._pool_for_registry(registry_id)
        if registry is None or pool is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "ml_backend_topology_changed",
                    "message": "backend or service-pool membership is no longer available",
                },
            )
        pool = await self._get_pool_for_update(pool.id)
        current_pool_id = await self.pool_id_for_registry(registry_id)
        if pool is None or current_pool_id != pool.id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "ml_backend_topology_changed",
                    "message": "service-pool membership changed while enabling the backend",
                },
            )
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
    async def unload(
        self, registry_id: uuid.UUID, *, expected_topology=None
    ) -> dict | None:
        backend = await self.get(registry_id)
        if not backend:
            return None
        from app.services.ml_routing.authority import lifecycle_control

        async with lifecycle_control(
            self.db, registry_id, "unload", expected_topology=expected_topology
        ) as authority:
            # Keep the existing pre-I/O release of the caller connection.
            await self.db.commit()
            return await MLBackendClient(
                backend,
                shadow_session_factory=self.shadow_session_factory,
                dispatch_context_factory=self.dispatch_context_factory,
                routing_authority=authority,
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
        from app.services.ml_routing.authority import lifecycle_control

        async with lifecycle_control(self.db, registry_id, "reload") as authority:
            await self.db.commit()
            return await MLBackendClient(
                backend,
                shadow_session_factory=self.shadow_session_factory,
                dispatch_context_factory=self.dispatch_context_factory,
                routing_authority=authority,
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
        from aap_inference.health import BackendHealthCoordinator
        from app.services.inference_health_data import InferenceHealthData
        from app.services.inference_health_observation import (
            health_observation_client_factory,
        )
        from app.services.inference_limits import capability_limits_from_settings

        return await BackendHealthCoordinator(
            InferenceHealthData(self.db),
            client_factory=health_observation_client_factory(
                self.db, client_factory=MLBackendClient
            ),
            capability_limits_factory=capability_limits_from_settings,
        ).check_health(registry_id, gpu_health_challenge=gpu_health_challenge)

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
        proj = await get_workspace_project(self.db, project_id=project_id)
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
        proj = await get_workspace_project(self.db, project_id=project_id)
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
