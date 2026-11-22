import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GPUBackendMembership(Base):
    """Durable closed-domain membership for one backend and GPU resource."""

    __tablename__ = "gpu_backend_memberships"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'active', 'retiring')",
            name="ck_gpu_backend_memberships_state",
        ),
        CheckConstraint(
            "membership_epoch > 0",
            name="ck_gpu_backend_memberships_epoch_positive",
        ),
        CheckConstraint(
            "runtime_epoch_baseline >= 0",
            name="ck_gpu_backend_memberships_runtime_baseline_nonnegative",
        ),
        CheckConstraint(
            "vram_budget_mb > 0",
            name="ck_gpu_backend_memberships_budget_positive",
        ),
        CheckConstraint(
            "gpu_resource_id = btrim(gpu_resource_id) AND "
            "gpu_resource_id !~ '[[:space:],]' AND "
            "position('/' in gpu_resource_id) > 1 AND "
            "position('/' in gpu_resource_id) < char_length(gpu_resource_id)",
            name="ck_gpu_backend_memberships_resource_id",
        ),
        CheckConstraint(
            "max_concurrency > 0 AND max_concurrency <= 10000",
            name="ck_gpu_backend_memberships_concurrency",
        ),
        CheckConstraint(
            "(state = 'retiring') = (retired_at IS NOT NULL)",
            name="ck_gpu_backend_memberships_retired_at",
        ),
        CheckConstraint(
            "(state = 'retiring') = (retirement_id IS NOT NULL)",
            name="ck_gpu_backend_memberships_retirement_id",
        ),
        CheckConstraint(
            "state <> 'retiring' OR ("
            "retired_generation_high_water IS NOT NULL AND "
            "retired_control_epoch_high_water IS NOT NULL AND "
            "retired_runtime_epoch_high_water IS NOT NULL)",
            name="ck_gpu_backend_memberships_retired_fence",
        ),
        CheckConstraint(
            "(state = 'retiring') = (retire_reason IS NOT NULL) AND "
            "(retire_reason IS NULL OR retire_reason IN ("
            "'registry_deleted', 'claim_removed', 'resource_moved', "
            "'managed_retirement'))",
            name="ck_gpu_backend_memberships_retire_reason",
        ),
        CheckConstraint(
            "(retired_generation_high_water IS NULL OR "
            "retired_generation_high_water >= 0) AND "
            "(retired_control_epoch_high_water IS NULL OR "
            "retired_control_epoch_high_water >= 0) AND "
            "(retired_runtime_epoch_high_water IS NULL OR "
            "retired_runtime_epoch_high_water >= 0)",
            name="ck_gpu_backend_memberships_retired_fence_nonnegative",
        ),
        CheckConstraint(
            "state = 'retiring' OR ("
            "retired_health_state IS NULL AND "
            "retired_health_meta IS NULL AND "
            "retired_health_checked_at IS NULL AND "
            "retired_generation_high_water IS NULL AND "
            "retired_control_epoch_high_water IS NULL AND "
            "retired_runtime_epoch_high_water IS NULL AND "
            "retired_token_expiry_high_water IS NULL)",
            name="ck_gpu_backend_memberships_current_has_no_retired_evidence",
        ),
        Index(
            "ix_gpu_backend_memberships_resource_state",
            "gpu_resource_id",
            "state",
        ),
        Index(
            "uq_gpu_backend_memberships_current_backend",
            "backend_registry_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'active')"),
        ),
    )

    backend_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "gpu_backend_fences.backend_registry_id",
            name="fk_gpu_backend_memberships_fence",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    gpu_resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    membership_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_epoch_baseline: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    vram_budget_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    eviction_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )

    retirement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retire_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retired_health_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    retired_health_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    retired_health_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_generation_high_water: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    retired_control_epoch_high_water: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    retired_runtime_epoch_high_water: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    retired_token_expiry_high_water: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
