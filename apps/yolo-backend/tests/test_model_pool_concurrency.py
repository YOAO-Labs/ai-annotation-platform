"""YOLO ModelPool lifecycle/concurrency invariants without GPU dependencies."""

from __future__ import annotations

import asyncio
import threading
import time
import weakref

import pytest


class _FakeModel:
    def __init__(self, key: str) -> None:
        self.key = key
        self.names = {0: "object"}
        self.device = "cuda:0"


async def _wait_for_thread_event(event: threading.Event) -> None:
    ready = await asyncio.wait_for(asyncio.to_thread(event.wait), timeout=1.0)
    assert ready


async def _eventually(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_build_reservation_evicts_before_next_build() -> None:
    """A full pool must free its LRU slot before constructing another model."""
    from model_pool import ModelPool

    events: list[str] = []

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        events.append(f"build:{size}")
        return _FakeModel(size)

    pool = ModelPool(
        cap=1,
        build_model=build,
        free_gpu_memory=lambda: events.append("free"),
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    await pool.warmup("detection", "yolo11", "m")

    assert events == ["build:s", "free", "build:m"]


@pytest.mark.asyncio
async def test_same_key_concurrency_has_one_builder_and_one_reservation() -> None:
    from model_pool import ModelPool

    started = threading.Event()
    release = threading.Event()
    build_calls = 0

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        nonlocal build_calls
        build_calls += 1
        started.set()
        assert release.wait(timeout=1.0)
        return _FakeModel(size)

    pool = ModelPool(2, build, lambda: None, build_timeout=1.0)

    async def use_model() -> None:
        async with pool.borrow("detection", "yolo11", "s") as lease:
            assert lease.model.key == "s"

    first = asyncio.create_task(use_model())
    await _wait_for_thread_event(started)
    second = asyncio.create_task(use_model())
    await asyncio.sleep(0)

    building = await pool.snapshot()
    assert building["builders"] == 1
    assert building["reserved_build_slots"] == 1
    assert building["current_size"] + building["reserved_build_slots"] <= pool.cap

    release.set()
    await asyncio.gather(first, second)
    assert build_calls == 1
    ready = await pool.snapshot()
    assert ready["builders"] == 0
    assert ready["current_size"] == 1


@pytest.mark.asyncio
async def test_different_keys_reserve_capacity_before_serialized_builds() -> None:
    from model_pool import ModelPool, PoolBusyError

    started_s = threading.Event()
    release_s = threading.Event()
    started_m = threading.Event()
    release_m = threading.Event()

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        if size == "s":
            started_s.set()
            assert release_s.wait(timeout=1.0)
        elif size == "m":
            started_m.set()
            assert release_m.wait(timeout=1.0)
        return _FakeModel(size)

    pool = ModelPool(2, build, lambda: None, build_timeout=1.0)
    first = asyncio.create_task(pool.warmup("detection", "yolo11", "s"))
    await _wait_for_thread_event(started_s)
    second = asyncio.create_task(pool.warmup("detection", "yolo11", "m"))
    await asyncio.sleep(0)

    reserved = await pool.snapshot()
    assert reserved["current_size"] == 0
    assert reserved["reserved_build_slots"] == 2
    assert reserved["current_size"] + reserved["reserved_build_slots"] <= pool.cap
    with pytest.raises(PoolBusyError):
        await pool.warmup("detection", "yolo11", "l")

    release_s.set()
    await _wait_for_thread_event(started_m)
    midway = await pool.snapshot()
    assert midway["current_size"] + midway["reserved_build_slots"] <= pool.cap
    release_m.set()
    await asyncio.gather(first, second)
    ready = await pool.snapshot()
    assert ready["current_size"] == 2
    assert ready["reserved_build_slots"] == 0


@pytest.mark.asyncio
async def test_borrowed_lru_is_not_evicted_or_unloaded() -> None:
    from model_pool import ModelPool, PoolBusyError

    build_calls: list[str] = []

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        build_calls.append(size)
        return _FakeModel(size)

    pool = ModelPool(
        1,
        build,
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")

    async with pool.borrow("detection", "yolo11", "s"):
        snapshot = await pool.snapshot()
        assert snapshot["borrowers"] == 1
        with pytest.raises(PoolBusyError):
            await pool.warmup("detection", "yolo11", "m")
        with pytest.raises(PoolBusyError):
            await pool.unload_all(reason="manual")

        still_borrowed = await pool.snapshot()
        assert [item["key"] for item in still_borrowed["loaded_keys"]] == [
            "yolo11/s/detection"
        ]
        assert build_calls == ["s"]


@pytest.mark.asyncio
async def test_cancelled_borrow_release_finishes_before_returning() -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def use_model() -> None:
        async with pool.borrow("detection", "yolo11", "s"):
            entered.set()
            await leave.wait()

    borrower = asyncio.create_task(use_model())
    await entered.wait()
    await pool._lock.acquire()  # noqa: SLF001 - hold borrower metadata release
    leave.set()
    await asyncio.sleep(0)
    borrower.cancel()
    pool._lock.release()  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await borrower
    assert (await pool.snapshot())["borrowers"] == 0


@pytest.mark.asyncio
async def test_per_entry_use_lock_serializes_mutable_model_use() -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    concurrent_users = 0
    max_concurrent_users = 0

    async def use_model(*, hold: bool) -> None:
        nonlocal concurrent_users, max_concurrent_users
        async with pool.borrow("detection", "yolo11", "s"):
            concurrent_users += 1
            max_concurrent_users = max(max_concurrent_users, concurrent_users)
            if hold:
                first_entered.set()
                await release_first.wait()
            concurrent_users -= 1

    first = asyncio.create_task(use_model(hold=True))
    await first_entered.wait()
    second = asyncio.create_task(use_model(hold=False))
    await asyncio.sleep(0)

    waiting = await pool.snapshot()
    assert waiting["borrowers"] == 2
    release_first.set()
    await asyncio.gather(first, second)
    assert max_concurrent_users == 1


@pytest.mark.asyncio
async def test_timed_out_builder_stays_reserved_until_real_future_finishes() -> None:
    from model_pool import ModelBuildTimeout, ModelPool

    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        started.set()
        assert release.wait(timeout=1.0)
        return _FakeModel(size)

    pool = ModelPool(1, build, lambda: None, build_timeout=0.02)
    with pytest.raises(ModelBuildTimeout):
        await pool.warmup("detection", "yolo11", "s")
    await _wait_for_thread_event(started)

    timed_out = await pool.snapshot()
    assert timed_out["builders"] == 1
    assert timed_out["reserved_build_slots"] == 1
    assert timed_out["current_size"] == 0

    release.set()

    async def build_finished() -> bool:
        return (await pool.snapshot())["builders"] == 0

    await _eventually(build_finished)
    completed = await pool.snapshot()
    assert completed["current_size"] == 1
    assert completed["reserved_build_slots"] == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_builder() -> None:
    from model_pool import ModelPool

    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        started.set()
        assert release.wait(timeout=1.0)
        return _FakeModel(size)

    pool = ModelPool(1, build, lambda: None, build_timeout=1.0)
    waiter = asyncio.create_task(pool.warmup("detection", "yolo11", "s"))
    await _wait_for_thread_event(started)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pool.builder_for_now("detection", "yolo11", "s") is await pool.builder_for(
        "detection", "yolo11", "s"
    )

    after_cancel = await pool.snapshot()
    assert after_cancel["builders"] == 1
    assert after_cancel["reserved_build_slots"] == 1

    release.set()

    async def build_finished() -> bool:
        return (await pool.snapshot())["builders"] == 0

    await _eventually(build_finished)
    assert (await pool.snapshot())["current_size"] == 1


@pytest.mark.asyncio
async def test_repeatedly_cancelled_builder_waits_for_thread_and_cleans_model() -> None:
    from model_pool import ModelPool

    started = threading.Event()
    release = threading.Event()

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        started.set()
        assert release.wait(timeout=1.0)
        return _FakeModel(size)

    pool = ModelPool(1, build, lambda: None, build_timeout=1.0)
    waiter = asyncio.create_task(pool.warmup("detection", "yolo11", "s"))
    await _wait_for_thread_event(started)
    builder = await pool.builder_for("detection", "yolo11", "s")
    assert builder is not None

    builder.cancel()
    await asyncio.sleep(0.01)
    builder.cancel()
    assert not builder.done()
    assert (await pool.snapshot())["reserved_build_slots"] == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert builder.cancelled()
    failed = await pool.snapshot()
    assert failed["builders"] == 0
    assert failed["current_size"] == 0
    assert failed["cleanup_failed"] is True
    assert failed["gpu_resident"] is None

    await pool.unload_all(reason="manual", force_cleanup=True)
    assert (await pool.snapshot())["gpu_resident"] is False


@pytest.mark.asyncio
async def test_cancelled_builder_drops_model_references_before_cleanup() -> None:
    from model_pool import ModelPool

    started = threading.Event()
    release = threading.Event()
    cleaned_without_model_reference = threading.Event()
    model_ref: weakref.ReferenceType[_FakeModel] | None = None

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        nonlocal model_ref
        model = _FakeModel(size)
        model_ref = weakref.ref(model)
        started.set()
        assert release.wait(timeout=1.0)
        return model

    def cleanup() -> None:
        assert model_ref is not None
        assert model_ref() is None
        cleaned_without_model_reference.set()

    pool = ModelPool(1, build, cleanup, build_timeout=1.0)
    waiter = asyncio.create_task(pool.warmup("detection", "yolo11", "s"))
    await _wait_for_thread_event(started)
    builder = await pool.builder_for("detection", "yolo11", "s")
    assert builder is not None

    builder.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert cleaned_without_model_reference.is_set()
    assert builder.cancelled()
    assert (await pool.snapshot())["builders"] == 0


@pytest.mark.asyncio
async def test_cancelled_successful_warmup_still_drops_waiter(monkeypatch) -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    original_drop = pool._drop_waiter  # noqa: SLF001
    drop_started = asyncio.Event()
    allow_drop = asyncio.Event()

    async def delayed_drop(key) -> None:
        drop_started.set()
        await allow_drop.wait()
        await original_drop(key)

    monkeypatch.setattr(pool, "_drop_waiter", delayed_drop)
    warmup = asyncio.create_task(pool.warmup("detection", "yolo11", "s"))
    await drop_started.wait()
    warmup.cancel()
    await asyncio.sleep(0)
    assert (await pool.snapshot())["waiters"] == 1

    allow_drop.set()
    with pytest.raises(asyncio.CancelledError):
        await warmup
    assert (await pool.snapshot())["waiters"] == 0
    assert await pool.unload_all(reason="manual") == 1


@pytest.mark.asyncio
async def test_idle_cutoff_uses_borrow_release_time() -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")

    async with pool.borrow("detection", "yolo11", "s"):
        cutoff_before_release = time.monotonic()

    assert await pool.unload_idle(idle_before=cutoff_before_release) == 0
    assert await pool.unload_idle(idle_before=time.monotonic() + 1.0) == 1


@pytest.mark.asyncio
async def test_unknown_model_device_never_reports_gpu_false() -> None:
    from model_pool import ModelPool

    model = _FakeModel("s")
    model.device = None
    pool = ModelPool(1, lambda *_args: model, lambda: None, build_timeout=1.0)

    await pool.warmup("detection", "yolo11", "s")

    assert (await pool.snapshot())["gpu_resident"] is None


@pytest.mark.asyncio
async def test_failed_build_stays_unknown_until_trusted_full_cleanup() -> None:
    from model_pool import ModelPool, PoolBusyError

    should_fail = True

    def build(_task: str, _series: str, size: str) -> _FakeModel:
        if should_fail:
            raise RuntimeError("partial CUDA build failed")
        return _FakeModel(size)

    pool = ModelPool(1, build, lambda: None, build_timeout=1.0)
    with pytest.raises(RuntimeError, match="partial CUDA"):
        await pool.warmup("detection", "yolo11", "s")
    assert (await pool.snapshot())["gpu_resident"] is None

    should_fail = False
    with pytest.raises(PoolBusyError, match="residency is unknown"):
        await pool.warmup("detection", "yolo11", "s")

    assert await pool.unload_all(reason="manual", force_cleanup=True) == 0
    assert (await pool.snapshot())["gpu_resident"] is False

    await pool.warmup("detection", "yolo11", "s")
    assert (await pool.snapshot())["gpu_resident"] is True


@pytest.mark.asyncio
async def test_cancelled_unload_waits_for_real_cleanup_before_clearing_state() -> None:
    from model_pool import ModelPool, PoolBusyError

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    def cleanup() -> None:
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=1.0)

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        cleanup,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    unload = asyncio.create_task(pool.unload_all(reason="manual"))
    await _wait_for_thread_event(cleanup_started)
    unload.cancel()
    await asyncio.sleep(0)

    cleaning = await pool.snapshot()
    assert cleaning["cleanup_in_progress"] is True
    assert cleaning["gpu_resident"] is None
    with pytest.raises(PoolBusyError):
        await pool.unload_all(reason="manual", force_cleanup=True)

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await unload
    cleaned = await pool.snapshot()
    assert cleaned["cleanup_in_progress"] is False
    assert cleaned["cleanup_failed"] is False
    assert cleaned["gpu_resident"] is False


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_interrupt_unload_state_commit() -> None:
    from model_pool import ModelPool

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    def cleanup() -> None:
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=1.0)
        cleanup_finished.set()

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        cleanup,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    unload = asyncio.create_task(pool.unload_all(reason="manual"))
    await _wait_for_thread_event(cleanup_started)
    await pool._lock.acquire()  # noqa: SLF001 - hold the metadata commit point
    allow_cleanup.set()
    await _wait_for_thread_event(cleanup_finished)
    await asyncio.sleep(0)

    unload.cancel()
    await asyncio.sleep(0)
    unload.cancel()
    assert not unload.done()
    pool._lock.release()  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await unload
    cleaned = await pool.snapshot()
    assert cleaned["cleanup_in_progress"] is False
    assert cleaned["cleanup_failed"] is False
    assert cleaned["gpu_resident"] is False


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_borrower_before_cleanup() -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use_model() -> None:
        async with pool.borrow("detection", "yolo11", "s"):
            entered.set()
            await release.wait()

    borrower = asyncio.create_task(use_model())
    await entered.wait()
    shutdown = asyncio.create_task(pool.shutdown())
    await asyncio.sleep(0.02)
    assert not shutdown.done()
    assert (await pool.snapshot())["borrowers"] == 1

    release.set()
    await borrower
    await asyncio.wait_for(shutdown, timeout=1.0)
    snapshot = await pool.snapshot()
    assert snapshot["current_size"] == 0
    assert snapshot["gpu_resident"] is False


@pytest.mark.asyncio
async def test_cancelled_shutdown_cleans_up_before_reraising() -> None:
    from model_pool import ModelPool

    pool = ModelPool(
        1,
        lambda _task, _series, size: _FakeModel(size),
        lambda: None,
        build_timeout=1.0,
    )
    await pool.warmup("detection", "yolo11", "s")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use_model() -> None:
        async with pool.borrow("detection", "yolo11", "s"):
            entered.set()
            await release.wait()

    borrower = asyncio.create_task(use_model())
    await entered.wait()
    shutdown = asyncio.create_task(pool.shutdown())
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0.02)
    shutdown.cancel()
    assert not shutdown.done()

    release.set()
    await borrower
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(shutdown), timeout=1.0)
    snapshot = await pool.snapshot()
    assert snapshot["current_size"] == 0
    assert snapshot["gpu_resident"] is False
