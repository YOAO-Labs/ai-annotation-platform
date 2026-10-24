import uuid
from datetime import datetime, date
from sqlalchemy import (
    String,
    Boolean,
    Integer,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id")
    )
    display_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type_label: Mapped[str] = mapped_column(String(50), nullable=False)
    type_key: Mapped[str] = mapped_column(String(30), nullable=False)
    # v0.10.28 · 媒体维度数据类型 (image / video / lidar); type_key 继续编码
    # 媒体+任务子类型 (video-track vs video-mm 等), data_type 只到媒体粒度,
    # 用于展示/筛选/媒体维度分流. backfill 见 alembic 0082.
    data_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="image"
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    status: Mapped[str] = mapped_column(String(30), default="in_progress")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # v0.23.3 ADR-0050 · 项目主服务池 (原 ml_backend_id, 迁移为 ml_backend_pool_id)。
    # 项目不再直接绑定物理 registry 实例, 而是绑定一个服务池; router (P3) 在池内
    # active 成员间选择。off mode 下 singleton pool 的 legacy_instance_id 解析回
    # 原 registry 实例, 行为与 v0.23.2 完全一致。DB FK 由 alembic 0132 加
    # (ON DELETE SET NULL); ORM 仍是 plain UUID 与原约定一致。
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # v0.14.13 · 项目级 variant 偏好. v0.23.3 改为按 pool_id 分桶 (原 backend_id,
    # 经 alembic 0132 singleton backfill 重 key)。
    # 形状: { "<pool_uuid>": { "<axis_key>": "<axis_value>", ... }, ... }
    # 优先级链: 本字段 > backend.default_variants > backend env 默认.
    # 前端 VariantSelector 用户切换变体时 PATCH 写回; 项目跨设备 / 协作时保留偏好.
    default_variants: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # v0.18.27 · 项目级「已保存的编排」(方案 A, 一项目一条); 形状 = pipeline_stages 数组
    # (v0.23.3: stage 内 ml_backend_pool_id 存 str, 原 ml_backend_id 经迁移重 key)。
    # NULL = 未配编排 → popover 不出「运行项目编排」入口。
    preannotate_pipeline: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    label_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # v0.10.17 · 工具维度类别 / 属性绑定 (ROADMAP §A 新建向导通用化).
    # 形状: { tool_unit_id: { enabled, classes: [...], attribute_schema: {...} } }
    # v0.10.22 · 旧扁平 classes / classes_config / attribute_schema 列已删除,
    # tool_bindings 是唯一存储真值; 扁平视图由响应 schema / 导出读时派生.
    tool_bindings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    sampling: Mapped[str] = mapped_column(String(30), default="sequence")
    maximum_annotations: Mapped[int] = mapped_column(Integer, default=1)
    show_overlap_first: Mapped[bool] = mapped_column(Boolean, default=False)
    iou_dedup_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.7", default=0.7
    )
    # v0.9.2 · GroundingDINO 阈值项目级 override（默认对齐 backend env 0.35 / 0.25）
    box_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.35", default=0.35
    )
    text_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.25", default=0.25
    )
    # 项目级「交互式 AI 工具」总开关 (工作台 smart-point / smart-box / exemplar /
    # magic-box)。归属项目设置「ML 模型」——能否用 AI 工具取决于绑了什么 backend,
    # 与几何类别无关。此前它被错误建模成 ai_interactive 工具单位塞在 tool_bindings 里,
    # 既有 enabled 开关对工作台零作用, 又派生出一套与 region/bbox 割裂的伪类别域。
    ai_interactive_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    raster_mask_native_editing_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    mask_qc_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    point_cloud_quality_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # v0.10.10 · I17.3 · 项目级渲染配置覆盖；空 dict = 全部沿用用户级 preferences
    rendering_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # v0.10.13 · E1 · CVAT-style 项目级 Markdown 标注指引；None = 未配置
    annotation_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.10.13 · E1 · 已上传的指引图片资源元数据列表
    # entry: {key, original_name, content_type, size, uploaded_at}
    guide_assets: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    # The database advances this only when the asset list actually changes.
    guide_assets_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    task_lock_ttl_seconds: Mapped[int] = mapped_column(Integer, default=300)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    review_tasks: Mapped[int] = mapped_column(Integer, default=0)
    in_progress_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # v0.10.25 · batch 聚合物化列 {total, assigned, in_review}; 由
    # batch._sync_project_counters 写时维护, 取代 list_projects 的实时 GROUP BY.
    batch_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # v0.10.29 · 视频帧逻辑采样配置 (项目级). {} = 不采样 (step=1); 或
    #   { "mode": "fps",  "target_fps": 10 }
    #   { "mode": "step", "frame_step": 5 }
    # frame_index 永远存源视频帧号 (决策 D2), 采样只是导航/打点视图层.
    video_sampling: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    video_collaboration: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # v0.14.1 · scene 连续标注调度: 打开后 get_next_task 优先返回"用户上一次提交
    # task 的同 scene 下一帧"(默认 OFF, 既有项目零回归); window 为连续 session 估计窗口.
    scene_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    prefer_same_scene_continuation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    scene_continuation_window_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30", default=30
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
