"""Dynamic RapidOCR engine-pool ownership and cancellation invariants."""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from catalog import ResolvedEngine
from engine_pool import (
    EngineBuildArtifact,
    EngineBuildTimeout,
    EngineDeviceError,
    EnginePool,
    EnginePoolBusyError,
)
from predictor import strict_cuda_engine_error


@dataclass
class _FakeEngine:
    name: str
    chains: dict[str, list[str] | None]


def _resolved(name: str) -> ResolvedEngine:
    return ResolvedEngine(
        det_path=f"/{name}-det.onnx",
        cls_path=f"/{name}-cls.onnx",
        rec_path=f"/{name}-rec.onnx",
        det_meta=("PP-OCRv5", "mobile", "ch"),
        rec_meta=("PP-OCRv5", "mobile", "ch"),
        use_det=True,
        use_cls=True,
        use_rec=True,
        lang="universal",
    )


def _engine(
    name: str,
    *,
    det: list[str] | None = None,
    cls: list[str] | None = None,
    rec: list[str] | None = None,
) -> _FakeEngine:
    cpu = ["CPUExecutionProvider"]
    return _FakeEngine(
        name,
        {
            "det": cpu if det is None else det,
            "cls": cpu if cls is None else cls,
            "rec": cpu if rec is None else rec,
        },
    )


def _inspect(engine: _FakeEngine) -> dict[str, list[str] | None]:
    return engine.chains


async def _wait_for_thread_event(event: threading.Event) -> None:
    ready = await asyncio.wait_for(asyncio.to_thread(event.wait), timeout=1.0)
    assert ready


@pytest.mark.parametrize(
    "failure", ["cpu", "wrong-device", "missing", "unobservable", "disable-failed"]
)
def test_strict_cuda_rejects_bad_ocr_sessions_and_recovers(failure) -> None:
    async def scenario() -> None:
        bad = failure
        released = []
        constructed = []

        class Session:
            def __init__(self, problem=None):
                self.problem = problem
                self.fallback_enabled = True

            def get_providers(self):
                if self.problem == "unobservable":
                    raise RuntimeError("provider inspection failed")
                return (
                    ["CPUExecutionProvider"]
                    if self.problem == "cpu"
                    else ["CUDAExecutionProvider", "CPUExecutionProvider"]
                )

            def get_provider_options(self):
                return {
                    "CUDAExecutionProvider": {
                        "device_id": "1" if self.problem == "wrong-device" else "0"
                    }
                }

            def disable_fallback(self):
                if self.problem == "disable-failed":
                    raise RuntimeError("fallback could not be disabled")
                self.fallback_enabled = False

        class Engine:
            def __init__(self):
                self.text_det = SimpleNamespace(
                    session=SimpleNamespace(session=Session())
                )
                self.text_cls = SimpleNamespace(
                    session=SimpleNamespace(session=Session())
                )
                self.text_rec = SimpleNamespace(
                    session=SimpleNamespace(
                        session=None if bad == "missing" else Session(bad)
                    )
                )

        def build(_resolved):
            instance = Engine()
            constructed.append(weakref.ref(instance))
            return instance

        from predictor import inspect_engine_providers

        pool = EnginePool(
            1,
            build,
            inspect_engine_providers,
            validate_engine=strict_cuda_engine_error,
            strict_cleanup=lambda: released.append(True),
        )
        with pytest.raises(EngineDeviceError):
            await pool.warmup(_resolved("strict"))
        failed = await pool.snapshot()
        assert failed["current_size"] == 0
        assert failed["cleanup_failed"] is True
        assert constructed[0]() is None
        assert released
        await pool.unload_all(reason="manual", force_cleanup=True)
        bad = None
        await pool.warmup(_resolved("strict"))
        async with pool.borrow(_resolved("strict")) as lease:
            assert all(
                getattr(lease.engine, component).session.session.fallback_enabled
                is False
                for component in ("text_det", "text_cls", "text_rec")
            )
        await pool.shutdown()

    asyncio.run(scenario())


async def _eventually(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_pool_rejects_non_positive_capacity_and_timeout() -> None:
    with pytest.raises(ValueError, match="cap"):
        EnginePool(0, lambda _r: _engine("x"), _inspect)
    with pytest.raises(ValueError, match="build_timeout"):
        EnginePool(1, lambda _r: _engine("x"), _inspect, build_timeout=0)


@pytest.mark.asyncio
async def test_same_key_cold_start_is_single_flight() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def build(resolved: ResolvedEngine) -> _FakeEngine:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=1.0)
        return _engine(resolved.det_path)

    target = _resolved("a")
    pool = EnginePool(2, build, _inspect, build_timeout=1.0)
    first = asyncio.create_task(pool.warmup(target))
    await _wait_for_thread_event(started)
    second = asyncio.create_task(pool.warmup(target))
    await asyncio.sleep(0)

    building = await pool.snapshot()
    assert building["builders"] == 1
    assert building["waiters"] == 2
    assert building["current_size"] + building["reserved_build_slots"] <= 2

    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result[0] is False
    assert second_result[0] is False
    assert calls == 1
    assert (await pool.snapshot())["current_size"] == 1


