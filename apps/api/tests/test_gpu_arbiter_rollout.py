from __future__ import annotations

import asyncio
from dataclasses import replace
import uuid

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aap_inference.gpu.rollout_state import (
    GPUArbiterRolloutConflict,
    GPUArbiterRolloutSnapshot,
    classify_gpu_arbiter_rollout,
    gpu_rollout_boundary_active,
)
from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from app.db.models.gpu_arbiter_rollout import GPUArbiterRollout
from app.services.inference_gpu_data import open_gpu_authority_data
from aap_inference_contract.gpu import GPUAdminRolloutSnapshot


def _resource_id() -> str:
    return f"node-rollout/GPU-{uuid.uuid4()}"


async def _begin_rollout(
    factory: async_sessionmaker[AsyncSession],
    resource_id: str,
    target_mode: GPUArbiterMode,
) -> GPUAdminRolloutSnapshot:
    async with open_gpu_authority_data(factory) as data:
        return await data.begin_rollout(resource_id, target_mode.value)


async def _complete_rollout(
    factory: async_sessionmaker[AsyncSession],
    resource_id: str,
    transition_id: uuid.UUID,
) -> GPUAdminRolloutSnapshot:
    async with open_gpu_authority_data(factory) as data:
        return await data.complete_rollout(resource_id, transition_id)


async def _block_rollout(
    factory: async_sessionmaker[AsyncSession],
    resource_id: str,
    transition_id: uuid.UUID,
    reason: str,
) -> GPUAdminRolloutSnapshot:
    async with open_gpu_authority_data(factory) as data:
        return await data.block_rollout(resource_id, transition_id, reason)


async def _read_rollout(
    factory: async_sessionmaker[AsyncSession],
    resource_id: str,
) -> GPUAdminRolloutSnapshot | None:
    async with open_gpu_authority_data(factory) as data:
        return await data.read_rollout(resource_id)


async def _read_rollouts(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[GPUAdminRolloutSnapshot, ...]:
    async with open_gpu_authority_data(factory) as data:
        return await data.read_rollouts()


async def _rollout_boundary(
    factory: async_sessionmaker[AsyncSession],
) -> bool:
    async with open_gpu_authority_data(factory) as data:
        return await data.rollout_boundary()


@pytest.fixture
async def cleanup_gpu_rollouts(test_engine: AsyncEngine):
    resource_ids: list[str] = []
    yield resource_ids.append
    if not resource_ids:
        return
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "ALTER TABLE gpu_arbiter_rollouts DISABLE TRIGGER "
                "trg_validate_gpu_arbiter_rollout"
            )
        )
        await db.execute(
            delete(GPUArbiterRollout).where(
                GPUArbiterRollout.gpu_resource_id.in_(resource_ids)
            )
        )
        await db.execute(
            text(
                "ALTER TABLE gpu_arbiter_rollouts ENABLE TRIGGER "
                "trg_validate_gpu_arbiter_rollout"
            )
        )


def _rollout(
    *,
    state: str,
    effective_mode: GPUArbiterMode,
    target_mode: GPUArbiterMode,
    blocker_reason: str | None = None,
) -> GPUArbiterRolloutSnapshot:
    transition_id = None if state in {"off", "enforcing"} else uuid.uuid4()
    return GPUArbiterRolloutSnapshot(
        resource_id="node-rollout/GPU-test",
        state=state,  # type: ignore[arg-type]
        effective_mode=effective_mode,
        target_mode=target_mode,
        transition_id=transition_id,
        last_transition_id=None,
        blocker_reason=blocker_reason,
        revision=2,
    )


def test_rollout_disabled_preserves_safe_pre_p6_modes() -> None:
    decision = classify_gpu_arbiter_rollout(
        "node-rollout/GPU-test",
        GPUArbiterMode.ENFORCE,
        None,
        rollout_enabled=False,
    )

    assert decision.state == "disabled"
    assert decision.effective_mode is GPUArbiterMode.OFF
    assert decision.dispatch_mode is GPUArbiterMode.OFF
    assert decision.dispatch_blocked is False


