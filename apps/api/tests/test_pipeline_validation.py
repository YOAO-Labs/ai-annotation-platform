"""能力判据跨端契约 (后端侧) + pipeline_validation 纯函数单测。

与前端 vitest (apps/web/src/pages/AIPreAnnotate/utils/capabilityContract.test.ts) 共读同一份
fixture (apps/web/src/__fixtures__/capability-validation-cases.json)。本测喂
check_capability_violations 断言违例码序列与 expect_codes 一致。后端判据漂移 → 本测红。
"""

import json
from pathlib import Path

import pytest

from app.services.pipeline_validation import (
    check_capability_violations,
    check_parent_geometry_roi,
    normalize_geometry_outputs,
    resolve_preannotate_queue,
)

_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "apps/web/src/__fixtures__/capability-validation-cases.json"
)


def _cases():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data["cases"]


def test_fixture_exists_and_nonempty():
    assert _FIXTURE.exists(), f"契约 fixture 缺失: {_FIXTURE}"
    assert len(_cases()) > 0


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_capability_contract(case):
    violations = check_capability_violations(
        case["model_caps"],
        where="stage 1 ",
        model_id="m",
        writes_attributes=case["writes_attributes"],
    )
    codes = [v.code for v in violations]
    assert codes == case["expect_codes"]


@pytest.mark.parametrize(
    "devices, expected",
    [
        (["cpu"], "ml.cpu"),
        (["cpu", "cpu"], "ml.cpu"),
        (["CPU", "cpu"], "ml.cpu"),
        (["gpu"], "ml"),
        (["gpu", "cpu"], "ml"),
        ([None], "ml"),
        (["cpu", None], "ml"),
        ([], "ml"),
    ],
)
def test_resolve_preannotate_queue(devices, expected):
    assert (
        resolve_preannotate_queue(devices, gpu_queue="ml", cpu_queue="ml.cpu")
        == expected
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        (["bbox", "polygon"], ["bbox", "polygon"]),
        (["box"], ["bbox"]),
        (["mask"], ["polygon"]),
        (["both"], ["bbox", "polygon"]),
        (["BBox", " polygon "], ["bbox", "polygon"]),
        ([], []),
        (None, []),
        ([123, "keypoint"], ["keypoint"]),
    ],
)
def test_normalize_geometry_outputs(raw, expected):
    caps = {} if raw is None else {"supported_geometric_outputs": raw}
    assert normalize_geometry_outputs(caps) == expected


@pytest.mark.parametrize(
    "geo, expect_violation",
    [
        (["bbox"], False),
        (["polygon"], False),
        (["raster_mask"], False),
        (["bbox", "keypoint"], False),
        (["mask"], False),
        (["both"], False),
        (None, False),
        ([], False),
        (["keypoint"], True),
        (["polyline"], True),
        (["rotated_bbox"], True),
        (["keypoint", "polyline"], True),
    ],
)
def test_check_parent_geometry_roi(geo, expect_violation):
    caps = {} if geo is None else {"supported_geometric_outputs": geo}
    violations = check_parent_geometry_roi(
        caps, where="stage 1 ", parent_model_id="src"
    )
    if expect_violation:
        assert len(violations) == 1
        assert violations[0].code == "no_roi_geometry"
        assert "stage 1 " in violations[0].detail
        assert "'src'" in violations[0].detail
    else:
        assert violations == []


def test_violation_detail_carries_context():
    """detail 成句带 where 前缀 + model_id (派发期 422 文案不漂移)。"""
    [v] = check_capability_violations(
        {"resource_profile": {"batchable": False}},
        where="源阶段",
        model_id="seg-x",
        writes_attributes=False,
    )
    assert v.code == "not_batchable"
    assert "源阶段" in v.detail
    assert "batchable=false" in v.detail
    assert "'seg-x'" in v.detail
