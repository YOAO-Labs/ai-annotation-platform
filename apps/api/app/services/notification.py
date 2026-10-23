"""v0.6.9 · NotificationService: 写表 + Redis Pub/Sub 推送。

频道命名 `notify:{user_id}`，消息体 = NotificationOut JSON。
WS 端订阅同名频道把消息直送到登录会话；同时表里有持久化记录，
WS 断线 / 多端登录 / 离线场景都能从 GET /notifications 拉到。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import (
    String,
    and_,
    case,
    delete,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.db.enums import (
    MANAGER_PLATFORM_ROLES,
    PlatformRole,
    ProjectRole,
)
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorStatus
from app.services.identity_read_relations import account_profiles
from app.services.async_job_access import GLOBAL_ASYNC_JOB_KINDS
from aap_workspace_contract.access import platform_role_is_manager
from app.services.project_aggregates import (
    membership_project_ids,
    owned_project_ids,
    valid_membership_conditions,
)
from app.services.workspace_read_relations import (
    project_member_facts,
    project_facts,
    project_search_scope,
)
from aap_notifications.preferences import NotificationPreferenceManager
from aap_notifications.preferences_postgres import NotificationPreferencesPostgresData


log = logging.getLogger(__name__)


def channel_for(user_id: uuid.UUID | str) -> str:
    return f"notify:{user_id}"


def _notification_project_id(row: Notification) -> uuid.UUID | None:
    """Extract the project scope carried by a notification payload, if any."""

    payload = row.payload
    if not isinstance(payload, dict):
        return None
    raw = payload.get("project_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


_ACTUAL_TARGET_TYPES = ("export", "async_job")


def _job_project_text_expr():
    return (
        select(
            func.cast(
                func.coalesce(AsyncJob.project_id, AsyncJob.retained_project_id),
                String,
            )
        )
        .where(AsyncJob.id == Notification.target_id)
        .correlate(Notification)
        .scalar_subquery()
    )


def _job_kind_expr():
    return (
        select(AsyncJob.kind)
        .where(AsyncJob.id == Notification.target_id)
        .correlate(Notification)
        .scalar_subquery()
    )


def _job_exists_expr():
    return (
        select(AsyncJob.id)
        .where(AsyncJob.id == Notification.target_id)
        .correlate(Notification)
        .exists()
    )


def _job_project_exists_expr(user: IdentityActorStatus):
    workspace_projects = project_search_scope(user)
    return (
        select(AsyncJob.id)
        .join(
            workspace_projects,
            workspace_projects.c.project_id
            == func.coalesce(AsyncJob.project_id, AsyncJob.retained_project_id),
        )
        .where(AsyncJob.id == Notification.target_id)
        .correlate(Notification)
        .exists()
    )


def _effective_project_text_expr():
    """SQL text expression for a notification's actual project scope.

    For an export/async-job target the owning ``AsyncJob`` row is the actual
    resource and wins over a stale or forged ``payload.project_id``.  Other
    notifications use the payload project.  A missing/malformed value yields
    NULL, which matches no accessible project and therefore fails closed.
    """

    return case(
        (
            Notification.target_type.in_(_ACTUAL_TARGET_TYPES),
            _job_project_text_expr(),
        ),
        else_=Notification.payload["project_id"].astext,
    )


def _is_restricted_expr():
    """A notification is project-restricted when it names or targets a project.

    Export targets are always restricted. An async-job target is restricted
    when its real job is project-scoped, and fails closed (restricted with a
    NULL project) when the job is missing or requires a project but has no
    authoritative scope. Only known global job kinds remain global.
    """

    payload_project = Notification.payload["project_id"].astext
    return or_(
        Notification.target_type == "export",
        and_(
            Notification.target_type == "async_job",
            or_(
                _job_project_text_expr().is_not(None),
                ~_job_exists_expr(),
                _job_kind_expr().not_in(GLOBAL_ASYNC_JOB_KINDS),
            ),
        ),
        and_(
            Notification.target_type.not_in(_ACTUAL_TARGET_TYPES),
            payload_project.is_not(None),
        ),
    )


def _requires_export_capability_expr():
    """True for full-export notifications, including export-kind async jobs."""

    return or_(
        Notification.target_type == "export",
        and_(
            Notification.target_type == "async_job",
            _job_kind_expr() == "export",
        ),
    )


def _member_project_text_select(user: IdentityActorStatus, *project_roles: str):
    memberships = membership_project_ids(user, *project_roles).subquery()
    return select(func.cast(memberships.c.project_id, String))


def _owned_project_text_select(user: IdentityActorStatus):
    owned_projects = owned_project_ids(user).subquery()
    return select(func.cast(owned_projects.c.project_id, String))


def _accessible_project_clause(user: IdentityActorStatus, project_text):
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return None
    arms = [project_text.in_(_member_project_text_select(user))]
    if platform_role_is_manager(user.role):
        arms.append(project_text.in_(_owned_project_text_select(user)))
    return or_(*arms)


def _export_capable_project_clause(user: IdentityActorStatus, project_text):
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return None
    arms = [
        project_text.in_(_member_project_text_select(user, ProjectRole.REVIEWER.value))
    ]
    if platform_role_is_manager(user.role):
        arms.append(project_text.in_(_owned_project_text_select(user)))
    return or_(*arms)


class NotificationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _is_in_app_muted(self, user_id: uuid.UUID, type: str) -> bool:
        """v0.7.0：查 notification_preferences；channels.in_app=False 表示用户已静音此 type。
        无记录默认 in_app=True（现网用户向后兼容）。"""
        return await NotificationPreferenceManager(
            NotificationPreferencesPostgresData(self.db)
        ).is_in_app_muted(user_id, type)

    async def notify(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict | None = None,
        defer_publish: bool = True,
    ) -> Notification | None:
        """Write one notification; publication defaults to deferred.

        Publishing before the enclosing transaction commits is unsafe: the WS
        delivery gate re-reads the row through a separate session and cannot see
        an uncommitted row, which permanently drops the one real-time delivery.
        By default the row is returned unpublished; call :meth:`publish_committed`
        after ``db.commit()``.  Passing ``defer_publish=False`` restores the
        legacy immediate publish and is only correct when the row is already
        committed or the caller never commits afterwards.
        """
        if await self._is_in_app_muted(user_id, type):
            return None

        row = Notification(
            id=uuid.uuid4(),
            user_id=user_id,
            type=type,
            target_type=target_type,
            target_id=target_id,
            payload=payload or {},
        )
        self.db.add(row)
        await self.db.flush()

        if not defer_publish:
            await self.publish_committed([row])

        return row

    async def notify_many(
        self,
        *,
        user_ids: list[uuid.UUID],
        type: str,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict | None = None,
        defer_publish: bool = True,
    ) -> list[Notification]:
        """Write a de-duplicated fan-out, publishing once for the whole batch.

        Rows are always written with ``defer_publish=True`` internally so the
        delivery re-authorization runs once per fan-out instead of once per row.
        Publication defaults to deferred: collect the returned rows and call
        :meth:`publish_committed` after ``db.commit()`` so the WS gate can see
        the committed rows (see :meth:`notify`).
        """

        out: list[Notification] = []
        seen: set[uuid.UUID] = set()
        for uid in user_ids:
            if uid in seen:
                continue
            seen.add(uid)
            row = await self.notify(
                user_id=uid,
                type=type,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                defer_publish=True,
            )
            if row is not None:
                out.append(row)
        if not defer_publish and out:
            await self.publish_committed(out)
        return out

    async def _resolve_delivery_scopes(
        self, rows: list[Notification]
    ) -> list[tuple[bool, uuid.UUID | None, bool]]:
        """Resolve ``(restricted, project_id, export_required)`` per notification.

        Export/async-job targets resolve the owning ``AsyncJob`` in one batched
        query; other targets use ``payload.project_id``. A missing or malformed
        restricted target fails closed, while an unscoped job remains global.
        """

        job_ids = {
            row.target_id for row in rows if row.target_type in {"export", "async_job"}
        }
        jobs: dict[uuid.UUID, tuple[uuid.UUID | None, str | None]] = {}
        if job_ids:
            job_rows = await self.db.execute(
                select(
                    AsyncJob.id,
                    func.coalesce(AsyncJob.project_id, AsyncJob.retained_project_id),
                    AsyncJob.kind,
                ).where(AsyncJob.id.in_(job_ids))
            )
            jobs = {jid: (pid, kind) for jid, pid, kind in job_rows.all()}

        scopes: list[tuple[bool, uuid.UUID | None, bool]] = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            target_type = row.target_type
            # The actual resource wins for export/async-job targets; a stale or
            # forged payload project must not widen access.
            if target_type in _ACTUAL_TARGET_TYPES:
                job = jobs.get(row.target_id)
                if job is None:
                    job_project, job_kind = None, None
                else:
                    job_project, job_kind = job
                if target_type == "export":
                    restricted = True
                    export_required = True
                else:  # async_job
                    restricted = (
                        job is None
                        or job_project is not None
                        or job_kind not in GLOBAL_ASYNC_JOB_KINDS
                    )
                    export_required = job is not None and job_kind == "export"
                project_id = job_project
            else:
                restricted = "project_id" in payload
                export_required = False
                project_id = _notification_project_id(row)
            scopes.append((restricted, project_id, export_required))
        return scopes

    async def _allowed_delivery_indices(
        self,
        rows: list[Notification],
        scopes: list[tuple[bool, uuid.UUID | None, bool]],
    ) -> set[int]:
        pairs = {
            (pid, row.user_id)
            for row, (restricted, pid, _export) in zip(rows, scopes)
            if restricted and pid is not None
        }
        allowed: set[int] = set()
        if not pairs:
            return {
                index
                for index, (restricted, _pid, _export) in enumerate(scopes)
                if not restricted
            }

        member_facts = project_member_facts()
        membership = aliased(ProjectMember, member_facts, adapt_on_names=True)
        member_rows = await self.db.execute(
            select(membership.project_id, membership.user_id)
            .select_from(membership)
            .join(account_profiles, account_profiles.c.id == membership.user_id)
            .where(
                tuple_(membership.project_id, membership.user_id).in_(pairs),
                *valid_membership_conditions(membership.role),
            )
        )
        member_pairs = {(pid, uid) for pid, uid in member_rows.all()}

        reviewer_facts = project_member_facts()
        reviewer_membership = aliased(
            ProjectMember, reviewer_facts, adapt_on_names=True
        )
        reviewer_rows = await self.db.execute(
            select(reviewer_membership.project_id, reviewer_membership.user_id)
            .select_from(reviewer_membership)
            .join(
                account_profiles,
                account_profiles.c.id == reviewer_membership.user_id,
            )
            .where(
                tuple_(reviewer_membership.project_id, reviewer_membership.user_id).in_(
                    pairs
                ),
                *valid_membership_conditions(reviewer_membership.role),
                reviewer_membership.role == ProjectRole.REVIEWER.value,
            )
        )
        reviewer_pairs = {(pid, uid) for pid, uid in reviewer_rows.all()}

        owner_project = aliased(Project, project_facts(), adapt_on_names=True)
        owner_rows = await self.db.execute(
            select(owner_project.id, owner_project.owner_id)
            .join(account_profiles, account_profiles.c.id == owner_project.owner_id)
            .where(
                tuple_(owner_project.id, owner_project.owner_id).in_(pairs),
                account_profiles.c.is_active.is_(True),
                account_profiles.c.role.in_(list(MANAGER_PLATFORM_ROLES)),
            )
        )
        owner_pairs = {(pid, uid) for pid, uid in owner_rows.all()}

        super_project = aliased(Project, project_facts(), adapt_on_names=True)
        super_rows = await self.db.execute(
            select(super_project.id, account_profiles.c.id)
            .select_from(super_project)
            .join(
                account_profiles,
                account_profiles.c.id.in_({uid for _pid, uid in pairs}),
            )
            .where(
                tuple_(super_project.id, account_profiles.c.id).in_(pairs),
                account_profiles.c.is_active.is_(True),
                account_profiles.c.role == PlatformRole.SUPER_ADMIN.value,
            )
        )
        super_pairs = set(super_rows.all())

        base_pairs = member_pairs | owner_pairs | super_pairs
        export_pairs = reviewer_pairs | owner_pairs | super_pairs
        for index, (restricted, pid, export_required) in enumerate(scopes):
            if not restricted:
                allowed.add(index)
                continue
            if pid is None:
                continue
            pair = (pid, rows[index].user_id)
            if export_required:
                if pair in export_pairs:
                    allowed.add(index)
            elif pair in base_pairs:
                allowed.add(index)
        return allowed

    async def notification_deliverable(
        self, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Whether an already-stored row may still be pushed to ``user_id``.

        Used by the notification socket to drop a restricted message whose
        project access was revoked after the row was published to Redis, without
        disconnecting unrelated deliveries.
        """

        row = await self.db.get(Notification, notification_id)
        if row is None or row.user_id != user_id:
            return False
        try:
            scopes = await self._resolve_delivery_scopes([row])
            allowed = await self._allowed_delivery_indices([row], scopes)
        except Exception:
            log.exception(
                "notification delivery recheck failed notification=%s", notification_id
            )
            return False
        return 0 in allowed

    async def publish_committed(self, notifications: Iterable[Notification]) -> None:
        """Best-effort publish for rows whose enclosing transaction committed.

        This method deliberately does not commit, enqueue, or retain state.  The
        caller owns the request-local collection and invokes it only after a
        successful business ``commit``.  A Redis failure is logged per row and can
        never turn an already committed write into a failed request.

        Project-scoped rows are re-authorized against current project access, so
        losing a membership stops future restricted deliveries for that project
        while unrelated notifications still publish.  Export deliveries require
        the export capability, not mere membership.
        """
        rows = list(notifications)
        try:
            scopes = await self._resolve_delivery_scopes(rows)
            allowed = await self._allowed_delivery_indices(rows, scopes)
        except Exception:
            # Fail closed for project-scoped rows; never turn an already
            # committed business write into a failed request.
            log.exception("notification delivery authorization failed")
            allowed = set()
            for index, row in enumerate(rows):
                payload = row.payload if isinstance(row.payload, dict) else {}
                # Without the DB we cannot resolve an async-job/export target;
                # fail closed there and for explicit payload projects.
                restricted = (
                    row.target_type in _ACTUAL_TARGET_TYPES or "project_id" in payload
                )
                if not restricted:
                    allowed.add(index)

        for index, row in enumerate(rows):
            if index not in allowed:
                log.info(
                    "notification suppressed for revoked project access user=%s type=%s",
                    row.user_id,
                    row.type,
                )
                continue
            try:
                await _publish(
                    user_id=row.user_id,
                    message={
                        "id": str(row.id),
                        "type": row.type,
                        "target_type": row.target_type,
                        "target_id": str(row.target_id),
                        "payload": row.payload,
                        "created_at": (
                            row.created_at or datetime.now(timezone.utc)
                        ).isoformat(),
                    },
                )
            except Exception as e:
                log.warning(
                    "notification publish failed user=%s type=%s err=%s",
                    row.user_id,
                    row.type,
                    e,
                )

    async def publish_by_id(self, notification_id: uuid.UUID) -> bool:
        """Publish one committed row, propagating transport failures to its relay.

        Missing rows and rows no longer authorized are safe no-ops. Unlike
        :meth:`publish_committed`, a Redis error is raised so a durable delivery
        lease is not acknowledged before publication is accepted.
        """
        row = await self.db.get(Notification, notification_id)
        if row is None:
            return False
        scopes = await self._resolve_delivery_scopes([row])
        if 0 not in await self._allowed_delivery_indices([row], scopes):
            log.info(
                "notification delivery suppressed for revoked project access "
                "notification=%s user=%s type=%s",
                row.id,
                row.user_id,
                row.type,
            )
            return False
        await _publish(
            user_id=row.user_id,
            message={
                "id": str(row.id),
                "type": row.type,
                "target_type": row.target_type,
                "target_id": str(row.target_id),
                "payload": row.payload,
                "created_at": (
                    row.created_at or datetime.now(timezone.utc)
                ).isoformat(),
            },
        )
        return True

    async def publish_sync(self, user_id: uuid.UUID, *, reason: str) -> None:
        """Best-effort ``notifications.sync`` publication for a committed change.

        Handlers call this only after their business transaction committed and
        only when the operation changed rows, so another session that receives
        the event always observes committed state. Publication failures are
        logged and never turn the already committed request into a failure.
        """
        try:
            await _publish(
                user_id=user_id,
                message={"type": "notifications.sync", "reason": reason},
            )
        except Exception as e:
            log.warning(
                "notification sync publish failed user=%s reason=%s err=%s",
                user_id,
                reason,
                e,
            )

    def _scope_conditions(self, user: IdentityActorStatus) -> list:
        """SQL conditions hiding project-restricted rows the account cannot read.

        Mirrors the shared resolver: super administrators see everything; every
        other account sees explicit global rows plus rows whose actual project is
        one they manage or hold a valid membership in.  Export rows additionally
        require the reviewer/manager export capability.

        The membership SQL contract comes from
        :func:`app.services.project_aggregates.valid_membership_conditions` and
        the management predicate from
        :func:`aap_workspace_contract.access.platform_role_is_manager`; this
        helper only adapts them to the correlated text expression of the
        notification row.
        """

        if user.role == PlatformRole.SUPER_ADMIN.value:
            actual_target = Notification.target_type.in_(_ACTUAL_TARGET_TYPES)
            valid_global_scope = and_(
                Notification.target_type == "async_job",
                _job_project_text_expr().is_(None),
                _job_kind_expr().in_(GLOBAL_ASYNC_JOB_KINDS),
            )
            valid_project_scope = and_(
                _job_project_text_expr().is_not(None),
                _job_project_exists_expr(user),
            )
            return [
                or_(
                    ~actual_target,
                    and_(
                        _job_exists_expr(),
                        or_(valid_global_scope, valid_project_scope),
                    ),
                )
            ]
        project_text = _effective_project_text_expr()
        restricted = _is_restricted_expr()
        return [
            or_(~restricted, _accessible_project_clause(user, project_text)),
            or_(
                ~_requires_export_capability_expr(),
                _export_capable_project_clause(user, project_text),
            ),
        ]

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        unread_only: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[Notification], int, int]:
        user = await IdentityAccountLockData(self.db).read_actor_status(user_id)
        if user is None:
            return [], 0, 0
        scope = self._scope_conditions(user)
        base = select(Notification).where(Notification.user_id == user_id, *scope)
        count_q = select(func.count(Notification.id)).where(
            Notification.user_id == user_id, *scope
        )
        unread_q = select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            *scope,
        )

        q = base
        if unread_only:
            q = q.where(Notification.read_at.is_(None))
        q = q.order_by(Notification.created_at.desc()).offset(offset).limit(limit)

        items = list((await self.db.execute(q)).scalars().all())
        total = (await self.db.execute(count_q)).scalar() or 0
        unread = (await self.db.execute(unread_q)).scalar() or 0
        return items, int(total), int(unread)

    async def unread_count(self, user_id: uuid.UUID) -> int:
        user = await IdentityAccountLockData(self.db).read_actor_status(user_id)
        if user is None:
            return 0
        scope = self._scope_conditions(user)
        q = select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            *scope,
        )
        return int((await self.db.execute(q)).scalar() or 0)

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        """Mark one row read. The handler publishes ``reason=read`` after commit."""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(timezone.utc))
        )
        return (result.rowcount or 0) > 0

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """Mark all rows read. The handler publishes ``reason=read`` after commit."""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(timezone.utc))
        )
        return int(result.rowcount or 0)

    async def delete_for_user(
        self, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> bool:
        """Delete one row. The handler publishes ``reason=deleted`` after commit."""
        result = await self.db.execute(
            delete(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
        return (result.rowcount or 0) > 0

    async def clear_read(self, user_id: uuid.UUID) -> int:
        """Delete all read rows. The handler publishes ``reason=deleted`` after commit."""
        result = await self.db.execute(
            delete(Notification).where(
                Notification.user_id == user_id,
                Notification.read_at.is_not(None),
            )
        )
        return int(result.rowcount or 0)


async def _publish(*, user_id: uuid.UUID, message: dict) -> None:
    r = aioredis.from_url(settings.redis_url)
    try:
        await r.publish(channel_for(user_id), json.dumps(message))
    finally:
        await r.close()
