"""可选的 pytest 测试分片：``--shard-index`` / ``--shard-count``。

分区规则：

- 分区键 = test item 的 nodeid 中第一个 ``::`` 之前的部分，即模块路径
  （例如 ``tests/test_foo.py``）。同一模块的所有测试必然落在同一分片。
- 默认按 sha256(key) 的前八个字节取模。提供 ``--shard-plan`` 时，只覆盖
  历史计划中明确分配的模块。其他模块仍按相同的哈希分区。
- 两种方式都不依赖 PYTHONHASHSEED、收集顺序或本次选集，分片内保持原顺序。
- 通过 ``pytest_ignore_collect`` 在收集前跳过其他分片模块，避免无谓导入；
  ``pytest_collection_modifyitems`` 保留为兜底过滤。

两个参数要么都不给（不分片，行为完全不变），要么同时给出。范围非法时抛
``pytest.UsageError``（exit code 4），在任何测试执行前失败；分片为空时不做任何
"空成功"兜底，交给 pytest 默认的 no-tests-collected 失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

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


def _read_plan(path: Path, count: int) -> dict[str, int]:
    try:
        groups = json.loads(path.read_text(encoding="utf-8"))["shards"]
        if not isinstance(groups, list) or len(groups) != count:
            raise ValueError(f"expected {count} shard groups")
        assigned = {}
        for index, group in enumerate(groups):
            if not isinstance(group, list):
                raise ValueError(f"shard {index} must list module paths")
            for module in group:
                if not isinstance(module, str) or module in assigned:
                    raise ValueError(f"invalid or repeated module: {module!r}")
                assigned[module] = index
        return assigned
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise pytest.UsageError(f"--shard-plan {path}: {exc}") from exc


def partition_items(items, selection: ShardSelection, assigned=None):
    """按当前分片拆分 items，保持各自相对顺序：返回 ``(kept, deselected)``。"""
    kept = []
    deselected = []
    modules = assigned.copy() if assigned else {}
    for item in items:
        module = shard_key(item.nodeid)
        bucket = modules.get(module)
        if bucket is None:
            bucket = modules[module] = shard_index_for_nodeid(module, selection.count)
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
    group.addoption(
        "--shard-plan",
        type=Path,
        default=None,
        metavar="JSON",
        help="从历史 JUnit 生成的模块分配表，需与分片总数一致。",
    )


def _selection_from_config(config: pytest.Config) -> ShardSelection | None:
    return parse_shard_selection(
        config.getoption("shard_index"), config.getoption("shard_count")
    )


def _shard_state(
    config: pytest.Config,
) -> tuple[ShardSelection | None, dict[str, int], Path]:
    state = getattr(config, "_yoao_shard_state", None)
    if state is None:
        selection = _selection_from_config(config)
        path = config.getoption("shard_plan")
        if path is not None and selection is None:
            raise pytest.UsageError(
                "--shard-plan 需要同时提供 --shard-index 和 --shard-count"
            )
        assigned = (
            _read_plan(path, selection.count)
            if path is not None and selection is not None
            else {}
        )
        state = (selection, assigned, Path(config.rootpath).resolve())
        config._yoao_shard_state = state
    return state


def pytest_configure(config: pytest.Config) -> None:
    _shard_state(config)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    selection, assigned, rootpath = _shard_state(config)
    if selection is None or selection.count == 1:
        return None
    path = Path(collection_path)
    name = path.name
    if (
        path.suffix != ".py"
        or name in {"conftest.py", "__init__.py"}
        or not (name.startswith("test_") or name.endswith("_test.py"))
        or not path.is_file()
    ):
        return None
    try:
        module = path.resolve().relative_to(rootpath).as_posix()
    except ValueError:
        return None
    bucket = assigned.get(module)
    if bucket is None:
        bucket = shard_index_for_nodeid(module, selection.count)
    return True if bucket != selection.index else None


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    selection, assigned, _ = _shard_state(config)
    if selection is None or selection.count == 1:
        return
    kept, deselected = partition_items(items, selection, assigned)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
