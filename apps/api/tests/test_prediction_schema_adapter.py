"""LabelStudio → 内部 schema adapter 测试."""

from __future__ import annotations

import pytest

from app.services.prediction import to_internal_shape


def test_rectanglelabels_with_value_field():
    raw = {
        "type": "rectanglelabels",
        "score": 0.92,
        "value": {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
            "rectanglelabels": ["car"],
        },
    }
    out = to_internal_shape(raw)
    assert out["type"] == "rectanglelabels"
    assert out["class_name"] == "car"
    assert out["confidence"] == 0.92
    assert out["geometry"] == {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


def test_rectanglelabels_percent_value_field_normalized():
    raw = {
        "type": "rectanglelabels",
        "score": 0.92,
        "value": {
            "x": 10,
            "y": 20,
            "width": 30,
            "height": 40,
            "rectanglelabels": ["car"],
        },
    }
    out = to_internal_shape(raw)
    assert out["geometry"] == {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


def test_polygonlabels_with_value_field():
    raw = {
        "type": "polygonlabels",
        "score": 0.85,
        "value": {
            "points": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
            "polygonlabels": ["person"],
        },
    }
    out = to_internal_shape(raw)
    assert out["type"] == "polygonlabels"
    assert out["class_name"] == "person"
    assert out["confidence"] == 0.85
    assert out["geometry"] == {
        "type": "polygon",
        "points": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
    }


def test_polylinelabels_with_value_field():
    raw = {
        "type": "polylinelabels",
        "score": 0.8,
        "value": {
            "points": [[10, 20], [30, 40]],
            "polylinelabels": ["lane"],
        },
    }
    out = to_internal_shape(raw)
    assert out["type"] == "polylinelabels"
    assert out["class_name"] == "lane"
    assert out["geometry"] == {
        "type": "polyline",
        "points": [[0.1, 0.2], [0.3, 0.4]],
    }
    assert out["tool_unit_id"] == "polyline"


def test_rectanglelabels_with_rotation_to_rotated_bbox():
    raw = {
        "type": "rectanglelabels",
        "score": 0.7,
        "value": {
            "x": 40,
            "y": 45,
            "width": 20,
            "height": 10,
            "rotation": 0,
            "rectanglelabels": ["car"],
        },
    }
    out = to_internal_shape(raw)
    assert out["geometry"] == {
        "type": "rotated_bbox",
        "cx": pytest.approx(0.5),
        "cy": pytest.approx(0.5),
        "w": pytest.approx(0.2),
        "h": pytest.approx(0.1),
        "angle": 0.0,
    }
    assert out["tool_unit_id"] == "rotated_bbox"


def test_legacy_labels_field_fallback():
    """老格式: value.labels 而非 value.{type}."""
    raw = {
        "type": "rectanglelabels",
        "score": 0.5,
        "value": {"x": 0, "y": 0, "width": 1, "height": 1, "labels": ["bottle"]},
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "bottle"


def test_legacy_class_field_fallback():
    """更老格式: value.class 字符串."""
    raw = {
        "type": "rectanglelabels",
        "score": 0.7,
        "value": {"x": 0, "y": 0, "width": 1, "height": 1, "class": "dog"},
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "dog"


def test_already_internal_shape_passthrough():
    """已是内部 schema 时保留原对象，仅补缺失的 tool_unit_id。"""
    raw = {
        "type": "rectanglelabels",
        "class_name": "cat",
        "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
        "confidence": 0.9,
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "cat"
    assert out["geometry"] == raw["geometry"]
    # 缺 tool_unit_id 时按 type 反推回填
    assert out["tool_unit_id"] == "bbox"


def test_internal_shape_passthrough_derives_geometry_unit():
    raw = {
        "type": "rectanglelabels",
        "class_name": "car",
        "geometry": {
            "type": "rotated_bbox",
            "cx": 0.5,
            "cy": 0.5,
            "w": 0.2,
            "h": 0.1,
            "angle": 30,
        },
        "confidence": 0.9,
    }
    out = to_internal_shape(raw)
    assert out is raw
    assert out["tool_unit_id"] == "rotated_bbox"


def test_internal_shape_passthrough_keeps_explicit_tool_unit_id():
    raw = {
        "type": "polygonlabels",
        "class_name": "road",
        "geometry": {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
        "confidence": 0.5,
        "tool_unit_id": "region",
    }
    assert to_internal_shape(raw) == raw


def test_score_missing_falls_back_to_confidence():
    raw = {
        "type": "rectanglelabels",
        "confidence": 0.6,
        "value": {"x": 0, "y": 0, "width": 1, "height": 1, "rectanglelabels": ["a"]},
    }
    assert to_internal_shape(raw)["confidence"] == 0.6


def test_score_missing_defaults_to_zero():
    raw = {
        "type": "rectanglelabels",
        "value": {"x": 0, "y": 0, "width": 1, "height": 1, "rectanglelabels": ["a"]},
    }
    assert to_internal_shape(raw)["confidence"] == 0.0


def test_no_labels_returns_empty_class():
    raw = {
        "type": "rectanglelabels",
        "score": 0.9,
        "value": {"x": 0, "y": 0, "width": 1, "height": 1},
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == ""


def test_unknown_type_returns_empty_geometry():
    raw = {"type": "keypoints", "score": 0.5, "value": {"x": 0.5, "y": 0.5}}
    out = to_internal_shape(raw)
    assert out["geometry"] == {}
    assert out["type"] == "keypoints"


def test_keypointlabels_points_to_internal_keypoint():
    raw = {
        "type": "keypointlabels",
        "score": 0.75,
        "value": {
            "keypointlabels": ["person"],
            "points": [
                {"x": 10, "y": 20, "v": 2},
                {"x": 30, "y": 40, "v": 1},
                {"x": 50, "y": 60, "v": 0},
            ],
        },
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "person"
    assert out["confidence"] == 0.75
    assert out["tool_unit_id"] == "keypoint"
    assert out["geometry"] == {
        "type": "keypoint",
        "points": [
            {"x": 0.1, "y": 0.2, "v": 2},
            {"x": 0.3, "y": 0.4, "v": 1},
            {"x": 0.5, "y": 0.6, "v": 0},
        ],
    }


def test_non_dict_input_safe():
    assert to_internal_shape(None) == {}
    assert to_internal_shape("garbage") == {}
    assert to_internal_shape([1, 2]) == {}


# ─── 协议 v2 OCR / doc_layout attributes 透传 ─────────────────────


def test_ocr_shape_extracts_attributes_text():
    """OCR backend 在 shape 顶层写 attributes.{text,language,...}, adapter 原样提取。"""
    raw = {
        "type": "rectanglelabels",
        "score": 0.95,
        "value": {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.05,
            "rectanglelabels": ["text_line"],
        },
        "attributes": {
            "text": "Invoice #2026",
            "language": "en",
            "orientation": 0,
        },
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "text_line"
    assert out["geometry"]["type"] == "bbox"
    assert out["attributes"]["text"] == "Invoice #2026"
    assert out["attributes"]["language"] == "en"


def test_non_ocr_shape_attributes_defaults_to_empty_dict():
    """无 attributes 字段时 adapter 输出 {} (不影响老路径)。"""
    raw = {
        "type": "rectanglelabels",
        "score": 0.9,
        "value": {
            "x": 0,
            "y": 0,
            "width": 1,
            "height": 1,
            "rectanglelabels": ["car"],
        },
    }
    out = to_internal_shape(raw)
    assert out["attributes"] == {}


def test_doc_layout_class_from_value_class_with_attributes():
    """doc_layout: class 取 value.class, 富属性走 shape 顶层 attributes。"""
    raw = {
        "type": "rectanglelabels",
        "score": 0.88,
        "value": {
            "x": 0.0,
            "y": 0.0,
            "width": 0.5,
            "height": 0.2,
            "class": "title",
        },
        "attributes": {"reading_order": 1},
    }
    out = to_internal_shape(raw)
    assert out["class_name"] == "title"
    assert out["attributes"] == {"reading_order": 1}


# ─── 黄金样本 — 锁定内部 schema 边界, 防止再次漂移 ────────────────────


def test_idempotent_on_internal_shape():
    """to_internal_shape 二次调用同形态结果 — 即 read path 多次应用安全."""
    raw = {
        "type": "rectanglelabels",
        "score": 0.9,
        "value": {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
            "rectanglelabels": ["car"],
        },
    }
    once = to_internal_shape(raw)
    twice = to_internal_shape(once)
    assert once == twice


def test_dual_presence_value_and_geometry_prefers_geometry():
    """边界: 当后端写入既含 LS value 又含内部 geometry (老 fixture / 迁移期)
    时, geometry 优先 pass-through, 不再二次解释 value."""
    raw = {
        "type": "rectanglelabels",
        "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
        "class_name": "internal-cls",
        "confidence": 0.42,
        "score": 0.99,
        "value": {
            "x": 0.5,
            "y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rectanglelabels": ["ls-cls"],
        },
    }
    out = to_internal_shape(raw)
    # geometry 路径胜出 — internal-cls / 0.42 / 0/0/1/1, 而非 ls-cls / 0.99
    assert out["class_name"] == "internal-cls"
    assert out["confidence"] == 0.42
    assert out["geometry"] == {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1}


def test_geometry_passthrough_preserves_unknown_fields():
    """已是内部 shape 时 pass-through 不丢非标字段 (前端可能附带 extra meta)."""
    raw = {
        "type": "rectanglelabels",
        "geometry": {"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
        "class_name": "x",
        "confidence": 0.5,
        "extra_meta": {"hint": "from-sam"},
    }
    out = to_internal_shape(raw)
    assert out is raw  # 同一对象返回, 字段无损
    assert out["extra_meta"] == {"hint": "from-sam"}


# ─── mask 多连通域 / 空洞 LS → 内部 schema ─────────────────


def test_polygonlabels_with_holes_field():
    """单连通带 hole: value.holes 透传到 geometry.holes."""
    raw = {
        "type": "polygonlabels",
        "score": 0.91,
        "value": {
            "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
            "holes": [
                [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]],
            ],
            "polygonlabels": ["donut"],
        },
    }
    out = to_internal_shape(raw)
    assert out["geometry"]["type"] == "polygon"
    assert len(out["geometry"]["points"]) == 4
    assert len(out["geometry"]["holes"]) == 1
    assert len(out["geometry"]["holes"][0]) == 4


def test_polygonlabels_with_polygons_field_multi():
    """多连通域: value.polygons → geometry.multi_polygon."""
    raw = {
        "type": "polygonlabels",
        "score": 0.88,
        "value": {
            "polygons": [
                {
                    "points": [[0, 0], [1, 0], [1, 1]],
                    "holes": [],
                },
                {
                    "points": [[2, 2], [3, 2], [3, 3], [2, 3]],
                    "holes": [[[2.4, 2.4], [2.6, 2.4], [2.6, 2.6]]],
                },
            ],
            "polygonlabels": ["multi"],
        },
    }
    out = to_internal_shape(raw)
    assert out["geometry"]["type"] == "multi_polygon"
    assert len(out["geometry"]["polygons"]) == 2
    assert "holes" not in out["geometry"]["polygons"][0]  # 无 hole 时不写字段
    assert len(out["geometry"]["polygons"][1]["holes"]) == 1


def test_polygonlabels_legacy_no_holes_field_unchanged():
    """仅有 points 的 LabelStudio shape 转换后不带 holes 字段。"""
    raw = {
        "type": "polygonlabels",
        "score": 0.85,
        "value": {
            "points": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
            "polygonlabels": ["person"],
        },
    }
    out = to_internal_shape(raw)
    # 老回归: geometry 字面值不带 holes 字段
    assert out["geometry"] == {
        "type": "polygon",
        "points": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
    }


def test_pydantic_polygon_geometry_default_holes_empty():
    """PolygonGeometry 解析无 holes 字段的老 dict 时 holes 默认 []."""
    from app.schemas._jsonb_types import PolygonGeometry

    g = PolygonGeometry.model_validate(
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]}
    )
    assert g.holes == []


def test_pydantic_polygon_geometry_with_holes():
    from app.schemas._jsonb_types import PolygonGeometry

    g = PolygonGeometry.model_validate(
        {
            "type": "polygon",
            "points": [[0, 0], [10, 0], [10, 10], [0, 10]],
            "holes": [
                [[2, 2], [8, 2], [8, 8], [2, 8]],
            ],
        }
    )
    assert len(g.holes) == 1
    assert len(g.holes[0]) == 4


def test_pydantic_polygon_geometry_rejects_invalid_hole():
    """hole 顶点 < 3 时 422."""
    from pydantic import ValidationError

    from app.schemas._jsonb_types import PolygonGeometry

    import pytest as _pytest

    with _pytest.raises(ValidationError):
        PolygonGeometry.model_validate(
            {
                "type": "polygon",
                "points": [[0, 0], [1, 0], [1, 1]],
                "holes": [[[0.5, 0.5], [0.6, 0.5]]],  # 仅 2 顶点
            }
        )


def test_pydantic_multi_polygon_discriminator():
    """Geometry union discriminator 路由 type=multi_polygon → MultiPolygonGeometry."""
    from pydantic import TypeAdapter

    from app.schemas._jsonb_types import (
        Geometry,
        MultiPolygonGeometry,
    )

    adapter = TypeAdapter(Geometry)
    g = adapter.validate_python(
        {
            "type": "multi_polygon",
            "polygons": [
                {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
                {
                    "type": "polygon",
                    "points": [[2, 2], [3, 2], [3, 3]],
                    "holes": [[[2.2, 2.2], [2.8, 2.2], [2.8, 2.8]]],
                },
            ],
        }
    )
    assert isinstance(g, MultiPolygonGeometry)
    assert len(g.polygons) == 2
    assert g.polygons[0].holes == []
    assert len(g.polygons[1].holes) == 1


def test_pydantic_multi_polygon_requires_at_least_one():
    from pydantic import ValidationError

    from app.schemas._jsonb_types import MultiPolygonGeometry

    import pytest as _pytest

    with _pytest.raises(ValidationError):
        MultiPolygonGeometry(polygons=[])


# ── 检测式视频追踪 result item → VideoTrackGeometry ──────────────────


def test_video_track_bbox_reshapes_keyframes_to_geometry():
    raw = {
        "type": "video_track_bbox",
        "track_id": "trk_abc",
        "class_name": "car",
        "score": 0.87,
        "keyframes": [
            {
                "frame_index": 0,
                "bbox": {"x": 0.10, "y": 0.20, "w": 0.08, "h": 0.06},
                "score": 0.9,
            },
            {
                "frame_index": 1,
                "bbox": {"x": 0.11, "y": 0.21, "w": 0.08, "h": 0.06},
                "score": 0.88,
            },
        ],
    }
    out = to_internal_shape(raw)
    assert out["type"] == "video_track_bbox"
    assert out["class_name"] == "car"
    assert out["confidence"] == 0.87
    assert out["tool_unit_id"] == "bbox"
    g = out["geometry"]
    assert g["type"] == "video_track_bbox"
    assert g["track_id"] == "trk_abc"
    assert g["outside"] == []
    assert len(g["keyframes"]) == 2
    # 每帧标 source=prediction, bbox 保 0-1, keyframe 级 score 不入几何。
    assert g["keyframes"][0] == {
        "frame_index": 0,
        "bbox": {"x": 0.10, "y": 0.20, "w": 0.08, "h": 0.06},
        "source": "prediction",
    }


def test_video_track_bbox_small_coords_not_percent_scaled():
    """坐标全 <1 (小目标) 不得被 _percent_scale 误判为百分比而除 100。"""
    raw = {
        "type": "video_track_bbox",
        "track_id": "trk_1",
        "class_name": "ball",
        "score": 0.5,
        "keyframes": [
            {"frame_index": 3, "bbox": {"x": 0.5, "y": 0.5, "w": 0.02, "h": 0.02}}
        ],
    }
    kf = to_internal_shape(raw)["geometry"]["keyframes"][0]
    assert kf["bbox"] == {"x": 0.5, "y": 0.5, "w": 0.02, "h": 0.02}


def test_video_track_bbox_semantic_label_passthrough():
    raw = {
        "type": "video_track_bbox",
        "track_id": "trk_1",
        "semantic_label": "car_3",
        "class_name": "car",
        "keyframes": [
            {"frame_index": 0, "bbox": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}}
        ],
    }
    assert to_internal_shape(raw)["geometry"]["semantic_label"] == "car_3"


def test_video_track_bbox_no_semantic_label_omitted():
    raw = {
        "type": "video_track_bbox",
        "track_id": "trk_1",
        "class_name": "car",
        "keyframes": [
            {"frame_index": 0, "bbox": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}}
        ],
    }
    assert "semantic_label" not in to_internal_shape(raw)["geometry"]


def test_video_track_bbox_geometry_validates_against_pydantic():
    """产出的 geometry dict 必须能通过 VideoTrackGeometry 校验 (accept 落库不会 422)。"""
    from app.schemas._jsonb_types import VideoTrackGeometry

    raw = {
        "type": "video_track_bbox",
        "track_id": "trk_x",
        "semantic_label": "car_3",
        "class_name": "car",
        "score": 0.8,
        "keyframes": [
            {"frame_index": 0, "bbox": {"x": 0.1, "y": 0.2, "w": 0.08, "h": 0.06}},
            {"frame_index": 5, "bbox": {"x": 0.12, "y": 0.21, "w": 0.08, "h": 0.06}},
        ],
    }
    g = VideoTrackGeometry.model_validate(to_internal_shape(raw)["geometry"])
    assert g.track_id == "trk_x"
    assert g.keyframes[0].source == "prediction"
    assert g.keyframes[1].frame_index == 5


# ── ingestion 端 int track_id → trk_<uuid> 映射 ──────────────────────


def test_remap_track_ids_int_to_uuid_with_semantic_label():
    from app.services.prediction import _remap_track_ids

    out = _remap_track_ids(
        [
            {
                "type": "video_track_bbox",
                "track_id": 3,
                "class_name": "car",
                "keyframes": [],
            }
        ]
    )
    assert out[0]["track_id"].startswith("trk_")
    assert out[0]["semantic_label"] == "car_3"


def test_remap_track_ids_passthrough_non_track():
    from app.services.prediction import _remap_track_ids

    items = [{"type": "rectanglelabels", "value": {"x": 1}}]
    out = _remap_track_ids(items)
    assert out[0] == {"type": "rectanglelabels", "value": {"x": 1}}


def test_remap_track_ids_idempotent_for_existing_trk():
    from app.services.prediction import _remap_track_ids

    item = {
        "type": "video_track_bbox",
        "track_id": "trk_deadbeef",
        "semantic_label": "car_3",
        "keyframes": [],
    }
    out = _remap_track_ids([item])
    assert out[0]["track_id"] == "trk_deadbeef"  # 不重映射


def test_remap_track_ids_does_not_mutate_input():
    from app.services.prediction import _remap_track_ids

    items = [
        {
            "type": "video_track_bbox",
            "track_id": 5,
            "class_name": "car",
            "keyframes": [],
        }
    ]
    _remap_track_ids(items)
    assert items[0]["track_id"] == 5  # 原对象未被改


def test_remap_track_ids_missing_class_falls_back_to_obj():
    from app.services.prediction import _remap_track_ids

    out = _remap_track_ids(
        [{"type": "video_track_bbox", "track_id": 9, "keyframes": []}]
    )
    assert out[0]["semantic_label"] == "obj_9"
