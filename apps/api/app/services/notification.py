"""v0.6.9 · NotificationService: 写表 + Redis Pub/Sub 推送。

频道命名 `notify:{user_id}`，消息体 = NotificationOut JSON。
WS 端订阅同名频道把消息直送到登录会话；同时表里有持久化记录，
WS 断线 / 多端登录 / 离线场景都能从 GET /notifications 拉到。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import (
    delete,
    inspect as sa_inspect,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.bulk_persistence import (
    BulkORMDelete,
    BulkORMUpdate,
    BulkUDCompileState,
)
from sqlalchemy.orm import make_transient_to_detached

from app.config import settings
from app.db.models.notification import Notification
from app.services.identity_account_lock_data import IdentityAccountLockData
from aap_notifications.preferences import NotificationPreferenceManager
from aap_notifications.preferences_postgres import NotificationPreferencesPostgresData
from aap_notifications.append import NotificationAppendManager
from aap_notifications.append_contract import AppendNotificationRequest
from aap_notifications.append_postgres import NotificationAppendPostgresData
from aap_notifications.read_state import NotificationReadStateManager
from aap_notifications.read_state_postgres import NotificationReadStatePostgresData
from aap_notifications.delete import NotificationDeleteManager
from aap_notifications.delete_postgres import NotificationDeletePostgresData
from aap_notifications.redis_pubsub import channel_for, publish_json as _publish_json
from aap_notifications.publication import NotificationPublicationManager
from aap_notifications.terminal_append import NotificationTerminalAppendManager
from aap_notifications.terminal_append_contract import (
    TERMINAL_APPEND_PROTOCOL,
    TerminalNotificationRequest,
)
from aap_notifications.terminal_append_postgres import (
    NotificationTerminalAppendPostgresData,
)
from aap_notifications.inbox_query_postgres import NotificationInboxQueryPostgresData
from aap_notifications.delivery_read import (
    NotificationDeliveryReadPolicy,
    notification_project_id,
)
from aap_notifications.delivery_read_postgres import (
    NotificationDeliveryReadPostgresData,
)
from app.services.notification_publication import publish_notification_request


log = logging.getLogger(__name__)


def _notification_project_id(row: Notification) -> uuid.UUID | None:
    return notification_project_id(row)


class NotificationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _is_in_app_muted(self, user_id: uuid.UUID, type: str) -> bool:
        """v0.7.0：查 notification_preferences；channels.in_app=False 表示用户已静音此 type。
        无记录默认 in_app=True（现网用户向后兼容）。"""
        return await NotificationPreferenceManager(
            NotificationPreferencesPostgresData(self.db)
        ).is_in_app_muted(user_id, type)

    async def notification_by_id(
        self, notification_id: uuid.UUID
    ) -> Notification | None:
        """Load one owner row while retaining the caller's ORM identity."""
        session = self.db.sync_session
        mapper = sa_inspect(Notification)
        identity_key = mapper.identity_key_from_primary_key((notification_id,))
        cached = session.identity_map.get(identity_key)
        state = sa_inspect(cached) if cached is not None else None
        if state is not None and not state.expired:
            return cached

        row = await NotificationDeliveryReadPostgresData(
            self.db, Notification
        ).read_notification(notification_id)
        if row is not None:
            return row
        if state is not None:
            if state.expired:
                session._remove_newly_deleted([state])
        return None

    async def terminal_notification_exists(
        self,
        user_id: uuid.UUID,
        type: str,
        target_id: uuid.UUID,
    ) -> bool:
        """Preserve the terminal helper's ORM SELECT/autoflush semantics."""
        if self.db.autoflush:
            await self.db.flush()
        connection = await self.db.connection()
        async with connection.begin_nested():
            with self.db.no_autoflush:
                result = await NotificationDeliveryReadPostgresData(
                    self.db, Notification
                ).find_terminal_notification(user_id, type, target_id)
        return result is not None

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

        notification_id = uuid.uuid4()
        notification_payload = payload or {}
        # Preference policy remains authoritative in the existing owner
        # preflight above. Append before the original full flush so pending
        # caller changes keep the same unit-of-work ordering under no_autoflush.
        appended = await NotificationAppendManager(
            NotificationAppendPostgresData(self.db)
        ).append(
            AppendNotificationRequest(
                schema="aap-notification-append/v1",
                operation="append",
                id=notification_id,
                user_id=user_id,
                type=type,
                target_type=target_type,
                target_id=target_id,
                payload=notification_payload,
            )
        )
        row = await self._attach_notification_snapshot(appended.notification)
        if not defer_publish:
            await self.publish_committed([row])
        return row

    async def _attach_notification_snapshot(self, snapshot) -> Notification:
        row = Notification(
            id=snapshot.id,
            user_id=snapshot.user_id,
            type=snapshot.type,
            target_type=snapshot.target_type,
            target_id=snapshot.target_id,
            payload=snapshot.payload,
            read_at=snapshot.read_at,
            created_at=snapshot.created_at,
        )
        make_transient_to_detached(row)
        row = await self.db.merge(row, load=False)
        await self.db.flush()
        return row

    async def notify_terminal(
        self,
        *,
        user_id: uuid.UUID,
        type: str,
        target_id: uuid.UUID,
        payload: dict,
    ) -> Notification | None:
        """Append and publish one terminal notification if this call won the race."""
        if self.db.autoflush:
            await self.db.flush()
        connection = await self.db.connection()
        async with connection.begin_nested():
            with self.db.no_autoflush:
                existing = await NotificationDeliveryReadPostgresData(
                    self.db, Notification
                ).find_terminal_notification(user_id, type, target_id)
            if existing is not None:
                return None

            if await self._is_in_app_muted(user_id, type):
                return None

            request = TerminalNotificationRequest(
                schema=TERMINAL_APPEND_PROTOCOL,
                operation="append_job_terminal",
                id=uuid.uuid4(),
                user_id=user_id,
                type=type,
                target_id=target_id,
                payload=payload,
            )
            appended = await NotificationTerminalAppendManager(
                NotificationTerminalAppendPostgresData(self.db)
            ).append(request)

        if not appended.created:
            return None

        row = await self._attach_notification_snapshot(appended.notification)
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
        return await NotificationDeliveryReadPolicy(
            NotificationDeliveryReadPostgresData(self.db, Notification)
        ).resolve_scopes(rows)

    async def _allowed_delivery_indices(
        self,
        rows: list[Notification],
        scopes: list[tuple[bool, uuid.UUID | None, bool]],
    ) -> set[int]:
        return await NotificationDeliveryReadPolicy(
            NotificationDeliveryReadPostgresData(self.db, Notification)
        ).allowed_indices(rows, scopes)

    async def _publication_authorizations(self, rows, scopes):
        return await NotificationDeliveryReadPolicy(
            NotificationDeliveryReadPostgresData(self.db, Notification)
        ).authorization_facts(rows, scopes)

    def _publication_manager(self) -> NotificationPublicationManager:
        data = SimpleNamespace(
            notification_by_id=self.notification_by_id,
            resolve_scopes=self._resolve_delivery_scopes,
            authorization_facts=self._publication_authorizations,
        )

        async def transport(request):
            return await publish_notification_request(request, embedded=_publish)

        return NotificationPublicationManager(data, transport, logger=log)

    async def notification_deliverable(
        self, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Whether an already-stored row may still be pushed to ``user_id``.

        Used by the notification socket to drop a restricted message whose
        project access was revoked after the row was published to Redis, without
        disconnecting unrelated deliveries.
        """

        row = await self.notification_by_id(notification_id)
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
        await self._publication_manager().publish_committed(notifications)

    async def publish_by_id(self, notification_id: uuid.UUID) -> bool:
        """Publish one committed row, propagating transport failures to its relay.

        Missing rows and rows no longer authorized are safe no-ops. Unlike
        :meth:`publish_committed`, a Redis error is raised so a durable delivery
        lease is not acknowledged before publication is accepted.
        """
        return await self._publication_manager().publish_by_id(notification_id)

    async def publish_sync(self, user_id: uuid.UUID, *, reason: str) -> None:
        """Best-effort ``notifications.sync`` publication for a committed change.

        Handlers call this only after their business transaction committed and
        only when the operation changed rows, so another session that receives
        the event always observes committed state. Publication failures are
        logged and never turn the already committed request into a failure.
        """
        await self._publication_manager().publish_sync(user_id, reason=reason)

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
        inbox = NotificationInboxQueryPostgresData(self.db, Notification)
        items = await inbox.page(
            user.id,
            user.role,
            unread_only=unread_only,
            limit=limit,
            offset=offset,
        )
        total = await inbox.total_count(user.id, user.role)
        unread = await inbox.unread_count(user.id, user.role)
        return items, total, unread

    async def unread_count(self, user_id: uuid.UUID) -> int:
        user = await IdentityAccountLockData(self.db).read_actor_status(user_id)
        if user is None:
            return 0
        inbox = NotificationInboxQueryPostgresData(self.db, Notification)
        return await inbox.unread_count(user.id, user.role)

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        """Mark one row read. The handler publishes ``reason=read`` after commit."""
        read_at = datetime.now(timezone.utc)
        result = await NotificationReadStateManager(
            NotificationReadStatePostgresData(self.db)
        ).mark_read(user_id, notification_id, read_at)
        self._synchronize_read_state_identity(
            user_id, read_at, notification_id=notification_id
        )
        return result.updated

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """Mark all rows read. The handler publishes ``reason=read`` after commit."""
        read_at = datetime.now(timezone.utc)
        result = await NotificationReadStateManager(
            NotificationReadStatePostgresData(self.db)
        ).mark_all_read(user_id, read_at)
        self._synchronize_read_state_identity(user_id, read_at)
        return result.updated

    def _synchronize_read_state_identity(
        self,
        user_id: uuid.UUID,
        read_at: datetime,
        *,
        notification_id: uuid.UUID | None = None,
    ) -> None:
        """Mirror SQLAlchemy's evaluate synchronization after the owner UPDATE.

        SQLAlchemy 2.0.49 evaluates bulk UPDATE predicates against identity-map
        state after autoflush and applies the SET to in-memory matches, even when
        the database matched no rows. Reusing that pinned path preserves the
        borrowed session's no-autoflush and dirty-identity behavior.
        """
        criteria = []
        if notification_id is not None:
            criteria.append(Notification.id == notification_id)
        criteria.extend(
            [Notification.user_id == user_id, Notification.read_at.is_(None)]
        )
        statement = update(Notification).where(*criteria).values(read_at=read_at)
        mapper = sa_inspect(Notification)
        # shortcut: Uses pinned SQLAlchemy cache hooks; rebaseline on upgrades.
        options = BulkUDCompileState.default_update_options + {
            "_subject_mapper": mapper,
            "_identity_token": None,
        }
        eval_condition = BulkUDCompileState._eval_condition_from_statement(
            options, statement
        )
        options = options + {"_eval_condition": eval_condition}

        # The SQLAlchemy post-evaluate hook only needs these fields from its
        # Result. Compile the same UPDATE locally; this performs no SQL.
        compiled = statement.compile()
        result = SimpleNamespace(
            context=SimpleNamespace(compiled_parameters=[compiled.construct_params()]),
            prefetch_cols=lambda: compiled.prefetch,
            postfetch_cols=lambda: compiled.postfetch,
        )
        session = getattr(self.db, "sync_session", self.db)
        BulkORMUpdate._do_post_synchronize_evaluate(session, statement, result, options)

    async def delete_for_user(
        self, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> bool:
        """Delete one row. The handler publishes ``reason=deleted`` after commit."""
        result = await NotificationDeleteManager(
            NotificationDeletePostgresData(self.db)
        ).delete_for_user(user_id, notification_id)
        self._synchronize_delete_identity(user_id, notification_id=notification_id)
        return result.deleted

    async def clear_read(self, user_id: uuid.UUID) -> int:
        """Delete all read rows. The handler publishes ``reason=deleted`` after commit."""
        result = await NotificationDeleteManager(
            NotificationDeletePostgresData(self.db)
        ).clear_read(user_id)
        self._synchronize_delete_identity(user_id, clear_read=True)
        return result.deleted

    def _synchronize_delete_identity(
        self,
        user_id: uuid.UUID,
        *,
        notification_id: uuid.UUID | None = None,
        clear_read: bool = False,
    ) -> None:
        """Mirror SQLAlchemy's post-evaluate DELETE identity synchronization."""
        if clear_read:
            criteria = [
                Notification.user_id == user_id,
                Notification.read_at.is_not(None),
            ]
        elif notification_id is not None:
            criteria = [
                Notification.id == notification_id,
                Notification.user_id == user_id,
            ]
        else:
            raise ValueError("delete cache synchronization needs an operation")

        # This ORM statement is only the pinned identity-map evaluator input;
        # the actual DELETE already ran through the Notifications owner.
        statement = delete(Notification).where(*criteria)
        mapper = sa_inspect(Notification)
        options = BulkUDCompileState.default_update_options + {
            "_subject_mapper": mapper,
            "_identity_token": None,
        }
        options = options + {
            "_eval_condition": BulkORMDelete._eval_condition_from_statement(
                options, statement
            )
        }
        session = getattr(self.db, "sync_session", self.db)
        BulkORMDelete._do_post_synchronize_evaluate(session, statement, None, options)


async def _publish(*, user_id: uuid.UUID, message: dict) -> None:
    await _publish_json(
        settings.redis_url,
        channel=channel_for(user_id),
        message=message,
    )
