"""ProjectTemplate 模型.

项目模板与「从已有项目复制」是独立入口：
- 可手工建 / 从源项目导出
- 可跨组织共享 (scope)
- 携带 annotation_guide markdown 文本; 不携带 guide_assets storage key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProjectTemplate(Base):
    __tablename__ = "project_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type_label: Mapped[str] = mapped_column(String(50), nullable=False)
    type_key: Mapped[str] = mapped_column(String(30), nullable=False)
    # 媒体维度数据类型 (image / video / lidar); 与 Project 对齐.
    # backfill 见 alembic 0082.
    data_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="image"
    )

    # 模板载荷 (与 CLONEABLE_PROJECT_FIELDS 对应)
    # 工具维度类别 / 属性绑定; 与 Project 对齐. backfill 见 alembic 0073.
    # 旧扁平 classes / classes_config / attribute_schema 列已删除.
    tool_bindings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    label_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    sampling: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="sequence", default="sequence"
    )
    maximum_annotations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    show_overlap_first: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    iou_dedup_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.7", default=0.7
    )
    box_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.35", default=0.35
    )
    text_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.25", default=0.25
    )
    rendering_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # E1 整合: 模板可携带 annotation_guide; guide_assets 不存.
    annotation_guide: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 共享语义
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private", default="private"
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    source_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
