"""Reusable configuration inputs exclude datasets, runtime and executor state."""

import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.training_run import TrainingConfiguration, TrainingOrigin
from app.schemas.training_initialization import TrainingInitialization


class TrainingConfigurationResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framework: Literal["ultralytics", "libreyolo"]
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    configuration: TrainingConfiguration
    initialization: TrainingInitialization | None = None


class TrainingRecipeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    expected_head_revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    configuration: TrainingConfigurationResolve | None = None
    source_run_id: uuid.UUID | None = None
    source_attempt_id: uuid.UUID | None = None
    origin: TrainingOrigin | None = None

    def request_payload(self):
        payload = self.model_dump(mode="json")
        configuration = payload.get("configuration")
        if configuration is not None:
            source = self.configuration.initialization
            if source is None or source.kind == "catalog":
                configuration.pop("initialization")
            else:
                configuration["initialization"] = source.model_dump(
                    mode="json", exclude_none=True
                )
        return payload

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("配方名称不能为空")
        return value.strip()

    @model_validator(mode="after")
    def one_source(self):
        if (self.configuration is None) == (self.source_run_id is None):
            raise ValueError("请选择当前配置或已验证实验作为唯一来源")
        if self.source_attempt_id and not self.source_run_id:
            raise ValueError("执行尝试必须属于来源实验")
        if self.source_run_id and self.origin:
            raise ValueError("实验来源不能同时提供其他来源")
        return self


class TrainingRecipeRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    recipe_id: uuid.UUID
    project_id: uuid.UUID
    creator_id: uuid.UUID | None
    ordinal: int
    name: str
    description: str
    snapshot: dict
    snapshot_digest: str
    origin: dict | None
    initialization_model_id: uuid.UUID | None = None
    created_at: datetime


class TrainingRecipeOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    head_revision: int
    archived: bool
    revision: TrainingRecipeRevisionOut
    compatible: bool
    incompatibility: str | None
    source_available: bool


class TrainingRecipePage(BaseModel):
    total: int
    items: list[TrainingRecipeOut]


class TrainingReuseCandidate(BaseModel):
    snapshot: dict
    snapshot_digest: str
    origin: TrainingOrigin
    label: str
    compatible: bool
    incompatibility: str | None
    source_dataset_id: uuid.UUID | None = None
