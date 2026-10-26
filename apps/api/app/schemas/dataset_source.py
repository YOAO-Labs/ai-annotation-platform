"""Inventory is deliberately separate from consumable dataset samples."""

from datetime import datetime
import uuid
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.storage_cache import CachePolicy


class ExternalProtectionDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    responsible_party: str = Field(
        min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$"
    )
    recovery_method: Literal["external_backup", "reacquire", "unrecoverable", "unknown"]
    recovery_reference: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class SourceExternalProtectionUpdate(ExternalProtectionDeclaration):
    expected_revision: int = Field(ge=0)


class SourceExternalProtectionOut(ExternalProtectionDeclaration):
    revision: int = Field(ge=1)
    recorded_at: datetime
    recorded_by_id: uuid.UUID


class DatasetSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: uuid.UUID
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    relative_path: str = Field(default="", max_length=1000)
    node_id: str = Field(min_length=1, max_length=248, pattern=r"^[a-zA-Z0-9_.@:-]+$")
    recursive: bool = True
    include_globs: list[str] = Field(
        default_factory=lambda: ["*"], min_length=1, max_length=20
    )
    exclude_globs: list[str] = Field(default_factory=list, max_length=20)
    ownership_mode: Literal["reference", "managed_copy"] = "reference"
    cache_policy: CachePolicy = "preview"


class DatasetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    cache_policy: CachePolicy | None = None


class AgentSourceStatus(BaseModel):
    state: Literal[
        "online", "offline", "revoked", "pending", "expired", "degraded", "unavailable"
    ]
    reason_code: str
    site_name: str | None = None
    agent_name: str | None = None
    last_seen: datetime | None = None
    measured_at: datetime


class DatasetSourceOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID | None
    location_id: uuid.UUID
    name: str
    config_revision: int
    enabled: bool
    mode: str = "inventory"
    relative_path: str
    node_id: str
    recursive: bool
    include_globs: list[str]
    exclude_globs: list[str] = Field(default_factory=list)
    ownership_mode: Literal["reference", "managed_copy"]
    cache_policy: CachePolicy
    effective_cache_policy: CachePolicy
    location_cache_policy: CachePolicy
    policy_revision: int
    unregistered_at: datetime | None
    external_protection: SourceExternalProtectionOut | None = None
    agent_status: AgentSourceStatus | None = None


class DatasetSourceList(BaseModel):
    items: list[DatasetSourceOut]
    total: int


class DatasetScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_id: uuid.UUID
    source_revision: int
    async_job_id: uuid.UUID | None
    status: str
    phase: str
    complete: bool
    generation: int
    error_code: str | None
    counts: dict[str, int]
    created_at: datetime
    completed_at: datetime | None


class DatasetScanList(BaseModel):
    items: list[DatasetScanOut]
    total: int


class DatasetScanEntryOut(BaseModel):
    path_digest: str
    relative_path: str
    kind: str
    state: str
    size_bytes: int | None
    content_verified: bool = False
    registered_item_id: uuid.UUID | None = None
    preview_state: str | None = None
    preview_bytes: int | None = None
    verified_content: dict | None = None


class VerifiedScanContent(BaseModel):
    sha256: str
    size_bytes: int
    media_type: str
    width: int
    height: int


class DatasetScanEntryList(BaseModel):
    items: list[DatasetScanEntryOut]
    total: int


class RegisteredReferenceOut(BaseModel):
    item_id: uuid.UUID
    created: bool
    sha256: str


class DatasetRegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    scan_run_id: uuid.UUID
    source_id: uuid.UUID
    async_job_id: uuid.UUID | None
    phase: str
    status: str
    generation: int
    error_code: str | None
    counts: dict[str, int]
    created_at: datetime
    completed_at: datetime | None
    ownership_mode: str
    cache_policy: str
    source_revision: int
    comparison_available: bool
    comparison_complete: bool
    registration_scope: str


class DatasetRegistrationConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all_safe", "additions_only"] = "all_safe"


