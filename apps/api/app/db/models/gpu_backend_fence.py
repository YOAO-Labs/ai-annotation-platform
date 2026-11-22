import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GPUBackendFence(Base):
    """Durable monotonic fencing high-water marks for one registry backend."""

    __tablename__ = "gpu_backend_fences"
    __table_args__ = (
        CheckConstraint(
            "generation_high_water >= 0",
            name="ck_gpu_backend_fences_generation_nonnegative",
        ),
        CheckConstraint(
            "control_epoch_high_water >= 0",
            name="ck_gpu_backend_fences_control_epoch_nonnegative",
        ),
        CheckConstraint(
            "runtime_epoch_high_water >= 0",
            name="ck_gpu_backend_fences_runtime_epoch_nonnegative",
        ),
        CheckConstraint(
            "(rollout_control_operation IS NULL "
            "AND rollout_control_transition_id IS NULL "
            "AND rollout_control_epoch IS NULL "
            "AND rollout_control_membership_epoch IS NULL "
            "AND rollout_control_boot_id IS NULL "
            "AND rollout_control_token_expires_at IS NULL) OR "
            "(rollout_control_operation IN "
            "('reset', 'mode_enforce', 'mode_legacy') "
            "AND rollout_control_transition_id IS NOT NULL "
            "AND rollout_control_epoch > 0 "
            "AND rollout_control_membership_epoch > 0 "
            "AND rollout_control_boot_id IS NOT NULL "
            "AND rollout_control_boot_id = btrim(rollout_control_boot_id) "
            "AND rollout_control_boot_id <> '' "
            "AND rollout_control_token_expires_at IS NOT NULL)",
            name="ck_gpu_backend_fences_rollout_control_shape",
        ),
    )

    backend_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    generation_high_water: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    control_epoch_high_water: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    runtime_epoch_high_water: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    token_expiry_high_water: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollout_control_operation: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    rollout_control_transition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    rollout_control_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rollout_control_membership_epoch: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    rollout_control_boot_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    rollout_control_token_expires_at: Mapped[datetime | None] = mapped_column(
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
