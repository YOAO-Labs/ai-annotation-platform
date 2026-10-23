import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class BucketSummary(BaseModel):
    name: str
    role: str
    roles: list[str]
    status: Literal["unknown", "ok", "stale"]
    object_count: int | None = None
    total_size_bytes: int | None = None
    error: str | None = None
    measured_at: datetime | None = None
    measurement_started_at: datetime | None = None
    attempt_status: str = "never"
    attempted_at: datetime | None = None
    scope: str = "current_objects"
    source: str = "s3_list_objects_v2"
    coverage: Literal["none", "complete"] = "none"
    is_estimate: bool = True


class BucketsResponse(BaseModel):
    items: list[BucketSummary]
    total_object_count: int | None
    total_size_bytes: int | None
    measured_bucket_count: int
    bucket_count: int
    physical_total_bytes: int | None = None
    physical_available_bytes: int | None = None
    quota_bytes: int | None = None
    inventory_job_id: uuid.UUID | None = None
    inventory_status: str | None = None
    location_id: uuid.UUID


class UsageInventoryResponse(BaseModel):
    job_id: uuid.UUID
    reused: bool
