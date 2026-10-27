"""Independent site identities and retained credential revisions."""

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), unique=True)
    region: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentRegistration(Base):
    __tablename__ = "agent_registrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(100))
    protocol_version: Mapped[int] = mapped_column(default=1, server_default="1")
    credential_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    pair_digest: Mapped[str | None] = mapped_column(String(64))
    pair_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draining: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    roots: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    limits: Mapped[dict] = mapped_column(JSONB)
    reported_limits: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "protocol_version = 1 AND credential_revision >= 1",
            name="ck_agent_registration_revision",
        ),
        CheckConstraint(
            "(pair_digest IS NULL) = (pair_expires_at IS NULL) AND (pair_digest IS NULL OR pair_digest ~ '^[a-f0-9]{64}$')",
            name="ck_agent_registration_pair",
        ),
        CheckConstraint(
            "jsonb_typeof(roots) = 'array' AND jsonb_typeof(limits) = 'object' AND (reported_limits IS NULL OR jsonb_typeof(reported_limits) = 'object')",
            name="ck_agent_registration_json",
        ),
        Index("ix_agent_registrations_site_created", "site_id", "created_at", "id"),
    )


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_registrations.id", ondelete="RESTRICT"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND token_digest ~ '^[a-f0-9]{64}$'",
            name="ck_agent_credential_identity",
        ),
    )
