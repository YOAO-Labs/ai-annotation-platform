"""``--shard-index`` / ``--shard-count`` 的行为回归。

覆盖两类可观察契约：

1. 纯分区函数：并集覆盖全部用例、分片间互斥、保持原顺序、同模块同分片、
   范围校验失败，以及 ``count=1/index=0`` 保留全部。
2. 真实 pytest 子进程：用最小测试目录 + 本仓库的分片插件跑分片，确认被排除的
   用例计入 deselected、各分片并集等于全集、且分片结果不受 PYTHONHASHSEED 影响。

已知答案（KAT）固定了 sha256 分区算法本身，防止实现被"自洽"地改动。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import pytest_sharding

API_ROOT = Path(__file__).resolve().parents[1]

MINI_MODULE_COUNT = 12
MINI_TESTS_PER_MODULE = 2
MINI_TOTAL = MINI_MODULE_COUNT * MINI_TESTS_PER_MODULE


def _corpus() -> list[str]:
    """40 个模块 × 5 个用例；同模块的多级 nodeid 也覆盖到。"""
    nodeids = [f"tests/test_mod_{m:02d}.py::test_case_{t}" for m in range(40) for t in range(5)]
    nodeids.append("tests/test_mod_00.py::TestGroup::test_method")
    return nodeids


def _items(nodeids: list[str]) -> list[SimpleNamespace]:
    return [SimpleNamespace(nodeid=nodeid) for nodeid in nodeids]


def _shard_nodeids(nodeids: list[str], selection: pytest_sharding.ShardSelection) -> list[str]:
    kept, _ = pytest_sharding.partition_items(_items(nodeids), selection)
    return [item.nodeid for item in kept]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_every_test_lands_in_exactly_one_shard_with_original_order(count: int) -> None:
    nodeids = _corpus()
    shards = [
        _shard_nodeids(nodeids, pytest_sharding.ShardSelection(index=index, count=count))
        for index in range(count)
    ]

    union = [nodeid for shard in shards for nodeid in shard]
    assert sorted(union) == sorted(nodeids)
    assert len(union) == len(set(union))  # 互斥：没有重复项
    for shard in shards:
        # 分片内部保持收集顺序（相对顺序与原序列一致）
        assert shard == [nodeid for nodeid in nodeids if nodeid in set(shard)]


@pytest.mark.parametrize("count", [2, 3, 4, 7])
def test_module_is_never_split_across_shards(count: int) -> None:
    modules: dict[str, set[int]] = {}
    for nodeid in _corpus():
        modules.setdefault(pytest_sharding.shard_key(nodeid), set()).add(
            pytest_sharding.shard_index_for_nodeid(nodeid, count)
        )
    assert all(len(buckets) == 1 for buckets in modules.values())


def test_assignment_does_not_depend_on_collection_order() -> None:
    nodeids = _corpus()
    for index in range(3):
        selection = pytest_sharding.ShardSelection(index=index, count=3)
        forward = _shard_nodeids(nodeids, selection)
        reverse = _shard_nodeids(list(reversed(nodeids)), selection)
        # 逆序收集后每个分片仍是同一集合，且分片内顺序随收集顺序反转。
        assert sorted(reverse) == sorted(forward)
        assert reverse == list(reversed(forward))


@pytest.mark.parametrize(
    ("nodeid", "count", "expected"),
    [
        ("tests/test_foo.py::test_anything", 2, 1),
        ("tests/test_foo.py::test_anything", 3, 0),
        ("tests/test_foo.py::test_anything", 4, 1),
        ("tests/test_shard_00.py::test_x", 4, 1),
        ("tests/test_bar.py::TestCls::test_m", 4, 0),
    ],
)
def test_partition_algorithm_known_answers(nodeid: str, count: int, expected: int) -> None:
    assert pytest_sharding.shard_index_for_nodeid(nodeid, count) == expected
    # 显式重复公式，证明 KAT 不是从实现里抄来的常量。
    key = nodeid.split("::", 1)[0]
    assert expected == int.from_bytes(
        hashlib.sha256(key.encode()).digest()[:8], "big"
    ) % count


def test_both_flags_absent_means_no_sharding() -> None:
    assert pytest_sharding.parse_shard_selection(None, None) is None


@pytest.mark.parametrize(("index", "count"), [(None, 2), (0, None)])
def test_half_specified_range_is_rejected(index, count) -> None:
    with pytest.raises(pytest.UsageError):
        pytest_sharding.parse_shard_selection(index, count)


@pytest.mark.parametrize(
    ("index", "count"),
    [(0, 0), (0, -1), (2, 2), (3, 2), (-1, 2), (-4, 3)],
)
def test_out_of_range_selection_is_rejected(index: int, count: int) -> None:
    with pytest.raises(pytest.UsageError):
        pytest_sharding.parse_shard_selection(index, count)


def test_count_one_index_zero_keeps_every_test() -> None:
    nodeids = _corpus()
    selection = pytest_sharding.parse_shard_selection(0, 1)
    assert selection is not None
    kept, deselected = pytest_sharding.partition_items(_items(nodeids), selection)
    assert [item.nodeid for item in kept] == nodeids
    assert deselected == []


# ── 真实 pytest 子进程 ────────────────────────────────────────────────


@pytest.fixture
def mini_suite(tmp_path: Path) -> Path:
    # 固定 rootdir，保证子进程里的 nodeid 是 test_shard_XX.py::...（分片键稳定）。
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for module in range(MINI_MODULE_COUNT):
        body = "".join(
            f"def test_case_{case}():\n    assert True\n\n"
            for case in range(MINI_TESTS_PER_MODULE)
        )
        (tmp_path / f"test_shard_{module:02d}.py").write_text(body, encoding="utf-8")
    return tmp_path


def _run_sharded_pytest(
    suite_dir: Path,
    *args: str,
    hash_seed: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTHONHASHSEED",
            # 父进程的 coverage 子进程配置不应影响最小测试目录。
            "COVERAGE_PROCESS_START",
            "COVERAGE_FILE",
            "COV_CORE_SOURCE",
            "COV_CORE_CONFIG",
            "COV_CORE_DATAFILE",
        }
    }
    # 只用本仓库的插件，不加载环境里已有的第三方 pytest 插件，保证行为可复现。
    env["PYTHONPATH"] = str(API_ROOT)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if hash_seed is not None:
        env["PYTHONHASHSEED"] = hash_seed
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.pytest_sharding",
            "-v",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=suite_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _passed_nodeids(result: subprocess.CompletedProcess[str]) -> set[str]:
    return set(re.findall(r"^(\S+::\S+) PASSED", result.stdout, flags=re.MULTILINE))


def test_real_pytest_shards_are_disjoint_and_cover_every_test(mini_suite: Path) -> None:
    runs = [
        _run_sharded_pytest(mini_suite, "--shard-index", str(index), "--shard-count", "3")
        for index in range(3)
    ]
    for index, result in enumerate(runs):
        assert result.returncode == 0, f"shard {index} failed:\n{result.stdout}\n{result.stderr}"
        assert "deselected" in result.stdout  # 被排除的用例如实计入统计
        passed = len(_passed_nodeids(result))
        assert f"{MINI_TOTAL - passed} deselected" in result.stdout
        assert passed > 0

    selected: set[str] = set()
    for result in runs:
        passed = _passed_nodeids(result)
        assert selected.isdisjoint(passed)
        selected |= passed
    assert len(selected) == MINI_TOTAL


def test_real_pytest_shard_is_stable_across_python_hash_seeds(mini_suite: Path) -> None:
    first = _run_sharded_pytest(
        mini_suite, "--shard-index", "1", "--shard-count", "3", hash_seed="0"
    )
    second = _run_sharded_pytest(
        mini_suite, "--shard-index", "1", "--shard-count", "3", hash_seed="123456"
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert _passed_nodeids(first) == _passed_nodeids(second) != set()


@pytest.mark.parametrize(
    "args",
    [
        ("--shard-count", "2"),
        ("--shard-index", "2", "--shard-count", "2"),
    ],
)
def test_real_pytest_rejects_invalid_range_before_running(mini_suite: Path, args) -> None:
    result = _run_sharded_pytest(mini_suite, *args)
    assert result.returncode == 4, f"expected UsageError exit code:\n{result.stdout}\n{result.stderr}"
    assert "--shard" in result.stderr
    assert "PASSED" not in result.stdout  # 任何用例都没有执行


def test_real_pytest_without_flags_keeps_all_tests(mini_suite: Path) -> None:
    result = _run_sharded_pytest(mini_suite)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deselected" not in result.stdout
    assert len(_passed_nodeids(result)) == MINI_TOTAL
