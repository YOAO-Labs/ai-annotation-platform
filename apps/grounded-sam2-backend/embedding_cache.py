"""SAM 2 image embedding LRU 缓存.

工作台同图连续点击 / 拖框是交互式精修的典型动作; SAM 2 `set_image()`
计算 image embedding 是 ~1.5s 的大头, 缓存后 2-N 次操作能降到 < 50ms.

设计要点:
  - Key 由调用方算好(`compute_cache_key()`); 内部不做任何 path/url 解析.
  - Value 直接持有 SAM2ImagePredictor 内部状态 (`_features` / `_orig_hw`)
    + 平台需要的 `(w,h)`. tensor 原地引用, 不 deepcopy(GPU 拷贝代价大;
    LRU 容量 = 内存上限的物理保证).
  - vendor 升级提醒: predictor 内部属性名 (`_features`/`_orig_hw`/`_is_image_set`/
    `_is_batch`) 跟随 IDEA-Research/Grounded-SAM-2 commit 走; sync_vendor.sh
    每次必须人肉跑一次 5-clicks 集成验收.
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
    """一条缓存的 SAM 2 image embedding 状态快照."""

    features: dict[str, Any]
    orig_hw: tuple[int, int]
    is_batch: bool
    wh: tuple[int, int]


def compute_cache_key(file_path: str, sam_variant: str) -> str:
    """`sha1(url_path|variant)`.

    HTTP/HTTPS URL 剥掉 query string (MinIO presigned signature TTL 滚动会让原串变, 但底层对象不变);
    本地路径直接用原串.
    """
    if file_path.startswith(("http://", "https://")):
        parts = urlsplit(file_path)
        identity = f"{parts.scheme}://{parts.netloc}{parts.path}"
    else:
        identity = file_path
    payload = f"{identity}|{sam_variant}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


class EmbeddingCache:
    """线程安全 LRU. FastAPI 单 worker 也加锁, 给后续可能扩并发的 M2 留好.

    Stats 字段按 prompt_type 分桶在 observability 层, 这里只给整体 size/hits/misses.
    """

    def __init__(self, capacity: int = 16, sam_variant: str = "tiny") -> None:
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
        """检查 key 是否存在, 不动 LRU 顺序也不计入 hits/misses.

        给 main.py 的 \"是否需要拉图\" 决策用; 真正的 hit/miss 计数走 get().
        """
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
