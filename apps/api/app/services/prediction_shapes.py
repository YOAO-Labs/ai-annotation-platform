"""Convert stored prediction shapes to labeling geometry."""

import logging
import math
from typing import Any

from yoao_workspace.configuration_values import TOOL_UNIT_IDS as _SCHEMA_TOOL_UNIT_IDS

logger = logging.getLogger("app.services.prediction")

TOOL_UNIT_IDS = frozenset(_SCHEMA_TOOL_UNIT_IDS)


def derive_tool_unit_from_ls_type(
    typ: str | None, value: dict[str, Any] | None = None
) -> str:
    """LabelStudio result.type → tool_unit_id.

    polygonlabels / brushlabels / multi_polygon → region; polylinelabels →
    polyline; keypointlabels → keypoint; rectanglelabels 默认 bbox, 带 rotation
    字段时归 rotated_bbox.
    """
    if typ in {"polygonlabels", "brushlabels", "multi_polygon"}:
        return "region"
    if typ == "polylinelabels":
        return "polyline"
    if typ == "keypointlabels":
        return "keypoint"
    if (
        typ == "rectanglelabels"
        and isinstance(value, dict)
        and value.get("rotation") is not None
    ):
        return "rotated_bbox"
    return "bbox"


def derive_tool_unit_from_result(result: list[dict] | None) -> str:
    """从 prediction.result 数组首条 shape.type 派生 tool_unit_id."""
    if not result:
        return "bbox"
    first = result[0] if isinstance(result[0], dict) else {}
    explicit = first.get("tool_unit_id")
    if explicit == "ai_interactive":
        logger.warning(
            "prediction result carries retired tool_unit_id 'ai_interactive'; "
            "deriving unit from geometry/type instead"
        )
    if explicit in TOOL_UNIT_IDS:
        return explicit
    value = first.get("value")
    return derive_tool_unit_from_ls_type(
        first.get("type"), value if isinstance(value, dict) else None
    )


def _tool_unit_from_internal_geometry(geometry: Any) -> str | None:
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get("type")
    if kind == "polyline":
        return "polyline"
    if kind == "rotated_bbox":
        return "rotated_bbox"
    if kind == "keypoint":
        return "keypoint"
    if kind in {
        "polygon",
        "multi_polygon",
        "raster_mask",
        "video_mask",
        "video_track_polygon",
        "video_track_mask",
    }:
        return "region"
    if kind == "video_track_polyline":
        return "polyline"
    if kind in {"bbox", "video_bbox", "video_track_bbox"}:
        return "bbox"
    return None


def _percent_scale(values: list[float]) -> float:
    """兼容平台历史: ML backend 文档用 [0,1], AAP import 写 LS 百分比 [0,100]."""
    return 100.0 if any(abs(v) > 1.0 for v in values) else 1.0


def _normalize_points(points: Any, scale: float) -> list[list[float]]:
    if not isinstance(points, list):
        return []
    out: list[list[float]] = []
    for pt in points:
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            out.append([float(pt[0]) / scale, float(pt[1]) / scale])
    return out


def _collect_point_values(points: Any) -> list[float]:
    values: list[float] = []
    if not isinstance(points, list):
        return values
    for pt in points:
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            values.extend([float(pt[0]), float(pt[1])])
    return values


def _normalize_keypoints(points: Any) -> list[dict[str, float | int]]:
    if not isinstance(points, list):
        return []
    values: list[float] = []
    parsed: list[tuple[float, float, int]] = []
    for pt in points:
        try:
            if isinstance(pt, dict):
                x = float(pt["x"])
                y = float(pt["y"])
                v = int(pt.get("v", 2))
            elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                x = float(pt[0])
                y = float(pt[1])
                v = int(pt[2]) if len(pt) >= 3 else 2
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        parsed.append((x, y, v))
        values.extend([x, y])
    scale = _percent_scale(values)
    return [{"x": x / scale, "y": y / scale, "v": v} for x, y, v in parsed]


