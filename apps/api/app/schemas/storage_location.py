"""Public registry contracts; deployment paths and node anchors stay private."""

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from app.schemas.dataset_source import SourceExternalProtectionOut

from app.schemas.storage_cache import CachePolicy, CacheQuota, CacheTTL


class FilesystemLocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    root_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    relative_path: str = Field(default="", max_length=1000)
    scope: Literal["restricted", "global"] = "restricted"
    use_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    cache_policy: CachePolicy = "preview"
    original_cache_quota_bytes: CacheQuota = 0
    original_cache_ttl_seconds: CacheTTL = 86400


class StorageLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    scope: Literal["restricted", "global"] | None = None
    use_user_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    cache_policy: CachePolicy | None = None
    original_cache_quota_bytes: CacheQuota | None = None
    original_cache_ttl_seconds: CacheTTL | None = None


class AgentLocationCreate(FilesystemLocationCreate):
    agent_id: uuid.UUID
    root_handle: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    root_revision: int = Field(strict=True, ge=1)


class StorageLocationApply(StorageLocationUpdate):
    impact_token: str = Field(min_length=1, max_length=8192)


class StorageLocationOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    enabled: bool
    config_revision: int
    maintenance_state: Literal["active", "draining", "stopped"] = "active"
    maintenance_revision: int = 1
    connection_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    root_revision: int | None = None
    root_handle: str | None = None
    relative_path: str | None = None
    scope: str | None = None
    use_user_ids: list[uuid.UUID] = Field(default_factory=list)
    declared_capabilities: dict[str, bool]
    cache_policy: CachePolicy = "preview"
    original_cache_quota_bytes: int = 0
    original_cache_ttl_seconds: int = 86400
    allowed_actions: list[str] = Field(default_factory=list)


class StorageMaintenanceCounts(BaseModel):
    sources: int
    enabled_sources: int
    unregistered_sources: int
    items: int
    reference_items: int
    datasets: int
    projects: int
    tasks: int
    annotations: int
    active_scans: int
    active_registrations: int
    enabled_schedules: int
    ready_previews: int
    failed_previews: int
    preview_bytes: int
    cache_entries: int
    cache_bytes: int
    active_cache_read_leases: int
    active_input_leases: int
    cleanup_input_leases: int
    ready_lineage_versions: int
    unfinished_lineage_versions: int
    target_records: int
    target_annotations: int
    target_datasets: int
    target_training: int
    target_media_cache: int
    target_audit_archive: int
    target_import: int
    target_export: int
    target_avatars: int
    target_bug_reports: int
    target_related_datasets: int
    target_related_projects: int
    target_related_tasks: int
    target_versions: int
    target_runs: int
    current_bindings: int
    unlocated_targets: int


class StorageMaintenanceImpact(BaseModel):
    location_id: uuid.UUID
    config_revision: int
    measured_at: datetime
    scope: Literal["stored_dependencies"]
    counts: StorageMaintenanceCounts
    frozen_coverage: Literal["recorded_location_lineage_only"]
    input_coverage: Literal["current_task_links_only"]
    execution_permission: Literal[False]


class StorageLocationFieldChange(BaseModel):
    field: str
    before: Any
    after: Any


class StorageLocationChangePreview(BaseModel):
    location_id: uuid.UUID
    config_revision: int
    measured_at: datetime
    expires_at: datetime
    impact_token: str
    changes: list[StorageLocationFieldChange]
    impact: StorageMaintenanceImpact


class StorageOriginalCounts(BaseModel):
    verified: int
    missing: int
    changed: int
    preview_only: int
    unavailable: int
    unverified: int


class StoragePreviewCounts(BaseModel):
    ready: int
    failed: int
    unrecorded: int
    not_requested: int
    blocked: int


