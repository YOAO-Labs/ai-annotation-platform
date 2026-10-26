from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import os
import uuid

import asyncpg
import pytest
from aap_protocol_v2.lifecycle import (
    ManagedLifecycleCapabilities,
    managed_lifecycle_capability_sha256,
)
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.db.models.gpu_arbiter_rollout import GPUArbiterRollout
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.gpu.control_preparation import (
    prepare_gpu_backend_rollout_control,
)
from aap_inference.gpu.fences import activate_gpu_backend_membership
from aap_inference.gpu.native import GPUAuthorityPoolData
from aap_inference.gpu.rollout_state import (
    begin_gpu_arbiter_rollout,
    complete_gpu_arbiter_rollout,
)
from aap_inference.gpu.settings import GPUArbiterMode


_RESOURCE = "node-rollout-control/GPU-a"


@pytest.fixture
async def gpu_authority_data() -> AsyncIterator[GPUAuthorityPoolData]:
    dsn = (
        make_url(os.environ["AAP_MODULE_DATA_INFERENCE_GPU_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    try:
        yield GPUAuthorityPoolData(pool)
    finally:
        await pool.close()


async def _native_call(
    authority_data: GPUAuthorityPoolData, operation, *args, **kwargs
):
    return await operation(authority_data, *args, **kwargs)


def _proof_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
async def rollout_control_db(
    test_engine: AsyncEngine,
    gpu_authority_data: GPUAuthorityPoolData,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, list[str]]]:
    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name=f"rollout-control-{backend_id}",
                url=f"http://rollout-control-{backend_id}.test",
                gpu_resource_id=_RESOURCE,
                vram_budget_mb=1024,
                eviction_priority=2,
                extra_params={"max_concurrency": 4},
            )
        )
    await _native_call(
        gpu_authority_data,
        activate_gpu_backend_membership,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
    )
    async with factory.begin() as db:
        fence = await db.get(GPUBackendFence, backend_id)
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert fence is not None
        assert isinstance(db_now, datetime)
        fence.control_epoch_high_water = 1
        fence.token_expiry_high_water = db_now - timedelta(seconds=10)

    demotions: list[str] = []
    try:
        yield factory, backend_id, demotions
    finally:
        async with factory.begin() as db:
            await db.execute(
                text(
                    "ALTER TABLE gpu_arbiter_rollouts DISABLE TRIGGER "
                    "trg_validate_gpu_arbiter_rollout"
                )
            )
            await db.execute(
                delete(GPUArbiterRollout).where(
                    GPUArbiterRollout.gpu_resource_id == _RESOURCE
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_arbiter_rollouts ENABLE TRIGGER "
                    "trg_validate_gpu_arbiter_rollout"
                )
            )
            await db.execute(
                update(GPUBackendFence)
                .where(GPUBackendFence.backend_registry_id == backend_id)
                .values(
                    generation_high_water=0,
                    control_epoch_high_water=0,
                    runtime_epoch_high_water=0,
                    token_expiry_high_water=None,
                    rollout_control_operation=None,
                    rollout_control_transition_id=None,
                    rollout_control_epoch=None,
                    rollout_control_membership_epoch=None,
                    rollout_control_boot_id=None,
                    rollout_control_token_expires_at=None,
                )
            )
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_memberships DISABLE TRIGGER "
                    "trg_validate_gpu_backend_membership"
                )
            )
            await db.execute(
                delete(GPUBackendMembership).where(
                    GPUBackendMembership.backend_registry_id == backend_id
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
                    GPUBackendFence.backend_registry_id == backend_id
                )
            )
            await db.execute(
                text(
                    "ALTER TABLE gpu_backend_fences ENABLE TRIGGER "
                    "trg_validate_gpu_backend_fence_delete"
                )
            )


async def _install_health(
    factory: async_sessionmaker[AsyncSession],
    backend_id: uuid.UUID,
    *,
    gate: str,
    control_epoch: str,
    resident: bool = False,
) -> None:
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    capability_sha256 = managed_lifecycle_capability_sha256(capability)
    async with factory.begin() as db:
        backend = await db.get(MLBackendRegistry, backend_id)
        membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE))
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert backend is not None
        assert membership is not None
        assert isinstance(db_now, datetime)
        probe_started_at = db_now - timedelta(seconds=2)
        observed_at = db_now - timedelta(seconds=1)
        backend.state = "connected"
        backend.last_checked_at = observed_at
        backend.health_meta = {
            "capabilities": {"managed_lifecycle": capability},
            "gpu_arbiter_probe": {
                "protocol_version": "1",
                "challenge": "a" * 64,
                "backend_registry_id": str(backend_id),
                "gpu_resource_id": _RESOURCE,
                "membership_epoch": str(membership.membership_epoch),
                "membership_state": membership.state,
                "managed_lifecycle_sha256": capability_sha256,
                "probe_started_at": _proof_timestamp(probe_started_at),
                "observed_at": _proof_timestamp(observed_at),
            },
            "residency": {
                "state": "resident" if resident else "unloaded",
                "gpu_loaded": resident,
                "active_requests": 0,
                "builders": 0,
                "borrowers": 0,
                "draining": False,
                "evictable": resident and gate == "enforce",
                "generation": "1" if resident else None,
                "pools": {
                    "models": {
                        "resident": resident,
                        "device": "cuda:0" if resident else None,
                        "provider": None,
                    }
                },
                "boot_id": f"boot-{backend_id}",
                "lifecycle_gate": gate,
                "control_epoch": control_epoch,
                "identity": {
                    "audience": "aap-gpu-lifecycle",
                    "backend_registry_id": str(backend_id),
                    "gpu_resource_id": _RESOURCE,
                },
            },
        }
        flag_modified(backend, "health_meta")


