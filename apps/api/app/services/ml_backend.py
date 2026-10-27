from __future__ import annotations

import uuid

from aap_inference_contract.topology import RegistryPublicFacts
from aap_protocol_v2.backend_auth import managed_provider
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.project_access import get_workspace_project
from aap_inference.gpu.policy import validate_gpu_claim
from app.config import settings


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


class GPUBackendManagedMutationBlocked(Exception):
    """A backend that entered a managed runtime must use managed retirement."""


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


class MLBackendService:
    """Caller-UOW topology and control application service.

    Registry facts never contain stored credentials. Network operations execute
    through the separately credentialed Inference owner.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _topology(self):
        from aap_inference.topology import BackendTopologyCoordinator
        from app.services.inference_topology_data import InferenceTopologyData

        return BackendTopologyCoordinator(InferenceTopologyData(self.db))

    async def _topology_call(self, operation):
        from aap_inference.topology import (
            CapabilityRevalidationError,
            ManagedMutationBlocked,
            PoolCapabilityMismatch,
            PoolCapabilityUnavailable,
            PoolMemberConflict,
            RegistryDeleteBlocked,
            RegistryURLConflict,
            TopologyChanged,
        )

        try:
            return await operation
        except RegistryURLConflict as exc:
            raise MLBackendURLConflict(exc.backend_name) from exc
        except PoolMemberConflict as exc:
            raise MLBackendPoolMemberConflict(exc.pool_id) from exc
        except PoolCapabilityUnavailable as exc:
            raise MLBackendPoolCapabilityUnavailable(str(exc)) from exc
        except PoolCapabilityMismatch as exc:
            from app.services.ml_routing.contracts import (
                CapabilityDiff,
                CapabilityMismatchError,
            )

            mismatch = exc.mismatch
            raise CapabilityMismatchError(
                CapabilityDiff(
                    pool_fingerprint=mismatch.pool_fingerprint,
                    candidate_fingerprint=mismatch.candidate_fingerprint,
                    differing_fields=mismatch.differing_fields,
                )
            ) from exc
        except CapabilityRevalidationError as exc:
            raise MLBackendCapabilityRevalidationError(
                exc.error_code,
                str(exc),
                blocking_member_ids=exc.blocking_member_ids,
            ) from exc
        except RegistryDeleteBlocked as exc:
            raise MLBackendDeleteBlocked(exc.running_jobs) from exc
        except ManagedMutationBlocked as exc:
            raise GPUBackendManagedMutationBlocked(str(exc)) from exc
        except TopologyChanged as exc:
            from app.services.ml_routing.contracts import MLBackendQuiescenceError

            raise MLBackendQuiescenceError(exc.error_code, str(exc)) from exc

    # ── 全局注册表 ───────────────────────────────────────────────────────────
    async def create_registry(
        self, name: str, url: str, source: str = "manual", **kwargs
    ):
        unknown = set(kwargs) - (_REGISTRY_MUTABLE_FIELDS - {"name", "url"})
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")
        validate_gpu_claim(
            kwargs.get("gpu_resource_id"),
            kwargs.get("vram_budget_mb"),
            extra_params=kwargs.get("extra_params"),
            config=settings,
        )
        # Managed first-party credentials stay resolved by the API owner; the
        # Inference owner only ever receives a materialized token.
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
        return await self._topology_call(
            self._topology().create_registry(name, url, source=source, **kwargs)
        )

    # ── v0.23.3 ADR-0050 §12.1 · Super Admin pool/member management ──────────
    async def create_pool(
        self, name: str, *, legacy_instance_id: uuid.UUID | None = None
    ):
        return await self._topology_call(
            self._topology().create_pool(name, legacy_instance_id=legacy_instance_id)
        )

    async def get_pool(self, pool_id: uuid.UUID):
        return await self._topology().get_pool(pool_id)

    async def list_pools(self):
        return await self._topology().list_pools()

    async def update_pool(
        self,
        pool_id: uuid.UUID,
        *,
        name: str | None = None,
        enabled: bool | None = None,
    ):
        return await self._topology_call(
            self._topology().update_pool(pool_id, name=name, enabled=enabled)
        )

    async def delete_pool(self, pool_id: uuid.UUID) -> bool:
        return await self._topology_call(self._topology().delete_pool(pool_id))

    async def add_pool_member(
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        weight: int = 1,
        capability_snapshot: dict | None = None,
    ):
        return await self._topology_call(
            self._topology().add_pool_member(
                pool_id,
                registry_id,
                weight=weight,
                capability_snapshot=capability_snapshot,
            )
        )

    async def preview_capability_drift(
        self, pool_id: uuid.UUID, registry_id: uuid.UUID
    ):
        return await self._topology_call(
            self._topology().preview_capability_drift(pool_id, registry_id)
        )

    async def accept_capability_drift(
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        expected_candidate_fingerprint: str,
        enable_pool: bool,
    ):
        return await self._topology_call(
            self._topology().accept_capability_drift(
                pool_id,
                registry_id,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                enable_pool=enable_pool,
            )
        )

    async def remove_pool_member(
        self,
        pool_id: uuid.UUID,
        registry_id: uuid.UUID,
        *,
        expected_topology=None,
    ) -> bool:
        return await self._topology_call(
            self._topology().remove_pool_member(
                pool_id, registry_id, expected_topology=expected_topology
            )
        )

    async def drain_pool_member(self, pool_id: uuid.UUID, registry_id: uuid.UUID):
        return await self._topology_call(
            self._topology().drain_pool_member(pool_id, registry_id)
        )

    async def resume_pool_member(self, pool_id: uuid.UUID, registry_id: uuid.UUID):
        return await self._topology_call(
            self._topology().resume_pool_member(pool_id, registry_id)
        )

    async def get_registry(self, registry_id: uuid.UUID) -> RegistryPublicFacts | None:
        """Return credential-free Registry facts for non-network consumers."""
        return await self._topology().get_registry(registry_id)

    async def get_by_url(self, url: str):
        return await self._topology().get_by_url(url)

    async def update(
        self,
        registry_id: uuid.UUID,
        *,
        preserve_manual_auth: bool = False,
        **kwargs,
    ):
        unknown = set(kwargs) - _REGISTRY_MUTABLE_FIELDS
        if unknown:
            raise TypeError(f"unsupported ML backend fields: {sorted(unknown)}")
        coordinator = self._topology()
        row = await coordinator.get_registry(registry_id)
        if row is None:
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
        if "url" in kwargs:
            url = kwargs["url"]
            if not isinstance(url, str) or not url:
                raise ValueError("url must not be null or empty")
            normalized_url = url.rstrip("/")
            conflict = await coordinator.get_by_url(normalized_url)
            if conflict is not None and conflict.id != row.id:
                raise MLBackendURLConflict(conflict.name)
            kwargs["url"] = normalized_url
        next_method = kwargs.get("auth_method", row.auth_method)
        if managed_provider(next_method) is not None and (
            "auth_method" in kwargs or "url" in kwargs or "auth_token" in kwargs
        ):
            # Resolve the handshake before the durable write (and its row lock).
            from app.services.managed_inference_auth import initialize_managed_auth

            kwargs["auth_token"] = await initialize_managed_auth(
                kwargs.get("url", row.url), kwargs.get("auth_token"), next_method
            )
        elif (
            managed_provider(row.auth_method) is not None
            and managed_provider(next_method) is None
        ):
            if next_method == "token" and not kwargs.get("auth_token"):
                raise HTTPException(
                    422, "改用手动 Token 时必须填写服务端令牌；系统托管凭据不能沿用。"
                )
            if next_method != "token":
                kwargs["auth_token"] = None
        validate_gpu_claim(
            kwargs.get("gpu_resource_id", row.gpu_resource_id),
            kwargs.get("vram_budget_mb", row.vram_budget_mb),
            extra_params=kwargs.get("extra_params", row.extra_params),
            config=settings,
        )
        if "eviction_priority" in kwargs and kwargs["eviction_priority"] is None:
            raise ValueError("eviction_priority must not be null")
        return await self._topology_call(
            coordinator.update_registry(registry_id, **kwargs)
        )

    async def validate_delete(self, registry_id: uuid.UUID) -> bool:
        return await self._topology_call(self._topology().validate_delete(registry_id))

    async def delete_with_topology(
        self, registry_id: uuid.UUID, *, expected_topology=None
    ):
        return await self._topology_call(
            self._topology().delete_registry(
                registry_id, expected_topology=expected_topology
            )
        )

    # ── 项目启用关联 (v0.23.3 ADR-0050 · 经服务池层) ─────────────────────────
    # Project associations use service-pool IDs; Registry adapters resolve their
    # physical IDs through the topology coordinator.

    async def pool_id_for_registry(self, registry_id: uuid.UUID) -> uuid.UUID | None:
        return await self._topology().pool_id_for_registry(registry_id)

    async def list_enabled_for_project(self, project_id: uuid.UUID):
        return await self._topology().list_enabled_for_project(project_id)

    async def list_available_for_project(self, project_id: uuid.UUID):
        return await self._topology().list_available_for_project(project_id)

    async def list_pools_available_for_project(self, project_id: uuid.UUID):
        return await self._topology().list_pools_available_for_project(project_id)

    async def set_pool_enabled(
        self,
        project_id: uuid.UUID,
        pool_id: uuid.UUID,
        enabled: bool,
        **overrides,
    ):
        return await self._topology_call(
            self._topology().set_pool_enabled(project_id, pool_id, enabled, **overrides)
        )

    async def get_assoc(self, project_id: uuid.UUID, registry_id: uuid.UUID):
        return await self._topology().get_assoc(project_id, registry_id)

    async def is_enabled(self, project_id: uuid.UUID, registry_id: uuid.UUID) -> bool:
        return await self._topology().is_enabled(project_id, registry_id)

    async def set_enabled(
        self,
        project_id: uuid.UUID,
        registry_id: uuid.UUID,
        enabled: bool,
        *,
        nowait: bool = False,
        **overrides,
    ):
        return await self._topology_call(
            self._topology().set_enabled(project_id, registry_id, enabled, **overrides)
        )

    # ── client 操作 (按 registry id) ─────────────────────────────────────────
    async def unload(
        self, registry_id: uuid.UUID, *, expected_topology=None
    ) -> dict | None:
        from app.services.inference_backend_control import backend_control_service

        return await backend_control_service(self.db).unload(
            registry_id, expected_topology=expected_topology
        )

    async def reload(
        self,
        registry_id: uuid.UUID,
        sam_variant: str | None = None,
        dino_variant: str | None = None,
        modality: str | None = None,
        task_type: str | None = None,
    ) -> dict | None:
        from app.services.inference_backend_control import backend_control_service

        return await backend_control_service(self.db).reload(
            registry_id,
            sam_variant=sam_variant,
            dino_variant=dino_variant,
            modality=modality,
            task_type=task_type,
        )

    async def warmup(self, registry_id: uuid.UUID, body: dict) -> dict | None:
        """协议 §4.4 · 转发 /warmup. body 原样上抛 backend, 各 backend schema 不同."""
        from app.services.inference_backend_control import backend_control_service

        return await backend_control_service(self.db).warmup(registry_id, body)

    async def check_health(
        self,
        registry_id: uuid.UUID,
        *,
        gpu_health_challenge: str | None = None,
    ) -> bool:
        from app.services.inference_backend_control import backend_control_service

        return await backend_control_service(self.db).check_health(
            registry_id, gpu_health_challenge=gpu_health_challenge
        )

    async def get_interactive_backend(self, project_id: uuid.UUID):
        return await self._topology().get_interactive_backend(project_id)

    async def get_project_backend(self, project_id: uuid.UUID):
        project = await get_workspace_project(self.db, project_id=project_id)
        primary_pool_id = project.ml_backend_pool_id if project is not None else None
        return await self._topology().get_project_backend(
            project_id, primary_pool_id=primary_pool_id
        )

    async def get_tracker_backend(self, project_id: uuid.UUID, model_key: str):
        return await self.get_tracker_backend_for_capabilities(project_id, [model_key])

    async def get_tracker_backend_for_capabilities(
        self, project_id: uuid.UUID, model_keys: list[str]
    ):
        project = await get_workspace_project(self.db, project_id=project_id)
        primary_pool_id = project.ml_backend_pool_id if project is not None else None
        return await self._topology().get_tracker_backend_for_capabilities(
            project_id, model_keys, primary_pool_id=primary_pool_id
        )
