"""SAM 3 image embedding LRU 缓存.

直接镜像 grounded-sam2-backend 的 EmbeddingCache 设计 (cap 32, LRU, 线程安全):
SAM 3 单次 image encoder 前向 ~ 1-2s (3090 / A100), 缓存命中后同图二次 prompt
跳过 encoder, 与 sam2 缓存机制语义等价.

Key 设计差异: variant 标签固定为 "sam3" (图像模型即 facebook/sam3 单档, 无 tiny/large 之分),
但仍保留 sam_variant 字段以便未来量化版本接入时分桶 (e.g. "sam3-int8").
sam2 缓存与 sam3 缓存互不共享 (embedding 来自不同模型, 不能跨), 由 cache_key 包含 variant
天然隔离.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass
class CacheEntry:
    """一条缓存的 SAM 3 image embedding 状态快照.

    `features` 持有 Sam3Processor.set_image() 写到 state["backbone_out"] 的字典:
    包含 vision tower features + (若 inst_interactivity 开启) sam2_backbone_out 等.
    其内部张量在 GPU 上, 不做深拷贝 (LRU 容量是显存上限的物理保证).

    `orig_hw = (original_height, original_width)`, `wh = (image.width, image.height)`,
    两者冗余但分别对应 _forward_grounding 用的 height/width 和我们 polygon 归一化用的 (w, h).

    `is_batch` 给未来 set_image_batch 留位; 当前不用.

    sync_vendor.sh 升级 commit 时务必跑端到端验收, 确认 Sam3Processor.set_image() 写到 state
    的 keys (`backbone_out`, `original_height`, `original_width`) 仍然一致.
    """

    features: dict[str, Any]
    orig_hw: tuple[int, int]
    is_batch: bool
    wh: tuple[int, int]


def compute_cache_key(file_path: str, sam_variant: str) -> str:
    """`sha1(url_path|variant)`.

    HTTP/HTTPS URL 剥掉 query string (MinIO presigned signature TTL 滚动会让原串变,
    但底层对象不变); 本地路径直接用原串.
    """
    if file_path.startswith(("http://", "https://")):
        parts = urlsplit(file_path)
        identity = f"{parts.scheme}://{parts.netloc}{parts.path}"
    else:
        identity = file_path
    payload = f"{identity}|{sam_variant}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


class EmbeddingCache:
    """线程安全 LRU. FastAPI 单 worker 也加锁, 与 grounded-sam2-backend 实现完全一致."""

    def __init__(self, capacity: int = 32, sam_variant: str = "sam3") -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._sam_variant = sam_variant
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def sam_variant(self) -> str:
        return self._sam_variant

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return entry

    def peek(self, key: str) -> bool:
        """检查 key 是否存在, 不动 LRU 顺序也不计入 hits/misses."""
        with self._lock:
            return key in self._store

    def put(self, key: str, entry: CacheEntry) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = entry
                return
            self._store[key] = entry
            if len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total else 0.0
            return {
                "size": len(self._store),
                "capacity": self._capacity,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "variant": self._sam_variant,
            }

    def size(self) -> int:
        with self._lock:
            return len(self._store)
