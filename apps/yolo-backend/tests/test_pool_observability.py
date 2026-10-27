"""ModelPool runtime observability and eviction tests."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_pool(cap: int = 2):
    from model_pool import ModelPool

    def _build(task: str, series: str, size: str):
        model = MagicMock(name=f"{series}-{size}-{task}")
        model.device = "cpu"
        return model

    return ModelPool(
        cap=cap,
        build_model=_build,
        free_gpu_memory=lambda: None,
        build_timeout=5.0,
    )


async def _borrow_once(pool, size: str = "s"):
    async with pool.borrow("detection", "yolo11", size) as lease:
        return lease


async def test_borrow_first_call_returns_cache_miss_with_load_ms() -> None:
    lease = await _borrow_once(_make_pool())
    assert lease.model is not None
    assert lease.cache_hit is False
    assert lease.model_load_ms is not None and lease.model_load_ms >= 0


async def test_borrow_second_call_returns_cache_hit_with_none_load_ms() -> None:
    pool = _make_pool()
    await _borrow_once(pool)
    lease = await _borrow_once(pool)
    assert lease.cache_hit is True
    assert lease.model_load_ms is None


async def test_borrow_increments_hit_count_only_on_hit() -> None:
    pool = _make_pool()
    await _borrow_once(pool)
    status = await pool.snapshot()
    assert status["loaded_keys"][0]["hit_count"] == 0
    await _borrow_once(pool)
    await _borrow_once(pool)
    status = await pool.snapshot()
    assert status["loaded_keys"][0]["hit_count"] == 2


async def test_pool_snapshot_shape_matches_protocol() -> None:
    pool = _make_pool(cap=2)
    await _borrow_once(pool)
    snapshot = await pool.snapshot()
    assert snapshot["cap"] == 2
    assert snapshot["current_size"] == 1
    assert snapshot["last_evict"] is None
    assert len(snapshot["loaded_keys"]) == 1
    item = snapshot["loaded_keys"][0]
    assert item["key"] == "yolo11/s/detection"
    assert isinstance(item["loaded_at"], str) and "T" in item["loaded_at"]
    assert isinstance(item["last_used_at"], str)
    assert item["hit_count"] == 0
    assert item["borrowers"] == 0


async def test_pool_snapshot_lru_evict_sets_last_evict_reason_lru() -> None:
    pool = _make_pool(cap=2)
    await _borrow_once(pool, "s")
    await _borrow_once(pool, "m")
    await _borrow_once(pool, "n")
    snapshot = await pool.snapshot()
    assert snapshot["current_size"] == 2
    assert snapshot["last_evict"] is not None
    assert snapshot["last_evict"]["key"] == "yolo11/s/detection"
    assert snapshot["last_evict"]["reason"] == "lru"


async def test_warmup_first_returns_miss_does_not_count_as_hit() -> None:
    pool = _make_pool()
    cache_hit, load_ms, evicted = await pool.warmup("detection", "yolo11", "s")
    assert cache_hit is False
    assert load_ms is not None and load_ms >= 0
    assert evicted is None
    assert (await pool.snapshot())["loaded_keys"][0]["hit_count"] == 0


async def test_warmup_second_returns_hit_with_none_load_ms() -> None:
    pool = _make_pool()
    await pool.warmup("detection", "yolo11", "s")
    cache_hit, load_ms, evicted = await pool.warmup("detection", "yolo11", "s")
    assert cache_hit is True
    assert load_ms is None
    assert evicted is None


async def test_warmup_returns_evicted_key_when_pool_full() -> None:
    pool = _make_pool(cap=1)
    await pool.warmup("detection", "yolo11", "s")
    cache_hit, _load_ms, evicted = await pool.warmup("detection", "yolo11", "m")
    assert cache_hit is False
    assert evicted == "yolo11/s/detection"


async def test_unload_all_idle_sets_last_evict_idle_timeout() -> None:
    pool = _make_pool()
    await _borrow_once(pool)
    assert await pool.unload_all(reason="idle") == 1
    snapshot = await pool.snapshot()
    assert snapshot["current_size"] == 0
    assert snapshot["last_evict"]["reason"] == "idle_timeout"


async def test_unload_all_manual_sets_last_evict_manual() -> None:
    pool = _make_pool()
    await _borrow_once(pool)
    assert await pool.unload_all(reason="manual") == 1
    assert (await pool.snapshot())["last_evict"]["reason"] == "manual"


async def test_pool_snapshot_after_evict_excludes_evicted_key() -> None:
    pool = _make_pool(cap=2)
    await _borrow_once(pool, "s")
    await _borrow_once(pool, "m")
    await _borrow_once(pool, "n")
    keys = {item["key"] for item in (await pool.snapshot())["loaded_keys"]}
    assert "yolo11/s/detection" not in keys
    assert "yolo11/m/detection" in keys
    assert "yolo11/n/detection" in keys
