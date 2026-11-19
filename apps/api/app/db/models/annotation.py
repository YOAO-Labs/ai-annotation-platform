import uuid
from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        CheckConstraint(
            "temporal_role IN ('keyframe','derived','sample')",
            name="ck_annotations_temporal_role",
        ),
        Index(
            "ix_annotations_project_track_active",
            "project_id",
            "track_id",
            "task_id",
            postgresql_where=text(
                "is_active = true AND was_cancelled = false AND track_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_annotations_project_updated_active",
            "project_id",
            "updated_at",
            "id",
            postgresql_where=text("is_active = true AND was_cancelled = false"),
        ),
        Index(
            "ix_annotations_project_class_active",
            "project_id",
            "class_name",
            "id",
            postgresql_where=text("is_active = true AND was_cancelled = false"),
        ),
        Index(
            "ix_annotations_project_source_active",
            "project_id",
            "source",
            "id",
            postgresql_where=text("is_active = true AND was_cancelled = false"),
        ),
        Index(
            "ix_annotations_project_tool_type_active",
            "project_id",
            "tool_unit_id",
            "annotation_type",
            "id",
            postgresql_where=text("is_active = true AND was_cancelled = false"),
        ),
        Index(
            "ix_annotations_task_segment_track_active",
            "task_id",
            "video_segment_id",
            "track_id",
            postgresql_where=text(
                "is_active = true AND was_cancelled = false "
                "AND video_segment_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_annotations_scene_track_task_active",
            "scene_track_id",
            "task_id",
            postgresql_where=text(
                "is_active = true AND was_cancelled = false "
                "AND scene_track_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_annotations_camera_member_active",
            "task_id",
            "scene_track_id",
            "sensor_role",
            unique=True,
            postgresql_where=text(
                "is_active = true AND was_cancelled = false "
                "AND scene_track_id IS NOT NULL AND sensor_role IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "(sensor_role IS NULL AND sensor_dataset_item_id IS NULL "
            "AND sensor_visibility IS NULL AND calibration_revision IS NULL "
            "AND calibration_digest IS NULL) OR "
            "(sensor_role IS NOT NULL AND sensor_dataset_item_id IS NOT NULL "
            "AND sensor_visibility IS NOT NULL AND calibration_revision IS NOT NULL "
            "AND calibration_digest IS NOT NULL)",
            name="ck_annotations_sensor_context_complete",
        ),
        CheckConstraint(
            "sensor_visibility IS NULL OR sensor_visibility IN "
            "('visible','occluded','truncated','unknown')",
            name="ck_annotations_sensor_visibility",
        ),
        CheckConstraint(
            "sensor_role IS NULL OR sensor_role LIKE 'camera_%'",
            name="ck_annotations_sensor_role",
        ),
        CheckConstraint(
            "calibration_revision IS NULL OR calibration_revision >= 1",
            name="ck_annotations_calibration_revision",
        ),
        CheckConstraint(
            "calibration_digest IS NULL OR char_length(calibration_digest) = 64",
            name="ck_annotations_calibration_digest",
        ),
        CheckConstraint(
            "sensor_role IS NULL OR (annotation_type = 'bbox' "
            "AND geometry->>'type' = 'bbox' AND scene_track_id IS NOT NULL "
            "AND track_id IS NOT NULL)",
            name="ck_annotations_camera_member_shape",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id")
    )
    video_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("video_segments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    annotation_type: Mapped[str] = mapped_column(String(30), default="bbox")
    # 标注所属工具单位; 校验 class_name ∈ project.tool_bindings[unit].classes.
    # 默认 bbox 兼容旧数据; migration backfill 按 annotation_type 反推 (polygon/mask → region).
    tool_unit_id: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="bbox", default="bbox"
    )
    class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    # ADR-0006 · predictions 按月分区；此列是软引用，不设数据库外键。
    # 业务侧已手动管理（删 prediction 前先 NULL），大表上重建复合 FK 收益低、锁表代价高。
    parent_prediction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id")
    )
    # ADR-0045 · 跨帧同一对象的通用标识 (几何类型无关), 格式 trk_<uuid.hex>.
    # 权威落点 (原分裂: video geometry 内 track_id + box_3d 借 group_id>=1e9, 编组下线后
    # group_id 列已删). 单一工厂 _new_track_id() 产出; propagate/interpolate/导出/3D 前端统一读本列.
    track_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # ADR-0069 · 3D Scene member 的权威 Track 外键。track_id 继续冗余为兼容外部键；
    # 非 Scene / compact video / 迁移异常链保持 NULL。
    scene_track_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scene_tracks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ADR-0071 · LiDAR SceneTrack 的多相机人工成员上下文。
    # 这些列要么同时为 NULL（普通/3D Annotation），要么同时完整。
    sensor_dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_items.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    sensor_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sensor_visibility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    calibration_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calibration_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # source 表示来源，temporal_role 表示成员在时序模型中的角色，两者正交。
    temporal_role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="sample", default="sample"
    )
    lead_time: Mapped[float | None] = mapped_column(Float)
    was_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    ground_truth: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # 属性级溯源 sidecar: 每个 attribute key 的来源标记
    # {key: {origin: "ai"|"human", model_ref?: {...}, confidence?: float, at?: iso}}.
    # 独立列 (不塞进 attributes 内, 避免污染值空间). 存量行为 {} → 读作全 human.
    # key 必须与 attributes 同步 (增删属性联动 meta), 见 AnnotationService。
    attributes_meta: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # CVAT 风格的 shape 状态位。
    z_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
