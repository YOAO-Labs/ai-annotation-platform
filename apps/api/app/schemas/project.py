from pydantic import BaseModel, ConfigDict, Field, model_validator
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from app.schemas._jsonb_types import (
    AttributeSchema,
    ClassConfigEntry,
    ClassesConfig,
    ProjectRenderingConfig,
    ToolBindings,
    validate_tool_bindings_keys,
)
from app.db.enums import PlatformRole, ProjectRole
from app.schemas.mask_qc import MaskQCConfig
from app.schemas.point_cloud_quality import PointCloudQualityConfig
from app.services.project_access import ProjectCapability
from pydantic import field_validator


class VideoSamplingConfig(BaseModel):
    # v0.10.29 · 视频帧逻辑采样配置. mode=none 退化为不采样 (step=1).
    mode: Literal["none", "fps", "step"] = "none"
    target_fps: float | None = Field(default=None, gt=0)
    frame_step: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> "VideoSamplingConfig":
        if self.mode == "fps":
            if self.target_fps is None:
                raise ValueError("mode=fps 必须提供 target_fps")
        elif self.mode == "step":
            if self.frame_step is None:
                raise ValueError("mode=step 必须提供 frame_step")
        else:  # mode == "none"
            if self.target_fps is not None or self.frame_step is not None:
                raise ValueError("mode=none 时 target_fps / frame_step 应为 None")
        return self


class VideoCollaborationConfig(BaseModel):
    enabled: bool = False
    overlap_frames: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_enabled_overlap(self) -> "VideoCollaborationConfig":
        if self.enabled and self.overlap_frames < 1:
            raise ValueError("启用视频协同时 overlap_frames 必须大于 0")
        return self


