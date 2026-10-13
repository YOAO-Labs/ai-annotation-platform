"""类别白名单 + classes 暴露.

- ModelPool 在模型 build 后缓存 model.names (逐 task), class_names() 暴露给 /setup。
- Context 接受 classes: list[int] 白名单 (推理层 model.predict(classes=) 过滤)。

不需要 ultralytics / GPU: build 回调 mock 成带 .names 的对象。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_modules() -> None:
    sys.modules.setdefault(
        "torch", MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False)))
    )


def _pool_with_names(names: dict[int, str], cap: int = 2):
    from model_pool import ModelPool

    def _build(task: str, series: str, size: str):
        m = MagicMock()
        m.names = names
        return m

    return ModelPool(
        cap=cap, build_model=_build, free_gpu_memory=lambda: None, build_timeout=5.0
    )


# ── ModelPool.class_names ──────────────────────────────────────────────────


def test_class_names_none_before_any_load() -> None:
    pool = _pool_with_names({0: "person"})
    assert pool.class_names("detection") is None


async def test_class_names_populated_after_borrow_sorted_by_index() -> None:
    pool = _pool_with_names({2: "car", 0: "person", 1: "bicycle"})
    async with pool.borrow("detection", "yolo11", "s"):
        pass
    classes = pool.class_names("detection")
    assert classes == [
        {"index": 0, "name": "person"},
        {"index": 1, "name": "bicycle"},
        {"index": 2, "name": "car"},
    ]


async def test_class_names_populated_after_warmup() -> None:
    pool = _pool_with_names({0: "plane", 1: "ship"})
    await pool.warmup("obb", "yolo11", "s")
    assert pool.class_names("obb") == [
        {"index": 0, "name": "plane"},
        {"index": 1, "name": "ship"},
    ]


async def test_class_names_isolated_per_task() -> None:
    pool = _pool_with_names({0: "person"})
    async with pool.borrow("detection", "yolo11", "s"):
        pass
    # 另一 task 未加载 → None (各 task 独立缓存).
    assert pool.class_names("segmentation") is None


# ── Context.classes schema ─────────────────────────────────────────────────


def test_context_accepts_class_whitelist() -> None:
    from schemas import Context

    ctx = Context(
        type="detection",
        variants={"series": "yolo11", "size": "s"},
        classes=[0, 2],
    )
    assert ctx.classes == [0, 2]


def test_context_classes_defaults_none() -> None:
    from schemas import Context

    ctx = Context(type="detection", variants={"series": "yolo11", "size": "s"})
    assert ctx.classes is None
