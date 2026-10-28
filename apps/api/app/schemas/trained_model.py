from aap_task_contract import normalize_task
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TrainedModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    bindings: list[str] = Field(min_length=1, max_length=1000)


class TrainedModelOut(BaseModel):
    task: str = "detect"
    media_type: str = "image"

    @model_validator(mode="after")
    def canonical_task_view(self):
        self.task = normalize_task(self.provenance.get("task", "detect"))
        return self

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    name: str
    framework: str
    framework_version: str
    artifact: dict
    classes: list[str]
    bindings: list[str]
    binding_digest: str
    provenance: dict
    evaluation: dict
    created_at: datetime
    disposition: str | None = None
    decision_sequence: int = 0
    archived: bool = False
    is_current_release: bool = False


class ModelDeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: uuid.UUID


class ModelDeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_version_id: uuid.UUID
    registry_id: uuid.UUID
    status: Literal["checking", "ready", "failed", "draining", "retired"]
    receipt: dict
    trial: dict
    error_message: str | None
    checked_at: datetime | None
    created_at: datetime


class ModelReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    expected_generation: int = Field(ge=0)
    model_version_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None
    rollback_from_id: uuid.UUID | None = None
    sample_confirmed: bool = False
    task: Literal["detect", "segment"] = "detect"

    def request_payload(self):
        payload = self.model_dump(mode="json")
        if self.task == "detect":
            payload.pop("task")
        return payload

    @model_validator(mode="after")
    def require_pair(self):
        if (self.model_version_id is None) != (self.deployment_id is None):
            raise ValueError("模型版本与部署必须同时指定")
        if self.model_version_id and not self.sample_confirmed:
            raise ValueError("发布前须确认样本试用结果")
        return self


class ModelReleaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    model_version_id: uuid.UUID | None
    deployment_id: uuid.UUID | None
    previous_release_id: uuid.UUID | None
    rollback_from_id: uuid.UUID | None
    generation: int
    task: Literal["detect", "segment"] = "detect"
    snapshot: dict
    created_at: datetime
