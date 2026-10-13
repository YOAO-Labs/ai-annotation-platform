"""EmbeddingCache 行为单测 (无 GPU). 镜像 grounded-sam2-backend 的覆盖, variant 默认 sam3."""

from __future__ import annotations

import threading

import pytest

from embedding_cache import CacheEntry, EmbeddingCache, compute_cache_key


def _entry(tag: int = 0) -> CacheEntry:
    return CacheEntry(
        features={"tag": tag}, orig_hw=(100, 200), is_batch=False, wh=(200, 100)
    )


# ---------- compute_cache_key ----------


def test_cache_key_strips_query_string():
    a = compute_cache_key(
        "https://minio.local/bucket/img.jpg?X-Amz-Signature=aaa&X-Amz-Date=20260101",
        "sam3",
    )
    b = compute_cache_key(
        "https://minio.local/bucket/img.jpg?X-Amz-Signature=bbb&X-Amz-Date=20260102",
        "sam3",
    )
    assert a == b, "presigned URL signature 滚动不应让 key 变化"


def test_cache_key_distinguishes_paths():
    a = compute_cache_key("https://minio.local/bucket/a.jpg", "sam3")
    b = compute_cache_key("https://minio.local/bucket/b.jpg", "sam3")
    assert a != b


def test_cache_key_distinguishes_sam2_vs_sam3():
    """sam2 cache 与 sam3 cache 必须互不污染 (embedding 来自不同模型, 不能跨)."""
    base = "https://minio.local/bucket/img.jpg"
    assert compute_cache_key(base, "tiny") != compute_cache_key(base, "sam3")
    assert compute_cache_key(base, "sam3") != compute_cache_key(base, "sam3-int8")


def test_cache_key_local_path():
    a = compute_cache_key("/data/a.jpg", "sam3")
    b = compute_cache_key("/data/a.jpg", "sam3")
    c = compute_cache_key("/data/b.jpg", "sam3")
    assert a == b
    assert a != c


# ---------- EmbeddingCache ----------


def test_put_get_roundtrip():
    c = EmbeddingCache(capacity=4)
    c.put("k1", _entry(1))
    got = c.get("k1")
    assert got is not None and got.features == {"tag": 1}


def test_default_variant_is_sam3_1():
    c = EmbeddingCache(capacity=4)
    assert c.sam_variant == "sam3"
    assert c.stats()["variant"] == "sam3"


def test_get_miss_returns_none_and_counts():
    c = EmbeddingCache(capacity=4)
    assert c.get("nope") is None
    s = c.stats()
    assert s["misses"] == 1 and s["hits"] == 0


def test_get_hit_counts():
    c = EmbeddingCache(capacity=4)
    c.put("k1", _entry())
    c.get("k1")
    c.get("k1")
    s = c.stats()
    assert s["hits"] == 2 and s["misses"] == 0
    assert s["hit_rate"] == 1.0


def test_lru_eviction_order():
    c = EmbeddingCache(capacity=2)
    c.put("a", _entry(1))
    c.put("b", _entry(2))
    c.get("a")
    c.put("c", _entry(3))
    assert c.get("a") is not None
    assert c.get("b") is None
    assert c.get("c") is not None
    assert c.size() == 2


def test_capacity_upper_bound():
    c = EmbeddingCache(capacity=3)
    for i in range(10):
        c.put(f"k{i}", _entry(i))
    assert c.size() == 3


def test_put_existing_key_updates_value_and_recency():
    c = EmbeddingCache(capacity=2)
    c.put("a", _entry(1))
    c.put("b", _entry(2))
    c.put("a", _entry(99))
    c.put("c", _entry(3))
    a = c.get("a")
    assert a is not None and a.features == {"tag": 99}
    assert c.get("b") is None


def test_clear_resets_state():
    c = EmbeddingCache(capacity=2)
    c.put("a", _entry())
    c.get("a")
    c.clear()
    s = c.stats()
    assert s["size"] == 0 and s["hits"] == 0 and s["misses"] == 0


def test_peek_does_not_change_counters_or_lru_order():
    c = EmbeddingCache(capacity=2)
    c.put("a", _entry(1))
    c.put("b", _entry(2))
    assert c.peek("a") is True
    assert c.peek("missing") is False
    s = c.stats()
    assert s["hits"] == 0 and s["misses"] == 0
    c.put("c", _entry(3))
    assert c.peek("a") is False
    assert c.peek("b") is True
    assert c.peek("c") is True


def test_invalid_capacity():
    with pytest.raises(ValueError):
        EmbeddingCache(capacity=0)
    with pytest.raises(ValueError):
        EmbeddingCache(capacity=-1)


def test_default_cap_32():
    """默认 cap 32, 与 grounded-sam2 默认 16 区分."""
    c = EmbeddingCache()
    assert c.capacity == 32


def test_stats_hit_rate_rounding():
    c = EmbeddingCache(capacity=2)
    c.put("a", _entry())
    for _ in range(7):
        c.get("a")
    for _ in range(3):
        c.get("missing")
    s = c.stats()
    assert s["hits"] == 7 and s["misses"] == 3
    assert s["hit_rate"] == 0.7


def test_concurrent_put_get_lock_safe():
    c = EmbeddingCache(capacity=64)

    def worker(start: int):
        for i in range(start, start + 50):
            c.put(f"k{i}", _entry(i))
            c.get(f"k{i}")

    threads = [threading.Thread(target=worker, args=(i * 50,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = c.stats()
    assert s["size"] <= 64
    assert s["hits"] + s["misses"] == 200
