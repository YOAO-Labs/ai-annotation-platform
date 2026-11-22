from yoao_task_contract import normalize_task_fields
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from yoao_workspace.configuration_values import (
    AttributeSchema as _AttributeSchema,
    ClassConfigEntry as _ClassConfigEntry,
    ClassesConfig as _ClassesConfig,
    ProjectRenderingConfig as _ProjectRenderingConfig,
    ToolBindings as _ToolBindings,
    VideoCollaborationConfig as _VideoCollaborationConfig,
    VideoSamplingConfig as _VideoSamplingConfig,
)
from yoao_workspace.mask_configuration_values import MaskQCConfig as _MaskQCConfig
from yoao_workspace.point_quality_configuration_values import (
    PointCloudQualityConfig as _PointCloudQualityConfig,
)
from app.db.enums import PlatformRole, ProjectRole
from yoao_workspace_contract.access import ProjectCapability


class ProjectBatchSummary(BaseModel):
    total: int = 0
    assigned: int = 0
    in_review: int = 0


class ProjectReadinessSummary(BaseModel):
    """Owner-only, database-backed checklist for starting a project.

    The dashboard deliberately keeps this separate from client preferences: a
    project is ready because the server has observed the required data,
    batches and assignments, rather than because a browser marked a step done.
    """

    project_id: UUID
    guide_ready: bool
    classes_ready: bool
    linked_dataset_count: int
    dataset_item_count: int
    task_count: int
    linked_task_count: int
    task_creation_active_jobs: int = 0
    task_creation_failed_jobs: int = 0
    latest_task_creation_status: str | None = None
    batch_count: int
    nonempty_batch_count: int
    executable_batch_count: int
    assigned_batch_count: int
    active_annotator_count: int
    active_reviewer_count: int
    activated_batch_count: int


class ProjectOut(BaseModel):
    id: UUID
    organization_id: UUID | None = None
    display_id: str
    name: str
    type_label: str
    type_key: str
    data_type: str = "image"
    owner_id: UUID
    owner_name: str | None = None
    owner_avatar_ref: str | None = None
    member_count: int = 0
    my_project_role: ProjectRole | None = None
    status: str
    ai_enabled: bool
    ml_backend_id: UUID | None = None
    classes: list[str] = []
    classes_config: _ClassesConfig = {}
    attribute_schema: _AttributeSchema = _AttributeSchema()
    tool_bindings: _ToolBindings = {}
    label_config: dict = {}
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: float = 0.7
    box_threshold: float = 0.35
    text_threshold: float = 0.25
    ai_interactive_enabled: bool = True
    raster_mask_native_editing_enabled: bool = True
    mask_qc_config: _MaskQCConfig = Field(default_factory=_MaskQCConfig)
    point_cloud_quality_config: _PointCloudQualityConfig = Field(
        default_factory=_PointCloudQualityConfig
    )
    rendering_config: _ProjectRenderingConfig = _ProjectRenderingConfig()
    video_sampling: _VideoSamplingConfig = _VideoSamplingConfig()
    video_collaboration: _VideoCollaborationConfig = _VideoCollaborationConfig()
    scene_mode: bool = False
    prefer_same_scene_continuation: bool = False
    scene_continuation_window_min: int = 30
    default_variants: dict[str, dict[str, str]] = Field(default_factory=dict)
    preannotate_pipeline: list[dict] | None = None

    @field_validator("preannotate_pipeline", mode="before")
    @classmethod
    def canonical_pipeline_tasks(cls, value):
        return (
            [normalize_task_fields(stage) for stage in value]
            if value is not None
            else None
        )

    capability_warnings: list[str] = []
    annotation_guide: str | None = None
    guide_assets: list[dict] = []
    guide_assets_revision: int = 1
    model_version: str | None = None
    task_lock_ttl_seconds: int = 300
    total_tasks: int
    completed_tasks: int
    review_tasks: int
    in_progress_tasks: int = 0
    ai_completed_tasks: int = 0
    batch_summary: ProjectBatchSummary = ProjectBatchSummary()
    due_date: date | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _derive_flat_class_views(self) -> "ProjectOut":
        from yoao_workspace_contract import (
            derive_attribute_schema,
            derive_classes_config,
            derive_classes_list,
        )

        tb = {
            k: (v.model_dump() if hasattr(v, "model_dump") else v)
            for k, v in (self.tool_bindings or {}).items()
        }
        self.classes = derive_classes_list(tb)
        self.classes_config = {
            name: _ClassConfigEntry(**cfg)
            for name, cfg in derive_classes_config(tb).items()
        }
        self.attribute_schema = _AttributeSchema(**derive_attribute_schema(tb))
        return self

    class Config:
        from_attributes = True


