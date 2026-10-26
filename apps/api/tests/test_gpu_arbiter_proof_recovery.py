from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import time
import uuid

import asyncpg
import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    ManagedLifecycleCapabilities,
    managed_lifecycle_capability_sha256,
    verify_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from redis.asyncio import Redis
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from aap_inference.backend_endpoint import BackendEndpointSnapshot
from app.config import settings
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_cancel_intent import GPUBackendCancelIntent
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.gpu import proofs as gpu_proofs_service
from aap_inference.gpu.contracts import GPUDispatchRequest
from aap_inference.gpu.fences import (
    activate_gpu_backend_membership,
    record_gpu_backend_token_expiry,
)
from aap_inference.gpu.proofs import (
    GPUBusyEvictionRuntimeSubjectError,
    GPUColdRuntimeSubjectError,
    GPUEvictionCancelRuntimeSubjectError,
    GPUIdleEvictionRuntimeSubjectError,
    GPUPreparedColdRuntimeSubject,
    GPUPreparedIdleEvictionRuntimeSubject,
    GPUResidentRuntimeSubjectError,
    commit_gpu_cold_terminal_from_health,
    commit_gpu_eviction_cancel_from_health,
    commit_gpu_eviction_phase_from_health,
    prepare_gpu_cold_runtime_generation,
    prepare_gpu_eviction_cancel_runtime_generation,
    prepare_gpu_idle_eviction_runtime_generation,
    record_gpu_resident_runtime_token_expiry,
)
from aap_inference.gpu.reconciliation import (
    commit_gpu_proof_reset_from_health,
    repair_gpu_resource,
)
from aap_inference.gpu.retirement import collect_gpu_backend_tombstone
from aap_inference.gpu.dispatch import build_gpu_dispatch_context_factory
from aap_inference.gpu.collector_database import (
    GPUCollectorDatabase,
    validate_gpu_collector_role_boundary,
)
from aap_inference.gpu.native import GPUAuthorityPoolData
from aap_inference.gpu.ledger import (
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUBackendDomainMember,
    GPUProofResetContext,
    GPUReconcileResult,
)
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    GPUResourceSettings,
)

_RESOURCE_A = "node-proof/GPU-a"
_RESOURCE_B = "node-proof/GPU-b"


def _proof_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _resource_config(
    *resource_ids: str,
    allocatable_mb: int = 8192,
) -> GPUAuthoritySettings:
    resources = {
        resource_id: GPUResourceSettings(
            node_id=resource_id.split("/", 1)[0],
            physical_device_token=resource_id.split("/", 1)[1],
            allocatable_mb=allocatable_mb,
            mode=GPUArbiterMode.OFF,
        )
        for resource_id in resource_ids
    }
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_resources=resources,
        redis_url=settings.redis_url,
    )


def _redis_url() -> str:
    return os.environ.get("TEST_REDIS_URL", settings.redis_url)


def _gpu_role_url(test_engine: AsyncEngine, env_name: str, role: str) -> str:
    raw = os.environ.get(env_name)
    if not raw:
        pytest.fail("the module-data verifier did not supply its fixture URLs")
    try:
        parsed = make_url(raw)
    except Exception:
        pytest.fail("the GPU Native role URL is invalid")
    owner = test_engine.url
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.username != role
        or parsed.host != "127.0.0.1"
        or parsed.port != owner.port
        or parsed.database != owner.database
    ):
        pytest.fail("the GPU Native role URL does not match the isolated test DB")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


