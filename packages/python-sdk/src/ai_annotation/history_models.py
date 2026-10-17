"""Typed response models for annotation and task audit history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_annotation.models import UserBrief


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str
    timestamp: datetime
    actor: UserBrief | None = None
    action: str | None = None
    detail: dict[str, Any] | None = None
    comment_id: UUID | None = None
    body: str | None = None


class AnnotationHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    annotation_id: UUID | None = None
    task_id: UUID
    entries: list[HistoryEntry]


__all__ = ["AnnotationHistoryResponse", "HistoryEntry"]
