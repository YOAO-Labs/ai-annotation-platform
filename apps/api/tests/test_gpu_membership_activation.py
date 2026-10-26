from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
import uuid

import asyncpg
import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    LifecycleModeResponse,
    ManagedLifecycleCapabilities,
    managed_lifecycle_capability_sha256,
    verify_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.backend_endpoint import (
    BackendEndpointBinding,
    BackendEndpointSnapshot,
)
from aap_inference_contract.backend_endpoint import BackendEndpointAuthority
from aap_inference.gpu.fences import (
    activate_gpu_backend_membership,
    advance_gpu_backend_fence,
)
from aap_inference.gpu.membership_activation import (
    promote_gpu_backend_membership,
    promote_gpu_resource_memberships,
)
from aap_inference.gpu.native import GPUAuthorityPoolData
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from sqlalchemy.engine import make_url


class _TestOwnedEndpointSource:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def open_owned(
        self,
        backend_registry_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        purpose: str,
        binding=None,
    ) -> BackendEndpointSnapshot:
        if purpose == "gpu_control" and binding is None:
            raise RuntimeError("GPU control requires a live test binding")
        authority = (
            None
            if binding is None
            else BackendEndpointAuthority(
                kind=binding.kind,
                scope_id=binding.scope_id,
                control_id=binding.control_id,
                attempt_id=binding.attempt_id,
            )
        )
        async with self._factory() as db:
            backend = await db.get(MLBackendRegistry, backend_registry_id)
        if backend is None:
            raise RuntimeError("test backend endpoint is unavailable")
        return BackendEndpointSnapshot(
            registry_id=backend.id,
            request_id=request_id,
            purpose=purpose,
            authority=authority,
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


class _TestBackendControlTransport:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.mode_handler = None

    async def control_binding(self, _backend_id, *, request_id, operation):
        yield BackendEndpointBinding(
            kind="control",
            scope_id=None,
            control_id=uuid.uuid4(),
            attempt_id=request_id,
            capability="0" * 64,
        )

    async def lifecycle_mode(
        self,
        endpoint: BackendEndpointSnapshot,
        request,
        admission_token: str,
        *,
        request_id: uuid.UUID,
        timeout_seconds: float,
    ) -> LifecycleModeResponse:
        if self.mode_handler is not None:
            return await self.mode_handler(endpoint, request, admission_token)
        async with self._factory() as db:
            backend = await db.get(MLBackendRegistry, endpoint.registry_id)
        assert backend is not None
        boot_id = backend.health_meta["residency"]["boot_id"]
        return _ack(
            endpoint.registry_id,
            endpoint.gpu_resource_id,
            boot_id=boot_id,
            control_epoch=request.control_epoch,
        )


@dataclass
class _PromotionHarness:
    factory: async_sessionmaker[AsyncSession]
    backend_ids: list[uuid.UUID]
    authority_data: GPUAuthorityPoolData
    endpoint_source: _TestOwnedEndpointSource
    backend_transport: _TestBackendControlTransport

    def __iter__(self):
        yield self.factory
        yield self.backend_ids


def _set_mode_handler(harness: _PromotionHarness, response_handler) -> None:
    async def handle(endpoint, request, admission_token):
        async with harness.factory() as db:
            backend = await db.get(MLBackendRegistry, endpoint.registry_id)
        assert backend is not None
        client = response_handler(backend)
        return await client.lifecycle_mode(
            request,
            admission_token=admission_token,
        )

    harness.backend_transport.mode_handler = handle


async def _promote_backend(
    harness: _PromotionHarness,
    signer: GPUAdmissionTokenSigner,
    backend_id: uuid.UUID,
    *,
    gpu_resource_id: str,
    membership_epoch: int,
    readiness_demoter=None,
    mode_timeout_seconds: float = 8.0,
    mode_response_handler=None,
):
    if mode_response_handler is not None:
        _set_mode_handler(harness, mode_response_handler)
    return await promote_gpu_backend_membership(
        harness.authority_data,
        signer,
        harness.endpoint_source,
        harness.backend_transport,
        backend_id,
        gpu_resource_id=gpu_resource_id,
        membership_epoch=membership_epoch,
        mode_timeout_seconds=mode_timeout_seconds,
        readiness_demoter=readiness_demoter,
    )


async def _promote_resource(
    harness: _PromotionHarness,
    gpu_resource_id: str,
    *,
    signer_factory,
    readiness_demoter=None,
    pending_only: bool = False,
    mode_response_handler=None,
):
    if mode_response_handler is not None:
        _set_mode_handler(harness, mode_response_handler)
    return await promote_gpu_resource_memberships(
        harness.authority_data,
        gpu_resource_id,
        signer_factory=signer_factory,
        endpoint_source=harness.endpoint_source,
        backend_transport=harness.backend_transport,
        readiness_demoter=readiness_demoter,
        pending_only=pending_only,
    )


_RESOURCE_A = "node-promotion/GPU-a"
_RESOURCE_B = "node-promotion/GPU-b"
_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807


def _proof_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _capability() -> dict:
    return ManagedLifecycleCapabilities().model_dump(mode="json")


def _residency(
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    boot_id: str,
    identity_bound: bool = False,
    control_epoch: str | None = None,
    active_requests: int = 0,
) -> dict:
    return {
        "state": "unloaded",
        "gpu_loaded": False,
        "active_requests": active_requests,
        "builders": 0,
        "borrowers": 0,
        "draining": False,
        "evictable": False,
        "generation": None,
        "pools": {
            "models": {
                "resident": False,
                "device": None,
                "provider": None,
            }
        },
        "boot_id": boot_id,
        "lifecycle_gate": "legacy",
        "control_epoch": control_epoch,
        "identity": (
            {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": resource_id,
            }
            if identity_bound
            else None
        ),
    }


async def _install_health(
    factory: async_sessionmaker[AsyncSession],
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    boot_id: str,
    identity_bound: bool = False,
    control_epoch: str | None = None,
    active_requests: int = 0,
    capability: dict | None = None,
) -> None:
    async with factory.begin() as db:
        backend = await db.get(MLBackendRegistry, backend_id)
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, resource_id),
        )
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert backend is not None
        assert membership is not None
        assert isinstance(db_now, datetime)
        probe_started_at = db_now - timedelta(seconds=2)
        observed_at = db_now - timedelta(seconds=1)
        managed_lifecycle = capability if capability is not None else _capability()
        capability_sha256 = (
            managed_lifecycle_capability_sha256(managed_lifecycle)
            if managed_lifecycle
            else None
        )
        backend.state = "connected"
        backend.last_checked_at = observed_at
        backend.health_meta = {
            "capabilities": {"managed_lifecycle": managed_lifecycle or None},
            "gpu_arbiter_probe": {
                "protocol_version": "1",
                "challenge": "a" * 64,
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
                boot_id=boot_id,
                identity_bound=identity_bound,
                control_epoch=control_epoch,
                active_requests=active_requests,
            ),
        }