class StorageReferenceEvidence(BaseModel):
    scope: Literal["stored_reference_items"]
    total: int
    original_counts: StorageOriginalCounts
    preview_counts: StoragePreviewCounts
    observed_from: datetime | None
    observed_through: datetime | None
    preview_recorded_at: datetime | None
    original_cache_recorded_count: int = 0
    original_cache_recorded_at: datetime | None = None


class StorageSourceRunEvidence(BaseModel):
    run_id: uuid.UUID
    scan_run_id: uuid.UUID
    job_id: uuid.UUID | None
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    phase: Literal[
        "enumerating",
        "validating",
        "resume_validation",
        "preflight",
        "registering",
        "unknown",
    ]
    complete: bool
    matches_current_config: bool
    created_at: datetime
    completed_at: datetime | None


class StorageSourceRunHistory(BaseModel):
    recent: StorageSourceRunEvidence | None
    last_complete: StorageSourceRunEvidence | None


class StorageSourceDiagnostics(BaseModel):
    references: StorageReferenceEvidence
    scan: StorageSourceRunHistory
    preflight: StorageSourceRunHistory


class StorageSourceSummaryItem(BaseModel):
    external_protection: SourceExternalProtectionOut | None = None
    source_id: uuid.UUID
    source_name: str
    dataset_id: uuid.UUID | None
    dataset_name: str | None
    enabled: bool
    unregistered_at: datetime | None
    diagnostics: StorageSourceDiagnostics


class StorageSourceJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    status: str
    created_at: datetime
    completed_at: datetime | None


class StorageSourceJobs(BaseModel):
    items: list[StorageSourceJob]
    total: int


class StorageSourceUsage(BaseModel):
    scope: Literal["visible_current_accepted_source_heads"]
    source: Literal["accepted_source_index"]
    unit: Literal["bytes"]
    coverage: Literal["complete", "partial", "none"]
    is_estimate: bool
    deduplication_basis: Literal["location_frozen_address_and_object_version"]
    includes_versions: bool
    reference_count: int
    file_count: int
    duplicate_reference_count: int
    known_size_bytes: int
    total_size_bytes: int | None
    unknown_reference_count: int
    conflict_file_count: int
    incomparable_file_count: int
    inactive_reference_count: int
    observed_from: datetime | None
    observed_through: datetime | None
    measured_at: datetime


class StorageSourceSummary(BaseModel):
    location_id: uuid.UUID
    items: list[StorageSourceSummaryItem]
    total: int
    dataset_count: int = 0
    usage: StorageSourceUsage
    jobs: StorageSourceJobs


class DirectoryEntry(BaseModel):
    name: str
    relative_path: str


class StorageDirectoryPage(BaseModel):
    entries: list[DirectoryEntry]
    next_cursor: str | None


class StorageProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(
        default="api", min_length=1, max_length=248, pattern=r"^[a-zA-Z0-9_.@:-]+$"
    )
    sample_file: str | None = Field(default=None, max_length=1000)


class StorageProbeOut(BaseModel):
    node_id: str
    config_revision: int
    observed_at: datetime
    expires_at: datetime
    ok: bool
    code: str
    capabilities: dict[str, str]


class StorageLocationHealth(BaseModel):
    observed_node_count: int = Field(ge=0)
    current_success_count: int = Field(ge=0)
    current_failure_count: int = Field(ge=0)
    stale_node_count: int = Field(ge=0)
    latest: StorageProbeOut | None
    measured_at: datetime
    fresh_until: datetime | None


class StorageLocationListItem(StorageLocationOut):
    health: StorageLocationHealth
    source_usage: StorageSourceUsage


class StorageLocationList(BaseModel):
    items: list[StorageLocationListItem]
    total: int


