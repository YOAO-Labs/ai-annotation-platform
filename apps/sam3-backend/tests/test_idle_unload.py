"""SAM3 image pool lazy load, unload, and cache ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from embedding_cache import CacheEntry, EmbeddingCache
from aap_backend_runtime import BuildArtifact, ManagedLruPool


def _run(coro):
    return asyncio.run(coro)


def _pool(build, cache: EmbeddingCache, cleanup_calls: list[int]):
    def cleanup_attachments(attachments):
        for attachment in attachments:
            attachment.clear()

    return ManagedLruPool(
        cap=1,
        build_resource=lambda key: BuildArtifact(
            build(key),
            attachments=(cache,),
        ),
        key_to_str=str,
        strict_cleanup=lambda: cleanup_calls.append(1),
        cleanup_attachments=cleanup_attachments,
    )


def test_unload_noop_when_already_unloaded() -> None:
    async def scenario() -> None:
        cache = EmbeddingCache(2, "sam3")
        pool = _pool(
            lambda _key: SimpleNamespace(device="cuda:0"),
            cache,
            [],
        )
        assert await pool.unload_all(reason="manual") == 0
        await pool.shutdown()

    _run(scenario())


def test_unload_clears_predictor_cache_and_runs_strict_cleanup() -> None:
    async def scenario() -> None:
        cache = EmbeddingCache(2, "sam3")
        cleanup_calls: list[int] = []
        pool = _pool(
            lambda _key: SimpleNamespace(device="cuda:0"),
            cache,
            cleanup_calls,
        )
        cache.put(
            "k1",
            CacheEntry(
                features={"x": 1},
                orig_hw=(100, 100),
                is_batch=False,
                wh=(100, 100),
            ),
        )
        cache_hit, load_ms, evicted = await pool.warmup("sam3")
        assert cache_hit is False
        assert load_ms is not None and load_ms >= 0
        assert evicted is None
        assert await pool.unload_all(reason="manual") == 1
        assert cache.size() == 0
        assert cleanup_calls == [1]
        await pool.shutdown()

    _run(scenario())


def test_concurrent_cold_borrows_are_single_flight() -> None:
    async def scenario() -> None:
        cache = EmbeddingCache(2, "sam3")
        builds = 0

        def build(_key):
            nonlocal builds
            builds += 1
            return SimpleNamespace(device="cuda:0")

        pool = _pool(build, cache, [])

        async def borrow_once():
            async with pool.borrow("sam3") as lease:
                return lease.resource

        resources = await asyncio.gather(*(borrow_once() for _ in range(3)))
        assert builds == 1
        assert all(resource is resources[0] for resource in resources)
        await pool.shutdown()

    _run(scenario())


def test_unload_then_borrow_rebuilds() -> None:
    async def scenario() -> None:
        cache = EmbeddingCache(2, "sam3")
        resources = []

        def build(_key):
            resource = SimpleNamespace(device="cuda:0")
            resources.append(resource)
            return resource

        pool = _pool(build, cache, [])
        async with pool.borrow("sam3") as first:
            assert first.cache_hit is False
        assert await pool.unload_all(reason="manual") == 1
        async with pool.borrow("sam3") as second:
            assert second.cache_hit is False
            assert second.resource is not resources[0]
        assert len(resources) == 2
        await pool.shutdown()

    _run(scenario())


def test_idle_unload_defaults() -> None:
    import main

    assert main.IDLE_UNLOAD_SECONDS == 600.0
    assert main.IDLE_CHECK_INTERVAL == 60.0
