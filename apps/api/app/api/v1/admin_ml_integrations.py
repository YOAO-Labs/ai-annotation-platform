"""v0.9.3 · 超管 ML 集成总览。

聚合返回：
- storage：读取平台桶的持久成功快照（仅 super_admin 走该端点）
- projects：跨所有项目的 ml_backends 列表，按 project 分组（保留 backend.url 但不返回 auth_token）

v0.9.6 · 加 /probe (无 DB 副作用的 health check) + /runtime-hints (前端 modal placeholder).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

import uuid
from uuid import UUID

import httpx
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
)
from aap_inference.gpu.diagnostics import record_unregistered_gpu_shadow_dispatch
from aap_inference.gpu.policy import GPUClaimConfigurationError
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
)
from aap_inference_contract.topology import to_record
from aap_inference_contract.backend_control import BackendRequestOnlyEndpoint
from aap_inference.gpu.policy import strict_gpu_loaded_evidence
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.ml_registry_deletion import (
    delete_registry as delete_registry_command,
)
from app.config import settings
from app.db.enums import UserRole
from app.db.models.project import Project
from app.deps import get_db, require_roles
from app.schemas.ml_backend import (
    ComputeInfo,
    GPUBackendConfigStatus,
    GPUConfigDiagnostic,
    GPUConfigErrorResponse,
    HealthMeta,
    MLBackendHealthResponse,
    MLBackendOut,
    MLBackendUnloadResponse,
    MLBackendRegistryConflictResponse,
    MLBackendRegistryCreate,
    MLBackendRegistryUpdate,
    RequestValidationErrorResponse,
    ResidencyInfo,
)
from app.schemas.ml_routing import (
    RuntimeSnapshotResponse,
    TopologyResponse,
)
from app.schemas.storage import BucketsResponse
from app.services.storage_usage import read_usage, require_admin
from app.services.audit import AuditService
from app.services.identity_commands import IdentityCommandService
from app.services.identity_contracts import IdentityActorFacts
from app.services.inference_backend_control import (
    BackendControlOperationError,
    backend_control_service,
)
from app.services.inference_gpu import gpu_authority_client
from app.services.inference_gpu_data import bind_gpu_authority_data
from app.services.inference_topology_data import InferenceTopologyData
from app.services.ml_backend import (
    GPUBackendManagedMutationBlocked,
    MLBackendCapabilityRevalidationError,
    MLBackendDeleteBlocked,
    MLBackendPoolCapabilityUnavailable,
    MLBackendPoolMemberConflict,
    MLBackendService,
    MLBackendURLConflict,
)
from app.services.workspace_read_relations import project_rows

router = APIRouter()


def _gpu_dispatch_http_error(error: GPUArbiterDispatchError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers=error.headers or None,
    )


def _control_http_error(error: BackendControlOperationError) -> HTTPException:
    failure = error.failure
    return HTTPException(
        status_code=failure.status_code or 502,
        detail=failure.detail or failure.code,
        headers=(
            {"Retry-After": failure.retry_after}
            if failure.retry_after is not None
            else None
        ),
    )


# v0.14.14: gsam2 image pool key 形如 "sam=<sv>/dino=<dv>" (协议 §4.3 loaded_keys[].key
# 由 backend 自定义命名). admin 试启动只在 message / loaded_variant 字段中沿用旧
# {sam_variant, dino_variant} 形态展示, 这里 parse 还原, 失败则不还原.
_GSAM2_IMAGE_KEY_RE = re.compile(r"^sam=(.+?)/dino=(.+)$")


def _parse_gsam2_image_key(key: str) -> dict | None:
    m = _GSAM2_IMAGE_KEY_RE.match(key)
    if not m:
        return None
    return {"sam_variant": m.group(1), "dino_variant": m.group(2)}


class ProjectMLBackendsGroup(BaseModel):
    project_id: str
    project_name: str
    backends: list[MLBackendOut]


class StorageOverview(BucketsResponse):
    pass


class MLIntegrationsOverview(BaseModel):
    storage: StorageOverview
    projects: list[ProjectMLBackendsGroup]
    total_backends: int
    connected_backends: int


@router.get("/overview", response_model=MLIntegrationsOverview)
async def get_overview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from app.deps import assert_request_scopes

    assert_request_scopes(request, "*")
    await require_admin(db, _admin.id)
    storage_overview = StorageOverview.model_validate(
        (await read_usage(db)).model_dump()
    )

    # v0.19.0 ADR-0044 · backend 全局化; 「按项目分组」改为按项目「已启用」关联分组
    # (project_ml_backend_pool join pool member join registry)。total/connected
    # 统计全局注册表去重后的真值。v0.23.3 ADR-0050 · 项目启用经服务池层。
    topology = InferenceTopologyData(db)
    project_alias = aliased(Project, project_rows(), adapt_on_names=True)
    projects_result = await db.execute(
        select(project_alias).order_by(project_alias.name)
    )
    projects = list(projects_result.scalars().all())
    project_ids = [project.id for project in projects]
    project_facts = []
    for offset in range(0, len(project_ids), 500):
        project_facts.extend(
            await topology.project_backend_facts(project_ids[offset : offset + 500])
        )
    backends_by_project: dict[uuid.UUID, list] = {}
    for fact in project_facts:
        association = fact.association
        if association is not None and association.enabled:
            backends_by_project.setdefault(fact.project_id, []).append(fact.registry)

    grouped = []
    for project in projects:
        project_backends = backends_by_project.get(project.id, [])
        if not project_backends and not project.ai_enabled:
            continue
        grouped.append(
            ProjectMLBackendsGroup(
                project_id=str(project.id),
                project_name=project.name,
                backends=[
                    MLBackendOut.model_validate(
                        {**to_record(backend), "project_id": project.id}
                    )
                    for backend in sorted(
                        project_backends,
                        key=lambda backend: backend.created_at,
                        reverse=True,
                    )
                ],
            )
        )

    registry_facts = await topology.registry_catalog()
    return MLIntegrationsOverview(
        storage=storage_overview,
        projects=grouped,
        total_backends=len(registry_facts),
        connected_backends=sum(
            1 for backend in registry_facts if backend.state == "connected"
        ),
    )


# ── v0.9.6 · /probe + /runtime-hints ──────────────────────────────────


_HTTP_BASE_URL = TypeAdapter(AnyHttpUrl)


def _validate_probe_base_url(value: str) -> None:
    """Validate URL syntax while allowing private, loopback, and internal hosts."""
    error = "URL must be HTTP(S) with a valid host and port, without credentials, query, fragment, or control characters"
    if (
        any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value)
        or "?" in value
        or "#" in value
    ):
        raise HTTPException(status_code=422, detail=error)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        _HTTP_BASE_URL.validate_python(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=error) from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or port == 0
    ):
        raise HTTPException(status_code=422, detail=error)


def _safe_probe_error(exc: Exception) -> str:
    """Return a stable failure category without echoing a request URL or secret."""
    if isinstance(exc, BackendControlOperationError):
        if exc.failure.status_code is not None:
            return f"HTTP {exc.failure.status_code}"
        return "网络请求失败"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return "网络请求失败"
    if isinstance(exc, ValueError):
        return "响应格式无效"
    return "探测失败"


class ProbeRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500)
    auth_method: Literal["none", "token"] = "none"
    auth_token: str | None = Field(default=None, max_length=500)


class ProbeResponse(BaseModel):
    """v0.9.6 · 无 DB 副作用的 health check.
    前端注册 modal 在保存前可调本端点验证连通性, 避免「先存再 health 失败 / DB 留无效行」摩擦.
    """

    ok: bool
    latency_ms: int
    status_code: int | None = None
    error: str | None = None
    gpu_info: dict | None = None
    cache: dict | None = None
    model_version: str | None = None


@router.post("/probe", response_model=ProbeResponse)
async def probe_backend(
    payload: ProbeRequest,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
):
    """探测一个 ML backend URL 的 /health 端点; 不写 DB."""
    _validate_probe_base_url(payload.url)
    try:
        endpoint = BackendRequestOnlyEndpoint(
            url=payload.url.rstrip("/"),
            auth_method=payload.auth_method,
            auth_token=payload.auth_token,
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Invalid request-only endpoint",
        ) from None
    try:
        fact = await backend_control_service(db).manual_probe(
            endpoint, timeout_seconds=settings.ml_health_timeout
        )
    except Exception as exc:
        return ProbeResponse(
            ok=False,
            latency_ms=0,
            error=_safe_probe_error(exc),
        )

    health = fact.health
    if not fact.ok or health is None:
        return ProbeResponse(
            ok=False,
            latency_ms=fact.latency_ms,
            status_code=fact.status_code,
            error=fact.error or ("响应非 JSON" if fact.status_code == 200 else None),
        )
    return ProbeResponse(
        ok=bool(health.get("ok", True)),
        latency_ms=fact.latency_ms,
        status_code=fact.status_code,
        gpu_info=health.get("gpu_info"),
        cache=health.get("cache"),
        model_version=health.get("model_version"),
    )


class RuntimeHints(BaseModel):
    """v0.9.6 · 前端 modal 启动时一次性查; 提供注册 form 的 placeholder hint."""

    ml_backend_default_url: str | None = None
    managed_backend_urls: dict[str, str] = Field(default_factory=dict)


@router.get("/runtime-hints", response_model=RuntimeHints)
async def runtime_hints(
    _admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
):
    return RuntimeHints(
        ml_backend_default_url=settings.ml_backend_default_url or None,
        managed_backend_urls=settings.ml_managed_backend_urls,
    )


# ─── v0.9.7 · 全局 backend 列表 ────────────────────────────────────────


ManagedProvider = Literal["gsam2", "sam3", "yolo", "onnxtools", "rapidocr"]


class ManagedAuthRegistry(BaseModel):
    id: UUID
    name: str
    auth_method: str
    state: str


class ManagedAuthItem(BaseModel):
    provider: ManagedProvider
    service_name: str
    url: str
    credential_state: Literal["missing", "ready", "invalid"]
    registries: list[ManagedAuthRegistry] = Field(default_factory=list)
    outcome: (
        Literal["synced", "initialized", "unavailable", "manual_preserved", "error"]
        | None
    ) = None
    message: str | None = None


class ManagedAuthStatus(BaseModel):
    items: list[ManagedAuthItem]


class ManagedAuthInitialize(BaseModel):
    model_config = {"extra": "forbid"}
    providers: list[ManagedProvider] | None = Field(
        default=None, min_length=1, max_length=5
    )


async def _managed_auth_status(db: AsyncSession) -> ManagedAuthStatus:
    from aap_protocol_v2.backend_auth import BACKEND_SERVICES
    from aap_protocol_v2.managed_credential import CredentialFileError, read_credential
    from app.services.managed_inference_auth import (
        configured_backends,
        credential_directory,
        provider_for_url,
    )

    catalog = await InferenceTopologyData(db).registry_catalog()
    items = []
    for provider, url in configured_backends().items():
        directory = credential_directory(provider)
        try:
            if not (directory / "token").exists():
                state = "missing"
            else:
                read_credential(directory)
                state = "ready"
        except (OSError, CredentialFileError):
            state = "invalid"
        items.append(
            ManagedAuthItem(
                provider=provider,
                service_name=BACKEND_SERVICES[provider],
                url=url,
                credential_state=state,
                registries=[
                    ManagedAuthRegistry(
                        id=row.id,
                        name=row.name,
                        auth_method=row.auth_method,
                        state=row.state,
                    )
                    for row in catalog
                    if provider_for_url(row.url) == provider
                ],
            )
        )
    return ManagedAuthStatus(items=items)


@router.get("/managed-auth", response_model=ManagedAuthStatus)
async def managed_auth_status(
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Inspect private local defaults without revealing or sending credentials."""
    return await _managed_auth_status(db)


