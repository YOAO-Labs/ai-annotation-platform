"""Public contracts for project training data versions."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from training_contract.dataset import DETECTION
from aap_task_contract import (
    VisualTask,
    legacy_training_task,
    normalize_task,
    normalize_task_fields,
)

from app.schemas.dataset_view import DatasetItemFilters


class TrainingDatasetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Literal["detect", "segment"] = DETECTION
    bridge: (
        Literal[
            "ultralytics-bbox-1",
            "ultralytics-simple-polygon-1",
            "ultralytics-instance-rle-1",
        ]
        | None
    ) = None
    batch_id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    dataset_filter: DatasetItemFilters | None = None
    task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20000)
    mode: Literal["reviewed", "manager_confirmed"] = "reviewed"
    confirmed_negative_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=20000
    )
    seed: int = Field(default=42, ge=0, le=2147483647)
    validation_fraction: float = Field(default=0.2, ge=0.05, le=0.5)
    parent_id: uuid.UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def read_task_aliases(cls, value):
        return normalize_task_fields(value)

    def canonical_payload(self):
        payload = self.model_dump(mode="json")
        if payload.get("dataset_id") is None:
            payload.pop("dataset_id")
        if self.dataset_filter is None:
            payload.pop("dataset_filter", None)
        else:
            payload["dataset_filter"] = self.dataset_filter.model_dump(
                mode="json", exclude_none=True
            )
        if payload.get("bridge") is None:
            payload.pop("bridge")
        if self.task == DETECTION:
            payload.pop("task")
        else:
            payload["task"] = legacy_training_task(self.task)
        return payload

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if self.dataset_filter is not None and self.dataset_id is None:
            raise ValueError("筛选范围必须指定数据集")
        if self.bridge is not None and self.bridge not in (
            {"ultralytics-bbox-1"}
            if self.task == DETECTION
            else {"ultralytics-simple-polygon-1", "ultralytics-instance-rle-1"}
        ):
            raise ValueError("训练桥接与任务不匹配")
        if self.mode == "manager_confirmed" and not self.task_ids:
            raise ValueError("负责人确认模式必须显式选择任务")
        if self.confirmed_negative_ids and (
            self.mode != "manager_confirmed"
            or not set(self.confirmed_negative_ids) <= set(self.task_ids)
        ):
            raise ValueError("负样本确认必须属于显式选择的任务")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("任务列表不能重复")
        return self


class TrainingDatasetCreate(TrainingDatasetSelection):
    name: str = Field(min_length=1, max_length=120)
    request_id: uuid.UUID
    preflight_digest: str = Field(min_length=64, max_length=64)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip():
            raise ValueError("数据版本名称不能为空")
        return value.strip()


class TrainingDatasetPreflight(BaseModel):
    ready: bool
    digest: str
    classes: list[str]
    summary: dict[str, int]
    issues: list[dict]
    excluded: list[dict]
    task: Literal["detect", "segment"] = DETECTION
    media_type: Literal["image"] = "image"
    format: str = "ultralytics-yolo"
    annotation_scope: dict = Field(default_factory=dict)
    source: dict = Field(default_factory=dict)


class TrainingDatasetContractProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: int = Field(alias="schema")
    task: VisualTask
    media_type: str | None
    source_media_type: str | None
    format: str
    geometry: str
    bridge: str


class TrainingDatasetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    task_dataset_id: uuid.UUID | None = None
    version_ordinal: int | None = None
    contract_profile: TrainingDatasetContractProfile | None = None
    task: Literal["detect", "segment"] = DETECTION
    media_type: str = "image"
    format: str = "ultralytics-yolo"
    source_media_type: str = "image"
    summary: dict = Field(default_factory=dict)
    class_count: int = 0
    annotation_scope: dict = Field(default_factory=dict)

    @field_validator("task", mode="before")
    @classmethod
    def canonical_task(cls, value):
        return normalize_task(value)

    creator_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    async_job_id: uuid.UUID | None
    status: str
    progress_pct: int
    snapshot_digest: str
    error_code: str | None
    error_message: str | None
    size_bytes: int | None
    package_sha256: str | None
    created_at: datetime
    completed_at: datetime | None


class TrainingDatasetOut(TrainingDatasetSummary):
    manifest: dict


class TrainingDatasetPage(BaseModel):
    items: list[TrainingDatasetSummary]
    total: int