async def _expire_control_intent(
    factory: async_sessionmaker[AsyncSession],
    backend_id: uuid.UUID,
) -> None:
    async with factory.begin() as db:
        fence = await db.get(GPUBackendFence, backend_id)
        db_now = await db.scalar(select(text("clock_timestamp()")))
        assert fence is not None
        assert isinstance(db_now, datetime)
        expired = db_now - timedelta(seconds=10)
        fence.token_expiry_high_water = expired
        fence.rollout_control_token_expires_at = expired


@pytest.mark.asyncio
async def test_promotion_persists_reset_then_enforce_intent_and_waits_for_health(
    rollout_control_db,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, demotions = rollout_control_db
    await _install_health(factory, backend_id, gate="legacy", control_epoch="1")
    rollout = await _native_call(
        gpu_authority_data,
        begin_gpu_arbiter_rollout,
        _RESOURCE,
        GPUArbiterMode.ENFORCE,
    )
    assert rollout.transition_id is not None

    async def demote(resource_id: str) -> None:
        demotions.append(resource_id)

    reset = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=rollout.transition_id,
        target_gate="enforce",
        readiness_demoter=demote,
    )
    replay = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=rollout.transition_id,
        target_gate="enforce",
        readiness_demoter=demote,
    )

    assert (reset.operation, reset.action, reset.control_epoch) == (
        "reset",
        "issue",
        "2",
    )
    assert (
        replay.operation,
        replay.action,
        replay.control_epoch,
        replay.token_expires_at,
    ) == (
        reset.operation,
        reset.action,
        reset.control_epoch,
        reset.token_expires_at,
    )
    assert replay.reason == "control_intent_replay"

    await _expire_control_intent(factory, backend_id)
    await _install_health(factory, backend_id, gate="legacy", control_epoch="2")
    enforce = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=rollout.transition_id,
        target_gate="enforce",
        readiness_demoter=demote,
    )
    assert (enforce.operation, enforce.action, enforce.control_epoch) == (
        "mode_enforce",
        "issue",
        "3",
    )

    await _expire_control_intent(factory, backend_id)
    await _install_health(factory, backend_id, gate="enforce", control_epoch="3")
    acknowledged = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=rollout.transition_id,
        target_gate="enforce",
        readiness_demoter=demote,
    )
    assert (acknowledged.operation, acknowledged.action) == (
        "mode_enforce",
        "acknowledged",
    )
    assert demotions == [_RESOURCE, _RESOURCE]


@pytest.mark.asyncio
async def test_demotion_allows_resident_pool_and_requires_fresh_legacy_health(
    rollout_control_db,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, demotions = rollout_control_db
    promotion = await _native_call(
        gpu_authority_data,
        begin_gpu_arbiter_rollout,
        _RESOURCE,
        GPUArbiterMode.ENFORCE,
    )
    assert promotion.transition_id is not None
    await _native_call(
        gpu_authority_data,
        complete_gpu_arbiter_rollout,
        _RESOURCE,
        promotion.transition_id,
    )
    demotion = await _native_call(
        gpu_authority_data,
        begin_gpu_arbiter_rollout,
        _RESOURCE,
        GPUArbiterMode.OBSERVE,
    )
    assert demotion.transition_id is not None
    await _install_health(
        factory,
        backend_id,
        gate="enforce",
        control_epoch="1",
        resident=True,
    )

    async def demote_ready(resource_id: str) -> None:
        demotions.append(resource_id)

    issued = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=demotion.transition_id,
        target_gate="legacy",
        readiness_demoter=demote_ready,
    )
    assert (issued.operation, issued.action, issued.control_epoch) == (
        "mode_legacy",
        "issue",
        "2",
    )

    await _expire_control_intent(factory, backend_id)
    await _install_health(
        factory,
        backend_id,
        gate="legacy",
        control_epoch="2",
        resident=True,
    )
    acknowledged = await _native_call(
        gpu_authority_data,
        prepare_gpu_backend_rollout_control,
        backend_id,
        gpu_resource_id=_RESOURCE,
        membership_epoch=1,
        transition_id=demotion.transition_id,
        target_gate="legacy",
        readiness_demoter=demote_ready,
    )
    assert (acknowledged.operation, acknowledged.action) == (
        "mode_legacy",
        "acknowledged",
    )
    assert demotions == [_RESOURCE]
