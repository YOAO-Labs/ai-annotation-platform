"""Request and response models for dataset maintenance operations."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

LidarAxisConvention = Literal[
    "iso_8855",
    "ros_rep103",
    "kitti_camera",
    "opencv_camera",
    "apollo",
    "y_forward",
    "sustechpoints_demo",
    "raw",
]


class _DatasetOperationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", protected_namespaces=("model_validate", "model_dump")
    )


class _DatasetOperationResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow", protected_namespaces=("model_validate", "model_dump")
    )


class DatasetImportFromConnectionRequest(_DatasetOperationRequest):
    connection_id: UUID
    source_path: str = ""
    recursive: bool = True
    include_globs: list[str] | None = None


class DatasetImportFromConnectionResult(_DatasetOperationResponse):
    job_id: UUID


class DatasetBackfillDimensionsResult(_DatasetOperationResponse):
    processed: int
    failed: int
    remaining_hint: bool


class DatasetBackfillMediaResult(_DatasetOperationResponse):
    status: str
    dataset_id: UUID


class DatasetBackfillScenesResult(_DatasetOperationResponse):
    dataset_id: UUID
    created_scenes: int
    assigned_items: int
    skipped_items: int
    dry_run: bool
    notes: list[str] = Field(default_factory=list)


class DatasetScanItemsResult(_DatasetOperationResponse):
    status: str
    new_items: int
    linked_tasks: int


class DatasetSniffAxisCandidate(_DatasetOperationResponse):
    convention: LidarAxisConvention
    score: float


class DatasetSniffAxisCamera(_DatasetOperationResponse):
    camera_role: str | None = None
    best: LidarAxisConvention
    score: float


class DatasetSniffAxisResult(_DatasetOperationResponse):
    best: LidarAxisConvention | None = None
    score: float | None = None
    candidates: list[DatasetSniffAxisCandidate] = Field(default_factory=list)
    source: Literal["task_link", "dataset_item"] | None = None
    camera_role: str | None = None
    camera_item_id: UUID | None = None
    per_camera: list[DatasetSniffAxisCamera] | None = None
    agreement: float | None = None
