import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserInvitation(Base):
    __tablename__ = "user_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 独立于平台 role 的项目角色。NULL = 历史账号级邀请 / 尚未回填；
    # 与 project_id 配对：项目邀请必须有兼容的 project_role，账号级邀请不得携带。
    project_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(128))
    # Optional project target.  This deliberately is not a foreign key: a
    # deleted project must not block project deletion, and a stale invitation
    # must fail closed at acceptance instead of silently becoming a legacy
    # account invitation.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def is_pending(self) -> bool:
        if self.accepted_at is not None:
            return False
        if self.revoked_at is not None:
            return False
        return self.expires_at > datetime.now(timezone.utc)

    @property
    def status(self) -> str:
        if self.accepted_at is not None:
            return "accepted"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= datetime.now(timezone.utc):
            return "expired"
        return "pending"
