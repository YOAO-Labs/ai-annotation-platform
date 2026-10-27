"""Bounded transport receipts; AsyncJob remains the business task owner."""

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SiteAgentCommand(Base):
    __tablename__ = "site_agent_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id", "credential_revision"],
            ["agent_credentials.agent_id", "agent_credentials.revision"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('queued','leased','received','applied','rejected','unsettled')",
            name="ck_agent_command_state",
        ),
        CheckConstraint(
            "credential_revision >= 1 AND received_bytes BETWEEN 0 AND 67108864 AND priority BETWEEN 0 AND 2",
            name="ck_agent_command_limits",
        ),
        CheckConstraint(
            "command_digest ~ '^[a-f0-9]{64}$' AND (result_digest IS NULL OR result_digest ~ '^[a-f0-9]{64}$')",
            name="ck_agent_command_digest",
        ),
        CheckConstraint(
            "octet_length(payload::text) <= 16384 AND octet_length(chunks::text) <= 262144 AND (result IS NULL OR octet_length(result::text) <= 2097152)",
            name="ck_agent_command_payload",
        ),
        CheckConstraint(
            "(result IS NULL) = (result_digest IS NULL) AND (result IS NULL) = (closed IS NULL)",
            name="ck_agent_command_result",
        ),
        Index("ix_agent_commands_queue", "agent_id", "state", "priority", "created_at"),
        Index("ix_agent_commands_location", "location_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[uuid.UUID]
    credential_revision: Mapped[int]
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT")
    )
    operation_id: Mapped[uuid.UUID | None]
    payload: Mapped[dict] = mapped_column(JSONB)
    command_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int]
    lease_session_id: Mapped[uuid.UUID | None]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_bytes: Mapped[int] = mapped_column(default=0)
    relay_released: Mapped[bool] = mapped_column(default=True)
    chunks: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    closed: Mapped[bool | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
