"""开集文本推理的 output 分支 (box / mask / both) 单测.

不依赖 ultralytics / GPU: 用 fake pool + fake YOLOE 模型 (带 boxes + masks) 注入,
验证 ``_predict_open_text`` 按 ctx.output 取检测框 / 分割多边形 / 两者。
"""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.modules.setdefault(
    "torch", MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False)))
)

# stub ultralytics yoloe VP predictor 叶子模块 (测试环境无 ultralytics);
# predictor._predict_visual_prompt 内部 lazy `from ...yoloe.predict import YOLOEVPSegPredictor`。
# 仅注册叶子模块: 叶子在 sys.modules 时 import 机制短路, 不触发父包导入, 故不覆盖其他测试
# 对 "ultralytics" 根 (MagicMock) 的桩, 避免跨测试污染。
_vp_mod = types.ModuleType("ultralytics.models.yolo.yoloe.predict")
_vp_mod.YOLOEVPSegPredictor = type("YOLOEVPSegPredictor", (), {})
sys.modules.setdefault("ultralytics.models.yolo.yoloe.predict", _vp_mod)

import predictor as pred  # noqa: E402
from predictor import YoloPredictor  # noqa: E402
from schemas import Context  # noqa: E402


class _FakeBoxes:
    def __init__(self) -> None:
        self.xyxy = np.array([[10.0, 20.0, 110.0, 220.0]])
        self.conf = np.array([0.9])
        self.cls = np.array([0.0])

    def __len__(self) -> int:
        return 1


class _FakeMasks:
    # ultralytics masks.xy: list[ndarray[N,2]] 像素点列 (一个多边形).
    xy = [np.array([[10.0, 20.0], [110.0, 20.0], [110.0, 220.0], [10.0, 220.0]])]


class _FakeResult:
    def __init__(self, with_mask: bool) -> None:
        self.boxes = _FakeBoxes()
        self.masks = _FakeMasks() if with_mask else None
        self.names = {0: "cat"}


class _FakeYoloe:
    """假 YOLOE: set_classes 后 names 映射到文本类名; predict 出一框 + (可选)一 mask.

    记录最近一次 predict 的 kwargs (visual_prompts / refer_image), 供 VP 测试断言。
    """

    def __init__(self, with_mask: bool = True) -> None:
        self._with_mask = with_mask
        self.device = "cpu"
        self.names = {0: "cat"}
        self.last_predict_kw: dict = {}

    def get_text_pe(self, classes: list[str]):
        return "PE"

    def set_classes(self, classes: list[str], pe=None) -> None:
        self.names = {i: c for i, c in enumerate(classes)}

    def predict(self, img, **kw):
        self.last_predict_kw = kw
        self.last_predict_img = img
        return [_FakeResult(self._with_mask)]


class _FakePool:
    def __init__(self, model) -> None:
        self._model = model

    @asynccontextmanager
    async def borrow(self, task: str, series: str, size: str):
        yield SimpleNamespace(
            model=self._model,
            cache_hit=True,
            model_load_ms=None,
        )


@pytest.fixture(autouse=True)
def _fake_image(monkeypatch):
    img = MagicMock()
    img.size = (200, 240)  # (W, H)
    monkeypatch.setattr(pred, "fetch_image", lambda *a, **k: img)
    return img


def _ctx(output: str, series: str = "yoloe-11"):
    return Context.model_validate(
        {
            "type": "text",
            "text": "cat",
            "output": output,
            "model_variants": {"series": series, "size": "s"},
        }
    )


async def test_output_box_emits_only_rectangles() -> None:
    p = YoloPredictor(_FakePool(_FakeYoloe()))
    items, *_ = await p.predict_one("x", _ctx("box"))
    assert [i["type"] for i in items] == ["rectanglelabels"]
    assert items[0]["value"]["rectanglelabels"] == ["cat"]


async def test_output_mask_emits_only_polygons() -> None:
    p = YoloPredictor(_FakePool(_FakeYoloe()))
    items, *_ = await p.predict_one("x", _ctx("mask"))
    assert [i["type"] for i in items] == ["polygonlabels"]
    assert items[0]["value"]["polygonlabels"] == ["cat"]


async def test_output_both_emits_rectangles_and_polygons() -> None:
    p = YoloPredictor(_FakePool(_FakeYoloe()))
    items, *_ = await p.predict_one("x", _ctx("both"))
    assert {i["type"] for i in items} == {"rectanglelabels", "polygonlabels"}


async def test_world_mask_is_rejected() -> None:
    """YOLO-World 没有分割头，mask 请求必须明确返回 422。"""
    p = YoloPredictor(_FakePool(_FakeYoloe(with_mask=False)))
    with pytest.raises(pred.HTTPException) as exc_info:
        await p.predict_one("x", _ctx("mask", series="yolo-worldv2"))
    assert exc_info.value.status_code == 422


async def test_empty_text_returns_empty() -> None:
    p = YoloPredictor(_FakePool(_FakeYoloe()))
    ctx = Context.model_validate(
        {
            "type": "text",
            "text": "",
            "output": "mask",
            "model_variants": {"series": "yoloe-11", "size": "s"},
        }
    )
    items, _hit, _load, infer_ms = await p.predict_one("x", ctx)
    assert items == []
    assert infer_ms == 0


