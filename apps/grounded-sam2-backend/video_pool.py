"""Cancellation-safe SAM2 video tracker pool."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aap_backend_runtime import (
    BuildArtifact,
    ManagedBuildTimeout,
    ManagedLruPool,
    ManagedPoolBusyError,
)

if TYPE_CHECKING:
    from video_predictor import SAM2VideoTracker

VideoBuildTimeout = ManagedBuildTimeout
VideoPoolBusyError = ManagedPoolBusyError


@dataclass(frozen=True, slots=True)
class TrackerLease:
    sam_variant: str
    tracker: SAM2VideoTracker
    cache_hit: bool
    model_load_ms: int | None


class VideoPool:
    """Own video trackers and serialize each tracker's mutable session state."""

    def __init__(
        self,
        cap: int,
        build_tracker: Callable[[str], SAM2VideoTracker],
        free_gpu_memory: Callable[[], None],
        *,
        preflight_model: Callable[[str], None] | None = None,
        build_timeout: float = 60.0,
        idle_unload_seconds: float = 600.0,
        build_serial_lock: asyncio.Lock | None = None,
    ) -> None:
        self._build_tracker = build_tracker
        self._idle_unload_seconds = idle_unload_seconds
        self._last_request_at = time.monotonic()
        self._pool: ManagedLruPool[str, SAM2VideoTracker] = ManagedLruPool(
            cap,
            self._build,
            lambda key: key,
            free_gpu_memory,
            device_of=lambda tracker: tracker.device,
            preflight=preflight_model,
            build_timeout=build_timeout,
            build_serial_lock=build_serial_lock,
            pool_name="video model pool",
            logger_name="grounded-sam2-backend.managed-pool",
        )

    def _build(self, sam_variant: str) -> BuildArtifact[SAM2VideoTracker]:
        tracker = self._build_tracker(sam_variant)
        return BuildArtifact(
            tracker,
            cleanup_uncertain=(getattr(tracker, "cleanup_uncertain", False) is True),
        )

    @property
    def cap(self) -> int:
        return self._pool.cap

    @property
    def idle_unload_seconds(self) -> float:
        return self._idle_unload_seconds

    @asynccontextmanager
    async def borrow(self, sam_variant: str) -> AsyncIterator[TrackerLease]:
        self._last_request_at = time.monotonic()
        async with self._pool.borrow(sam_variant) as lease:
            yield TrackerLease(
                sam_variant=sam_variant,
                tracker=lease.resource,
                cache_hit=lease.cache_hit,
                model_load_ms=lease.load_ms,
            )

    async def warmup(
        self,
        sam_variant: str,
    ) -> tuple[bool, int | None, str | None]:
        self._last_request_at = time.monotonic()
        return await self._pool.warmup(sam_variant)

    async def is_loaded(self, sam_variant: str) -> bool:
        snapshot = await self._pool.snapshot()
        return any(item["key"] == sam_variant for item in snapshot["loaded_keys"])

    async def builder_for(self, sam_variant: str) -> asyncio.Task[Any] | None:
        return await self._pool.builder_for(sam_variant)

    def builder_for_now(self, sam_variant: str) -> asyncio.Task[Any] | None:
        return self._pool.builder_for_now(sam_variant)

    async def snapshot(self) -> dict[str, Any]:
        snapshot = await self._pool.snapshot()
        snapshot.update(
            {
                "idle_seconds": round(
                    time.monotonic() - self._last_request_at,
                    2,
                ),
                "idle_unload_seconds": self._idle_unload_seconds,
            }
        )
        return snapshot

    async def pool_status(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        return {
            key: snapshot[key]
            for key in ("cap", "current_size", "loaded_keys", "last_evict")
        }

    async def health(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        return {
            key: snapshot[key]
            for key in (
                "cap",
                "current_size",
                "loaded_keys",
                "last_evict",
                "active_sessions",
                "idle_seconds",
                "idle_unload_seconds",
            )
        }

    async def unload_all(
        self,
        *,
        reason: str = "idle",
        idle_before: float | None = None,
        force_cleanup: bool = False,
    ) -> int:
        return await self._pool.unload_all(
            reason=reason,
            idle_before=idle_before,
            force_cleanup=force_cleanup,
        )

    async def unload_idle(self, *, idle_before: float) -> int:
        return await self._pool.unload_idle(idle_before=idle_before)

    async def shutdown(self) -> None:
        await self._pool.shutdown()


__all__ = [
    "TrackerLease",
    "VideoBuildTimeout",
    "VideoPool",
    "VideoPoolBusyError",
]
