"""Cancellation-safe dynamic pool for composite RapidOCR engines."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from catalog import ResolvedEngine

logger = logging.getLogger("rapidocr-backend.engine-pool")
UTC = timezone.utc

GPU_PROVIDERS = frozenset({"CUDAExecutionProvider", "TensorrtExecutionProvider"})
CPU_PROVIDERS = frozenset({"CPUExecutionProvider"})
COMPONENTS = ("det", "cls", "rec")


class EnginePoolBusyError(RuntimeError):
    """No pool slot or cleanup boundary is currently safe to use."""


class EngineDeviceError(RuntimeError):
    """A constructed engine does not satisfy the configured device policy."""


class EngineBuildTimeout(RuntimeError):
    """The caller timed out while the real builder remains pool-owned."""

    def __init__(self, message: str, *, builder: asyncio.Task[_BuildResult]) -> None:
        super().__init__(message)
        self.builder = builder


@dataclass(slots=True)
class _EngineEntry:
    key: str
    resolved: ResolvedEngine
    engine: Any
    loaded_at: datetime
    last_used_at: datetime
    last_used_monotonic: float
    hit_count: int = 0
    borrowers: int = 0
    use_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class EngineLease:
    key: str
    engine: Any
    cache_hit: bool
    engine_load_ms: int | None


@dataclass(frozen=True, slots=True)
class EngineBuildArtifact:
    """One built engine plus conservative cleanup truth from its factory."""

    engine: Any
    cleanup_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class _BuildResult:
    load_ms: int
    evicted_key: str | None


ProviderInspector = Callable[[Any], dict[str, list[str] | None] | None]


class EnginePool:
    """LRU pool whose entries each own det, cls, and rec ORT sessions.

    All index changes happen under one event-loop lock. Engine construction,
    destruction, and inference happen outside it. The invariant
    ``resident entries + reserved builders <= cap`` therefore also bounds the
    number of composite three-session engines that can be owned by the pool.
    """

    def __init__(
        self,
        cap: int,
        build_engine: Callable[[ResolvedEngine], Any],
        inspect_providers: ProviderInspector,
        *,
        build_timeout: float = 30.0,
        validate_engine: Callable[[Any], str | None] | None = None,
        strict_cleanup: Callable[[], None] | None = None,
    ) -> None:
        if cap <= 0:
            raise ValueError("cap must be positive")
        if build_timeout <= 0:
            raise ValueError("build_timeout must be positive")

        self._cap = cap
        self._build_engine = build_engine
        self._inspect_providers = inspect_providers
        self._validate_engine = validate_engine
        self._build_timeout = build_timeout
        self._strict_cleanup = strict_cleanup or (lambda: None)

        self._entries: OrderedDict[str, _EngineEntry] = OrderedDict()
        self._builders: dict[str, asyncio.Task[_BuildResult]] = {}
        self._waiters: dict[str, int] = {}
        self._last_evict: dict[str, Any] | None = None
        self._cleanup_in_progress = False
        self._cleanup_failed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._builder_retirements: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @property
    def cap(self) -> int:
        return self._cap

    @asynccontextmanager
    async def borrow(self, resolved: ResolvedEngine) -> AsyncIterator[EngineLease]:
        """Pin one engine while waiting for and holding its mutable-state lock."""

        entry, cache_hit, load_ms = await self._acquire_borrower(resolved)
        use_lock_acquired = False
        try:
            await entry.use_lock.acquire()
            use_lock_acquired = True
            yield EngineLease(
                key=entry.key,
                engine=entry.engine,
                cache_hit=cache_hit,
                engine_load_ms=load_ms,
            )
        finally:
            if use_lock_acquired:
                entry.use_lock.release()
            await self._release_borrower_cancellation_safe(entry)

    async def warmup(
        self,
        resolved: ResolvedEngine,
    ) -> tuple[bool, int | None, str | None]:
        """Ensure one composite engine is resident without borrowing it."""

        key = resolved.pool_key
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._touch_locked(entry, count_hit=False)
                return True, None, None
            builder = self._get_or_start_builder_locked(resolved)
            self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            result = await self._wait_for_builder(key, builder)
        finally:
            await self._drop_waiter_cancellation_safe(key)
        return False, result.load_ms, result.evicted_key

    async def _acquire_borrower(
        self,
        resolved: ResolvedEngine,
    ) -> tuple[_EngineEntry, bool, int | None]:
        key = resolved.pool_key
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.borrowers += 1
                self._touch_locked(entry, count_hit=True)
                return entry, True, None
            builder = self._get_or_start_builder_locked(resolved)
            self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            result = await self._wait_for_builder(key, builder)
            async with self._lock:
                entry = self._entries.get(key)
                if entry is None:
                    raise RuntimeError(f"completed builder did not publish {key!r}")
                entry.borrowers += 1
                self._touch_locked(entry, count_hit=False)
                self._drop_waiter_locked(key)
                return entry, False, result.load_ms
        except BaseException:
            await self._drop_waiter_cancellation_safe(key)
            raise

    async def _release_borrower(self, entry: _EngineEntry) -> None:
        async with self._lock:
            current = self._entries.get(entry.key)
            if current is not entry or entry.borrowers <= 0:
                raise RuntimeError(f"invalid borrower release for {entry.key!r}")
            entry.borrowers -= 1
            self._touch_locked(entry, count_hit=False)

    async def _release_borrower_cancellation_safe(
        self,
        entry: _EngineEntry,
    ) -> None:
        cleanup = asyncio.create_task(self._release_borrower(entry))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    def _get_or_start_builder_locked(
        self,
        resolved: ResolvedEngine,
    ) -> asyncio.Task[_BuildResult]:
        self._prune_done_builders_locked()
        key = resolved.pool_key
        builder = self._builders.get(key)
        if builder is not None:
            return builder
        if self._cleanup_in_progress:
            raise EnginePoolBusyError("engine pool cleanup is in progress")
        if self._cleanup_failed:
            raise EnginePoolBusyError(
                "engine residency is unknown; full cleanup is required"
            )

        evicted: tuple[str, _EngineEntry] | None = None
        if len(self._entries) + len(self._builders) >= self._cap:
            for candidate_key, candidate in self._entries.items():
                if (
                    candidate.borrowers == 0
                    and self._waiters.get(candidate_key, 0) == 0
                ):
                    evicted = (candidate_key, candidate)
                    break
            if evicted is None:
                raise EnginePoolBusyError("all engine pool slots are active")
            self._entries.pop(evicted[0])
            self._record_evict_locked(evicted[0], "lru")

        task = asyncio.create_task(self._build_and_publish(resolved, evicted))
        self._builders[key] = task
        task.add_done_callback(
            lambda completed, builder_key=key: self._builder_completed(
                builder_key,
                completed,
            )
        )
        self._assert_capacity_locked()
        return task

    async def _build_and_publish(
        self,
        resolved: ResolvedEngine,
        evicted: tuple[str, _EngineEntry] | None,
    ) -> _BuildResult:
        key = resolved.pool_key
        current_task = asyncio.current_task()
        engine: Any = None
        built: Any = None
        entry: _EngineEntry | None = None
        cleanup_uncertain = False
        evicted_key: str | None = None
        try:
            if evicted is not None:
                evicted_key = evicted[0]
                engines = [evicted[1].engine]
                evicted = None
                _, cleanup_cancelled = await self._run_executor_to_completion(
                    self._release_engines_sync,
                    engines,
                )
                if cleanup_cancelled:
                    raise asyncio.CancelledError

            started = time.monotonic()
            built, build_cancelled = await self._run_executor_to_completion(
                self._build_engine,
                resolved,
            )
            if isinstance(built, EngineBuildArtifact):
                engine = built.engine
                cleanup_uncertain = built.cleanup_uncertain
            else:
                engine = built
            if build_cancelled:
                raise asyncio.CancelledError
            if self._validate_engine is not None:
                if cleanup_uncertain:
                    raise EngineDeviceError("engine construction has uncertain cleanup")
                error = self._validate_engine(engine)
                if error is not None:
                    raise EngineDeviceError(error)
            load_ms = int((time.monotonic() - started) * 1000)
            loaded_at = datetime.now(UTC)
            entry = _EngineEntry(
                key=key,
                resolved=resolved,
                engine=engine,
                loaded_at=loaded_at,
                last_used_at=loaded_at,
                last_used_monotonic=time.monotonic(),
            )

            async with self._lock:
                if self._builders.get(key) is not current_task:
                    raise RuntimeError(f"builder ownership changed for {key!r}")
                self._entries[key] = entry
                if cleanup_uncertain:
                    self._cleanup_failed = True
                self._assert_capacity_locked()
            return _BuildResult(load_ms=load_ms, evicted_key=evicted_key)
        except BaseException as build_error:
            engines = [engine] if engine is not None else []
            engine = None
            built = None
            entry = None
            cleanup_cancelled = False
            try:
                _, cleanup_cancelled = await self._run_executor_to_completion(
                    self._release_engines_sync,
                    engines,
                )
            except asyncio.CancelledError:
                cleanup_cancelled = True
            except BaseException:
                logger.exception("cleanup after failed RapidOCR build also failed")
            _, commit_cancelled = await self._run_task_to_completion(
                self._commit_failed_build(key, current_task)
            )
            if (
                isinstance(build_error, asyncio.CancelledError)
                or cleanup_cancelled
                or commit_cancelled
            ):
                raise asyncio.CancelledError from build_error
            raise

    async def _commit_failed_build(
        self,
        key: str,
        current_task: asyncio.Task[Any] | None,
    ) -> None:
        async with self._lock:
            if self._builders.get(key) is not current_task:
                raise RuntimeError(f"builder ownership changed for {key!r}")
            self._cleanup_failed = True
            self._assert_capacity_locked()

    def _builder_completed(
        self,
        key: str,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        """Retire a builder only after its coroutine and cleanup frame are done."""

        self._consume_task_result(task)
        retirement = asyncio.create_task(self._retire_builder(key, task))
        self._builder_retirements.add(retirement)
        retirement.add_done_callback(self._builder_retirement_completed)

    def _builder_retirement_completed(self, task: asyncio.Task[None]) -> None:
        self._builder_retirements.discard(task)
        self._consume_task_result(task)

    async def _retire_builder(
        self,
        key: str,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        async with self._lock:
            if self._builders.get(key) is task:
                self._builders.pop(key)
            self._assert_capacity_locked()

    def _prune_done_builders_locked(self) -> None:
        for key, task in list(self._builders.items()):
            if task.done() and self._builders.get(key) is task:
                self._builders.pop(key)

    @staticmethod
    async def _run_executor_to_completion(
        call: Callable[..., Any],
        *args: Any,
    ) -> tuple[Any, bool]:
        """Wait for a real executor owner even when this task is repeatedly cancelled."""

        future = asyncio.get_running_loop().run_in_executor(None, call, *args)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            return future.result(), cancelled
        except BaseException as exc:
            if cancelled:
                raise asyncio.CancelledError from exc
            raise

    @staticmethod
    async def _run_task_to_completion(
        coroutine: Any,
    ) -> tuple[Any, bool]:
        """Run an async state commit to completion despite repeated cancellation."""

        task = asyncio.create_task(coroutine)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        return task.result(), cancelled

    async def _wait_for_builder(
        self,
        key: str,
        builder: asyncio.Task[_BuildResult],
    ) -> _BuildResult:
        try:
            return await asyncio.wait_for(
                asyncio.shield(builder),
                timeout=self._build_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise EngineBuildTimeout(
                f"engine build timeout ({self._build_timeout}s) for {key!r}; "
                "builder remains tracked",
                builder=builder,
            ) from exc

    async def builder_for(
        self,
        resolved: ResolvedEngine,
    ) -> asyncio.Task[_BuildResult] | None:
        async with self._lock:
            self._prune_done_builders_locked()
            return self._builders.get(resolved.pool_key)

    def builder_for_now(
        self,
        resolved: ResolvedEngine,
    ) -> asyncio.Task[_BuildResult] | None:
        """Read a builder synchronously on the pool's owning event loop."""

        builder = self._builders.get(resolved.pool_key)
        return builder if builder is not None and not builder.done() else None

    async def _drop_waiter(self, key: str) -> None:
        async with self._lock:
            self._drop_waiter_locked(key)

    async def _drop_waiter_cancellation_safe(self, key: str) -> None:
        cleanup = asyncio.create_task(self._drop_waiter(key))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    def _drop_waiter_locked(self, key: str) -> None:
        waiters = self._waiters.get(key, 0)
        if waiters <= 1:
            self._waiters.pop(key, None)
        else:
            self._waiters[key] = waiters - 1

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _touch_locked(self, entry: _EngineEntry, *, count_hit: bool) -> None:
        self._entries.move_to_end(entry.key)
        entry.last_used_at = datetime.now(UTC)
        entry.last_used_monotonic = time.monotonic()
        if count_hit:
            entry.hit_count += 1

    def _record_evict_locked(self, key: str, reason: str) -> None:
        self._last_evict = {
            "key": key,
            "at": datetime.now(UTC),
            "reason": reason,
        }
        logger.info("RapidOCR engine evicted %s (reason=%s)", key, reason)

    def _assert_capacity_locked(self) -> None:
        reserved_builders = sum(key not in self._entries for key in self._builders)
        if len(self._entries) + reserved_builders > self._cap:
            raise RuntimeError("engine pool capacity invariant violated")

    def _release_engines_sync(self, engines: list[Any]) -> None:
        engines.clear()
        gc.collect()
        self._strict_cleanup()

    async def unload_all(
        self,
        *,
        reason: str = "idle",
        idle_before: float | None = None,
        force_cleanup: bool = False,
    ) -> int:
        """Release every engine without crossing a real pool owner."""

        async with self._lock:
            self._prune_done_builders_locked()
            if self._cleanup_in_progress:
                if reason == "idle":
                    return 0
                raise EnginePoolBusyError("engine pool cleanup is already in progress")
            if idle_before is not None and any(
                entry.last_used_monotonic > idle_before
                for entry in self._entries.values()
            ):
                return 0
            if (
                self._builders
                or self._waiters
                or any(entry.borrowers > 0 for entry in self._entries.values())
            ):
                if reason == "idle":
                    return 0
                raise EnginePoolBusyError(
                    "engine pool has active builders, waiters, or borrowers"
                )

            count = len(self._entries)
            if count == 0 and not force_cleanup:
                return 0
            engines = [entry.engine for entry in self._entries.values()]
            last_key = next(reversed(self._entries), None)
            self._entries.clear()
            self._cleanup_in_progress = True

        cleanup = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(
                None,
                self._release_engines_sync,
                engines,
            )
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                pass

        cleanup_error: BaseException | None = None
        try:
            cleanup.result()
        except BaseException as exc:
            cleanup_error = exc

        _, commit_cancelled = await self._run_task_to_completion(
            self._commit_cleanup_result(
                cleanup_error=cleanup_error,
                last_key=last_key,
                reason=reason,
            )
        )
        cancelled = cancelled or commit_cancelled

        if cleanup_error is not None:
            if cancelled:
                raise asyncio.CancelledError from cleanup_error
            raise cleanup_error
        logger.info("RapidOCR pool unloaded %d engines (reason=%s)", count, reason)
        if cancelled:
            raise asyncio.CancelledError
        return count

    async def _commit_cleanup_result(
        self,
        *,
        cleanup_error: BaseException | None,
        last_key: str | None,
        reason: str,
    ) -> None:
        async with self._lock:
            self._cleanup_in_progress = False
            if cleanup_error is not None:
                self._cleanup_failed = True
            else:
                self._cleanup_failed = False
                if last_key is not None:
                    self._record_evict_locked(
                        last_key,
                        "idle_timeout" if reason == "idle" else "manual",
                    )

    async def unload_idle(self, *, idle_before: float) -> int:
        return await self.unload_all(reason="idle", idle_before=idle_before)

    async def shutdown(self) -> None:
        """Finish final cleanup before propagating caller cancellation."""

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_impl())
            self._shutdown_task.add_done_callback(self._consume_task_result)
        task = self._shutdown_task
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                pass

        shutdown_error: BaseException | None = None
        try:
            task.result()
        except BaseException as exc:
            shutdown_error = exc
        if shutdown_error is not None:
            if cancelled:
                raise asyncio.CancelledError from shutdown_error
            raise shutdown_error
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown_impl(self) -> None:
        while True:
            async with self._lock:
                self._prune_done_builders_locked()
                builders = list(self._builders.values())
                active = bool(
                    self._waiters
                    or any(entry.borrowers > 0 for entry in self._entries.values())
                    or self._cleanup_in_progress
                )
            if not builders and not active:
                break
            if builders:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in builders),
                    return_exceptions=True,
                )
            else:
                await asyncio.sleep(0.01)
        await self.unload_all(reason="shutdown", force_cleanup=True)

    async def snapshot(self) -> dict[str, Any]:
        """Return pool metadata and complete ORT provider-chain residency."""

        async with self._lock:
            self._prune_done_builders_locked()
            loaded_keys = [
                {
                    "key": entry.key,
                    "loaded_at": entry.loaded_at.isoformat(),
                    "last_used_at": entry.last_used_at.isoformat(),
                    "hit_count": entry.hit_count,
                    "borrowers": entry.borrowers,
                }
                for entry in self._entries.values()
            ]
            last_evict = None
            if self._last_evict is not None:
                last_evict = {
                    "key": self._last_evict["key"],
                    "at": self._last_evict["at"].isoformat(),
                    "reason": self._last_evict["reason"],
                }

            provider_groups: list[list[str]] = []
            provider_unknown = False
            per_engine: dict[str, dict[str, Any]] = {}
            for key, entry in self._entries.items():
                inspected = self._inspect_providers(entry.engine)
                component_chains: dict[str, list[str] | None] = {}
                engine_unknown = inspected is None
                if inspected is None:
                    inspected = {}
                for component in COMPONENTS:
                    providers = inspected.get(component)
                    chain = list(providers) if providers else None
                    component_chains[component] = chain
                    if chain is None:
                        engine_unknown = True
                    else:
                        provider_groups.append(chain)

                known_groups = [
                    chain for chain in component_chains.values() if chain is not None
                ]
                has_gpu = any(
                    provider in GPU_PROVIDERS
                    for providers in known_groups
                    for provider in providers
                )
                has_non_cpu = any(
                    provider not in CPU_PROVIDERS
                    for providers in known_groups
                    for provider in providers
                )
                if has_gpu:
                    resident: bool | None = True
                elif (
                    engine_unknown
                    or has_non_cpu
                    or len(known_groups) != len(COMPONENTS)
                ):
                    resident = None
                else:
                    resident = False
                primaries = [providers[0] for providers in known_groups]
                provider = (
                    primaries[0]
                    if not engine_unknown
                    and len(primaries) == len(COMPONENTS)
                    and len(set(primaries)) == 1
                    else None
                )
                provider_unknown = (
                    provider_unknown or engine_unknown or (has_non_cpu and not has_gpu)
                )
                per_engine[key] = {
                    "resident": resident,
                    "device": (
                        "cuda"
                        if resident is True
                        else "cpu"
                        if resident is False
                        else None
                    ),
                    "provider": provider,
                    "sessions": component_chains,
                }

            builders = len(self._builders)
            reserved_build_slots = sum(
                key not in self._entries for key in self._builders
            )
            borrowers = sum(entry.borrowers for entry in self._entries.values())
            resident_values = [item["resident"] for item in per_engine.values()]
            uncertain = (
                builders > 0 or self._cleanup_in_progress or self._cleanup_failed
            )
            if True in resident_values:
                gpu_resident: bool | None = True
            elif uncertain or None in resident_values:
                gpu_resident = None
            else:
                gpu_resident = False

            primaries = [providers[0] for providers in provider_groups]
            effective_provider = (
                primaries[0]
                if not uncertain
                and not provider_unknown
                and primaries
                and len(primaries) == len(self._entries) * len(COMPONENTS)
                and len(set(primaries)) == 1
                else None
            )
            if not self._entries:
                device = None
            elif gpu_resident is True:
                device = "cuda"
            elif gpu_resident is False:
                device = "cpu"
            else:
                device = None

            return {
                "cap": self._cap,
                "current_size": len(self._entries),
                "loaded_keys": loaded_keys,
                "last_evict": last_evict,
                "builders": builders,
                "reserved_build_slots": reserved_build_slots,
                "borrowers": borrowers,
                "waiters": sum(self._waiters.values()),
                "cleanup_in_progress": self._cleanup_in_progress,
                "cleanup_failed": self._cleanup_failed,
                "gpu_resident": gpu_resident,
                "device": device,
                "provider": effective_provider,
                "session_count": len(provider_groups),
                "engines": per_engine,
            }


__all__ = [
    "EngineBuildArtifact",
    "EngineBuildTimeout",
    "EngineLease",
    "EnginePool",
    "EnginePoolBusyError",
]
