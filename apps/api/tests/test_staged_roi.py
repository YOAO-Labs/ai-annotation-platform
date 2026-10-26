"""v0.18.1 · 多阶段预标注 ROI 裁剪 + 属性回写纯函数单测 (路径 B M1).

- crop_inputs_from_boxes: 按 bbox 百分比裁 crop, pad 外扩, 跳过非 bbox / 旋转框。
- merge_classify_attributes: 下游分类结果 attributes 按 id 回写到对应父框 (union)。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import pytest
from PIL import Image
from aap_inference_contract.execution import ModelTaskResult

from aap_inference.roi import (
    collect_geometry_shapes,
    compose_transforms,
    crop_inputs_from_boxes,
    geometry_prompts_from_boxes,
    merge_classify_attributes,
    remap_geometry_to_image,
)


def _img(w: int, h: int) -> Image.Image:
    return Image.new("RGB", (w, h), (123, 200, 50))


def _bbox(x, y, w, h, cls="car"):
    return {
        "type": "rectanglelabels",
        "value": {"x": x, "y": y, "width": w, "height": h, "rectanglelabels": [cls]},
    }


def _decode(data_uri: str) -> Image.Image:
    assert data_uri.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    return Image.open(io.BytesIO(raw))


def test_crop_basic_pixel_mapping_and_pad():
    img = _img(1000, 500)
    # 框居中: 占图 20%x20%, 即 200x100 px, 左上 (400,200)
    boxes = [_bbox(40, 40, 20, 20)]
    batch = crop_inputs_from_boxes(img, boxes, pad=0.0)
    assert len(batch.inputs) == 1
    assert batch.skipped_geometry == 0
    assert batch.inputs[0]["id"] == "0"
    crop = _decode(batch.inputs[0]["file_path"])
    assert crop.size == (200, 100)
    # pad 5% → 各边外扩 framaw*0.05=10px / frah*0.05=5px → 220x110
    batch_pad = crop_inputs_from_boxes(img, boxes, pad=0.05)
    crop_pad = _decode(batch_pad.inputs[0]["file_path"])
    assert crop_pad.size == (220, 110)


def test_crop_clamps_to_image_bounds():
    img = _img(100, 100)
    # 贴左上角的框, pad 会越界 → clamp 到 0
    boxes = [_bbox(0, 0, 50, 50)]
    batch = crop_inputs_from_boxes(img, boxes, pad=0.2)
    crop = _decode(batch.inputs[0]["file_path"])
    # 左/上 clamp 到 0, 右/下 外扩 50*0.2=10 → 60
    assert crop.size == (60, 60)


def test_crop_skips_non_bbox_and_rotated():
    img = _img(200, 200)
    boxes = [
        {"type": "polygonlabels", "value": {"points": [[0, 0], [1, 1]]}},
        {
            "type": "rectanglelabels",
            "value": {"x": 10, "y": 10, "width": 20, "height": 20, "rotation": 30},
        },
        _bbox(10, 10, 20, 20),  # idx 2: 唯一有效
    ]
    batch = crop_inputs_from_boxes(img, boxes, pad=0.0)
    assert len(batch.inputs) == 1
    assert batch.inputs[0]["id"] == "2"  # id 保留原下标, 供回写
    # 多边形 + 旋转框 → 几何不支持, 计入 skipped_geometry
    assert batch.skipped_geometry == 2


def test_crop_parent_class_filter_preserves_original_index():
    img = _img(200, 200)
    boxes = [_bbox(10, 10, 20, 20, cls="car"), _bbox(50, 50, 10, 10, cls="person")]
    batch = crop_inputs_from_boxes(img, boxes, pad=0.0, parent_class_filter=["person"])
    # 只裁 person (idx1), id 保留原下标 "1"
    assert len(batch.inputs) == 1
    assert batch.inputs[0]["id"] == "1"
    # car 被类别路由跳过 (非几何), 不计入 skipped_geometry
    assert batch.skipped_geometry == 0


def test_crop_presigned_delivery_uses_upload_fn():
    img = _img(200, 200)
    boxes = [_bbox(10, 10, 20, 20), _bbox(50, 50, 10, 10)]
    calls: list[int] = []

    def fake_upload(box_idx: int, jpeg_bytes: bytes) -> str:
        calls.append(box_idx)
        assert jpeg_bytes[:2] == b"\xff\xd8"  # JPEG SOI
        return f"http://store/crop-{box_idx}.jpg"

    batch = crop_inputs_from_boxes(
        img, boxes, pad=0.0, delivery="presigned", upload_fn=fake_upload
    )
    assert calls == [0, 1]
    assert batch.inputs[0]["file_path"] == "http://store/crop-0.jpg"
    assert batch.inputs[1]["file_path"] == "http://store/crop-1.jpg"


def test_crop_cache_reuses_across_sibling_stages():
    img = _img(200, 200)
    boxes = [_bbox(10, 10, 20, 20), _bbox(50, 50, 10, 10)]
    calls: list[int] = []

    def fake_upload(box_idx: int, jpeg_bytes: bytes) -> str:
        calls.append(box_idx)
        return f"http://store/crop-{box_idx}.jpg"

    cache: dict = {}
    # 两个并行兄弟阶段 target 同一批父框 (同 pad) → 第二次全部命中缓存, 不重复上传/编码。
    crop_inputs_from_boxes(
        img, boxes, pad=0.0, delivery="presigned", upload_fn=fake_upload, cache=cache
    )
    batch2 = crop_inputs_from_boxes(
        img, boxes, pad=0.0, delivery="presigned", upload_fn=fake_upload, cache=cache
    )
    # 上传/编码只发生一次 (2 个框各一次), 第二次零上传
    assert calls == [0, 1]
    assert batch2.inputs[0]["file_path"] == "http://store/crop-0.jpg"


def test_crop_presigned_without_upload_fn_raises():
    import pytest

    img = _img(100, 100)
    boxes = [_bbox(10, 10, 20, 20)]
    with pytest.raises(ValueError):
        crop_inputs_from_boxes(img, boxes, delivery="presigned")


@dataclass
class _FakeResult:
    task_id: str
    result: list


def test_merge_attributes_union_by_id():
    boxes = [_bbox(0, 0, 10, 10), _bbox(20, 20, 10, 10)]
    # 下游对 box0 返回 color, box1 返回 color+vehicle_type
    classify = [
        _FakeResult(
            "0",
            [
                {
                    "type": "rectanglelabels",
                    "score": 0.9,
                    "attributes": {"color": "blue"},
                }
            ],
        ),
        _FakeResult(
            "1",
            [
                {
                    "type": "rectanglelabels",
                    "score": 0.8,
                    "attributes": {"color": "red", "vehicle_type": "bus"},
                }
            ],
        ),
    ]
    n = merge_classify_attributes(boxes, classify)
    assert n == 2
    assert boxes[0]["attributes"] == {"color": "blue"}
    assert boxes[1]["attributes"] == {"color": "red", "vehicle_type": "bus"}


def test_merge_picks_highest_score_and_respects_write_keys():
    boxes = [_bbox(0, 0, 10, 10)]
    classify = [
        _FakeResult(
            "0",
            [
                {"score": 0.3, "attributes": {"color": "black", "vehicle_type": "car"}},
                {
                    "score": 0.95,
                    "attributes": {"color": "white", "vehicle_type": "truck"},
                },
            ],
        )
    ]
    merge_classify_attributes(boxes, classify, write_keys=["color"])
    # 取最高分项 (white/truck), 但 write_keys 只留 color
    assert boxes[0]["attributes"] == {"color": "white"}


def test_merge_preserves_existing_attributes():
    boxes = [
        {
            "type": "rectanglelabels",
            "value": {"x": 0, "y": 0, "width": 10, "height": 10},
            "attributes": {"existing": "keep"},
        }
    ]
    classify = [_FakeResult("0", [{"score": 0.9, "attributes": {"color": "blue"}}])]
    merge_classify_attributes(boxes, classify)
    assert boxes[0]["attributes"] == {"existing": "keep", "color": "blue"}


def test_merge_classification_from_frozen_model_results():
    boxes = [_bbox(0, 0, 10, 10)]
    result = ModelTaskResult(
        task_id="0",
        result=({"score": 0.9, "attributes": {"color": "blue"}},),
    )

    assert merge_classify_attributes(boxes, [result]) == 1
    assert boxes[0]["attributes"] == {"color": "blue"}


def test_collect_geometry_shapes_from_frozen_model_results():
    result = ModelTaskResult(
        task_id="task-1",
        result=(
            {
                "type": "polygonlabels",
                "value": {"points": [[1, 1], [2, 2], [3, 1]]},
                "parent_box_idx": 0,
            },
        ),
    )

    shapes = collect_geometry_shapes([result], [_bbox(0, 0, 10, 10)])
    assert shapes[0]["parent_box_idx"] == 0
    prompts = geometry_prompts_from_boxes(shapes)
    assert prompts.skipped_geometry == 0
    assert prompts.prompts[0]["box"] == pytest.approx([0.01, 0.01, 0.03, 0.02])


def test_merge_ignores_out_of_range_or_empty():
    boxes = [_bbox(0, 0, 10, 10)]
    classify = [
        _FakeResult("9", [{"score": 0.9, "attributes": {"color": "blue"}}]),  # 越界
        _FakeResult("0", [{"score": 0.9, "attributes": {}}]),  # 空属性
    ]
    n = merge_classify_attributes(boxes, classify)
    assert n == 0
    assert "attributes" not in boxes[0] or not boxes[0].get("attributes")


# ── v0.18.12 · geometry-prompt 投递 (box-seg 下游) ──


def test_geometry_prompts_normalize_and_index():
    """LS 百分比 → 归一化 [0,1] [x1,y1,x2,y2]; parent_box_idx = 原下标。"""
    boxes = [_bbox(10, 20, 30, 40), _bbox(50, 50, 10, 10)]
    batch = geometry_prompts_from_boxes(boxes)
    assert batch.skipped_geometry == 0
    assert batch.prompts[0]["parent_box_idx"] == 0
    assert batch.prompts[0]["box"][0] == pytest.approx(0.1)
    assert batch.prompts[0]["box"][1] == pytest.approx(0.2)
    assert batch.prompts[0]["box"][2] == pytest.approx(0.4)  # (10+30)/100
    assert batch.prompts[0]["box"][3] == pytest.approx(0.6)  # (20+40)/100
    assert batch.prompts[1]["parent_box_idx"] == 1


def test_geometry_prompts_class_filter_keeps_original_index():
    """parent_class_filter 只对目标类生成 prompt, 但 parent_box_idx 保留原下标。"""
    boxes = [_bbox(0, 0, 10, 10, cls="person"), _bbox(20, 20, 10, 10, cls="car")]
    batch = geometry_prompts_from_boxes(boxes, parent_class_filter=["car"])
    assert len(batch.prompts) == 1
    assert batch.prompts[0]["parent_box_idx"] == 1  # car 在原下标 1
    assert batch.skipped_geometry == 0  # person 是路由跳过, 非几何跳过


def test_geometry_prompts_skips_rotated_and_degenerate():
    """旋转框 / 退化框计入 skipped_geometry, 不产 prompt。"""
    boxes = [
        {
            "type": "rectanglelabels",
            "value": {"x": 10, "y": 10, "width": 10, "height": 10, "rotation": 30},
        },
        _bbox(0, 0, 0, 10),  # 退化 (w=0)
        _bbox(30, 30, 20, 20),  # 有效
    ]
    batch = geometry_prompts_from_boxes(boxes)
    assert batch.skipped_geometry == 2
    assert len(batch.prompts) == 1
    assert batch.prompts[0]["parent_box_idx"] == 2


# ── v0.18.15 · crop 坐标回映 + polygon 父框 ──


def _poly(pts, cls="person"):
    return {"type": "polygonlabels", "value": {"points": pts, "polygonlabels": [cls]}}


def test_crop_records_transform_normalized():
    """crop 落归一化图像空间的仿射变换 (ox/oy/sx/sy), 供回映。"""
    img = _img(1000, 500)
    boxes = [_bbox(40, 40, 20, 20)]  # 像素 left=400,top=200,200x100
    batch = crop_inputs_from_boxes(img, boxes, pad=0.0)
    t = batch.transforms["0"]
    assert t["ox"] == pytest.approx(0.4)
    assert t["oy"] == pytest.approx(0.4)
    assert t["sx"] == pytest.approx(0.2)
    assert t["sy"] == pytest.approx(0.2)


def test_remap_bbox_roundtrip_lands_in_parent():
    """crop 内全幅检出 → 回映回原父框; 居中半幅 → 落父框内部。"""
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}
    # 全幅检出 (整张 crop) → 应还原成父框 (40,40,20,20)
    full = [
        {
            "type": "rectanglelabels",
            "value": {"x": 0, "y": 0, "width": 100, "height": 100},
        }
    ]
    [r] = remap_geometry_to_image(full, transform)
    assert r["value"]["x"] == pytest.approx(40)
    assert r["value"]["y"] == pytest.approx(40)
    assert r["value"]["width"] == pytest.approx(20)
    assert r["value"]["height"] == pytest.approx(20)
    # 居中半幅 → 落在父框 (40~60) 内部 (45~55)
    center = [
        {
            "type": "rectanglelabels",
            "value": {"x": 25, "y": 25, "width": 50, "height": 50},
        }
    ]
    [c] = remap_geometry_to_image(center, transform)
    assert c["value"]["x"] == pytest.approx(45)
    assert c["value"]["width"] == pytest.approx(10)
    assert 40 <= c["value"]["x"] <= 60


def test_remap_polygon_points():
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}
    shapes = [
        {"type": "polygonlabels", "value": {"points": [[0, 0], [100, 0], [100, 100]]}}
    ]
    [r] = remap_geometry_to_image(shapes, transform)
    assert r["value"]["points"][0] == [pytest.approx(40), pytest.approx(40)]
    assert r["value"]["points"][1] == [pytest.approx(60), pytest.approx(40)]
    assert r["value"]["points"][2] == [pytest.approx(60), pytest.approx(60)]


def test_remap_drops_degenerate_polygon_without_outer_ring():
    # 退化多边形 (外环 points 空但 holes 非空) 应被丢弃, 不产出 points:[] 的鬼影 shape。
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}
    shapes = [
        {
            "type": "polygonlabels",
            "value": {
                "polygons": [
                    {"points": [[0, 0], [100, 0], [100, 100]]},
                    {"points": [], "holes": [[[10, 10], [20, 10], [20, 20]]]},
                ]
            },
        }
    ]
    [r] = remap_geometry_to_image(shapes, transform)
    assert len(r["value"]["polygons"]) == 1  # 只保留有外环的 poly
    assert r["value"]["polygons"][0]["points"]


def test_remap_skips_shape_with_only_degenerate_polygons():
    # 整个 shape 只有退化多边形 → 无有效环, 整条丢弃 (不进入下游)。
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}
    shapes = [
        {
            "type": "polygonlabels",
            "value": {
                "polygons": [{"points": [], "holes": [[[1, 1], [2, 1], [2, 2]]]}]
            },
        }
    ]
    assert remap_geometry_to_image(shapes, transform) == []


def test_remap_does_not_mutate_input():
    transform = {"ox": 0.5, "oy": 0.0, "sx": 0.5, "sy": 1.0}
    shapes = [
        {
            "type": "rectanglelabels",
            "value": {"x": 0, "y": 0, "width": 100, "height": 100},
        }
    ]
    remap_geometry_to_image(shapes, transform)
    assert shapes[0]["value"]["x"] == 0  # 原始未改


def test_compose_transforms_chains_depth3():
    """outer crop 占图右半, inner 占 crop 右半 → 合成后占图右 1/4 起。"""
    outer = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}
    inner = {"ox": 0.5, "oy": 0.0, "sx": 0.5, "sy": 1.0}
    c = compose_transforms(outer, inner)
    assert c["ox"] == pytest.approx(0.5)  # 0.4 + 0.5*0.2
    assert c["oy"] == pytest.approx(0.4)
    assert c["sx"] == pytest.approx(0.1)  # 0.2*0.5
    assert c["sy"] == pytest.approx(0.2)


def test_crop_supports_polygon_parent_bbox():
    """polygon 父框取外接框裁 crop, 并落 transform。"""
    img = _img(1000, 1000)
    boxes = [_poly([[10, 10], [30, 10], [30, 30], [10, 30]])]  # 外接框 10,10,20,20
    batch = crop_inputs_from_boxes(img, boxes, pad=0.0)
    assert len(batch.inputs) == 1
    crop = _decode(batch.inputs[0]["file_path"])
    assert crop.size == (200, 200)
    assert batch.transforms["0"]["ox"] == pytest.approx(0.1)
    assert batch.transforms["0"]["sx"] == pytest.approx(0.2)


def test_geometry_prompts_supports_polygon_parent():
    """polygon 父框 → geometry-prompt 取外接框归一化。"""
    boxes = [
        _poly([[10, 20], [40, 20], [40, 60], [10, 60]])
    ]  # bbox x=10,y=20,w=30,h=40
    batch = geometry_prompts_from_boxes(boxes)
    assert batch.skipped_geometry == 0
    assert batch.prompts[0]["box"][0] == pytest.approx(0.1)
    assert batch.prompts[0]["box"][2] == pytest.approx(0.4)
    assert batch.prompts[0]["box"][3] == pytest.approx(0.6)


def test_collect_geometry_shapes_filters_bad_parent_idx():
    """收集下游 polygon: 保留合法 parent_box_idx, 丢越界项。"""
    boxes = [_bbox(0, 0, 10, 10)]
    seg = [
        _FakeResult(
            "task-1",
            [
                {
                    "type": "polygonlabels",
                    "value": {"points": [[1, 1]]},
                    "parent_box_idx": 0,
                },
                {
                    "type": "polygonlabels",
                    "value": {"points": [[2, 2]]},
                    "parent_box_idx": 9,
                },  # 越界
                {
                    "type": "polygonlabels",
                    "value": {"points": [[3, 3]]},
                },  # 无 parent_box_idx → 保留
            ],
        ),
    ]
    shapes = collect_geometry_shapes(seg, boxes)
    assert len(shapes) == 2
    assert shapes[0]["parent_box_idx"] == 0


def test_remap_accepts_normalized_backend_coords():
    """回归守卫: grounded-sam2 / sam3 的 /predict 返回归一化 [0,1] 几何 (yolo / onnxtools
    返回百分比 [0,100])。两种口径反投影后必须落在同一原图位置——此前硬当百分比处理,
    归一化口径的子框被缩小 100 倍并塌到 crop 左上角。"""
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}

    pct = [
        {
            "type": "rectanglelabels",
            "value": {"x": 25, "y": 25, "width": 50, "height": 50},
        }
    ]
    norm = [
        {
            "type": "rectanglelabels",
            "value": {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5},
        }
    ]

    [a] = remap_geometry_to_image(pct, transform)
    [b] = remap_geometry_to_image(norm, transform)
    for k in ("x", "y", "width", "height"):
        assert a["value"][k] == pytest.approx(b["value"][k])
    # crop 内居中半幅 → 原图 45~55 (父框 40~60 的中间一半)
    assert b["value"]["x"] == pytest.approx(45)
    assert b["value"]["width"] == pytest.approx(10)


def test_remap_accepts_normalized_polygon_coords():
    """polygon 路径同款口径自适应 (sam3-segmentation / segment-yoloe 的 mask→polygon 子框)。"""
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}

    pct = [
        {"type": "polygonlabels", "value": {"points": [[0, 0], [100, 0], [100, 100]]}}
    ]
    norm = [
        {"type": "polygonlabels", "value": {"points": [[0, 0], [1.0, 0], [1.0, 1.0]]}}
    ]

    [a] = remap_geometry_to_image(pct, transform)
    [b] = remap_geometry_to_image(norm, transform)
    for pa, pb in zip(a["value"]["points"], b["value"]["points"]):
        assert pa == pytest.approx(pb)
    # 三角形顶点铺满 crop → 原图父框 (40~60) 的三个角
    for got, want in zip(b["value"]["points"], [[40, 40], [60, 40], [60, 60]]):
        assert got == pytest.approx(want)


def test_remap_polygon_with_holes_and_multipolygon():
    """回归守卫: 带洞 / 多连通 mask→polygon (grounded-sam2 / sam3 在 mask 有洞或多连通时输出)
    在 ROI 反投影中曾被漏处理——holes 留在 crop 坐标系 (错位), polygons 形态因缺 points 被整条丢弃。"""
    transform = {"ox": 0.4, "oy": 0.4, "sx": 0.2, "sy": 0.2}

    # ② 单连通带洞: 外环铺满 crop, 洞在 crop 中心半幅。
    holed = [
        {
            "type": "polygonlabels",
            "value": {
                "points": [[0, 0], [1.0, 0], [1.0, 1.0], [0, 1.0]],
                "holes": [[[0.25, 0.25], [0.75, 0.25], [0.75, 0.75]]],
            },
        }
    ]
    [r] = remap_geometry_to_image(holed, transform)
    for got, want in zip(
        r["value"]["points"], [[40, 40], [60, 40], [60, 60], [40, 60]]
    ):
        assert got == pytest.approx(want)
    for got, want in zip(r["value"]["holes"][0], [[45, 45], [55, 45], [55, 55]]):
        assert got == pytest.approx(want)

    # ③ 多连通: 两个独立环, 均须反投影且整条 shape 不被丢弃。
    multi = [
        {
            "type": "polygonlabels",
            "value": {
                "polygons": [
                    {"points": [[0, 0], [0.5, 0], [0.5, 0.5]]},
                    {"points": [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0]]},
                ]
            },
        }
    ]
    [m] = remap_geometry_to_image(multi, transform)
    assert len(m["value"]["polygons"]) == 2
    for got, want in zip(
        m["value"]["polygons"][0]["points"], [[40, 40], [50, 40], [50, 50]]
    ):
        assert got == pytest.approx(want)
    for got, want in zip(
        m["value"]["polygons"][1]["points"], [[50, 50], [60, 50], [60, 60]]
    ):
        assert got == pytest.approx(want)