@pytest.mark.asyncio
async def test_distinct_builders_reserve_capacity_and_reject_fourth() -> None:
    releases = {name: threading.Event() for name in ("a", "b", "c")}
    started = {name: threading.Event() for name in releases}

    def build(resolved: ResolvedEngine) -> _FakeEngine:
        name = resolved.det_path.removeprefix("/").removesuffix("-det.onnx")
        started[name].set()
        assert releases[name].wait(timeout=1.0)
        return _engine(name)

    pool = EnginePool(3, build, _inspect, build_timeout=1.0)
    tasks = [
        asyncio.create_task(pool.warmup(_resolved(name))) for name in ("a", "b", "c")
    ]
    for event in started.values():
        await _wait_for_thread_event(event)

    reserved = await pool.snapshot()
    assert reserved["current_size"] == 0
    assert reserved["reserved_build_slots"] == 3
    assert reserved["current_size"] + reserved["reserved_build_slots"] == pool.cap
    with pytest.raises(EnginePoolBusyError):
        await pool.warmup(_resolved("d"))

    for event in releases.values():
        event.set()
    await asyncio.gather(*tasks)
    assert (await pool.snapshot())["current_size"] == 3


@pytest.mark.asyncio
async def test_lru_releases_victim_before_replacement_build() -> None:
    events: list[str] = []

    def build(resolved: ResolvedEngine) -> _FakeEngine:
        name = resolved.det_path.removeprefix("/").removesuffix("-det.onnx")
        events.append(f"build:{name}")
        return _engine(name)

    pool = EnginePool(
        1,
        build,
        _inspect,
        strict_cleanup=lambda: events.append("cleanup"),
    )
    await pool.warmup(_resolved("a"))
    _, _, evicted = await pool.warmup(_resolved("b"))

    assert events == ["build:a", "cleanup", "build:b"]
    assert evicted == _resolved("a").pool_key
    assert [item["key"] for item in (await pool.snapshot())["loaded_keys"]] == [
        _resolved("b").pool_key
    ]


@pytest.mark.asyncio
async def test_borrowed_entry_blocks_lru_and_full_unload() -> None:
    pool = EnginePool(1, lambda _r: _engine("a"), _inspect)
    target = _resolved("a")
    await pool.warmup(target)

    async with pool.borrow(target):
        assert (await pool.snapshot())["borrowers"] == 1
        with pytest.raises(EnginePoolBusyError):
            await pool.warmup(_resolved("b"))
        with pytest.raises(EnginePoolBusyError):
            await pool.unload_all(reason="manual", force_cleanup=True)

    assert await pool.unload_all(reason="manual", force_cleanup=True) == 1
    unloaded = await pool.snapshot()
    assert unloaded["current_size"] == 0
    assert unloaded["gpu_resident"] is False


@pytest.mark.asyncio
async def test_per_entry_use_lock_counts_waiting_borrowers_and_serializes_use() -> None:
    pool = EnginePool(1, lambda _r: _engine("a"), _inspect)
    target = _resolved("a")
    await pool.warmup(target)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    users = 0
    max_users = 0

    async def use(*, hold: bool) -> None:
        nonlocal users, max_users
        async with pool.borrow(target):
            users += 1
            max_users = max(max_users, users)
            if hold:
                first_entered.set()
                await release_first.wait()
            users -= 1

    first = asyncio.create_task(use(hold=True))
    await first_entered.wait()
    second = asyncio.create_task(use(hold=False))
    await asyncio.sleep(0)
    assert (await pool.snapshot())["borrowers"] == 2

    release_first.set()
    await asyncio.gather(first, second)
    assert max_users == 1
    assert (await pool.snapshot())["borrowers"] == 0