# ── visual prompt exemplar 路径 ──


def _ex_ctx(output: str, exemplars: list[dict], score_threshold=None):
    body = {
        "type": "exemplar",
        "exemplars": exemplars,
        "output": output,
        "model_variants": {"series": "yoloe-11", "size": "s"},
    }
    if score_threshold is not None:
        body["score_threshold"] = score_threshold
    return Context.model_validate(body)


async def test_exemplar_mask_emits_polygons_and_passes_refer() -> None:
    model = _FakeYoloe()
    p = YoloPredictor(_FakePool(model))
    ctx = _ex_ctx("mask", [{"bbox": [0.1, 0.2, 0.3, 0.4], "label": True}])
    items, *_ = await p.predict_one("x", ctx)
    assert [i["type"] for i in items] == ["polygonlabels"]
    # refer_image 必须显式传 (= source 自身); visual_prompts 带 bboxes + cls.
    assert "refer_image" in model.last_predict_kw
    assert model.last_predict_kw["refer_image"] is model.last_predict_img
    vp = model.last_predict_kw["visual_prompts"]
    # 归一化 [0.1,0.2,0.3,0.4] × (W=200,H=240) → 像素 [20,48,60,96].
    assert vp["bboxes"].tolist() == [[20.0, 48.0, 60.0, 96.0]]
    assert vp["cls"].tolist() == [0]  # MVP 单类


async def test_exemplar_both_emits_box_and_polygon() -> None:
    p = YoloPredictor(_FakePool(_FakeYoloe()))
    items, *_ = await p.predict_one("x", _ex_ctx("both", [{"bbox": [0, 0, 0.5, 0.5]}]))
    assert {i["type"] for i in items} == {"rectanglelabels", "polygonlabels"}


async def test_exemplar_filters_negative_boxes() -> None:
    """YOLOE 无负框: label=False 的样例被剔除, 只有负框时不推理返回空。"""
    model = _FakeYoloe()
    p = YoloPredictor(_FakePool(model))
    # 一正一负 → 只保留正框.
    await p.predict_one(
        "x",
        _ex_ctx(
            "box",
            [
                {"bbox": [0, 0, 0.2, 0.2], "label": True},
                {"bbox": [0.5, 0.5, 0.9, 0.9], "label": False},
            ],
        ),
    )
    assert len(model.last_predict_kw["visual_prompts"]["bboxes"]) == 1


async def test_exemplar_only_negative_returns_empty() -> None:
    model = _FakeYoloe()
    p = YoloPredictor(_FakePool(model))
    items, _hit, _load, infer_ms = await p.predict_one(
        "x", _ex_ctx("mask", [{"bbox": [0, 0, 0.2, 0.2], "label": False}])
    )
    assert items == []
    assert infer_ms == 0
    assert model.last_predict_kw == {}  # 未触发 predict


async def test_exemplar_score_threshold_maps_to_conf() -> None:
    model = _FakeYoloe()
    p = YoloPredictor(_FakePool(model))
    await p.predict_one(
        "x", _ex_ctx("box", [{"bbox": [0, 0, 0.2, 0.2]}], score_threshold=0.4)
    )
    assert model.last_predict_kw["conf"] == 0.4


async def test_exemplar_box_coords_are_normalized_0_1() -> None:
    """exemplar 是交互候选 → 坐标须归一化 0-1 (与 sam3/gsam2 一致),
    否则前端浮层 `coord * imgW` 把框画到画布外 (百分比 ×imgW = ×100 飞出)。
    fake box xyxy=[10,20,110,220] / 图 200×240 → x=0.05 w=0.5。"""
    p = YoloPredictor(_FakePool(_FakeYoloe(with_mask=False)))
    items, *_ = await p.predict_one("x", _ex_ctx("box", [{"bbox": [0, 0, 0.2, 0.2]}]))
    v = items[0]["value"]
    assert v["x"] == pytest.approx(0.05)
    assert v["y"] == pytest.approx(20 / 240)
    assert v["width"] == pytest.approx(0.5)
    assert v["height"] == pytest.approx(200 / 240)


async def test_exemplar_polygon_coords_are_normalized_0_1() -> None:
    """exemplar mask 多边形同样归一化 0-1。fake poly 角点 (10,20) / 200×240 → (0.05, 0.0833)。"""
    p = YoloPredictor(_FakePool(_FakeYoloe(with_mask=True)))
    items, *_ = await p.predict_one("x", _ex_ctx("mask", [{"bbox": [0, 0, 0.2, 0.2]}]))
    pts = items[0]["value"]["points"]
    assert pts[0][0] == pytest.approx(0.05)
    assert pts[0][1] == pytest.approx(20 / 240)


async def test_text_batch_box_coords_stay_percent() -> None:
    """对照: 文本批量路径走入库, 坐标保持 Label Studio 百分比 (0-100), 不受 exemplar 归一化影响。
    同一 fake box → x=5.0 w=50.0。"""
    p = YoloPredictor(_FakePool(_FakeYoloe(with_mask=False)))
    items, *_ = await p.predict_one("x", _ctx("box"))
    v = items[0]["value"]
    assert v["x"] == pytest.approx(5.0)
    assert v["width"] == pytest.approx(50.0)
