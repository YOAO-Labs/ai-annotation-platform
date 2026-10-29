"""Cancellation-safe ownership for ONNXTools' three lazy inference handles."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("onnxtools-backend.handle-pool")
UTC = timezone.utc

HANDLE_ORDER = ("pipeline", "detector", "va")
HANDLE_SESSION_COUNTS = {"pipeline": 2, "detector": 1, "va": 1}
GPU_PROVIDERS = frozenset({"CUDAExecutionProvider", "TensorrtExecutionProvider"})
CPU_PROVIDERS = frozenset({"CPUExecutionProvider"})


class HandlePoolBusyError(RuntimeError):
    """The fixed handle pool cannot safely start or unload work right now."""


class HandleDeviceError(RuntimeError):
    """A constructed handle does not satisfy the configured device policy."""


class HandleBuildTimeout(RuntimeError):
    """A caller timed out while the real single-flight builder remains active."""

    def __init__(self, message: str, *, builder: asyncio.Task[_BuildResult]) -> None:
        super().__init__(message)
        self.builder = builder


@dataclass(slots=True)
class _HandleEntry:
    name: str
    handle: Any
    loaded_at: datetime
    last_used_at: datetime
    last_used_monotonic: float
    hit_count: int = 0
    borrowers: int = 0
    use_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class HandleLease:
    name: str
    handle: Any
    cache_hit: bool
    handle_load_ms: int | None


@dataclass(frozen=True, slots=True)
class _BuildResult:
    load_ms: int


ProviderInspector = Callable[[str, Any], list[list[str]] | None]


class HandlePool:
    """Fixed-slot pool with single-flight builders and borrower ownership.

    The pool has three logical handles (pipeline, detector, and va), but a pipeline
    owns two ORT sessions.  Every handle construction and inference runs outside the
    metadata lock.  Cleanup may detach entries only when no builder, waiter, or
    borrower can still hold an untracked reference.
    """

    def __init__(
        self,
        factories: Mapping[str, Callable[[], Any]],
        inspect_providers: ProviderInspector,
        *,
        build_timeout: float = 30.0,
        validate_handle: Callable[[str, Any], str | None] | None = None,
        strict_cleanup: Callable[[], None] | None = None,
    ) -> None:
        if set(factories) != set(HANDLE_ORDER):
            raise ValueError(f"factories must define exactly {HANDLE_ORDER!r}")
        if build_timeout <= 0:
            raise ValueError("build_timeout must be positive")

        self._factories = dict(factories)
        self._inspect_providers = inspect_providers
        self._validate_handle = validate_handle
        self._build_timeout = build_timeout
        self._strict_cleanup = strict_cleanup or (lambda: None)
        self._cap = len(HANDLE_ORDER)

        self._entries: OrderedDict[str, _HandleEntry] = OrderedDict()
        self._builders: dict[str, asyncio.Task[_BuildResult]] = {}
        self._waiters: dict[str, int] = {}
        self._last_evict: dict[str, Any] | None = None
        self._cleanup_in_progress = False
        self._cleanup_failed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._builder_retirements: set[asyncio.Task[None]] = set()

        self._lock = asyncio.Lock()
        # Serial construction avoids transiently building four ORT sessions from the
        # composite pipeline alongside another cold handle.
        self._build_serial_lock = asyncio.Lock()

    @property
    def cap(self) -> int:
        return self._cap

    @asynccontextmanager
    async def borrow(self, name: str) -> AsyncIterator[HandleLease]:
        """Borrow one handle and serialize access to its mutable upstream object."""

        entry, cache_hit, load_ms = await self._acquire_borrower(name)
        use_lock_acquired = False
        try:
            await entry.use_lock.acquire()
            use_lock_acquired = True
            yield HandleLease(
                name=name,
                handle=entry.handle,
                cache_hit=cache_hit,
                handle_load_ms=load_ms,
            )
        finally:
            if use_lock_acquired:
                entry.use_lock.release()
            await self._release_borrower_cancellation_safe(entry)

    async def warmup(self, name: str) -> tuple[bool, int | None]:
        """Ensure a handle is resident without borrowing it for inference."""

        self._validate_name(name)
        async with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                self._touch_locked(entry, count_hit=False)
                return True, None
            builder = self._get_or_start_builder_locked(name)
            self._waiters[name] = self._waiters.get(name, 0) + 1

        try:
            result = await self._wait_for_builder(name, builder)
        finally:
            await self._drop_waiter_cancellation_safe(name)
        return False, result.load_ms

    async def _acquire_borrower(
        self,
        name: str,
    ) -> tuple[_HandleEntry, bool, int | None]:
        self._validate_name(name)
        async with self._lock:
            entry = self._entries.get(name)
            if entry is not None:
                entry.borrowers += 1
                self._touch_locked(entry, count_hit=True)
                return entry, True, None
            builder = self._get_or_start_builder_locked(name)
            self._waiters[name] = self._waiters.get(name, 0) + 1

        try:
            result = await self._wait_for_builder(name, builder)
            async with self._lock:
                entry = self._entries.get(name)
                if entry is None:
                    raise RuntimeError(f"completed builder did not publish {name!r}")
                entry.borrowers += 1
                self._touch_locked(entry, count_hit=False)
                self._drop_waiter_locked(name)
                return entry, False, result.load_ms
        except BaseException:
            await self._drop_waiter_cancellation_safe(name)
            raise

    async def _release_borrower(self, entry: _HandleEntry) -> None:
        async with self._lock:
            current = self._entries.get(entry.name)
            if current is not entry or entry.borrowers <= 0:
                raise RuntimeError(f"invalid borrower release for {entry.name!r}")
            entry.borrowers -= 1
            self._touch_locked(entry, count_hit=False)

    async def _release_borrower_cancellation_safe(self, entry: _HandleEntry) -> None:
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
        name: str,
    ) -> asyncio.Task[_BuildResult]:
        self._prune_done_builders_locked()
        builder = self._builders.get(name)
        if builder is not None:
            return builder
        if self._cleanup_in_progress:
            raise HandlePoolBusyError("handle pool cleanup is in progress")
        if self._cleanup_failed:
            raise HandlePoolBusyError(
                "handle residency is unknown; full cleanup is required"
            )
        if len(self._entries) + len(self._builders) >= self._cap:
            raise HandlePoolBusyError("all fixed handle slots are occupied")

        task = asyncio.create_task(self._build_and_publish(name))
        self._builders[name] = task
        task.add_done_callback(
            lambda completed, builder_name=name: self._builder_completed(
                builder_name,
                completed,
            )
        )
        self._assert_capacity_locked()
        return task

    async def _build_and_publish(self, name: str) -> _BuildResult:
        current_task = asyncio.current_task()
        handle: Any = None
        entry: _HandleEntry | None = None
        try:
            started = time.monotonic()
            async with self._build_serial_lock:
                handle, build_cancelled = await self._run_executor_to_completion(
                    self._factories[name],
                )
            if build_cancelled:
                raise asyncio.CancelledError
            if self._validate_handle is not None:
                error = self._validate_handle(name, handle)
                if error is not None:
                    raise HandleDeviceError(error)
            load_ms = int((time.monotonic() - started) * 1000)
            loaded_at = datetime.now(UTC)
            entry = _HandleEntry(
                name=name,
                handle=handle,
                loaded_at=loaded_at,
                last_used_at=loaded_at,
                last_used_monotonic=time.monotonic(),
            )

            async with self._lock:
                if self._builders.get(name) is not current_task:
                    raise RuntimeError(f"builder ownership changed for {name!r}")
                self._entries[name] = entry
                self._assert_capacity_locked()
            return _BuildResult(load_ms=load_ms)
        except BaseException as build_error:
            handles = [handle] if handle is not None else []
            handle = None
            entry = None
            cleanup_cancelled = False
            try:
                _, cleanup_cancelled = await self._run_executor_to_completion(
                    self._release_handles_sync,
                    handles,
                )
            except asyncio.CancelledError:
                cleanup_cancelled = True
            except BaseException:
                logger.exception("cleanup after failed handle build also failed")
            _, commit_cancelled = await self._run_task_to_completion(
                self._commit_failed_build(name, current_task)
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
        name: str,
        current_task: asyncio.Task[Any] | None,
    ) -> None:
        async with self._lock:
            if self._builders.get(name) is not current_task:
                raise RuntimeError(f"builder ownership changed for {name!r}")
            # A factory can allocate a CUDA session before raising.  Only a later
            # successful full cleanup restores a trusted false residency value.
            self._cleanup_failed = True
            self._assert_capacity_locked()

    def _builder_completed(
        self,
        name: str,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        """Retire a builder only after its coroutine and cleanup frame are done."""

        self._consume_task_result(task)
        retirement = asyncio.create_task(self._retire_builder(name, task))
        self._builder_retirements.add(retirement)
        retirement.add_done_callback(self._builder_retirement_completed)

    def _builder_retirement_completed(self, task: asyncio.Task[None]) -> None:
        self._builder_retirements.discard(task)
        self._consume_task_result(task)

    async def _retire_builder(
        self,
        name: str,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        async with self._lock:
            if self._builders.get(name) is task:
                self._builders.pop(name)
            self._assert_capacity_locked()

    def _prune_done_builders_locked(self) -> None:
        for name, task in list(self._builders.items()):
            if task.done() and self._builders.get(name) is task:
                self._builders.pop(name)

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
    async def _run_task_to_completion(coroutine: Any) -> tuple[Any, bool]:
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
        name: str,
        builder: asyncio.Task[_BuildResult],
    ) -> _BuildResult:
        try:
            return await asyncio.wait_for(
                asyncio.shield(builder),
                timeout=self._build_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise HandleBuildTimeout(
                f"handle build timeout ({self._build_timeout}s) for {name!r}; "
                "builder remains tracked",
                builder=builder,
            ) from exc

    async def builder_for(self, name: str) -> asyncio.Task[_BuildResult] | None:
        self._validate_name(name)
        async with self._lock:
            self._prune_done_builders_locked()
            return self._builders.get(name)

    def builder_for_now(self, name: str) -> asyncio.Task[_BuildResult] | None:
        """Read the current builder without yielding on the owning event loop.

        Cancellation handlers use this synchronous view so a repeated cancellation
        cannot interrupt builder discovery before lifecycle ownership is recorded.
        """

        self._validate_name(name)
        builder = self._builders.get(name)
        return builder if builder is not None and not builder.done() else None

    async def _drop_waiter(self, name: str) -> None:
        async with self._lock:
            self._drop_waiter_locked(name)

    async def _drop_waiter_cancellation_safe(self, name: str) -> None:
        cleanup = asyncio.create_task(self._drop_waiter(name))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    def _drop_waiter_locked(self, name: str) -> None:
        waiters = self._waiters.get(name, 0)
        if waiters <= 1:
            self._waiters.pop(name, None)
        else:
            self._waiters[name] = waiters - 1

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _touch_locked(self, entry: _HandleEntry, *, count_hit: bool) -> None:
        self._entries.move_to_end(entry.name)
        entry.last_used_at = datetime.now(UTC)
        entry.last_used_monotonic = time.monotonic()
        if count_hit:
            entry.hit_count += 1

    def _assert_capacity_locked(self) -> None:
        reserved_builders = sum(name not in self._entries for name in self._builders)
        if len(self._entries) + reserved_builders > self._cap:
            raise RuntimeError("handle pool capacity invariant violated")

    def _release_handles_sync(self, handles: list[Any]) -> None:
        handles.clear()
        gc.collect()
        self._strict_cleanup()

    async def unload_all(
        self,
        *,
        reason: str = "idle",
        idle_before: float | None = None,
        force_cleanup: bool = False,
    ) -> int:
        """Release all handles without crossing builders, waiters, or borrowers."""

        async with self._lock:
            self._prune_done_builders_locked()
            if self._cleanup_in_progress:
                if reason == "idle":
                    return 0
                raise HandlePoolBusyError("handle pool cleanup is already in progress")
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
                raise HandlePoolBusyError(
                    "handle pool has active builders, waiters, or borrowers"
                )

            count = len(self._entries)
            if count == 0 and not force_cleanup:
                return 0
            handles = [entry.handle for entry in self._entries.values()]
            last_name = next(reversed(self._entries), None)
            self._entries.clear()
            self._cleanup_in_progress = True

        cleanup = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(
                None, self._release_handles_sync, handles
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
                last_name=last_name,
                reason=reason,
            )
        )
        cancelled = cancelled or commit_cancelled

        if cleanup_error is not None:
            if cancelled:
                raise asyncio.CancelledError from cleanup_error
            raise cleanup_error
        logger.info("handle pool unloaded %d handles (reason=%s)", count, reason)
        if cancelled:
            raise asyncio.CancelledError
        return count

    async def _commit_cleanup_result(
        self,
        *,
        cleanup_error: BaseException | None,
        last_name: str | None,
        reason: str,
    ) -> None:
        async with self._lock:
            self._cleanup_in_progress = False
            if cleanup_error is not None:
                self._cleanup_failed = True
            else:
                self._cleanup_failed = False
                if last_name is not None:
                    self._last_evict = {
                        "key": f"onnxtools/{last_name}",
                        "at": datetime.now(UTC),
                        "reason": "idle_timeout" if reason == "idle" else "manual",
                    }

    async def unload_idle(self, *, idle_before: float) -> int:
        return await self.unload_all(reason="idle", idle_before=idle_before)

    async def shutdown(self) -> None:
        """Finish final cleanup before propagating cancellation to the caller."""

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
        """Wait for all real owners before final full cleanup."""

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
        """Return pool metadata and conservative ORT residency in one snapshot."""

        async with self._lock:
            self._prune_done_builders_locked()
            ordered_entries = [
                self._entries[name] for name in HANDLE_ORDER if name in self._entries
            ]
            loaded_keys = [
                {
                    "key": f"onnxtools/{entry.name}",
                    "loaded_at": entry.loaded_at.isoformat(),
                    "last_used_at": entry.last_used_at.isoformat(),
                    "hit_count": entry.hit_count,
                    "borrowers": entry.borrowers,
                }
                for entry in ordered_entries
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
            per_handle: dict[str, dict[str, Any]] = {}
            for name in HANDLE_ORDER:
                entry = self._entries.get(name)
                if entry is None:
                    resident: bool | None = (
                        None
                        if name in self._builders
                        or self._cleanup_in_progress
                        or self._cleanup_failed
                        else False
                    )
                    per_handle[name] = {
                        "resident": resident,
                        "device": None,
                        "provider": None,
                    }
                    continue

                inspected = self._inspect_providers(entry.name, entry.handle)
                known_groups: list[list[str]] = []
                handle_unknown = inspected is None
                if inspected is not None:
                    for providers in inspected:
                        if providers:
                            known_groups.append(providers)
                        else:
                            handle_unknown = True
                provider_groups.extend(known_groups)
                provider_unknown = provider_unknown or handle_unknown
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
                    handle_resident: bool | None = True
                elif handle_unknown or has_non_cpu or not known_groups:
                    handle_resident = None
                else:
                    handle_resident = False
                handle_primaries = [providers[0] for providers in known_groups]
                handle_provider = (
                    handle_primaries[0]
                    if not handle_unknown
                    and handle_primaries
                    and len(set(handle_primaries)) == 1
                    else None
                )
                per_handle[name] = {
                    "resident": handle_resident,
                    "device": (
                        "cuda"
                        if handle_resident is True
                        else "cpu"
                        if handle_resident is False
                        else None
                    ),
                    "provider": handle_provider,
                }

            builders = len(self._builders)
            reserved_build_slots = sum(
                name not in self._entries for name in self._builders
            )
            borrowers = sum(entry.borrowers for entry in ordered_entries)
            handle_values = [item["resident"] for item in per_handle.values()]
            if True in handle_values:
                # A known GPU session is sufficient evidence even when another
                # composite session is unreadable or currently building.
                gpu_resident: bool | None = True
            elif None in handle_values:
                gpu_resident = None
            else:
                gpu_resident = False

            primary_providers = [providers[0] for providers in provider_groups]
            effective_provider = (
                primary_providers[0]
                if not provider_unknown
                and primary_providers
                and len(set(primary_providers)) == 1
                else None
            )
            if not ordered_entries:
                device = None
            elif gpu_resident is True:
                device = "cuda"
            elif gpu_resident is False:
                device = "cpu"
            else:
                device = None

            return {
                "cap": self._cap,
                "current_size": len(ordered_entries),
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
                "handles": per_handle,
            }

    def _validate_name(self, name: str) -> None:
        if name not in self._factories:
            raise ValueError(f"unknown handle {name!r}")


__all__ = [
    "HANDLE_ORDER",
    "HandleBuildTimeout",
    "HandleLease",
    "HandlePool",
    "HandlePoolBusyError",
]
