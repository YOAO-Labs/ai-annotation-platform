"""Local topology observations used when rechecking a borrowed write boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4
from aap_inference_contract.topology import (
    CapabilityDriftAcceptance,
    CapabilityDriftPreview,
    PoolFacts,
    PoolMemberFacts,
    ProjectAssociationFacts,
    ProjectBackendFacts,
    ProjectPoolFacts,
    RegistryName,
    RegistryPublicFacts,
    RegistryReference,
)


RepairModelLockStatus = Literal["ready", "backend_unavailable", "model_changed"]


@dataclass(frozen=True)
class RegistryTopologySnapshot:
    """An earlier DB observation, not a network credential or a durable lease proof."""

    registry_id: UUID
    member_id: UUID | None
    pool_id: UUID | None
    routing_generation: int | None
    traffic_state: str | None


@dataclass(frozen=True)
class RegistryDeletion:
    deleted: bool
    pool_id: UUID | None = None


class RegistryURLConflict(Exception):
    def __init__(self, backend_name: str) -> None:
        super().__init__(f"ML backend URL already registered by {backend_name}")
        self.backend_name = backend_name


class PoolMemberConflict(Exception):
    def __init__(self, pool_id: UUID) -> None:
        super().__init__(f"registry already belongs to service pool {pool_id}")
        self.pool_id = pool_id


class PoolCapabilityUnavailable(Exception):
    pass


class PoolCapabilityMismatch(Exception):
    def __init__(self, mismatch: Any) -> None:
        self.mismatch = mismatch
        super().__init__(str(mismatch))


class TopologyChanged(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class CapabilityRevalidationError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        blocking_member_ids: tuple[UUID, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.blocking_member_ids = blocking_member_ids


class ManagedMutationBlocked(Exception):
    pass


class RegistryDeleteBlocked(Exception):
    def __init__(self, running_jobs: int) -> None:
        super().__init__(f"ml backend has {running_jobs} running prediction job(s)")
        self.running_jobs = running_jobs


class BackendTopologyData(Protocol):
    """Closed operation set over the caller-owned topology transaction."""

    async def flush(self) -> None: ...
    async def lock_project_reference(self, project_id: UUID) -> None: ...
    async def registry_facts(self, registry_id: UUID) -> RegistryPublicFacts | None: ...
    async def registry_url_owner(self, url: str) -> RegistryName | None: ...
    async def registry_catalog(self) -> list[RegistryPublicFacts]: ...
    async def registry_names(self, registry_ids: Sequence[UUID]) -> dict[UUID, str]: ...
    async def registry_reference(
        self, registry_id: UUID
    ) -> RegistryReference | None: ...
    async def pool_reference(self, pool_id: UUID) -> PoolFacts | None: ...
    async def pool_for_registry(self, registry_id: UUID) -> PoolFacts | None: ...
    async def pool_catalog(self) -> list[PoolFacts]: ...
    async def pool_members(self, pool_id: UUID) -> list[PoolMemberFacts]: ...
    async def project_backend_catalog(
        self, project_id: UUID
    ) -> list[ProjectBackendFacts]: ...
    async def project_backend_facts(
        self, project_ids: Sequence[UUID]
    ) -> list[ProjectBackendFacts]: ...
    async def enabled_project_ids(self, project_ids: Sequence[UUID]) -> list[UUID]: ...
    async def project_pool_catalog(
        self, project_id: UUID
    ) -> list[ProjectPoolFacts]: ...
    async def project_assoc_for_registry(
        self, project_id: UUID, registry_id: UUID
    ) -> ProjectAssociationFacts | None: ...
    async def project_interactive_backend(
        self, project_id: UUID
    ) -> RegistryPublicFacts | None: ...
    async def create_manual_registry(
        self, registry_id: UUID, values: Mapping[str, Any]
    ) -> RegistryPublicFacts: ...
    async def singleton_pool(self, registry_id: UUID) -> PoolFacts | None: ...
    async def create_singleton_pool(
        self,
        registry_id: UUID,
        pool_id: UUID,
        name: str,
        snapshot: Mapping[str, Any] | None,
        fingerprint: str | None,
    ) -> PoolFacts: ...
    async def create_singleton_member(
        self, registry_id: UUID, pool_id: UUID, member_id: UUID
    ) -> PoolMemberFacts: ...
    async def patch_registry(
        self, registry_id: UUID, patch: Mapping[str, Any]
    ) -> RegistryPublicFacts | None: ...
    async def validate_registry_delete(self, registry_id: UUID) -> tuple[bool, int]: ...
    async def delete_registry(
        self, registry_id: UUID, expected: RegistryTopologySnapshot | None
    ) -> RegistryDeletion: ...
    async def create_pool(self, pool_id: UUID, name: str) -> PoolFacts: ...
    async def patch_pool(
        self, pool_id: UUID, patch: Mapping[str, Any]
    ) -> PoolFacts | None: ...
    async def delete_empty_pool(self, pool_id: UUID) -> bool: ...
    async def lock_member_upsert_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[
        PoolFacts | None, RegistryPublicFacts | None, PoolMemberFacts | None
    ]: ...
    async def lock_repair_model_rows(
        self, project_id: UUID, pool_id: UUID, registry_ids: Sequence[UUID]
    ) -> RepairModelLockStatus: ...
    async def upsert_member(
        self, pool_id: UUID, registry_id: UUID, weight: int, expected: Mapping[str, Any]
    ) -> tuple[PoolMemberFacts, PoolFacts]: ...
    async def remove_member(
        self,
        pool_id: UUID,
        registry_id: UUID,
        expected: RegistryTopologySnapshot | None,
    ) -> bool: ...
    async def drain_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None: ...
    async def resume_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None: ...
    async def drift_review_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, list[PoolMemberFacts]]: ...
    async def lock_drift_acceptance_rows(
        self, pool_id: UUID, registry_id: UUID
    ) -> tuple[PoolFacts | None, list[PoolMemberFacts]]: ...
    async def accept_drift(
        self,
        pool_id: UUID,
        registry_id: UUID,
        expected_candidate_fingerprint: str,
        enable_pool: bool,
        expected: Mapping[str, Any],
    ) -> PoolFacts | None: ...
    async def set_project_registry_enablement(
        self,
        project_id: UUID,
        registry_id: UUID,
        enabled: bool,
        overrides: Mapping[str, Any],
    ) -> ProjectAssociationFacts: ...
    async def set_project_pool_enablement(
        self,
        project_id: UUID,
        pool_id: UUID,
        enabled: bool,
        overrides: Mapping[str, Any],
    ) -> ProjectAssociationFacts: ...
    async def delete_project_associations(self, project_id: UUID) -> int: ...


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_mutable_json(item) for item in value]
    return value


def _registry_capability(
    registry: RegistryPublicFacts,
) -> tuple[dict[str, Any] | None, str | None]:
    from .pool_capability import canonicalize_capability, capability_fingerprint

    health_meta = _mutable_json(registry.health_meta or {})
    raw = health_meta.get("capabilities")
    if not isinstance(raw, Mapping):
        return None, None
    snapshot = canonicalize_capability(raw)
    return snapshot, capability_fingerprint(snapshot)


def _blocking_members(
    rows: list[PoolMemberFacts],
    *,
    target_registry_id: UUID,
    candidate_fingerprint: str,
) -> tuple[UUID, ...]:
    blocking = []
    for row in rows:
        if row.registry_id == target_registry_id or row.traffic_state == "disabled":
            continue
        _, fingerprint = _registry_capability(row.registry)
        if fingerprint != candidate_fingerprint:
            blocking.append(row.registry_id)
    return tuple(sorted(blocking, key=str))


class BackendTopologyCoordinator:
    """Apply manual backend topology policy over a borrowed fixed data port."""

    def __init__(self, data: BackendTopologyData) -> None:
        self.data = data

    async def create_registry(
        self, name: str, url: str, source: str = "manual", **kwargs: Any
    ) -> RegistryPublicFacts:
        mutable_fields = {
            "is_interactive",
            "auth_method",
            "auth_token",
            "extra_params",
            "gpu_resource_id",
            "vram_budget_mb",
            "eviction_priority",
        }
        unknown = set(kwargs) - mutable_fields
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")
        extra = kwargs.get("extra_params")
        if isinstance(extra, Mapping) and "plugin" in extra:
            raise ManagedMutationBlocked(
                "Plugin installation metadata is reserved for the installation owner"
            )
        if source != "manual":
            raise ValueError("manual topology creation only accepts source=manual")
        normalized_url = url.rstrip("/")
        owner = await self.data.registry_url_owner(normalized_url)
        if owner is not None:
            raise RegistryURLConflict(owner.name)
        registry_id = uuid4()
        registry_values = {
            "name": name,
            "url": normalized_url,
            "source": "manual",
            **kwargs,
        }
        await self.data.flush()
        registry = await self.data.create_manual_registry(registry_id, registry_values)
        await self.data.flush()
        snapshot, fingerprint = _registry_capability(registry)
        if await self.data.singleton_pool(registry.id) is None:
            pool = await self.data.create_singleton_pool(
                registry.id, uuid4(), registry.name, snapshot, fingerprint
            )
            await self.data.flush()
            await self.data.create_singleton_member(registry.id, pool.id, uuid4())
            await self.data.flush()
        return registry

    async def ensure_singleton_pool(self, registry_id: UUID) -> PoolFacts | None:
        registry = await self.data.registry_facts(registry_id)
        if registry is None:
            return None
        existing = await self.data.singleton_pool(registry_id)
        if existing is not None:
            return existing
        snapshot, fingerprint = _registry_capability(registry)
        pool = await self.data.create_singleton_pool(
            registry_id, uuid4(), registry.name, snapshot, fingerprint
        )
        await self.data.flush()
        await self.data.create_singleton_member(registry_id, pool.id, uuid4())
        await self.data.flush()
        return pool

    async def create_pool(
        self, name: str, *, legacy_instance_id: UUID | None = None
    ) -> PoolFacts:
        if legacy_instance_id is not None:
            raise ValueError(
                "legacy_instance_id cannot be set without membership; create the pool then add the member"
            )
        pool = await self.data.create_pool(uuid4(), name)
        await self.data.flush()
        return pool

    async def get_pool(self, pool_id: UUID) -> PoolFacts | None:
        return await self.data.pool_reference(pool_id)

    async def list_pools(self) -> list[PoolFacts]:
        return await self.data.pool_catalog()

    async def update_pool(
        self,
        pool_id: UUID,
        *,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> PoolFacts | None:
        patch = {}
        if name is not None:
            patch["name"] = name
        if enabled is not None:
            patch["enabled"] = enabled
        result = await self.data.patch_pool(pool_id, patch)
        await self.data.flush()
        return result

    async def delete_pool(self, pool_id: UUID) -> bool:
        result = await self.data.delete_empty_pool(pool_id)
        await self.data.flush()
        return result

    async def add_pool_member(
        self,
        pool_id: UUID,
        registry_id: UUID,
        *,
        weight: int = 1,
        capability_snapshot: dict[str, Any] | None = None,
    ) -> tuple[PoolMemberFacts, PoolFacts]:
        from .pool_capability import diff_capabilities

        pool, registry, member = await self.data.lock_member_upsert_rows(
            pool_id, registry_id
        )
        if pool is None:
            raise ValueError(f"pool {pool_id} not found")
        if registry is None:
            raise ValueError(f"registry {registry_id} not found")
        if member is not None and member.pool_id != pool_id:
            raise PoolMemberConflict(member.pool_id)
        current_health = _mutable_json(registry.health_meta or {})
        candidate_snapshot, candidate_fp = _registry_capability(registry)
        if not current_health.get("capabilities"):
            candidate_snapshot, candidate_fp = None, None
        if pool.capability_fingerprint is not None:
            mismatch = diff_capabilities(pool.capability_snapshot, candidate_snapshot)
            if mismatch is not None:
                raise PoolCapabilityMismatch(mismatch)
        if member is None and pool.capability_fingerprint is None:
            rows = await self.data.pool_members(pool_id)
            if rows:
                raise PoolCapabilityUnavailable(
                    "service pool capability baseline is unavailable; refresh the existing member before adding replicas"
                )
        expected = {
            "pool_generation": pool.routing_generation,
            "pool_fingerprint": pool.capability_fingerprint,
            "pool_snapshot": _mutable_json(pool.capability_snapshot),
            "registry_capabilities": current_health.get("capabilities"),
            "candidate_snapshot": candidate_snapshot,
            "candidate_fingerprint": candidate_fp,
            "member_id": str(member.id) if member is not None else None,
            "member_state": member.traffic_state if member is not None else None,
            "member_weight": member.weight if member is not None else None,
        }
        return await self.data.upsert_member(pool_id, registry_id, weight, expected)

    async def remove_pool_member(
        self,
        pool_id: UUID,
        registry_id: UUID,
        *,
        expected_topology: RegistryTopologySnapshot | None = None,
    ) -> bool:
        return await self.data.remove_member(pool_id, registry_id, expected_topology)

    async def drain_pool_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None:
        return await self.data.drain_member(pool_id, registry_id)

    async def resume_pool_member(
        self, pool_id: UUID, registry_id: UUID
    ) -> PoolMemberFacts | None:
        member = await self.data.resume_member(pool_id, registry_id)
        if member is not None and member.traffic_state == "disabled":
            raise ValueError(
                "disabled member needs capability re-validation before resume"
            )
        return member

    async def get_registry(self, registry_id: UUID) -> RegistryPublicFacts | None:
        return await self.data.registry_facts(registry_id)

    async def get_by_url(self, url: str) -> RegistryPublicFacts | None:
        owner = await self.data.registry_url_owner(url.rstrip("/"))
        return await self.data.registry_facts(owner.id) if owner is not None else None

    async def registry_catalog(self) -> list[RegistryPublicFacts]:
        return await self.data.registry_catalog()

    async def registry_names(self, registry_ids: Sequence[UUID]) -> dict[UUID, str]:
        return await self.data.registry_names(registry_ids)

    async def registry_reference(self, registry_id: UUID) -> RegistryReference | None:
        return await self.data.registry_reference(registry_id)

    async def update_registry(
        self, registry_id: UUID, **kwargs: Any
    ) -> RegistryPublicFacts | None:
        mutable_fields = {
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
        unknown = set(kwargs) - mutable_fields
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")
        row = await self.data.registry_facts(registry_id)
        if row is None:
            return None
        old_extra = row.extra_params
        new_extra = kwargs.get("extra_params", old_extra)
        if (isinstance(old_extra, Mapping) and "plugin" in old_extra) or (
            isinstance(new_extra, Mapping) and "plugin" in new_extra
        ):
            raise ManagedMutationBlocked(
                "Plugin installation changes require the installation owner"
            )
        patch = dict(kwargs)
        if "url" in patch:
            url = patch["url"]
            if not isinstance(url, str) or not url:
                raise ValueError("url must not be null or empty")
            patch["url"] = url.rstrip("/")
            owner = await self.data.registry_url_owner(patch["url"])
            if owner is not None and owner.id != row.id:
                raise RegistryURLConflict(owner.name)
        updated = await self.data.patch_registry(registry_id, patch)
        await self.data.flush()
        return updated

    async def validate_delete(self, registry_id: UUID) -> bool:
        row = await self.data.registry_facts(registry_id)
        if row is not None and isinstance(row.extra_params, Mapping) and "plugin" in row.extra_params:
            raise ManagedMutationBlocked("Plugin Registry history must be retained")
        exists, running_jobs = await self.data.validate_registry_delete(registry_id)
        if running_jobs:
            raise RegistryDeleteBlocked(running_jobs)
        return exists

    async def delete_registry(
        self,
        registry_id: UUID,
        *,
        expected_topology: RegistryTopologySnapshot | None = None,
    ) -> RegistryDeletion:
        row = await self.data.registry_facts(registry_id)
        if row is not None and isinstance(row.extra_params, Mapping) and "plugin" in row.extra_params:
            raise ManagedMutationBlocked("Plugin Registry history must be retained")
        return await self.data.delete_registry(registry_id, expected_topology)

    async def pool_id_for_registry(self, registry_id: UUID) -> UUID | None:
        pool = await self.data.pool_for_registry(registry_id)
        return pool.id if pool is not None else None

    async def list_enabled_for_project(
        self, project_id: UUID
    ) -> list[RegistryPublicFacts]:
        return [
            fact.registry
            for fact in await self.data.project_backend_catalog(project_id)
            if fact.association is not None and fact.association.enabled
        ]

    async def list_available_for_project(
        self, project_id: UUID
    ) -> list[tuple[RegistryPublicFacts, ProjectAssociationFacts | None]]:
        return [
            (fact.registry, fact.association)
            for fact in await self.data.project_backend_catalog(project_id)
        ]

    async def list_pools_available_for_project(
        self, project_id: UUID
    ) -> list[tuple[PoolFacts, int, ProjectAssociationFacts | None]]:
        return [
            (fact.pool, fact.member_count, fact.association)
            for fact in await self.data.project_pool_catalog(project_id)
        ]

    async def set_enabled(
        self,
        project_id: UUID,
        registry_id: UUID,
        enabled: bool,
        **overrides: Any,
    ) -> ProjectAssociationFacts:
        overrides = {
            key: overrides[key] for key in ("default_variants",) if key in overrides
        }
        await self.data.lock_project_reference(project_id)
        return await self.data.set_project_registry_enablement(
            project_id, registry_id, enabled, overrides
        )

    async def set_pool_enabled(
        self,
        project_id: UUID,
        pool_id: UUID,
        enabled: bool,
        **overrides: Any,
    ) -> ProjectAssociationFacts:
        overrides = {
            key: overrides[key] for key in ("default_variants",) if key in overrides
        }
        await self.data.lock_project_reference(project_id)
        return await self.data.set_project_pool_enablement(
            project_id, pool_id, enabled, overrides
        )

    async def get_assoc(
        self, project_id: UUID, registry_id: UUID
    ) -> ProjectAssociationFacts | None:
        return await self.data.project_assoc_for_registry(project_id, registry_id)

    async def is_enabled(self, project_id: UUID, registry_id: UUID) -> bool:
        assoc = await self.get_assoc(project_id, registry_id)
        return bool(assoc and assoc.enabled)

    async def get_interactive_backend(
        self, project_id: UUID
    ) -> RegistryPublicFacts | None:
        return await self.data.project_interactive_backend(project_id)

    async def get_project_backend(
        self, project_id: UUID, *, primary_pool_id: UUID | None
    ) -> RegistryPublicFacts | None:
        if primary_pool_id is not None:
            pool = await self.data.pool_reference(primary_pool_id)
            if pool is not None and pool.legacy_instance_id is not None:
                assoc = await self.data.project_assoc_for_registry(
                    project_id, pool.legacy_instance_id
                )
                if assoc is not None and assoc.enabled:
                    backend = await self.data.registry_facts(pool.legacy_instance_id)
                    if backend is not None:
                        return backend
        return await self.get_interactive_backend(project_id)

    async def get_tracker_backend_for_capabilities(
        self,
        project_id: UUID,
        model_keys: Sequence[str],
        *,
        primary_pool_id: UUID | None,
    ) -> RegistryPublicFacts | None:
        required = set(model_keys)
        if not required:
            return None

        def supports(backend: RegistryPublicFacts) -> bool:
            health_meta = _mutable_json(backend.health_meta or {})
            capabilities = health_meta.get("capabilities") or {}
            return required.issubset(set(capabilities.get("supported_trackers") or []))

        supporting = [
            backend
            for backend in await self.list_enabled_for_project(project_id)
            if backend.state == "connected" and supports(backend)
        ]
        if not supporting:
            return None
        if primary_pool_id is not None:
            pool = await self.data.pool_reference(primary_pool_id)
            bound_id = pool.legacy_instance_id if pool is not None else None
            if bound_id is not None:
                for backend in supporting:
                    if backend.id == bound_id:
                        return backend
        return supporting[0]

    async def preview_capability_drift(
        self, pool_id: UUID, registry_id: UUID
    ) -> CapabilityDriftPreview | None:
        from .pool_capability import diff_capabilities

        pool, rows = await self.data.drift_review_rows(pool_id, registry_id)
        if pool is None:
            return None
        target = next((row for row in rows if row.registry_id == registry_id), None)
        if target is None:
            return None
        candidate_snapshot, candidate_fingerprint = _registry_capability(
            target.registry
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
        blockers = (
            _blocking_members(
                rows,
                target_registry_id=registry_id,
                candidate_fingerprint=candidate_fingerprint,
            )
            if candidate_fingerprint is not None
            else ()
        )
        return CapabilityDriftPreview(
            pool_id=pool.id,
            registry_id=target.registry_id,
            member_state=target.traffic_state,
            pool_enabled=pool.enabled,
            pool_fingerprint=pool.capability_fingerprint,
            candidate_fingerprint=candidate_fingerprint,
            differing_fields=differing_fields,
            has_drift=has_drift,
            can_accept=(
                target.registry.state == "connected"
                and target.traffic_state == "disabled"
                and pool.capability_fingerprint is not None
                and candidate_fingerprint is not None
                and not blockers
            ),
            blocking_member_ids=blockers,
        )

    async def accept_capability_drift(
        self,
        pool_id: UUID,
        registry_id: UUID,
        *,
        expected_candidate_fingerprint: str,
        enable_pool: bool,
    ) -> tuple[PoolFacts, CapabilityDriftAcceptance] | None:
        from .pool_capability import diff_capabilities

        pool, rows = await self.data.lock_drift_acceptance_rows(pool_id, registry_id)
        if pool is None:
            return None
        target = next((row for row in rows if row.registry_id == registry_id), None)
        if target is None:
            return None
        if target.registry.state != "connected":
            raise CapabilityRevalidationError(
                "ml_backend_capability_revalidation_failed",
                "backend must be connected before accepting capability drift",
            )
        snapshot, fingerprint = _registry_capability(target.registry)
        if snapshot is None or fingerprint is None:
            raise CapabilityRevalidationError(
                "ml_backend_capability_snapshot_unavailable",
                "backend capability snapshot is unavailable",
            )
        if fingerprint != expected_candidate_fingerprint:
            raise CapabilityRevalidationError(
                "ml_backend_capability_changed_during_review",
                "backend capability changed after review; refresh the preview",
            )
        if target.traffic_state != "disabled":
            raise CapabilityRevalidationError(
                "ml_backend_capability_revalidation_not_required",
                "only a disabled member can accept capability drift",
            )
        if pool.capability_snapshot is None or pool.capability_fingerprint is None:
            raise CapabilityRevalidationError(
                "ml_backend_capability_snapshot_unavailable",
                "service pool capability baseline is unavailable",
            )
        mismatch = diff_capabilities(pool.capability_snapshot, snapshot)
        blockers = _blocking_members(
            rows, target_registry_id=registry_id, candidate_fingerprint=fingerprint
        )
        if blockers:
            raise CapabilityRevalidationError(
                "ml_backend_pool_capability_blocked_by_members",
                "active or draining pool members do not match the reviewed capability",
                blocking_member_ids=blockers,
            )
        if enable_pool:
            legacy = next(
                (row for row in rows if row.registry_id == pool.legacy_instance_id),
                None,
            )
            if legacy is None or (
                legacy.registry_id != registry_id and legacy.traffic_state != "active"
            ):
                raise CapabilityRevalidationError(
                    "ml_backend_capability_revalidation_failed",
                    "legacy instance must be an active member before enabling the pool",
                )
        health = _mutable_json(target.registry.health_meta or {})
        expected = {
            "pool_generation": pool.routing_generation,
            "pool_fingerprint": pool.capability_fingerprint,
            "pool_snapshot": _mutable_json(pool.capability_snapshot),
            "candidate_capabilities": health.get("capabilities"),
            "candidate_state": target.registry.state,
            "candidate_member_id": str(target.id),
            "candidate_member_state": target.traffic_state,
            "member_facts": [
                {
                    "id": str(row.id),
                    "registry_id": str(row.registry_id),
                    "traffic_state": row.traffic_state,
                    "capabilities": _mutable_json(
                        _mutable_json(row.registry.health_meta or {}).get(
                            "capabilities"
                        )
                    ),
                }
                for row in rows
            ],
            "replace_baseline": mismatch is not None,
            "replacement_snapshot": _mutable_json(snapshot),
        }
        updated = await self.data.accept_drift(
            pool_id, registry_id, expected_candidate_fingerprint, enable_pool, expected
        )
        if updated is None:
            return None
        return updated, CapabilityDriftAcceptance(
            old_fingerprint=pool.capability_fingerprint,
            new_fingerprint=fingerprint,
            differing_fields=mismatch.differing_fields if mismatch is not None else (),
            pool_enabled=updated.enabled,
        )