@router.post("/managed-auth/initialize", response_model=ManagedAuthStatus)
async def initialize_managed_backends(
    payload: ManagedAuthInitialize,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from aap_protocol_v2.backend_auth import managed_provider
    from aap_protocol_v2.managed_credential import (
        CredentialFileError,
        ensure_credential,
    )
    from app.services.managed_inference_auth import (
        configured_backends,
        credential_directory,
    )

    status = await _managed_auth_status(db)
    selected = set(
        payload.providers if payload.providers is not None else configured_backends()
    )
    if selected - configured_backends().keys():
        raise HTTPException(422, "所选内置服务未在当前部署的可信地址映射中配置。")
    for item in status.items:
        if item.provider not in selected:
            continue
        try:
            ensure_credential(credential_directory(item.provider))
            item.credential_state = "ready"
        except (OSError, CredentialFileError):
            item.outcome = "error"
            item.message = "本地凭据目录不可用，请检查归属、私有权限和文件完整性。"
            continue
        if not item.registries:
            item.outcome = "initialized"
            item.message = "默认凭据已初始化；尚未注册此服务，没有实例需要同步。"
            continue
        synced = 0
        preserved = 0
        failures: list[str] = []
        for registry in item.registries:
            if (
                registry.auth_method != "none"
                and managed_provider(registry.auth_method) is None
            ):
                preserved += 1
                continue
            try:
                async with db.begin_nested():
                    updated = await MLBackendService(db).update(
                        registry.id,
                        preserve_manual_auth=True,
                        auth_method=f"managed:{item.provider}",
                    )
                    if updated is None:
                        raise HTTPException(409, "实例已删除，请刷新后重试。")
                    await AuditService.log(
                        db,
                        actor=admin,
                        action="ml_registry.managed_auth_synced",
                        target_type="ml_backend",
                        target_id=str(registry.id),
                        request=request,
                        status_code=200,
                        detail={"provider": item.provider},
                    )
                    registry.auth_method = updated.auth_method
                    registry.state = updated.state
                await db.commit()
                synced += 1
            except HTTPException as exc:
                manual = (
                    isinstance(exc.detail, dict)
                    and exc.detail.get("error_code") == "manual_auth_preserved"
                )
                if manual:
                    preserved += 1
                else:
                    failures.append(str(exc.detail))
        if failures:
            item.outcome = "unavailable"
            item.message = (
                f"已同步 {synced} 个实例，保留 {preserved} 个手动认证实例；"
                f"{len(failures)} 个实例未完成同步：{failures[0]}"
            )
        elif preserved:
            item.outcome = "manual_preserved"
            item.message = (
                f"已同步 {synced} 个实例，保留 {preserved} 个手动认证实例；"
                "如需改用系统托管，请在实例编辑中明确切换。"
            )
        else:
            item.outcome = "synced"
            item.message = (
                f"已验证并同步 {synced} 个实例的认证；"
                "请执行实例健康检查以更新服务状态。"
            )
    await AuditService.log(
        db,
        actor=admin,
        action="ml_registry.managed_auth_initialized",
        target_type="ml_backend",
        target_id="managed",
        request=request,
        status_code=200,
        detail={"providers": sorted(selected)},
    )
    await db.commit()
    return status


class GlobalBackendItem(BaseModel):
    """v0.9.7 · CreateProjectWizard step 4 dropdown 用的 backend 概要项."""

    id: str
    name: str
    url: str
    state: str
    is_interactive: bool
    auth_method: str
    extra_params: dict = Field(default_factory=dict)
    gpu_resource_id: str | None = None
    vram_budget_mb: int | None = None
    eviction_priority: int | None = None
    gpu_config: GPUBackendConfigStatus | None = None
    health_meta: HealthMeta | dict | None = None
    source_project_id: str
    source_project_name: str
    last_checked_at: str | None = None


class GlobalBackendListResponse(BaseModel):
    items: list[GlobalBackendItem]


@router.get("/all", response_model=GlobalBackendListResponse)
async def list_all_backends(
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
) -> GlobalBackendListResponse:
    """v0.19.0 ADR-0044 · 列全局注册表所有 backend。

    用于 CreateProjectWizard step 4 让用户选「为新项目启用一个已注册 backend」;
    复用时 create_project 端点为新项目建启用关联 (复用同一全局 registry id)。
    source_project_* 字段保留兼容: 现以 source ('manual'/'env') 作来源标签。
    """
    topology = InferenceTopologyData(db)
    backends = await topology.registry_catalog()
    backends.sort(
        key=lambda backend: (
            backend.last_checked_at is not None,
            backend.last_checked_at,
        ),
        reverse=True,
    )
    role_value = admin.role.value if hasattr(admin.role, "value") else admin.role
    can_view_gpu_topology = role_value == UserRole.SUPER_ADMIN.value
    gpu_config_by_backend: dict[uuid.UUID, GPUBackendConfigStatus] = {}
    if can_view_gpu_topology:
        await db.rollback()
        try:
            diagnostics = await gpu_authority_client().get_diagnostics()
        except GPUArbiterDispatchError as exc:
            raise _gpu_dispatch_http_error(exc) from exc
        gpu_config_by_backend = {
            uuid.UUID(item["backend_id"]): GPUBackendConfigStatus.model_validate(
                item["gpu_config"]
            )
            for item in diagnostics.result.get("backends", [])
            if isinstance(item, dict)
            and isinstance(item.get("backend_id"), str)
            and isinstance(item.get("gpu_config"), dict)
        }

    items: list[GlobalBackendItem] = []
    for backend in backends:
        health_meta = to_record(backend.health_meta) if backend.health_meta else None
        items.append(
            GlobalBackendItem(
                id=str(backend.id),
                name=backend.name,
                url=backend.url,
                state=backend.state,
                is_interactive=backend.is_interactive,
                auth_method=backend.auth_method,
                extra_params=to_record(backend.extra_params) or {},
                gpu_resource_id=backend.gpu_resource_id
                if can_view_gpu_topology
                else None,
                vram_budget_mb=backend.vram_budget_mb
                if can_view_gpu_topology
                else None,
                eviction_priority=backend.eviction_priority
                if can_view_gpu_topology
                else None,
                gpu_config=gpu_config_by_backend.get(backend.id),
                health_meta=(
                    health_meta
                    if can_view_gpu_topology
                    else {"capabilities": health_meta.get("capabilities")}
                    if isinstance(health_meta, dict)
                    else {"capabilities": None}
                ),
                source_project_id="",
                source_project_name=backend.source,
                last_checked_at=(
                    backend.last_checked_at.isoformat()
                    if backend.last_checked_at
                    else None
                ),
            )
        )
    return GlobalBackendListResponse(items=items)


class GPUArbiterRuntimeObservationItem(BaseModel):
    status: Literal[
        "disabled",
        "missing",
        "prepared",
        "ready",
        "not_ready",
        "corrupt",
        "unavailable",
    ]
    reason: str
    ready: bool
    ledger_revision: int | None = None
    ledger_incarnation: str | None = None
    reconcile_deadline_ms: int | None = None
    committed_mb: int | None = None
    backend_count: int
    active_backend_count: int
    membership_state_counts: dict[str, int]
    allocation_state_counts: dict[str, int]
    lease_count: int | None
    card_queue_count: int | None
    backend_queue_count: int | None
    transition_present: bool | None
    durable_domain_matches: bool | None = None


class GPUArbiterRolloutItem(BaseModel):
    enabled: bool
    state: Literal[
        "disabled",
        "uninitialized",
        "off",
        "promoting",
        "enforcing",
        "demoting",
        "blocked",
    ]
    effective_mode: Literal["off", "observe", "enforce"]
    target_mode: Literal["off", "observe", "enforce"] | None = None
    dispatch_blocked: bool
    blocked_reason: str | None = None
    transition_id: uuid.UUID | None = None
    last_transition_id: uuid.UUID | None = None
    revision: int | None = None


class GPUArbiterResourceItem(BaseModel):
    gpu_resource_id: str
    node_id: str
    physical_device_token: str
    allocatable_mb: int
    configured_mode: Literal["off", "observe", "enforce"] | None = None
    desired_mode: Literal["off", "observe", "enforce"]
    effective_mode: Literal["off", "observe", "enforce"]
    claimed_budget_mb: int
    claimed_backend_count: int
    status: Literal["ok", "info", "warning", "critical", "blocker"]
    diagnostics: list[GPUConfigDiagnostic] = Field(default_factory=list)
    rollout: GPUArbiterRolloutItem
    runtime: GPUArbiterRuntimeObservationItem


class GPUArbiterResourcesResponse(BaseModel):
    global_desired_mode: Literal["off", "observe", "enforce"]
    rollout_enabled: bool
    runtime_ready: bool
    observe_runtime_ready: bool
    enforce_runtime_ready: bool
    resources: list[GPUArbiterResourceItem]
    diagnostics: list[GPUConfigDiagnostic]


@router.get("/gpu-resources", response_model=GPUArbiterResourcesResponse)
async def list_gpu_resources(
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> GPUArbiterResourcesResponse:
    """Return typed physical resources and static claim diagnostics; no side effects."""

    authority = gpu_authority_client()

    # Drop any caller-owned transaction before the authority performs its bounded
    # diagnostics and runtime observations.
    await db.rollback()
    try:
        summary = await authority.get_diagnostics()
    except GPUArbiterDispatchError as exc:
        raise _gpu_dispatch_http_error(exc) from exc
    result = summary.result
    resources = result.get("resources", [])
    rollouts = result.get("rollouts", [])
    rollout_by_resource = {
        item["resource_id"]: item for item in rollouts if isinstance(item, dict)
    }
    static_diagnostics = [
        GPUConfigDiagnostic.model_validate(item)
        for item in result.get("diagnostics", [])
    ]
    observation_semaphore = asyncio.Semaphore(4)

    async def observe_one(resource: dict):
        try:
            async with observation_semaphore:
                async with asyncio.timeout(3):
                    observation = await authority.observe_resource(
                        resource["gpu_resource_id"],
                        allocatable_mb=resource["allocatable_mb"],
                    )
            return observation, None
        except TimeoutError:
            return None, "gpu_arbiter_observation_timeout"
        except GPUArbiterDispatchError as exc:
            if exc.error_code != GPUArbiterErrorCode.UNAVAILABLE.value:
                raise _gpu_dispatch_http_error(exc) from exc
            return None, exc.error_code

    observed = await asyncio.gather(*(observe_one(resource) for resource in resources))
    observations = [item[0] for item in observed]
    unavailable = [
        (index, resource, observed[index][1])
        for index, resource in enumerate(resources)
        if observed[index][0] is None
    ]
    if unavailable:
        try:
            gpu_data = await bind_gpu_authority_data(db)
            for index, resource, reason in unavailable:
                resource_id = resource["gpu_resource_id"]
                state_counts = {
                    state: len(
                        await gpu_data.membership_candidates(
                            resource_id, states=(state,)
                        )
                    )
                    for state in ("pending", "active", "retiring")
                }
                observations[index] = GPUArbiterRuntimeObservationItem(
                    status="unavailable",
                    reason=reason,
                    ready=False,
                    backend_count=sum(state_counts.values()),
                    active_backend_count=state_counts["active"],
                    membership_state_counts=state_counts,
                    allocation_state_counts={},
                    lease_count=None,
                    card_queue_count=None,
                    backend_queue_count=None,
                    transition_present=None,
                    durable_domain_matches=None,
                )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="GPU authority observation unavailable",
            ) from exc
        await db.rollback()
    response_resources: list[GPUArbiterResourceItem] = []
    runtime_diagnostics: list[GPUConfigDiagnostic] = []
    resource_ready: dict[str, bool] = {}
    for resource, observation in zip(resources, observations, strict=True):
        resource_id = resource["gpu_resource_id"]
        rollout = rollout_by_resource[resource_id]
        runtime = GPUArbiterRuntimeObservationItem.model_validate(
            observation.model_dump(mode="json", exclude={"protocol"})
        )
        item_diagnostics = [
            GPUConfigDiagnostic.model_validate(item)
            for item in resource.get("diagnostics", [])
        ]
        if resource["effective_mode"] == "enforce" and not runtime.ready:
            runtime_diagnostic = GPUConfigDiagnostic(
                code="gpu_arbiter_runtime_not_ready",
                level="blocker",
                message=(
                    "持久 rollout 已 enforcing，但 Redis 账本尚未 ready："
                    f"{runtime.reason or runtime.status}"
                ),
                resource_id=resource_id,
            )
            item_diagnostics.append(runtime_diagnostic)
            runtime_diagnostics.append(runtime_diagnostic)
        resource_ready[resource_id] = bool(
            resource["desired_mode"] == "enforce"
            and resource["effective_mode"] == "enforce"
            and runtime.ready
        )
        status = max(
            (diagnostic.level for diagnostic in item_diagnostics),
            key={
                "ok": 0,
                "info": 1,
                "warning": 2,
                "critical": 3,
                "blocker": 4,
            }.__getitem__,
            default="ok",
        )
        response_resources.append(
            GPUArbiterResourceItem(
                **{
                    **resource,
                    "diagnostics": item_diagnostics,
                    "status": status,
                    "rollout": {
                        "enabled": settings.gpu_arbiter_rollout_enabled,
                        "state": rollout["state"],
                        "effective_mode": rollout["effective_mode"],
                        "target_mode": rollout["target_mode"],
                        "dispatch_blocked": rollout["dispatch_blocked"],
                        "blocked_reason": (
                            rollout.get("blocked_reason")
                            or rollout.get("blocker_reason")
                        ),
                        "transition_id": rollout["transition_id"],
                        "last_transition_id": rollout["last_transition_id"],
                        "revision": rollout["revision"],
                    },
                    "runtime": runtime,
                }
            )
        )

    configured_ids = {item["gpu_resource_id"] for item in resources}
    desired_enforce_ids = {
        item["gpu_resource_id"]
        for item in resources
        if item["desired_mode"] == "enforce"
    }
    enforce_runtime_ready = bool(desired_enforce_ids) and all(
        resource_ready[resource_id] for resource_id in desired_enforce_ids
    )
    runtime_ready = (
        not settings.gpu_arbiter_config_errors
        and all(
            rollout["resource_id"] in configured_ids or rollout["state"] == "off"
            for rollout in rollouts
        )
        and all(
            (
                resource_ready[resource["gpu_resource_id"]]
                if resource["desired_mode"] == "enforce"
                else not rollout_by_resource[resource["gpu_resource_id"]][
                    "dispatch_blocked"
                ]
            )
            for resource in resources
        )
    )
    return GPUArbiterResourcesResponse(
        global_desired_mode=settings.gpu_arbiter_mode.value,
        rollout_enabled=settings.gpu_arbiter_rollout_enabled,
        runtime_ready=runtime_ready,
        observe_runtime_ready=True,
        enforce_runtime_ready=enforce_runtime_ready,
        resources=response_resources,
        diagnostics=[*static_diagnostics, *runtime_diagnostics],
    )


# ─── v0.19.0 ADR-0044 · superadmin 全局注册表 CRUD ──────────────────────────
#
# backend 是全局资源 (一物理 backend = 一 registry 行 = 一能力快照 = 一并发闸)。
# 增删改全局项是 superadmin 职责 (ModelMarket 注册管理), 与「项目启用」(PUT
# /projects/{id}/ml-backends/{rid}/enablement) 解耦。删除走 service 的 running-job
# 守卫 + projects/predictions FK SET NULL 级联。


@router.post(
    "/registry",
    response_model=MLBackendOut,
    status_code=201,
    responses={
        409: {"model": MLBackendRegistryConflictResponse},
        422: {"model": GPUConfigErrorResponse | RequestValidationErrorResponse},
    },
)
async def create_registry(
    data: MLBackendRegistryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> MLBackendOut:
    """注册一个全局 backend (source=manual)。同 url 已存在则 409。"""
    svc = MLBackendService(db)
    existing = await svc.get_by_url(data.url)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_url_conflict",
                "message": f"该 URL 已注册为全局 backend ({existing.name})",
            },
        )
    try:
        backend = await svc.create_registry(
            name=data.name,
            url=data.url,
            source="manual",
            is_interactive=data.is_interactive,
            **(
                {"auth_method": data.auth_method}
                if "auth_method" in data.model_fields_set
                else {}
            ),
            auth_token=data.auth_token,
            extra_params=data.extra_params,
            gpu_resource_id=data.gpu_resource_id,
            vram_budget_mb=data.vram_budget_mb,
            eviction_priority=data.eviction_priority,
        )
    except GPUClaimConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "gpu_config_invalid",
                "message": str(exc),
                "diagnostics": [
                    to_record(diagnostic) for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    except GPUBackendManagedMutationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "plugin_installation_owner_required", "message": str(exc)},
        ) from exc
    await AuditService.log(
        db,
        actor=admin,
        action="ml_registry.created",
        target_type="ml_backend",
        target_id=str(backend.id),
        request=request,
        status_code=201,
        detail={"name": data.name, "url": data.url},
    )
    await db.commit()
    fresh = await svc.get_registry(backend.id)
    if fresh is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    return MLBackendOut.model_validate(to_record(fresh))


