"""v0.8.1 · 账号自助注销服务

冷静期：用户申请 → 写 deactivation_* 三字段 + audit + 通知所有 super_admin →
7 天后 Celery beat 任务命中 scheduled_at <= now 时自动执行软删（is_active=False）+
GDPR 脱敏 audit_logs + 清三字段 + 再次通知 super_admin。
冷静期内用户可 DELETE /me/deactivation-request 撤销。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.user import User
from app.services.audit import AuditAction, AuditService
from app.services.notification import NotificationService
from app.services.user_lifecycle import UserLifecycleService, set_disabled_metadata


COOLDOWN_DAYS = 7


async def _list_super_admin_ids(db: AsyncSession) -> list[uuid.UUID]:
    rows = (
        (
            await db.execute(
                select(User.id).where(
                    User.role == UserRole.SUPER_ADMIN.value,
                    User.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


class DeactivationService:
    @staticmethod
    async def request(
        db: AsyncSession,
        *,
        user: User,
        reason: str | None,
        request: Request | None = None,
    ) -> list[Notification]:
        """Record the request; returns rows to publish after the caller commits."""
        user = (await UserLifecycleService.lock_accounts(db, [user.id])).get(user.id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not user.is_active:
            raise HTTPException(status_code=400, detail="账号当前已停用")
        if user.deactivation_requested_at is not None:
            raise HTTPException(status_code=400, detail="已有进行中的注销申请")

        # 最后一名 super_admin 不能自助注销（必须先转交身份）
        if user.role == UserRole.SUPER_ADMIN.value:
            cnt = (
                await db.execute(
                    select(User.id).where(
                        User.role == UserRole.SUPER_ADMIN.value,
                        User.is_active.is_(True),
                    )
                )
            ).all()
            if len(cnt) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="您是最后一名超级管理员，请先指定继任者再申请注销",
                )

        now = datetime.now(timezone.utc)
        user.deactivation_requested_at = now
        user.deactivation_reason = (reason or "").strip()[:500] or None
        user.deactivation_scheduled_at = now + timedelta(days=COOLDOWN_DAYS)

        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.USER_DEACTIVATION_REQUEST,
            target_type="user",
            target_id=str(user.id),
            request=request,
            status_code=200,
            detail={
                "scheduled_at": user.deactivation_scheduled_at.isoformat(),
                "reason_present": bool(user.deactivation_reason),
            },
        )

        # 通知所有在线 super_admin —— 给他们 7 天窗口处理（如转交任务、降级前置）
        # Rows are returned unpublished; the caller publishes after commit so the
        # WS delivery gate can see the committed notification rows.
        super_ids = await _list_super_admin_ids(db)
        if not super_ids:
            return []
        return await NotificationService(db).notify_many(
            user_ids=super_ids,
            type="user.deactivation_requested",
            target_type="user",
            target_id=user.id,
            payload={
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "scheduled_at": user.deactivation_scheduled_at.isoformat(),
            },
        )

    @staticmethod
    async def cancel(
        db: AsyncSession,
        *,
        user: User,
        request: Request | None = None,
    ) -> User:
        user = (await UserLifecycleService.lock_accounts(db, [user.id])).get(user.id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="账号当前已停用")
        if user.deactivation_requested_at is None:
            raise HTTPException(status_code=400, detail="当前无待生效的注销申请")
        user.deactivation_requested_at = None
        user.deactivation_reason = None
        user.deactivation_scheduled_at = None
        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.USER_DEACTIVATION_CANCEL,
            target_type="user",
            target_id=str(user.id),
            request=request,
            status_code=200,
        )
        return user

    @staticmethod
    async def execute_due(db: AsyncSession) -> tuple[int, list[Notification]]:
        """Celery beat 调用：扫描所有 scheduled_at <= now 的活跃用户，执行软删 + GDPR。
        返回 (处理条数, 待发布通知)。在事务中执行，调用方负责 commit 后发布通知。"""
        now = datetime.now(timezone.utc)
        rows = (
            (
                await db.execute(
                    select(User)
                    .where(
                        User.is_active.is_(True),
                        User.deactivation_scheduled_at.isnot(None),
                        User.deactivation_scheduled_at <= now,
                    )
                    .order_by(User.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0, []

        super_ids = await _list_super_admin_ids(db)
        notif = NotificationService(db)
        pending_notifications: list[Notification] = []

        for user in rows:
            previous_scheduled = user.deactivation_scheduled_at
            previous_reason = user.deactivation_reason
            set_disabled_metadata(
                user,
                kind="deleted",
                actor_id=user.id,
                reason=previous_reason or "自助注销申请到期",
                at=now,
            )
            user.deactivation_requested_at = None
            user.deactivation_reason = None
            user.deactivation_scheduled_at = None

            await AuditService.log(
                db,
                actor=user,
                action=AuditAction.USER_DEACTIVATION_APPROVE,
                target_type="user",
                target_id=str(user.id),
                request=None,
                status_code=200,
                detail={
                    "email": user.email,
                    "role": user.role,
                    "scheduled_at": previous_scheduled.isoformat()
                    if previous_scheduled
                    else None,
                    "auto": True,
                },
            )

            # GDPR 脱敏：被注销用户在 audit_logs 历史行中的 actor_email / actor_role 置 NULL
            await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await db.execute(
                update(AuditLog)
                .where(AuditLog.actor_id == user.id)
                .values(actor_email=None, actor_role=None)
            )

            # 通知 super_admin 已自动生效（与 request 时的通知遥相呼应）
            for sid in super_ids:
                if sid == user.id:
                    continue
                row = await notif.notify(
                    user_id=sid,
                    type="user.deactivation_completed",
                    target_type="user",
                    target_id=user.id,
                    payload={"email": user.email, "name": user.name, "auto": True},
                )
                if row is not None:
                    pending_notifications.append(row)

        return len(rows), pending_notifications