def _track_to_internal_shape(s: dict, confidence: float, attributes: dict) -> dict:
    """检测式视频追踪 result item → 内部 `VideoTrackGeometry` 形态.

    输入 (backend 聚合轨迹, worker ingestion 已把 int track_id 映射成 `trk_<uuid>`)::

        {"type": "video_track_bbox", "track_id": "trk_...", "semantic_label": "car_3",
         "class_name": "car", "score": 0.87,
         "keyframes": [{"frame_index": 0, "bbox": {"x","y","w","h"}, "score"?: ...}, ...]}

    输出内部 shape 的 ``geometry`` 对齐 ``VideoTrackGeometry`` (``_jsonb_types.py``):
    每帧 ``source="prediction"``; bbox 直接信 0-1 (不 ``_percent_scale``)。keyframe 级 score
    不入几何 (schema 无此字段), 轨迹级 score 走 ``confidence``。
    """
    keyframes: list[dict] = []
    for kf in s.get("keyframes") or []:
        if not isinstance(kf, dict):
            continue
        bbox = kf.get("bbox") or {}
        keyframes.append(
            {
                "frame_index": int(kf.get("frame_index", 0)),
                "bbox": {
                    "x": float(bbox.get("x", 0.0)),
                    "y": float(bbox.get("y", 0.0)),
                    "w": float(bbox.get("w", 0.0)),
                    "h": float(bbox.get("h", 0.0)),
                },
                "source": "prediction",
            }
        )
    geometry: dict = {
        "type": "video_track_bbox",
        "track_id": str(s.get("track_id", "")),
        "keyframes": keyframes,
        "outside": [],
    }
    semantic_label = s.get("semantic_label")
    if semantic_label is not None:
        geometry["semantic_label"] = str(semantic_label)
    return {
        "type": "video_track_bbox",
        "class_name": s.get("class_name", "") or "",
        "geometry": geometry,
        "confidence": confidence,
        "tool_unit_id": derive_tool_unit_from_ls_type("video_track_bbox"),
        "attributes": attributes,
    }


