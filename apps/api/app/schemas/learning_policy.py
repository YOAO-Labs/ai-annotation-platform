"""Explicit project policy settings; no automatic threshold is guessed."""

import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.training_run import (
    TrainingParameters,
    TrainingConfiguration,
    TrainingOrigin,
)
from app.schemas.training_initialization import TrainingInitialization


class LearningPolicySave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    task: Literal["detect", "segment"] = "detect"
    enabled: bool = False
    parent_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    framework: Literal["ultralytics", "libreyolo"] = "ultralytics"
    base_model: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parameters: TrainingParameters = Field(default_factory=TrainingParameters)
    configuration: TrainingConfiguration | None = None
    initialization: TrainingInitialization | None = None
    origin: TrainingOrigin | None = None
    min_changed_samples: int = Field(ge=1, le=20000)
    check_interval_minutes: int = Field(ge=1, le=1440)
    cooldown_minutes: int = Field(ge=0, le=10080)

    @model_validator(mode="after")
    def one_parameter_source(self):
        if self.configuration is not None and "parameters" in self.model_fields_set:
            raise ValueError("configuration 与旧 parameters 不能同时提交")
        if self.origin is not None and (
            self.origin.kind != "recipe" or self.configuration is None
        ):
            raise ValueError("策略配方必须携带完整配置及具体修订")
        if (
            self.initialization
            and self.initialization.kind == "trained_model"
            and self.configuration is None
        ):
            raise ValueError("项目权重初始化必须携带完整配置")
        return self


class LearningPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    task: Literal["detect", "segment"] = "detect"
    creator_id: uuid.UUID | None
    revision: int
    enabled: bool
    config: dict
    initialization_model_id: uuid.UUID | None = None
    baseline_dataset_id: uuid.UUID | None
    blocked_train_digest: str | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    last_started_at: datetime | None
    state: str
    message: str | None


class LearningTriggerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_revision: int
    dataset_id: uuid.UUID
    status: str
    candidate_digest: str | None
    train_digest: str | None
    validation_digest: str | None
    changed_samples: int
    message: str | None
    created_at: datetime
    completed_at: datetime | None
    run_id: uuid.UUID | None = None


class LearningTriggerPage(BaseModel):
    total: int
    items: list[LearningTriggerOut]