@pytest.mark.asyncio
async def test_cancelled_borrow_release_finishes_before_returning() -> None:
    pool = EnginePool(1, lambda _r: _engine("a"), _inspect)
    target = _resolved("a")
    await pool.warmup(target)
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def use() -> None:
        async with pool.borrow(target):
            entered.set()
            await leave.wait()

    borrower = asyncio.create_task(use())
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
async def test_cancelled_waiter_keeps_real_builder_reserved() -> None:
    started = threading.Event()
    release = threading.Event()

    def build(_resolved: ResolvedEngine) -> _FakeEngine:
        started.set()
        assert release.wait(timeout=1.0)
        return _engine("a")

    target = _resolved("a")
    pool = EnginePool(1, build, _inspect, build_timeout=1.0)
    waiter = asyncio.create_task(pool.warmup(target))
    await _wait_for_thread_event(started)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    builder = await pool.builder_for(target)
    assert builder is not None
    assert pool.builder_for_now(target) is builder
    snapshot = await pool.snapshot()
    assert snapshot["builders"] == 1
    assert snapshot["waiters"] == 0
    assert snapshot["gpu_resident"] is None

    release.set()
    await builder
    assert (await pool.snapshot())["current_size"] == 1


@pytest.mark.asyncio
async def test_timed_out_builder_stays_reserved_until_thread_finishes() -> None:
    started = threading.Event()
    release = threading.Event()

    def build(_resolved: ResolvedEngine) -> _FakeEngine:
        started.set()
        assert release.wait(timeout=1.0)
        return _engine("a")

    target = _resolved("a")
    pool = EnginePool(1, build, _inspect, build_timeout=0.02)
    with pytest.raises(EngineBuildTimeout):
        await pool.warmup(target)
    await _wait_for_thread_event(started)
    assert (await pool.snapshot())["reserved_build_slots"] == 1

    release.set()

    async def finished() -> bool:
        return (await pool.snapshot())["builders"] == 0

    await _eventually(finished)
    assert (await pool.snapshot())["current_size"] == 1


@pytest.mark.asyncio
async def test_repeatedly_cancelled_builder_waits_for_thread_and_cleans_late_engine() -> (
    None
):
    started = threading.Event()
    release = threading.Event()

    def build(_resolved: ResolvedEngine) -> _FakeEngine:
        started.set()
        assert release.wait(timeout=1.0)
        return _engine("late")

    target = _resolved("a")
    pool = EnginePool(1, build, _inspect, build_timeout=1.0)
    waiter = asyncio.create_task(pool.warmup(target))
    await _wait_for_thread_event(started)
    builder = await pool.builder_for(target)
    assert builder is not None

    builder.cancel()
    await asyncio.sleep(0.01)
    builder.cancel()
    assert not builder.done()
    assert (await pool.snapshot())["builders"] == 1

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
async def test_cancelled_builder_drops_engine_references_before_cleanup() -> None:
    started = threading.Event()
    release = threading.Event()
    cleaned_without_engine_reference = threading.Event()
    engine_ref: weakref.ReferenceType[_FakeEngine] | None = None

    def build(_resolved: ResolvedEngine) -> _FakeEngine:
        nonlocal engine_ref
        engine = _engine("late")
        engine_ref = weakref.ref(engine)
        started.set()
        assert release.wait(timeout=1.0)
        return engine

    def cleanup() -> None:
        assert engine_ref is not None
        assert engine_ref() is None
        cleaned_without_engine_reference.set()

    target = _resolved("a")
    pool = EnginePool(
        1,
        build,
        _inspect,
        strict_cleanup=cleanup,
        build_timeout=1.0,
    )
    waiter = asyncio.create_task(pool.warmup(target))
    await _wait_for_thread_event(started)
    builder = await pool.builder_for(target)
    assert builder is not None

    builder.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert cleaned_without_engine_reference.is_set()
    assert builder.cancelled()
    assert (await pool.snapshot())["builders"] == 0


@pytest.mark.asyncio
async def test_provider_snapshot_reads_all_three_chains_and_true_dominates_unknown() -> (
    None
):
    engines = {
        "cpu": _engine("cpu"),
        "mixed": _engine(
            "mixed",
            det=["CPUExecutionProvider", "CUDAExecutionProvider"],
            cls=["CPUExecutionProvider"],
            rec=None,
        ),
    }
    # Explicitly mark the rec chain unknown; ``_engine`` uses CPU for omitted args.
    engines["mixed"].chains["rec"] = None
    pool = EnginePool(
        2,
        lambda resolved: engines[
            resolved.det_path.removeprefix("/").removesuffix("-det.onnx")
        ],
        _inspect,
    )
    empty = await pool.snapshot()
    assert empty["gpu_resident"] is False
    assert empty["session_count"] == 0

    await pool.warmup(_resolved("cpu"))
    cpu = await pool.snapshot()
    assert cpu["gpu_resident"] is False
    assert cpu["provider"] == "CPUExecutionProvider"
    assert cpu["session_count"] == 3

    await pool.warmup(_resolved("mixed"))
    mixed = await pool.snapshot()
    assert mixed["engines"][_resolved("mixed").pool_key]["resident"] is True
    assert mixed["gpu_resident"] is True
    assert mixed["provider"] is None
    assert mixed["session_count"] == 5


