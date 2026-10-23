from app.schemas._jsonb_types import (
    BboxGeometry,
    KeypointGeometry,
    MultiPolygonGeometry,
    PolygonGeometry,
    PolylineGeometry,
    RasterMaskGeometry,
    RotatedBboxGeometry,
    ToolUnitId,
)
from pydantic import BaseModel
from typing import Any, Literal, Union
from uuid import UUID
from datetime import datetime


# v0.9.11 · PredictionShape — DB 存 LabelStudio 标准 {type, value, score};
# read 路径 to_internal_shape 转 {type, class_name, geometry, confidence} 后由 PredictionOut 暴露.
# 前端 codegen 据此派生 (替代 src/types/index.ts 手写). 复用 _jsonb_types.{Bbox,Polygon,MultiPolygon}Geometry,
# 保持与 AnnotationResponse.geometry 类型一致 (frontend 的 Geometry alias 同时覆盖 annotation + prediction).
# Union 含 dict fallback 兼容未知 LS 类型 (e.g. keypoints) 的 to_internal_shape 空 geometry.
# v0.9.14 · 新增 MultiPolygonGeometry 分支支持 mask 多连通域 / 空洞.
class PredictionShape(BaseModel):
    type: str  # LabelStudio 类型: rectanglelabels | polygonlabels | ...
    class_name: str
    geometry: Union[
        BboxGeometry,
        PolygonGeometry,
        MultiPolygonGeometry,
        RotatedBboxGeometry,
        PolylineGeometry,
        KeypointGeometry,
        RasterMaskGeometry,
        dict[str, Any],
    ]
    confidence: float
    # v0.14.9 · OCR / doc_layout 识别属性 (attributes.text / language / orientation);
    # 由 to_internal_shape 从 result shape 顶层提取, 非 OCR 时为 {}. read 路径透出供
    # 工作台候选展示文本摘要 + 采纳时写入 annotation.attributes.
    attributes: dict[str, Any] = {}
    # 原始 predictions.result 数组下标。GET /predictions 可能过滤/重排 shape,
    # 采纳时必须把这个原始下标传回 /accept?shape_index=...
    shape_index: int | None = None
    # Mixed-stage candidates retain each shape's geometry-derived tool binding.
    tool_unit_id: ToolUnitId | None = None


class PredictionOut(BaseModel):
    id: UUID
    task_id: UUID
    project_id: UUID
    ml_backend_id: UUID | None = None
    model_version: str | None = None
    score: float | None = None
    source: str | None = None
    # v0.10.17 · 工具单位; to_internal_shape 按 result.type 推断 (rectanglelabels→bbox,
    # polygonlabels/multipolygonlabels→region, point/line→bbox 占位).
    tool_unit_id: ToolUnitId = "bbox"
    result: list[PredictionShape]
    cluster: int | None = None
    created_at: datetime
    # v0.9.5 · 工作台 AIInspectorPanel 单条费用 / 推理时间透传（PredictionMeta join）
    inference_time_ms: int | None = None
    total_cost: float | None = None

    class Config:
        from_attributes = True


class PredictionMetaOut(BaseModel):
    inference_time_ms: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None

    class Config:
        from_attributes = True


PredictionSourceScope = Literal["ml_backend", "external_import", "all"]


class PredictionPurgeRequest(BaseModel):
    source_scope: PredictionSourceScope = "external_import"
    task_ids: list[UUID] | None = None
    dry_run: bool = False


class PredictionPurgeCounts(BaseModel):
    ml_backend: int = 0
    external_import: int = 0
    unknown: int = 0
    total: int = 0


class PredictionPurgeResponse(BaseModel):
    source_scope: PredictionSourceScope
    task_ids: list[UUID] | None = None
    dry_run: bool
    counts: PredictionPurgeCounts
