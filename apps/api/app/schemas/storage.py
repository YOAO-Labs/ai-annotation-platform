import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


class StorageUsageAccountingSummary(BaseModel):
    scope: Literal["current_objects"] = "current_objects"
    coverage: Literal["partial"] = "partial"
    producers: list[str] = Field(
        default_factory=lambda: [
            "prediction_inputs",
            "managed_original_creation",
            "thumbnail_outputs",
            "original_cache",
            "image_pyramid_outputs",
            "video_derivative_outputs",
            "export_media_outputs",
            "export_zip_outputs",
            "bug_attachment_uploads",
            "avatar_uploads",
            "comment_attachment_uploads",
            "guide_asset_uploads",
            "raster_mask_objects",
            "audit_archive_outputs",
            "training_dataset_outputs",
            "active_learning_media",
            "sample_evaluation_reports",
            "training_redacted_logs",
        ]
    )
    confirmed_added_bytes: int = 0
    confirmed_removed_bytes: int = 0
    net_change_bytes: int = 0
    pending_operation_count: int = 0
    unknown_operation_count: int = 0
    projected_total_bytes: None = None


class StorageUsageReconciliationSummary(BaseModel):
    scope: Literal["current_objects"] = "current_objects"
    coverage: Literal["partial"] = "partial"
    status: Literal["no_baseline", "partial"]
    measured_at: datetime
    bucket_count: int
    compared_object_count: int
    uncertain_object_count: int
    unexplained_object_count: int
    confirmed_change_bytes: int
    known_unexplained_change_bytes: int
    unexplained_change_bytes: int | None


class UsageScheduleRequest(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=1, le=168)


class UsageScheduleSummary(BaseModel):
    enabled: bool = False
    interval_hours: int = 24
    status: Literal["off", "active", "paused"] = "off"
    next_due_at: datetime | None = None
    last_started_at: datetime | None = None
    pause_reason: str | None = None


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
    uncovered_binding_purposes: list[str] = Field(default_factory=list)
    accounting: StorageUsageAccountingSummary | None = None
    reconciliation: StorageUsageReconciliationSummary | None = None
    usage_schedule: UsageScheduleSummary | None = None


class UsageInventoryResponse(BaseModel):
    job_id: uuid.UUID
    reused: bool