class ProjectPage(BaseModel):
    items: list[ProjectOut]
    total: int
    page: int
    page_size: int
    pages: int


class ProjectStats(BaseModel):
    total_data: int
    completed: int
    ai_rate: float
    pending_review: int
    total_annotations: int = 0
    ai_derived_annotations: int = 0
    total_data_series: list[int] = Field(default_factory=list)
    completed_series: list[int] = Field(default_factory=list)
    ai_rate_series: list[float] = Field(default_factory=list)
    pending_review_series: list[int] = Field(default_factory=list)


class ProjectClassUsageOut(BaseModel):
    classes: dict[str, int] = Field(default_factory=dict)
    attributes: dict[str, int] = Field(default_factory=dict)


class ProjectCleanupOrphansRequest(BaseModel):
    dry_run: bool = True


class ProjectCleanupOrphansOut(BaseModel):
    orphan_annotations: int = 0
    orphan_attribute_keys: dict[str, int] = Field(default_factory=dict)


class ProjectMemberOut(BaseModel):
    id: UUID
    user_id: UUID
    user_name: str
    user_email: str
    role: ProjectRole
    platform_role: PlatformRole
    version: int = 1
    assigned_at: datetime
    updated_at: datetime | None = None
    avatar_ref: str | None = None

    class Config:
        from_attributes = True


class MentionCandidateOut(BaseModel):
    """讨论区 @ 提及候选：项目成员 + 项目负责人 + 启用的平台超管。

    后端提及校验本就放行 owner / project_admin / super_admin，这里只补齐可发现性。
    """

    user_id: UUID
    user_name: str
    user_email: str | None = None
    kind: Literal["member", "owner", "super_admin"] = "member"

    class Config:
        from_attributes = True


class ProjectMemberCreate(BaseModel):
    user_id: UUID
    role: Literal["annotator", "reviewer", "viewer"]


class ProjectAccessOut(BaseModel):
    """Resolved project access for the current account (``GET .../access``)."""

    project_id: UUID
    user_id: UUID
    platform_role: PlatformRole
    project_role: ProjectRole | None = None
    membership_id: UUID | None = None
    membership_version: int | None = None
    access_kind: Literal["super_admin", "owner", "member"]
    is_manager: bool = False
    capabilities: list[ProjectCapability] = Field(default_factory=list)


class ProjectMemberRolePreviewRequest(BaseModel):
    """Input for the read-only role-change preview (no writes)."""

    project_role: Literal["annotator", "reviewer", "viewer"]
    replacement_annotator_id: UUID | None = None
    replacement_reviewer_id: UUID | None = None


class ProjectMemberRolePreviewOut(BaseModel):
    member_id: UUID
    user_id: UUID
    current_role: ProjectRole
    current_version: int
    target_role: ProjectRole
    requires_handoff: bool = False
    blockers: list[str] = Field(default_factory=list)
    blocker_details: list[dict] = Field(default_factory=list)
    resource_snapshot: dict = Field(default_factory=dict)
    preview_token: str


class ProjectMemberRoleChangeRequest(BaseModel):
    project_role: Literal["annotator", "reviewer", "viewer"]
    expected_version: int = Field(ge=1)
    preview_token: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    replacement_annotator_id: UUID | None = None
    replacement_reviewer_id: UUID | None = None


class ProjectTransferRequest(BaseModel):
    new_owner_id: UUID
