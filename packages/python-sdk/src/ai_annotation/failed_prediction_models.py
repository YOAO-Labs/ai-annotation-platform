"""Typed request and response contracts for failed prediction operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from ai_annotation.models import _AAPModel


class FailedPredictionItem(_AAPModel):
    id: UUID
    task_id: UUID | None = None
    task_display_id: str | None = None
    project_id: UUID
    project_name: str | None = None
    ml_backend_id: UUID | None = None
    backend_name: str | None = None
    model_version: str | None = None
    error_type: str
    message: str
    retry_count: int
    last_retry_at: datetime | None = None
    dismissed_at: datetime | None = None
    created_at: datetime


class FailedPredictionPage(_AAPModel):
    items: list[FailedPredictionItem]
    total: int
    page: int
    page_size: int


class FailedPredictionRetryReceipt(_AAPModel):
    """HTTP 202 receipt; retry progress is reported asynchronously elsewhere."""

    status: Literal["queued"]
    failed_id: UUID


class FailedPredictionDismissReceipt(_AAPModel):
    status: Literal["dismissed", "restored"]
    failed_id: UUID
    dismissed_at: datetime | None = None
