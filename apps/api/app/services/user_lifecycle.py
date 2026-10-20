"""Identity-owned account lifecycle state and credential generation writes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.schemas.user import UserOut
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_session_data import IdentitySessionData

REACTIVATABLE_KINDS = {"suspended", "emergency_suspended"}
HISTORICAL_KINDS = {"deleted", "historical_unknown"}
MANAGED_PROJECT_ROLES = ("employee",)


def lifecycle_user_out(user: User) -> UserOut:
    """Convert an ORM user without changing the response contract."""

    return UserOut.model_validate(user)


def set_disabled_metadata(
    user: User,
    *,
    kind: str,
    actor_id: uuid.UUID | None,
    reason: str | None,
    at: datetime | None = None,
) -> None:
    """Set all lifecycle fields together for every deactivation writer."""

    user.is_active = False
    user.status = "offline"
    user.disabled_kind = kind
    user.disabled_at = at or datetime.now(timezone.utc)
    user.disabled_by = actor_id
    user.disabled_reason = (reason or "").strip()[:500] or None


def reactivate_account(user: User) -> None:
    """Clear lifecycle metadata when the account owner restores access."""

    user.is_active = True
    user.status = "offline"
    user.disabled_kind = None
    user.disabled_at = None
    user.disabled_by = None
    user.disabled_reason = None


def set_platform_role(user: User, role: str) -> None:
    """Apply a validated role change while the account lock is held."""
    user.role = role


async def retire_account_credentials(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """Retire credentials and bump generation while the account lock is held."""
    try:
        result = await IdentitySessionData(db).retire_account_credentials(user_id)
    except IdentityGatewayUnavailable:
        raise HTTPException(status_code=503, detail="身份认证服务暂不可用") from None
    revoked_ids = result["revoked_key_ids"]
    revoked_id_set = set(revoked_ids)
    for instance in db.sync_session.identity_map.values():
        if isinstance(instance, ApiKey) and instance.id in revoked_id_set:
            set_committed_value(instance, "revoked_at", result["revoked_at"])
    return revoked_ids


class UserLifecycleService:
    """Own account-row locking and identity-derived lifecycle project scope."""

    @staticmethod
    async def lock_super_admin_invariant(db: AsyncSession) -> None:
        """Take the Identity reservation fence before any lifecycle User lock."""
        try:
            acquired = await IdentitySessionData(db).lock_super_admin_invariant()
        except IdentityGatewayUnavailable:
            raise HTTPException(
                status_code=503, detail="身份生命周期服务暂不可用"
            ) from None
        if not acquired:
            raise HTTPException(status_code=409, detail="账号正在更新，请刷新后重试")

    @staticmethod
    async def super_admin_status(
        db: AsyncSession, excluded_user_id: uuid.UUID | None
    ) -> dict:
        try:
            return await IdentitySessionData(db).super_admin_status(excluded_user_id)
        except IdentityGatewayUnavailable:
            raise HTTPException(
                status_code=503, detail="身份生命周期服务暂不可用"
            ) from None

    @staticmethod
    async def lock_accounts(
        db: AsyncSession,
        user_ids: Iterable[uuid.UUID],
        *,
        nowait: bool = True,
        authority_only: bool = False,
    ) -> dict[uuid.UUID, User]:
        """Lock current accounts; authority-only KEY SHARE admits non-auth projections.

        Account role, active-state and password writers retain the default FOR UPDATE.
        """
        try:
            accounts = await db.scalars(
                select(User)
                .where(User.id.in_(sorted(set(user_ids))))
                .order_by(User.id)
                .with_for_update(
                    read=authority_only, key_share=authority_only, nowait=nowait
                )
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            await db.rollback()
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise HTTPException(
                    status_code=409, detail="账号正在更新，请刷新后重试"
                ) from exc
            raise
        return {user.id: user for user in accounts}

    @staticmethod
    def _target_task_filter(target_id: uuid.UUID):
        return or_(
            Task.assignee_id == target_id,
            Task.reviewer_id == target_id,
            Task.id.in_(select(TaskLock.task_id).where(TaskLock.user_id == target_id)),
        )

    @staticmethod
    async def _project_ids_for_target(
        db: AsyncSession, target_id: uuid.UUID
    ) -> set[uuid.UUID]:
        member_ids = select(ProjectMember.project_id).where(
            ProjectMember.user_id == target_id
        )
        batch_ids = select(TaskBatch.project_id).where(
            or_(
                TaskBatch.annotator_id == target_id,
                TaskBatch.reviewer_id == target_id,
            )
        )
        task_ids = select(Task.project_id).where(
            UserLifecycleService._target_task_filter(target_id)
        )
        rows = await db.execute(
            select(Project.id).where(
                or_(
                    Project.owner_id == target_id,
                    Project.id.in_(member_ids),
                    Project.id.in_(batch_ids),
                    Project.id.in_(task_ids),
                )
            )
        )
        return {row[0] for row in rows.all()}

    @classmethod
    async def derived_project_ids(
        cls, db: AsyncSession, target_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Return owned, member, assigned, and task/lock-derived project scope."""

        return await cls._project_ids_for_target(db, target_id)