@router.put(
    "/registry/{registry_id}",
    response_model=MLBackendOut,
    responses={
        409: {"model": MLBackendRegistryConflictResponse},
        422: {"model": GPUConfigErrorResponse | RequestValidationErrorResponse},
    },
)
async def update_registry(
    registry_id: uuid.UUID,
    data: MLBackendRegistryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> MLBackendOut:
    """编辑全局 backend 的端点固有属性 (name/url/auth/extra_params/is_interactive)。"""
    svc = MLBackendService(db)
    updates = data.model_dump(exclude_unset=True)
    try:
        backend = await svc.update(registry_id, **updates)
    except GPUClaimConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "gpu_config_invalid",
                "message": str(exc),
                "diagnostics": [
                    to_record(diagnostic) for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    except MLBackendURLConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_url_conflict",
                "message": f"该 URL 已注册为全局 backend ({exc.backend_name})",
            },
        ) from exc
    except GPUBackendManagedMutationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "gpu_backend_retirement_required",
                "message": str(exc),
            },
        ) from exc
    if backend is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_registry.updated",
        target_type="ml_backend",
        target_id=str(registry_id),
        request=request,
        status_code=200,
        detail={"fields": list(updates.keys())},
    )
    await db.commit()
    fresh = await svc.get_registry(registry_id)
    if fresh is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    return MLBackendOut.model_validate(to_record(fresh))