def test_rollout_enabled_requires_durable_promotion_before_enforce() -> None:
    missing = classify_gpu_arbiter_rollout(
        "node-rollout/GPU-test",
        GPUArbiterMode.ENFORCE,
        None,
        rollout_enabled=True,
    )
    off = _rollout(
        state="off",
        effective_mode=GPUArbiterMode.OFF,
        target_mode=GPUArbiterMode.OFF,
    )
    unpromoted = classify_gpu_arbiter_rollout(
        off.resource_id,
        GPUArbiterMode.ENFORCE,
        off,
        rollout_enabled=True,
    )

    assert missing.dispatch_blocked
    assert missing.blocked_reason == "gpu_rollout_not_initialized"
    assert unpromoted.dispatch_blocked
    assert unpromoted.blocked_reason == "gpu_rollout_promotion_required"


def test_transition_states_block_dispatch_and_preserve_effective_mode() -> None:
    promoting = _rollout(
        state="promoting",
        effective_mode=GPUArbiterMode.OFF,
        target_mode=GPUArbiterMode.ENFORCE,
    )
    demoting = _rollout(
        state="demoting",
        effective_mode=GPUArbiterMode.ENFORCE,
        target_mode=GPUArbiterMode.OBSERVE,
    )
    blocked = replace(
        demoting,
        state="blocked",
        blocker_reason="backend_mode_ack_unavailable",
    )

    promotion = classify_gpu_arbiter_rollout(
        promoting.resource_id,
        GPUArbiterMode.ENFORCE,
        promoting,
        rollout_enabled=True,
    )
    demotion = classify_gpu_arbiter_rollout(
        demoting.resource_id,
        GPUArbiterMode.OBSERVE,
        demoting,
        rollout_enabled=True,
    )
    failure = classify_gpu_arbiter_rollout(
        blocked.resource_id,
        GPUArbiterMode.OBSERVE,
        blocked,
        rollout_enabled=True,
    )

    assert promotion.dispatch_blocked
    assert promotion.effective_mode is GPUArbiterMode.OFF
    assert demotion.dispatch_blocked
    assert demotion.effective_mode is GPUArbiterMode.ENFORCE
    assert failure.dispatch_blocked
    assert failure.blocked_reason == "backend_mode_ack_unavailable"


def test_enforcing_requires_matching_desired_mode_and_demotion_first() -> None:
    enforcing = _rollout(
        state="enforcing",
        effective_mode=GPUArbiterMode.ENFORCE,
        target_mode=GPUArbiterMode.ENFORCE,
    )

    active = classify_gpu_arbiter_rollout(
        enforcing.resource_id,
        GPUArbiterMode.ENFORCE,
        enforcing,
        rollout_enabled=True,
    )
    rollback = classify_gpu_arbiter_rollout(
        enforcing.resource_id,
        GPUArbiterMode.OFF,
        enforcing,
        rollout_enabled=True,
    )

    assert active.dispatch_mode is GPUArbiterMode.ENFORCE
    assert rollback.dispatch_blocked
    assert rollback.effective_mode is GPUArbiterMode.ENFORCE
    assert rollback.blocked_reason == "gpu_rollout_demotion_required"


