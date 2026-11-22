"""公开 pydantic 模型。

响应只声明 SDK 用户关心的稳定字段; extra="allow" 容忍服务端新增字段 (前向兼容),
未声明字段仍可通过属性访问。输入模型按各自写入合同校验。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID
from yoao.point_cloud_quality_config_models import PointCloudQualityConfigRead

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

T = TypeVar("T")

PlatformRole = Literal["super_admin", "project_admin", "employee", "viewer"]
ProjectRole = Literal["annotator", "reviewer", "viewer"]
ProjectCapability = Literal[
    "project.read",
    "project.manage",
    "member.read",
    "member.manage",
    "task.read",
    "annotation.write",
    "review.write",
    "export.annotations",
    "performance.read",
]
ProjectAccessKind = Literal["super_admin", "owner", "member"]


class _SDKModel(BaseModel):
    model_config = ConfigDict(
        extra="allow", protected_namespaces=("model_validate", "model_dump")
    )


class Page(_SDKModel, Generic[T]):
    """limit/offset 分页响应 (datasets 等)。"""

    items: list[T]
    total: int
    limit: int
    offset: int


class Project(_SDKModel):
    point_cloud_quality_config: PointCloudQualityConfigRead | None = None
    id: UUID
    display_id: str
    name: str
    type_key: str
    data_type: str = "image"
    status: str
    my_project_role: ProjectRole | None = None
    created_at: datetime | None = None


class ProjectAccess(_SDKModel):
    """当前账号在某个项目上的解析后权限 (``GET /projects/{id}/access``)。

    管理者 (超管 / 负责人) 的 membership 字段可能为 None; 普通成员给出
    membership id/version, ``access_kind`` 与固定 ``capabilities``。
    """

    project_id: UUID
    user_id: UUID
    platform_role: PlatformRole
    access_kind: ProjectAccessKind
    project_role: ProjectRole | None = None
    membership_id: UUID | None = None
    membership_version: int | None = None
    is_manager: bool = False
    capabilities: list[ProjectCapability] = Field(default_factory=list)


class Dataset(_SDKModel):
    id: UUID
    display_id: str
    name: str
    description: str = ""
    data_type: str = "image"
    is_temporal: bool = False
    file_count: int = 0
    total_size: int = 0
    created_at: datetime | None = None
    archived_at: datetime | None = None
    archived_by: UUID | None = None
    lifecycle_revision: int = 1


DatasetListState = Literal["active", "archived", "all"]
DatasetLifecycleOperation = Literal["archive", "restore"]
DatasetLifecycleState = Literal["active", "archived"]


class DatasetLifecycleCounts(_SDKModel):
    items: int
    sources: int
    projects: int
    tasks: int
    annotations: int
    active_scans: int
    active_registrations: int
    enabled_schedules: int
    pending_jobs: int
    ready_lineage_versions: int
    unfinished_lineage_versions: int


class DatasetLifecyclePreview(_SDKModel):
    """A short-lived impact review bound to the actor and dataset revision."""

    dataset_id: UUID
    lifecycle_revision: int
    state: DatasetLifecycleState
    operation: DatasetLifecycleOperation
    measured_at: datetime
    expires_at: datetime
    impact_token: str
    counts: DatasetLifecycleCounts
    frozen_coverage: Literal["recorded_dataset_membership_only"]


class DatasetLifecycleResult(_SDKModel):
    dataset_id: UUID
    lifecycle_revision: int
    state: DatasetLifecycleState
    archived_at: datetime | None
    archived_by: UUID | None


class DatasetItem(_SDKModel):
    id: UUID
    dataset_id: UUID
    file_name: str
    file_path: str
    file_type: str
    file_size: int | None = None
    content_hash: str | None = None
    width: int | None = None
    height: int | None = None
    metadata: Any = Field(default_factory=dict)
    file_url: str | None = None
    thumbnail_url: str | None = None
    blurhash: str | None = None
    created_at: datetime | None = None


class DatasetUnlinkPreview(_SDKModel):
    will_delete_tasks: int = 0
    will_delete_annotations: int = 0
    will_delete_batches: int = 0


class DatasetUnlinkResult(_SDKModel):
    deleted_tasks: int = 0
    deleted_annotations: int = 0
    deleted_batches: int = 0
    deleted_batch_ids: list[UUID] = Field(default_factory=list)


class UploadedItem(_SDKModel):
    """upload_files 单文件三步流的结果 (file_name 由 SDK 补充)。"""

    item_id: UUID
    file_name: str
    status: str = "ok"
    linked_tasks: Any = None


class ZipUploadResult(_SDKModel):
    added: int = 0
    deduped: int = 0
    skipped: int = 0
    errors: list[Any] = Field(default_factory=list)
    total_in_zip: int = 0
    linked_tasks: Any = None
    scene_inference_notes: list[str] = Field(default_factory=list)


class LinkResult(_SDKModel):
    """status="linking" 时建 task 走异步, 用 async_job_id 配合 jobs.wait。"""

    status: str
    dataset_id: UUID
    project_id: UUID
    async_job_id: UUID | None = None
    created_tasks: int | None = None


class Task(_SDKModel):
    id: UUID
    project_id: UUID
    display_id: str
    file_name: str
    file_type: str
    status: str
    file_url: str | None = None
    assignee_id: UUID | None = None
    batch_id: UUID | None = None
    sequence_order: int | None = None
    created_at: datetime | None = None


class TaskPage(_SDKModel):
    """tasks.list 响应; cursor 翻页时 total 为 None (复用首页值)。"""

    items: list[Task]
    total: int | None = None
    limit: int = 50
    offset: int = 0
    next_cursor: str | None = None


class Annotation(_SDKModel):
    id: UUID
    task_id: UUID
    source: str
    annotation_type: str
    tool_unit_id: str = "bbox"
    class_name: str
    geometry: dict[str, Any]
    confidence: float | None = None
    group_id: int | None = None
    is_active: bool = True
    version: int = 1
    created_at: datetime | None = None


FeedbackKind = Literal["issue", "comment", "reject", "bug"]
FeedbackAnchorType = Literal["project", "task", "annotation", "pixel", "point_cloud"]
FeedbackStatus = Literal["open", "resolved", "wont_fix"]
FeedbackSeverity = Literal["info", "warn", "blocker"]


class FeedbackMention(_SDKModel):
    user_id: UUID = Field(alias="userId")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    offset: int = Field(ge=0)
    length: int = Field(ge=1)

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class DiscussionActions(_SDKModel):
    edit: bool = False
    change_status: bool = False
    delete: bool = False
    reply: bool = False


class AnnotationFeedbackPatch(BaseModel):
    """Only fields explicitly supplied are sent; explicit nulls stay explicit."""

    model_config = ConfigDict(extra="forbid")

    status: FeedbackStatus | None = None
    severity: FeedbackSeverity | None = None
    title: str | None = Field(default=None, max_length=500)
    body: str | None = None
    mentions: list[FeedbackMention] | None = None


class AnnotationFeedbackReply(BaseModel):
    """A reply inherits its anchor from the server-side parent feedback."""

    model_config = ConfigDict(extra="forbid")

    body: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_text_or_attachment(self):
        if not self.attachments and not self.body.strip():
            raise ValueError("feedback replies must contain text or an attachment")
        return self


class FeedbackVideoFrameRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_frame: int = Field(strict=True, ge=0)
    to_frame: int = Field(strict=True, ge=0)


class FeedbackVideoViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    center_x: float = Field(strict=True, allow_inf_nan=False)
    center_y: float = Field(strict=True, allow_inf_nan=False)
    zoom: float = Field(strict=True, allow_inf_nan=False, gt=0)


class FeedbackVideoTimelineWindow(BaseModel):
    """Use from_ in Python; serialized JSON retains the wire key 'from'."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: float = Field(alias="from", strict=True, allow_inf_nan=False, ge=0)
    to: float = Field(strict=True, allow_inf_nan=False, ge=0)


