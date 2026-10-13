"""Legacy PoolStatus projection over the managed image pool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import main
from aap_backend_runtime import BuildArtifact, ManagedLruPool


def _run(coro):
    return asyncio.run(coro)


def _pool():
    return ManagedLruPool(
        1,
        lambda _key: BuildArtifact(SimpleNamespace(device="cuda:0")),
        str,
        lambda: None,
    )


def test_pool_status_when_unloaded() -> None:
    async def scenario() -> None:
        pool = _pool()
        status = main._legacy_image_pool_status(await pool.snapshot())
        assert status == {
            "cap": 1,
            "current_size": 0,
            "loaded_keys": [],
            "last_evict": None,
        }
        await pool.shutdown()

    _run(scenario())


def test_pool_status_tracks_load_hits_and_manual_evict() -> None:
    async def scenario() -> None:
        pool = _pool()
        cache_hit, load_ms, evicted = await pool.warmup("sam3")
        assert cache_hit is False
        assert load_ms is not None and load_ms >= 0
        assert evicted is None

        async with pool.borrow("sam3") as lease:
            assert lease.cache_hit is True
        async with pool.borrow("sam3") as lease:
            assert lease.cache_hit is True

        status = main._legacy_image_pool_status(await pool.snapshot())
        assert status["current_size"] == 1
        assert status["loaded_keys"][0]["key"] == "sam3"
        assert status["loaded_keys"][0]["hit_count"] == 2
        assert "borrowers" not in status["loaded_keys"][0]

        assert await pool.unload_all(reason="manual") == 1
        status = main._legacy_image_pool_status(await pool.snapshot())
        assert status["last_evict"]["key"] == "sam3"
        assert status["last_evict"]["reason"] == "manual"
        await pool.shutdown()

    _run(scenario())


def test_warmup_hit_does_not_increment_hit_count() -> None:
    async def scenario() -> None:
        pool = _pool()
        assert (await pool.warmup("sam3"))[0] is False
        assert (await pool.warmup("sam3"))[0] is True
        status = main._legacy_image_pool_status(await pool.snapshot())
        assert status["loaded_keys"][0]["hit_count"] == 0
        await pool.shutdown()

    _run(scenario())


def test_idle_unload_projects_idle_timeout_reason() -> None:
    async def scenario() -> None:
        pool = _pool()
        await pool.warmup("sam3")
        assert await pool.unload_idle(idle_before=float("inf")) == 1
        status = main._legacy_image_pool_status(await pool.snapshot())
        assert status["last_evict"]["reason"] == "idle_timeout"
        await pool.shutdown()

    _run(scenario())
