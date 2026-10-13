"""main._coerce_exemplars 路由归一 + /setup exemplar_capabilities 单测.

无 GPU: mock 掉 sam3 / torch 依赖, 只测纯路由归一逻辑与 setup 声明。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def main_mod():
    fake_sam3_mod = types.ModuleType("sam3")
    fake_sam3_mod.build_sam3_image_model = MagicMock(return_value=MagicMock())
    sys.modules.setdefault("sam3", fake_sam3_mod)
    sys.modules.pop("main", None)
    import main as m  # noqa: PLC0415

    return m


def test_coerce_multi_box(main_mod):
    out = main_mod._coerce_exemplars(
        {
            "exemplars": [
                {"bbox": [0.1, 0.1, 0.2, 0.2], "label": True},
                {"bbox": [0.5, 0.5, 0.6, 0.6], "label": False},
                {"bbox": [0.7, 0.7, 0.8, 0.8]},
            ]
        }
    )
    assert [e["label"] for e in out] == [True, False, True]
    assert out[0]["bbox"] == [0.1, 0.1, 0.2, 0.2]


def test_coerce_falls_back_to_single_bbox(main_mod):
    """无 exemplars 时退化单 bbox 正框 (旧路径)。"""
    out = main_mod._coerce_exemplars({"bbox": [0.2, 0.2, 0.45, 0.55]})
    assert out == [{"bbox": [0.2, 0.2, 0.45, 0.55], "label": True}]


def test_coerce_empty_exemplars_falls_back_to_bbox(main_mod):
    out = main_mod._coerce_exemplars({"exemplars": [], "bbox": [0.0, 0.0, 1.0, 1.0]})
    assert out == [{"bbox": [0.0, 0.0, 1.0, 1.0], "label": True}]


def test_coerce_missing_both_raises(main_mod):
    from fastapi import HTTPException  # noqa: PLC0415

    with pytest.raises(HTTPException) as exc:
        main_mod._coerce_exemplars({})
    assert exc.value.status_code == 422


def test_coerce_bad_item_bbox_raises(main_mod):
    from fastapi import HTTPException  # noqa: PLC0415

    with pytest.raises(HTTPException) as exc:
        main_mod._coerce_exemplars({"exemplars": [{"bbox": [0.1, 0.2, 0.3]}]})
    assert exc.value.status_code == 422


def test_setup_advertises_exemplar_capabilities(main_mod):
    data = main_mod.setup()
    caps = data["exemplar_capabilities"]
    assert caps["multi_box"] is True
    assert caps["negative_box"] is True
    assert caps["text_combination"] is True
    assert caps["threshold_refilter"] is True


@pytest.mark.parametrize("geometry", ["polygon", "mask"])
@pytest.mark.parametrize("accepts_boxes", [None, False, "true", True])
def test_native_exemplar_pairs_require_explicit_platform_support(
    main_mod, geometry, accepts_boxes
):
    context = {"type": "exemplar", "output": "both", "output_geometry": geometry}
    if accepts_boxes is not None:
        context["native_mask_companion_boxes"] = accepts_boxes
    expected = "both" if geometry == "polygon" or accepts_boxes is True else "mask"
    assert main_mod._coerce_output(context) == expected
