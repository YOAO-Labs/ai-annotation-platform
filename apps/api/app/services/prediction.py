from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.prediction import (
    Prediction,
    PredictionMeta,
    FailedPrediction,
    INTERACTIVE_ACCEPT_PREDICTION_SOURCE,
)
from app.db.models.task import Task
from app.schemas._jsonb_types import TOOL_UNIT_IDS as _SCHEMA_TOOL_UNIT_IDS
from app.services.annotation_propagation import _new_track_id
from app.services.raster_mask_storage import prepare_mask_payload_for_write

logger = logging.getLogger("app.services.prediction")

# 显式 tool_unit_id 的白名单, 单一来源取自 schema 的 ToolUnitId (此前本地手抄一份, 漏了
# point_mask_3d 而漂移)。'ai_interactive' 已退役: result item 若显式带该值, 不在白名单内,
# derive_tool_unit_from_result 会绕过它、改按几何类型派生并记 warning。
TOOL_UNIT_IDS = frozenset(_SCHEMA_TOOL_UNIT_IDS)


def _public_video_provenance(evidence):
    from app.services.video_prediction_references import public_provenance

    return public_provenance(evidence)


async def claim_failed_prediction_retry(
    db: AsyncSession, failed_id: uuid.UUID, max_retries: int
) -> FailedPrediction | None:
    """Lock and reserve one failed prediction for a single queued retry."""
    row = (
        await db.execute(
            select(FailedPrediction)
            .where(FailedPrediction.id == failed_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.dismissed_at is not None
        or (row.retry_count or 0) >= max_retries
        or bool((row.extra or {}).get("retry_pending"))
    ):
        return None
    row.extra = {**(row.extra or {}), "retry_pending": True}
    row.last_retry_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def release_failed_prediction_retry_claim(
    db: AsyncSession, failed_id: uuid.UUID
) -> None:
    row = await db.scalar(
        select(FailedPrediction)
        .where(FailedPrediction.id == failed_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row
        and not (row.extra or {}).get("retry_job_id")
        and (row.extra or {}).get("retry_pending")
    ):
        extra = dict(row.extra or {})
        extra.pop("retry_pending", None)
        row.extra = extra
        await db.flush()


def derive_tool_unit_from_ls_type(
    typ: str | None, value: dict[str, Any] | None = None
) -> str:
    """v0.10.17 · LabelStudio result.type → tool_unit_id.

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
    """v0.10.17 · 从 prediction.result 数组首条 shape.type 派生 tool_unit_id."""
    if not result:
        return "bbox"
    first = result[0] if isinstance(result[0], dict) else {}
    explicit = first.get("tool_unit_id")
    if explicit == "ai_interactive":
        # 退役值: 忽略, 落到下方按几何/LS type 派生 (与迁移 0116 归位同规则)。
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
    """v0.21.1 · 检测式视频追踪 result item → 内部 `VideoTrackGeometry` 形态.

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
    """v0.9.7 fix · LabelStudio 标准 result shape → 内部前端 schema.

    Worker 把 ML backend 返回的 LabelStudio 标准 ``{type, value, score}`` 原样
    存入 ``predictions.result``; 前端 ``predictionsToBoxes``
    (apps/web/.../transforms.ts) 期望 ``{type, class_name, geometry, confidence}``.
    历史 v0.9.4 phase 1 后端真正接通 SAM/DINO 时引入这个 schema gap, 一直未发现
    (因为前端工作台未真实跑过预标 → 渲染候选). 本 adapter 在 read 路径补这层转换;
    DB 维持 LabelStudio 标准 (与导出 / CVAT 等通用工具兼容).

    兼容旧格式: 已有 ``geometry`` 字段时 pass-through, 不做二次转换.
    v0.10.17 · 返回字典含 ``tool_unit_id``, 由 LS type 派生.
    v0.14.9 · 协议 v2: 返回字典含 ``attributes`` (从 shape 顶层 ``s["attributes"]`` 提取),
    供 OCR / doc_layout 富属性 (text / language / orientation) 透传到 annotation.attributes;
    非 OCR shape 为 ``{}``。
    """
    if not isinstance(s, dict):
        return {}
    if "geometry" in s:
        # Normalize missing/retired tool IDs from geometry while preserving dict identity.
        if s.get("tool_unit_id") not in TOOL_UNIT_IDS:
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

    # v0.14.9 · 协议 v2 OCR / doc_layout: backend 在 shape 顶层写
    # `attributes: {text, language?, orientation?, ...}` (与几何 value 同级)。
    # 这里原样提取, 供 accept 路径透传到 annotation.attributes; 无则 {}。
    raw_attributes = s.get("attributes")
    attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}

    # v0.21.1 · 检测式视频追踪 result item: 与扁平单几何不同, 是**嵌套聚合轨迹**
    # `{type, track_id, class_name, keyframes:[{frame_index, bbox:{x,y,w,h}, ...}]}`。
    # class_name 在顶层 (非 value.{type}); 坐标已归一 0-1 (backend 直返, 见协议约定),
    # **不走 _percent_scale 自动探测** —— 小目标 bbox 各分量可能全 <1, 探测会误判为百分比。
    # track_id 应已在 worker ingestion 阶段由原生 int 映射成 `trk_<uuid>` (读路径纯函数、
    # 每次调用不得再生 uuid, 否则轨迹身份漂移)。此处只做形态重塑, 不做映射。
    if typ == "video_track_bbox":
        return _track_to_internal_shape(s, confidence, attributes)

    # LabelStudio 字段名约定: value.{type} 是 label 数组 (rectanglelabels/polygonlabels/...)
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
        # v0.9.14 · 三种 LS shape 兼容:
        #   ① 旧: {points: [[x,y]...]}                      → polygon (无 hole)
        #   ② 新单连通带 hole: {points, holes: [[[x,y]...]]} → polygon (带 hole)
        #   ③ 新多连通: {polygons: [{points, holes?}, ...]}  → multi_polygon
        # ② / ③ 由 grounded-sam2-backend predictor.py 在 mask 多连通或带 hole 时输出.
        # 老路径 (无 holes / polygons) 输出字面与 v0.9.13 之前完全一致, 不写 holes 字段
        # 给老 fixture / 老 DB JSONB 持续兼容; PolygonGeometry.holes 默认 [] 由 Pydantic 补.
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
        # v0.10.17 · 与 prediction.tool_unit_id 派生同源.
        "tool_unit_id": derive_tool_unit_from_ls_type(typ, val),
        # v0.14.9 · OCR / doc_layout 富属性 (text / language / orientation 等); 非 OCR 为 {}。
        "attributes": attributes,
    }


def to_video_bbox_result(raw_result: list[dict], frame_index: int) -> list[dict]:
    """v0.21.4 / v0.21.7 · 图像 backend 检测结果 (LS 标准 shape) → 单帧 ``video_bbox`` 内部几何。

    复用 ``to_internal_shape`` 把 ``rectanglelabels`` 归一到 0-1 的 bbox (内含 ``_percent_scale``
    自动兼容 backend 的 0-1 / 0-100 两种口径, 与图像检测候选完全同源), 再套一层 ``frame_index``
    改写成 ``video_bbox``。**只收 bbox 几何**——非 bbox (旋转框 / polygon / keypoint 等) 跳过,
    视频单帧几何目前只有 ``video_bbox``。

    ``VideoBboxGeometry`` 是 ``extra="forbid"``: geometry dict 只放 ``type/frame_index/x/y/w/h``;
    ``class_name`` / ``confidence`` 放在 result item 顶层 (accept 从顶层读 ``type`` 决定
    annotation_type, 见 ``services/annotation.py``)。

    v0.21.4 单帧工作台 (ml_backends.predict-frame) 与 v0.21.7 逐帧批量 fan-out (workers 段任务)
    共用这一个 reshaper —— 故从 API 模块提升到 services 层, 两侧同源。
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


def _remap_track_ids(result: list[dict]) -> list[dict]:
    """v0.21.1 · 检测式追踪 result item 的原生 int ``track_id`` → ``trk_<uuid>`` (ingestion
    一次性映射)。

    必须在 **worker 存 ``prediction.result`` 之前**做, 而非读路径 ``to_internal_shape`` ——
    后者每次 read 都跑, 在其中 ``uuid4()`` 会导致轨迹身份逐次漂移。原 int 记进
    ``semantic_label`` (如 ``car_3``, 供跨 task Re-ID 心智)。非 ``video_track_bbox`` item 原样;
    已是 ``trk_`` 字符串 (幂等 / 重入) 不重映射。返回新列表, 不改传入对象。
    """
    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict) or item.get("type") != "video_track_bbox":
            out.append(item)
            continue
        raw_tid = item.get("track_id")
        if isinstance(raw_tid, str) and raw_tid.startswith("trk_"):
            out.append(item)
            continue
        new_item = dict(item)
        new_item["track_id"] = _new_track_id()
        if item.get("semantic_label") is None and raw_tid is not None:
            cls = item.get("class_name") or "obj"
            new_item["semantic_label"] = f"{cls}_{raw_tid}"
        out.append(new_item)
    return out


class PredictionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_from_ml_result(
        self,
        task_id: uuid.UUID,
        project_id: uuid.UUID,
        ml_backend_id: uuid.UUID | None,
        result: list[dict],
        score: float | None = None,
        model_version: str | None = None,
        inference_time_ms: int | None = None,
        token_meta: dict | None = None,
        source: str = "ml_backend",
        pipeline_extra: dict | None = None,
        ml_backend_pool_id: uuid.UUID | None = None,
        model_provenance: dict | None = None,
        reference_receipt=None,
        native_reference_receipt=None,
        repair_reference_receipt=None,
        background_native_reference_receipt=None,
        original_receipt=None,
        native_original_receipt=None,
        repair_original_receipt=None,
        video_input_receipt=None,
        video_result_receipt=None,
    ) -> Prediction:
        # v0.21.1 · 检测式追踪原生 int track_id → trk_<uuid> (读路径不得再生 uuid, 见 _remap_track_ids)。
        from app.services.reference_access import reject_reference_scope

        reference_evidence = None
        original_evidence = None
        video_evidence = None
        native_qualification = None
        if video_input_receipt is not None:
            from app.services.video_prediction_references import validate_result_receipt
            from app.services.content_snapshot import ContentError

            if (
                source != "ml_backend"
                or video_result_receipt is None
                or any(
                    receipt is not None
                    for receipt in (
                        reference_receipt,
                        native_reference_receipt,
                        repair_reference_receipt,
                        background_native_reference_receipt,
                        original_receipt,
                        native_original_receipt,
                        repair_original_receipt,
                    )
                )
            ):
                raise ContentError("prediction_video_receipt_invalid")
            if getattr(video_result_receipt, "anchor", None) is not video_input_receipt:
                raise ContentError("prediction_video_receipt_invalid")
            video_evidence = await validate_result_receipt(
                self.db,
                task_id,
                project_id,
                video_result_receipt,
                dict(
                    task_id=task_id,
                    project_id=project_id,
                    ml_backend_id=ml_backend_id,
                    result=result,
                    score=score,
                    model_version=model_version,
                    inference_time_ms=inference_time_ms,
                    token_meta=token_meta,
                    source=source,
                    pipeline_extra=pipeline_extra,
                    ml_backend_pool_id=ml_backend_pool_id,
                    model_provenance=model_provenance,
                ),
            )
        elif video_result_receipt is not None:
            from app.services.content_snapshot import ContentError

            raise ContentError("prediction_video_receipt_invalid")
        elif repair_original_receipt is not None:
            from app.services.managed_mask_repair import validate_receipt
            from app.services.content_snapshot import ContentError

            if (
                any(
                    value is not None
                    for value in (
                        reference_receipt,
                        native_reference_receipt,
                        repair_reference_receipt,
                        background_native_reference_receipt,
                        original_receipt,
                        native_original_receipt,
                    )
                )
                or source != "mask_repair"
                or (token_meta or {}).get("mask_objects") is not None
                or len(result) != 1
                or (result[0].get("geometry") or {}).get("type") != "raster_mask"
            ):
                raise ContentError("repair_original_receipt_invalid")
            original_evidence = await validate_receipt(
                self.db,
                task_id,
                None,
                repair_original_receipt,
                "prediction",
                result,
                project_id=project_id,
            )
        elif native_original_receipt is not None:
            from app.services.managed_native_inference import validate_native_receipt
            from app.services.content_snapshot import ContentError

            if (
                any(
                    receipt is not None
                    for receipt in (
                        original_receipt,
                        reference_receipt,
                        native_reference_receipt,
                        repair_reference_receipt,
                        background_native_reference_receipt,
                    )
                )
                or source != INTERACTIVE_ACCEPT_PREDICTION_SOURCE
                or (token_meta or {}).get("mask_objects") is not None
            ):
                raise ContentError("prediction_original_receipt_invalid")
            original_evidence = await validate_native_receipt(
                self.db, task_id, project_id, native_original_receipt, result=result
            )
        elif original_receipt is not None:
            from app.services.prediction_originals import validate_receipt
            from app.services.content_snapshot import ContentError

            if (
                reference_receipt is not None
                or native_reference_receipt is not None
                or repair_reference_receipt is not None
                or background_native_reference_receipt is not None
                or source != "ml_backend"
            ):
                raise ContentError("prediction_original_receipt_invalid")
            original_evidence = await validate_receipt(
                self.db, task_id, project_id, original_receipt
            )
        elif background_native_reference_receipt is not None:
            from app.services.reference_native_predictions import validate_receipt
            from app.services.content_snapshot import ContentError

            if (
                reference_receipt is not None
                or native_reference_receipt is not None
                or repair_reference_receipt is not None
                or source != "ml_backend"
            ):
                raise ContentError("background_native_reference_receipt_invalid")
            reference_evidence, native_qualification = await validate_receipt(
                self.db,
                task_id,
                project_id,
                background_native_reference_receipt,
                dict(
                    task_id=task_id,
                    project_id=project_id,
                    ml_backend_id=ml_backend_id,
                    result=result,
                    score=score,
                    model_version=model_version,
                    inference_time_ms=inference_time_ms,
                    token_meta=token_meta,
                    source=source,
                    pipeline_extra=pipeline_extra,
                    ml_backend_pool_id=ml_backend_pool_id,
                    model_provenance=model_provenance,
                ),
            )
        elif repair_reference_receipt is not None:
            from app.services.reference_mask_repair import validate_receipt
            from app.services.content_snapshot import ContentError

            if (
                reference_receipt is not None
                or native_reference_receipt is not None
                or source != "mask_repair"
                or (token_meta or {}).get("mask_objects") is not None
                or len(result) != 1
                or (result[0].get("geometry") or {}).get("type") != "raster_mask"
            ):
                raise ContentError("repair_reference_receipt_invalid")
            reference_evidence = await validate_receipt(
                self.db,
                task_id,
                None,
                repair_reference_receipt,
                "prediction",
                result,
                project_id=project_id,
            )
        elif native_reference_receipt is not None:
            from app.services.reference_inference import validate_native_receipt
            from app.services.content_snapshot import ContentError

            if (
                reference_receipt is not None
                or source != INTERACTIVE_ACCEPT_PREDICTION_SOURCE
                or (token_meta or {}).get("mask_objects") is not None
            ):
                raise ContentError("prediction_reference_receipt_invalid")
            reference_evidence = await validate_native_receipt(
                self.db, task_id, project_id, native_reference_receipt, result=result
            )
        elif reference_receipt is None:
            await reject_reference_scope(self.db, task_ids=[task_id])
        else:
            from app.services.prediction_references import validate_receipt

            reference_evidence = await validate_receipt(
                self.db, task_id, project_id, reference_receipt
            )
            from app.services.raster_mask_storage import collect_mask_geometries

            if (
                (token_meta or {}).get("mask_objects") is not None
                or collect_mask_geometries(result)
                or any(
                    isinstance(shape, dict) and shape.get("type") == "brushlabels"
                    for shape in result
                )
            ):
                from app.services.content_snapshot import ContentError

                raise ContentError("reference_prediction_geometry_not_supported")
        result = _remap_track_ids(result)
        task = await self.db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            task is not None
            and task.file_type == "image"
            and source == INTERACTIVE_ACCEPT_PREDICTION_SOURCE
            and native_original_receipt is None
        ):
            from app.services.content_snapshot import ContentError
            from app.services.reference_keys import is_reference

            if not is_reference(task.file_path):
                raise ContentError("prediction_requires_original_provenance")
        if task is not None and task.file_type == "image" and source == "mask_repair":
            from app.services.reference_keys import is_reference
            from app.services.content_snapshot import ContentError

            if not is_reference(task.file_path) and repair_original_receipt is None:
                raise ContentError("repair_original_requires_new_plan")
        if (
            task is not None
            and task.file_type == "video"
            and source == "ml_backend"
            and result
            and video_evidence is None
        ):
            from types import SimpleNamespace
            from app.services.video_prediction_acceptance import requires_qualification
            from app.services.content_snapshot import ContentError

            if requires_qualification(SimpleNamespace(source=source, result=result)):
                raise ContentError("prediction_video_requires_new_request")
        await prepare_mask_payload_for_write(
            self.db,
            task,
            result,
            portable_mask_meta=token_meta
            if (token_meta or {}).get("mask_objects") is not None
            else None,
            native_original_receipt=native_original_receipt,
            repair_original_receipt=repair_original_receipt,
            video_result_receipt=video_result_receipt,
        )
        prediction = Prediction(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            # v0.23.3 ADR-0050 §5.4 · requested pool (单阶段 = 请求的池; 多阶段 = root stage pool)。
            ml_backend_pool_id=ml_backend_pool_id,
            model_version=model_version,
            score=score,
            # v0.10.17 · 按 result[0].type 派生 tool_unit_id.
            tool_unit_id=derive_tool_unit_from_result(result),
            result=result,
            source=source,
        )
        self.db.add(prediction)
        await self.db.flush()

        meta_data = token_meta or {}
        meta = PredictionMeta(
            id=uuid.uuid4(),
            prediction_id=prediction.id,
            # v0.10.25 · 复合 FK 冗余分区键；flush 后 prediction.created_at 已由 RETURNING 回写。
            prediction_created_at=prediction.created_at,
            inference_time_ms=inference_time_ms,
            prompt_tokens=meta_data.get("prompt_tokens"),
            completion_tokens=meta_data.get("completion_tokens"),
            total_tokens=meta_data.get("total_tokens"),
            prompt_cost=meta_data.get("prompt_cost"),
            completion_cost=meta_data.get("completion_cost"),
            total_cost=meta_data.get("total_cost"),
            # v0.18.1 · 多阶段预标注阶段元信息 (stage_count / enriched_attr_keys),
            # 追溯「哪个属性来自哪个阶段」; MVP 暂存 extra JSONB 不改表。
            extra={
                **{
                    key: value
                    for key, value in (pipeline_extra or {}).items()
                    if key
                    not in {
                        "reference_input",
                        "reference_execution",
                        "reference_native",
                        "original_input",
                        "original_video_input",
                        "video_prediction_proof",
                    }
                },
                **(
                    {"reference_input": reference_evidence}
                    if reference_evidence
                    else {}
                ),
                **({"original_input": original_evidence} if original_evidence else {}),
                **(
                    {"original_video_input": _public_video_provenance(video_evidence)}
                    if video_evidence is not None
                    else {}
                ),
                **({"trained_model": model_provenance} if model_provenance else {}),
                **(
                    {"inference_runtime": meta_data["inference_runtime"]}
                    if model_provenance and "inference_runtime" in meta_data
                    else {}
                ),
                **(
                    {"segmentation": meta_data["segmentation"]}
                    if "segmentation" in meta_data
                    else {}
                ),
            },
        )
        if native_qualification is not None:
            from app.services.reference_native_predictions import (
                PROOF_KEY,
                persisted_qualification,
            )

            meta.extra = {
                **meta.extra,
                PROOF_KEY: persisted_qualification(
                    prediction, meta.extra, native_qualification
                ),
            }
        if video_evidence is not None:
            from app.services.video_prediction_acceptance import (
                PROOF_KEY as VIDEO_PROOF_KEY,
                persisted_qualification as qualify_video_prediction,
            )

            meta.extra = {
                **meta.extra,
                VIDEO_PROOF_KEY: qualify_video_prediction(
                    prediction, meta.extra, video_evidence
                ),
            }
        self.db.add(meta)

        await self.db.execute(select(Task).where(Task.id == task_id).with_for_update())
        task = await self.db.get(Task, task_id)
        if task:
            task.total_predictions = (task.total_predictions or 0) + 1

        await self.db.flush()
        if repair_original_receipt is not None:
            from app.services.managed_mask_repair import recheck_credentials

            await recheck_credentials(self.db, repair_original_receipt)
        if video_input_receipt is not None:
            await video_input_receipt.owner.recheck_credentials()
        return prediction

    async def create_failed(
        self,
        task_id: uuid.UUID | None,
        project_id: uuid.UUID,
        ml_backend_id: uuid.UUID | None,
        error_type: str,
        message: str,
        model_version: str | None = None,
        extra: dict | None = None,
        ml_backend_pool_id: uuid.UUID | None = None,
    ) -> FailedPrediction:
        failed = FailedPrediction(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=ml_backend_id,
            # v0.23.3 ADR-0050 §5.4 · 失败所在的 requested pool; 选择前失败 instance id 可为 null。
            ml_backend_pool_id=ml_backend_pool_id,
            model_version=model_version,
            error_type=error_type,
            message=message,
            extra=extra or {},
        )
        self.db.add(failed)
        await self.db.flush()
        return failed

    async def list_by_task(
        self, task_id: uuid.UUID, model_version: str | None = None
    ) -> list[Prediction]:
        q = select(Prediction).where(Prediction.task_id == task_id)
        if model_version:
            q = q.where(Prediction.model_version == model_version)
        q = q.order_by(Prediction.created_at.desc())
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def get_latest_for_task(self, task_id: uuid.UUID) -> Prediction | None:
        result = await self.db.execute(
            select(Prediction)
            .where(Prediction.task_id == task_id)
            .order_by(Prediction.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
