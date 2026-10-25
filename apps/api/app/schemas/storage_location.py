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
    root_handle: str | None = None
    relative_path: str | None = None
    scope: str | None = None
    use_user_ids: list[uuid.UUID] = Field(default_factory=list)
    declared_capabilities: dict[str, bool]
    cache_policy: CachePolicy = "preview"
    original_cache_quota_bytes: int = 0
    original_cache_ttl_seconds: int = 86400
    allowed_actions: list[str] = Field(default_factory=list)


class StorageLocationList(BaseModel):
    items: list[StorageLocationOut]
    total: int


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