@pytest.mark.asyncio
async def test_rollout_transition_is_durable_idempotent_and_reversible(
    test_engine: AsyncEngine,
    cleanup_gpu_rollouts,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = _resource_id()
    cleanup_gpu_rollouts(resource_id)

    promotion = await _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE)
    replay = await _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE)
    assert promotion.state == "promoting"
    assert promotion.effective_mode == GPUArbiterMode.OFF.value
    assert promotion.transition_id is not None
    assert replay == promotion

    enforcing = await _complete_rollout(factory, resource_id, promotion.transition_id)
    settled_replay = await _complete_rollout(
        factory, resource_id, promotion.transition_id
    )
    assert enforcing.state == "enforcing"
    assert enforcing.effective_mode == GPUArbiterMode.ENFORCE.value
    assert enforcing.last_transition_id == promotion.transition_id
    assert settled_replay == enforcing
    listed = await _read_rollouts(factory)
    assert enforcing in listed

    demotion = await _begin_rollout(factory, resource_id, GPUArbiterMode.OBSERVE)
    assert demotion.state == "demoting"
    assert demotion.effective_mode == GPUArbiterMode.ENFORCE.value
    assert demotion.transition_id is not None

    off = await _complete_rollout(factory, resource_id, demotion.transition_id)
    assert off.state == "off"
    assert off.effective_mode == GPUArbiterMode.OFF.value
    assert off.target_mode == GPUArbiterMode.OBSERVE.value
    assert off.last_transition_id == demotion.transition_id


@pytest.mark.asyncio
async def test_rollout_concurrent_begin_returns_one_transition(
    test_engine: AsyncEngine,
    cleanup_gpu_rollouts,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = _resource_id()
    cleanup_gpu_rollouts(resource_id)

    first, second = await asyncio.gather(
        _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE),
        _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE),
    )
    assert first == second
    assert first.transition_id is not None


@pytest.mark.asyncio
async def test_rollout_blocker_is_exact_and_requires_explicit_recovery(
    test_engine: AsyncEngine,
    cleanup_gpu_rollouts,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = _resource_id()
    cleanup_gpu_rollouts(resource_id)
    promotion = await _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE)
    assert promotion.transition_id is not None

    blocked = await _block_rollout(
        factory,
        resource_id,
        promotion.transition_id,
        "backend_mode_ack_unavailable",
    )
    replay = await _block_rollout(
        factory,
        resource_id,
        promotion.transition_id,
        "backend_mode_ack_unavailable",
    )
    assert blocked.state == "blocked"
    assert replay == blocked

    with pytest.raises(GPUArbiterRolloutConflict):
        await _complete_rollout(factory, resource_id, promotion.transition_id)
    with pytest.raises(GPUArbiterRolloutConflict):
        await _block_rollout(
            factory, resource_id, promotion.transition_id, "different_reason"
        )

    recovery = await _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE)
    assert recovery.state == "promoting"
    assert recovery.transition_id not in {None, promotion.transition_id}


@pytest.mark.asyncio
async def test_rollout_database_trigger_rejects_raw_skip_and_delete(
    test_engine: AsyncEngine,
    cleanup_gpu_rollouts,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    resource_id = _resource_id()
    cleanup_gpu_rollouts(resource_id)
    await _begin_rollout(factory, resource_id, GPUArbiterMode.OFF)

    async with factory.begin() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(GPUArbiterRollout)
                .where(GPUArbiterRollout.gpu_resource_id == resource_id)
                .values(
                    state="enforcing",
                    effective_mode="enforce",
                    target_mode="enforce",
                    revision=2,
                )
            )
        await db.rollback()

    async with factory.begin() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                delete(GPUArbiterRollout).where(
                    GPUArbiterRollout.gpu_resource_id == resource_id
                )
            )
        await db.rollback()

    assert (await _read_rollout(factory, resource_id)) is not None


@pytest.mark.asyncio
async def test_rollout_global_boundary_includes_transitions_and_enforcing(
    test_engine: AsyncEngine,
    cleanup_gpu_rollouts,
) -> None:
    factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    disabled = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_arbiter_rollout_enabled=False,
    )

    class UnusedNativeData:
        async def step(self, action, arguments):
            raise AssertionError(f"disabled rollout unexpectedly called {action}")

    assert not await gpu_rollout_boundary_active(
        UnusedNativeData(),
        config=disabled,
    )
    # Other tests may leave durable rows, so assert the local transition makes
    # the boundary active instead of requiring a globally empty table.
    resource_id = _resource_id()
    cleanup_gpu_rollouts(resource_id)
    await _begin_rollout(factory, resource_id, GPUArbiterMode.ENFORCE)
    assert await _rollout_boundary(factory)
