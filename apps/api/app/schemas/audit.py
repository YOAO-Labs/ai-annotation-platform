from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.schemas._jsonb_types import AuditDetail


class AuditLogOut(BaseModel):
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
    detail_json: AuditDetail | None
    request_id: str | None = None
    created_at: datetime

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_to_str(cls, v):
        return str(v) if v is not None else None

    class Config:
        from_attributes = True


class AuditLogList(BaseModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None


class AuditSummaryTotals(BaseModel):
    event_count: int
    error_count: int
    action_kind_count: int


class AuditSummaryDailyPoint(BaseModel):
    day: date
    event_count: int
    error_count: int


class AuditSummaryBucket(BaseModel):
    key: str
    event_count: int


class AuditMonthlySummary(BaseModel):
    month: str
    timezone: str = "UTC"
    materialized_through: date | None
    totals: AuditSummaryTotals
    daily: list[AuditSummaryDailyPoint]
    top_actions: list[AuditSummaryBucket]
    target_types: list[AuditSummaryBucket]
    actor_roles: list[AuditSummaryBucket]


class AuditArchiveOut(BaseModel):
    year: int
    month: int
    object_key: str
    size_bytes: int | None = None
    archived_at: str | None = None


class AuditArchiveRowsOut(BaseModel):
    items: list[dict]
    total: int
    limit: int
    offset: int
    year: int
    month: int
