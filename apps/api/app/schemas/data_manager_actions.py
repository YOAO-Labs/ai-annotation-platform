from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.export import ExportRequestBody


MAX_DATA_MANAGER_TASK_IDS = 200


class DataManagerTaskSelection(BaseModel):
    task_ids: list[UUID] = Field(
        ..., min_length=1, max_length=MAX_DATA_MANAGER_TASK_IDS
    )

    @model_validator(mode="after")
    def canonicalize_task_ids(self) -> DataManagerTaskSelection:
        self.task_ids = sorted(set(self.task_ids), key=str)
        return self


class DataManagerTaskExportRequest(ExportRequestBody, DataManagerTaskSelection):
    targets: list[str] = Field(..., min_length=1, max_length=20)
    include_attributes: bool = True
    video_frame_mode: Literal["keyframes", "all_frames"] = "keyframes"
    axis_frame: Literal["iso", "source"] = "iso"
    indexed_overlap_policy: Literal[
        "error", "z_order", "larger_area", "smaller_area"
    ] = "error"
    video_overlap_policy: Literal["error", "z_order", "larger_area", "smaller_area"] = (
        "error"
    )
    mots_frame_base: Literal[0, 1] = 0

    @model_validator(mode="after")
    def reject_video_scope(self) -> DataManagerTaskExportRequest:
        if self.scope is not None:
            raise ValueError("task-scoped export cannot include a video scope")
        return self