class DatasetSourceChangeOut(BaseModel):
    path_digest: str
    relative_path: str
    kind: Literal["added", "unchanged", "changed", "not_seen", "excluded", "unreadable"]
    reason: str | None
    before_item_id: uuid.UUID | None
    before_revision_id: uuid.UUID | None
    accepted_revision_id: uuid.UUID | None
    accepted_item_id: uuid.UUID | None
    can_accept_revision: bool
    can_revalidate: bool
    can_relocate: bool
    relocated_to_path: str | None
    before_sha256: str | None
    candidate_sha256: str | None
    registration_state: str | None
    error_code: str | None


class SourceMoveCandidate(BaseModel):
    path_digest: str
    relative_path: str
    candidate_sha256: str


class SourceMoveCandidateList(BaseModel):
    total: int
    items: list[SourceMoveCandidate]


class SourceRevisionAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=0)
    before_revision_id: uuid.UUID
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceRevisionResult(BaseModel):
    revision_id: uuid.UUID
    previous_revision_id: uuid.UUID | None
    revision_number: int
    item_id: uuid.UUID
    sha256: str
    created: bool


class SourceRevisionOut(SourceRevisionResult):
    acceptance_order: int
    previous_path_digest: str | None
    acceptance_kind: Literal[
        "initial", "content_revision", "observation_revalidation", "relocation"
    ]
    path_digest: str
    scan_run_id: uuid.UUID
    registration_id: uuid.UUID | None
    accepted_by_id: uuid.UUID | None
    accepted_at: datetime | None
    item_available: bool


class SourceRevisionList(BaseModel):
    total: int
    items: list[SourceRevisionOut]


class DatasetSourceChangeList(BaseModel):
    remaining_added_count: int
    complete: bool
    observed_at: datetime
    counts: dict[str, int]
    total: int
    items: list[DatasetSourceChangeOut]


class DatasetRegistrationEntryOut(BaseModel):
    path_digest: str
    relative_path: str
    state: str
    item_id: uuid.UUID | None
    error_code: str | None
    attempts: int
    size_bytes: int | None
    sha256: str | None
    preview_estimated_bytes: int | None = None
    preview_error: str | None = None


class SourceLifecycleImpact(BaseModel):
    source_id: uuid.UUID
    expected_revision: int
    proof: str
    item_count: int
    project_count: int
    task_count: int
    annotation_count: int
    active_attempt_count: int
    preview_count: int
    preview_bytes: int
    measured_at: datetime


class SourceLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "enable", "unregister"]
    expected_revision: int = Field(ge=1)
    impact_proof: str


class DatasetRegistrationEntryList(BaseModel):
    items: list[DatasetRegistrationEntryOut]
    total: int


class SourceSyncScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool
    interval_minutes: int = Field(default=60, ge=15, le=10080)
    new_file_policy: Literal["manual", "pure_additions"] = "manual"
    timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value in {"localtime", "Factory"} or value.startswith(("posix/", "right/")):
            raise ValueError("Use an IANA timezone such as Asia/Shanghai")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone such as Asia/Shanghai") from None
        return value


class SourceSyncScheduleContinue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    scan_id: uuid.UUID


class SourceSyncScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    source_id: uuid.UUID
    revision: int
    enabled: bool
    interval_minutes: int
    new_file_policy: Literal["manual", "pure_additions"]
    timezone: str
    actor_id: uuid.UUID
    state: Literal[
        "idle", "scanning", "preflighting", "awaiting_review", "registering", "blocked"
    ]
    scan_id: uuid.UUID | None
    registration_id: uuid.UUID | None
    next_run_at: datetime | None
    last_checked_at: datetime | None
    last_started_at: datetime | None
    last_error: str | None


class SourceRulesPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    recursive: bool
    include_globs: list[str] = Field(min_length=1, max_length=20)
    exclude_globs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("include_globs", "exclude_globs")
    @classmethod
    def valid_globs(cls, values):
        if any(not value or len(value) > 1000 or "\x00" in value for value in values):
            raise ValueError("Invalid source filter")
        return values


class SourceRulesApply(SourceRulesPreview):
    impact_proof: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceRulesImpactItem(BaseModel):
    path_digest: str
    relative_path: str
    impact: Literal["within_scope", "leaving_scope", "entering_scope", "outside_scope"]


class SourceRulesImpact(BaseModel):
    source_id: uuid.UUID
    expected_revision: int
    proof: str
    counts: dict[str, int]
    total: int
    items: list[SourceRulesImpactItem]
    active_attempt_count: int