@router.delete("/registry/{registry_id}", status_code=204)
async def delete_registry(
    registry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> None:
    """删除全局 backend。running prediction job 仍在跑则 409; 否则级联解绑
    projects.ml_backend_pool_id / project_ml_backend_pool (CASCADE) / 历史 prediction (SET NULL)。
    v0.23.3 ADR-0050 · 删除前须先 drain + inflight=0 + GPU retirement + 成员移除
    (legacy_instance_id FK RESTRICT + member FK RESTRICT)。"""
    from app.services.ml_routing.contracts import MLBackendQuiescenceError

    admin = await IdentityCommandService(db).lock_platform_super_admin(
        admin.id, nowait=False
    )

    try:
        ok = await delete_registry_command(db, registry_id)
    except MLBackendQuiescenceError as exc:
        raise HTTPException(
            status_code=503 if exc.unavailable else 409, detail=exc.as_detail()
        ) from exc
    except MLBackendDeleteBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail=f"该 backend 上仍有 {exc.running_jobs} 个运行中的预标任务, 无法删除",
        ) from exc
    except GPUBackendManagedMutationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "gpu_backend_retirement_required",
                "message": str(exc),
            },
        ) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_registry.deleted",
        target_type="ml_backend",
        target_id=str(registry_id),
        request=request,
        status_code=204,
        detail={},
    )
    await db.commit()


