from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from aap_inference.gpu.fences import (
    GPUFenceExhaustedError,
    GPUFenceMembershipError,
    activate_gpu_backend_membership,
    advance_gpu_backend_fence,
    record_gpu_backend_token_expiry,
)
from aap_inference.gpu.native import GPUAuthorityPoolData


@pytest.fixture
async def gpu_authority_data() -> AsyncIterator[GPUAuthorityPoolData]:
    dsn = (
        make_url(os.environ["AAP_MODULE_DATA_INFERENCE_GPU_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=24)
    try:
        yield GPUAuthorityPoolData(pool)
    finally:
        await pool.close()


async def _activate(
    authority_data: GPUAuthorityPoolData,
    backend_id: uuid.UUID,
    *,
    membership_epoch: int,
) -> str:
    return await activate_gpu_backend_membership(
        authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_ID,
        membership_epoch=membership_epoch,
    )


async def _record_token_expiry(
    authority_data: GPUAuthorityPoolData,
    backend_id: uuid.UUID,
    *,
    membership_epoch: int,
    token_expires_at: datetime,
) -> datetime:
    return await record_gpu_backend_token_expiry(
        authority_data,
        backend_id,
        gpu_resource_id=_RESOURCE_ID,
        membership_epoch=membership_epoch,
        token_expires_at=token_expires_at,
    )


_MAX_POSITIVE_INT64 = 9_223_372_036_854_775_807
_RESOURCE_ID = "node-fence/GPU-test"


async def _cleanup_committed_backend(
    factory: async_sessionmaker[AsyncSession], backend_id: uuid.UUID
) -> None:
    """Remove committed test rows without weakening the production trigger."""

    async with factory.begin() as db:
        await db.execute(
            update(GPUBackendFence)
            .where(GPUBackendFence.backend_registry_id == backend_id)
            .values(
                generation_high_water=0,
                control_epoch_high_water=0,
                runtime_epoch_high_water=0,
                token_expiry_high_water=None,
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


@pytest.fixture
async def committed_backend(
    test_engine: AsyncEngine,
    gpu_authority_data: GPUAuthorityPoolData,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, int]]:
    """Create a committed row visible to independent concurrent connections."""

    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gpu-fence-test",
                url=f"http://gpu-fence-{backend_id}.test",
                gpu_resource_id=_RESOURCE_ID,
                vram_budget_mb=1024,
            )
        )
    assert await _activate(gpu_authority_data, backend_id, membership_epoch=1) == "1"
    try:
        yield factory, backend_id, 1
    finally:
        await _cleanup_committed_backend(factory, backend_id)


async def _advance(
    authority_data: GPUAuthorityPoolData,
    backend_id: uuid.UUID,
    membership_epoch: int,
    counter: str,
    *,
    token_expires_at: datetime | None = None,
) -> str:
    return await advance_gpu_backend_fence(
        authority_data,
        backend_id,
        counter,  # type: ignore[arg-type]
        gpu_resource_id=_RESOURCE_ID,
        membership_epoch=membership_epoch,
        token_expires_at=token_expires_at,
    )


@pytest.mark.asyncio
async def test_fence_sequences_start_at_one_and_advance_independently(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend

    async with factory() as db:
        membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_ID))
        fence = await db.get(GPUBackendFence, backend_id)
        assert membership is not None
        assert fence is not None
        assert membership.state == "active"
        assert fence.runtime_epoch_high_water == 1

    assert (
        await _advance(gpu_authority_data, backend_id, membership_epoch, "generation")
        == "1"
    )
    assert (
        await _advance(
            gpu_authority_data, backend_id, membership_epoch, "control_epoch"
        )
        == "1"
    )
    assert (
        await _advance(gpu_authority_data, backend_id, membership_epoch, "generation")
        == "2"
    )

    async with factory() as db:
        row = await db.get(GPUBackendFence, backend_id)
        assert row is not None
        assert row.generation_high_water == 2
        assert row.control_epoch_high_water == 1


@pytest.mark.asyncio
async def test_activation_requires_new_runtime_epoch_after_pending_baseline(
    test_engine: AsyncEngine,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            GPUBackendFence(
                backend_registry_id=backend_id,
                runtime_epoch_high_water=5,
            )
        )
        await db.flush()
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gpu-runtime-baseline-test",
                url=f"http://gpu-runtime-baseline-{backend_id}.test",
                gpu_resource_id=_RESOURCE_ID,
                vram_budget_mb=1024,
            )
        )

    try:
        async with factory() as db:
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_ID))
            assert membership is not None
            assert membership.runtime_epoch_baseline == 5

        with pytest.raises(IntegrityError):
            async with factory.begin() as db:
                membership = await db.get(
                    GPUBackendMembership, (backend_id, _RESOURCE_ID)
                )
                assert membership is not None
                membership.state = "active"

        assert (
            await _activate(gpu_authority_data, backend_id, membership_epoch=1) == "6"
        )
    finally:
        await _cleanup_committed_backend(factory, backend_id)


