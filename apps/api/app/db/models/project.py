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
    data_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="image"
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    status: Mapped[str] = mapped_column(String(30), default="in_progress")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ml_backend_pool_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    default_variants: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    preannotate_pipeline: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    label_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    tool_bindings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    sampling: Mapped[str] = mapped_column(String(30), default="sequence")
    maximum_annotations: Mapped[int] = mapped_column(Integer, default=1)
    show_overlap_first: Mapped[bool] = mapped_column(Boolean, default=False)
    iou_dedup_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.7", default=0.7
    )
    box_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.35", default=0.35
    )
    text_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.25", default=0.25
    )
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
    rendering_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    annotation_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    guide_assets: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
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
    batch_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    video_sampling: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    video_collaboration: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
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
