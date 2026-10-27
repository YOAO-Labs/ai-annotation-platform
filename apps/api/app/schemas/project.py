from aap_task_contract import normalize_task_fields
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from aap_workspace.configuration_values import (
    AttributeSchema as _AttributeSchema,
    ClassConfigEntry as _ClassConfigEntry,
    ClassesConfig as _ClassesConfig,
    ProjectRenderingConfig as _ProjectRenderingConfig,
    ToolBindings as _ToolBindings,
    VideoCollaborationConfig as _VideoCollaborationConfig,
    VideoSamplingConfig as _VideoSamplingConfig,
)
from aap_workspace.mask_configuration_values import MaskQCConfig as _MaskQCConfig
from aap_workspace.point_quality_configuration_values import (
    PointCloudQualityConfig as _PointCloudQualityConfig,
)
from app.db.enums import PlatformRole, ProjectRole
from aap_workspace_contract.access import ProjectCapability


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
    # v0.10.28 · 媒体维度数据类型 (image / video / lidar).
    data_type: str = "image"
    owner_id: UUID
    owner_name: str | None = None
    owner_avatar_ref: str | None = None
    member_count: int = 0
    #: Project role of the *requesting* account (None for managers / viewers
    #: without a membership).  Filled by the list/query/detail serializers.
    my_project_role: ProjectRole | None = None
    status: str
    ai_enabled: bool
    ml_backend_id: UUID | None = None
    # v0.10.22 · 扁平视图字段不再有 DB 列, 由下方 validator 从 tool_bindings 读时派生.
    classes: list[str] = []
    classes_config: _ClassesConfig = {}
    attribute_schema: _AttributeSchema = _AttributeSchema()
    # v0.10.17 · 工具维度类别 / 属性绑定 (唯一存储真值).
    tool_bindings: _ToolBindings = {}
    label_config: dict = {}
    sampling: str = "sequence"
    maximum_annotations: int = 1
    show_overlap_first: bool = False
    iou_dedup_threshold: float = 0.7
    box_threshold: float = 0.35
    text_threshold: float = 0.25
    # 交互式 AI 工具总开关 (归 ML 模型设置项)。
    ai_interactive_enabled: bool = True
    raster_mask_native_editing_enabled: bool = True
    mask_qc_config: _MaskQCConfig = Field(default_factory=_MaskQCConfig)
    point_cloud_quality_config: _PointCloudQualityConfig = Field(
        default_factory=_PointCloudQualityConfig
    )
    # v0.10.10 · I17.3 · 项目级渲染配置覆盖；空 dict 表示项目不覆盖任何字段
    rendering_config: _ProjectRenderingConfig = _ProjectRenderingConfig()
    # v0.10.29 · 视频帧逻辑采样配置; 空 dict (mode=none) 表示不采样.
    video_sampling: _VideoSamplingConfig = _VideoSamplingConfig()
    video_collaboration: _VideoCollaborationConfig = _VideoCollaborationConfig()
    # v0.14.1 · scene 连续标注调度开关 + 连续 session 估计窗口(分钟).
    scene_mode: bool = False
    prefer_same_scene_continuation: bool = False
    scene_continuation_window_min: int = 30
    # v0.14.13 · 项目级 variant 偏好 (按 backend_id 分桶). 空 dict = 未设, 由前端落到 backend.default_variants.
    default_variants: dict[str, dict[str, str]] = Field(default_factory=dict)
    # v0.18.27 · 项目级「已保存的编排」(方案 A). None = 未配编排.
    preannotate_pipeline: list[dict] | None = None

    @field_validator("preannotate_pipeline", mode="before")
    @classmethod
    def canonical_pipeline_tasks(cls, value):
        return (
            [normalize_task_fields(stage) for stage in value]
            if value is not None
            else None
        )

    # v0.19.3 WS1 · 保存编排时的能力软提示 (batchable/产 class 判据), 仅 PATCH 响应填充;
    # 非 DB 列, 不挡保存, dispatch-time 422 仍是最终闸。
    capability_warnings: list[str] = []
    # v0.10.13 · E1 · 标注指引 Markdown 原文; None 表示未配置.
    annotation_guide: str | None = None
    # v0.10.13 · E1 · 已上传的指引图片资源元数据列表.
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
        # v0.10.22 · classes / classes_config / attribute_schema 无 DB 列,
        # 从 tool_bindings 派生扁平投影供前端 / 导出消费.
        from aap_workspace_contract import (
            derive_attribute_schema,
            derive_classes_config,
            derive_classes_list,
        )

        # tool_bindings 的 value 在此已是 ToolBinding 模型实例; derive_* 需要纯 dict.
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
    #: Project responsibility.  Flat by design; invitation DTOs keep the
    #: ``project_member_role`` name.  Required, so a missed serializer fails
    #: loudly instead of returning an untyped string.
    role: ProjectRole
    #: Account/platform role, added so clients no longer conflate the two.
    platform_role: PlatformRole
    #: Membership CAS version; increments on role change.
    version: int = 1
    assigned_at: datetime
    updated_at: datetime | None = None
    # 头像引用（preset:<slug> / upload:<token>）；None = 前端回退姓名首字母。
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
    #: Explicit replacement responsibilities for outstanding work handoff.
    replacement_annotator_id: UUID | None = None
    replacement_reviewer_id: UUID | None = None


class ProjectMemberRolePreviewOut(BaseModel):
    member_id: UUID
    user_id: UUID
    current_role: ProjectRole
    current_version: int
    target_role: ProjectRole
    #: True when the target role needs an explicit, valid handoff first.
    requires_handoff: bool = False
    blockers: list[str] = Field(default_factory=list)
    blocker_details: list[dict] = Field(default_factory=list)
    #: Canonical snapshot of affected work/locks/claims at preview time.
    resource_snapshot: dict = Field(default_factory=dict)
    #: Opaque token covering the snapshot; must be echoed on the write.
    preview_token: str


class ProjectMemberRoleChangeRequest(BaseModel):
    project_role: Literal["annotator", "reviewer", "viewer"]
    #: CAS guard against a stale membership.
    expected_version: int = Field(ge=1)
    #: Token returned by the preview; revalidated under locks.
    preview_token: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    replacement_annotator_id: UUID | None = None
    replacement_reviewer_id: UUID | None = None


class ProjectTransferRequest(BaseModel):
    new_owner_id: UUID
