"""v0.21.0 · ProjectPipeline 请求 / 响应 schema."""

from __future__ import annotations

from aap_task_contract import normalize_task_fields

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import field_validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


PipelineScope = Literal["private", "organization", "public"]


def _validate_scope_owner(
    scope: PipelineScope,
    project_id: UUID | None,
    organization_id: UUID | None,
) -> None:
    if scope == "private":
        if project_id is None or organization_id is not None:
            raise ValueError(
                "private 编排必须指定 project_id 且不能指定 organization_id"
            )
    elif scope == "organization":
        if project_id is not None or organization_id is None:
            raise ValueError(
                "organization 编排必须指定 organization_id 且不能指定 project_id"
            )
    elif project_id is not None or organization_id is not None:
        raise ValueError("public 编排不能指定 project_id / organization_id")


class ProjectPipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scope: PipelineScope = "private"
    project_id: UUID | None = None
    organization_id: UUID | None = None
    stages: list[dict]
    is_default: bool = False

    @model_validator(mode="after")
    def _check_scope_owner(self) -> "ProjectPipelineCreate":
        _validate_scope_owner(self.scope, self.project_id, self.organization_id)
        if self.scope != "private" and self.is_default:
            raise ValueError("只有 private 项目编排可以设为默认")
        return self


class ProjectPipelineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    scope: PipelineScope | None = None
    project_id: UUID | None = None
    organization_id: UUID | None = None
    stages: list[dict] | None = None
    is_default: bool | None = None


class ProjectPipelineApplyRequest(BaseModel):
    pipeline_id: UUID
    set_default: bool = False


class ProjectPipelineOut(BaseModel):
    id: UUID
    scope: PipelineScope
    project_id: UUID | None = None
    organization_id: UUID | None = None
    name: str
    stages: list[dict]

    @field_validator("stages", mode="before")
    @classmethod
    def canonical_pipeline_tasks(cls, value):
        return (
            [normalize_task_fields(stage) for stage in value]
            if value is not None
            else None
        )

    is_default: bool = False
    created_by: UUID
    created_by_name: str | None = None
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
