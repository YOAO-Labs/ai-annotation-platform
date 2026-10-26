"""可选的 pytest 测试分片：``--shard-index`` / ``--shard-count``。

分区规则（CI 契约，算法本身是稳定接口，改动会重新分配全部测试）：

- 分区键 = test item 的 nodeid 中第一个 ``::`` 之前的部分，即模块路径
  （例如 ``tests/test_foo.py``）。同一模块的所有测试必然落在同一分片。
- 分片 = ``int.from_bytes(sha256(key.encode()).digest()[:8], "big") % count``。
  sha256 与 PYTHONHASHSEED 无关，因此分片在任意 hash seed / 收集顺序下稳定。
- 每个分片内部保持 pytest 原有的收集顺序。

两个参数要么都不给（不分片，行为完全不变），要么同时给出。范围非法时抛
``pytest.UsageError``（exit code 4），在任何测试执行前失败；分片为空时不做任何
"空成功"兜底，交给 pytest 默认的 no-tests-collected 失败。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class ShardSelection:
    """已校验的分片范围。"""

    index: int
    count: int


def parse_shard_selection(
    index: int | None, count: int | None
) -> ShardSelection | None:
    """校验 CLI 分片参数；都不给返回 None，非法组合抛 UsageError。"""
    if index is None and count is None:
        return None
    if index is None or count is None:
        raise pytest.UsageError(
            "--shard-index 与 --shard-count 必须同时提供（分片需要完整的范围）。"
        )
    if count <= 0:
        raise pytest.UsageError(f"--shard-count 必须是正整数，得到 {count}。")
    if not 0 <= index < count:
        raise pytest.UsageError(
            f"--shard-index 必须满足 0 <= index < count，得到 "
            f"index={index}, count={count}。"
        )
    return ShardSelection(index=index, count=count)


def shard_key(nodeid: str) -> str:
    """分区键：nodeid 中第一个 ``::`` 之前的模块路径。"""
    return nodeid.split("::", 1)[0]


def shard_index_for_nodeid(nodeid: str, count: int) -> int:
    """nodeid 所属分片下标；count 由调用方保证为正。"""
    digest = hashlib.sha256(shard_key(nodeid).encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


def partition_items(items, selection: ShardSelection):
    """按当前分片拆分 items，保持各自相对顺序：返回 ``(kept, deselected)``。"""
    kept = []
    deselected = []
    for item in items:
        bucket = shard_index_for_nodeid(item.nodeid, selection.count)
        (kept if bucket == selection.index else deselected).append(item)
    return kept, deselected


def pytest_addoption(parser):
    group = parser.getgroup("sharding", "test sharding")
    group.addoption(
        "--shard-index",
        action="store",
        type=int,
        default=None,
        metavar="INT",
        help="当前分片下标（0 <= index < count）；必须与 --shard-count 同时使用。",
    )
    group.addoption(
        "--shard-count",
        action="store",
        type=int,
        default=None,
        metavar="INT",
        help="分片总数（正整数）；必须与 --shard-index 同时使用。",
    )


def _selection_from_config(config: pytest.Config) -> ShardSelection | None:
    return parse_shard_selection(
        config.getoption("shard_index"), config.getoption("shard_count")
    )


def pytest_configure(config: pytest.Config) -> None:
    # 尽早校验：非法范围在收集开始前就以 UsageError 失败。
    _selection_from_config(config)


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    selection = _selection_from_config(config)
    if selection is None or selection.count == 1:
        # 未分片，或 count=1/index=0：保留全部，不报告 deselected。
        return
    kept, deselected = partition_items(items, selection)
    if deselected:
        # 让 pytest 的输出/统计如实反映被分片排除的用例（计数准确，不静默丢弃）。
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
