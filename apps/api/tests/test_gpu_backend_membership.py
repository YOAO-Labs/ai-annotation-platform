from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import (
    GPUBackendManagedMutationBlocked,
    MLBackendService,
)


_RESOURCE_A = "node-membership/GPU-a"
_RESOURCE_B = "node-membership/GPU-b"


def _install_gpu_resources(monkeypatch, *resource_ids: str) -> None:
    """Declare the physical resources these tests claim so validation stays real."""
    resources = {}
    for resource_id in resource_ids:
        node_id, device = resource_id.split("/", 1)
        resources[resource_id] = {
            "node_id": node_id,
            "physical_device_token": device,
            "allocatable_mb": 8192,
            "mode": "off",
        }
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(resources),
        raising=True,
    )


async def _create_gpu_backend(
    db: AsyncSession,
    *,
    resource_id: str = _RESOURCE_A,
    extra_params: dict | None = None,
) -> MLBackendRegistry:
    backend = MLBackendRegistry(
        id=uuid.uuid4(),
        name="gpu-membership-test",
        url=f"http://gpu-membership-{uuid.uuid4()}.test",
        gpu_resource_id=resource_id,
        vram_budget_mb=2048,
        eviction_priority=2,
        extra_params=extra_params or {},
    )
    db.add(backend)
    await db.flush()
    return backend


async def test_gpu_claim_creates_pending_membership_before_commit(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(
        db_session,
        extra_params={"max_concurrency": "7"},
    )

    membership = await db_session.get(GPUBackendMembership, (backend.id, _RESOURCE_A))
    fence = await db_session.get(GPUBackendFence, backend.id)

    assert membership is not None
    assert membership.state == "pending"
    assert membership.membership_epoch == 1
    assert membership.vram_budget_mb == 2048
    assert membership.eviction_priority == 2
    assert membership.max_concurrency == 7
    assert fence is not None
    assert fence.generation_high_water == 0
    assert fence.runtime_epoch_high_water == 0


async def test_resource_move_retires_old_membership_and_creates_pending_target(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    checked_at = datetime.now(UTC)
    backend.state = "connected"
    backend.last_checked_at = checked_at
    backend.health_meta = {
        "residency": {
            "state": "unloaded",
            "gpu_loaded": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "pools": {},
        }
    }
    await db_session.flush()

    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(gpu_resource_id=_RESOURCE_B, vram_budget_mb=3072)
    )
    await db_session.flush()

    old_membership = await db_session.get(
        GPUBackendMembership, (backend.id, _RESOURCE_A)
    )
    new_membership = await db_session.get(
        GPUBackendMembership, (backend.id, _RESOURCE_B)
    )
    assert old_membership is not None
    assert old_membership.state == "retiring"
    assert old_membership.membership_epoch == 2
    assert old_membership.retire_reason == "resource_moved"
    assert old_membership.retired_health_state == "connected"
    assert old_membership.retired_health_checked_at == checked_at
    assert old_membership.retired_generation_high_water == 0
    assert new_membership is not None
    assert new_membership.state == "pending"
    assert new_membership.membership_epoch == 1
    assert new_membership.vram_budget_mb == 3072


async def test_same_resource_config_update_advances_membership_epoch(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(
            vram_budget_mb=4096,
            eviction_priority=5,
            extra_params={"max_concurrency": 9},
        )
    )
    await db_session.flush()

    membership = await db_session.get(GPUBackendMembership, (backend.id, _RESOURCE_A))
    assert membership is not None
    await db_session.refresh(membership)
    assert membership.membership_epoch == 2
    assert membership.vram_budget_mb == 4096
    assert membership.eviction_priority == 5
    assert membership.max_concurrency == 9


async def test_noop_protected_update_keeps_active_membership_epoch(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    fence = await db_session.get(GPUBackendFence, backend.id)
    membership = await db_session.get(GPUBackendMembership, (backend.id, _RESOURCE_A))
    assert fence is not None
    assert membership is not None
    fence.runtime_epoch_high_water = 1
    await db_session.flush()
    membership.state = "active"
    await db_session.flush()

    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(url=backend.url)
    )
    await db_session.refresh(membership)

    assert membership.state == "active"
    assert membership.membership_epoch == 1


async def test_retired_resource_cannot_reenter_before_tombstone_gc(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(gpu_resource_id=_RESOURCE_B)
    )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend.id)
                .values(gpu_resource_id=_RESOURCE_A)
            )

    old_membership = await db_session.get(
        GPUBackendMembership, (backend.id, _RESOURCE_A)
    )
    new_membership = await db_session.get(
        GPUBackendMembership, (backend.id, _RESOURCE_B)
    )
    assert old_membership is not None
    assert new_membership is not None
    await db_session.refresh(old_membership)
    await db_session.refresh(new_membership)
    assert old_membership.state == "retiring"
    assert new_membership.state == "pending"