async def _create_pending_backend(
    factory: async_sessionmaker[AsyncSession],
    resource_id: str = _RESOURCE_A,
    *,
    boot_id: str | None = None,
    url: str | None = None,
) -> uuid.UUID:
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name=f"gpu-promotion-{backend_id}",
                url=url or f"http://gpu-promotion-{backend_id}.test",
                gpu_resource_id=resource_id,
                vram_budget_mb=1024,
                eviction_priority=2,
                extra_params={"max_concurrency": 4},
            )
        )
    await _install_health(
        factory,
        backend_id,
        resource_id,
        boot_id=boot_id or f"boot-{backend_id}",
    )
    return backend_id


async def _cleanup_backends(
    factory: async_sessionmaker[AsyncSession],
    backend_ids: list[uuid.UUID],
) -> None:
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


@pytest.fixture
async def promotion_db(
    test_engine: AsyncEngine,
) -> AsyncIterator[_PromotionHarness]:
    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    pool = await asyncpg.create_pool(
        dsn=make_url(os.environ["AAP_MODULE_DATA_INFERENCE_GPU_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    backend_ids: list[uuid.UUID] = []
    try:
        yield _PromotionHarness(
            factory=factory,
            backend_ids=backend_ids,
            authority_data=GPUAuthorityPoolData(pool),
            endpoint_source=_TestOwnedEndpointSource(factory),
            backend_transport=_TestBackendControlTransport(factory),
        )
    finally:
        await _cleanup_backends(factory, backend_ids)
        await pool.close()


def _signer() -> tuple[GPUAdmissionTokenSigner, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    return (
        GPUAdmissionTokenSigner(active_kid="test", _private_key=private_key),
        private_key,
    )


async def _demote_ready(_resource_id: str) -> None:
    return None


def _is_serialization_failure(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == "40001"


def _ack(
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    boot_id: str,
    control_epoch: str,
) -> LifecycleModeResponse:
    return LifecycleModeResponse.model_validate(
        {
            "ok": True,
            "gate": "legacy",
            "control_epoch": control_epoch,
            "residency": _residency(
                backend_id,
                resource_id,
                boot_id=boot_id,
                identity_bound=True,
                control_epoch=control_epoch,
            ),
        }
    )


@pytest.mark.asyncio
async def test_pending_membership_atomically_activates_and_receives_signed_ack(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    signer, private_key = _signer()
    requests: list[tuple[object, object]] = []

    class Client:
        def __init__(self, backend: MLBackendRegistry) -> None:
            assert backend.id == backend_id

        async def lifecycle_mode(self, request, *, admission_token):
            claims = verify_admission_token(
                admission_token,
                keyring={"test": private_key.public_key()},
            )
            requests.append((request, claims))
            return _ack(
                backend_id,
                _RESOURCE_A,
                boot_id=f"boot-{backend_id}",
                control_epoch=request.control_epoch,
            )

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=Client,
    )

    assert result.status == "promoted"
    assert result.requires_proof_reset is True
    assert result.runtime_epoch == "1"
    assert result.control_epoch == "1"
    assert len(requests) == 1
    request, claims = requests[0]
    assert request.model_dump(mode="json") == {
        "gate": "legacy",
        "control_epoch": "1",
    }
    assert claims.scope is AdmissionScope.MODE
    assert claims.backend_registry_id == str(backend_id)
    assert claims.gpu_resource_id == _RESOURCE_A
    assert claims.boot_id == f"boot-{backend_id}"
    assert claims.generation is None
    assert claims.owner == f"membership:{backend_id}:1"
    assert claims.operation == "mode:legacy:1"
    assert claims.jti == f"mode:{backend_id}:1:1:legacy"

    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "active"
        assert fence.runtime_epoch_high_water == 1
        assert fence.control_epoch_high_water == 1
        assert fence.token_expiry_high_water == datetime.fromtimestamp(
            claims.exp,
            tz=UTC,
        )


async def test_ready_is_revoked_before_mode_http_after_epoch_advance(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    signer, _private_key = _signer()
    mode_entered = asyncio.Event()
    release_mode = asyncio.Event()
    redis_ready = True

    async def demote_ready(resource_id: str) -> None:
        nonlocal redis_ready
        assert resource_id == _RESOURCE_A
        redis_ready = False

    class Client:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            assert redis_ready is False
            mode_entered.set()
            await release_mode.wait()
            return _ack(
                backend_id,
                _RESOURCE_A,
                boot_id=f"boot-{backend_id}",
                control_epoch=request.control_epoch,
            )

    promotion_task = asyncio.create_task(
        _promote_backend(
            promotion_db,
            signer,
            backend_id,
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
            readiness_demoter=demote_ready,
            mode_response_handler=Client,
        )
    )
    try:
        await asyncio.wait_for(mode_entered.wait(), timeout=2)
        assert redis_ready is False
        async with factory() as db:
            membership = await db.get(
                GPUBackendMembership,
                (backend_id, _RESOURCE_A),
            )
            fence = await db.get(GPUBackendFence, backend_id)
            assert membership is not None
            assert fence is not None
            assert membership.state == "active"
            assert fence.control_epoch_high_water == 1
            assert fence.token_expiry_high_water is not None
    finally:
        release_mode.set()

    result = await asyncio.wait_for(promotion_task, timeout=2)
    assert result.status == "promoted"


async def test_signer_failure_keeps_pending_and_skips_mode_http(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)

    def unavailable_signer() -> GPUAdmissionTokenSigner:
        raise RuntimeError("secret unavailable")

    def unexpected_client(_backend):
        raise AssertionError("mode HTTP must not run without a signer")

    demotions: list[str] = []

    async def demote_ready(resource_id: str) -> None:
        demotions.append(resource_id)

    results = await _promote_resource(
        promotion_db,
        _RESOURCE_A,
        signer_factory=unavailable_signer,
        readiness_demoter=demote_ready,
        mode_response_handler=unexpected_client,
    )

    assert [(item.status, item.reason) for item in results] == [
        ("blocked", "signer_unavailable")
    ]
    assert demotions == [_RESOURCE_A]
    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "pending"
        assert fence.runtime_epoch_high_water == 0
        assert fence.control_epoch_high_water == 0
        assert fence.token_expiry_high_water is None


async def test_pending_only_resource_promotion_skips_active_members(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    await activate_gpu_backend_membership(
        promotion_db.authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
    )

    results = await _promote_resource(
        promotion_db,
        _RESOURCE_A,
        signer_factory=lambda: pytest.fail("active member must not load signer"),
        pending_only=True,
    )

    assert results == ()


async def test_mode_failure_does_not_roll_back_durable_activation(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    signer, _private_key = _signer()

    class FailingClient:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            raise RuntimeError("backend unavailable")

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=FailingClient,
    )

    assert result.status == "active_unacked"
    assert result.reason == "mode_ack_failed"

    stale_retry = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail(
            "stale proof must not retry HTTP"
        ),
    )
    assert stale_retry.status == "blocked"
    assert stale_retry.reason == "probe_membership_mismatch"

    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "active"
        assert fence.runtime_epoch_high_water == 1
        assert fence.control_epoch_high_water == 1
        assert fence.token_expiry_high_water is not None


async def test_invalid_ack_leaves_membership_active_unacked(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    signer, _private_key = _signer()

    class WrongBootClient:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            return _ack(
                backend_id,
                _RESOURCE_A,
                boot_id="wrong-boot",
                control_epoch=request.control_epoch,
            )

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=WrongBootClient,
    )

    assert result.status == "active_unacked"
    assert result.reason == "mode_ack_invalid"
    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        assert membership is not None
        assert membership.state == "active"


@pytest.mark.parametrize(
    ("capability", "active_requests", "expected_reason"),
    (
        ({}, 0, "managed_lifecycle_capability_mismatch"),
        (None, 1, "legacy_residency_not_stably_idle"),
    ),
)
async def test_invalid_pending_proof_never_activates(
    promotion_db,
    capability,
    active_requests: int,
    expected_reason: str,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    await _install_health(
        factory,
        backend_id,
        _RESOURCE_A,
        boot_id=f"boot-{backend_id}",
        capability=capability,
        active_requests=active_requests,
    )
    signer, _private_key = _signer()
    demotions: list[str] = []

    async def demote_ready(resource_id: str) -> None:
        demotions.append(resource_id)

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "blocked"
    assert result.reason == expected_reason
    assert demotions == [_RESOURCE_A]
    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "pending"
        assert fence.runtime_epoch_high_water == 0
        assert fence.control_epoch_high_water == 0


async def test_control_epoch_overflow_rolls_back_activation(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    async with factory.begin() as db:
        await db.execute(
            update(GPUBackendFence)
            .where(GPUBackendFence.backend_registry_id == backend_id)
            .values(control_epoch_high_water=_MAX_POSITIVE_INT64)
        )
    signer, _private_key = _signer()

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "unavailable"
    async with factory() as db:
        membership = await db.get(
            GPUBackendMembership,
            (backend_id, _RESOURCE_A),
        )
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "pending"
        assert fence.runtime_epoch_high_water == 0
        assert fence.control_epoch_high_water == _MAX_POSITIVE_INT64
        assert fence.token_expiry_high_water is None


async def test_same_card_duplicate_boot_ids_block_both_members(promotion_db) -> None:
    factory, backend_ids = promotion_db
    first = await _create_pending_backend(factory, boot_id="shared-boot")
    second = await _create_pending_backend(factory, boot_id="shared-boot")
    backend_ids.extend((first, second))
    signer, _private_key = _signer()

    results = await _promote_resource(
        promotion_db,
        _RESOURCE_A,
        signer_factory=lambda: signer,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert len(results) == 2
    assert {item.reason for item in results} == {"lifecycle_boot_id_aliased"}
    async with factory() as db:
        memberships = (
            (
                await db.execute(
                    select(GPUBackendMembership).where(
                        GPUBackendMembership.backend_registry_id.in_((first, second))
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {item.state for item in memberships} == {"pending"}


async def test_cross_card_boot_alias_blocks_even_with_invalid_peer_capability(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    target = await _create_pending_backend(
        factory,
        _RESOURCE_A,
        boot_id="cross-card-shared-boot",
    )
    peer = await _create_pending_backend(
        factory,
        _RESOURCE_B,
        boot_id="cross-card-shared-boot",
    )
    backend_ids.extend((target, peer))
    await _install_health(
        factory,
        peer,
        _RESOURCE_B,
        boot_id="cross-card-shared-boot",
        capability={},
        active_requests=1,
    )
    signer, _private_key = _signer()
    demotions: list[str] = []

    async def demote_ready(resource_id: str) -> None:
        demotions.append(resource_id)

    result = await _promote_backend(
        promotion_db,
        signer,
        target,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "blocked"
    assert result.reason == "lifecycle_boot_id_aliased"
    assert result.requires_proof_reset is True
    assert demotions == [_RESOURCE_A]


@pytest.mark.parametrize(
    ("target_url", "peer_url"),
    (
        (
            "http://gpu-alias.test:80/backend",
            "http://GPU-ALIAS.test/backend/",
        ),
        (
            "http://gpu-dot.test/backend",
            "http://gpu-dot.test/a/../backend/",
        ),
        (
            "http://gpu-percent.test/backend",
            "http://GPU-PERCENT.test./%62ackend",
        ),
        (
            "http://[::1]:80/backend",
            "http://[::1]/backend/",
        ),
        (
            "http://[2001:db8::1]/backend",
            "http://[2001:0db8:0:0:0:0:0:1]/backend",
        ),
        (
            "http://10.0.0.1/backend",
            "http://167772161/backend",
        ),
        (
            "http://10.0.0.1/backend",
            "http://0x0a000001/backend",
        ),
    ),
)
async def test_canonical_endpoint_alias_blocks_cross_card_activation(
    promotion_db,
    target_url: str,
    peer_url: str,
) -> None:
    factory, backend_ids = promotion_db
    target = await _create_pending_backend(
        factory,
        _RESOURCE_A,
        url=target_url,
    )
    peer = await _create_pending_backend(
        factory,
        _RESOURCE_B,
        url=peer_url,
    )
    backend_ids.extend((target, peer))
    signer, _private_key = _signer()

    result = await _promote_backend(
        promotion_db,
        signer,
        target,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "blocked"
    assert result.reason == "lifecycle_endpoint_aliased"
    assert result.requires_proof_reset is True


async def test_busy_card_lock_does_not_hold_global_promotion_barrier(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    blocked_backend = await _create_pending_backend(factory, _RESOURCE_A)
    healthy_backend = await _create_pending_backend(factory, _RESOURCE_B)
    backend_ids.extend((blocked_backend, healthy_backend))
    signer, _private_key = _signer()

    holder = factory()
    transaction = await holder.begin()
    await holder.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
        ),
        {"resource_id": _RESOURCE_A},
    )

    class Client:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            return _ack(
                healthy_backend,
                _RESOURCE_B,
                boot_id=f"boot-{healthy_backend}",
                control_epoch=request.control_epoch,
            )

    try:
        blocked = await _promote_backend(
            promotion_db,
            signer,
            blocked_backend,
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
            readiness_demoter=_demote_ready,
            mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
        )
        promoted = await _promote_backend(
            promotion_db,
            signer,
            healthy_backend,
            gpu_resource_id=_RESOURCE_B,
            membership_epoch=1,
            readiness_demoter=_demote_ready,
            mode_response_handler=Client,
        )
    finally:
        await transaction.rollback()
        await holder.close()

    assert (blocked.status, blocked.reason) == ("blocked", "gpu_resource_busy")
    assert promoted.status == "promoted"


async def test_concurrent_cards_retry_short_global_promotion_contention(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    resources = tuple(f"node-promotion/GPU-concurrent-{index}" for index in range(4))
    backends = tuple(
        [
            await _create_pending_backend(factory, resource_id)
            for resource_id in resources
        ]
    )
    backend_ids.extend(backends)
    signer, _private_key = _signer()

    class Client:
        def __init__(self, backend: MLBackendRegistry) -> None:
            self.backend = backend

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            assert self.backend.gpu_resource_id is not None
            return _ack(
                self.backend.id,
                self.backend.gpu_resource_id,
                boot_id=f"boot-{self.backend.id}",
                control_epoch=request.control_epoch,
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                _promote_backend(
                    promotion_db,
                    signer,
                    backend_id,
                    gpu_resource_id=resource_id,
                    membership_epoch=1,
                    readiness_demoter=_demote_ready,
                    mode_response_handler=Client,
                )
                for backend_id, resource_id in zip(backends, resources, strict=True)
            )
        ),
        timeout=3,
    )

    assert [result.status for result in results] == ["promoted"] * len(resources)


async def test_multi_resource_writer_and_promotion_do_not_deadlock(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    first_backend = await _create_pending_backend(factory, _RESOURCE_A)
    target_backend = await _create_pending_backend(factory, _RESOURCE_B)
    backend_ids.extend((first_backend, target_backend))
    signer, _private_key = _signer()
    promotion_has_resource = asyncio.Event()
    release_promotion = asyncio.Event()
    writer_has_global = asyncio.Event()

    async def pause_before_global(resource_id: str) -> None:
        assert resource_id == _RESOURCE_B
        promotion_has_resource.set()
        await release_promotion.wait()

    promotion_task = asyncio.create_task(
        _promote_backend(
            promotion_db,
            signer,
            target_backend,
            gpu_resource_id=_RESOURCE_B,
            membership_epoch=1,
            readiness_demoter=pause_before_global,
            mode_response_handler=lambda _backend: Client(),
        )
    )
    await asyncio.wait_for(promotion_has_resource.wait(), timeout=2)

    async def update_two_resources() -> None:
        async with factory.begin() as db:
            first = await db.get(MLBackendRegistry, first_backend)
            assert first is not None
            first.state = "error"
            await db.flush()
            writer_has_global.set()

            target = await db.get(MLBackendRegistry, target_backend)
            assert target is not None
            target.state = "error"
            await db.flush()

    class Client:
        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            return _ack(
                target_backend,
                _RESOURCE_B,
                boot_id=f"boot-{target_backend}",
                control_epoch=request.control_epoch,
            )

    writer_task = asyncio.create_task(update_two_resources())
    try:
        await asyncio.wait_for(writer_has_global.wait(), timeout=2)
        # Keep resource B locked until the writer has attempted its second
        # update. Releasing promotion earlier lets its retry race that update.
        with pytest.raises(DBAPIError) as writer_error:
            await asyncio.wait_for(writer_task, timeout=2)
        assert _is_serialization_failure(writer_error.value)
        release_promotion.set()
        result = await asyncio.wait_for(promotion_task, timeout=2)
    finally:
        release_promotion.set()
        for task in (promotion_task, writer_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(promotion_task, writer_task, return_exceptions=True)

    assert result.status == "promoted"


async def test_trigger_writers_fail_fast_when_global_promotion_barrier_is_busy(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db

    async def assert_barrier_busy(
        write: Callable[[AsyncSession], Awaitable[None]],
    ) -> None:
        async with factory.begin() as holder, factory() as writer:
            # Bound database lock waits independently of cold connections and
            # client scheduling. A blocking trigger returns 55P03, not 40001.
            await writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await holder.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('aap:gpu-membership-promotion', 0))"
                )
            )

            async def flush_write() -> None:
                await write(writer)
                await writer.flush()

            with pytest.raises(DBAPIError) as error:
                await asyncio.wait_for(flush_write(), timeout=5)
            assert _is_serialization_failure(error.value)

    async def insert_backend(db: AsyncSession) -> None:
        backend_id = uuid.uuid4()
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name=f"gpu-promotion-{backend_id}",
                url=f"http://gpu-promotion-{backend_id}.test",
                gpu_resource_id=_RESOURCE_B,
                vram_budget_mb=1024,
                eviction_priority=2,
                extra_params={"max_concurrency": 4},
            )
        )

    await assert_barrier_busy(insert_backend)

    backend_id = await _create_pending_backend(factory, _RESOURCE_B)
    backend_ids.append(backend_id)

    async def update_endpoint(db: AsyncSession) -> None:
        backend = await db.get(MLBackendRegistry, backend_id)
        assert backend is not None
        backend.url = f"http://updated-{backend_id}.test"

    await assert_barrier_busy(update_endpoint)

    async def clear_health_proof(db: AsyncSession) -> None:
        await db.execute(
            update(MLBackendRegistry)
            .where(MLBackendRegistry.id == backend_id)
            .values(state="error", health_meta=None, last_checked_at=None)
        )

    await assert_barrier_busy(clear_health_proof)


async def test_multi_resource_trigger_writers_do_not_form_lock_cycle(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    first_backend = await _create_pending_backend(factory, _RESOURCE_A)
    second_backend = await _create_pending_backend(factory, _RESOURCE_B)
    backend_ids.extend((first_backend, second_backend))
    first_has_global = asyncio.Event()
    continue_first = asyncio.Event()

    async def first_writer() -> None:
        async with factory.begin() as db:
            first = await db.get(MLBackendRegistry, first_backend)
            assert first is not None
            first.state = "error"
            await db.flush()
            first_has_global.set()
            await continue_first.wait()
            second = await db.get(MLBackendRegistry, second_backend)
            assert second is not None
            second.state = "error"
            await db.flush()

    async def competing_writer() -> None:
        async with factory.begin() as db:
            second = await db.get(MLBackendRegistry, second_backend)
            assert second is not None
            second.url = f"http://competing-{second_backend}.test"
            await db.flush()

    first_task = asyncio.create_task(first_writer())
    await asyncio.wait_for(first_has_global.wait(), timeout=1)
    competing_task = asyncio.create_task(competing_writer())
    try:
        competing_outcome = (
            await asyncio.gather(
                competing_task,
                return_exceptions=True,
            )
        )[0]
        continue_first.set()
        await asyncio.wait_for(first_task, timeout=1)
    finally:
        continue_first.set()
        for task in (first_task, competing_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first_task, competing_task, return_exceptions=True)

    assert isinstance(competing_outcome, DBAPIError)
    assert _is_serialization_failure(competing_outcome)


async def test_multi_resource_writer_fails_fast_on_busy_later_card(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    first_backend = await _create_pending_backend(factory, _RESOURCE_A)
    second_backend = await _create_pending_backend(factory, _RESOURCE_B)
    backend_ids.extend((first_backend, second_backend))
    holder = factory()
    transaction = await holder.begin()
    await holder.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
        ),
        {"resource_id": _RESOURCE_B},
    )

    async def update_two_resources() -> None:
        async with factory.begin() as db:
            first = await db.get(MLBackendRegistry, first_backend)
            assert first is not None
            first.state = "error"
            await db.flush()
            second = await db.get(MLBackendRegistry, second_backend)
            assert second is not None
            second.state = "error"
            await db.flush()

    writer_task = asyncio.create_task(update_two_resources())
    try:
        with pytest.raises(DBAPIError) as writer_error:
            await asyncio.wait_for(writer_task, timeout=0.5)
        assert _is_serialization_failure(writer_error.value)
    finally:
        await transaction.rollback()
        await holder.close()


async def test_active_unacked_member_recovers_with_larger_control_epoch(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    assert (
        await activate_gpu_backend_membership(
            promotion_db.authority_data,
            backend_id,
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
        )
        == "1"
    )
    assert (
        await advance_gpu_backend_fence(
            promotion_db.authority_data,
            backend_id,
            "control_epoch",
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
            token_expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )
        == "1"
    )
    await _install_health(
        factory,
        backend_id,
        _RESOURCE_A,
        boot_id=f"boot-{backend_id}",
    )
    signer, _private_key = _signer()

    class Client:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            return _ack(
                backend_id,
                _RESOURCE_A,
                boot_id=f"boot-{backend_id}",
                control_epoch=request.control_epoch,
            )

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=Client,
    )

    assert result.status == "promoted"
    assert result.requires_proof_reset is True
    assert result.runtime_epoch == "1"
    assert result.control_epoch == "2"


async def test_fresh_active_ack_is_observed_without_another_mode_call(
    promotion_db,
) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    await activate_gpu_backend_membership(
        promotion_db.authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
    )
    await advance_gpu_backend_fence(
        promotion_db.authority_data,
        backend_id,
        "control_epoch",
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        token_expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    await _install_health(
        factory,
        backend_id,
        _RESOURCE_A,
        boot_id=f"boot-{backend_id}",
        identity_bound=True,
        control_epoch="1",
    )
    signer, _private_key = _signer()

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "acknowledged"
    assert result.control_epoch == "1"
    assert result.requires_proof_reset is False
    async with factory() as db:
        fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.control_epoch_high_water == 1


async def test_pre_horizon_active_ack_requires_proof_reset(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    await activate_gpu_backend_membership(
        promotion_db.authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
    )
    await advance_gpu_backend_fence(
        promotion_db.authority_data,
        backend_id,
        "control_epoch",
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    await _install_health(
        factory,
        backend_id,
        _RESOURCE_A,
        boot_id=f"boot-{backend_id}",
        identity_bound=True,
        control_epoch="1",
    )
    signer, _private_key = _signer()

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=lambda _backend: pytest.fail("unexpected mode HTTP"),
    )

    assert result.status == "acknowledged"
    assert result.requires_proof_reset is True


async def test_stale_bound_ack_recovers_with_new_control_epoch(promotion_db) -> None:
    factory, backend_ids = promotion_db
    backend_id = await _create_pending_backend(factory)
    backend_ids.append(backend_id)
    await activate_gpu_backend_membership(
        promotion_db.authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
    )
    expired = datetime.now(UTC) - timedelta(seconds=10)
    assert (
        await advance_gpu_backend_fence(
            promotion_db.authority_data,
            backend_id,
            "control_epoch",
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
            token_expires_at=expired,
        )
        == "1"
    )
    assert (
        await advance_gpu_backend_fence(
            promotion_db.authority_data,
            backend_id,
            "control_epoch",
            gpu_resource_id=_RESOURCE_A,
            membership_epoch=1,
            token_expires_at=expired,
        )
        == "2"
    )
    await _install_health(
        factory,
        backend_id,
        _RESOURCE_A,
        boot_id=f"boot-{backend_id}",
        identity_bound=True,
        control_epoch="1",
    )
    signer, _private_key = _signer()

    class Client:
        def __init__(self, _backend) -> None:
            pass

        async def lifecycle_mode(self, request, *, admission_token):  # noqa: ARG002
            return _ack(
                backend_id,
                _RESOURCE_A,
                boot_id=f"boot-{backend_id}",
                control_epoch=request.control_epoch,
            )

    result = await _promote_backend(
        promotion_db,
        signer,
        backend_id,
        gpu_resource_id=_RESOURCE_A,
        membership_epoch=1,
        readiness_demoter=_demote_ready,
        mode_response_handler=Client,
    )

    assert result.status == "promoted"
    assert result.control_epoch == "3"
    assert result.requires_proof_reset is True