class StorageBindingOut(BaseModel):
    purpose: str
    location_id: uuid.UUID | None = None
    generation: int | None = None
    location_config_revision: int | None = None
    bucket: str | None = None
    state: Literal["missing", "configured", "mismatch"]
    read_policy: Literal["consumer_owned_compatibility"]
    write_policy: Literal["deployment_mapping", "current_binding"]
    change_policy: Literal[
        "legacy_mapping_locked",
        "reviewed_avatar_switch",
        "reviewed_bug_attachment_switch",
        "reviewed_audit_archive_switch",
        "reviewed_export_switch",
        "reviewed_import_switch",
        "reviewed_dataset_switch",
        "reviewed_media_cache_switch",
        "reviewed_annotations_switch",
        "reviewed_training_switch",
    ]


class AvatarBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class AvatarBindingCandidates(BaseModel):
    items: list[AvatarBindingCandidate]
    total: int


class AvatarBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class AvatarBindingApply(AvatarBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class AvatarBindingTarget(AvatarBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class AvatarBindingHistory(BaseModel):
    pending: int
    ready: int
    retired: int
    unlocated_users: int


class AvatarBindingPreview(BaseModel):
    purpose: Literal["avatars"] = "avatars"
    previous: StorageBindingOut
    target: AvatarBindingTarget
    history: AvatarBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class BugAttachmentBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class BugAttachmentBindingCandidates(BaseModel):
    items: list[BugAttachmentBindingCandidate]
    total: int


class BugAttachmentBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class BugAttachmentBindingApply(BugAttachmentBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class BugAttachmentBindingTarget(BugAttachmentBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class BugAttachmentBindingHistory(BaseModel):
    pending: int
    ready: int
    retired: int


class BugAttachmentBindingPreview(BaseModel):
    purpose: Literal["bug-reports"] = "bug-reports"
    previous: StorageBindingOut
    target: BugAttachmentBindingTarget
    history: BugAttachmentBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class AuditArchiveBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class AuditArchiveBindingCandidates(BaseModel):
    items: list[AuditArchiveBindingCandidate]
    total: int


class AuditArchiveBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class AuditArchiveBindingApply(AuditArchiveBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class AuditArchiveBindingTarget(AuditArchiveBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class AuditArchiveBindingHistory(BaseModel):
    pending: int
    ready: int


class AuditArchiveBindingPreview(BaseModel):
    purpose: Literal["audit-archive"] = "audit-archive"
    previous: StorageBindingOut
    target: AuditArchiveBindingTarget
    history: AuditArchiveBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class ExportBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class ExportBindingCandidates(BaseModel):
    items: list[ExportBindingCandidate]
    total: int


class ExportBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class ExportBindingApply(ExportBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class ExportBindingTarget(ExportBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class ExportBindingHistory(BaseModel):
    zip_pending: int
    zip_ready: int
    zip_retired: int
    media_building: int
    media_ready: int
    media_retiring: int
    media_deleted: int


class ExportBindingPreview(BaseModel):
    purpose: Literal["export"] = "export"
    previous: StorageBindingOut
    target: ExportBindingTarget
    history: ExportBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class ImportBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class ImportBindingCandidates(BaseModel):
    items: list[ImportBindingCandidate]
    total: int


class ImportBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class ImportBindingApply(ImportBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class ImportBindingTarget(ImportBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class ImportBindingHistory(BaseModel):
    lease_active: int
    lease_cleanup_pending: int
    lease_deleted: int
    creation_staging: int
    mask_uploads: int
    mask_batches: int


class ImportBindingPreview(BaseModel):
    purpose: Literal["import"] = "import"
    previous: StorageBindingOut
    target: ImportBindingTarget
    history: ImportBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class DatasetBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class DatasetBindingCandidates(BaseModel):
    items: list[DatasetBindingCandidate]
    total: int


class DatasetBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class DatasetBindingApply(DatasetBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class DatasetBindingTarget(DatasetBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class DatasetBindingHistory(BaseModel):
    asset_locations: int
    creation_allocations: int
    auxiliary_originals: int
    retirements: int


class DatasetBindingPreview(BaseModel):
    purpose: Literal["datasets"] = "datasets"
    previous: StorageBindingOut
    target: DatasetBindingTarget
    history: DatasetBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class AnnotationsBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class AnnotationsBindingCandidates(BaseModel):
    items: list[AnnotationsBindingCandidate]
    total: int


class AnnotationsBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class AnnotationsBindingApply(AnnotationsBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class AnnotationsBindingTarget(AnnotationsBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class AnnotationsBindingHistory(BaseModel):
    task_originals: int
    creation_allocations: int
    raster_mask_objects: int
    comment_attachments: int
    guide_assets: int


class AnnotationsBindingPreview(BaseModel):
    purpose: Literal["annotations"] = "annotations"
    previous: StorageBindingOut
    target: AnnotationsBindingTarget
    history: AnnotationsBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class TrainingBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class TrainingBindingCandidates(BaseModel):
    items: list[TrainingBindingCandidate]
    total: int


class TrainingBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class TrainingBindingApply(TrainingBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class TrainingBindingTarget(TrainingBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class TrainingBindingHistory(BaseModel):
    training_versions: int
    training_runs: int
    training_previews: int
    sample_evaluations: int
    active_learning_rounds: int
    deletion_intents: int


class TrainingBindingPreview(BaseModel):
    purpose: Literal["training"] = "training"
    previous: StorageBindingOut
    target: TrainingBindingTarget
    history: TrainingBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class MediaCacheBindingCandidate(BaseModel):
    location_id: uuid.UUID
    name: str
    config_revision: int


class MediaCacheBindingCandidates(BaseModel):
    items: list[MediaCacheBindingCandidate]
    total: int


class MediaCacheBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    target_location_id: uuid.UUID
    target_config_revision: int = Field(ge=1)


class MediaCacheBindingApply(MediaCacheBindingChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class MediaCacheBindingTarget(MediaCacheBindingCandidate):
    bucket: str
    key_prefix: str
    connection_revision: int | None = None


class MediaCacheBindingHistory(BaseModel):
    directory_previews: int
    original_cache_entries: int
    image_pyramid_outputs: int
    video_derivative_outputs: int
    thumbnail_outputs: int
    legacy_thumbnails: int


class MediaCacheBindingPreview(BaseModel):
    purpose: Literal["media-cache"] = "media-cache"
    previous: StorageBindingOut
    target: MediaCacheBindingTarget
    history: MediaCacheBindingHistory
    capabilities: dict[str, bool]
    can_apply: bool
    measured_at: datetime
    expires_at: datetime
    impact_token: str


class StorageFaultOut(BaseModel):
    id: uuid.UUID
    location_id: uuid.UUID
    source_id: uuid.UUID | None
    dataset_id: uuid.UUID | None
    item_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    job_id: uuid.UUID | None
    registration_id: uuid.UUID | None
    source_name: str | None
    node_id: str
    operation: Literal["scan", "scan_content", "reference_read", "preview_generate"]
    category: Literal[
        "root_unavailable", "access_denied", "source_changed", "preview_failed"
    ]
    state: Literal["active", "recovered"]
    first_failed_at: datetime
    last_failed_at: datetime
    recovered_at: datetime | None
    failure_count: int
    scope_status: Literal["current", "historical", "unknown"]
    responsible_role: Literal["platform_admin", "dataset_admin"]
    responsible_user_id: None = None


class StorageFaultCounts(BaseModel):
    active: int
    recovered: int


class StorageObservationRateCounts(BaseModel):
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    failure_rate: float | None = Field(ge=0, le=1)


class StorageObservationRates(BaseModel):
    measured_at: datetime
    window_start: datetime
    window_end: datetime
    timezone: Literal["UTC"]
    unit: Literal["attempt"]
    coverage: Literal["recorded_attempts_only"]
    original_read: StorageObservationRateCounts
    preview_generate: StorageObservationRateCounts


class StorageFaultList(BaseModel):
    items: list[StorageFaultOut]
    total: int
    counts: StorageFaultCounts
    rates: StorageObservationRates | None = None
    observation_only: Literal[True] = True
