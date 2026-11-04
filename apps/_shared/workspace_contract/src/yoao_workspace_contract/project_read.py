"""Frozen, language-neutral snapshot of Workspace-owned project fields."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)


class WorkspaceProjectSnapshot(BaseModel):
    """Raw project fields owned by Workspace, without presentation or Labeling data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: UUID = Field(strict=False)
    organization_id: UUID | None = Field(strict=False)
    display_id: StrictStr
    name: StrictStr
    type_label: StrictStr
    type_key: StrictStr
    data_type: StrictStr
    owner_id: UUID = Field(strict=False)
    status: StrictStr
    ai_enabled: StrictBool
    tool_bindings: dict[str, Any]
    label_config: dict[str, Any]
    sampling: StrictStr
    maximum_annotations: StrictInt
    show_overlap_first: StrictBool
    iou_dedup_threshold: StrictFloat
    box_threshold: StrictFloat
    text_threshold: StrictFloat
    ai_interactive_enabled: StrictBool
    raster_mask_native_editing_enabled: StrictBool
    mask_qc_config: dict[str, Any]
    point_cloud_quality_config: dict[str, Any]
    rendering_config: dict[str, Any]
    video_sampling: dict[str, Any]
    video_collaboration: dict[str, Any]
    scene_mode: StrictBool
    prefer_same_scene_continuation: StrictBool
    scene_continuation_window_min: StrictInt
    default_variants: dict[str, Any]
    preannotate_pipeline: list[Any] | None
    annotation_guide: StrictStr | None
    guide_assets: list[Any]
    guide_assets_revision: StrictInt = Field(ge=1)
    model_version: StrictStr | None
    task_lock_ttl_seconds: StrictInt
    due_date: date | None = Field(strict=False)
    created_at: datetime = Field(strict=False)
    updated_at: datetime = Field(strict=False)
