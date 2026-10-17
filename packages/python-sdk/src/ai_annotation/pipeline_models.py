"""Typed contracts for named pipelines and project preannotation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel

PipelineScope = Literal["private", "organization", "public"]
VisualTaskName = Literal[
    "detect",
    "segment",
    "classify",
    "pose",
    "obb",
    "ocr",
    "doc_layout",
    "tracker",
    "interactive_seg",
]


class PipelineStage(BaseModel):
    """One stage in a saved or submitted pipeline.

    The stage graph and backend capability checks remain server-owned. Config
    maps are open because their keys depend on the selected backend/model.
    """

    model_config = ConfigDict(
        extra="forbid", protected_namespaces=("model_validate", "model_dump")
    )

    stage: int = Field(
        description="Unique stage number; it does not need to start at zero."
    )
    ml_backend_id: UUID = Field(
        description="Enabled ML backend selected for this stage."
    )
    source: dict[str, Any] | None = Field(
        default=None, description="Open dataset/source metadata for the root stage."
    )
    model_id: str | None = None
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None
    model_variants: dict[str, str] | None = None
    params: dict[str, Any] | None = Field(
        default=None, description="Backend-specific model parameters."
    )
    class_filter: list[int] | None = None
    parent_stage: int | None = Field(
        default=None,
        description="Parent stage number; null identifies the unique root stage.",
    )
    parent_class_filter: list[str] | None = None
    roi: dict[str, Any] | None = Field(
        default=None,
        description="Open ROI configuration; server validates supported modes.",
    )
    write: dict[str, Any] | None = Field(
        default=None,
        description="Open output/write configuration; server validates targets.",
    )
    on_failure: Literal["keep_parent", "drop_box"] = Field(
        default="keep_parent",
        description="How to handle this stage failing for a parent region.",
    )
    label: str | None = None
    input: dict[str, Any] | None = Field(
        default=None, description="Open input-mode configuration for downstream stages."
    )


class PipelineStageOut(PipelineStage):
    """A response stage that tolerates fields added by newer API versions."""

    model_config = ConfigDict(extra="allow")


class ProjectPipelineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scope: PipelineScope = "private"
    project_id: UUID | None = Field(
        default=None,
        description="Required for private pipelines; null for other scopes.",
    )
    organization_id: UUID | None = Field(
        default=None,
        description="Required for organization pipelines; null for other scopes.",
    )
    stages: list[PipelineStage] = Field(
        description="Pipeline stages; backend availability is checked when applied or run."
    )
    is_default: bool = False

    @model_validator(mode="after")
    def validate_scope_owner(self) -> ProjectPipelineCreate:
        if self.scope == "private":
            if self.project_id is None or self.organization_id is not None:
                raise ValueError(
                    "private pipelines require project_id and forbid organization_id"
                )
        elif self.scope == "organization":
            if self.project_id is not None or self.organization_id is None:
                raise ValueError(
                    "organization pipelines require organization_id and forbid project_id"
                )
        elif self.project_id is not None or self.organization_id is not None:
            raise ValueError(
                "public pipelines cannot specify project_id or organization_id"
            )
        if self.scope != "private" and self.is_default:
            raise ValueError("only private project pipelines can be default")
        return self


class ProjectPipelineUpdate(BaseModel):
    """Partial update; omission and explicit null are distinct on the wire."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    scope: PipelineScope | None = None
    project_id: UUID | None = None
    organization_id: UUID | None = None
    stages: list[PipelineStage] | None = None
    is_default: bool | None = None


class ProjectPipelineApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_id: UUID
    set_default: bool = False


class ProjectPipeline(_AAPModel):
    id: UUID
    scope: PipelineScope
    project_id: UUID | None = None
    organization_id: UUID | None = None
    name: str
    stages: list[PipelineStageOut]
    is_default: bool = False
    created_by: UUID
    created_by_name: str | None = None
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime


class PreannotateRequest(BaseModel):
    """Request body for asynchronous project preannotation.

    When pipeline_stages is supplied, the API derives top-level backend/model
    settings from its unique root stage. Scope and backend capability checks
    happen server-side when the job is submitted.
    """

    model_config = ConfigDict(
        extra="forbid", protected_namespaces=("model_validate", "model_dump")
    )

    ml_backend_id: UUID | None = Field(
        default=None,
        description="Required for a single-stage request; derived from the root when stages are supplied.",
    )
    task_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional explicit task set, limited to 200 task IDs.",
    )
    prompt: str | None = None
    output_mode: Literal["box", "mask", "both"] = "mask"
    batch_id: UUID | None = Field(
        default=None,
        description="Optional project batch; bulk runs require active or unassigned draft status.",
    )
    execution_scope: Literal["bulk", "workbench"] = Field(
        default="bulk",
        description="Bulk requires eligible pending tasks; workbench accepts one editable task.",
    )
    params: dict[str, Any] | None = None
    predict_mode: Literal["skip_predicted", "overwrite", "append"] = Field(
        default="skip_predicted",
        description="overwrite replaces existing predictions; append retains them.",
    )
    model_id: str | None = None
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None
    model_variants: dict[str, str] | None = None
    class_filter: list[int] | None = None
    pipeline_stages: list[PipelineStage] | None = Field(
        default=None,
        description="Optional staged pipeline. Exactly one stage has parent_stage=null.",
    )
    on_key_conflict: Literal["reject", "last_wins"] = Field(
        default="reject",
        description="Policy for sibling stages that write the same final attribute key.",
    )

    @model_validator(mode="after")
    def require_backend_for_flat_request(self) -> PreannotateRequest:
        if not self.pipeline_stages and self.ml_backend_id is None:
            raise ValueError(
                "ml_backend_id is required when pipeline_stages is omitted"
            )
        return self


class PreannotationSubmission(_AAPModel):
    """Acknowledgement for a queued (or idempotently reused) preannotation job."""

    job_id: UUID
    status: str
    total_tasks: int | None = None
    channel: str
    celery_task_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