def to_internal_shape(s: dict) -> dict:
    """将 LabelStudio result shape 转成内部前端 schema。

    Worker 把 ML backend 返回的 LabelStudio 标准 ``{type, value, score}`` 原样
    存入 ``predictions.result``; 前端 ``predictionsToBoxes``
    (apps/web/.../transforms.ts) 期望 ``{type, class_name, geometry, confidence}``.
    此适配器在读取时转换；数据库保留 LabelStudio 标准，
    以便与导出和 CVAT 等工具兼容。

    兼容旧格式: 已有 ``geometry`` 字段时 pass-through, 不做二次转换.
    返回字典含从 LS type 派生的 ``tool_unit_id``。
    协议 v2: 返回字典含 ``attributes`` (从 shape 顶层 ``s["attributes"]`` 提取),
    供 OCR / doc_layout 富属性 (text / language / orientation) 透传到 annotation.attributes;
    非 OCR shape 为 ``{}``。
    """
    if not isinstance(s, dict):
        return {}
    if "geometry" in s:
        s = dict(s)
        if s.get("tool_unit_id") not in _SCHEMA_TOOL_UNIT_IDS:
            s["tool_unit_id"] = _tool_unit_from_internal_geometry(
                s.get("geometry")
            ) or derive_tool_unit_from_ls_type(s.get("type"))
        return s

    typ = s.get("type", "rectanglelabels")
    val = s.get("value") or {}
    raw_score = s.get("score")
    if raw_score is None:
        raw_score = s.get("confidence")
    confidence = float(raw_score) if raw_score is not None else 0.0

    raw_attributes = s.get("attributes")
    attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}

    if typ == "video_track_bbox":
        return _track_to_internal_shape(s, confidence, attributes)

    labels = val.get(typ)
    if not labels:
        labels = val.get("labels") or []
    if not labels and "class" in val:
        labels = [val["class"]]
    class_name = labels[0] if labels else ""

    if typ == "rectanglelabels":
        x = float(val.get("x", 0))
        y = float(val.get("y", 0))
        w = float(val.get("width", 0))
        h = float(val.get("height", 0))
        scale = (
            100.0
            if s.get("coordinate_units") == "percent"
            else _percent_scale([x, y, w, h])
        )
        x /= scale
        y /= scale
        w /= scale
        h /= scale
        if val.get("rotation") is not None:
            angle = float(val.get("rotation") or 0) % 360
            rad = math.radians(angle)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            geometry = {
                "type": "rotated_bbox",
                "cx": x + (w / 2) * cos_a - (h / 2) * sin_a,
                "cy": y + (w / 2) * sin_a + (h / 2) * cos_a,
                "w": w,
                "h": h,
                "angle": angle,
            }
        else:
            geometry = {
                "type": "bbox",
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            }
    elif typ == "polygonlabels":
        polygons_raw = val.get("polygons")
        if polygons_raw:
            scale_values: list[float] = []
            for p in polygons_raw:
                scale_values.extend(_collect_point_values(p.get("points", [])))
                for hole in p.get("holes") or []:
                    scale_values.extend(_collect_point_values(hole))
            scale = _percent_scale(scale_values)
            new_polys: list[dict] = []
            for p in polygons_raw:
                entry: dict = {
                    "type": "polygon",
                    "points": _normalize_points(p.get("points", []), scale),
                }
                holes = p.get("holes") or []
                if holes:
                    entry["holes"] = [_normalize_points(hole, scale) for hole in holes]
                new_polys.append(entry)
            geometry = {"type": "multi_polygon", "polygons": new_polys}
        else:
            scale_values = _collect_point_values(val.get("points", []))
            for hole in val.get("holes") or []:
                scale_values.extend(_collect_point_values(hole))
            scale = _percent_scale(scale_values)
            geometry = {
                "type": "polygon",
                "points": _normalize_points(val.get("points", []), scale),
            }
            holes = val.get("holes") or []
            if holes:
                geometry["holes"] = [_normalize_points(hole, scale) for hole in holes]
    elif typ == "polylinelabels":
        points = val.get("points") or []
        scale = _percent_scale(_collect_point_values(points))
        geometry = {
            "type": "polyline",
            "points": _normalize_points(points, scale),
        }
    elif typ == "keypointlabels" and val.get("points") is not None:
        geometry = {
            "type": "keypoint",
            "points": _normalize_keypoints(val.get("points")),
        }
    else:
        geometry = {}

    return {
        "type": typ,
        "class_name": class_name,
        "geometry": geometry,
        "confidence": confidence,
        "tool_unit_id": derive_tool_unit_from_ls_type(typ, val),
        "attributes": attributes,
    }


def to_video_bbox_result(raw_result: list[dict], frame_index: int) -> list[dict]:
    """将图像模型的 LabelStudio 检测结果转成单帧 ``video_bbox`` 几何。

    复用 ``to_internal_shape`` 把 ``rectanglelabels`` 归一到 0-1 的 bbox (内含 ``_percent_scale``
    自动兼容 backend 的 0-1 / 0-100 两种口径, 与图像检测候选完全同源), 再套一层 ``frame_index``
    改写成 ``video_bbox``。**只收 bbox 几何**——非 bbox (旋转框 / polygon / keypoint 等) 跳过,
    视频单帧几何目前只有 ``video_bbox``。

    ``VideoBboxGeometry`` 是 ``extra="forbid"``: geometry dict 只放 ``type/frame_index/x/y/w/h``;
    ``class_name`` / ``confidence`` 放在 result item 顶层 (accept 从顶层读 ``type`` 决定
    annotation_type, 见 ``services/annotation.py``)。

    工作台单帧推理 (ml_backends.predict-frame) 与 worker 批量逐帧推理
    共用此转换。
    """
    out: list[dict] = []
    for raw in raw_result:
        if not isinstance(raw, dict):
            continue
        shape = to_internal_shape(raw)
        geom = shape.get("geometry") or {}
        if geom.get("type") != "bbox":
            continue
        out.append(
            {
                "type": "video_bbox",
                "class_name": shape.get("class_name", "") or "",
                "confidence": shape.get("confidence", 0.0),
                "geometry": {
                    "type": "video_bbox",
                    "frame_index": frame_index,
                    "x": geom["x"],
                    "y": geom["y"],
                    "w": geom["w"],
                    "h": geom["h"],
                },
                "tool_unit_id": "bbox",
                "attributes": shape.get("attributes") or {},
            }
        )
    return out
