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


class StorageUsageCategory(BaseModel):
    category: Literal[
        "managed_original",
        "preview_cache",
        "training_version",
        "temporary",
        "other_platform",
        "unclassified",
    ]
    object_count: int
    size_bytes: int


class StorageUsageBreakdown(BaseModel):
    categories: list[StorageUsageCategory]
    sampled_size_bytes: int
    sampled_object_count: int
    total_size_bytes: int | None
    conflict_object_count: int
    measured_bucket_count: int
    bucket_count: int
    measured_from: datetime | None
    measured_through: datetime | None
    has_stale_data: bool
    coverage: Literal["none", "partial", "complete"]
    scope: Literal["configured_platform_buckets_current_objects"] = (
        "configured_platform_buckets_current_objects"
    )
    source: Literal["s3_list_objects_v2_and_saved_ownership"] = (
        "s3_list_objects_v2_and_saved_ownership"
    )
    deduplication_basis: Literal["endpoint_bucket_key"] = "endpoint_bucket_key"
    includes_versions: bool = False
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
    breakdown: StorageUsageBreakdown | None = None


class UsageInventoryResponse(BaseModel):
    job_id: uuid.UUID
    reused: bool
