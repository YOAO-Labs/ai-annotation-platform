"""Identity-owned account commands used by HTTP and application workflows."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password

from app.db.enums import UserRole
from app.db.models.user import User
from app.services.user_lifecycle import UserLifecycleService


class IdentityCommandService:
    """Own mutable account data and acquire canonical account locks for commands."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def lock_account(
        self, user_id: uuid.UUID, *, nowait: bool = True
    ) -> User | None:
        """Refresh under the canonical account lock, preserving caller wait policy."""
        return (
            await UserLifecycleService.lock_accounts(self.db, [user_id], nowait=nowait)
        ).get(user_id)

    async def lock_account_by_email(self, email: str) -> User | None:
        user_id = await self.db.scalar(select(User.id).where(User.email == email))
        if user_id is None:
            return None
        return await self.lock_account(user_id, nowait=False)

    async def lock_active_account(
        self, user_id: uuid.UUID, *, nowait: bool = True
    ) -> User:
        user = await self.lock_account(user_id, nowait=nowait)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证凭据",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    async def lock_platform_super_admin(
        self, user_id: uuid.UUID, *, nowait: bool = False
    ) -> User:
        """Re-read and hold current super-admin authority through the caller's commit."""
        user = await self.lock_active_account(user_id, nowait=nowait)
        if user.role != UserRole.SUPER_ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要角色权限: super_admin",
            )
        return user

    async def create_open_registration_account(
        self,
        *,
        email: str,
        name: str,
        password_hash: str,
        email_verification_required: bool,
        now: datetime,
    ) -> User:
        user = User(
            email=email,
            name=name,
            password_hash=password_hash,
            role=UserRole.VIEWER.value,
            status="offline" if email_verification_required else "online",
            is_active=True,
            email_verified_at=None if email_verification_required else now,
        )
        self.db.add(user)
        await self.db.flush()
        return user

    async def create_invited_account(
        self,
        *,
        email: str,
        name: str,
        password_hash: str,
        role: str,
        group_name: str | None,
        group_id: uuid.UUID | None,
        now: datetime,
    ) -> User:
        """Stage an account after invitation authority and role checks."""
        user = User(
            email=email,
            name=name,
            password_hash=password_hash,
            role=role,
            group_name=group_name,
            group_id=group_id,
            status="online",
            is_active=True,
            email_verified_at=now,
        )
        self.db.add(user)
        await self.db.flush()
        return user

    async def ensure_import_admin(self, *, environment: str) -> User:
        """Reuse admin, or bootstrap it only outside production for this CLI."""
        admin = await self.db.scalar(select(User).where(User.email == "admin"))
        if admin is not None:
            return admin
        if environment == "production":
            raise RuntimeError(
                "生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。"
                "请先手动创建管理员账号再运行本导入脚本。"
            )

        admin = User(
            id=uuid.uuid4(),
            email="admin",
            name="超级管理员",
            password_hash=hash_password("123456"),
            role=UserRole.SUPER_ADMIN.value,
            is_active=True,
        )
        self.db.add(admin)
        await self.db.flush()
        return admin

    async def create_bootstrap_super_admin(
        self,
        *,
        email: str,
        name: str,
        password: str,
        now: datetime,
    ) -> User | None:
        """Create the first privileged account, leaving an existing email unchanged."""
        existing = await self.db.scalar(select(User).where(User.email == email))
        if existing is not None:
            return None

        admin = User(
            email=email,
            name=name,
            password_hash=hash_password(password),
            role=UserRole.SUPER_ADMIN.value,
            group_name=None,
            status="online",
            is_active=True,
            email_verified_at=now,
        )
        self.db.add(admin)
        await self.db.flush()
        return admin

    @staticmethod
    def record_login_for_locked_user(user: User, now: datetime) -> None:
        user.last_login_at = now
        user.last_seen_at = now
        user.status = "online"

    @staticmethod
    def record_heartbeat_for_locked_user(user: User, now: datetime) -> None:
        user.last_seen_at = now
        if user.status != "online":
            user.status = "online"

    @staticmethod
    def record_seen_for_locked_user(user: User, now: datetime) -> None:
        user.last_seen_at = now

    async def mark_inactive_accounts_offline(self, cutoff: datetime):
        """Bulk-update online accounts with absent or stale heartbeats."""
        return await self.db.execute(
            update(User)
            .where(
                User.status == "online",
                or_(User.last_seen_at.is_(None), User.last_seen_at < cutoff),
            )
            .values(status="offline")
        )

    @staticmethod
    def record_logout_for_locked_user(user: User) -> None:
        user.status = "offline"

    @staticmethod
    def update_name_for_locked_user(user: User, name: str) -> str:
        old_name = user.name
        user.name = name
        return old_name

    @staticmethod
    def update_avatar_for_locked_user(user: User, avatar_ref: str | None) -> str | None:
        previous_ref = user.avatar_ref
        user.avatar_ref = avatar_ref
        return previous_ref

    @staticmethod
    def set_password_for_locked_user(
        user: User,
        password_hash: str,
        *,
        admin_reset_at: datetime | None = None,
    ) -> None:
        """Set a credential while its account row is locked; None clears reset state."""
        user.password_hash = password_hash
        user.password_admin_reset_at = admin_reset_at

    @staticmethod
    def update_preferences_for_locked_user(user: User, preferences: dict) -> None:
        user.preferences = preferences

    @staticmethod
    def set_group_for_locked_user(
        user: User,
        *,
        group_id: uuid.UUID | None,
        group_name: str | None,
    ) -> None:
        """Set both account group projections under the caller-held account lock."""
        user.group_id = group_id
        user.group_name = group_name
