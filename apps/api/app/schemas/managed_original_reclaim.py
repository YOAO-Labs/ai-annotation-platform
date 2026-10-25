"""Locator-free controlled deletion and reference observation API contracts."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class ManagedOriginalReclaimReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=1000)
    retry_of_id: uuid.UUID | None = None


class ManagedOriginalReclaimApply(ManagedOriginalReclaimReview):
    impact_token: str = Field(min_length=1, max_length=8000)


class ManagedOriginalReclaimPreview(BaseModel):
    retirement_id: uuid.UUID
    measured_at: datetime
    expires_at: datetime
    eligible: bool
    blockers: list[str]
    impact_token: str | None
    expected_size_bytes: int | None
    version_id: str | None
    creation_id: uuid.UUID | None
    location_id: uuid.UUID | None
    irreversible: Literal[True] = True


class ManagedOriginalReclaimStart(BaseModel):
    retirement_id: uuid.UUID
    reclaim_id: uuid.UUID
    async_job_id: uuid.UUID
    existing: bool


class ManagedOriginalReclaimItem(BaseModel):
    id: uuid.UUID
    async_job_id: uuid.UUID | None
    retry_of_id: uuid.UUID | None
    status: Literal["pending", "running", "completed", "failed", "cancelled", "unknown"]
    phase: Literal[
        "claimed",
        "reading",
        "delete_intent",
        "delete_ack",
        "absence_verified",
        "settled",
    ]
    outcome: (
        Literal[
            "deleted",
            "already_absent",
            "missing",
            "changed",
            "failed",
            "unknown",
            "cancelled",
        ]
        | None
    )
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    expected_size_bytes: int
    version_id: str
    creation_id: uuid.UUID
    location_id: uuid.UUID
    delete_started: bool
    absence_proven: bool
    unsettled_operation: bool


class ManagedOriginalReclaimHistory(BaseModel):
    retirement_id: uuid.UUID
    measured_at: datetime
    can_preview: bool
    blockers: list[str]
    max_bytes: int
    timeout_seconds: int
    items: list[ManagedOriginalReclaimItem]
    total: int
    limit: int
    offset: int
    has_more: bool


class ReferenceReconciliationOut(BaseModel):
    status: Literal["idle", "pending", "running", "completed", "failed"] = "idle"
    async_job_id: uuid.UUID | None = None
    measured_at: datetime | None = None
    last_success_at: datetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    unknown: dict[str, int] = Field(default_factory=dict)
    deltas: list[dict[str, str | int]] = Field(default_factory=list)
    error_code: str | None = None
