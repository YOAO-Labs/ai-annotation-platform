"""Durable inventory contracts, independent of short-lived job history."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetSource(Base):
    __tablename__ = "dataset_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"), index=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB)
    config_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    root_identity: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    ownership_mode: Mapped[str] = mapped_column(
        String(20), default="reference", server_default="reference"
    )
    cache_policy: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none"
    )
    policy_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    external_protection_revision: Mapped[int] = mapped_column(
        default=0, server_default="0"
    )
    external_protection: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    unregistered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(external_protection_revision = 0 AND external_protection IS NULL) OR "
            "(external_protection_revision > 0 AND "
            "COALESCE(jsonb_typeof(external_protection) = 'object', false))",
            name="ck_dataset_source_external_protection",
        ),
    )


class DatasetScanRun(Base):
    __tablename__ = "dataset_scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    phase: Mapped[str] = mapped_column(
        String(30), default="enumerating", server_default="enumerating"
    )
    complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def source_revision(self):
        return self.snapshot.get("source_revision", 0)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_scan_status",
        ),
        Index(
            "uq_dataset_source_active_scan",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )


class DatasetScanEntry(Base):
    __tablename__ = "dataset_scan_entries"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    observed: Mapped[dict] = mapped_column(JSONB)
    verified_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    directory_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validated_generation: Mapped[int] = mapped_column(default=0, server_default="0")

    __table_args__ = (
        Index(
            "ix_dataset_scan_work", "run_id", "kind", "state", "validated_generation"
        ),
    )


class DatasetReference(Base):
    """A business item permanently names one observed, verified content revision."""

    __tablename__ = "dataset_references"

    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    path_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[int] = mapped_column(nullable=False)
    location_revision: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "source_id", "path_digest", "run_id", name="uq_reference_source_observation"
        ),
    )


class DatasetRegistrationRun(Base):
    """Durable preflight/registration progress, independent of job retention."""

    __tablename__ = "dataset_registration_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT"), unique=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    async_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), unique=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    phase: Mapped[str] = mapped_column(String(20), default="preflight")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    generation: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(60))
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comparison_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_registration_status",
        ),
        CheckConstraint(
            "phase IN ('preflight','registering')", name="ck_dataset_registration_phase"
        ),
        Index(
            "uq_dataset_source_active_registration",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    @property
    def source_revision(self):
        return self.snapshot["source_revision"]

    @property
    def ownership_mode(self):
        return self.snapshot.get("ownership_mode", "reference")

    @property
    def cache_policy(self):
        return self.snapshot.get("cache_policy", "none")

    @property
    def comparison_available(self):
        return self.snapshot.get("comparison_schema") == 1

    @property
    def registration_scope(self):
        return self.snapshot.get("registration_scope", "all_safe")


class DatasetRegistrationEntry(Base):
    __tablename__ = "dataset_registration_entries"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_registration_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(60))
    attempts: Mapped[int] = mapped_column(default=0)
    refresh_unverified: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_estimated_bytes: Mapped[int | None] = mapped_column(BigInteger)
    preview_error: Mapped[str | None] = mapped_column(String(60))
    original_estimated_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','ready','registered','unchanged','error')",
            name="ck_dataset_registration_entry_state",
        ),
        Index("ix_dataset_registration_work", "run_id", "state", "path_digest"),
    )


class DatasetSourceRevision(Base):
    """Append-only accepted identities, independent of business deletion."""

    __tablename__ = "dataset_source_revisions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), index=True
    )
    path_digest: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[uuid.UUID] = mapped_column()
    acceptance_order: Mapped[int] = mapped_column(
        BigInteger,
        server_default=text("nextval('dataset_source_acceptance_order_seq')"),
        unique=True,
    )
    revision_number: Mapped[int] = mapped_column()
    item_id_at_accept: Mapped[uuid.UUID] = mapped_column()
    asset_id_at_accept: Mapped[uuid.UUID] = mapped_column()
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="SET NULL"), index=True
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="SET NULL")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    ownership_mode: Mapped[str] = mapped_column(String(20))
    owns_item: Mapped[bool] = mapped_column(Boolean)
    previous_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_source_revisions.id", ondelete="RESTRICT")
    )
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_registration_runs.id", ondelete="RESTRICT")
    )
    accepted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "source_id",
            "path_digest",
            "revision_number",
            name="uq_source_revision_number",
        ),
        UniqueConstraint(
            "source_id", "path_digest", "run_id", name="uq_source_revision_observation"
        ),
        UniqueConstraint(
            "registration_id", "path_digest", name="uq_source_revision_resolution"
        ),
        CheckConstraint("revision_number > 0", name="ck_source_revision_number"),
        CheckConstraint(
            "ownership_mode IN ('reference','managed_copy')",
            name="ck_source_revision_ownership",
        ),
    )


class DatasetSourceItem(Base):
    """Current accepted path head; immutable revisions retain previous identities."""

    __tablename__ = "dataset_source_items"
    current_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_source_revisions.id", ondelete="RESTRICT")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column()
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_items.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="CASCADE")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    ownership_mode: Mapped[str] = mapped_column(String(20))
    owns_item: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "ownership_mode IN ('reference','managed_copy')",
            name="ck_source_item_ownership",
        ),
    )


class DatasetSourcePreview(Base):
    """One source-owned derivative; never an original or a presigned thumbnail."""

    __tablename__ = "dataset_source_previews"
    thumbnail_output_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("thumbnail_outputs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_assets.id", ondelete="CASCADE")
    )
    source_sha256: Mapped[str] = mapped_column(String(64))
    spec: Mapped[str] = mapped_column(String(60))
    policy_revision: Mapped[int] = mapped_column()
    generation: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(60))
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("storage_locations.id")
    )
    bucket: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str | None] = mapped_column(String(1000))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index(
            "ix_source_preview_usage_locator", func.md5(bucket), func.md5(object_key)
        ),
        ForeignKeyConstraint(
            ["source_id", "path_digest"],
            ["dataset_source_items.source_id", "dataset_source_items.path_digest"],
            ondelete="CASCADE",
        ),
        CheckConstraint("state IN ('ready','failed')", name="ck_source_preview_state"),
    )


class DatasetSourceChange(Base):
    """A preflight's frozen baseline and verified differences, beyond job history."""

    __tablename__ = "dataset_source_changes"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_registration_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT")
    )
    # Historical IDs survive explicit business item deletion.
    before_item_id: Mapped[uuid.UUID | None] = mapped_column()
    before_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_source_revisions.id", ondelete="RESTRICT")
    )
    before_sha256: Mapped[str | None] = mapped_column(String(64))
    before_observation: Mapped[dict | None] = mapped_column(JSONB)
    before_anchor: Mapped[dict | None] = mapped_column(JSONB)
    candidate_sha256: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(60))
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pending','added','unchanged','changed','not_seen','excluded','unreadable')",
            name="ck_dataset_source_change_kind",
        ),
        Index("ix_dataset_source_change_page", "run_id", "kind", "path_digest"),
    )


class DatasetSourceSyncSchedule(Base):
    """Durable, revocable delegation; no bearer token or API-key plaintext."""

    __tablename__ = "dataset_source_sync_schedules"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(default=60)
    new_file_policy: Mapped[str] = mapped_column(
        String(30), default="manual", server_default="manual"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC"
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column()
    key_proof: Mapped[str | None] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(30), default="idle")
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT")
    )
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_registration_runs.id", ondelete="RESTRICT")
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(60))
    dispatch_token: Mapped[uuid.UUID | None] = mapped_column()
    dispatch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    __table_args__ = (
        CheckConstraint(
            "interval_minutes BETWEEN 15 AND 10080", name="ck_source_sync_interval"
        ),
        CheckConstraint(
            "new_file_policy IN ('manual','pure_additions')",
            name="ck_source_sync_new_file_policy",
        ),
        CheckConstraint(
            "state IN ('idle','scanning','preflighting','awaiting_review','registering','blocked')",
            name="ck_source_sync_state",
        ),
        Index("ix_source_sync_due", "enabled", "next_check_at", "source_id"),
    )