class _GPUAuthoritySessionProvider:
    """Keep fixture-owner ORM UOWs separate from authority Native transactions."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authority_data: GPUAuthorityPoolData,
        authority_pool: asyncpg.Pool,
    ) -> None:
        self._sessions = sessions
        self._authority_data = authority_data
        self._authority_pool = authority_pool

    def __call__(self, *args, **kwargs) -> AsyncSession:
        return self._sessions(*args, **kwargs)

    def begin(self):
        return self._sessions.begin()

    @asynccontextmanager
    async def transaction(self):
        async with self._authority_data.transaction() as data:
            yield data

    async def aclose(self) -> None:
        await self._authority_pool.close()


async def _open_gpu_authority_provider(
    test_engine: AsyncEngine,
) -> _GPUAuthoritySessionProvider:
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    role_url = _gpu_role_url(
        test_engine,
        "AAP_MODULE_DATA_INFERENCE_GPU_URL",
        "inference_gpu_authority_client",
    )
    pool = await asyncpg.create_pool(dsn=role_url, min_size=1, max_size=4)
    provider = _GPUAuthoritySessionProvider(
        sessions,
        GPUAuthorityPoolData(pool),
        pool,
    )
    try:
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT current_user") == (
                "inference_gpu_authority_client"
            )
            assert await connection.fetchval("SELECT session_user") == (
                "inference_gpu_authority_client"
            )
        async with provider.transaction():
            pass
    except BaseException:
        await provider.aclose()
        raise
    return provider


@pytest.fixture
async def gpu_authority_provider(
    test_engine: AsyncEngine,
) -> AsyncIterator[_GPUAuthoritySessionProvider]:
    provider = await _open_gpu_authority_provider(test_engine)
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
async def gpu_collector_database(
    test_engine: AsyncEngine,
) -> AsyncIterator[GPUCollectorDatabase]:
    role_url = _gpu_role_url(
        test_engine,
        "AAP_MODULE_DATA_INFERENCE_GPU_COLLECTOR_URL",
        "inference_gpu_collector_client",
    )
    pool = await asyncpg.create_pool(dsn=role_url, min_size=1, max_size=4)
    try:
        async with pool.acquire() as connection:
            collector_role = await connection.fetchval("SELECT current_user")
            assert collector_role == "inference_gpu_collector_client"
            assert await connection.fetchval("SELECT session_user") == (
                "inference_gpu_collector_client"
            )
        application_role, collector_role = validate_gpu_collector_role_boundary(
            "inference_gpu_authority_client",
            collector_role,
        )
        database = GPUCollectorDatabase(
            pool=pool,
            application_role=application_role,
            collector_role=collector_role,
        )
        async with database.transaction():
            pass
        yield database
    finally:
        await pool.close()


async def _record_token_expiry(
    provider: _GPUAuthoritySessionProvider,
    backend_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    token_expires_at: datetime,
) -> datetime:
    async with provider.transaction() as data:
        return await record_gpu_backend_token_expiry(
            data,
            backend_id,
            gpu_resource_id=gpu_resource_id,
            membership_epoch=membership_epoch,
            token_expires_at=token_expires_at,
        )


def _native_read_operation(name: str):
    async def invoke(source: _GPUAuthoritySessionProvider, *args, **kwargs):
        async with source.transaction() as data:
            return await getattr(gpu_proofs_service, name)(data, *args, **kwargs)

    return invoke


read_gpu_busy_eviction_runtime_subject = _native_read_operation(
    "read_gpu_busy_eviction_runtime_subject"
)
read_gpu_cold_runtime_subject = _native_read_operation("read_gpu_cold_runtime_subject")
read_gpu_eviction_cancel_runtime_subject = _native_read_operation(
    "read_gpu_eviction_cancel_runtime_subject"
)
read_gpu_eviction_drain_health = _native_read_operation(
    "read_gpu_eviction_drain_health"
)
read_gpu_idle_eviction_runtime_subject = _native_read_operation(
    "read_gpu_idle_eviction_runtime_subject"
)
read_gpu_resident_runtime_subject = _native_read_operation(
    "read_gpu_resident_runtime_subject"
)


class _ProofEndpointSource:
    def __init__(self, factory: _GPUAuthoritySessionProvider) -> None:
        self._factory = factory

    async def open_owned(
        self,
        backend_registry_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        purpose: str,
        binding=None,
    ) -> BackendEndpointSnapshot:
        async with self._factory() as db:
            backend = await db.get(MLBackendRegistry, backend_registry_id)
        if backend is None:
            raise RuntimeError("test backend endpoint is unavailable")
        return BackendEndpointSnapshot(
            registry_id=backend.id,
            request_id=request_id,
            purpose=purpose,
            authority=None,
            endpoint_sha256=hashlib.sha256(backend.url.encode()).hexdigest(),
            url=backend.url,
            auth_method=None,
            auth_token=None,
            gpu_resource_id=backend.gpu_resource_id,
            vram_budget_mb=backend.vram_budget_mb,
            eviction_priority=backend.eviction_priority,
            extra_params=backend.extra_params,
        )

    async def readiness(self) -> bool:
        return True


class _UnusedBackendControlTransport:
    @asynccontextmanager
    async def control_binding(self, _registry_id, *, request_id, operation):
        yield None

    async def health_meta(self, *args, **kwargs):
        raise AssertionError("unexpected backend health transport")

    async def lifecycle_mode(self, *args, **kwargs):
        raise AssertionError("unexpected backend mode transport")

    async def lifecycle_reset(self, *args, **kwargs):
        raise AssertionError("unexpected backend reset transport")

    async def lifecycle_drain(self, *args, **kwargs):
        raise AssertionError("unexpected backend drain transport")

    async def lifecycle_cancel_drain(self, *args, **kwargs):
        raise AssertionError("unexpected backend cancel transport")

    async def lifecycle_unload(self, *args, **kwargs):
        raise AssertionError("unexpected backend unload transport")


class _ProofDispatchClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


async def _refresh_test_health(_backend_id: uuid.UUID, _challenge: str) -> bool:
    return True


async def _clean_redis_namespace(redis_url: str, namespace: str) -> None:
    client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry_on_timeout=False,
    )
    try:
        async with asyncio.timeout(5):
            keys = [key async for key in client.scan_iter(match=f"{namespace}:*")]
            if keys:
                await client.unlink(*keys)
    finally:
        async with asyncio.timeout(2):
            await client.aclose()


@pytest.fixture
async def proof_store() -> AsyncIterator[GPUArbiterStore]:
    redis_url = _redis_url()
    namespace = f"gpu-proof-consumer-test:{uuid.uuid4().hex}"
    store = GPUArbiterStore.from_url(redis_url, namespace=namespace)
    try:
        assert await store.ping() is True
        yield store
    finally:
        await store.aclose()
        await _clean_redis_namespace(redis_url, namespace)


async def _create_active_backends(
    test_engine: AsyncEngine,
    *,
    resource_id: str,
    budgets: tuple[int, ...],
) -> tuple[_GPUAuthoritySessionProvider, tuple[uuid.UUID, ...]]:
    factory = await _open_gpu_authority_provider(test_engine)
    backend_ids = tuple(uuid.uuid4() for _ in budgets)
    try:
        async with factory.begin() as db:
            for index, (backend_id, budget) in enumerate(
                zip(backend_ids, budgets, strict=True)
            ):
                db.add(
                    MLBackendRegistry(
                        id=backend_id,
                        name=f"gpu-proof-{index}",
                        url=f"http://gpu-proof-{backend_id}.test",
                        gpu_resource_id=resource_id,
                        vram_budget_mb=budget,
                        eviction_priority=index,
                        extra_params={"max_concurrency": index + 4},
                    )
                )
        for backend_id in backend_ids:
            async with factory.transaction() as data:
                activated = await activate_gpu_backend_membership(
                    data,
                    backend_id,
                    gpu_resource_id=resource_id,
                    membership_epoch=1,
                )
            assert activated == "1"
        return factory, backend_ids
    except BaseException:
        await _cleanup_backends(factory, backend_ids)
        raise


async def _cleanup_backends(
    factory: _GPUAuthoritySessionProvider, backend_ids: tuple[uuid.UUID, ...]
) -> None:
    try:
        if not backend_ids:
            return
        async with factory.begin() as db:
            await db.execute(
                update(GPUBackendFence)
                .where(GPUBackendFence.backend_registry_id.in_(backend_ids))
                .values(
                    generation_high_water=0,
                    control_epoch_high_water=0,
                    runtime_epoch_high_water=0,
                    token_expiry_high_water=None,
                )
            )
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id.in_(backend_ids))
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_memberships DISABLE TRIGGER "
                    "trg_validate_gpu_backend_membership"
                )
            )
            await db.execute(
                delete(GPUBackendMembership).where(
                    GPUBackendMembership.backend_registry_id.in_(backend_ids)
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_memberships ENABLE TRIGGER "
                    "trg_validate_gpu_backend_membership"
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_fences DISABLE TRIGGER "
                    "trg_validate_gpu_backend_fence_delete"
                )
            )
            await db.execute(
                delete(GPUBackendFence).where(
                    GPUBackendFence.backend_registry_id.in_(backend_ids)
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_fences ENABLE TRIGGER "
                    "trg_validate_gpu_backend_fence_delete"
                )
            )
    finally:
        await factory.aclose()


def _residency(
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    resident: bool,
) -> dict:
    if resident:
        return {
            "state": "resident",
            "gpu_loaded": True,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": True,
            "generation": "1",
            "pools": {
                "models": {
                    "resident": True,
                    "device": "cuda:0",
                    "provider": None,
                }
            },
            "boot_id": f"boot-{backend_id}",
            "lifecycle_gate": "enforce",
            "control_epoch": "1",
            "identity": {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": resource_id,
            },
        }
    return {
        "state": "unloaded",
        "gpu_loaded": False,
        "active_requests": 0,
        "builders": 0,
        "borrowers": 0,
        "draining": False,
        "evictable": False,
        "generation": None,
        "pools": {
            "models": {
                "resident": False,
                "device": "cpu",
                "provider": None,
            }
        },
        "boot_id": f"boot-{backend_id}",
        "lifecycle_gate": "enforce",
        "control_epoch": "1",
        "identity": {
            "audience": "aap-gpu-lifecycle",
            "backend_registry_id": str(backend_id),
            "gpu_resource_id": resource_id,
        },
    }


async def _install_live_health(
    factory: _GPUAuthoritySessionProvider,
    backend_ids: tuple[uuid.UUID, ...],
    *,
    resource_id: str,
    resident_backend_ids: frozenset[uuid.UUID] = frozenset(),
) -> dict[uuid.UUID, datetime]:
    started_by_backend: dict[uuid.UUID, datetime] = {}
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    capability_sha256 = managed_lifecycle_capability_sha256(capability)
    async with factory.begin() as db:
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert isinstance(db_now, datetime)
        for index, backend_id in enumerate(backend_ids):
            membership = await db.get(GPUBackendMembership, (backend_id, resource_id))
            fence = await db.get(GPUBackendFence, backend_id)
            backend = await db.get(MLBackendRegistry, backend_id)
            assert membership is not None
            assert fence is not None
            assert backend is not None
            probe_started_at = db_now - timedelta(seconds=2, milliseconds=index)
            observed_at = db_now - timedelta(seconds=1, milliseconds=index)
            started_by_backend[backend_id] = probe_started_at
            resident = backend_id in resident_backend_ids
            fence.generation_high_water = 1 if resident else 0
            fence.control_epoch_high_water = 1
            fence.token_expiry_high_water = probe_started_at - timedelta(seconds=1)
            backend.state = "connected"
            backend.last_checked_at = observed_at
            backend.health_meta = {
                "capabilities": {"managed_lifecycle": capability},
                "gpu_arbiter_probe": {
                    "protocol_version": "1",
                    "challenge": f"{index + 1:064x}",
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": resource_id,
                    "membership_epoch": str(membership.membership_epoch),
                    "membership_state": membership.state,
                    "managed_lifecycle_sha256": capability_sha256,
                    "probe_started_at": _proof_timestamp(probe_started_at),
                    "observed_at": _proof_timestamp(observed_at),
                },
                "residency": _residency(
                    backend_id,
                    resource_id,
                    resident=resident,
                ),
            }
    return started_by_backend


async def _membership_domain(
    factory: _GPUAuthoritySessionProvider, resource_id: str
) -> tuple[GPUBackendDomainMember, ...]:
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(GPUBackendMembership)
                    .where(GPUBackendMembership.gpu_resource_id == resource_id)
                    .order_by(GPUBackendMembership.backend_registry_id)
                )
            )
            .scalars()
            .all()
        )
    return tuple(
        GPUBackendDomainMember(
            backend_id=str(row.backend_registry_id),
            membership_epoch=row.membership_epoch,
            state=row.state,  # type: ignore[arg-type]
        )
        for row in rows
    )


async def _enable_cold_runtime_health(
    factory: _GPUAuthoritySessionProvider,
    backend_id: uuid.UUID,
) -> None:
    async with factory.begin() as db:
        backend = await db.get(MLBackendRegistry, backend_id)
        assert backend is not None
        health_meta = dict(backend.health_meta)
        residency = dict(health_meta["residency"])
        residency["lifecycle_gate"] = "enforce"
        health_meta["residency"] = residency
        backend.health_meta = health_meta
        flag_modified(backend, "health_meta")


async def _bootstrap_cold_loading(
    factory: _GPUAuthoritySessionProvider,
    store: GPUArbiterStore,
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    bootstrap_card: bool = True,
) -> tuple[GPUPreparedColdRuntimeSubject, str, str]:
    if bootstrap_card:
        reconciled = await store.reconcile_card(
            resource_id,
            8192,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=await _membership_domain(factory, resource_id),
            allocations=(),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=int(time.time() * 1000) + 120_000,
            repair_id=f"cold-terminal-{uuid.uuid4()}",
        )
        assert reconciled.status == "reconciled", (
            reconciled.status,
            reconciled.reason,
        )
    subject = await read_gpu_cold_runtime_subject(
        factory,
        backend_id=str(backend_id),
        gpu_resource_id=resource_id,
    )
    generation = str(subject.generation_high_water + 1)
    lease_id = f"workload:{uuid.uuid4()}"
    owner_id = f"dispatch:{uuid.uuid4()}"
    acquired = await store.acquire_cold_admission_owner(
        resource_id,
        backend_id=str(backend_id),
        membership_epoch=subject.membership_epoch,
        owner_id=owner_id,
        generation=generation,
        ttl_ms=30_000,
    )
    assert acquired.status == "acquired"
    prepared = await prepare_gpu_cold_runtime_generation(
        factory,
        subject,
        token_expires_at=subject.db_now + timedelta(seconds=120),
    )
    assert prepared.generation == generation
    renewed = await store.revalidate_cold_admission_owner(
        resource_id,
        backend_id=str(backend_id),
        membership_epoch=subject.membership_epoch,
        owner_id=owner_id,
        generation=generation,
        ttl_ms=30_000,
    )
    assert renewed.status == "renewed"
    admission = await store.admit(
        resource_id,
        backend_id=str(backend_id),
        membership_epoch=subject.membership_epoch,
        budget_mb=subject.budget_mb,
        generation=generation,
        eviction_priority=subject.eviction_priority,
        evictable=False,
        max_concurrency=subject.max_concurrency,
        lease_id=lease_id,
        owner_id=owner_id,
        operation="predict",
        heartbeat_ttl_ms=15_000,
        hard_ttl_ms=120_000,
        require_cold_owner=True,
    )
    assert admission.admitted
    loading = await store.transition_allocation(
        resource_id,
        backend_id=str(backend_id),
        expected_generation=generation,
        target_state=GPUAllocationState.LOADING,
        request_lease_id=lease_id,
        request_owner_id=owner_id,
    )
    assert loading.status == "transitioned"
    return prepared, lease_id, owner_id


async def _install_cold_terminal_health(
    factory: _GPUAuthoritySessionProvider,
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    challenge: str,
    terminal: str,
    stored_challenge: str | None = None,
) -> None:
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    capability_sha256 = managed_lifecycle_capability_sha256(capability)
    async with factory.begin() as db:
        membership = await db.get(GPUBackendMembership, (backend_id, resource_id))
        backend = await db.get(MLBackendRegistry, backend_id)
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert membership is not None
        assert backend is not None
        assert isinstance(db_now, datetime)
        probe_started_at = db_now - timedelta(milliseconds=2)
        observed_at = db_now - timedelta(milliseconds=1)
        gpu_loaded = terminal in {"resident", "unknown_busy"}
        residency_state = "unloaded" if terminal == "unloaded" else "resident"
        pool_resident = gpu_loaded
        residency = {
            "state": residency_state,
            "gpu_loaded": gpu_loaded,
            "active_requests": 1 if terminal == "unknown_busy" else 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": gpu_loaded,
            "generation": None if terminal == "unloaded" else "1",
            "pools": {
                "models": {
                    "resident": pool_resident,
                    "device": "cuda:0" if pool_resident else "cpu",
                    "provider": None,
                }
            },
            "boot_id": f"boot-{backend_id}",
            "lifecycle_gate": "enforce",
            "control_epoch": "1",
            "identity": {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": resource_id,
            },
        }
        cpu_fallback = terminal in {"cpu_device", "cpu_provider"}
        compute = {
            "configured_device": "cuda",
            "effective_device": (
                "cpu" if terminal in {"resident", "cpu_device"} else "cuda"
            ),
            "effective_provider": (
                "CPUExecutionProvider" if terminal == "cpu_provider" else None
            ),
            "cpu_fallback_supported": cpu_fallback or terminal == "resident",
        }
        backend.state = "connected"
        backend.last_checked_at = observed_at
        backend.health_meta = {
            "capabilities": {"managed_lifecycle": capability},
            "compute": compute,
            "gpu_arbiter_probe": {
                "protocol_version": "1",
                "challenge": stored_challenge or challenge,
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": resource_id,
                "membership_epoch": str(membership.membership_epoch),
                "membership_state": "active",
                "managed_lifecycle_sha256": capability_sha256,
                "probe_started_at": _proof_timestamp(probe_started_at),
                "observed_at": _proof_timestamp(observed_at),
            },
            "residency": residency,
        }
        flag_modified(backend, "health_meta")


async def _install_eviction_phase_health(
    factory: _GPUAuthoritySessionProvider,
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    challenge: str,
    generation: str,
    phase: str,
    stored_challenge: str | None = None,
    active_requests: int | None = None,
    builders: int = 0,
    borrowers: int = 0,
) -> None:
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    capability_sha256 = managed_lifecycle_capability_sha256(capability)
    async with factory.begin() as db:
        membership = await db.get(GPUBackendMembership, (backend_id, resource_id))
        backend = await db.get(MLBackendRegistry, backend_id)
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert membership is not None
        assert backend is not None
        assert isinstance(db_now, datetime)
        probe_started_at = db_now - timedelta(milliseconds=2)
        observed_at = db_now - timedelta(milliseconds=1)
        unloaded = phase == "unload"
        resumed = phase == "resume"
        backend.state = "connected"
        backend.last_checked_at = observed_at
        backend.health_meta = {
            "capabilities": {"managed_lifecycle": capability},
            "gpu_arbiter_probe": {
                "protocol_version": "1",
                "challenge": stored_challenge or challenge,
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": resource_id,
                "membership_epoch": str(membership.membership_epoch),
                "membership_state": "active",
                "managed_lifecycle_sha256": capability_sha256,
                "probe_started_at": _proof_timestamp(probe_started_at),
                "observed_at": _proof_timestamp(observed_at),
            },
            "residency": {
                "state": (
                    "unloaded" if unloaded else "resident" if resumed else "draining"
                ),
                "gpu_loaded": not unloaded,
                "active_requests": (1 if phase == "busy" else 0)
                if active_requests is None
                else active_requests,
                "builders": builders,
                "borrowers": borrowers,
                "draining": not unloaded and not resumed,
                "evictable": resumed,
                "generation": generation,
                "pools": {
                    "models": {
                        "resident": not unloaded,
                        "device": "cpu" if unloaded else "cuda:0",
                        "provider": None,
                    }
                },
                "boot_id": f"boot-{backend_id}",
                "lifecycle_gate": "enforce",
                "control_epoch": "1",
                "identity": {
                    "audience": "aap-gpu-lifecycle",
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": resource_id,
                },
            },
        }
        flag_modified(backend, "health_meta")


async def _prepare_selected_idle_eviction(
    factory: _GPUAuthoritySessionProvider,
    store: GPUArbiterStore,
    *,
    victim_id: uuid.UUID,
    requester_id: uuid.UUID,
    resource_id: str,
    allow_busy: bool = False,
) -> tuple[GPUPreparedIdleEvictionRuntimeSubject, str, int]:
    async with factory() as db:
        victim_membership = await db.get(
            GPUBackendMembership,
            (victim_id, resource_id),
        )
        requester_membership = await db.get(
            GPUBackendMembership,
            (requester_id, resource_id),
        )
        victim_backend = await db.get(MLBackendRegistry, victim_id)
        assert victim_membership is not None
        assert requester_membership is not None
        assert victim_backend is not None
        challenge = victim_backend.health_meta["gpu_arbiter_probe"]["challenge"]
    reconciled = await store.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=await _membership_domain(factory, resource_id),
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=int(time.time() * 1000) + 120_000,
        repair_id=f"idle-eviction-{uuid.uuid4()}",
    )
    assert reconciled.status == "reconciled", (
        reconciled.status,
        reconciled.reason,
    )
    lease_id = f"workload:{uuid.uuid4()}"
    workload_owner_id = f"dispatch:{uuid.uuid4()}"
    admission = await store.admit(
        resource_id,
        backend_id=str(victim_id),
        membership_epoch=victim_membership.membership_epoch,
        budget_mb=victim_membership.vram_budget_mb,
        generation="1",
        eviction_priority=victim_membership.eviction_priority,
        evictable=False,
        max_concurrency=victim_membership.max_concurrency,
        lease_id=lease_id,
        owner_id=workload_owner_id,
        operation="predict",
        heartbeat_ttl_ms=15_000,
        hard_ttl_ms=120_000,
    )
    assert admission.admitted
    transition = await store.transition_allocation(
        resource_id,
        backend_id=str(victim_id),
        expected_generation="1",
        target_state=GPUAllocationState.LOADING,
        request_lease_id=lease_id,
        request_owner_id=workload_owner_id,
    )
    assert transition.status == "transitioned"
    finalized = await store.finalize_cold_allocation(
        resource_id,
        backend_id=str(victim_id),
        expected_generation="1",
        request_lease_id=lease_id,
        request_owner_id=workload_owner_id,
        target_state=GPUAllocationState.RESIDENT,
        target_evictable=True,
        resident_cooldown_ms=1,
    )
    assert finalized.status == "transitioned"
    await asyncio.sleep(0.002)
    if not allow_busy:
        released = await store.release_lease(
            resource_id,
            backend_id=str(victim_id),
            lease_id=lease_id,
            owner_id=workload_owner_id,
            generation="1",
        )
        assert released.status == "released"
    async with factory() as db:
        read_subject = (
            read_gpu_busy_eviction_runtime_subject
            if allow_busy
            else read_gpu_idle_eviction_runtime_subject
        )
        subject = await read_subject(
            db,
            backend_id=str(victim_id),
            gpu_resource_id=resource_id,
            expected_generation="1",
            challenge=challenge,
        )
    prepared = await prepare_gpu_idle_eviction_runtime_generation(
        factory,
        subject,
        token_expires_at=subject.db_now + timedelta(seconds=120),
    )
    owner_id = f"eviction:{uuid.uuid4()}"
    selected = await store.begin_idle_eviction(
        resource_id,
        requester_backend_id=str(requester_id),
        requester_membership_epoch=requester_membership.membership_epoch,
        requester_budget_mb=requester_membership.vram_budget_mb,
        requester_eviction_priority=requester_membership.eviction_priority,
        victim_backend_id=str(victim_id),
        victim_membership_epoch=victim_membership.membership_epoch,
        victim_expected_generation=prepared.source_generation,
        victim_next_generation=prepared.generation,
        owner_id=owner_id,
        ttl_ms=30_000,
        hard_ttl_ms=120_000,
        allow_busy=allow_busy,
    )
    assert selected.status == "selected"
    assert selected.owner_hard_deadline_ms is not None
    return prepared, owner_id, selected.owner_hard_deadline_ms


def _fake_context(
    resource_id: str,
    memberships: tuple[GPUBackendDomainMember, ...],
    *,
    allocatable_mb: int = 8192,
) -> GPUProofResetContext:
    return GPUProofResetContext(
        resource_id=resource_id,
        allocatable_mb=allocatable_mb,
        reset_id=f"proof-{uuid.uuid4().hex}",
        begin_fingerprint="a" * 64,
        ledger_revision=1,
        ledger_incarnation=uuid.uuid4().hex,
        prepared_at_ms=int(time.time() * 1000),
        backend_ids=tuple(item.backend_id for item in memberships),
        active_backend_ids=tuple(
            item.backend_id for item in memberships if item.state == "active"
        ),
        backend_memberships=memberships,
    )


class _RecordingProofStore:
    def __init__(self, *, block: bool = False) -> None:
        self.calls: list[dict] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def commit_proof_reset(self, resource_id: str, allocatable_mb: int, **kwargs):
        self.calls.append(
            {
                "resource_id": resource_id,
                "allocatable_mb": allocatable_mb,
                **kwargs,
            }
        )
        self.entered.set()
        await self.release.wait()
        ready = kwargs["ready"]
        return GPUReconcileResult(
            status="reconciled" if ready else "not_ready",
            ready=ready,
            ledger_revision=2,
            ledger_incarnation=kwargs["expected_reset_incarnation"],
            committed_mb=sum(item.budget_mb for item in kwargs["allocations"]),
            reason="" if ready else "proof_incomplete",
        )


@pytest.mark.asyncio
async def test_proof_recovery_commits_complete_multi_backend_snapshot_idempotently(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024, 2048),
    )
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_ids[1]}),
        )
        memberships = await _membership_domain(factory, _RESOURCE_A)
        prepared = await proof_store.begin_proof_reset(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id=f"proof-{uuid.uuid4().hex}",
        )
        assert prepared.status == "prepared"
        raw = Redis.from_url(_redis_url(), decode_responses=True)
        try:
            stale_prepared_at_ms = int(time.time() * 1000) - 600_000
            await raw.hset(
                proof_store.keys(_RESOURCE_A).card,
                mapping={
                    "proof_reset_prepared_at_ms": stale_prepared_at_ms,
                    "updated_at_ms": stale_prepared_at_ms,
                },
            )
        finally:
            await raw.aclose()
        context = await proof_store.prepared_proof_reset(_RESOURCE_A)
        assert context is not None

        first = await commit_gpu_proof_reset_from_health(
            factory,
            proof_store,
            context,
            config=_resource_config(_RESOURCE_A),
        )
        second = await commit_gpu_proof_reset_from_health(
            factory,
            proof_store,
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert first.status == "reconciled"
        assert first.ready is True
        assert second.status == "reconciled"
        assert second.ready is True
        assert second.idempotent is True
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.ready is True
        assert len(snapshot.allocations) == 1
        allocation = snapshot.allocations[0]
        assert allocation.backend_id == str(backend_ids[1])
        assert allocation.state is GPUAllocationState.RESIDENT
        assert allocation.generation == "1"
        assert allocation.budget_mb == 2048
        assert allocation.evictable is False
        assert allocation.not_evict_before_ms == context.prepared_at_ms + 30_000
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_proof_recovery_accepts_matching_managed_lifecycle_capability(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore()
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        capability = ManagedLifecycleCapabilities().model_dump(mode="json")
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_ids[0])
            assert backend is not None
            assert backend.health_meta is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["capabilities"] = {"managed_lifecycle": capability}
            health_meta["gpu_arbiter_probe"]["managed_lifecycle_sha256"] = (
                managed_lifecycle_capability_sha256(capability)
            )
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )

        result = await commit_gpu_proof_reset_from_health(
            factory,
            store,  # type: ignore[arg-type]
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert result.ready is True
        assert store.calls[-1]["ready"] is True
        allocations = store.calls[-1]["allocations"]
        assert len(allocations) == 1
        assert allocations[0].state is GPUAllocationState.RESIDENT
        assert allocations[0].evictable is False
        assert allocations[0].not_evict_before_ms == context.prepared_at_ms + 30_000
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_stale_prepared_context_can_still_commit_not_ready(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        memberships = await _membership_domain(factory, _RESOURCE_A)
        prepared = await proof_store.begin_proof_reset(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id=f"stale-proof-{uuid.uuid4().hex}",
        )
        assert prepared.status == "prepared"
        stale_prepared_at_ms = int(time.time() * 1000) - 600_000
        await raw.hset(
            proof_store.keys(_RESOURCE_A).card,
            mapping={
                "proof_reset_prepared_at_ms": stale_prepared_at_ms,
                "updated_at_ms": stale_prepared_at_ms,
            },
        )
        context = await proof_store.prepared_proof_reset(_RESOURCE_A)
        assert context is not None

        first = await commit_gpu_proof_reset_from_health(
            factory,
            proof_store,
            context,
            config=_resource_config(_RESOURCE_A),
        )
        second = await commit_gpu_proof_reset_from_health(
            factory,
            proof_store,
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert first.status == "not_ready"
        assert first.ready is False
        assert first.reason == "proof_incomplete"
        assert second.status == "not_ready"
        assert second.ready is False
        assert second.idempotent is True
        assert await proof_store.prepared_proof_reset(_RESOURCE_A) is None
    finally:
        await raw.aclose()
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_pending_member_is_conservative_unknown(
    test_engine: AsyncEngine,
) -> None:
    factory, active_backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    pending_backend_id = uuid.uuid4()
    backend_ids = active_backend_ids + (pending_backend_id,)
    store = _RecordingProofStore()
    try:
        async with factory.begin() as db:
            db.add(
                MLBackendRegistry(
                    id=pending_backend_id,
                    name="pending-proof",
                    url=f"http://pending-proof-{pending_backend_id}.test",
                    gpu_resource_id=_RESOURCE_A,
                    vram_budget_mb=512,
                )
            )
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
        )
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )

        result = await commit_gpu_proof_reset_from_health(
            factory,
            store,  # type: ignore[arg-type]
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert result.ready is False
        pending_allocation = next(
            item
            for item in store.calls[-1]["allocations"]
            if item.backend_id == str(pending_backend_id)
        )
        assert pending_allocation.state is GPUAllocationState.UNKNOWN
        assert pending_allocation.generation is None
        assert pending_allocation.budget_mb == 512
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    (
        "horizon_equal",
        "horizon_missing",
        "identity_mismatch",
        "stale_probe",
        "bool_counter",
        "generation_ahead",
        "control_epoch_stale",
        "legacy_gate",
        "identity_missing",
        "capability_missing",
        "capability_hash_mismatch",
        "capability_snapshot_invalid",
        "legacy_probe_schema",
        "extra_field",
    ),
)
async def test_proof_recovery_maps_untrusted_evidence_to_unknown(
    test_engine: AsyncEngine,
    invalid_case: str,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore()
    try:
        started = await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        backend_id = backend_ids[0]
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            backend = await db.get(MLBackendRegistry, backend_id)
            assert fence is not None
            assert backend is not None
            assert backend.health_meta is not None
            if invalid_case == "horizon_equal":
                fence.token_expiry_high_water = started[backend_id]
            elif invalid_case == "horizon_missing":
                fence.token_expiry_high_water = None
            else:
                health_meta = json.loads(json.dumps(backend.health_meta))
                if invalid_case == "bool_counter":
                    health_meta["residency"]["active_requests"] = False
                elif invalid_case == "generation_ahead":
                    health_meta["residency"]["generation"] = "2"
                elif invalid_case == "control_epoch_stale":
                    fence.control_epoch_high_water = 2
                elif invalid_case == "legacy_gate":
                    health_meta["residency"]["lifecycle_gate"] = "legacy"
                elif invalid_case == "identity_missing":
                    health_meta["residency"]["evictable"] = False
                    health_meta["residency"]["control_epoch"] = None
                    health_meta["residency"]["identity"] = None
                elif invalid_case == "capability_missing":
                    health_meta.pop("capabilities")
                    health_meta["gpu_arbiter_probe"]["managed_lifecycle_sha256"] = None
                elif invalid_case == "capability_hash_mismatch":
                    health_meta["gpu_arbiter_probe"]["managed_lifecycle_sha256"] = (
                        "a" * 64
                    )
                elif invalid_case == "capability_snapshot_invalid":
                    health_meta["capabilities"] = {
                        "managed_lifecycle": {"protocol_version": "1"}
                    }
                elif invalid_case == "legacy_probe_schema":
                    health_meta["gpu_arbiter_probe"].pop("managed_lifecycle_sha256")
                elif invalid_case == "identity_mismatch":
                    health_meta["residency"]["identity"]["backend_registry_id"] = str(
                        uuid.uuid4()
                    )
                elif invalid_case == "stale_probe":
                    stale_observed_at = started[backend_id] - timedelta(minutes=10)
                    stale_started_at = stale_observed_at - timedelta(seconds=1)
                    health_meta["gpu_arbiter_probe"]["probe_started_at"] = (
                        _proof_timestamp(stale_started_at)
                    )
                    health_meta["gpu_arbiter_probe"]["observed_at"] = _proof_timestamp(
                        stale_observed_at
                    )
                    backend.last_checked_at = stale_observed_at
                    fence.token_expiry_high_water = stale_started_at - timedelta(
                        seconds=1
                    )
                else:
                    health_meta["residency"]["unexpected"] = True
                backend.health_meta = health_meta
                flag_modified(backend, "health_meta")
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )

        result = await commit_gpu_proof_reset_from_health(
            factory,
            store,  # type: ignore[arg-type]
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert result.ready is False
        assert len(store.calls) == 1
        call = store.calls[-1]
        assert call["ready"] is False
        assert len(call["allocations"]) == 1
        allocation = call["allocations"][0]
        assert allocation.state is GPUAllocationState.UNKNOWN
        assert allocation.generation is None
        assert allocation.evictable is False
        assert allocation.budget_mb == 1024
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_busy_but_strict_residency_keeps_known_generation_unknown(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore()
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_ids[0])
            assert backend is not None
            assert backend.health_meta is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["active_requests"] = 1
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )

        result = await commit_gpu_proof_reset_from_health(
            factory,
            store,  # type: ignore[arg-type]
            context,
            config=_resource_config(_RESOURCE_A),
        )

        assert result.ready is False
        assert len(store.calls) == 1
        allocation = store.calls[0]["allocations"][0]
        assert allocation.state is GPUAllocationState.UNKNOWN
        assert allocation.generation == "1"
        assert allocation.evictable is False
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_recovery_rechecks_horizon_after_membership_lock_wait(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore()
    holder_release = asyncio.Event()
    holder_locked = asyncio.Event()
    holder_task: asyncio.Task | None = None
    recovery_task: asyncio.Task | None = None
    try:
        started = await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        backend_id = backend_ids[0]
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )

        async def hold_new_horizon() -> None:
            async with factory.transaction() as data:
                await record_gpu_backend_token_expiry(
                    data,
                    backend_id,
                    gpu_resource_id=_RESOURCE_A,
                    membership_epoch=1,
                    token_expires_at=(started[backend_id] + timedelta(milliseconds=1)),
                )
                holder_locked.set()
                await holder_release.wait()

        holder_task = asyncio.create_task(hold_new_horizon())
        await asyncio.wait_for(holder_locked.wait(), timeout=1)
        recovery_task = asyncio.create_task(
            commit_gpu_proof_reset_from_health(
                factory,
                store,  # type: ignore[arg-type]
                context,
                config=_resource_config(_RESOURCE_A),
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(store.entered.wait(), timeout=0.05)
        holder_release.set()
        await holder_task
        result = await recovery_task

        assert result.ready is False
        assert len(store.calls) == 1
        allocation = store.calls[-1]["allocations"][0]
        assert allocation.state is GPUAllocationState.UNKNOWN
        assert allocation.generation is None
    finally:
        holder_release.set()
        if holder_task is not None:
            await asyncio.gather(holder_task, return_exceptions=True)
        if recovery_task is not None:
            await asyncio.gather(recovery_task, return_exceptions=True)
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_recovery_holds_same_card_locks_through_redis_commit(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore(block=True)
    recovery_task: asyncio.Task | None = None
    writer_task: asyncio.Task | None = None
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )
        recovery_task = asyncio.create_task(
            commit_gpu_proof_reset_from_health(
                factory,
                store,  # type: ignore[arg-type]
                context,
                config=_resource_config(_RESOURCE_A),
            )
        )
        await asyncio.wait_for(store.entered.wait(), timeout=1)
        writer_task = asyncio.create_task(
            _record_token_expiry(
                factory,
                backend_ids[0],
                gpu_resource_id=_RESOURCE_A,
                membership_epoch=1,
                token_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(writer_task), timeout=0.05)

        store.release.set()
        result, _ = await asyncio.gather(recovery_task, writer_task)
        assert result.ready is True
        assert len(store.calls) == 1
    finally:
        store.release.set()
        if recovery_task is not None:
            await asyncio.gather(recovery_task, return_exceptions=True)
        if writer_task is not None:
            await asyncio.gather(writer_task, return_exceptions=True)
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_resource_barrier_rejects_same_card_insert_but_not_other_card(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    same_backend_id = uuid.uuid4()
    other_backend_id = uuid.uuid4()
    all_backend_ids = backend_ids + (same_backend_id, other_backend_id)
    store = _RecordingProofStore(block=True)
    recovery_task: asyncio.Task | None = None
    same_task: asyncio.Task | None = None
    other_task: asyncio.Task | None = None
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
        )
        context = _fake_context(
            _RESOURCE_A, await _membership_domain(factory, _RESOURCE_A)
        )
        recovery_task = asyncio.create_task(
            commit_gpu_proof_reset_from_health(
                factory,
                store,  # type: ignore[arg-type]
                context,
                config=_resource_config(_RESOURCE_A, _RESOURCE_B),
            )
        )
        await asyncio.wait_for(store.entered.wait(), timeout=1)

        async def insert_backend(backend_id: uuid.UUID, resource_id: str) -> None:
            async with factory.begin() as db:
                db.add(
                    MLBackendRegistry(
                        id=backend_id,
                        name=f"insert-{backend_id}",
                        url=f"http://insert-{backend_id}.test",
                        gpu_resource_id=resource_id,
                        vram_budget_mb=512,
                    )
                )
                await db.flush()

        same_task = asyncio.create_task(insert_backend(same_backend_id, _RESOURCE_A))
        other_task = asyncio.create_task(insert_backend(other_backend_id, _RESOURCE_B))
        await asyncio.wait_for(other_task, timeout=1)
        same_outcome = (await asyncio.gather(same_task, return_exceptions=True))[0]
        assert isinstance(same_outcome, DBAPIError)
        orig = getattr(same_outcome, "orig", None)
        assert (
            getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        ) == "40001"

        store.release.set()
        await recovery_task
        assert len(store.calls) == 1
    finally:
        store.release.set()
        for task in (recovery_task, same_task, other_task):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await _cleanup_backends(factory, all_backend_ids)


@pytest.mark.asyncio
async def test_domain_change_clears_only_prepared_domain_not_ready(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    store = _RecordingProofStore()
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        original_domain = await _membership_domain(factory, _RESOURCE_A)
        context = _fake_context(_RESOURCE_A, original_domain)
        async with factory.begin() as db:
            await db.execute(
                update(GPUBackendFence)
                .where(GPUBackendFence.backend_registry_id == backend_ids[0])
                .values(runtime_epoch_high_water=0)
            )
            await db.execute(
                update(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend_ids[0])
                .values(gpu_resource_id=_RESOURCE_B)
            )

        result = await commit_gpu_proof_reset_from_health(
            factory,
            store,  # type: ignore[arg-type]
            context,
            config=_resource_config(_RESOURCE_A, _RESOURCE_B),
        )

        assert result.ready is False
        assert len(store.calls) == 1
        call = store.calls[-1]
        assert call["ready"] is False
        assert call["allocations"] == ()
        assert call["backend_memberships"] == original_domain
        current_old_domain = await _membership_domain(factory, _RESOURCE_A)
        assert current_old_domain[0].state == "retiring"
        assert current_old_domain != original_domain
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_redis_gc_receipt_resumes_db_tombstone_and_orphan_fence_finalize(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    gpu_collector_database: GPUCollectorDatabase,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    resource_id = "node-proof/GPU-gc-receipt"
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gc-receipt",
                url=f"http://gc-receipt-{backend_id}.test",
                gpu_resource_id=resource_id,
                vram_budget_mb=1024,
            )
        )
    async with factory.begin() as db:
        await db.execute(
            update(MLBackendRegistry)
            .where(MLBackendRegistry.id == backend_id)
            .values(gpu_resource_id=None, vram_budget_mb=None)
        )
    async with factory() as db:
        tombstone = await db.get(GPUBackendMembership, (backend_id, resource_id))
    assert tombstone is not None
    assert tombstone.retirement_id is not None
    retirement_id = str(tombstone.retirement_id)
    domain = (
        GPUBackendDomainMember(
            backend_id=str(backend_id),
            membership_epoch=2,
            state="retiring",
        ),
    )
    prepared = await proof_store.begin_proof_reset(
        resource_id,
        2048,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=domain,
        reset_id="gc-receipt-bootstrap",
    )
    assert prepared.status == "prepared"
    context = await proof_store.prepared_proof_reset(resource_id)
    assert context is not None
    committed = await proof_store.commit_proof_reset(
        resource_id,
        2048,
        reset_id=context.reset_id,
        expected_reset_revision=context.ledger_revision,
        expected_reset_incarnation=context.ledger_incarnation,
        backend_memberships=domain,
        allocations=(
            GPUAllocation(
                backend_id=str(backend_id),
                state=GPUAllocationState.UNKNOWN,
                budget_mb=1024,
                generation=None,
                eviction_priority=0,
                evictable=False,
                max_concurrency=4,
                reservation_lease_id=None,
                reservation_owner_id=None,
                last_used_at_ms=1,
                not_evict_before_ms=0,
            ),
        ),
        ready=False,
        evidence_deadline_ms=0,
        proof_fingerprint="4" * 64,
    )
    assert committed.status == "not_ready"
    snapshot = await proof_store.snapshot(resource_id)
    staged = await proof_store.collect_retired_backend(
        resource_id,
        expected_ledger_revision=snapshot.ledger_revision,
        expected_ledger_incarnation=snapshot.ledger_incarnation,
        backend_memberships=domain,
        backend_id=str(backend_id),
        membership_epoch=2,
        retirement_id=retirement_id,
        vram_budget_mb=1024,
        evidence_deadline_ms=int(time.time() * 1000) + 120_000,
        evidence_fingerprint="5" * 64,
        collection_id="gc-receipt-stage",
    )
    assert staged.status == "collected"
    collected_snapshot = await proof_store.snapshot(resource_id)
    assert collected_snapshot.backend_ids == ()
    assert collected_snapshot.committed_mb == 0
    receipt = await proof_store.verify_tombstone_gc_receipt(
        resource_id,
        backend_memberships=domain,
        backend_id=str(backend_id),
        membership_epoch=2,
        retirement_id=retirement_id,
    )
    assert receipt is not None

    # Simulate a process crash after Redis, followed by registry deletion.  The
    # next worker must finalize from the exact Redis receipt without frozen health.
    async with factory.begin() as db:
        await db.execute(
            delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
        )
    receipt_after_registry_delete = await proof_store.verify_tombstone_gc_receipt(
        resource_id,
        backend_memberships=domain,
        backend_id=str(backend_id),
        membership_epoch=2,
        retirement_id=retirement_id,
    )
    assert receipt_after_registry_delete is not None
    async with factory() as db:
        locked_tombstone = await db.get(GPUBackendMembership, (backend_id, resource_id))
    assert locked_tombstone is not None
    assert str(locked_tombstone.retirement_id) == retirement_id
    finalized = await collect_gpu_backend_tombstone(
        gpu_collector_database,
        proof_store,
        backend_id,
        resource_id,
        2,
        proof=None,
    )
    assert finalized.status == "collected", finalized
    assert finalized.reason == "redis_receipt_finalized"
    assert finalized.redis_idempotent is True
    async with factory() as db:
        assert await db.get(GPUBackendMembership, (backend_id, resource_id)) is None
        assert await db.get(GPUBackendFence, backend_id) is None


@pytest.mark.asyncio
async def test_repair_bootstraps_missing_card_then_leaves_ready_card_read_only(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024, 2048),
    )
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_ids[1]}),
        )
        first = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert first.action == "bootstrap"
        assert first.ready is True
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.ready is True
        assert snapshot.committed_mb == 2048
        assert tuple(item.state for item in snapshot.allocations) == (
            GPUAllocationState.RESIDENT,
        )

        second = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert second.action == "already_ready"
        assert second.ledger_revision == snapshot.ledger_revision
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_repair_resumes_legacy_v2_prepared_reset_into_v3(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        config = _resource_config(_RESOURCE_A)
        initial = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=config,
        )
        assert initial.ready is True
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        prepared = await proof_store.begin_proof_reset(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=snapshot.ledger_revision,
            expected_ledger_incarnation=snapshot.ledger_incarnation,
            backend_memberships=snapshot.backend_memberships,
            reset_id="legacy-v2-prepared-repair",
        )
        assert prepared.status == "prepared"

        raw = Redis.from_url(_redis_url(), decode_responses=True)
        try:
            await raw.hset(
                proof_store.keys(_RESOURCE_A).card,
                "ledger_version",
                "2",
            )
            context = await proof_store.prepared_proof_reset(_RESOURCE_A)
            assert context is not None

            resumed = await repair_gpu_resource(
                factory,
                proof_store,
                _RESOURCE_A,
                8192,
                config=config,
            )

            assert (resumed.action, resumed.status, resumed.ready) == (
                "resume_prepared",
                "reconciled",
                True,
            )
            assert (
                await raw.hget(
                    proof_store.keys(_RESOURCE_A).card,
                    "ledger_version",
                )
                == "3"
            )
            recovered = await proof_store.snapshot(_RESOURCE_A)
            assert recovered.allocations[0].not_evict_before_ms == (
                context.prepared_at_ms
                + config.gpu_arbiter_residency_cooldown_seconds * 1000
            )
        finally:
            await raw.aclose()
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_repair_revalidates_ready_card_after_control_horizon_advances(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        first = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert first.ready is True

        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            db_now = await db.scalar(select(text("clock_timestamp()")))
            assert fence is not None
            assert isinstance(db_now, datetime)
            fence.control_epoch_high_water = 2
            fence.token_expiry_high_water = db_now + timedelta(seconds=30)

        invalidated = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
            force_proof_reset=True,
        )
        assert invalidated.ready is False
        assert (await proof_store.snapshot(_RESOURCE_A)).ready is False

        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            backend = await db.get(MLBackendRegistry, backend_id)
            assert fence is not None
            assert backend is not None
            assert backend.health_meta is not None
            fence.control_epoch_high_water = 2
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["control_epoch"] = "2"
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        recovered = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert recovered.ready is True
        assert (await proof_store.snapshot(_RESOURCE_A)).ready is True
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_readiness_blocker_demotes_existing_ready_card(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        ready = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert ready.ready is True

        blocked = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
            readiness_blocker="membership_promotion_unconfirmed",
        )

        assert blocked.ready is False
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.ready is False
        assert snapshot.not_ready_reason == "membership_promotion_unconfirmed"
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_resident_runtime_subject_allows_nonidle_and_post_horizon_proof(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            backend = await db.get(MLBackendRegistry, backend_id)
            assert fence is not None
            assert backend is not None
            assert backend.health_meta is not None
            fence.generation_high_water = 2
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["active_requests"] = 2
            health_meta["residency"]["builders"] = 1
            health_meta["residency"]["borrowers"] = 1
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        subject = await read_gpu_resident_runtime_subject(
            factory,
            backend_id=str(backend_id),
            gpu_resource_id=_RESOURCE_A,
        )
        assert subject.generation == "1"
        assert subject.control_epoch == "1"
        assert subject.runtime_epoch == "1"
        assert subject.membership_epoch == 1

        token_expires_at = subject.db_now + timedelta(minutes=2)
        persisted = await record_gpu_resident_runtime_token_expiry(
            factory,
            subject,
            token_expires_at=token_expires_at,
        )
        assert persisted == token_expires_at

        # Workload admission deliberately does not require a new proof after each
        # token horizon advance; that condition belongs only to proof recovery.
        async with factory() as db:
            repeated = await read_gpu_resident_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
            )
            fence = await db.get(GPUBackendFence, backend_id)
        assert repeated.generation == "1"
        assert fence is not None
        assert fence.generation_high_water == 2
        assert fence.control_epoch_high_water == 1
        assert fence.token_expiry_high_water == token_expires_at
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_runtime_subject_atomically_advances_generation_and_token_horizon(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            assert backend.health_meta is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["lifecycle_gate"] = "enforce"
            challenge = health_meta["gpu_arbiter_probe"]["challenge"]
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        async with factory() as db:
            subject = await read_gpu_cold_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
                expected_challenge=challenge,
            )
        assert subject.observed_generation is None
        assert subject.generation_high_water == 0
        assert subject.control_epoch == "1"
        with pytest.raises(
            GPUColdRuntimeSubjectError,
            match="cold_runtime_challenge_mismatch",
        ):
            async with factory() as db:
                await read_gpu_cold_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_challenge="f" * 64,
                )

        token_expires_at = subject.db_now + timedelta(minutes=2)
        for invalid_ttl in (
            timedelta(0),
            timedelta(seconds=-1),
            timedelta(minutes=5, microseconds=1),
        ):
            with pytest.raises(ValueError, match="no greater than five minutes"):
                await prepare_gpu_cold_runtime_generation(
                    factory,
                    subject,
                    token_expires_at=token_expires_at,
                    evidence_ttl=invalid_ttl,
                )
        prepared = await prepare_gpu_cold_runtime_generation(
            factory,
            subject,
            token_expires_at=token_expires_at,
        )
        assert prepared.generation == "1"
        assert prepared.token_expires_at == token_expires_at
        with pytest.raises(
            GPUColdRuntimeSubjectError,
            match="runtime_subject_changed",
        ):
            await prepare_gpu_cold_runtime_generation(
                factory,
                subject,
                token_expires_at=token_expires_at,
            )
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            repeated = await read_gpu_cold_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
            )
        assert fence is not None
        assert repeated.generation_high_water == 1
        assert fence.generation_high_water == 1
        assert fence.token_expiry_high_water == token_expires_at
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_runtime_subject_allows_only_one_concurrent_generation_prepare(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            assert backend.health_meta is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["lifecycle_gate"] = "enforce"
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        async with factory() as db:
            subject = await read_gpu_cold_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
            )
        token_expires_at = subject.db_now + timedelta(minutes=2)
        results = await asyncio.gather(
            prepare_gpu_cold_runtime_generation(
                factory,
                subject,
                token_expires_at=token_expires_at,
            ),
            prepare_gpu_cold_runtime_generation(
                factory,
                subject,
                token_expires_at=token_expires_at,
            ),
            return_exceptions=True,
        )

        prepared = [
            result
            for result in results
            if isinstance(result, GPUPreparedColdRuntimeSubject)
        ]
        rejected = [
            result
            for result in results
            if isinstance(result, GPUColdRuntimeSubjectError)
        ]
        assert len(prepared) == 1
        assert prepared[0].generation == "1"
        assert len(rejected) == 1
        assert rejected[0].reason == "runtime_subject_changed"
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.generation_high_water == 1
        assert fence.token_expiry_high_water == token_expires_at
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.parametrize(
    ("backend_id", "resource_id", "expected_reason"),
    (
        ("NOT-A-UUID", _RESOURCE_A, "backend_identity_invalid"),
        (
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            _RESOURCE_A,
            "backend_identity_invalid",
        ),
        (str(uuid.uuid4()), "bad resource", "gpu_resource_id_invalid"),
    ),
)
@pytest.mark.asyncio
async def test_cold_runtime_subject_rejects_invalid_identity_with_cold_error(
    gpu_authority_provider: _GPUAuthoritySessionProvider,
    backend_id: str,
    resource_id: str,
    expected_reason: str,
) -> None:
    with pytest.raises(GPUColdRuntimeSubjectError, match=expected_reason):
        await read_gpu_cold_runtime_subject(
            gpu_authority_provider,
            backend_id=backend_id,
            gpu_resource_id=resource_id,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        ("active", "cold_runtime_not_ready"),
        ("legacy_gate", "cold_runtime_not_ready"),
        ("pool_unknown", "residency_unloaded_inconsistent"),
        ("generation_ahead", "residency_generation_ahead"),
    ),
)
@pytest.mark.asyncio
async def test_cold_runtime_subject_rejects_untrusted_state(
    test_engine: AsyncEngine,
    mutation: str,
    expected_reason: str,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            fence = await db.get(GPUBackendFence, backend_id)
            assert backend is not None
            assert backend.health_meta is not None
            assert fence is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["lifecycle_gate"] = "enforce"
            if mutation == "active":
                health_meta["residency"]["active_requests"] = 1
            elif mutation == "legacy_gate":
                health_meta["residency"]["lifecycle_gate"] = "legacy"
            elif mutation == "pool_unknown":
                health_meta["residency"]["pools"]["models"]["resident"] = None
            elif mutation == "generation_ahead":
                health_meta["residency"]["generation"] = "2"
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        async with factory() as db:
            with pytest.raises(
                GPUColdRuntimeSubjectError,
                match=expected_reason,
            ):
                await read_gpu_cold_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_resident_runtime_subject_refreshes_preloaded_identity_map(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        async with factory() as stale_db:
            assert await stale_db.get(MLBackendRegistry, backend_id) is not None
            assert await stale_db.get(GPUBackendFence, backend_id) is not None
            assert (
                await stale_db.get(
                    GPUBackendMembership,
                    (backend_id, _RESOURCE_A),
                )
                is not None
            )

            async with factory.begin() as writer:
                backend = await writer.get(MLBackendRegistry, backend_id)
                assert backend is not None
                backend.state = "disconnected"

            with pytest.raises(
                GPUResidentRuntimeSubjectError,
                match="registry_claim_mismatch",
            ):
                await read_gpu_resident_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("legacy_gate", "residency_evictable_inconsistent"),
        ("draining", "resident_runtime_not_ready"),
        ("pool_unknown", "resident_runtime_not_ready"),
        ("control_stale", "residency_control_epoch_mismatch"),
        ("capability_mismatch", "managed_lifecycle_capability_mismatch"),
        ("horizon_missing", "token_horizon_missing"),
    ],
)
@pytest.mark.asyncio
async def test_resident_runtime_subject_rejects_untrusted_state(
    test_engine: AsyncEngine,
    mutation: str,
    expected_reason: str,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            backend = await db.get(MLBackendRegistry, backend_id)
            assert fence is not None
            assert backend is not None
            assert backend.health_meta is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            if mutation == "legacy_gate":
                health_meta["residency"]["lifecycle_gate"] = "legacy"
            elif mutation == "draining":
                health_meta["residency"]["draining"] = True
            elif mutation == "pool_unknown":
                health_meta["residency"]["pools"]["models"]["resident"] = None
            elif mutation == "control_stale":
                health_meta["residency"]["control_epoch"] = "2"
            elif mutation == "capability_mismatch":
                health_meta["gpu_arbiter_probe"]["managed_lifecycle_sha256"] = "0" * 64
            else:
                assert mutation == "horizon_missing"
                fence.token_expiry_high_water = None
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        async with factory() as db:
            with pytest.raises(
                GPUResidentRuntimeSubjectError,
                match=expected_reason,
            ):
                await read_gpu_resident_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_runtime_token_horizon_revalidates_subject_before_commit(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        async with factory() as db:
            subject = await read_gpu_resident_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
            )
            original_horizon = (
                await db.get(GPUBackendFence, backend_id)
            ).token_expiry_high_water

        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            assert fence is not None
            fence.control_epoch_high_water = 2

        with pytest.raises(
            GPUResidentRuntimeSubjectError,
            match="residency_control_epoch_mismatch",
        ):
            await record_gpu_resident_runtime_token_expiry(
                factory,
                subject,
                token_expires_at=subject.db_now + timedelta(minutes=2),
            )

        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.token_expiry_high_water == original_horizon
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_resident_dispatch_authority_integrates_db_redis_and_signer(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset(backend_ids),
        )
        repaired = await repair_gpu_resource(
            factory,
            proof_store,
            _RESOURCE_A,
            8192,
            config=_resource_config(_RESOURCE_A),
        )
        assert repaired.ready is True

        private_key = Ed25519PrivateKey.generate()
        signer = GPUAdmissionTokenSigner(
            active_kid="test",
            _private_key=private_key,
        )
        dispatch = build_gpu_dispatch_context_factory(
            factory,
            _ProofEndpointSource(factory),
            _UnusedBackendControlTransport(),
            config=_resource_config(_RESOURCE_A),
            store_factory=lambda: GPUArbiterStore.from_url(
                _redis_url(),
                namespace=proof_store.namespace,
            ),
            signer_factory=lambda: signer,
            health_refresher=_refresh_test_health,
            clock=_ProofDispatchClock(),
        )
        request = GPUDispatchRequest(
            request_id=uuid.uuid4(),
            backend_id=str(backend_id),
            gpu_resource_id=_RESOURCE_A,
            operation="predict",
            scope=AdmissionScope.PREDICT,
        )

        async with dispatch(request) as grant:
            claims = verify_admission_token(
                grant.admission_token,
                keyring={"test": private_key.public_key()},
            )
            assert grant.generation == claims.generation == "1"
            assert claims.backend_registry_id == str(backend_id)
            assert claims.gpu_resource_id == _RESOURCE_A
            assert claims.scope is AdmissionScope.PREDICT
            assert claims.owner is None
            assert claims.operation is None
            snapshot = await proof_store.snapshot(_RESOURCE_A)
            assert tuple(item.lease_id for item in snapshot.leases) == (claims.jti,)
            grant.report_response(200)

        assert (await proof_store.snapshot(_RESOURCE_A)).leases == ()
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.generation_high_water == 1
        assert fence.control_epoch_high_water == 1
        assert fence.token_expiry_high_water == datetime.fromtimestamp(
            claims.exp,
            tz=UTC,
        )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.parametrize(
    ("terminal", "expected_state", "expected_evictable", "expected_committed"),
    (
        ("resident", GPUAllocationState.RESIDENT, True, 1024),
        ("cpu_device", GPUAllocationState.CPU_FALLBACK, False, 0),
        ("cpu_provider", GPUAllocationState.CPU_FALLBACK, False, 0),
        ("unloaded", GPUAllocationState.UNLOADED, False, 0),
        ("unknown_busy", GPUAllocationState.UNKNOWN, False, 1024),
    ),
)
@pytest.mark.asyncio
async def test_cold_terminal_commit_classifies_exact_post_response_health(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    terminal: str,
    expected_state: GPUAllocationState,
    expected_evictable: bool,
    expected_committed: int,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    challenge = "a" * 64
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        await _enable_cold_runtime_health(factory, backend_id)
        prepared, lease_id, owner_id = await _bootstrap_cold_loading(
            factory,
            proof_store,
            backend_id,
            _RESOURCE_A,
        )
        await _install_cold_terminal_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=challenge,
            terminal=terminal,
        )

        result = await commit_gpu_cold_terminal_from_health(
            factory,
            proof_store,
            prepared,
            challenge=challenge,
            lease_id=lease_id,
            owner_id=owner_id,
            resident_cooldown_ms=30_000,
        )

        assert result.status == "finalized"
        assert result.state is expected_state
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == expected_committed
        assert snapshot.allocations[0].state is expected_state
        assert snapshot.allocations[0].evictable is expected_evictable
        assert snapshot.allocations[0].reservation_lease_id is None
        assert tuple(item.lease_id for item in snapshot.leases) == (lease_id,)
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_terminal_commit_retries_exactly_after_redis_response_loss(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    monkeypatch,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    challenge = "e" * 64
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        await _enable_cold_runtime_health(factory, backend_id)
        prepared, lease_id, owner_id = await _bootstrap_cold_loading(
            factory,
            proof_store,
            backend_id,
            _RESOURCE_A,
        )
        await _install_cold_terminal_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=challenge,
            terminal="resident",
        )
        before = await proof_store.snapshot(_RESOURCE_A)
        original_finalize = proof_store.finalize_cold_allocation
        attempts = 0

        async def lose_first_response(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            result = await original_finalize(*args, **kwargs)
            if attempts == 1:
                raise RuntimeError("terminal response lost")
            return result

        monkeypatch.setattr(
            proof_store,
            "finalize_cold_allocation",
            lose_first_response,
        )

        result = await commit_gpu_cold_terminal_from_health(
            factory,
            proof_store,
            prepared,
            challenge=challenge,
            lease_id=lease_id,
            owner_id=owner_id,
            resident_cooldown_ms=30_000,
        )

        assert attempts == 2
        assert result.status == "finalized"
        assert result.state is GPUAllocationState.RESIDENT
        assert result.idempotent is True
        after = await proof_store.snapshot(_RESOURCE_A)
        assert after.ledger_revision == before.ledger_revision + 1
        assert after.allocations[0].state is GPUAllocationState.RESIDENT
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_terminal_commit_rejects_stale_health_challenge(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    challenge = "1" * 64
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        await _enable_cold_runtime_health(factory, backend_id)
        prepared, lease_id, owner_id = await _bootstrap_cold_loading(
            factory,
            proof_store,
            backend_id,
            _RESOURCE_A,
        )
        await _install_cold_terminal_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=challenge,
            terminal="resident",
            stored_challenge="0" * 64,
        )

        result = await commit_gpu_cold_terminal_from_health(
            factory,
            proof_store,
            prepared,
            challenge=challenge,
            lease_id=lease_id,
            owner_id=owner_id,
            resident_cooldown_ms=30_000,
        )

        assert result.status == "finalized"
        assert result.state is GPUAllocationState.UNKNOWN
        assert result.reason == "terminal_challenge_mismatch"
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == 1024
        assert snapshot.allocations[0].state is GPUAllocationState.UNKNOWN
        assert snapshot.allocations[0].evictable is False
        assert tuple(item.lease_id for item in snapshot.leases) == (lease_id,)
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_terminal_commit_skips_redis_when_generation_advanced(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    challenge = "b" * 64
    try:
        await _install_live_health(factory, backend_ids, resource_id=_RESOURCE_A)
        await _enable_cold_runtime_health(factory, backend_id)
        prepared, lease_id, owner_id = await _bootstrap_cold_loading(
            factory,
            proof_store,
            backend_id,
            _RESOURCE_A,
        )
        await _install_cold_terminal_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=challenge,
            terminal="resident",
        )
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, backend_id)
            assert fence is not None
            fence.generation_high_water = 2
        before = await proof_store.snapshot(_RESOURCE_A)

        result = await commit_gpu_cold_terminal_from_health(
            factory,
            proof_store,
            prepared,
            challenge=challenge,
            lease_id=lease_id,
            owner_id=owner_id,
            resident_cooldown_ms=30_000,
        )

        assert result.status == "stale"
        assert result.reason == "generation_changed"
        after = await proof_store.snapshot(_RESOURCE_A)
        assert after.ledger_revision == before.ledger_revision
        assert after.allocations[0].state is GPUAllocationState.LOADING
        assert after.allocations[0].reservation_lease_id == lease_id
        assert tuple(item.lease_id for item in after.leases) == (lease_id,)
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_terminal_commit_preserves_same_card_resident_sibling(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(512, 1024),
    )
    sibling_id, target_id = backend_ids
    challenge = "f" * 64
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({sibling_id}),
        )
        await _enable_cold_runtime_health(factory, target_id)
        sibling_observed_at_ms = int(time.time() * 1000)
        reconciled = await proof_store.reconcile_card(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=await _membership_domain(factory, _RESOURCE_A),
            allocations=(
                GPUAllocation(
                    backend_id=str(sibling_id),
                    state=GPUAllocationState.UNKNOWN,
                    budget_mb=512,
                    generation="1",
                    eviction_priority=0,
                    evictable=False,
                    max_concurrency=4,
                    reservation_lease_id=None,
                    reservation_owner_id=None,
                    last_used_at_ms=sibling_observed_at_ms,
                    not_evict_before_ms=0,
                ),
            ),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=int(time.time() * 1000) + 120_000,
            repair_id=f"cold-terminal-sibling-{uuid.uuid4()}",
        )
        assert reconciled.status == "reconciled", reconciled.reason
        unknown_snapshot = await proof_store.snapshot(_RESOURCE_A)
        reconciled = await proof_store.reconcile_card(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=unknown_snapshot.ledger_revision,
            expected_ledger_incarnation=unknown_snapshot.ledger_incarnation,
            backend_memberships=await _membership_domain(factory, _RESOURCE_A),
            allocations=(
                GPUAllocation(
                    backend_id=str(sibling_id),
                    state=GPUAllocationState.RESIDENT,
                    budget_mb=512,
                    generation="1",
                    eviction_priority=0,
                    evictable=False,
                    max_concurrency=4,
                    reservation_lease_id=None,
                    reservation_owner_id=None,
                    last_used_at_ms=sibling_observed_at_ms,
                    not_evict_before_ms=sibling_observed_at_ms + 30_000,
                ),
            ),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=int(time.time() * 1000) + 120_000,
            repair_id=f"cold-terminal-sibling-resident-{uuid.uuid4()}",
        )
        assert reconciled.status == "reconciled", reconciled.reason
        prepared, lease_id, owner_id = await _bootstrap_cold_loading(
            factory,
            proof_store,
            target_id,
            _RESOURCE_A,
            bootstrap_card=False,
        )
        await _install_cold_terminal_health(
            factory,
            target_id,
            _RESOURCE_A,
            challenge=challenge,
            terminal="unloaded",
        )

        result = await commit_gpu_cold_terminal_from_health(
            factory,
            proof_store,
            prepared,
            challenge=challenge,
            lease_id=lease_id,
            owner_id=owner_id,
            resident_cooldown_ms=30_000,
        )

        assert result.state is GPUAllocationState.UNLOADED
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        allocations = {item.backend_id: item for item in snapshot.allocations}
        assert snapshot.committed_mb == 512
        assert allocations[str(sibling_id)].state is GPUAllocationState.RESIDENT
        assert allocations[str(sibling_id)].generation == "1"
        assert allocations[str(sibling_id)].evictable is False
        assert allocations[str(target_id)].state is GPUAllocationState.UNLOADED
        assert tuple(item.lease_id for item in snapshot.leases) == (lease_id,)
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_cold_terminal_commit_isolated_across_cards(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory_a, backend_ids_a = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    factory_b, backend_ids_b = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_B,
        budgets=(2048,),
    )
    backend_a = backend_ids_a[0]
    backend_b = backend_ids_b[0]
    try:
        await _install_live_health(
            factory_a,
            backend_ids_a,
            resource_id=_RESOURCE_A,
        )
        await _install_live_health(
            factory_b,
            backend_ids_b,
            resource_id=_RESOURCE_B,
        )
        await _enable_cold_runtime_health(factory_a, backend_a)
        await _enable_cold_runtime_health(factory_b, backend_b)
        prepared_a, lease_a, owner_a = await _bootstrap_cold_loading(
            factory_a,
            proof_store,
            backend_a,
            _RESOURCE_A,
        )
        prepared_b, lease_b, owner_b = await _bootstrap_cold_loading(
            factory_b,
            proof_store,
            backend_b,
            _RESOURCE_B,
        )
        await _install_cold_terminal_health(
            factory_a,
            backend_a,
            _RESOURCE_A,
            challenge="c" * 64,
            terminal="resident",
        )
        await _install_cold_terminal_health(
            factory_b,
            backend_b,
            _RESOURCE_B,
            challenge="d" * 64,
            terminal="unloaded",
        )

        result_a, result_b = await asyncio.gather(
            commit_gpu_cold_terminal_from_health(
                factory_a,
                proof_store,
                prepared_a,
                challenge="c" * 64,
                lease_id=lease_a,
                owner_id=owner_a,
                resident_cooldown_ms=30_000,
            ),
            commit_gpu_cold_terminal_from_health(
                factory_b,
                proof_store,
                prepared_b,
                challenge="d" * 64,
                lease_id=lease_b,
                owner_id=owner_b,
                resident_cooldown_ms=30_000,
            ),
        )

        assert result_a.state is GPUAllocationState.RESIDENT
        assert result_b.state is GPUAllocationState.UNLOADED
        snapshot_a = await proof_store.snapshot(_RESOURCE_A)
        snapshot_b = await proof_store.snapshot(_RESOURCE_B)
        assert snapshot_a.committed_mb == 1024
        assert snapshot_a.allocations[0].backend_id == str(backend_a)
        assert tuple(item.lease_id for item in snapshot_a.leases) == (lease_a,)
        assert snapshot_b.committed_mb == 0
        assert snapshot_b.allocations[0].backend_id == str(backend_b)
        assert tuple(item.lease_id for item in snapshot_b.leases) == (lease_b,)
    finally:
        await _cleanup_backends(factory_a, backend_ids_a)
        await _cleanup_backends(factory_b, backend_ids_b)


@pytest.mark.asyncio
async def test_cold_dispatch_authority_integrates_generation_intent_and_reservation(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(1024,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
        )
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            health_meta = dict(backend.health_meta)
            residency = dict(health_meta["residency"])
            residency["lifecycle_gate"] = "enforce"
            health_meta["residency"] = residency
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        reconciled = await proof_store.reconcile_card(
            _RESOURCE_A,
            8192,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=await _membership_domain(factory, _RESOURCE_A),
            allocations=(),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=int(time.time() * 1000) + 120_000,
            repair_id="cold-authority-bootstrap",
        )
        assert reconciled.status == "reconciled"

        private_key = Ed25519PrivateKey.generate()
        signer = GPUAdmissionTokenSigner(
            active_kid="test",
            _private_key=private_key,
        )
        dispatch = build_gpu_dispatch_context_factory(
            factory,
            _ProofEndpointSource(factory),
            _UnusedBackendControlTransport(),
            config=_resource_config(_RESOURCE_A),
            store_factory=lambda: GPUArbiterStore.from_url(
                _redis_url(),
                namespace=proof_store.namespace,
            ),
            signer_factory=lambda: signer,
            health_refresher=_refresh_test_health,
            clock=_ProofDispatchClock(),
        )
        request = GPUDispatchRequest(
            request_id=uuid.uuid4(),
            backend_id=str(backend_id),
            gpu_resource_id=_RESOURCE_A,
            operation="predict",
            scope=AdmissionScope.PREDICT,
        )

        async with dispatch(request) as grant:
            claims = verify_admission_token(
                grant.admission_token,
                keyring={"test": private_key.public_key()},
            )
            assert grant.generation == claims.generation == "1"
            snapshot = await proof_store.snapshot(_RESOURCE_A)
            assert snapshot.allocations[0].state is GPUAllocationState.LOADING
            assert snapshot.allocations[0].reservation_lease_id == claims.jti
            assert tuple(item.lease_id for item in snapshot.leases) == (claims.jti,)
            grant.report_uncertain_if_missing("request_aborted")

        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.allocations[0].state is GPUAllocationState.UNKNOWN
        assert snapshot.allocations[0].evictable is False
        assert snapshot.leases[0].state.value == "uncertain"
        assert snapshot.transition_present is False
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.generation_high_water == 1
        assert fence.token_expiry_high_water is not None
        assert fence.token_expiry_high_water >= datetime.fromtimestamp(
            claims.exp,
            tz=UTC,
        )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_idle_eviction_subject_advances_generation_and_horizon_atomically(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60,),
    )
    backend_id = backend_ids[0]
    challenge = f"{1:064x}"
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_id}),
        )
        async with factory() as db:
            subject = await read_gpu_idle_eviction_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
                expected_generation="1",
                challenge=challenge,
            )
        assert subject.generation_high_water == 1
        assert subject.generation == "1"
        assert subject.challenge == challenge
        assert subject.backend.url

        with pytest.raises(
            GPUIdleEvictionRuntimeSubjectError,
            match="idle_eviction_challenge_mismatch",
        ):
            async with factory() as db:
                await read_gpu_idle_eviction_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_generation="1",
                    challenge="f" * 64,
                )
        with pytest.raises(
            GPUIdleEvictionRuntimeSubjectError,
            match="idle_eviction_generation_mismatch",
        ):
            async with factory() as db:
                await read_gpu_idle_eviction_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_generation="2",
                    challenge=challenge,
                )

        token_expires_at = subject.db_now + timedelta(seconds=120)
        prepared = await prepare_gpu_idle_eviction_runtime_generation(
            factory,
            subject,
            token_expires_at=token_expires_at,
        )
        assert prepared.source_generation == "1"
        assert prepared.generation == "2"
        assert prepared.token_expires_at == token_expires_at
        assert prepared.backend.url == subject.backend.url
        with pytest.raises(
            GPUIdleEvictionRuntimeSubjectError,
            match="runtime_subject_changed",
        ):
            await prepare_gpu_idle_eviction_runtime_generation(
                factory,
                subject,
                token_expires_at=token_expires_at,
            )
        async with factory() as db:
            fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.generation_high_water == 2
        assert fence.token_expiry_high_water == token_expires_at
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("active_requests", 1),
        ("builders", 1),
        ("borrowers", 1),
        ("evictable", False),
    ),
)
@pytest.mark.asyncio
async def test_idle_eviction_subject_rejects_nonidle_or_protected_victim(
    test_engine: AsyncEngine,
    field: str,
    value: int | bool,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_id}),
        )
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"][field] = value
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")
        with pytest.raises(
            GPUIdleEvictionRuntimeSubjectError,
            match="idle_eviction_runtime_not_ready",
        ):
            async with factory() as db:
                await read_gpu_idle_eviction_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_generation="1",
                    challenge=f"{1:064x}",
                )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_busy_eviction_subject_keeps_strict_identity_and_advances_generation(
    test_engine: AsyncEngine,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60,),
    )
    backend_id = backend_ids[0]
    challenge = f"{1:064x}"
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_id}),
        )
        async with factory() as db:
            quiesced = await read_gpu_busy_eviction_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
                expected_generation="1",
                challenge=challenge,
            )
        assert quiesced.require_idle is False
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["active_requests"] = 2
            health_meta["residency"]["borrowers"] = 1
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")

        with pytest.raises(
            GPUIdleEvictionRuntimeSubjectError,
            match="idle_eviction_runtime_not_ready",
        ):
            async with factory() as db:
                await read_gpu_idle_eviction_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_generation="1",
                    challenge=challenge,
                )

        async with factory() as db:
            subject = await read_gpu_busy_eviction_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
                expected_generation="1",
                challenge=challenge,
            )
        assert subject.require_idle is False
        assert subject.generation == "1"
        prepared = await prepare_gpu_idle_eviction_runtime_generation(
            factory,
            subject,
            token_expires_at=subject.db_now + timedelta(seconds=120),
        )
        assert prepared.require_idle is False
        assert (prepared.source_generation, prepared.generation) == ("1", "2")

        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["evictable"] = False
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")
        with pytest.raises(
            GPUBusyEvictionRuntimeSubjectError,
            match="busy_eviction_runtime_not_ready",
        ):
            async with factory() as db:
                await read_gpu_busy_eviction_runtime_subject(
                    factory,
                    backend_id=str(backend_id),
                    gpu_resource_id=_RESOURCE_A,
                    expected_generation="1",
                    challenge=challenge,
                )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_busy_eviction_cancel_intent_advances_once_and_replays_exactly(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        (
            prepared,
            owner_id,
            owner_hard_deadline_ms,
        ) = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
            allow_busy=True,
        )
        token_expires_at = datetime.fromtimestamp(
            (owner_hard_deadline_ms - 5_000) / 1000,
            UTC,
        )

        with pytest.raises(
            GPUEvictionCancelRuntimeSubjectError,
            match="token_expiry_exceeds_owner_deadline",
        ):
            await prepare_gpu_eviction_cancel_runtime_generation(
                factory,
                prepared,
                owner_id=owner_id,
                owner_hard_deadline_ms=owner_hard_deadline_ms,
                token_expires_at=datetime.fromtimestamp(
                    (owner_hard_deadline_ms + 1) / 1000,
                    UTC,
                ),
            )

        # A never-exposed durable generation gap is legal.  Cancel advances from
        # the current high-water while remaining bound to the exact Redis drain.
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, victim_id)
            assert fence is not None
            fence.generation_high_water = int(prepared.generation) + 2
        expected_cancel_generation = str(int(prepared.generation) + 3)

        first, replayed = await asyncio.gather(
            prepare_gpu_eviction_cancel_runtime_generation(
                factory,
                prepared,
                owner_id=owner_id,
                owner_hard_deadline_ms=owner_hard_deadline_ms,
                token_expires_at=token_expires_at,
            ),
            prepare_gpu_eviction_cancel_runtime_generation(
                factory,
                prepared,
                owner_id=owner_id,
                owner_hard_deadline_ms=owner_hard_deadline_ms,
                token_expires_at=token_expires_at,
            ),
        )
        assert {first.idempotent, replayed.idempotent} == {False, True}
        assert (
            first.drain_generation == replayed.drain_generation == prepared.generation
        )
        assert first.generation == replayed.generation == expected_cancel_generation
        assert first.jti == replayed.jti
        assert first.owner_id == replayed.owner_id == owner_id
        assert first.operation == replayed.operation == "evict"
        assert (
            first.owner_hard_deadline_ms
            == replayed.owner_hard_deadline_ms
            == owner_hard_deadline_ms
        )
        assert first.pool_ids == replayed.pool_ids == prepared.pool_ids

        recovered = await read_gpu_eviction_cancel_runtime_subject(
            factory,
            backend_id=str(victim_id),
            gpu_resource_id=_RESOURCE_A,
            owner_id=owner_id,
        )
        assert recovered.idempotent is True
        assert recovered.generation == first.generation
        assert recovered.jti == first.jti
        assert recovered.token_expires_at == first.token_expires_at

        async with factory() as db:
            fence = await db.get(GPUBackendFence, victim_id)
            intent = await db.get(GPUBackendCancelIntent, victim_id)
        assert fence is not None
        assert intent is not None
        assert fence.generation_high_water == int(first.generation)
        assert fence.token_expiry_high_water == max(
            prepared.token_expires_at,
            token_expires_at,
        )
        assert intent.drain_generation == int(prepared.generation)
        assert intent.generation == int(first.generation)
        assert intent.jti == first.jti
        assert tuple(intent.pool_ids) == prepared.pool_ids
        assert intent.owner_hard_deadline_ms == owner_hard_deadline_ms

        for changed in (
            {"owner_id": f"{owner_id}:other"},
            {"owner_hard_deadline_ms": owner_hard_deadline_ms - 1},
            {"token_expires_at": token_expires_at + timedelta(seconds=1)},
        ):
            kwargs = {
                "owner_id": owner_id,
                "owner_hard_deadline_ms": owner_hard_deadline_ms,
                "token_expires_at": token_expires_at,
                **changed,
            }
            with pytest.raises(
                GPUEvictionCancelRuntimeSubjectError,
                match="cancel_intent_conflict",
            ):
                await prepare_gpu_eviction_cancel_runtime_generation(
                    factory,
                    prepared,
                    **kwargs,
                )
        async with factory() as db:
            fence_after = await db.get(GPUBackendFence, victim_id)
        assert fence_after is not None
        assert fence_after.generation_high_water == int(first.generation)
        assert fence_after.token_expiry_high_water == max(
            prepared.token_expires_at,
            token_expires_at,
        )

        with pytest.raises(
            GPUEvictionCancelRuntimeSubjectError,
            match="cancel_intent_identity_changed",
        ):
            await read_gpu_eviction_cancel_runtime_subject(
                factory,
                backend_id=str(victim_id),
                gpu_resource_id=_RESOURCE_A,
                owner_id=f"{owner_id}:other",
            )
        async with factory.begin() as db:
            intent = await db.get(GPUBackendCancelIntent, victim_id)
            assert intent is not None
            prefix = "0" if intent.subject_fingerprint[0] != "0" else "1"
            intent.subject_fingerprint = prefix + intent.subject_fingerprint[1:]
        with pytest.raises(
            GPUEvictionCancelRuntimeSubjectError,
            match="cancel_intent_source_changed",
        ):
            await read_gpu_eviction_cancel_runtime_subject(
                factory,
                backend_id=str(victim_id),
                gpu_resource_id=_RESOURCE_A,
                owner_id=owner_id,
            )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_busy_eviction_cancel_commit_requires_ack_and_fresh_resident_health(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        (
            prepared,
            owner_id,
            owner_hard_deadline_ms,
        ) = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
            allow_busy=True,
        )
        before = await proof_store.snapshot(_RESOURCE_A)
        victim_before = next(
            item for item in before.allocations if item.backend_id == str(victim_id)
        )
        cancel_subject = await prepare_gpu_eviction_cancel_runtime_generation(
            factory,
            prepared,
            owner_id=owner_id,
            owner_hard_deadline_ms=owner_hard_deadline_ms,
            token_expires_at=datetime.fromtimestamp(
                (owner_hard_deadline_ms - 5_000) / 1000,
                UTC,
            ),
        )
        async with factory.begin() as db:
            intent = await db.get(GPUBackendCancelIntent, victim_id)
            assert intent is not None
            original_jti = intent.jti
            intent.jti = f"{original_jti}:changed"
        stale_intent = await commit_gpu_eviction_cancel_from_health(
            factory,
            proof_store,
            cancel_subject,
            ack_confirmed=True,
            challenge="8" * 64,
        )
        assert (stale_intent.status, stale_intent.reason) == (
            "stale",
            "cancel_intent_changed",
        )
        assert (await proof_store.snapshot(_RESOURCE_A)).allocations[0].state is (
            GPUAllocationState.DRAINING
        )
        async with factory.begin() as db:
            intent = await db.get(GPUBackendCancelIntent, victim_id)
            assert intent is not None
            intent.jti = original_jti
        assert (
            await proof_store.arm_eviction_cancel(
                _RESOURCE_A,
                backend_id=str(victim_id),
                expected_generation=cancel_subject.drain_generation,
                transition_owner_id=owner_id,
            )
        ).status == "armed"
        challenge = "8" * 64
        await _install_eviction_phase_health(
            factory,
            victim_id,
            _RESOURCE_A,
            challenge=challenge,
            generation=cancel_subject.generation,
            phase="resume",
            active_requests=2,
            builders=1,
            borrowers=1,
        )

        original_transition = proof_store.transition_eviction_allocation
        transition_calls = 0

        async def lose_first_transition_response(*args, **kwargs):
            nonlocal transition_calls
            transition_calls += 1
            result = await original_transition(*args, **kwargs)
            if transition_calls == 1:
                raise TimeoutError("simulated Redis response loss")
            return result

        monkeypatch.setattr(
            proof_store,
            "transition_eviction_allocation",
            lose_first_transition_response,
        )
        committed = await commit_gpu_eviction_cancel_from_health(
            factory,
            proof_store,
            cancel_subject,
            ack_confirmed=True,
            challenge=challenge,
        )
        assert (committed.status, committed.state, committed.reason) == (
            "finalized",
            GPUAllocationState.RESIDENT,
            "cancelled_resident",
        )
        assert committed.idempotent is True
        assert transition_calls == 2

        after = await proof_store.snapshot(_RESOURCE_A)
        victim_after = next(
            item for item in after.allocations if item.backend_id == str(victim_id)
        )
        assert (
            victim_after.generation,
            victim_after.state,
            victim_after.evictable,
            victim_after.not_evict_before_ms,
        ) == (
            cancel_subject.generation,
            GPUAllocationState.RESIDENT,
            True,
            victim_before.not_evict_before_ms,
        )
        assert after.committed_mb == before.committed_mb == 60
        assert [(item.lease_id, item.generation) for item in after.leases] == [
            (before.leases[0].lease_id, "1")
        ]

        replayed = await commit_gpu_eviction_cancel_from_health(
            factory,
            proof_store,
            cancel_subject,
            ack_confirmed=True,
            challenge=challenge,
        )
        assert (replayed.status, replayed.idempotent) == ("finalized", True)
        assert transition_calls == 3
        assert (await proof_store.snapshot(_RESOURCE_A)).ledger_revision == (
            after.ledger_revision
        )

        late_unload = await original_transition(
            _RESOURCE_A,
            backend_id=str(victim_id),
            expected_state=GPUAllocationState.DRAINING,
            expected_generation=cancel_subject.drain_generation,
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id=owner_id,
        )
        assert late_unload.status == "stale_generation"
        assert (await proof_store.snapshot(_RESOURCE_A)).allocations == (
            after.allocations
        )
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.parametrize(
    ("ack_confirmed", "challenge", "corrupt_pool", "expected_reason"),
    (
        (False, "9" * 64, False, "cancel_ack_uncertain"),
        (True, None, False, "cancel_health_uncertain"),
        (
            True,
            "a" * 64,
            True,
            "eviction_residency_identity_mismatch",
        ),
    ),
)
@pytest.mark.asyncio
async def test_busy_eviction_cancel_uncertainty_never_reopens_resident(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    ack_confirmed: bool,
    challenge: str | None,
    corrupt_pool: bool,
    expected_reason: str,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        (
            prepared,
            owner_id,
            owner_hard_deadline_ms,
        ) = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
            allow_busy=True,
        )
        cancel_subject = await prepare_gpu_eviction_cancel_runtime_generation(
            factory,
            prepared,
            owner_id=owner_id,
            owner_hard_deadline_ms=owner_hard_deadline_ms,
            token_expires_at=datetime.fromtimestamp(
                (owner_hard_deadline_ms - 5_000) / 1000,
                UTC,
            ),
        )
        assert (
            await proof_store.arm_eviction_cancel(
                _RESOURCE_A,
                backend_id=str(victim_id),
                expected_generation=cancel_subject.drain_generation,
                transition_owner_id=owner_id,
            )
        ).status == "armed"
        if challenge is not None:
            await _install_eviction_phase_health(
                factory,
                victim_id,
                _RESOURCE_A,
                challenge=challenge,
                generation=cancel_subject.generation,
                phase="resume",
                active_requests=1,
            )
        if corrupt_pool:
            async with factory.begin() as db:
                backend = await db.get(MLBackendRegistry, victim_id)
                assert backend is not None
                health_meta = json.loads(json.dumps(backend.health_meta))
                health_meta["residency"]["pools"]["unexpected"] = health_meta[
                    "residency"
                ]["pools"].pop("models")
                backend.health_meta = health_meta
                flag_modified(backend, "health_meta")

        result = await commit_gpu_eviction_cancel_from_health(
            factory,
            proof_store,
            cancel_subject,
            ack_confirmed=ack_confirmed,
            challenge=challenge,
        )
        assert (result.status, result.state, result.reason) == (
            "finalized",
            GPUAllocationState.UNKNOWN,
            expected_reason,
        )
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        victim = next(
            item for item in snapshot.allocations if item.backend_id == str(victim_id)
        )
        assert (victim.state, victim.generation, victim.evictable) == (
            GPUAllocationState.UNKNOWN,
            cancel_subject.drain_generation,
            False,
        )
        assert snapshot.committed_mb == 60
        assert len(snapshot.leases) == 1
        assert snapshot.leases[0].generation == "1"
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_busy_eviction_drain_health_classifies_all_activity_domains_read_only(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60,),
    )
    backend_id = backend_ids[0]
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({backend_id}),
        )
        async with factory() as db:
            subject = await read_gpu_busy_eviction_runtime_subject(
                factory,
                backend_id=str(backend_id),
                gpu_resource_id=_RESOURCE_A,
                expected_generation="1",
                challenge=f"{1:064x}",
            )
        prepared = await prepare_gpu_idle_eviction_runtime_generation(
            factory,
            subject,
            token_expires_at=subject.db_now + timedelta(seconds=120),
        )
        async with factory() as db:
            fence_before = await db.get(GPUBackendFence, backend_id)
            assert fence_before is not None
            durable_before = (
                fence_before.generation_high_water,
                fence_before.token_expiry_high_water,
            )

        activity_domains = (
            {"active_requests": 2, "builders": 0, "borrowers": 0},
            {"active_requests": 0, "builders": 1, "borrowers": 0},
            {"active_requests": 0, "builders": 0, "borrowers": 1},
        )
        for index, counters in enumerate(activity_domains, start=2):
            challenge = f"{index:064x}"
            await _install_eviction_phase_health(
                factory,
                backend_id,
                _RESOURCE_A,
                challenge=challenge,
                generation=prepared.generation,
                phase="busy",
                **counters,
            )
            async with factory() as db:
                classified = await read_gpu_eviction_drain_health(
                    factory,
                    prepared,
                    challenge=challenge,
                )
            assert classified.status == "draining_busy"
            assert (
                classified.active_requests,
                classified.builders,
                classified.borrowers,
            ) == (
                counters["active_requests"],
                counters["builders"],
                counters["borrowers"],
            )

        ready_challenge = f"{5:064x}"
        await _install_eviction_phase_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=ready_challenge,
            generation=prepared.generation,
            phase="drain",
        )
        async with factory() as db:
            ready = await read_gpu_eviction_drain_health(
                factory,
                prepared,
                challenge=ready_challenge,
            )
            fence_after = await db.get(GPUBackendFence, backend_id)
        assert ready.status == "ready_to_unload"
        assert (ready.active_requests, ready.builders, ready.borrowers) == (0, 0, 0)
        assert fence_after is not None
        assert (
            fence_after.generation_high_water,
            fence_after.token_expiry_high_water,
        ) == durable_before

        async with factory() as db:
            stale_challenge = await read_gpu_eviction_drain_health(
                factory,
                prepared,
                challenge="f" * 64,
            )
        assert (stale_challenge.status, stale_challenge.reason) == (
            "uncertain",
            "eviction_challenge_mismatch",
        )
        assert stale_challenge.active_requests is None

        invalid_challenge = f"{6:064x}"
        await _install_eviction_phase_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=invalid_challenge,
            generation=str(int(prepared.generation) + 1),
            phase="busy",
        )
        async with factory() as db:
            uncertain = await read_gpu_eviction_drain_health(
                factory,
                prepared,
                challenge=invalid_challenge,
            )
        assert (uncertain.status, uncertain.reason) == (
            "uncertain",
            "eviction_residency_identity_mismatch",
        )
        assert uncertain.active_requests is None

        pool_challenge = f"{7:064x}"
        await _install_eviction_phase_health(
            factory,
            backend_id,
            _RESOURCE_A,
            challenge=pool_challenge,
            generation=prepared.generation,
            phase="busy",
        )
        async with factory.begin() as db:
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend is not None
            health_meta = json.loads(json.dumps(backend.health_meta))
            health_meta["residency"]["pools"]["unexpected"] = {
                "resident": True,
                "device": "cuda:0",
                "provider": None,
            }
            backend.health_meta = health_meta
            flag_modified(backend, "health_meta")
        async with factory() as db:
            pool_uncertain = await read_gpu_eviction_drain_health(
                factory,
                prepared,
                challenge=pool_challenge,
            )
        assert (pool_uncertain.status, pool_uncertain.reason) == (
            "uncertain",
            "eviction_residency_identity_mismatch",
        )

    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_eviction_phase_commit_proves_drain_and_unload_exactly(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        prepared, owner_id, _ = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
        )
        # A concurrent preparation may burn a later generation without exposing it.
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, victim_id)
            assert fence is not None
            fence.generation_high_water = int(prepared.generation) + 1
        drain_challenge = "a" * 64
        await _install_eviction_phase_health(
            factory,
            victim_id,
            _RESOURCE_A,
            challenge=drain_challenge,
            generation=prepared.generation,
            phase="drain",
        )
        drain = await commit_gpu_eviction_phase_from_health(
            factory,
            proof_store,
            prepared,
            phase="drain",
            challenge=drain_challenge,
            owner_id=owner_id,
        )
        assert (drain.status, drain.state, drain.reason) == (
            "finalized",
            GPUAllocationState.UNLOADING,
            "ready_to_unload",
        )
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == 60
        assert snapshot.allocations[0].state is GPUAllocationState.UNLOADING

        unload_challenge = "b" * 64
        await _install_eviction_phase_health(
            factory,
            victim_id,
            _RESOURCE_A,
            challenge=unload_challenge,
            generation=prepared.generation,
            phase="unload",
        )
        original_transition = proof_store.transition_eviction_allocation
        transition_calls = 0

        async def lose_first_transition_response(*args, **kwargs):
            nonlocal transition_calls
            transition_calls += 1
            result = await original_transition(*args, **kwargs)
            if transition_calls == 1:
                raise TimeoutError("simulated Redis response loss")
            return result

        monkeypatch.setattr(
            proof_store,
            "transition_eviction_allocation",
            lose_first_transition_response,
        )
        unloaded = await commit_gpu_eviction_phase_from_health(
            factory,
            proof_store,
            prepared,
            phase="unload",
            challenge=unload_challenge,
            owner_id=owner_id,
        )
        assert (unloaded.status, unloaded.state, unloaded.reason) == (
            "finalized",
            GPUAllocationState.UNLOADED,
            "unloaded",
        )
        assert unloaded.idempotent is True
        assert transition_calls == 2
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == 0
        assert snapshot.allocations[0].state is GPUAllocationState.UNLOADED

        retried = await commit_gpu_eviction_phase_from_health(
            factory,
            proof_store,
            prepared,
            phase="unload",
            challenge=unload_challenge,
            owner_id=owner_id,
        )
        assert (retried.status, retried.idempotent) == ("finalized", True)
        assert (await proof_store.snapshot(_RESOURCE_A)).committed_mb == 0
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_eviction_drain_uncertainty_becomes_unknown_without_releasing_budget(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        prepared, owner_id, _ = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
        )
        result = await commit_gpu_eviction_phase_from_health(
            factory,
            proof_store,
            prepared,
            phase="drain",
            challenge=None,
            owner_id=owner_id,
        )
        assert (result.status, result.state, result.reason) == (
            "finalized",
            GPUAllocationState.UNKNOWN,
            "eviction_response_uncertain",
        )
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == 60
        assert snapshot.allocations[0].state is GPUAllocationState.UNKNOWN
        assert snapshot.allocations[0].evictable is False
    finally:
        await _cleanup_backends(factory, backend_ids)


@pytest.mark.asyncio
async def test_eviction_phase_commit_skips_redis_after_durable_control_changes(
    test_engine: AsyncEngine,
    proof_store: GPUArbiterStore,
) -> None:
    factory, backend_ids = await _create_active_backends(
        test_engine,
        resource_id=_RESOURCE_A,
        budgets=(60, 50),
    )
    victim_id, requester_id = backend_ids
    try:
        await _install_live_health(
            factory,
            backend_ids,
            resource_id=_RESOURCE_A,
            resident_backend_ids=frozenset({victim_id}),
        )
        prepared, owner_id, _ = await _prepare_selected_idle_eviction(
            factory,
            proof_store,
            victim_id=victim_id,
            requester_id=requester_id,
            resource_id=_RESOURCE_A,
        )
        async with factory.begin() as db:
            fence = await db.get(GPUBackendFence, victim_id)
            assert fence is not None
            fence.control_epoch_high_water = 2
        challenge = "c" * 64
        await _install_eviction_phase_health(
            factory,
            victim_id,
            _RESOURCE_A,
            challenge=challenge,
            generation=prepared.generation,
            phase="drain",
        )
        result = await commit_gpu_eviction_phase_from_health(
            factory,
            proof_store,
            prepared,
            phase="drain",
            challenge=challenge,
            owner_id=owner_id,
        )
        assert (result.status, result.reason) == ("stale", "control_epoch_changed")
        snapshot = await proof_store.snapshot(_RESOURCE_A)
        assert snapshot.committed_mb == 60
        assert snapshot.allocations[0].state is GPUAllocationState.DRAINING
    finally:
        await _cleanup_backends(factory, backend_ids)