async def test_resource_change_immediately_invalidates_cached_health(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    _install_gpu_resources(monkeypatch, _RESOURCE_A, _RESOURCE_B)
    backend = await _create_gpu_backend(db_session)
    backend.state = "connected"
    backend.health_meta = {"residency": {"gpu_loaded": True}}
    backend.last_checked_at = datetime.now(UTC)
    await db_session.flush()

    updated = await MLBackendService(db_session).update(
        backend.id,
        gpu_resource_id=_RESOURCE_B,
    )

    assert updated is not None
    assert updated.state == "disconnected"
    assert updated.health_meta is None
    assert updated.last_checked_at is None


async def test_registry_delete_keeps_retiring_membership_and_fence(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    backend_id = backend.id

    await db_session.execute(
        delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
    )
    await db_session.flush()

    membership = await db_session.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
    assert membership is not None
    assert membership.state == "retiring"
    assert membership.retire_reason == "registry_deleted"
    assert await db_session.get(GPUBackendFence, backend_id) is not None

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend_id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(retired_generation_high_water=9)
            )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(GPUBackendMembership).where(
                    GPUBackendMembership.backend_registry_id == backend_id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
            )


async def test_backend_cannot_have_two_current_memberships(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(GPUBackendMembership).values(
                    backend_registry_id=backend.id,
                    gpu_resource_id=_RESOURCE_B,
                    membership_epoch=1,
                    state="pending",
                    vram_budget_mb=1024,
                    eviction_priority=0,
                    max_concurrency=4,
                )
            )


async def test_retiring_membership_cannot_be_inserted_directly(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(GPUBackendMembership).values(
                    backend_registry_id=backend.id,
                    gpu_resource_id=_RESOURCE_B,
                    membership_epoch=1,
                    runtime_epoch_baseline=0,
                    state="retiring",
                    vram_budget_mb=1024,
                    eviction_priority=0,
                    max_concurrency=4,
                    retired_at=datetime.now(UTC),
                    retire_reason="managed_retirement",
                    retired_generation_high_water=0,
                    retired_control_epoch_high_water=0,
                    retired_runtime_epoch_high_water=0,
                )
            )


async def test_active_membership_requires_runtime_activation(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    membership = await db_session.get(GPUBackendMembership, (backend.id, _RESOURCE_A))
    assert membership is not None

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            membership.state = "active"
            await db_session.flush()


async def test_pending_runtime_baseline_cannot_be_rebased_directly(
    db_session: AsyncSession,
) -> None:
    backend_id = uuid.uuid4()
    db_session.add(
        GPUBackendFence(
            backend_registry_id=backend_id,
            runtime_epoch_high_water=5,
        )
    )
    await db_session.flush()
    backend = MLBackendRegistry(
        id=backend_id,
        name="gpu-membership-baseline-test",
        url=f"http://gpu-membership-baseline-{backend_id}.test",
        gpu_resource_id=_RESOURCE_A,
        vram_budget_mb=2048,
        eviction_priority=2,
        extra_params={},
    )
    db_session.add(backend)
    await db_session.flush()

    membership = await db_session.get(GPUBackendMembership, (backend_id, _RESOURCE_A))
    assert membership is not None
    assert membership.runtime_epoch_baseline == 5

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend_id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(runtime_epoch_baseline=4)
            )


async def test_current_membership_cannot_forge_retirement(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    forged_evidence = {
        "state": "retiring",
        "membership_epoch": 2,
        "retired_at": datetime.now(UTC),
        "retire_reason": "managed_retirement",
        "retired_generation_high_water": 0,
        "retired_control_epoch_high_water": 0,
        "retired_runtime_epoch_high_water": 0,
    }

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend.id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(**forged_evidence)
            )

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend.id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(gpu_resource_id=_RESOURCE_B, **forged_evidence)
            )


async def test_active_membership_epoch_cannot_be_rolled_back(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    await db_session.execute(
        update(MLBackendRegistry)
        .where(MLBackendRegistry.id == backend.id)
        .values(vram_budget_mb=3072)
    )
    membership = await db_session.get(GPUBackendMembership, (backend.id, _RESOURCE_A))
    fence = await db_session.get(GPUBackendFence, backend.id)
    assert membership is not None
    assert fence is not None
    await db_session.refresh(membership)
    assert membership.membership_epoch == 2

    fence.runtime_epoch_high_water = 1
    await db_session.flush()
    membership.state = "active"
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend.id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(membership_epoch=1)
            )


async def test_current_membership_cannot_be_deleted_directly(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(GPUBackendMembership).where(
                    GPUBackendMembership.backend_registry_id == backend.id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
            )


async def test_managed_runtime_blocks_service_and_raw_mutation(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    _install_gpu_resources(monkeypatch, _RESOURCE_A)
    backend = await _create_gpu_backend(db_session)
    fence = await db_session.get(GPUBackendFence, backend.id)
    assert fence is not None
    fence.runtime_epoch_high_water = 1
    await db_session.flush()

    with pytest.raises(GPUBackendManagedMutationBlocked):
        async with db_session.begin_nested():
            await MLBackendService(db_session).update(
                backend.id,
                vram_budget_mb=4096,
            )

    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend.id)
                .values(url=f"http://changed-{uuid.uuid4()}.test")
            )
            await db_session.flush()
    assert "requires retirement" in str(exc_info.value)


async def test_managed_runtime_blocks_registry_delete(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)
    fence = await db_session.get(GPUBackendFence, backend.id)
    assert fence is not None
    fence.runtime_epoch_high_water = 1
    await db_session.flush()

    with pytest.raises(GPUBackendManagedMutationBlocked):
        async with db_session.begin_nested():
            await MLBackendService(db_session).delete_with_topology(backend.id)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == backend.id)
            )


async def test_invalid_membership_evidence_is_rejected(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(GPUBackendMembership)
                .where(
                    GPUBackendMembership.backend_registry_id == backend.id,
                    GPUBackendMembership.gpu_resource_id == _RESOURCE_A,
                )
                .values(retired_generation_high_water=0)
            )


async def test_invalid_gpu_max_concurrency_is_not_silently_defaulted(
    db_session: AsyncSession,
) -> None:
    backend = await _create_gpu_backend(db_session)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend.id)
                .values(extra_params={"max_concurrency": "invalid"})
            )
