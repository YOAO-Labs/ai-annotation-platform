"""Cancellation-safe LRU ownership shared by the ML backend pools.

Each consumer passes its own ``logger_name`` so log records keep the emitting
backend's identity even though the pool implementation is shared.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

logger = logging.getLogger("aap_backend_runtime.managed-pool")
UTC = timezone.utc

KeyT = TypeVar("KeyT", bound=Hashable)
ResourceT = TypeVar("ResourceT")


class ManagedPoolBusyError(RuntimeError):
    """No pool slot can be changed without crossing a real owner."""


class ManagedBuildTimeout(TimeoutError):
    """The caller timed out while the real builder remains pool-owned."""

    def __init__(
        self,
        message: str,
        *,
        builder: asyncio.Task[_BuildResult],
    ) -> None:
        super().__init__(message)
        self.builder = builder


@dataclass(slots=True)
class BuildArtifact(Generic[ResourceT]):
    """One built root plus attachments that share its physical lifetime."""

    resource: ResourceT
    attachments: tuple[Any, ...] = ()
    cleanup_uncertain: bool = False


@dataclass(slots=True)
class _PoolEntry(Generic[KeyT, ResourceT]):
    key: KeyT
    artifact: BuildArtifact[ResourceT]
    loaded_at: datetime
    last_used_at: datetime
    last_used_monotonic: float
    hit_count: int = 0
    borrowers: int = 0
    use_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class ResourceLease(Generic[KeyT, ResourceT]):
    key: KeyT
    resource: ResourceT
    attachments: tuple[Any, ...]
    cache_hit: bool
    load_ms: int | None


@dataclass(frozen=True, slots=True)
class _BuildResult:
    load_ms: int
    evicted_key: str | None


class ManagedLruPool(Generic[KeyT, ResourceT]):
    """LRU pool whose builders, waiters, borrowers, and cleanup stay explicit."""

    def __init__(
        self,
        cap: int,
        build_resource: Callable[[KeyT], BuildArtifact[ResourceT]],
        key_to_str: Callable[[KeyT], str],
        strict_cleanup: Callable[[], None],
        *,
        cleanup_attachments: Callable[[tuple[Any, ...]], None] | None = None,
        device_of: Callable[[ResourceT], str | None] | None = None,
        preflight: Callable[[KeyT], None] | None = None,
        build_timeout: float = 30.0,
        build_serial_lock: asyncio.Lock | None = None,
        pool_name: str = "resources",
        logger_name: str | None = None,
    ) -> None:
        if cap <= 0:
            raise ValueError("cap must be positive")
        if build_timeout <= 0:
            raise ValueError("build_timeout must be positive")
        self._cap = cap
        self._build_resource = build_resource
        self._key_to_str = key_to_str
        self._strict_cleanup = strict_cleanup
        self._cleanup_attachments = cleanup_attachments or (lambda _items: None)
        self._device_of = device_of or (
            lambda resource: getattr(resource, "device", None)
        )
        self._preflight = preflight
        self._build_timeout = build_timeout
        self._pool_name = pool_name
        self._logger = logging.getLogger(logger_name) if logger_name else logger

        self._entries: OrderedDict[KeyT, _PoolEntry[KeyT, ResourceT]] = OrderedDict()
        self._builders: dict[KeyT, asyncio.Task[_BuildResult]] = {}
        self._waiters: dict[KeyT, int] = {}
        self._last_evict: dict[str, Any] | None = None
        self._cleanup_in_progress = False
        self._cleanup_failed = False
        self._cleanup_quarantine: list[BuildArtifact[ResourceT]] = []
        self._pending_evictions: dict[
            asyncio.Task[_BuildResult],
            tuple[KeyT, BuildArtifact[ResourceT]],
        ] = {}
        self._builders_retiring: set[asyncio.Task[_BuildResult]] = set()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._builder_retirements: set[asyncio.Task[None]] = set()

        self._lock = asyncio.Lock()
        self._build_serial_lock = build_serial_lock or asyncio.Lock()

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def loaded(self) -> bool:
        return bool(self._entries)

    def is_loaded(self, key: KeyT) -> bool:
        return key in self._entries

    @asynccontextmanager
    async def borrow(self, key: KeyT) -> AsyncIterator[ResourceLease[KeyT, ResourceT]]:
        """Borrow one entry and serialize use of its mutable model state."""

        entry, cache_hit, load_ms = await self._acquire_borrower(key)
        use_lock_acquired = False
        try:
            await entry.use_lock.acquire()
            use_lock_acquired = True
            yield ResourceLease(
                key=key,
                resource=entry.artifact.resource,
                attachments=entry.artifact.attachments,
                cache_hit=cache_hit,
                load_ms=load_ms,
            )
        finally:
            if use_lock_acquired:
                entry.use_lock.release()
            await self._release_borrower_cancellation_safe(entry)

    async def warmup(self, key: KeyT) -> tuple[bool, int | None, str | None]:
        """Ensure one entry is resident without borrowing it for inference."""

        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._touch_locked(entry, count_hit=False)
                return True, None, None
            builder = self._get_or_start_builder_locked(key)
            self._waiters[key] = self._waiters.get(key, 0) + 1

        try:
            result = await self._wait_for_builder(key, builder)
        finally:
            await self._drop_waiter_cancellation_safe(key)
        return False, result.load_ms, result.evicted_key

    async def _acquire_borrower(
        self,
        key: KeyT,
    ) -> tuple[_PoolEntry[KeyT, ResourceT], bool, int | None]:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.borrowers += 1
                self._touch_locked(entry, count_hit=True)
                return entry, True, None
            builder = self._get_or_start_builder_locked(key)
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

    async def _release_borrower(
        self,
        entry: _PoolEntry[KeyT, ResourceT],
    ) -> None:
        async with self._lock:
            current = self._entries.get(entry.key)
            if current is not entry or entry.borrowers <= 0:
                raise RuntimeError(f"invalid borrower release for {entry.key!r}")
            entry.borrowers -= 1
            self._touch_locked(entry, count_hit=False)

    async def _release_borrower_cancellation_safe(
        self,
        entry: _PoolEntry[KeyT, ResourceT],
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
        key: KeyT,
    ) -> asyncio.Task[_BuildResult]:
        self._prune_done_builders_locked()
        self._refresh_cleanup_failed_locked()
        builder = self._builders.get(key)
        if builder is not None:
            return builder
        if self._cleanup_in_progress:
            raise ManagedPoolBusyError(f"{self._pool_name} cleanup is in progress")
        if self._cleanup_failed:
            raise ManagedPoolBusyError(
                f"{self._pool_name} residency is unknown; full cleanup is required"
            )
        # This callback is restricted to cheap checks that cannot allocate a
        # resource. Running it before eviction keeps proven pre-build failures
        # out of the builder/cleanup state machine.
        if self._preflight is not None:
            self._preflight(key)

        evicted: tuple[KeyT, _PoolEntry[KeyT, ResourceT]] | None = None
        if len(self._entries) + self._reserved_build_slots_locked() >= self._cap:
            for candidate_key, candidate in self._entries.items():
                if (
                    candidate.borrowers == 0
                    and self._waiters.get(candidate_key, 0) == 0
                ):
                    evicted = (candidate_key, candidate)
                    break
            if evicted is None:
                raise ManagedPoolBusyError(f"all {self._pool_name} slots are active")
            self._entries.pop(evicted[0])
            self._record_evict_locked(evicted[0], "lru")

        evicted_key = evicted[0] if evicted is not None else None
        task = asyncio.create_task(self._build_and_publish(key, evicted_key))
        if evicted is not None:
            # Establish a root outside the coroutine before it can be cancelled.
            self._pending_evictions[task] = (evicted[0], evicted[1].artifact)
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
        key: KeyT,
        expected_evicted_key: KeyT | None,
    ) -> _BuildResult:
        current_task = asyncio.current_task()
        artifact: BuildArtifact[ResourceT] | None = None
        entry: _PoolEntry[KeyT, ResourceT] | None = None
        evicted_key: str | None = None
        cleanup_artifacts: list[BuildArtifact[ResourceT]] = []
        pending_eviction = self._pending_evictions.pop(current_task, None)
        try:
            if pending_eviction is not None:
                pending_key, pending_artifact = pending_eviction
                if pending_key != expected_evicted_key:
                    raise RuntimeError("pending eviction ownership changed")
                evicted_key = self._key_to_str(pending_key)
                cleanup_artifacts = [pending_artifact]
                pending_eviction = None
                _, cleanup_cancelled = await self._run_executor_to_completion(
                    self._release_artifacts_sync,
                    cleanup_artifacts,
                )
                if cleanup_cancelled:
                    raise asyncio.CancelledError

            started = time.monotonic()
            async with self._build_serial_lock:
                artifact, build_cancelled = await self._run_executor_to_completion(
                    self._build_resource,
                    key,
                )
            if build_cancelled:
                raise asyncio.CancelledError
            load_ms = int((time.monotonic() - started) * 1000)
            loaded_at = datetime.now(UTC)
            entry = _PoolEntry(
                key=key,
                artifact=artifact,
                loaded_at=loaded_at,
                last_used_at=loaded_at,
                last_used_monotonic=time.monotonic(),
            )

            async with self._lock:
                if self._builders.get(key) is not current_task:
                    raise RuntimeError(f"builder ownership changed for {key!r}")
                self._entries[key] = entry
                if artifact.cleanup_uncertain:
                    self._cleanup_failed = True
                self._assert_capacity_locked()
            return _BuildResult(load_ms=load_ms, evicted_key=evicted_key)
        except BaseException as build_error:
            complete_artifact_owned = artifact is not None
            if artifact is not None:
                cleanup_artifacts.append(artifact)
            artifact = None
            entry = None
            cleanup_cancelled = False
            cleanup_error: BaseException | None = None
            try:
                _, cleanup_cancelled = await self._run_executor_to_completion(
                    self._release_artifacts_sync,
                    cleanup_artifacts,
                )
            except asyncio.CancelledError:
                cleanup_cancelled = True
            except BaseException as exc:
                cleanup_error = exc
                self._logger.exception(
                    "cleanup after failed %s build also failed", self._pool_name
                )
            _, commit_cancelled = await self._run_task_to_completion(
                self._commit_failed_build(
                    key,
                    current_task,
                    failed_artifacts=cleanup_artifacts,
                    mark_unknown=(
                        cleanup_error is not None
                        or not (
                            isinstance(build_error, asyncio.CancelledError)
                            and complete_artifact_owned
                        )
                    ),
                )
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
        key: KeyT,
        current_task: asyncio.Task[Any] | None,
        *,
        failed_artifacts: list[BuildArtifact[ResourceT]],
        mark_unknown: bool,
    ) -> None:
        async with self._lock:
            if self._builders.get(key) is not current_task:
                raise RuntimeError(f"builder ownership changed for {key!r}")
            self._cleanup_quarantine.extend(failed_artifacts)
            failed_artifacts.clear()
            self._cleanup_failed = self._cleanup_failed or mark_unknown
            self._assert_capacity_locked()

    def _builder_completed(
        self,
        key: KeyT,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        self._consume_task_result(task)
        self._builders_retiring.add(task)
        retirement = asyncio.create_task(self._retire_builder(key, task))
        self._builder_retirements.add(retirement)
        retirement.add_done_callback(self._builder_retirement_completed)

    def _builder_retirement_completed(self, task: asyncio.Task[None]) -> None:
        self._builder_retirements.discard(task)
        self._consume_task_result(task)

    async def _retire_builder(
        self,
        key: KeyT,
        task: asyncio.Task[_BuildResult],
    ) -> None:
        pending = self._pending_evictions.get(task)
        cleanup_artifacts = [pending[1]] if pending is not None else []
        cleanup_error: BaseException | None = None
        if cleanup_artifacts:
            try:
                await self._run_executor_to_completion(
                    self._release_artifacts_sync,
                    cleanup_artifacts,
                )
            except BaseException as exc:
                cleanup_error = exc
        async with self._lock:
            self._pending_evictions.pop(task, None)
            self._builders_retiring.discard(task)
            if cleanup_error is not None:
                self._cleanup_quarantine.extend(cleanup_artifacts)
                cleanup_artifacts.clear()
                self._cleanup_failed = True
            if self._builders.get(key) is task:
                self._builders.pop(key)
            self._assert_capacity_locked()

    def _prune_done_builders_locked(self) -> None:
        for key, task in list(self._builders.items()):
            if (
                task.done()
                and task not in self._pending_evictions
                and self._builders.get(key) is task
            ):
                self._builders.pop(key)

    async def _wait_for_builder(
        self,
        key: KeyT,
        builder: asyncio.Task[_BuildResult],
    ) -> _BuildResult:
        try:
            return await asyncio.wait_for(
                asyncio.shield(builder),
                timeout=self._build_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ManagedBuildTimeout(
                f"{self._pool_name} build timeout ({self._build_timeout}s) "
                f"for {key!r}; builder remains tracked",
                builder=builder,
            ) from exc

    async def builder_for(self, key: KeyT) -> asyncio.Task[_BuildResult] | None:
        async with self._lock:
            self._prune_done_builders_locked()
            return self._builders.get(key)

    def builder_for_now(self, key: KeyT) -> asyncio.Task[_BuildResult] | None:
        builder = self._builders.get(key)
        return builder if builder is not None and not builder.done() else None

    async def _drop_waiter(self, key: KeyT) -> None:
        async with self._lock:
            self._drop_waiter_locked(key)

    async def _drop_waiter_cancellation_safe(self, key: KeyT) -> None:
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

    def _drop_waiter_locked(self, key: KeyT) -> None:
        waiters = self._waiters.get(key, 0)
        if waiters <= 1:
            self._waiters.pop(key, None)
        else:
            self._waiters[key] = waiters - 1

    def _touch_locked(
        self,
        entry: _PoolEntry[KeyT, ResourceT],
        *,
        count_hit: bool,
    ) -> None:
        self._entries.move_to_end(entry.key)
        entry.last_used_at = datetime.now(UTC)
        entry.last_used_monotonic = time.monotonic()
        if count_hit:
            entry.hit_count += 1

    def _record_evict_locked(self, key: KeyT, reason: str) -> None:
        self._last_evict = {
            "key": self._key_to_str(key),
            "at": datetime.now(UTC),
            "reason": reason,
        }
        self._logger.info("%s evicted %r (reason=%s)", self._pool_name, key, reason)

    def _reserved_build_slots_locked(self) -> int:
        return sum(key not in self._entries for key in self._builders)

    def _assert_capacity_locked(self) -> None:
        if len(self._entries) + self._reserved_build_slots_locked() > self._cap:
            raise RuntimeError(f"{self._pool_name} capacity invariant violated")

    @staticmethod
    def _entry_cleanup_uncertain(entry: _PoolEntry[Any, Any]) -> bool:
        return bool(
            entry.artifact.cleanup_uncertain
            or getattr(entry.artifact.resource, "cleanup_uncertain", False) is True
        )

    def _refresh_cleanup_failed_locked(self) -> None:
        if self._cleanup_quarantine or any(
            self._entry_cleanup_uncertain(entry) for entry in self._entries.values()
        ):
            self._cleanup_failed = True

    def _release_artifacts_sync(
        self,
        artifacts: list[BuildArtifact[ResourceT]],
    ) -> None:
        artifact: BuildArtifact[ResourceT] | None = None
        for artifact in artifacts:
            self._cleanup_attachments(artifact.attachments)
        artifact = None
        artifacts.clear()
        gc.collect()
        self._strict_cleanup()

    async def unload_all(
        self,
        *,
        reason: str = "idle",
        idle_before: float | None = None,
        force_cleanup: bool = False,
    ) -> int:
        """Release every entry without crossing a real pool owner."""

        async with self._lock:
            self._prune_done_builders_locked()
            self._refresh_cleanup_failed_locked()
            if self._cleanup_in_progress:
                if reason == "idle":
                    return 0
                raise ManagedPoolBusyError(f"{self._pool_name} cleanup is in progress")
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
                raise ManagedPoolBusyError(
                    f"{self._pool_name} has active builders, waiters, or borrowers"
                )

            count = len(self._entries)
            if count == 0 and not force_cleanup:
                return 0
            artifacts = [entry.artifact for entry in self._entries.values()]
            artifacts.extend(self._cleanup_quarantine)
            last_key = next(reversed(self._entries), None)
            self._entries.clear()
            self._cleanup_quarantine.clear()
            self._cleanup_in_progress = True

        cleanup = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(
                None,
                self._release_artifacts_sync,
                artifacts,
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
                failed_artifacts=artifacts,
                last_key=last_key,
                reason=reason,
            )
        )
        cancelled = cancelled or commit_cancelled

        if cleanup_error is not None:
            if cancelled:
                raise asyncio.CancelledError from cleanup_error
            raise cleanup_error
        if cancelled:
            raise asyncio.CancelledError
        return count

    async def _commit_cleanup_result(
        self,
        *,
        cleanup_error: BaseException | None,
        failed_artifacts: list[BuildArtifact[ResourceT]],
        last_key: KeyT | None,
        reason: str,
    ) -> None:
        async with self._lock:
            self._cleanup_in_progress = False
            if cleanup_error is not None:
                self._cleanup_quarantine.extend(failed_artifacts)
                failed_artifacts.clear()
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
        """Return ownership metadata and conservative physical residency."""

        async with self._lock:
            self._prune_done_builders_locked()
            self._refresh_cleanup_failed_locked()
            loaded_keys = [
                {
                    "key": self._key_to_str(entry.key),
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

            raw_devices: list[str | None] = []
            for entry in self._entries.values():
                try:
                    value = self._device_of(entry.artifact.resource)
                except Exception:
                    value = None
                raw_devices.append(str(value) if value is not None else None)
            devices = {device for device in raw_devices if device is not None}
            has_gpu = any(
                device.lower().startswith(("cuda", "mps", "xpu")) for device in devices
            )
            unknown = bool(
                self._builders
                or self._cleanup_in_progress
                or self._cleanup_failed
                or any(device is None for device in raw_devices)
            )
            if has_gpu:
                gpu_resident: bool | None = True
            elif unknown:
                gpu_resident = None
            else:
                gpu_resident = False

            return {
                "cap": self._cap,
                "current_size": len(self._entries),
                "loaded_keys": loaded_keys,
                "last_evict": last_evict,
                "builders": len(self._builders),
                "reserved_build_slots": self._reserved_build_slots_locked(),
                "borrowers": sum(entry.borrowers for entry in self._entries.values()),
                "waiters": sum(self._waiters.values()),
                "cleanup_in_progress": self._cleanup_in_progress,
                "cleanup_failed": self._cleanup_failed,
                "gpu_resident": gpu_resident,
                "device": next(iter(devices)) if len(devices) == 1 else None,
                "active_sessions": sum(
                    int(getattr(entry.artifact.resource, "active_sessions", 0))
                    for entry in self._entries.values()
                ),
            }

    async def inspect_entries(
        self,
        inspect: Callable[[KeyT, ResourceT, tuple[Any, ...]], Any],
    ) -> list[Any]:
        """Build value-only diagnostics while roots remain protected by the lock."""

        async with self._lock:
            return [
                inspect(entry.key, entry.artifact.resource, entry.artifact.attachments)
                for entry in self._entries.values()
            ]

    @staticmethod
    async def _run_executor_to_completion(
        call: Callable[..., Any],
        *args: Any,
    ) -> tuple[Any, bool]:
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

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()


__all__ = [
    "BuildArtifact",
    "ManagedBuildTimeout",
    "ManagedLruPool",
    "ManagedPoolBusyError",
    "ResourceLease",
]
