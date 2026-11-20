"""AnnotationFeedback 统一反馈表。

设计取舍 (见 docs/adr/0027-annotation-feedback-unified-table.md):
- 反馈统一读取；旧 bug_reports、annotation_comments 与 task 驳回记录由各自写路径维护，
  有项目归属的旧记录由对应写路径在同一事务中镜像。
- anchor_type ∈ {project, task, annotation, pixel, point_cloud} 统一锚点模型.
  pixel anchor 在 anchor_position 携带 {x, y, frame?} 像素相对坐标 (与 geometry JSONB 同语义).
- kind ∈ {issue, comment, reject, bug}; reject 走专项 helper 写入 (不与 task.reject_reason 双写, 见 ADR).
- thread_parent_id 自引用形成评论链 (CVAT Comment thread 模式).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnnotationFeedback(Base):
    __tablename__ = "annotation_feedbacks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # anchor_type ∈ {project, task, annotation, pixel, point_cloud}
    anchor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id"), nullable=True
    )
    # pixel anchor 携带归一化 x/y；point_cloud anchor 携带 scene/frame/质检定位器。
    # none_as_null=True: Python None → SQL NULL (而非 JSONB 字面 null), 以匹配 CHECK 约束.
    anchor_position: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list, nullable=False
    )
    mentions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list, nullable=False
    )
    canvas_drawing: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    thread_parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_feedbacks.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
