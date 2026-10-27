"""Locator-free, explicitly observational managed-original verification contracts."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel


class ManagedOriginalVerificationStart(BaseModel):
    retirement_id: uuid.UUID
    verification_id: uuid.UUID
    async_job_id: uuid.UUID
    existing: bool


class ManagedOriginalVerificationItem(BaseModel):
    id: uuid.UUID
    async_job_id: uuid.UUID | None
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    outcome: (
        Literal["matched", "missing", "changed", "unverifiable", "failed", "cancelled"]
        | None
    )
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    context_matches: bool
    unsettled_operation: bool
    expected_sha256: str
    expected_size_bytes: int
    observed_sha256: str | None
    observed_size_bytes: int | None


class ManagedOriginalVerificationHistory(BaseModel):
    retirement_id: uuid.UUID
    measured_at: datetime
    scope: Literal["registered_retirement_assertion_only"] = (
        "registered_retirement_assertion_only"
    )
    execution_permission: Literal[False] = False
    can_start: bool
    blockers: list[str]
    max_bytes: int
    timeout_seconds: int
    items: list[ManagedOriginalVerificationItem]
    total: int
    limit: int
    offset: int
    has_more: bool