@router.post("/registry/{registry_id}/health", response_model=MLBackendHealthResponse)
async def check_registry_health(
    registry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> MLBackendHealthResponse:
    """对全局 backend 探活 + 落能力快照 (与项目作用域 health 同逻辑, 无项目启用前置)。"""
    svc = MLBackendService(db)
    backend = await svc.get_registry(registry_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    healthy = await backend_control_service(db).check_health(registry_id)
    await db.commit()
    backend = await svc.get_registry(registry_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    return MLBackendHealthResponse(
        status="ok" if healthy else "error",
        backend_id=backend.id,
        backend_name=backend.name,
    )


@router.post(
    "/registry/{registry_id}/unload",
    response_model=MLBackendUnloadResponse,
)
async def unload_registry_backend(
    registry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> MLBackendUnloadResponse:
    """全局卸载 backend，不要求它已被任一项目启用。"""

    from app.services.ml_routing.contracts import MLBackendQuiescenceError
    from app.services.ml_routing.safety import require_registry_quiescent

    control = backend_control_service(db)
    try:
        topology = await require_registry_quiescent(db, registry_id)
        result = await control.unload(registry_id, expected_topology=topology)
    except MLBackendQuiescenceError as exc:
        raise HTTPException(
            status_code=503 if exc.unavailable else 409, detail=exc.as_detail()
        ) from exc
    except GPUArbiterDispatchError as exc:
        raise _gpu_dispatch_http_error(exc) from exc
    except BackendControlOperationError as exc:
        raise _control_http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"backend unload failed: {exc}"
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_backend.unloaded",
        target_type="ml_backend",
        target_id=str(registry_id),
        request=request,
        status_code=200,
        detail={"project_id": None, "result": result},
    )
    await db.commit()
    return MLBackendUnloadResponse.model_validate(result)


# ─── v0.10.26 · 容器直连观测 (与项目注册解耦) ──────────────────────────────
#
# 即使没有任何项目注册 backend, 运维也能直连 env 配的 ML_BACKEND_OBSERVE_URLS
# Read-only discovery uses registered authentication when present. Only deployment-
# pinned, unregistered first-party targets may use an existing local credential.


def _observe_urls() -> list[str]:
    """配置的观测 URL; 留空时回退 [ml_backend_default_url] (若非空)。去重保序。"""
    urls = list(settings.ml_backend_observe_urls or [])
    if not urls and settings.ml_backend_default_url:
        urls = [settings.ml_backend_default_url]
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = u.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


class VariantCatalog(BaseModel):
    sam_variant: list[str] = []
    dino_variant: list[str] = []


class ObserveTarget(BaseModel):
    url: str
    ok: bool
    latency_ms: int
    status_code: int | None = None
    error: str | None = None
    gpu_info: dict | None = None
    model_version: str | None = None
    pool: dict | None = None
    video_pool: dict | None = None  # v0.10.36 · 视频追踪显存池
    cache: dict | None = None
    compute: ComputeInfo | None = None
    residency: ResidencyInfo | dict | None = None
    variant_catalog: VariantCatalog | None = None
    supported_variants: list[dict] = []
    supported_trackers: list[
        str
    ] = []  # v0.10.36 · /setup 暴露的 video tracker model_key 列表
    supports_variants: bool = False
    registered: bool = False
    registered_label: str | None = None


class ObserveResponse(BaseModel):
    targets: list[ObserveTarget]
    configured_count: int


def _extract_variant_catalog(setup: dict | None) -> VariantCatalog | None:
    """从 /setup.params 的 enum 字段抽变体目录 (sam_variant / dino_variant)。"""
    if not setup:
        return None
    props = ((setup.get("params") or {}).get("properties")) or {}
    sam = props.get("sam_variant", {}).get("enum") or []
    dino = props.get("dino_variant", {}).get("enum") or []
    if not sam and not dino:
        return None
    return VariantCatalog(sam_variant=list(sam), dino_variant=list(dino))


def _extract_supported_variants(setup: dict | None) -> list[dict]:
    """Extract v2 generic variant axes from /setup without changing their shape.

    优先顶层 supported_variants (gsam2 等把变体挂在顶层); 顶层为空时回落到各 model 的
    supported_variants (rapidocr 等 v2 backend 把变体挂在 models[].supported_variants 上),
    按 axis key 去重合并。否则 supports_variants 会误判为 False, 运行时观测面板对这类
    容器错显「该容器不暴露变体目录」。
    """
    if not setup:
        return []
    groups = setup.get("supported_variants") or []
    if isinstance(groups, list) and groups:
        return list(groups)
    merged: dict[str, dict] = {}
    for model in setup.get("models") or []:
        if not isinstance(model, dict):
            continue
        for group in model.get("supported_variants") or []:
            key = group.get("key") if isinstance(group, dict) else None
            if key and key not in merged:
                merged[key] = group
    return list(merged.values())


def _probe_one(fact) -> ObserveTarget:
    """Project one fixed, typed environment observation into the public response."""
    health = fact.health
    if not fact.ok or not isinstance(health, dict):
        return ObserveTarget(
            url=fact.url,
            ok=False,
            latency_ms=fact.latency_ms,
            status_code=fact.status_code,
            error=fact.error
            or (
                f"HTTP {fact.status_code}"
                if fact.status_code is not None and fact.status_code != 200
                else "响应 JSON 格式无效"
            ),
        )

    setup = fact.setup
    catalog = _extract_variant_catalog(setup)
    supported_variants = _extract_supported_variants(setup)
    return ObserveTarget(
        url=fact.url,
        ok=bool(health.get("ok", True)),
        latency_ms=fact.latency_ms,
        status_code=fact.status_code or 200,
        gpu_info=health.get("gpu_info"),
        model_version=health.get("model_version"),
        pool=health.get("pool"),
        video_pool=health.get("video_pool"),
        cache=health.get("cache"),
        compute=health.get("compute"),
        residency=health.get("residency"),
        variant_catalog=catalog,
        supported_variants=supported_variants,
        supports_variants=catalog is not None or bool(supported_variants),
        supported_trackers=list((setup or {}).get("supported_trackers") or []),
    )


@router.get("/observe", response_model=ObserveResponse)
async def observe_backends(
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ObserveResponse:
    """直连观测 env 配的后端容器: 并发探测各 URL 的 /health + /setup, 标注是否已被注册。"""
    urls = _observe_urls()
    if not urls:
        return ObserveResponse(targets=[], configured_count=0)

    endpoints = tuple(BackendRequestOnlyEndpoint(url=url) for url in urls)
    control = backend_control_service(db)
    reply = await control.environment_observation(
        endpoints, timeout_seconds=settings.ml_health_timeout
    )
    targets = [_probe_one(fact) for fact in reply.results]
    labels = await control.registration_labels(urls)
    source_by_url = {label.url.rstrip("/"): label.source for label in labels}

    for target in targets:
        source = source_by_url.get(target.url.rstrip("/"))
        if source:
            target.registered = True
            target.registered_label = f"已注册 ({source})"

    return ObserveResponse(targets=targets, configured_count=len(urls))


class SmokeTestRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500)
    sam_variant: str | None = None
    dino_variant: str | None = None
    variant: dict | None = None


class SmokeTestResponse(BaseModel):
    ok: bool
    skipped: bool = False
    reloaded: bool | None = None
    auto_unloaded: bool = False
    load_latency_ms: int | None = None
    loaded_variant: dict | None = None
    message: str
    error: str | None = None


def _smoke_loaded_state(
    health: dict,
) -> tuple[bool, dict | None, str]:
    raw_pool = health.get("pool")
    pool_valid = raw_pool is None or isinstance(raw_pool, dict)
    pool = raw_pool if isinstance(raw_pool, dict) else {}
    raw_loaded_keys = pool.get("loaded_keys")
    raw_loaded_variants = pool.get("loaded_variants")
    loaded_keys_valid = raw_loaded_keys is None or isinstance(raw_loaded_keys, list)
    loaded_variants_valid = raw_loaded_variants is None or isinstance(
        raw_loaded_variants, list
    )
    loaded_keys = raw_loaded_keys if isinstance(raw_loaded_keys, list) else []
    loaded_variants = (
        raw_loaded_variants if isinstance(raw_loaded_variants, list) else []
    )
    current_size = pool.get("current_size")
    if current_size is None:
        current_size = len(loaded_keys) or len(loaded_variants)
        current_size_valid = True
    else:
        current_size_valid = (
            isinstance(current_size, int)
            and not isinstance(current_size, bool)
            and current_size >= 0
        )
        if not current_size_valid:
            current_size = 0

    raw_video_pool = health.get("video_pool")
    video_pool_valid = raw_video_pool is None or isinstance(raw_video_pool, dict)
    video_pool = raw_video_pool if isinstance(raw_video_pool, dict) else {}
    raw_video_loaded_keys = video_pool.get("loaded_keys")
    raw_video_loaded_variants = video_pool.get("loaded_variants")
    video_loaded_keys_valid = raw_video_loaded_keys is None or isinstance(
        raw_video_loaded_keys, list
    )
    video_loaded_variants_valid = raw_video_loaded_variants is None or isinstance(
        raw_video_loaded_variants, list
    )
    video_loaded_keys = (
        raw_video_loaded_keys if isinstance(raw_video_loaded_keys, list) else []
    )
    video_loaded_variants = (
        raw_video_loaded_variants if isinstance(raw_video_loaded_variants, list) else []
    )
    video_current_size = video_pool.get("current_size")
    if video_current_size is None:
        video_current_size = len(video_loaded_keys) or len(video_loaded_variants)
        video_current_size_valid = True
    else:
        video_current_size_valid = (
            isinstance(video_current_size, int)
            and not isinstance(video_current_size, bool)
            and video_current_size >= 0
        )
        if not video_current_size_valid:
            video_current_size = 0
    active_sessions = video_pool.get("active_sessions")
    active_sessions_valid = active_sessions is None or (
        isinstance(active_sessions, int)
        and not isinstance(active_sessions, bool)
        and active_sessions >= 0
    )
    active_sessions = active_sessions if active_sessions_valid else 0
    active_sessions = active_sessions or 0
    loaded_flag = health.get("loaded")
    loaded_flag_valid = loaded_flag is None or isinstance(loaded_flag, bool)
    legacy_evidence_valid = all(
        (
            pool_valid,
            loaded_keys_valid,
            loaded_variants_valid,
            current_size_valid,
            video_pool_valid,
            video_loaded_keys_valid,
            video_loaded_variants_valid,
            video_current_size_valid,
            active_sessions_valid,
            loaded_flag_valid,
        )
    ) and (
        isinstance(loaded_flag, bool)
        or raw_pool is not None
        or raw_video_pool is not None
    )

    residency_present = "residency" in health
    residency_loaded = strict_gpu_loaded_evidence(health)
    if residency_present:
        was_loaded = residency_loaded is not False
    else:
        was_loaded = (
            not legacy_evidence_valid
            or loaded_flag is True
            or current_size > 0
            or video_current_size > 0
            or active_sessions > 0
        )
    if not was_loaded:
        return False, None, ""

    first_display: dict | None = None
    if loaded_keys:
        first_key = loaded_keys[0]
        raw_key = (
            first_key.get("key") or ""
            if isinstance(first_key, dict)
            else str(first_key or "")
        )
        parsed = _parse_gsam2_image_key(raw_key) if raw_key else None
        first_display = parsed or ({"key": raw_key} if raw_key else None)
    elif loaded_variants:
        first_variant = loaded_variants[0]
        first_display = (
            first_variant
            if isinstance(first_variant, dict)
            else {"key": str(first_variant)}
        )
    display_payload = first_display if first_display else loaded_variants
    if residency_present:
        occupancy = (
            "residency 报告 GPU 仍驻留"
            if residency_loaded is True
            else "residency 无法严格证明 GPU 已空"
        )
    elif video_current_size > 0 or active_sessions > 0:
        occupancy = "视频模型或会话仍驻留"
    elif not legacy_evidence_valid:
        occupancy = "旧协议驻留字段格式不可识别，无法证明 GPU 已空"
    else:
        occupancy = f"容器已有变体常驻（{display_payload}）"
    return True, first_display, occupancy


@router.post("/observe/smoke-test", response_model=SmokeTestResponse)
async def smoke_test_backend(
    payload: SmokeTestRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> SmokeTestResponse:
    """Try a registered or explicitly unregistered backend without proxying arbitrary requests."""
    _validate_probe_base_url(payload.url)
    base = payload.url.rstrip("/")
    registered_backend = await MLBackendService(db).get_by_url(base)
    await db.commit()
    variant = {"sam_variant": payload.sam_variant, "dino_variant": payload.dino_variant}
    generic_variant = payload.variant or None
    audit_detail: dict = {"url": base, **variant, "variant": generic_variant}
    endpoint = BackendRequestOnlyEndpoint(url=base)
    control = backend_control_service(db)

    async def _audit(result: SmokeTestResponse) -> None:
        await AuditService.log(
            db,
            actor=admin,
            action="ml_backend.smoke_tested",
            target_type="ml_backend",
            target_id=base,
            request=request,
            status_code=200,
            detail={
                **audit_detail,
                "ok": result.ok,
                "skipped": result.skipped,
                "auto_unloaded": result.auto_unloaded,
            },
        )
        await db.commit()

    if generic_variant:
        result = SmokeTestResponse(
            ok=True,
            skipped=True,
            message="该容器未声明通用 warm 接口",
            loaded_variant=generic_variant,
        )
        await _audit(result)
        return result

    try:
        if registered_backend is not None:
            healthy, health = await control.health_meta(registered_backend.id)
            if not healthy:
                raise RuntimeError("registered backend /health 不可达")
            health = health or {}
        else:
            fact = await control.manual_probe(
                endpoint, timeout_seconds=settings.ml_health_timeout
            )
            if fact.status_code != 200 or fact.error is not None:
                result = SmokeTestResponse(
                    ok=False,
                    message="试启动失败：/health 不可达",
                    error=fact.error
                    or (
                        f"HTTP {fact.status_code}"
                        if fact.status_code is not None
                        else "连接失败"
                    ),
                )
                await _audit(result)
                return result
            if fact.health is None:
                result = SmokeTestResponse(
                    ok=True,
                    skipped=True,
                    message="容器 /health 格式不可识别，无法证明 GPU 已空；未执行试启动。",
                )
                await _audit(result)
                return result
            health = fact.health
    except GPUArbiterDispatchError as exc:
        raise _gpu_dispatch_http_error(exc) from exc
    except BackendControlOperationError as exc:
        raise _control_http_error(exc) from exc
    except Exception as exc:
        result = SmokeTestResponse(
            ok=False,
            message="试启动失败：/health 不可达",
            error=_safe_probe_error(exc),
        )
        await _audit(result)
        return result

    if not isinstance(health, dict):
        result = SmokeTestResponse(
            ok=True,
            skipped=True,
            message="容器 /health 格式不可识别，无法证明 GPU 已空；未执行试启动。",
        )
        await _audit(result)
        return result

    was_loaded, first_display, occupancy = _smoke_loaded_state(health)
    if was_loaded:
        result = SmokeTestResponse(
            ok=True,
            skipped=True,
            loaded_variant=first_display,
            message=f"{occupancy}；为避免驱逐在用模型，未执行试启动。",
        )
        await _audit(result)
        return result

    if registered_backend is None:
        try:
            gpu_diagnostics = await gpu_authority_client().get_diagnostics()
        except GPUArbiterDispatchError as exc:
            raise _gpu_dispatch_http_error(exc) from exc
        resources = gpu_diagnostics.result.get("resources")
        if not isinstance(resources, list) or any(
            not isinstance(resource, dict)
            or resource.get("effective_mode") not in {"off", "observe", "enforce"}
            for resource in resources
        ):
            raise _gpu_dispatch_http_error(
                GPUArbiterDispatchError(GPUArbiterErrorCode.UNAVAILABLE)
            )
        if any(resource["effective_mode"] == "enforce" for resource in resources):
            raise _gpu_dispatch_http_error(
                GPUArbiterDispatchError(
                    GPUArbiterErrorCode.CONFIG_INVALID,
                    message=(
                        "effective enforce 下未注册 URL 只能执行只读 health/setup；"
                        "请先注册 backend 并完成受管身份绑定"
                    ),
                )
            )

    body = {key: value for key, value in variant.items() if value}
    load_started = time.monotonic()
    auto_unloaded = False
    try:
        if registered_backend is not None:
            reload_data = await control.reload(
                registered_backend.id,
                sam_variant=payload.sam_variant,
                dino_variant=payload.dino_variant,
            )
            if reload_data is None:
                raise ValueError("registered backend unavailable")
            load_latency_ms = int((time.monotonic() - load_started) * 1000)
        else:
            shadow_settings = GPUAuthoritySettings(
                gpu_arbiter_mode=GPUArbiterMode(settings.gpu_arbiter_mode.value)
            )
            record_unregistered_gpu_shadow_dispatch(
                base, "reload", config=shadow_settings
            )
            record_unregistered_gpu_shadow_dispatch(
                base, "unload", config=shadow_settings
            )
            smoke_result = await control.smoke_unregistered(endpoint, body)
            if (
                set(smoke_result) != {"reload", "auto_unloaded", "load_latency_ms"}
                or not isinstance(smoke_result["reload"], dict)
                or type(smoke_result["auto_unloaded"]) is not bool
                or type(smoke_result["load_latency_ms"]) is not int
                or smoke_result["load_latency_ms"] < 0
            ):
                raise ValueError("invalid unregistered smoke response")
            reload_data = smoke_result["reload"]
            auto_unloaded = smoke_result["auto_unloaded"]
            load_latency_ms = smoke_result["load_latency_ms"]
    except GPUArbiterDispatchError as exc:
        raise _gpu_dispatch_http_error(exc) from exc
    except BackendControlOperationError as exc:
        raise _control_http_error(exc) from exc
    except Exception as exc:
        result = SmokeTestResponse(
            ok=False,
            message="试启动失败：模型未能加载",
            error=_safe_probe_error(exc),
        )
        await _audit(result)
        return result

    if registered_backend is not None:
        try:
            auto_unloaded = (await control.unload(registered_backend.id)) is not None
        except Exception:
            auto_unloaded = False

    loaded_variant = {
        "sam_variant": reload_data.get("sam_variant"),
        "dino_variant": reload_data.get("dino_variant"),
    }
    result = SmokeTestResponse(
        ok=True,
        reloaded=reload_data.get("reloaded"),
        auto_unloaded=auto_unloaded,
        load_latency_ms=load_latency_ms,
        loaded_variant=loaded_variant,
        message=(
            f"试启动成功（加载 {load_latency_ms}ms）"
            + (
                "，已发送自动卸载请求；请以 residency 确认显存释放。"
                if auto_unloaded
                else "，但自动卸载未确认；请检查 residency 或 idle 卸载。"
            )
        ),
    )
    await _audit(result)
    return result


class ServicePoolCreateRequest(BaseModel):
    name: str
    legacy_instance_id: UUID | None = None


class ServicePoolPatchRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None


class ServicePoolMemberPutRequest(BaseModel):
    weight: int = Field(default=1, ge=1, le=100)


class CapabilityDriftAcceptRequest(BaseModel):
    expected_candidate_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    enable_pool: bool = True


class CapabilityDriftPreviewResponse(BaseModel):
    pool_id: UUID
    registry_id: UUID
    member_state: str
    pool_enabled: bool
    pool_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    differing_fields: list[str] = Field(default_factory=list)
    has_drift: bool
    can_accept: bool
    blocking_members: list[UUID] = Field(default_factory=list)


class ServicePoolAdminItem(BaseModel):
    id: UUID
    name: str
    enabled: bool
    routing_policy: str
    legacy_instance_id: UUID | None = None
    routing_generation: int
    capability_fingerprint: str | None = None
    members: list["ServicePoolMemberItem"] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ServicePoolMemberItem(BaseModel):
    registry_id: UUID
    registry_name: str
    traffic_state: str
    weight: int


ServicePoolAdminItem.model_rebuild()


async def _pool_to_admin_item(db: AsyncSession, pool) -> ServicePoolAdminItem:
    record = to_record(pool)
    members = await InferenceTopologyData(db).pool_members(pool.id)
    member_records = [to_record(member) for member in members]
    return ServicePoolAdminItem(
        id=record["id"],
        name=record["name"],
        enabled=record["enabled"],
        routing_policy=record["routing_policy"],
        legacy_instance_id=record["legacy_instance_id"],
        routing_generation=record["routing_generation"],
        capability_fingerprint=record["capability_fingerprint"],
        members=[
            ServicePoolMemberItem(
                registry_id=member["registry_id"],
                registry_name=member["registry"]["name"],
                traffic_state=member["traffic_state"],
                weight=member["weight"],
            )
            for member in member_records
        ],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


@router.get(
    "/service-pools",
    response_model=list[ServicePoolAdminItem],
)
async def list_service_pools(
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> list[ServicePoolAdminItem]:
    pools = await MLBackendService(db).list_pools()
    return [await _pool_to_admin_item(db, p) for p in pools]


@router.post("/service-pools", response_model=ServicePoolAdminItem, status_code=201)
async def create_service_pool(
    data: ServicePoolCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    svc = MLBackendService(db)
    try:
        pool = await svc.create_pool(
            data.name, legacy_instance_id=data.legacy_instance_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.created",
        target_type="ml_service_pool",
        target_id=str(pool.id),
        request=request,
        status_code=201,
        detail={"name": data.name},
    )
    await db.commit()
    pool = await svc.get_pool(pool.id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.get("/service-pools/{pool_id}", response_model=ServicePoolAdminItem)
async def get_service_pool(
    pool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    pool = await MLBackendService(db).get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.patch("/service-pools/{pool_id}", response_model=ServicePoolAdminItem)
async def patch_service_pool(
    pool_id: uuid.UUID,
    data: ServicePoolPatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    svc = MLBackendService(db)
    try:
        pool = await svc.update_pool(pool_id, name=data.name, enabled=data.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.updated",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"name": data.name, "enabled": data.enabled},
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.delete("/service-pools/{pool_id}", status_code=204)
async def delete_service_pool(
    pool_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> None:
    svc = MLBackendService(db)
    try:
        ok = await svc.delete_pool(pool_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="service pool not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.deleted",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=204,
        detail={},
    )
    await db.commit()


@router.put(
    "/service-pools/{pool_id}/members/{registry_id}",
    response_model=ServicePoolAdminItem,
)
async def add_or_update_pool_member(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    data: ServicePoolMemberPutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    from app.services.ml_routing.contracts import CapabilityMismatchError

    svc = MLBackendService(db)
    try:
        await svc.add_pool_member(pool_id, registry_id, weight=data.weight)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CapabilityMismatchError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except MLBackendPoolMemberConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_pool_member_conflict",
                "message": str(exc),
                "current_pool_id": str(exc.pool_id),
            },
        ) from exc
    except MLBackendPoolCapabilityUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_pool_capability_unavailable",
                "message": str(exc),
            },
        ) from exc
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.member_upserted",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"registry_id": str(registry_id), "weight": data.weight},
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.delete(
    "/service-pools/{pool_id}/members/{registry_id}",
    response_model=ServicePoolAdminItem,
)
async def remove_pool_member(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    from app.services.ml_routing.contracts import MLBackendQuiescenceError
    from app.services.ml_routing.safety import require_registry_quiescent

    svc = MLBackendService(db)
    try:
        topology = await require_registry_quiescent(db, registry_id)
        ok = await svc.remove_pool_member(
            pool_id, registry_id, expected_topology=topology
        )
    except MLBackendQuiescenceError as exc:
        raise HTTPException(
            status_code=503 if exc.unavailable else 409, detail=exc.as_detail()
        ) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="member not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.member_removed",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"registry_id": str(registry_id)},
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.post(
    "/service-pools/{pool_id}/members/{registry_id}/drain",
    response_model=ServicePoolAdminItem,
)
async def drain_pool_member(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    svc = MLBackendService(db)
    member = await svc.drain_pool_member(pool_id, registry_id)
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.member_drained",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"registry_id": str(registry_id)},
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.post(
    "/service-pools/{pool_id}/members/{registry_id}/resume",
    response_model=ServicePoolAdminItem,
)
async def resume_pool_member(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    svc = MLBackendService(db)
    try:
        member = await svc.resume_pool_member(pool_id, registry_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if member is None:
        raise HTTPException(status_code=404, detail="member not found")
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.member_resumed",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"registry_id": str(registry_id)},
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


@router.get(
    "/service-pools/{pool_id}/members/{registry_id}/capability-drift",
    response_model=CapabilityDriftPreviewResponse,
)
async def preview_pool_member_capability_drift(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> CapabilityDriftPreviewResponse:
    preview = await MLBackendService(db).preview_capability_drift(pool_id, registry_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="service pool member not found")
    record = to_record(preview)
    return CapabilityDriftPreviewResponse(
        pool_id=record["pool_id"],
        registry_id=record["registry_id"],
        member_state=record["member_state"],
        pool_enabled=record["pool_enabled"],
        pool_fingerprint=record["pool_fingerprint"],
        candidate_fingerprint=record["candidate_fingerprint"],
        differing_fields=record["differing_fields"],
        has_drift=record["has_drift"],
        can_accept=record["can_accept"],
        blocking_members=record["blocking_member_ids"],
    )


@router.post(
    "/service-pools/{pool_id}/members/{registry_id}/capability-drift/accept",
    response_model=ServicePoolAdminItem,
)
async def accept_pool_member_capability_drift(
    pool_id: uuid.UUID,
    registry_id: uuid.UUID,
    data: CapabilityDriftAcceptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> ServicePoolAdminItem:
    svc = MLBackendService(db)
    if await svc.preview_capability_drift(pool_id, registry_id) is None:
        raise HTTPException(status_code=404, detail="service pool member not found")
    healthy = await backend_control_service(db).check_health(registry_id)
    if not healthy:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_capability_revalidation_failed",
                "message": "backend health and capability refresh failed",
            },
        )
    try:
        accepted = await svc.accept_capability_drift(
            pool_id,
            registry_id,
            expected_candidate_fingerprint=data.expected_candidate_fingerprint,
            enable_pool=data.enable_pool,
        )
    except MLBackendCapabilityRevalidationError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
                "blocking_members": [str(item) for item in exc.blocking_member_ids],
            },
        ) from exc
    if accepted is None:
        raise HTTPException(status_code=404, detail="service pool member not found")
    pool, acceptance = accepted
    await AuditService.log(
        db,
        actor=admin,
        action="ml_service_pool.capability_drift_accepted",
        target_type="ml_service_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={
            "registry_id": str(registry_id),
            "old_fingerprint": acceptance.old_fingerprint,
            "new_fingerprint": acceptance.new_fingerprint,
            "differing_fields": list(acceptance.differing_fields),
            "pool_enabled": acceptance.pool_enabled,
        },
    )
    await db.commit()
    pool = await svc.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    return await _pool_to_admin_item(db, pool)


# ── v0.23.3 ADR-0050 §12.3 · topology / runtime-snapshot (v0.23.4 read model) ──


@router.get("/topology", response_model=TopologyResponse)
async def get_topology(
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
) -> TopologyResponse:
    """Pool/member topology, role-scoped (§12.3). Super Admin sees full member detail
    + health + GPU; Project Admin sees a trimmed summary (routing_policy/weight/state
    projected to ``unknown``/``None`` server-side per plan Appendix A.6)."""
    from app.services.ml_routing.diagnostics import build_topology

    # role may be stored as enum or string; normalize to compare against SUPER_ADMIN.
    role_val = admin.role.value if hasattr(admin.role, "value") else admin.role
    return await build_topology(
        db, super_admin=(role_val == UserRole.SUPER_ADMIN.value)
    )


@router.get("/runtime-snapshot", response_model=RuntimeSnapshotResponse)
async def get_runtime_snapshot(
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> RuntimeSnapshotResponse:
    """Super Admin runtime observations with per-source freshness.

    Execution counts come from the durable routing owner in every mode; failed
    owner reads remain unknown. Redis circuit hints are independently best effort.
    """
    from app.services.ml_routing.diagnostics import build_runtime_snapshot

    ledger = None
    if settings.ml_backend_router_mode != "off":
        try:
            from app.services.ml_routing.router import make_ledger_from_settings

            ledger = make_ledger_from_settings()
        except Exception:  # noqa: BLE001 — snapshot must not fail on Redis issues
            ledger = None
    try:
        return await build_runtime_snapshot(db, ledger)
    finally:
        if ledger is not None:
            try:
                await ledger.aclose()
            except Exception:
                # Snapshot data already records Redis freshness; a client-close
                # failure must not replace that typed partial response with 500.
                pass
