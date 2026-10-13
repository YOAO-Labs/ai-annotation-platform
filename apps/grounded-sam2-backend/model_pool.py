"""Cancellation-safe image model pool for Grounded-SAM2."""

from __future__ import annotations

import asyncio
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
from embedding_cache import EmbeddingCache

if TYPE_CHECKING:
    from predictor import GroundedSAM2Predictor

VariantKey = tuple[str, str]
ModelBuildTimeout = ManagedBuildTimeout
ModelPoolBusyError = ManagedPoolBusyError


@dataclass(frozen=True, slots=True)
class ModelLease:
    """One borrowed predictor and its variant-scoped embedding cache."""

    key: VariantKey
    predictor: GroundedSAM2Predictor
    cache: EmbeddingCache
    cache_hit: bool
    model_load_ms: int | None


class ModelPool:
    """Own image predictors, caches, builders, borrowers, and cleanup as one pool."""

    def __init__(
        self,
        cap: int,
        build_predictor: Callable[[str, str, EmbeddingCache], GroundedSAM2Predictor],
        free_gpu_memory: Callable[[], None],
        *,
        embedding_cache_size: int = 16,
        preflight_model: Callable[[str, str], None] | None = None,
        build_timeout: float = 30.0,
        build_serial_lock: asyncio.Lock | None = None,
    ) -> None:
        self._build_predictor = build_predictor
        self._embedding_cache_size = embedding_cache_size
        self._preflight_model = preflight_model
        self._pool: ManagedLruPool[VariantKey, GroundedSAM2Predictor] = ManagedLruPool(
            cap,
            self._build,
            self._key_str,
            free_gpu_memory,
            cleanup_attachments=self._cleanup_attachments,
            device_of=lambda predictor: predictor.device,
            preflight=(
                (lambda key: self._preflight_model(*key))
                if self._preflight_model is not None
                else None
            ),
            build_timeout=build_timeout,
            build_serial_lock=build_serial_lock,
            pool_name="image model pool",
            logger_name="grounded-sam2-backend.managed-pool",
        )

    def _build(
        self,
        key: VariantKey,
    ) -> BuildArtifact[GroundedSAM2Predictor]:
        sam_variant, dino_variant = key
        cache = EmbeddingCache(
            capacity=self._embedding_cache_size,
            sam_variant=sam_variant,
        )
        predictor = self._build_predictor(sam_variant, dino_variant, cache)
        return BuildArtifact(
            predictor,
            (cache,),
            cleanup_uncertain=(getattr(predictor, "cleanup_uncertain", False) is True),
        )

    @staticmethod
    def _cleanup_attachments(attachments: tuple[Any, ...]) -> None:
        for attachment in attachments:
            if isinstance(attachment, EmbeddingCache):
                attachment.clear()

    @staticmethod
    def _key_str(key: VariantKey) -> str:
        sam_variant, dino_variant = key
        return f"sam={sam_variant}/dino={dino_variant}"

    @property
    def cap(self) -> int:
        return self._pool.cap

    @asynccontextmanager
    async def borrow(
        self,
        sam_variant: str,
        dino_variant: str,
    ) -> AsyncIterator[ModelLease]:
        key = (sam_variant, dino_variant)
        async with self._pool.borrow(key) as lease:
            cache = lease.attachments[0]
            if not isinstance(cache, EmbeddingCache):
                raise RuntimeError("image model cache ownership is invalid")
            yield ModelLease(
                key=key,
                predictor=lease.resource,
                cache=cache,
                cache_hit=lease.cache_hit,
                model_load_ms=lease.load_ms,
            )

    async def warmup(
        self,
        sam_variant: str,
        dino_variant: str,
    ) -> tuple[bool, int | None, str | None]:
        return await self._pool.warmup((sam_variant, dino_variant))

    async def is_loaded(self, sam_variant: str, dino_variant: str) -> bool:
        snapshot = await self._pool.snapshot()
        key = self._key_str((sam_variant, dino_variant))
        return any(item["key"] == key for item in snapshot["loaded_keys"])

    async def builder_for(
        self,
        sam_variant: str,
        dino_variant: str,
    ) -> asyncio.Task[Any] | None:
        return await self._pool.builder_for((sam_variant, dino_variant))

    def builder_for_now(
        self,
        sam_variant: str,
        dino_variant: str,
    ) -> asyncio.Task[Any] | None:
        return self._pool.builder_for_now((sam_variant, dino_variant))

    async def snapshot(self) -> dict[str, Any]:
        return await self._pool.snapshot()

    async def pool_status(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        return {
            key: snapshot[key]
            for key in ("cap", "current_size", "loaded_keys", "last_evict")
        }

    async def aggregate_cache_stats(self) -> dict[str, Any]:
        buckets: dict[str, dict[str, Any]] = {}
        total_size = total_hits = total_misses = 0
        rows = await self._pool.inspect_entries(
            lambda key, _predictor, attachments: (
                f"{key[0]}/{key[1]}",
                attachments[0].stats(),
            )
        )
        for key, stats in rows:
            buckets[key] = stats
            total_size += stats["size"]
            total_hits += stats["hits"]
            total_misses += stats["misses"]
        total = total_hits + total_misses
        return {
            "size": total_size,
            "hits": total_hits,
            "misses": total_misses,
            "hit_rate": round(total_hits / total, 4) if total else 0.0,
            "buckets": buckets,
        }

    async def total_cache_size(self) -> int:
        return sum(
            await self._pool.inspect_entries(
                lambda _key, _predictor, attachments: attachments[0].size()
            )
        )

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
    "ModelBuildTimeout",
    "ModelLease",
    "ModelPool",
    "ModelPoolBusyError",
    "VariantKey",
]
