"""Audit pages, summary, archive records and concrete query contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ai_annotation.models import _AAPModel


class AuditLogOut(_AAPModel):
    id: int
    actor_id: UUID | None
    actor_email: str | None
    actor_role: str | None
    action: str
    target_type: str | None
    target_id: str | None
    method: str | None
    path: str | None
    status_code: int | None
    ip: str | None
    detail_json: dict[str, Any] | None
    request_id: str | None = None
    created_at: datetime


class AuditLogList(_AAPModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None


class AuditSummaryTotals(_AAPModel):
    event_count: int
    error_count: int
    action_kind_count: int


class AuditSummaryDailyPoint(_AAPModel):
    day: date
    event_count: int
    error_count: int


class AuditSummaryBucket(_AAPModel):
    key: str
    event_count: int


class AuditMonthlySummary(_AAPModel):
    month: str
    timezone: str = "UTC"
    materialized_through: date | None
    totals: AuditSummaryTotals
    daily: list[AuditSummaryDailyPoint]
    top_actions: list[AuditSummaryBucket]
    target_types: list[AuditSummaryBucket]
    actor_roles: list[AuditSummaryBucket]


class AuditArchiveOut(_AAPModel):
    year: int
    month: int
    object_key: str
    size_bytes: int | None = None
    archived_at: str | None = None


class AuditArchiveRowsOut(_AAPModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    year: int
    month: int


class _AuditRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, allow_inf_nan=False
    )


class AuditLogFilters(_AuditRequest):
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    actor_id: UUID | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    business_only: bool = False
    detail_key: str | None = Field(default=None, min_length=1)
    detail_value: str | None = None

    @model_validator(mode="after")
    def paired_detail_filter(self) -> "AuditLogFilters":
        if (self.detail_key is None) != (self.detail_value is None):
            raise ValueError("detail_key and detail_value must be supplied together")
        return self


class AuditLogQuery(AuditLogFilters):
    page: StrictInt = Field(default=1, ge=1)
    page_size: StrictInt = Field(default=20, ge=1, le=200)
    cursor: str | None = None


class AuditExportQuery(AuditLogFilters):
    format: Literal["csv", "json"] = "csv"


class AuditArchiveQuery(_AuditRequest):
    year: StrictInt = Field(ge=2000, le=9999)
    month: StrictInt = Field(ge=1, le=12)
    limit: StrictInt = Field(default=100, ge=1, le=1000)
    offset: StrictInt = Field(default=0, ge=0)
