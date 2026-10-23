"""Public quality evidence never contains storage locators or key proofs."""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatasetQualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_width: int = Field(default=64, ge=1, le=100000)
    min_height: int = Field(default=64, ge=1, le=100000)
    max_width: int = Field(default=8192, ge=1, le=100000)
    max_height: int = Field(default=8192, ge=1, le=100000)

    @model_validator(mode="after")
    def ranges(self):
        if self.max_width < self.min_width or self.max_height < self.min_height:
            raise ValueError("Maximum dimensions must not be smaller than minimums")
        return self


class DatasetQualityRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    dataset_id: uuid.UUID
    async_job_id: uuid.UUID | None
    rule_version: str
    config: DatasetQualityConfig
    status: str
    total: int
    processed: int
    summary: dict
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class DatasetQualityRunList(BaseModel):
    items: list[DatasetQualityRunOut]
    total: int


class DatasetQualityResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    run_id: uuid.UUID
    item_id: uuid.UUID
    asset_id: uuid.UUID | None
    file_name: str
    sha256: str | None
    size_bytes: int | None
    state: str
    reason_code: str | None
    bytes_verified: bool
    findings: list[str]
    width: int | None
    height: int | None
    observed_at: datetime | None
    duplicate_count: int
    stale: bool
    review_state: str
    review_note: str
    review_revision: int
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None


class DatasetQualityResultList(BaseModel):
    items: list[DatasetQualityResultOut]
    total: int


class DatasetQualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["open", "confirmed", "dismissed"]
    note: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
