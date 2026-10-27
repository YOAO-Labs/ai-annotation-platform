"""Fixed review scope requests and sanitized execution evidence."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QualityReviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding: (
        Literal[
            "decode_failed", "dimensions_out_of_range", "exact_duplicate", "skipped"
        ]
        | None
    ) = None
    review: Literal["open", "confirmed", "dismissed"] | None = None
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class QualityReviewPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["selected", "filtered"]
    result_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    query: QualityReviewQuery = Field(default_factory=QualityReviewQuery)
    state: Literal["open", "confirmed", "dismissed"]
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.note.strip():
            raise ValueError("A review reason is required")
        if self.scope == "selected" and (
            not self.result_ids or len(set(self.result_ids)) != len(self.result_ids)
        ):
            raise ValueError("Select 1 to 200 unique results")
        if self.scope == "filtered" and self.result_ids:
            raise ValueError("Filtered scopes cannot contain explicit result IDs")
        return self


class QualityReviewExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern="^[0-9a-f]{64}$")
    range_policy: Literal["snapshot"]


class QualityReviewBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    quality_run_id: uuid.UUID
    created_by: uuid.UUID | None
    async_job_id: uuid.UUID | None
    selection: dict
    proof: dict
    scope_digest: str
    decision: str
    note: str
    status: str
    total: int
    actionable_count: int
    historical_count: int
    processed: int
    summary: dict
    error_code: str | None
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None


class QualityReviewBatchList(BaseModel):
    items: list[QualityReviewBatchOut]
    total: int


class QualityReviewMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    result_id: uuid.UUID
    snapshot: dict
    state: str
    reason_code: str | None
    applied_revision: int | None
    processed_at: datetime | None


class QualityReviewMemberList(BaseModel):
    items: list[QualityReviewMemberOut]
    total: int
