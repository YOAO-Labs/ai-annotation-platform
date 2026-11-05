"""Durable ownership for isolated fixture runs and their admissions."""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.managed_original_creation import ManagedOriginalCreation


class FixtureOriginalTarget(Base):
    __tablename__ = "fixture_original_targets"
    __table_args__ = (
        UniqueConstraint(
            "storage_identity", name="uq_fixture_original_target_identity"
        ),
        CheckConstraint(
            "generation >= 1 AND length(bucket) BETWEEN 1 AND 255 "
            "AND storage_identity ~ '^[0-9a-f]{64}$' "
            "AND store_receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_fixture_original_target",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    store_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class FixtureRun(Base):
    __tablename__ = "fixture_runs"
    __table_args__ = (
        CheckConstraint(
            "family IN ('owned', 'screenshot', 'scale')", name="ck_fixture_run_family"
        ),
        CheckConstraint(
            "(parent_run_id IS NULL AND scope_key IS NULL) OR (parent_run_id IS NOT NULL AND scope_key IS NOT NULL AND length(scope_key) > 0)",
            name="ck_fixture_run_parent_scope",
        ),
        CheckConstraint(
            "(original_target_id IS NULL AND original_storage_identity IS NULL AND original_store_receipt_sha256 IS NULL) OR (parent_run_id IS NULL AND original_target_id IS NOT NULL AND original_storage_identity IS NOT NULL AND original_store_receipt_sha256 IS NOT NULL AND original_storage_identity ~ '^[0-9a-f]{64}$' AND original_store_receipt_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_fixture_run_original_store",
        ),
        Index(
            "uq_fixture_run_active_parent_scope",
            "parent_run_id",
            "scope_key",
            unique=True,
            postgresql_where=text("parent_run_id IS NOT NULL AND state <> 'retired'"),
        ),
        CheckConstraint(
            "(state = 'open' AND sealed_at IS NULL AND retired_at IS NULL) OR (state = 'sealing' AND sealed_at IS NOT NULL AND retired_at IS NULL) OR (state = 'retired' AND sealed_at IS NOT NULL AND retired_at IS NOT NULL)",
            name="ck_fixture_run_state",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT")
    )
    scope_key: Mapped[str | None] = mapped_column(String(100))
    family: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="open"
    )
    original_target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_original_targets.id", ondelete="RESTRICT")
    )
    original_storage_identity: Mapped[str | None] = mapped_column(String(64))
    original_store_receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FixtureAuditEvent(Base):
    __tablename__ = "fixture_audit_events"
    __table_args__ = {"schema": "yoao_fixture_v1"}

    audit_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    audit_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class FixtureRunResource(Base):
    __tablename__ = "fixture_run_resources"
    __table_args__ = (
        UniqueConstraint("kind", "resource_id", name="uq_fixture_run_resource_owner"),
        UniqueConstraint(
            "run_id", "kind", "logical_key", name="uq_fixture_run_logical_resource"
        ),
        UniqueConstraint("item_id", name="uq_fixture_creation_item"),
        Index(
            "uq_fixture_original_resource",
            "resource_id",
            unique=True,
            postgresql_where=text("kind IN ('creation', 'auxiliary')"),
        ),
        ForeignKeyConstraint(
            ["run_id", "admission_id"],
            [
                "yoao_fixture_v1.fixture_run_admissions.run_id",
                "yoao_fixture_v1.fixture_run_admissions.id",
            ],
            name="fk_fixture_resource_admission",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(logical_key) > 0", name="ck_fixture_run_logical_key"),
        CheckConstraint(
            "kind IN ('user', 'project', 'dataset', 'item', 'task', 'creation', 'auxiliary', 'annotation', 'registry', 'template', 'reclaim', 'invitation')",
            name="ck_fixture_run_resource_kind",
        ),
        CheckConstraint(
            "state IN ('reserved', 'published', 'retired')",
            name="ck_fixture_run_resource_state",
        ),
        CheckConstraint(
            "(kind = 'creation' AND creation_id IS NOT NULL AND creation_id = resource_id AND admission_id IS NOT NULL AND item_id IS NOT NULL AND item_fingerprint IS NOT NULL AND item_fingerprint ~ '^[0-9a-f]{64}$') OR (kind <> 'creation' AND creation_id IS NULL AND admission_id IS NULL AND item_id IS NULL AND item_fingerprint IS NULL)",
            name="ck_fixture_run_resource_creation",
        ),
        CheckConstraint(
            "storage_identity IS NULL OR (kind = 'dataset' AND storage_identity ~ '^[0-9a-f]{64}$')",
            name="ck_fixture_run_resource_storage_identity",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    logical_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="reserved"
    )
    creation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(ManagedOriginalCreation.__table__.c.id, ondelete="RESTRICT")
    )
    admission_id: Mapped[uuid.UUID | None] = mapped_column()
    storage_identity: Mapped[str | None] = mapped_column(String(64))
    item_id: Mapped[uuid.UUID | None] = mapped_column()
    item_fingerprint: Mapped[str | None] = mapped_column(String(64))


class FixtureRunAdmission(Base):
    __tablename__ = "fixture_run_admissions"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_fixture_run_admission_run_id"),
        CheckConstraint(
            "(state IN ('active', 'unknown') AND settled_at IS NULL) OR (state = 'settled' AND settled_at IS NOT NULL)",
            name="ck_fixture_run_admission_state",
        ),
        Index("ix_fixture_run_admissions_run", "run_id", "state"),
        {"schema": "yoao_fixture_v1"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FixtureImageWorkIntent(Base):
    __tablename__ = "fixture_image_work_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "admission_id"],
            [
                "yoao_fixture_v1.fixture_run_admissions.run_id",
                "yoao_fixture_v1.fixture_run_admissions.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(state = 'prepared' AND job_id IS NULL AND generation IS NULL) OR "
            "(state IN ('admitted', 'settled') AND job_id IS NOT NULL AND generation >= 1)",
            name="ck_fixture_image_work_intent_state",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    admission_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    generation: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="prepared"
    )


class FixtureAuxiliaryIntent(Base):
    __tablename__ = "fixture_auxiliary_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "admission_id"],
            [
                "yoao_fixture_v1.fixture_run_admissions.run_id",
                "yoao_fixture_v1.fixture_run_admissions.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "resource_kind", "creation_id"],
            [
                "yoao_fixture_v1.fixture_run_resources.run_id",
                "yoao_fixture_v1.fixture_run_resources.kind",
                "yoao_fixture_v1.fixture_run_resources.resource_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "resource_kind = 'auxiliary'", name="ck_fixture_auxiliary_resource_kind"
        ),
        CheckConstraint(
            "kind IN ('map', 'raw_lidar')", name="ck_fixture_auxiliary_kind"
        ),
        CheckConstraint(
            "jsonb_typeof(source_contract) = 'object' AND octet_length(source_contract::text) <= 65536",
            name="ck_fixture_auxiliary_contract",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' AND size_bytes BETWEEN 0 AND 268435456",
            name="ck_fixture_auxiliary_content",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    creation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(ManagedOriginalCreation.__table__.c.id, ondelete="RESTRICT"),
        primary_key=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    resource_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="auxiliary"
    )
    admission_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_contract: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FixturePresentationJob(Base):
    __tablename__ = "fixture_presentation_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('batch_predict', 'export')",
            name="ck_fixture_presentation_job_kind",
        ),
        CheckConstraint(
            "state IN ('admitted', 'deleting', 'retired')",
            name="ck_fixture_presentation_job_state",
        ),
        {"schema": "yoao_fixture_v1"},
    )

    job_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="admitted"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FixtureMaintenanceWindow(Base):
    __tablename__ = "fixture_maintenance_windows"
    __table_args__ = (
        CheckConstraint(
            "binding_generation >= 1 AND initial_revision >= 1 AND draining_revision = initial_revision + 1 AND storage_identity ~ '^[0-9a-f]{64}$'",
            name="ck_fixture_maintenance_evidence",
        ),
        CheckConstraint(
            "(state = 'draining' AND revision = draining_revision AND stopped_revision IS NULL AND resumed_revision IS NULL AND resumed_at IS NULL) OR (state = 'stopped' AND stopped_revision IS NOT NULL AND revision = stopped_revision AND stopped_revision = draining_revision + 1 AND resumed_revision IS NULL AND resumed_at IS NULL) OR (state = 'resumed' AND resumed_revision IS NOT NULL AND (stopped_revision IS NULL OR stopped_revision = draining_revision + 1) AND revision = resumed_revision AND resumed_revision = coalesce(stopped_revision, draining_revision) + 1 AND resumed_at IS NOT NULL)",
            name="ck_fixture_maintenance_state",
        ),
        Index(
            "uq_fixture_maintenance_active_location",
            "location_id",
            unique=True,
            postgresql_where=text("state <> 'resumed'"),
        ),
        Index(
            "uq_fixture_maintenance_active_run",
            "run_id",
            unique=True,
            postgresql_where=text("state <> 'resumed'"),
        ),
        {"schema": "yoao_fixture_v1"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("yoao_fixture_v1.fixture_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    binding_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    initial_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    draining_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stopped_revision: Mapped[int | None] = mapped_column(BigInteger)
    resumed_revision: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