class FeedbackVideoContext(BaseModel):
    """Strict V1 write contract; response anchors preserve unknown versions as dictionaries."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    track_id: str | None = Field(default=None, strict=True)
    annotation_version: int | None = Field(default=None, strict=True, ge=1)
    frame_range: FeedbackVideoFrameRange | None = None
    viewport: FeedbackVideoViewport | None = None
    timeline_window: FeedbackVideoTimelineWindow | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value


class FeedbackAnchorPosition(BaseModel):
    """Typed pixel/video input with the existing Mask and 3D locator fields."""

    model_config = ConfigDict(extra="forbid")

    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    frame: int | None = Field(default=None, ge=0)
    video_context: FeedbackVideoContext | None = None
    region_bbox: tuple[float, float, float, float] | None = None
    region_digest: str | None = None
    boundary_digest: str | None = None
    mask_qc_issue_id: UUID | None = None
    point_cloud_quality_issue_id: UUID | None = None
    scene_id: UUID | None = None
    scene_track_id: UUID | None = None
    auxiliary_layers: list[str] = Field(default_factory=list)
    compare_locator: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_video_frame(cls, value):
        if isinstance(value, dict) and value.get("video_context") is not None:
            if type(value.get("frame")) is not int or value["frame"] < 0:
                raise ValueError("video_context requires a non-negative integer frame")
        return value

    @model_validator(mode="after")
    def _validate_coordinates(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        if self.region_bbox is not None:
            x0, y0, x1, y1 = self.region_bbox
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError(
                    "region_bbox must be a normalized non-empty half-open box"
                )
        if self.video_context is not None:
            if self.frame is None:
                raise ValueError("video_context requires frame")
            frame_range = self.video_context.frame_range
            if frame_range and not (
                frame_range.from_frame <= self.frame <= frame_range.to_frame
            ):
                raise ValueError("frame_range must contain frame")
            window = self.video_context.timeline_window
            if window and window.from_ > window.to:
                raise ValueError("timeline_window must be ordered")
        return self


class AnnotationFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FeedbackKind
    anchor_type: FeedbackAnchorType
    project_id: UUID
    body: str
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    anchor_position: FeedbackAnchorPosition | None = None
    severity: FeedbackSeverity | None = None
    title: str | None = Field(default=None, max_length=500)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    thread_parent_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_anchor(self):
        anchor = self.anchor_position
        context = anchor.video_context if anchor else None
        if context is not None:
            if self.anchor_type != "pixel":
                raise ValueError("video_context requires a pixel anchor")
            if context.annotation_version is not None and self.annotation_id is None:
                raise ValueError("annotation_version requires annotation_id")
        if self.anchor_type == "project":
            if self.task_id or self.annotation_id or anchor:
                raise ValueError(
                    "project anchor cannot carry task, annotation or position"
                )
        elif self.anchor_type == "task":
            if not self.task_id or self.annotation_id or anchor:
                raise ValueError(
                    "task anchor requires task_id and no annotation or position"
                )
        elif self.anchor_type == "annotation":
            if not (self.task_id and self.annotation_id) or anchor:
                raise ValueError("annotation anchor requires both ids and no position")
        elif self.anchor_type == "pixel":
            if (
                not self.task_id
                or anchor is None
                or anchor.x is None
                or anchor.y is None
            ):
                raise ValueError("pixel anchor requires task_id, x and y")
        elif self.anchor_type == "point_cloud":
            if (
                not self.task_id
                or anchor is None
                or anchor.point_cloud_quality_issue_id is None
            ):
                raise ValueError(
                    "point_cloud anchor requires task_id and quality issue"
                )
        return self


class AnnotationFeedback(_SDKModel):
    id: UUID
    kind: FeedbackKind
    anchor_type: FeedbackAnchorType
    project_id: UUID
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    anchor_position: dict[str, Any] | None = None
    status: FeedbackStatus
    severity: FeedbackSeverity | None = None
    title: str | None = None
    body: str
    author_id: UUID
    author_name: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[FeedbackMention] = Field(default_factory=list)
    canvas_drawing: dict[str, Any] | None = None
    thread_parent_id: UUID | None = None
    is_active: bool = True
    resolved_at: datetime | None = None
    resolved_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    actions: DiscussionActions = Field(default_factory=DiscussionActions)

    @property
    def video_context(self) -> FeedbackVideoContext | None:
        """Return a recognized context without interpreting unknown or malformed versions."""
        value = (self.anchor_position or {}).get("video_context")
        if not isinstance(value, dict):
            return None
        try:
            return FeedbackVideoContext.model_validate(value)
        except ValidationError:
            return None


class FeedbackPage(_SDKModel):
    items: list[AnnotationFeedback]
    next_cursor: str | None = None
    total: int | None = None
    status_counts: dict[str, int] | None = None


class FeedbackThreadPage(_SDKModel):
    root: AnnotationFeedback
    items: list[AnnotationFeedback]
    next_cursor: str | None = None
    total: int


class ImportResult(_SDKModel):
    """predictions.import_file 响应 (后端 ImportResult)。"""

    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = False


class Job(_SDKModel):
    id: UUID
    kind: str
    status: str
    project_id: UUID | None = None
    user_id: UUID | None = None
    progress_pct: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobPage(_SDKModel):
    items: list[Job]
    total: int


class GpuInfo(_SDKModel):
    """ML Backend `/health` 缓存的 GPU 指标 (字段缺失 = backend 未上报)。"""

    device_name: str | None = None
    memory_used_mb: int | None = None
    memory_total_mb: int | None = None
    memory_free_mb: int | None = None
    gpu_utilization_percent: int | None = None
    gpu_temperature_celsius: int | None = None
    gpu_power_watts: float | None = None


class HostInfo(_SDKModel):
    container_cpu_percent: float | None = None
    container_memory_percent: float | None = None


class CacheStats(_SDKModel):
    hits: int | None = None
    misses: int | None = None
    size: int | None = None
    capacity: int | None = None
    hit_rate: float | None = None


class HealthMeta(_SDKModel):
    """backend `/health` 深度指标缓存; capabilities 等未声明字段经 extra="allow" 仍可访问。"""

    gpu_info: GpuInfo | None = None
    host: HostInfo | None = None
    cache: CacheStats | None = None
    model_version: str | None = None


class MLBackend(_SDKModel):
    """ML Backend (只读监控)。state: connected / error。

    全局注册表 (ADR-0044): `id` 是全局 registry id, 一物理 backend 全局
    一份；多个项目启用同一 backend 时，项目端点返回同一 `id`。脚本若保存了
    旧的项目级 backend id，需替换为 registry id。项目端点 (`MLBackends.list/get`) 会回填 `project_id`；
    全局 / admin 场景该字段为 None。
    """

    id: UUID
    project_id: UUID | None = None
    name: str
    url: str
    state: str
    health_meta: HealthMeta | None = None
    error_message: str | None = None
    last_checked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MLBackendHealth(_SDKModel):
    status: str
    backend_id: UUID
    backend_name: str


class MLBackendUnloadResult(_SDKModel):
    ok: bool | None = None
    unloaded: bool | int | None = None
    residency: Any = None


class ProjectMLBackend(_SDKModel):
    backend: MLBackend
    enabled: bool = False
    default_variants: dict[str, Any] | None = None


class ServicePoolSummary(_SDKModel):
    id: UUID
    name: str
    enabled: bool = False
    legacy_instance_id: UUID | None = None
    member_count: int = 0
    routing_generation: int = 1


class ProjectServicePool(_SDKModel):
    pool: ServicePoolSummary
    enabled: bool = False
    default_variants: dict[str, Any] | None = None


class ServicePoolMember(_SDKModel):
    registry_id: UUID
    registry_name: str
    traffic_state: str
    weight: int = 1


class ServicePool(_SDKModel):
    id: UUID
    name: str
    enabled: bool
    routing_policy: str
    legacy_instance_id: UUID | None = None
    routing_generation: int
    capability_fingerprint: str | None = None
    members: list[ServicePoolMember] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CapabilityDrift(_SDKModel):
    pool_id: UUID
    registry_id: UUID
    member_state: str
    pool_enabled: bool
    pool_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    differing_fields: list[str] = Field(default_factory=list)
    has_drift: bool
    can_accept: bool
    blocking_members: list[UUID] = Field(default_factory=list)


class ServicePoolTopology(_SDKModel):
    schema_version: str
    generated_at: datetime
    router_mode: str
    pools: list[dict[str, Any]] = Field(default_factory=list)


class ServicePoolRuntimeSnapshot(_SDKModel):
    schema_version: str
    observed_at: datetime
    router_mode: str
    partial: bool = False
    partial_reason: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    pools: list[dict[str, Any]] = Field(default_factory=list)


class MLBackendStatsSnapshot(_SDKModel):
    """/ws/ml-backend-stats 每 1s 推送的单个 backend 实时快照。

    `loaded` / `idle_unload_seconds` / `last_request_age_seconds` / `pool` / `video_pool`
    是 REST `/health` 拿不到、仅 WS 推送的池/预热维度。
    """

    backend_id: UUID | None = None
    backend_name: str | None = None
    physical_key: str | None = None
    url_host: str | None = None
    state: str
    gpu_info: GpuInfo | None = None
    host: HostInfo | None = None
    cache: CacheStats | None = None
    model_version: str | None = None
    loaded: bool | None = None
    idle_unload_seconds: float | None = None
    last_request_age_seconds: float | None = None
    pool: dict | None = None
    video_pool: dict | None = None
    timestamp: datetime | None = None


class UserBrief(_SDKModel):
    """责任人 inline 摘要 (batch annotator / reviewer 等)。"""

    id: UUID
    name: str
    email: str | None = None
    role: str | None = None
    avatar_initial: str | None = None
    avatar_ref: str | None = None


class Batch(_SDKModel):
    """项目批次。progress_pct 为 0-100 浮点; annotator/reviewer 为责任人摘要。"""

    id: UUID
    project_id: UUID
    display_id: str
    name: str
    status: str
    total_tasks: int = 0
    completed_tasks: int = 0
    review_tasks: int = 0
    approved_tasks: int = 0
    rejected_tasks: int = 0
    progress_pct: float = 0.0
    annotator: UserBrief | None = None
    reviewer: UserBrief | None = None
    created_at: datetime | None = None


class BatchDistributeResult(_SDKModel):
    distributed_batches: int
    annotator_per_batch: dict[str, str | None] = Field(default_factory=dict)
    reviewer_per_batch: dict[str, str | None] = Field(default_factory=dict)


class BulkBatchActionItem(_SDKModel):
    batch_id: UUID
    reason: str


class BulkBatchActionResult(_SDKModel):
    succeeded: list[UUID] = Field(default_factory=list)
    skipped: list[BulkBatchActionItem] = Field(default_factory=list)
    failed: list[BulkBatchActionItem] = Field(default_factory=list)


class Member(_SDKModel):
    """项目成员。

    ``role`` 是项目角色 (annotator / reviewer / viewer), ``platform_role`` 是
    账号的平台角色; 二者不再混用。``version`` 是成员 CAS 版本, 角色变更后自增。
    """

    id: UUID
    user_id: UUID
    user_name: str
    user_email: str
    role: ProjectRole
    platform_role: PlatformRole
    version: int = 1
    assigned_at: datetime | None = None
    updated_at: datetime | None = None
    avatar_ref: str | None = None


class ProjectMemberRolePreview(_SDKModel):
    """成员角色变更预检 (``POST .../role/preview``); 只读, 不写库。

    ``preview_token`` 必须原样回传到 ``PATCH .../role``; ``requires_handoff``
    为真时需在写入时给出显式接替人。变更在锁内按 ``current_version`` 做 CAS 校验。
    """

    member_id: UUID
    user_id: UUID
    current_role: ProjectRole
    current_version: int
    target_role: ProjectRole
    preview_token: str
    requires_handoff: bool = False
    blockers: list[str] = Field(default_factory=list)
    blocker_details: list[dict[str, Any]] = Field(default_factory=list)
    resource_snapshot: dict[str, Any] = Field(default_factory=dict)


class Me(_SDKModel):
    """当前认证主体 (GET /auth/me)。

    ``role`` 是账号的平台角色 (super_admin / project_admin / employee / viewer),
    不再表示项目职责; 项目职责见 :meth:`Client.projects.access` 或成员 ``role``。
    其余字段经 extra 透传。
    """

    id: UUID
    email: str
    name: str
    role: PlatformRole


class TaskActionResult(_SDKModel):
    status: str
    task_id: UUID


class ReviewClaim(_SDKModel):
    task_id: UUID
    reviewer_id: UUID
    reviewer_claimed_at: datetime
    is_self: bool


class AnnotationBulkUpdateResult(_SDKModel):
    updated_ids: list[UUID] = Field(default_factory=list)
    updated_count: int = 0


class JobRetryResult(_SDKModel):
    status: str
    job_id: UUID
    queued: int
    skipped: int


class ProjectStats(_SDKModel):
    """可见项目聚合统计 (GET /projects/stats)。*_series 为最近 12 周时间序列。"""

    total_data: int = 0
    completed: int = 0
    ai_rate: float = 0.0
    pending_review: int = 0
    total_annotations: int = 0
    ai_derived_annotations: int = 0
    total_data_series: list[int] = Field(default_factory=list)
    completed_series: list[int] = Field(default_factory=list)
    ai_rate_series: list[float] = Field(default_factory=list)
    pending_review_series: list[int] = Field(default_factory=list)


class PersonStat(_SDKModel):
    """全员绩效卡片项 (GET /dashboard/admin/people 的 items)。"""

    user_id: str
    name: str
    email: str | None = None
    role: str
    main_metric: int = 0
    main_metric_label: str | None = None
    throughput_score: int = 0
    quality_score: int = 0
    activity_score: int = 0
    rejected_rate: float | None = None
    sparkline_7d: list[int] = Field(default_factory=list)
    weekly_compare_pct: float | None = None
    alerts: list[str] = Field(default_factory=list)


class MyPerformance(_SDKModel):
    """标注员自助绩效 (GET /dashboard/me/performance)。自身 4 周趋势 + 团队均线对标。"""

    user_id: str
    name: str
    period: str | None = None
    throughput: int = 0
    quality_score: int = 0
    weekly_compare_pct: float | None = None
    trend_throughput: list[int] = Field(default_factory=list)
    trend_quality: list[int] = Field(default_factory=list)
    team_trend_throughput: list[float] = Field(default_factory=list)
    p50_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    first_pass_yield: float | None = None


class DashboardStats(_SDKModel):
    """admin / reviewer / annotator 仪表盘原始数据 (字段随角色而异, 全经 extra 透传)。"""


class ApiKey(_SDKModel):
    id: UUID
    name: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreated(ApiKey):
    """创建响应附带一次性 plaintext, 之后无法再次查看。"""

    plaintext: str = Field(repr=False)