@pytest.mark.asyncio
async def test_generation_advance_is_atomic_across_independent_sessions(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend

    values = await asyncio.gather(
        *(
            _advance(gpu_authority_data, backend_id, membership_epoch, "generation")
            for _ in range(24)
        )
    )

    assert sorted(map(int, values)) == list(range(1, 25))
    async with factory() as db:
        high_water = await db.scalar(
            select(GPUBackendFence.generation_high_water).where(
                GPUBackendFence.backend_registry_id == backend_id
            )
        )
    assert high_water == 24


@pytest.mark.asyncio
async def test_failed_followup_does_not_reuse_a_durable_generation(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend

    issued_before_hypothetical_redis_failure = await _advance(
        gpu_authority_data, backend_id, membership_epoch, "generation"
    )
    issued_after_retry = await _advance(
        gpu_authority_data, backend_id, membership_epoch, "generation"
    )

    assert issued_before_hypothetical_redis_failure == "1"
    assert issued_after_retry == "2"


@pytest.mark.asyncio
async def test_fence_overflow_fails_closed(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend
    assert (
        await _advance(gpu_authority_data, backend_id, membership_epoch, "generation")
        == "1"
    )
    async with factory.begin() as db:
        await db.execute(
            update(GPUBackendFence)
            .where(GPUBackendFence.backend_registry_id == backend_id)
            .values(generation_high_water=_MAX_POSITIVE_INT64)
        )

    with pytest.raises(GPUFenceExhaustedError):
        await _advance(gpu_authority_data, backend_id, membership_epoch, "generation")

    async with factory() as db:
        high_water = await db.scalar(
            select(GPUBackendFence.generation_high_water).where(
                GPUBackendFence.backend_registry_id == backend_id
            )
        )
    assert high_water == _MAX_POSITIVE_INT64


@pytest.mark.asyncio
async def test_fence_rejects_negative_high_water(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend
    assert (
        await _advance(gpu_authority_data, backend_id, membership_epoch, "generation")
        == "1"
    )

    with pytest.raises(IntegrityError):
        async with factory.begin() as db:
            await db.execute(
                update(GPUBackendFence)
                .where(GPUBackendFence.backend_registry_id == backend_id)
                .values(generation_high_water=-1)
            )


@pytest.mark.asyncio
async def test_managed_registry_delete_is_blocked_and_preserves_fence(
    test_engine: AsyncEngine,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gpu-fence-retirement-test",
                url=f"http://gpu-fence-retirement-{backend_id}.test",
                gpu_resource_id=_RESOURCE_ID,
                vram_budget_mb=1024,
            )
        )
    try:
        assert (
            await _activate(gpu_authority_data, backend_id, membership_epoch=1) == "1"
        )
        assert await _advance(gpu_authority_data, backend_id, 1, "control_epoch") == "1"
        with pytest.raises(IntegrityError):
            async with factory.begin() as db:
                await db.execute(
                    delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
                )
        async with factory() as db:
            assert await db.get(GPUBackendFence, backend_id) is not None
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_ID))
            assert membership is not None
            assert membership.state == "active"
            assert await db.get(MLBackendRegistry, backend_id) is not None
    finally:
        await _cleanup_committed_backend(factory, backend_id)


@pytest.mark.asyncio
async def test_token_expiry_high_water_is_monotonic(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend
    later = datetime.now(UTC) + timedelta(minutes=5)
    earlier = later - timedelta(minutes=1)

    recorded = await asyncio.gather(
        *(
            _record_token_expiry(
                gpu_authority_data,
                backend_id,
                membership_epoch=membership_epoch,
                token_expires_at=value,
            )
            for value in (earlier, later, earlier)
        )
    )

    assert max(recorded) == later
    async with factory() as db:
        fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.token_expiry_high_water == later
        assert fence.generation_high_water == 0
        assert fence.control_epoch_high_water == 0


@pytest.mark.asyncio
async def test_counter_and_token_expiry_can_advance_atomically(
    committed_backend,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory, backend_id, membership_epoch = committed_backend
    expiry = datetime.now(UTC) + timedelta(minutes=5)

    assert (
        await _advance(
            gpu_authority_data,
            backend_id,
            membership_epoch,
            "generation",
            token_expires_at=expiry,
        )
        == "1"
    )

    async with factory() as db:
        fence = await db.get(GPUBackendFence, backend_id)
        assert fence is not None
        assert fence.generation_high_water == 1
        assert fence.token_expiry_high_water == expiry


@pytest.mark.asyncio
async def test_fence_cannot_be_deleted_while_membership_exists(
    committed_backend,
) -> None:
    factory, backend_id, _ = committed_backend

    with pytest.raises(IntegrityError):
        async with factory.begin() as db:
            await db.execute(
                delete(GPUBackendFence).where(
                    GPUBackendFence.backend_registry_id == backend_id
                )
            )


@pytest.mark.asyncio
async def test_orphan_fence_delete_requires_receipt_and_resource_lock(
    test_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    resource_id = "node-fence/GPU-orphan-guard"
    async with factory.begin() as db:
        db.add(GPUBackendFence(backend_registry_id=backend_id))

    with pytest.raises(IntegrityError):
        async with factory.begin() as db:
            await db.execute(
                delete(GPUBackendFence).where(
                    GPUBackendFence.backend_registry_id == backend_id
                )
            )

    receipt = json.dumps(
        {
            "backend_id": str(backend_id),
            "resource_id": resource_id,
            "membership_epoch": "1",
            "retirement_id": str(uuid.uuid4()),
            "fingerprint": "a" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    async with factory.begin() as db:
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('aap:gpu-resource:' || :resource_id, 0))"
            ),
            {"resource_id": resource_id},
        )
        await db.execute(
            text("SELECT set_config('app.gpu_tombstone_gc_receipt', :receipt, true)"),
            {"receipt": receipt},
        )
        result = await db.execute(
            delete(GPUBackendFence).where(
                GPUBackendFence.backend_registry_id == backend_id
            )
        )
        assert result.rowcount == 1

    async with factory() as db:
        assert await db.get(GPUBackendFence, backend_id) is None


@pytest.mark.asyncio
async def test_retirement_waits_for_latest_locked_fence_evidence(
    test_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gpu-fence-lock-order-test",
                url=f"http://gpu-fence-lock-order-{backend_id}.test",
                gpu_resource_id=_RESOURCE_ID,
                vram_budget_mb=1024,
            )
        )

    writer = factory()
    await writer.begin()
    await writer.execute(
        select(GPUBackendMembership)
        .where(
            GPUBackendMembership.backend_registry_id == backend_id,
            GPUBackendMembership.gpu_resource_id == _RESOURCE_ID,
        )
        .with_for_update()
    )
    await writer.execute(
        update(GPUBackendFence)
        .where(GPUBackendFence.backend_registry_id == backend_id)
        .values(
            generation_high_water=3,
            control_epoch_high_water=2,
            token_expiry_high_water=expiry,
        )
    )

    delete_started = asyncio.Event()

    async def delete_registry() -> None:
        async with factory.begin() as db:
            delete_started.set()
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == backend_id)
            )

    delete_task = asyncio.create_task(delete_registry())
    try:
        await delete_started.wait()
        await asyncio.sleep(0.05)
        assert not delete_task.done()
        await writer.commit()
        await asyncio.wait_for(delete_task, timeout=2)

        async with factory() as db:
            membership = await db.get(GPUBackendMembership, (backend_id, _RESOURCE_ID))
            assert membership is not None
            assert membership.state == "retiring"
            assert membership.retired_generation_high_water == 3
            assert membership.retired_control_epoch_high_water == 2
            assert membership.retired_token_expiry_high_water == expiry
    finally:
        if writer.in_transaction():
            await writer.rollback()
        await writer.close()
        if not delete_task.done():
            delete_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await delete_task
        await _cleanup_committed_backend(factory, backend_id)


@pytest.mark.asyncio
async def test_fence_rejects_stale_membership_epoch(
    test_engine: AsyncEngine,
    gpu_authority_data: GPUAuthorityPoolData,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    backend_id = uuid.uuid4()
    async with factory.begin() as db:
        db.add(
            MLBackendRegistry(
                id=backend_id,
                name="gpu-stale-membership-test",
                url=f"http://gpu-stale-membership-{backend_id}.test",
                gpu_resource_id=_RESOURCE_ID,
                vram_budget_mb=1024,
            )
        )

    try:
        async with factory.begin() as db:
            await db.execute(
                update(MLBackendRegistry)
                .where(MLBackendRegistry.id == backend_id)
                .values(vram_budget_mb=2048)
            )
        assert (
            await _activate(gpu_authority_data, backend_id, membership_epoch=2) == "1"
        )

        with pytest.raises(GPUFenceMembershipError):
            await _advance(gpu_authority_data, backend_id, 1, "generation")
    finally:
        await _cleanup_committed_backend(factory, backend_id)
