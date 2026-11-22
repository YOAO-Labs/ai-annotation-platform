from yoao_workspace.configuration_values import ToolUnitId as _ToolUnitId
from app.schemas._jsonb_types import (
    BboxGeometry,
    KeypointGeometry,
    MultiPolygonGeometry,
    PolygonGeometry,
    PolylineGeometry,
    RasterMaskGeometry,
    RotatedBboxGeometry,
)
from pydantic import BaseModel
from typing import Any, Literal, Union
from uuid import UUID
from datetime import datetime


class PredictionShape(BaseModel):
    type: str
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
    attributes: dict[str, Any] = {}
    shape_index: int | None = None
    tool_unit_id: _ToolUnitId | None = None


class PredictionOut(BaseModel):
    id: UUID
    task_id: UUID
    project_id: UUID
    ml_backend_id: UUID | None = None
    model_version: str | None = None
    score: float | None = None
    source: str | None = None
    tool_unit_id: _ToolUnitId = "bbox"
    result: list[PredictionShape]
    cluster: int | None = None
    created_at: datetime
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
