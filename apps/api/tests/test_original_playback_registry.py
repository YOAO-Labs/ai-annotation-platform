"""Pure ownership tests: factories and local stages require no application DB."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.services import original_playback_registry as service
from app.services.storage_access import mark_unsettled


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.tick = 0.0

    def advance(self, seconds, *, wall=True):
        self.tick += seconds
        if wall:
            self.now += timedelta(seconds=seconds)


class Stage:
    def __init__(self, size=8, *, block=False, fail=False):
        self.size = size
        self.closed = False
        self.close_calls = 0
        self.started = threading.Event()
        self.finish = threading.Event()
        self.block = block
        self.fail = fail

    def close(self):
        self.close_calls += 1
        self.started.set()
        if self.block:
            assert self.finish.wait(3), "Test did not release the owned close thread"
        if self.fail:
            raise RuntimeError("Uncertain close")
        self.closed = True


@pytest.fixture
async def world():
    clock = Clock()
    registry = service.PlaybackRegistry(
        clock=lambda: clock.now, timer=lambda: clock.tick
    )
    yield registry, clock
    await registry.shutdown()


async def prepare(registry, clock, *, key="one", context="authority", stage=None, **kw):
    stage = Stage() if stage is None else stage

    async def factory():
        return stage

    return await registry.prepare(
        key=key,
        context=context,
        size_bytes=kw.pop("size_bytes", stage.size),
        factory=kw.pop("factory", factory),
        expires_at=kw.pop("expires_at", clock.now + timedelta(hours=2)),
        media_type=kw.pop("media_type", "video/mp4"),
        **kw,
    )


async def spin_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def assert_error(error, code, status):
    assert error.value.code == code
    assert error.value.status == status


@pytest.mark.asyncio
async def test_exact_key_reuses_one_stage_and_mints_independent_scoped_leases(world):
    registry, clock = world
    stage = Stage()
    first = await prepare(registry, clock, stage=stage)

    async def forbidden():
        raise AssertionError("A ready stage must not recapture the original")

    second = await prepare(registry, clock, factory=forbidden)
    assert first.id != second.id and first.secret != second.secret
    assert first.secret not in repr(first) and "authority" not in repr(first)
    assert registry.context_for_release(first.id) == "authority"
    pin = registry.pin(second.id, second.secret)
    assert pin.stage is stage
    assert "authority" not in repr(pin)
    await registry.revoke_lease(first.id)
    assert not stage.closed
    await registry.revoke_lease(second.id)
    assert not stage.closed
    with pytest.raises(ContentError) as error:
        registry.pin(second.id, second.secret)
    assert_error(error, "video_playback_expired", 410)
    await registry.release_pin(pin)
    await registry.release_pin(pin)
    await registry.revoke_lease(second.id)
    assert stage.closed and stage.close_calls == 1
    assert registry.diagnostics() == {
        "stages": 0,
        "reserved_bytes": 0,
        "leases": 0,
        "response_pins": 0,
        "unsettled_stages": 0,
    }


@pytest.mark.asyncio
async def test_inflight_same_key_is_explicit_429_and_never_joins_or_runs_factory(world):
    registry, clock = world
    arrived, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        arrived.set()
        await finish.wait()
        return Stage()

    pending = asyncio.create_task(prepare(registry, clock, factory=factory))
    await arrived.wait()
    try:
        with pytest.raises(ContentError) as error:
            await prepare(registry, clock, factory=factory)
        assert_error(error, "video_playback_preparing", 429)
        assert calls == 1
    finally:
        finish.set()
        await pending


@pytest.mark.asyncio
async def test_stage_and_declared_byte_budgets_reject_before_factory(world):
    registry, clock = world
    await prepare(registry, clock, key="a")
    await prepare(registry, clock, key="b")

    async def forbidden():
        raise AssertionError("Admission must precede capture")

    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, key="c", factory=forbidden)
    assert_error(error, "video_playback_capacity_exceeded", 429)
    with pytest.raises(ContentError) as error:
        await prepare(
            registry, clock, size_bytes=service.MAX_STAGE_BYTES + 1, factory=forbidden
        )
    assert_error(error, "content_too_large", 413)
    assert registry.diagnostics()["reserved_bytes"] == 16


@pytest.mark.asyncio
async def test_lease_and_global_pin_limits_do_not_consume_extra_capacity(world):
    registry, clock = world
    leases = [await prepare(registry, clock) for _ in range(32)]
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock)
    assert_error(error, "video_playback_lease_capacity_exceeded", 429)
    pins = [registry.pin(leases[0].id, leases[0].secret) for _ in range(8)]
    try:
        with pytest.raises(ContentError) as error:
            registry.pin(leases[0].id, leases[0].secret)
        assert_error(error, "video_playback_response_capacity_exceeded", 429)
        assert registry.diagnostics()["response_pins"] == 8
    finally:
        for pin in pins:
            await registry.release_pin(pin)
    assert registry.diagnostics()["leases"] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, "", "wrong", "秘密", "x" * 300])
async def test_cookie_must_be_exact_ascii_capability_for_this_lease(world, secret):
    registry, clock = world
    lease = await prepare(registry, clock)
    with pytest.raises(ContentError) as error:
        registry.pin(lease.id, secret)
    assert_error(error, "video_playback_capability_invalid", 403)
    assert registry.diagnostics()["response_pins"] == 0


@pytest.mark.asyncio
async def test_cookie_from_another_lease_or_missing_process_is_never_accepted(world):
    registry, clock = world
    first, second = await prepare(registry, clock), await prepare(registry, clock)
    with pytest.raises(ContentError) as error:
        registry.pin(first.id, second.secret)
    assert_error(error, "video_playback_capability_invalid", 403)
    with pytest.raises(ContentError) as error:
        registry.pin(uuid.uuid4(), first.secret)
    assert_error(error, "video_playback_expired", 410)


@pytest.mark.asyncio
async def test_authority_expiry_caps_lease_and_pin_survives_only_to_real_release(world):
    registry, clock = world
    stage = Stage()
    expiration = clock.now + timedelta(seconds=7)
    lease = await prepare(registry, clock, stage=stage, expires_at=expiration)
    pin = registry.pin(lease.id, lease.secret)
    assert lease.expires_at == pin.expires_at == expiration
    clock.advance(7)
    await registry.reap()
    assert registry.diagnostics()["leases"] == 0 and not stage.closed
    with pytest.raises(ContentError) as error:
        registry.context_for_release(lease.id)
    assert_error(error, "video_playback_expired", 410)
    await registry.release_pin(pin)
    assert stage.closed


@pytest.mark.asyncio
async def test_monotonic_expiry_still_retires_when_wall_clock_moves_back(world):
    registry, clock = world
    stage = Stage()
    lease = await prepare(registry, clock, stage=stage)
    clock.advance(301, wall=False)
    await registry.reap()
    assert stage.closed
    with pytest.raises(ContentError):
        registry.pin(lease.id, lease.secret)


@pytest.mark.asyncio
async def test_stage_maximum_age_cannot_be_extended_by_new_leases(world):
    registry, clock = world
    stage = Stage()
    for index in range(13):
        lease = await prepare(registry, clock, stage=stage)
        assert lease.expires_at <= datetime(2026, 10, 24, 1, tzinfo=timezone.utc)
        if index < 12:
            clock.advance(290)
    clock.advance(120)
    await registry.reap()
    assert stage.closed
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_ready_key_requires_same_context_size_and_representation(world):
    registry, clock = world
    await prepare(registry, clock)
    for changes in (
        {"context": "another"},
        {"size_bytes": 7},
        {"media_type": "video/webm"},
    ):
        with pytest.raises(ContentError) as error:
            await prepare(registry, clock, **changes)
        assert_error(error, "video_playback_context_changed", 409)
    assert registry.diagnostics()["leases"] == 1


@pytest.mark.asyncio
async def test_failed_and_invalid_capture_release_only_known_reservations(world):
    registry, clock = world

    async def failed():
        raise ContentError("source_changed")

    with pytest.raises(ContentError):
        await prepare(registry, clock, factory=failed)
    assert registry.diagnostics()["stages"] == 0
    mismatch = Stage(7)
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, stage=mismatch, size_bytes=8)
    assert_error(error, "content_mismatch", 409)
    assert mismatch.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_unsettled_capture_responsibility_is_not_erased_by_reap_or_shutdown(
    world,
):
    registry, clock = world

    async def uncertain():
        raise mark_unsettled(ContentError("storage_operation_unsettled", 503))

    with pytest.raises(ContentError):
        await prepare(registry, clock, factory=uncertain)
    clock.advance(10000)
    await registry.reap()
    await registry.shutdown()
    assert registry.diagnostics()["stages"] == 1
    assert registry.diagnostics()["reserved_bytes"] == 8
    assert registry.diagnostics()["unsettled_stages"] == 1


@pytest.mark.asyncio
async def test_cancelled_capture_drains_late_success_and_repeated_cancellation(world):
    registry, clock = world
    arrived, interrupted, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stage = Stage()

    async def late():
        arrived.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await finish.wait()
        return stage

    pending = asyncio.create_task(prepare(registry, clock, factory=late))
    await arrived.wait()
    pending.cancel()
    await interrupted.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    assert registry.diagnostics()["stages"] == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stage.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_cancelled_actual_close_is_drained_and_its_slot_not_reused_early(world):
    registry, clock = world
    stage = Stage(block=True)
    lease = await prepare(registry, clock, stage=stage)
    pending = asyncio.create_task(registry.revoke_lease(lease.id))
    await spin_until(stage.started.is_set)
    pending.cancel()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and registry.diagnostics()["stages"] == 1
    stage.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stage.closed and stage.close_calls == 1
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_revoke_contexts_preserves_neighbor_and_closes_only_after_last_pin(world):
    registry, clock = world
    owned_stage, neighbor_stage = Stage(), Stage()
    owned = await prepare(registry, clock, context="owned", stage=owned_stage)
    owned_again = await prepare(registry, clock, context="owned")
    neighbor = await prepare(
        registry, clock, key="neighbor", context=object(), stage=neighbor_stage
    )
    pin = registry.pin(owned.id, owned.secret)

    await registry.revoke_contexts(lambda context: context == "owned")
    for lease in (owned, owned_again):
        with pytest.raises(ContentError) as error:
            registry.pin(lease.id, lease.secret)
        assert_error(error, "video_playback_expired", 410)
    registry.validate_lease(neighbor)
    assert not owned_stage.closed and not neighbor_stage.closed
    assert registry.diagnostics()["stages"] == 2
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, key="next")
    assert_error(error, "video_playback_capacity_exceeded", 429)

    await registry.release_pin(pin)
    assert owned_stage.closed and owned_stage.close_calls == 1
    assert not neighbor_stage.closed
    await registry.revoke_contexts(lambda context: context == "owned")
    await prepare(registry, clock, key="next")
    registry.validate_lease(neighbor)
    assert registry.diagnostics()["stages"] == 2


@pytest.mark.asyncio
async def test_revoke_contexts_fences_a_capture_that_can_no_longer_lease(world):
    registry, clock = world
    owned_started, owned_stage = asyncio.Event(), Stage()
    neighbor_started, neighbor_stage = asyncio.Event(), Stage()
    release_owned, release_neighbor = asyncio.Event(), asyncio.Event()

    def factory(started, stage, release):
        async def build():
            started.set()
            await release.wait()
            return stage

        return build

    owned = asyncio.create_task(
        prepare(
            registry,
            clock,
            key="owned",
            context="owned",
            factory=factory(owned_started, owned_stage, release_owned),
        )
    )
    neighbor = asyncio.create_task(
        prepare(
            registry,
            clock,
            key="neighbor",
            context=object(),
            factory=factory(neighbor_started, neighbor_stage, release_neighbor),
        )
    )
    await owned_started.wait()
    await neighbor_started.wait()

    # Revoking a capture belongs to its owner: it is retired, not cancelled, so
    # it can never mint a lease for a fixture that is already being destroyed.
    await registry.revoke_contexts(lambda context: context == "owned")
    assert not owned.done() and not neighbor.done()
    release_owned.set()
    with pytest.raises(ContentError) as error:
        await owned
    assert_error(error, "video_playback_expired", 410)
    assert owned_stage.closed and owned_stage.close_calls == 1
    assert registry.diagnostics()["stages"] == 1

    # The unrelated capture still publishes, and the revoked key is free again.
    release_neighbor.set()
    registry.validate_lease(await neighbor)
    revived = await prepare(registry, clock, key="owned", context="owned")
    assert registry.diagnostics()["stages"] == 2
    await registry.revoke_lease(revived.id)
    assert not neighbor_stage.closed


@pytest.mark.asyncio
async def test_revoke_contexts_drains_all_matching_closes_after_repeated_cancellation(
    world,
):
    registry, clock = world
    stages = [Stage(block=True), Stage(block=True)]
    leases = [
        await prepare(registry, clock, key=index, context="owned", stage=stage)
        for index, stage in enumerate(stages)
    ]
    pending = asyncio.create_task(
        registry.revoke_contexts(lambda context: context == "owned")
    )
    try:
        await spin_until(lambda: all(stage.started.is_set() for stage in stages))
        for lease in leases:
            with pytest.raises(ContentError):
                registry.validate_lease(lease)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert registry.diagnostics()["stages"] == 2
    finally:
        for stage in stages:
            stage.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert all(stage.closed and stage.close_calls == 1 for stage in stages)
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_unknown_close_keeps_quota_and_cannot_reopen_the_retired_stage(world):
    registry, clock = world
    stage = Stage(fail=True)
    lease = await prepare(registry, clock, stage=stage)
    await registry.revoke_lease(lease.id)
    await registry.reap()
    assert stage.close_calls == 1
    assert registry.diagnostics()["stages"] == 1
    assert registry.diagnostics()["unsettled_stages"] == 1
    with pytest.raises(ContentError):
        registry.pin(lease.id, lease.secret)
    second = await prepare(registry, clock, stage=Stage())
    assert second.id != lease.id
    assert registry.diagnostics()["stages"] == 2


@pytest.mark.asyncio
async def test_shutdown_seals_before_waiting_for_pins_and_is_cancel_safe(world):
    registry, clock = world
    stage = Stage()
    lease = await prepare(registry, clock, stage=stage)
    pin = registry.pin(lease.id, lease.secret)
    shutdown = asyncio.create_task(registry.shutdown())
    await asyncio.sleep(0)
    with pytest.raises(ContentError) as error:
        registry.pin(lease.id, lease.secret)
    assert_error(error, "video_playback_expired", 410)
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, key="other")
    assert_error(error, "video_playback_unavailable", 503)
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done() and not stage.closed
    await registry.release_pin(pin)
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    await registry.shutdown()
    assert stage.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_active_capture_before_late_close(world):
    registry, clock = world
    started, interrupted, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stage = Stage()

    async def late():
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await finish.wait()
        return stage

    pending = asyncio.create_task(prepare(registry, clock, factory=late))
    await started.wait()
    shutdown = asyncio.create_task(registry.shutdown())
    await interrupted.wait()
    assert not shutdown.done()
    finish.set()
    with pytest.raises((asyncio.CancelledError, ContentError)):
        await pending
    await shutdown
    assert stage.closed and stage.close_calls == 1


@pytest.mark.asyncio
async def test_periodic_reaper_expires_without_further_client_requests(world):
    registry, clock = world
    registry._reaper_seconds = 0.005
    stage = Stage()
    await prepare(registry, clock, stage=stage)
    registry.start_reaper()
    reaper = registry._reaper
    registry.start_reaper()
    assert registry._reaper is reaper
    clock.advance(301)
    await spin_until(lambda: registry.diagnostics()["stages"] == 0)
    assert stage.closed
    await registry.shutdown()
    assert reaper.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"size_bytes": -1},
        {"size_bytes": True},
        {"key": []},
        {"expires_at": datetime(2026, 10, 24)},
        {"media_type": ""},
    ],
)
async def test_invalid_prepare_never_reserves_capacity(world, changes):
    registry, clock = world
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, **changes)
    assert_error(error, "video_playback_invalid", 422)
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_expired_authority_never_reserves_or_captures(world):
    registry, clock = world
    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, expires_at=clock.now)
    assert_error(error, "video_playback_expired", 410)
    assert registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_real_anonymous_file_remains_open_until_last_actual_response_pin(world):
    import tempfile

    registry, clock = world
    file = tempfile.TemporaryFile(mode="w+b")
    file.write(b"verified")
    file.seek(0)
    stage = service.BinaryStage(file, "a" * 64, 8, {})
    lease = await prepare(registry, clock, stage=stage)
    first = registry.pin(lease.id, lease.secret)
    second = registry.pin(lease.id, lease.secret)
    await registry.revoke_lease(lease.id)
    await registry.release_pin(first)
    assert not file.closed and registry.diagnostics()["stages"] == 1
    await registry.release_pin(second)
    assert file.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_cancelled_late_unknown_close_preserves_uncertainty_marker(world):
    registry, clock = world
    arrived, interrupted, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stage = Stage(fail=True)

    async def late():
        arrived.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await finish.wait()
        return stage

    pending = asyncio.create_task(prepare(registry, clock, factory=late))
    await arrived.wait()
    pending.cancel()
    await interrupted.wait()
    finish.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await pending
    assert error.value.storage_operation_unsettled is True
    assert registry.diagnostics()["stages"] == 1
    assert registry.diagnostics()["unsettled_stages"] == 1


@pytest.mark.asyncio
async def test_authority_expiring_during_capture_never_publishes_late_stage(world):
    registry, clock = world
    stage = Stage()
    expiration = clock.now + timedelta(seconds=5)

    async def late():
        clock.advance(5)
        return stage

    with pytest.raises(ContentError) as error:
        await prepare(registry, clock, stage=stage, factory=late, expires_at=expiration)
    assert_error(error, "video_playback_expired", 410)
    assert stage.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wall", [True, False])
async def test_revalidation_after_auth_await_rejects_expired_lease_and_keeps_pin_fd(
    world, wall
):
    registry, clock = world
    stage = Stage()
    lease = await prepare(registry, clock, stage=stage)
    pin = registry.pin(lease.id, lease.secret)
    registry.validate_lease(lease)
    registry.validate_pin(pin)
    clock.advance(301, wall=wall)
    for validate, value in (
        (registry.validate_lease, lease),
        (registry.validate_pin, pin),
    ):
        with pytest.raises(ContentError) as error:
            validate(value)
        assert_error(error, "video_playback_expired", 410)
    assert not stage.closed and registry.diagnostics()["response_pins"] == 1
    await registry.release_pin(pin)
    assert stage.closed and registry.diagnostics()["stages"] == 0


@pytest.mark.asyncio
async def test_revalidation_rejects_revoked_lease_while_other_lease_and_pin_stay_valid(
    world,
):
    registry, clock = world
    stage = Stage()
    first = await prepare(registry, clock, stage=stage)
    second = await prepare(registry, clock)
    pin = registry.pin(first.id, first.secret)
    await registry.revoke_lease(first.id)
    with pytest.raises(ContentError):
        registry.validate_pin(pin)
    with pytest.raises(ContentError):
        registry.validate_lease(first)
    registry.validate_lease(second)
    assert not stage.closed
    await registry.release_pin(pin)
    assert not stage.closed
    await registry.revoke_lease(second.id)
    assert stage.closed


@pytest.mark.asyncio
async def test_revalidation_after_shutdown_retires_capability_without_closing_active_pin(
    world,
):
    registry, clock = world
    stage = Stage()
    lease = await prepare(registry, clock, stage=stage)
    pin = registry.pin(lease.id, lease.secret)
    shutdown = asyncio.create_task(registry.shutdown())
    await asyncio.sleep(0)
    try:
        with pytest.raises(ContentError):
            registry.validate_lease(lease)
        with pytest.raises(ContentError):
            registry.validate_pin(pin)
        assert not stage.closed
    finally:
        await registry.release_pin(pin)
        await shutdown
    assert stage.closed


@pytest.mark.asyncio
async def test_exact_returned_objects_are_required_and_forged_pin_cannot_release_capacity(
    world,
):
    from dataclasses import replace

    registry, clock = world
    lease = await prepare(registry, clock)
    pin = registry.pin(lease.id, lease.secret)
    with pytest.raises(ContentError):
        registry.validate_lease(replace(lease))
    forged = replace(pin)
    with pytest.raises(ContentError):
        registry.validate_pin(forged)
    with pytest.raises(ValueError):
        await registry.release_pin(forged)
    assert registry.diagnostics()["response_pins"] == 1
    registry.validate_pin(pin)
    await registry.release_pin(pin)
    with pytest.raises(ContentError):
        registry.validate_pin(pin)
    assert registry.diagnostics()["response_pins"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["stage", "context", "media_type", "expires_at", "lease_id", "_entry", "_lease"],
)
async def test_revalidation_fences_mutated_pin_fields_without_losing_real_release(
    world, field
):
    registry, clock = world
    lease = await prepare(registry, clock)
    pin = registry.pin(lease.id, lease.secret)
    original = getattr(pin, field)
    replacement = {
        "stage": Stage(),
        "context": object(),
        "media_type": "video/webm",
        "expires_at": lease.expires_at + timedelta(seconds=1),
        "lease_id": uuid.uuid4(),
        "_entry": object(),
        "_lease": object(),
    }[field]
    setattr(pin, field, replacement)
    try:
        with pytest.raises(ContentError):
            registry.validate_pin(pin)
    finally:
        setattr(pin, field, original)
        await registry.release_pin(pin)
    assert registry.diagnostics()["response_pins"] == 0