@pytest.mark.asyncio
async def test_unknown_provider_name_never_reports_cpu_or_effective_provider() -> None:
    unknown = _engine(
        "unknown",
        det=["FutureExecutionProvider"],
        cls=["FutureExecutionProvider"],
        rec=["FutureExecutionProvider"],
    )
    pool = EnginePool(1, lambda _r: unknown, _inspect)
    await pool.warmup(_resolved("unknown"))

    snapshot = await pool.snapshot()
    assert snapshot["gpu_resident"] is None
    assert snapshot["device"] is None
    assert snapshot["provider"] is None


@pytest.mark.asyncio
async def test_cleanup_failure_blocks_cold_build_until_forced_cleanup_recovers() -> (
    None
):
    fail_cleanup = True

    def cleanup() -> None:
        if fail_cleanup:
            raise RuntimeError("cleanup failed")

    pool = EnginePool(
        1,
        lambda resolved: _engine(resolved.det_path),
        _inspect,
        strict_cleanup=cleanup,
    )
    await pool.warmup(_resolved("a"))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await pool.unload_all(reason="manual", force_cleanup=True)

    failed = await pool.snapshot()
    assert failed["current_size"] == 0
    assert failed["cleanup_failed"] is True
    assert failed["gpu_resident"] is None
    with pytest.raises(EnginePoolBusyError):
        await pool.warmup(_resolved("b"))

    fail_cleanup = False
    assert await pool.unload_all(reason="manual", force_cleanup=True) == 0
    assert (await pool.snapshot())["gpu_resident"] is False


@pytest.mark.asyncio
async def test_cpu_replacement_after_partial_cuda_build_stays_unknown_until_cleanup() -> (
    None
):
    pool = EnginePool(
        2,
        lambda _resolved: EngineBuildArtifact(
            engine=_engine("cpu-replacement"),
            cleanup_uncertain=True,
        ),
        _inspect,
    )
    target = _resolved("a")

    await pool.warmup(target)
    uncertain = await pool.snapshot()
    assert uncertain["current_size"] == 1
    assert uncertain["cleanup_failed"] is True
    assert uncertain["gpu_resident"] is None
    # The published replacement can finish the triggering request, but no new cold
    # allocation is admitted until a trustworthy full cleanup removes both graphs.
    async with pool.borrow(target):
        pass
    with pytest.raises(EnginePoolBusyError):
        await pool.warmup(_resolved("b"))

    assert await pool.unload_all(reason="manual", force_cleanup=True) == 1
    assert (await pool.snapshot())["gpu_resident"] is False


@pytest.mark.asyncio
async def test_cancelled_unload_waits_for_real_cleanup() -> None:
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    def cleanup() -> None:
        cleanup_started.set()
        assert allow_cleanup.wait(timeout=1.0)

    pool = EnginePool(
        1,
        lambda _r: _engine("a"),
        _inspect,
        strict_cleanup=cleanup,
    )
    await pool.warmup(_resolved("a"))
    unload = asyncio.create_task(pool.unload_all(reason="manual"))
    await _wait_for_thread_event(cleanup_started)
    unload.cancel()
    await asyncio.sleep(0)
    assert (await pool.snapshot())["cleanup_in_progress"] is True

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await unload
    cleaned = await pool.snapshot()
    assert cleaned["cleanup_in_progress"] is False
    assert cleaned["gpu_resident"] is False


@pytest.mark.asyncio
async def test_repeatedly_cancelled_shutdown_waits_for_borrower_and_cleanup() -> None:
    pool = EnginePool(1, lambda _r: _engine("a"), _inspect)
    target = _resolved("a")
    await pool.warmup(target)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def use() -> None:
        async with pool.borrow(target):
            entered.set()
            await release.wait()

    borrower = asyncio.create_task(use())
    await entered.wait()
    shutdown = asyncio.create_task(pool.shutdown())
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0.02)
    shutdown.cancel()
    assert not shutdown.done()
    assert (await pool.snapshot())["borrowers"] == 1

    release.set()
    await borrower
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(shutdown), timeout=1.0)
    snapshot = await pool.snapshot()
    assert snapshot["current_size"] == 0
    assert snapshot["gpu_resident"] is False