class ProjectCreate(BaseModel):
    name: str
    type_label: str
    # v0.10.28 · B 路线: 新建项目以 data_type (媒体维度) 为主. type_key 可缺省,
    # 由 data_type 派生兼容值 (见 api/v1/projects.create_project) 保旧分流不破.
    type_key: str | None = None
    data_type: str | None = None
    classes: list[str] = []
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    # v0.10.17 · 工具维度类别 / 属性绑定. 给定时优先于扁平 classes_config / attribute_schema.
    tool_bindings: ToolBindings | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, v):
        return validate_tool_bindings_keys(v)

    ai_enabled: bool = False
    ml_backend_id: UUID | None = None
    # v0.9.7 · 从 wizard step 4 选一个全局已注册 backend, 后端复制 row 入新项目.
    # 与 ml_backend_id 互斥: 直接给 ml_backend_id 表示已存在本项目下的 backend (罕见);
    # 给 ml_backend_source_id 表示"从其它项目复用一份配置".
    ml_backend_source_id: UUID | None = None
    # v0.10.11 · "从已有项目复制配置" — 给定时, 用源项目的可克隆字段
    # (classes / classes_config / attribute_schema / ai 配置 / label_config /
    # rendering_config / sampling 等) 兜底当前 payload 未显式给出的字段;
    # 不复制 datasets / tasks / annotations / members / batches.
    # 调用者必须对源项目有 view 权限; 否则 404.
    source_project_id: UUID | None = None
    # v0.10.13 · E1 · 当 source_project_id 给定时, 是否同时复制 annotation_guide
    # + guide_assets. 默认 False; 复制时 guide_assets 共享原 storage key (不重新上传).
    copy_annotation_guide: bool = False
    # v0.10.14 · E2 · 从 ProjectTemplate 应用模板创建项目. 与 source_project_id
    # 互斥 (同时给则 400). 给定时, 后端从模板 deepcopy 模板载荷字段进 payload,
    # 并将模板 usage_count + 1.
    template_id: UUID | None = None
    due_date: date | None = None
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    # 交互式 AI 工具总开关 (归 ML 模型设置项); 缺省开启。
    ai_interactive_enabled: bool = True
    # 图片原生 Raster Mask 写入默认开启；部署总闸仍可统一禁用。
    raster_mask_native_editing_enabled: bool = True
    # v0.10.29 · 视频帧逻辑采样配置; None / 缺省 = 不采样 (空 dict).
    video_sampling: VideoSamplingConfig | None = None
    video_collaboration: VideoCollaborationConfig | None = None

    @field_validator("video_collaboration")
    @classmethod
    def _video_collaboration_cannot_be_null(
        cls, value: VideoCollaborationConfig | None
    ) -> VideoCollaborationConfig:
        if value is None:
            raise ValueError("video_collaboration cannot be null")
        return value

    # v0.14.4 · 项目级 scene 模式声明;仅 image/lidar 项目可开启。
    scene_mode: bool = False

    @model_validator(mode="after")
    def _validate_source_template_exclusive(self) -> "ProjectCreate":
        if self.template_id is not None and self.source_project_id is not None:
            raise ValueError("template_id 与 source_project_id 互斥, 不能同时给")
        if self.template_id is not None and self.copy_annotation_guide:
            # 模板自带 annotation_guide, copy_annotation_guide 只对 source_project_id 生效
            raise ValueError("copy_annotation_guide 仅在 source_project_id 给定时有效")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = None
    type_label: str | None = None
    type_key: str | None = None
    data_type: str | None = None
    status: str | None = None
    classes: list[str] | None = None
    classes_config: ClassesConfig | None = None
    attribute_schema: AttributeSchema | None = None
    # v0.10.17 · 工具维度类别 / 属性绑定; PATCH 用整体替换语义 (与现有 classes_config 一致).
    tool_bindings: ToolBindings | None = None

    @field_validator("tool_bindings", mode="before")
    @classmethod
    def _check_tool_bindings_keys(cls, v):
        return validate_tool_bindings_keys(v)

    ai_enabled: bool | None = None
    ml_backend_id: UUID | None = None
    due_date: date | None = None
    sampling: str | None = None
    maximum_annotations: int | None = None
    show_overlap_first: bool | None = None
    iou_dedup_threshold: Annotated[float, Field(ge=0.3, le=0.95)] | None = None
    # v0.9.2 · DINO 阈值项目级 override
    box_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    text_threshold: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    # 交互式 AI 工具总开关 (归 ML 模型设置项)。
    ai_interactive_enabled: bool | None = None
    raster_mask_native_editing_enabled: bool | None = None
    mask_qc_config: MaskQCConfig | None = None
    point_cloud_quality_config: PointCloudQualityConfig | None = None

    @field_validator("mask_qc_config")
    @classmethod
    def _mask_qc_config_cannot_be_null(cls, value: MaskQCConfig | None) -> MaskQCConfig:
        if value is None:
            raise ValueError("mask_qc_config cannot be null")
        return value

    @field_validator("point_cloud_quality_config")
    @classmethod
    def _point_cloud_quality_config_cannot_be_null(
        cls, value: PointCloudQualityConfig | None
    ) -> PointCloudQualityConfig:
        if value is None:
            raise ValueError("point_cloud_quality_config cannot be null")
        return value

    # v0.10.10 · I17.3 · 项目级渲染配置覆盖；空 dict / 字段缺省 = 沿用用户级偏好
    rendering_config: ProjectRenderingConfig | None = None
    # v0.10.13 · E1 · 标注指引 Markdown 原文; 显式 None 仅在 owner 主动清空时出现.
    # 单独 PATCH guide_assets 用 guide_assets 端点; 这里允许 owner 在异常情况下手动
    # 重置 list (例如批量删 orphan 后端 sync), 一般 UI 不直接写.
    annotation_guide: str | None = None
    guide_assets: list[dict] | None = None
    # v0.10.29 · 视频帧逻辑采样配置; PATCH 用整体替换语义 (与 rendering_config 一致).
    video_sampling: VideoSamplingConfig | None = None
    video_collaboration: VideoCollaborationConfig | None = None

    @field_validator("video_collaboration")
    @classmethod
    def _video_collaboration_cannot_be_null(
        cls, value: VideoCollaborationConfig | None
    ) -> VideoCollaborationConfig:
        if value is None:
            raise ValueError("video_collaboration cannot be null")
        return value

    # v0.14.1 · scene 连续标注调度开关 + 连续 session 估计窗口(分钟).
    scene_mode: bool | None = None
    prefer_same_scene_continuation: bool | None = None
    scene_continuation_window_min: Annotated[int, Field(ge=1, le=480)] | None = None
    # v0.14.13 · 项目级 variant 偏好 (按 backend_id 分桶).
    # PATCH 用整体替换语义; 前端可只发改动的 backend 桶 (其它桶保留靠业务侧 merge).
    default_variants: dict[str, dict[str, str]] | None = None
    # v0.18.27 · 项目级「已保存的编排」(方案 A). exclude_unset 区分「不改」与「清除」:
    # 不传 = 不动; 显式 null = 清除。非空时结构由 update_project 端点用 PipelineStage 复核。
    # 用 list[dict] 而非 list[PipelineStage]: 存储态 ml_backend_id 是 str, 且要原样回吐 JSONB。
    preannotate_pipeline: list[dict] | None = None


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
    classes_config: ClassesConfig = {}
    attribute_schema: AttributeSchema = AttributeSchema()
    # v0.10.17 · 工具维度类别 / 属性绑定 (唯一存储真值).
    tool_bindings: ToolBindings = {}
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
    mask_qc_config: MaskQCConfig = Field(default_factory=MaskQCConfig)
    point_cloud_quality_config: PointCloudQualityConfig = Field(
        default_factory=PointCloudQualityConfig
    )
    # v0.10.10 · I17.3 · 项目级渲染配置覆盖；空 dict 表示项目不覆盖任何字段
    rendering_config: ProjectRenderingConfig = ProjectRenderingConfig()
    # v0.10.29 · 视频帧逻辑采样配置; 空 dict (mode=none) 表示不采样.
    video_sampling: VideoSamplingConfig = VideoSamplingConfig()
    video_collaboration: VideoCollaborationConfig = VideoCollaborationConfig()
    # v0.14.1 · scene 连续标注调度开关 + 连续 session 估计窗口(分钟).
    scene_mode: bool = False
    prefer_same_scene_continuation: bool = False
    scene_continuation_window_min: int = 30
    # v0.14.13 · 项目级 variant 偏好 (按 backend_id 分桶). 空 dict = 未设, 由前端落到 backend.default_variants.
    default_variants: dict[str, dict[str, str]] = Field(default_factory=dict)
    # v0.18.27 · 项目级「已保存的编排」(方案 A). None = 未配编排.
    preannotate_pipeline: list[dict] | None = None
    # v0.19.3 WS1 · 保存编排时的能力软提示 (batchable/产 class 判据), 仅 PATCH 响应填充;
    # 非 DB 列, 不挡保存, dispatch-time 422 仍是最终闸。
    capability_warnings: list[str] = []
    # v0.10.13 · E1 · 标注指引 Markdown 原文; None 表示未配置.
    annotation_guide: str | None = None
    # v0.10.13 · E1 · 已上传的指引图片资源元数据列表.
    guide_assets: list[dict] = []
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
        from app.services.project import (
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
            name: ClassConfigEntry(**cfg)
            for name, cfg in derive_classes_config(tb).items()
        }
        self.attribute_schema = AttributeSchema(**derive_attribute_schema(tb))
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
