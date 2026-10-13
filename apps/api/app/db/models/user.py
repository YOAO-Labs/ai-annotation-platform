import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Platform role.  New ordinary staff are employees; public self-registration
    # explicitly creates a viewer.  Legacy annotator/reviewer values are only
    # pre-cutover historical data.  The server default matches migration 0174:
    # an insert that omits role creates a usable employee account.
    role: Mapped[str] = mapped_column(
        String(50), default="employee", server_default="employee"
    )
    group_name: Mapped[str | None] = mapped_column(String(100))
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="offline")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Account lifecycle metadata is deliberately separate from ``status``.
    # ``status`` is the transient online/offline heartbeat state; these fields
    # describe why an account stopped authenticating and whether that state is
    # eligible for explicit reactivation.
    disabled_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    disabled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    disabled_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # v0.12.0 · 邮箱验证时间戳。None = 未验证；非空 = 已验证。仅在开放注册 +
    # require_email_verification 打开时作为登录 gate；邀请注册 / 管理员建号恒视为已验证。
    # is_active 保持「停用/注销」单一语义，不背验证含义。
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v0.8.3 · 心跳机制：登录 / POST /me/heartbeat / 关键操作时刷新；
    # Celery beat 据此把 status='online' 但超 OFFLINE_THRESHOLD_MINUTES 未活跃的用户置 offline
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # v0.8.1 · 管理员重置密码后落时间戳，登录后强制跳改密；用户自助改密后清空。
    password_admin_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v0.8.1 · 自助注销冷静期（7 天）：申请时填 reason + scheduled_at = now+7d；
    # 撤销时清三字段；Celery beat 命中 scheduled_at <= now 时执行软删并清三字段。
    deactivation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deactivation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deactivation_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # v0.8.4 · 用户级周目标（标注员任务量目标）；ProjectMember.weekly_target 覆盖优先
    weekly_target_default: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 头像引用。NULL = 首字母回退；`preset:<slug>` = 内置像素头像；`upload:<32位hex>` = 用户上传。
    # 语法与解析集中在 services/avatar.py；upload: 只能由上传端点写入,客户端不可自选。
    avatar_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # v0.9.41 · 用户偏好 JSONB：标注工作台渲染配置（smoothImage / cssImageFilter / 顶点大小等）；schema 在 schemas/user_preferences.py。
    preferences: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
