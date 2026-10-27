"""Public, locator-free observations of registered retirement assertions."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ManagedOriginalReclamationCoverage(BaseModel):
    historical_locators: Literal["not_backfilled"] = "not_backfilled"
    unregistered_originals: Literal["not_recorded"] = "not_recorded"
    object_presence: Literal["not_checked"] = "not_checked"
    exclusive_ownership: Literal["not_established"] = "not_established"
    backup_holds: Literal["unknown"] = "unknown"
    migration_holds: Literal["unknown"] = "unknown"
    declared_requirements: Literal["registered_retirement_peers_only"] = (
        "registered_retirement_peers_only"
    )
    training_snapshots: Literal["recorded_supported_shapes"] = (
        "recorded_supported_shapes"
    )
    active_learning_roots: Literal["recorded_members_and_source_snapshots"] = (
        "recorded_members_and_source_snapshots"
    )


class ManagedOriginalReclamationCounts(BaseModel):
    total_assertions: int
    retained_assertions: int
    protected_assertions: int
    blocked_assertions: int
    policy_candidate_assertions: int
    unknown_size_assertions: int
    known_asserted_bytes: int
    unknown_training_versions: int
    unknown_active_learning_rounds: int


class ManagedOriginalReclamationRoots(BaseModel):
    asset_locations: int
    dataset_items: int
    tasks: int
    task_dataset_links: int
    training_versions: int
    active_learning_roots: int
    ambiguous_legacy_tasks: int


ReclamationBlocker = Literal[
    "current_asset_location",
    "current_dataset_item",
    "current_task",
    "current_task_dataset_link",
    "retained_training_version",
    "active_learning_history",
    "indefinite_retention",
    "retention_window",
    "related_indefinite_retention",
    "related_retention_window",
    "declared_backup_hold",
    "declared_migration_hold",
    "storage_identity_unknown",
    "storage_identity_mismatch",
    "content_identity_unverified",
    "locator_unverified",
    "unsupported_training_snapshot",
    "unsupported_active_learning_snapshot",
    "legacy_task_bucket_unknown",
    "exclusive_ownership_unproven",
    "backup_holds_unknown",
    "migration_holds_unknown",
    "object_presence_not_checked",
]


class ManagedOriginalReclamationItem(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    dataset_id: uuid.UUID
    dataset_item_id: uuid.UUID
    retired_at: datetime
    policy_revision: int
    retain_until: datetime | None
    effective_retain_until: datetime | None
    indefinite_retention: bool
    peer_assertions: int
    declared_backup_holds: int
    declared_migration_holds: int
    size_bytes: int | None
    identity_state: Literal["current", "unknown", "mismatch"]
    status: Literal["retained", "protected", "blocked"]
    policy_candidate: bool
    roots: ManagedOriginalReclamationRoots
    blockers: list[ReclamationBlocker]


class ManagedOriginalReclamationPreview(BaseModel):
    measured_at: datetime
    scope: Literal["registered_managed_original_retirement_assertions"] = (
        "registered_managed_original_retirement_assertions"
    )
    unit: Literal["retirement_assertion"] = "retirement_assertion"
    execution_permission: Literal[False] = False
    coverage: ManagedOriginalReclamationCoverage
    counts: ManagedOriginalReclamationCounts
    # Assertion sizes can repeat a physical copy and do not establish its presence.
    reclaimable_bytes: None = None
    items: list[ManagedOriginalReclamationItem]
    limit: int
    offset: int
    has_more: bool
