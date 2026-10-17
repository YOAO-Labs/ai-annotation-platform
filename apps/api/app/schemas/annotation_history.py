"""v0.7.2 · 标注框编辑/审核历史时间线 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.schemas.user import UserBrief


class HistoryEntry(BaseModel):
    """一条历史事件 — audit 或 comment 统一外形。

    kind ∈ { audit, comment }；audit 含 action + detail；comment 含 body + comment_id。
    所有事件按 timestamp 倒序合并。
    """

    kind: str
    timestamp: datetime
    actor: UserBrief | None = None
    # audit 字段
    action: str | None = None
    detail: dict[str, Any] | None = None
    # comment 字段
    comment_id: UUID | None = None
    body: str | None = None


class AnnotationHistoryResponse(BaseModel):
    # I4 · task 级时间线 (GET /tasks/{id}/audit-history) 时 annotation_id=None.
    annotation_id: UUID | None = None
    task_id: UUID
    entries: list[HistoryEntry]
