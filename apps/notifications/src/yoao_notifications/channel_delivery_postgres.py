"""PostgreSQL lease and visibility clients for external channel deliveries."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

CHANNEL_DELIVERY_PROTOCOL = "yoao-notification-channel-delivery/v1"
CHANNEL_DELIVERY_POLICY = "yoao-notification-channel-delivery/v1"

_CHANNEL_DELIVERY_CAPABILITIES = frozenset(
    {
        "yoao_diagnostics_v1.bug_report_visible_to_actor(uuid, uuid)",
        "yoao_labeling_v1.notification_target_visible_to_actor(uuid, text, uuid, uuid, uuid)",
        "yoao_notifications_v1.channel_delivery_policy()",
        "yoao_notifications_v1.claim_channel_deliveries(jsonb)",
        "yoao_notifications_v1.delivery_job_scopes(uuid[])",
        "yoao_notifications_v1.delivery_member_pairs(jsonb)",
        "yoao_notifications_v1.delivery_owner_pairs(jsonb)",
        "yoao_notifications_v1.delivery_reviewer_pairs(jsonb)",
        "yoao_notifications_v1.delivery_super_pairs(jsonb)",
        "yoao_notifications_v1.finish_channel_delivery(jsonb)",
    }
)


class NotificationChannelDeliveryUnavailable(RuntimeError):
    """The channel delivery PostgreSQL boundary is unavailable or misconfigured."""


def _decode_json(raw: Any) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError, UnicodeError):
        raise NotificationChannelDeliveryUnavailable() from None


def _encode_json(payload: Any) -> str:
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _check_policy_reply(raw: Any) -> str:
    value = _decode_json(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema") != CHANNEL_DELIVERY_PROTOCOL
        or value.get("ok") is not True
        or not isinstance(value.get("data"), dict)
        or value["data"].get("policy") != CHANNEL_DELIVERY_POLICY
    ):
        raise NotificationChannelDeliveryUnavailable()
    return CHANNEL_DELIVERY_POLICY


def _decode_claim_reply(raw: Any) -> list[dict[str, Any]]:
    value = _decode_json(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema") != CHANNEL_DELIVERY_PROTOCOL
        or value.get("ok") is not True
        or not isinstance(value.get("data"), dict)
        or not isinstance(value["data"].get("items"), list)
    ):
        raise NotificationChannelDeliveryUnavailable()
    return list(value["data"]["items"])


def _decode_finish_reply(raw: Any) -> bool:
    value = _decode_json(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema") != CHANNEL_DELIVERY_PROTOCOL
        or value.get("ok") is not True
        or not isinstance(value.get("data"), dict)
        or type(value["data"].get("accepted")) is not bool
    ):
        raise NotificationChannelDeliveryUnavailable()
    return value["data"]["accepted"]


def _build_finish_payload(
    *,
    delivery_id: uuid.UUID,
    worker_id: uuid.UUID,
    lease_id: uuid.UUID,
    outcome: str,
    reason: str | None = None,
    provider_message_id: str | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CHANNEL_DELIVERY_PROTOCOL,
        "delivery_id": str(delivery_id),
        "worker_id": str(worker_id),
        "lease_id": str(lease_id),
        "outcome": outcome,
    }
    if reason is not None:
        payload["reason"] = reason
    if provider_message_id is not None:
        payload["provider_message_id"] = provider_message_id
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    return payload


class NotificationChannelDeliveryPostgresData:
    """Function-only asyncpg client bound to yoao_notification_channel_delivery_client."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: float = 5.0,
        command_timeout: float = 10.0,
    ) -> None:
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _open(self):
        import asyncpg

        return await asyncpg.connect(
            self.dsn,
            timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            server_settings={"application_name": "yoao-notification-channel-delivery"},
        )

    async def _close(self, connection) -> None:
        if connection is None:
            return
        try:
            await asyncio.shield(connection.close(timeout=2))
        except Exception:
            if not asyncio.current_task().cancelling():
                raise NotificationChannelDeliveryUnavailable() from None

    @staticmethod
    async def _assert_capabilities(connection) -> None:
        import asyncpg

        try:
            rows = await connection.fetch(
                """
                SELECT namespace.nspname,
                       function.proname,
                       pg_catalog.oidvectortypes(function.proargtypes) AS args
                FROM pg_catalog.pg_proc AS function
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = function.pronamespace
                WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND function.prokind IN ('f', 'p', 'a', 'w')
                  AND function.prorettype NOT IN (
                      'pg_catalog.trigger'::pg_catalog.regtype,
                      'pg_catalog.event_trigger'::pg_catalog.regtype
                  )
                  AND (
                      pg_catalog.left(namespace.nspname, 4) = 'aap_'
                      OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
                      OR function.prosecdef
                  )
                  AND pg_catalog.has_function_privilege(
                      current_user, function.oid, 'EXECUTE'
                  )
                """
            )
            actual = {
                f"{row['nspname']}.{row['proname']}({row['args']})" for row in rows
            }
            if actual != _CHANNEL_DELIVERY_CAPABILITIES:
                raise NotificationChannelDeliveryUnavailable()
            has_table_access = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND (
                          pg_catalog.has_table_privilege(
                              current_user, relation.oid,
                              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                          )
                          OR pg_catalog.has_any_column_privilege(
                              current_user, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                          )
                      )
                )
                """
            )
            if has_table_access is not False:
                raise NotificationChannelDeliveryUnavailable()
        except NotificationChannelDeliveryUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise NotificationChannelDeliveryUnavailable() from None

    async def channel_delivery_policy(self) -> str:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT yoao_notifications_v1.channel_delivery_policy()"
                )
                await self._assert_capabilities(connection)
                return _check_policy_reply(raw)
        except NotificationChannelDeliveryUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def claim_deliveries(
        self,
        worker_id: uuid.UUID,
        *,
        limit: int = 16,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        import asyncpg

        payload = _encode_json(
            {
                "schema": CHANNEL_DELIVERY_PROTOCOL,
                "worker_id": str(worker_id),
                "limit": limit,
                "lease_seconds": lease_seconds,
            }
        )
        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT yoao_notifications_v1.claim_channel_deliveries($1::jsonb)",
                    payload,
                )
                return _decode_claim_reply(raw)
        except NotificationChannelDeliveryUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def finish_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        outcome: str,
        reason: str | None = None,
        provider_message_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> bool:
        import asyncpg

        payload = _encode_json(
            _build_finish_payload(
                delivery_id=delivery_id,
                worker_id=worker_id,
                lease_id=lease_id,
                outcome=outcome,
                reason=reason,
                provider_message_id=provider_message_id,
                retry_after_seconds=retry_after_seconds,
            )
        )
        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT yoao_notifications_v1.finish_channel_delivery($1::jsonb)",
                    payload,
                )
                return _decode_finish_reply(raw)
        except NotificationChannelDeliveryUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def is_bug_report_visible(
        self, actor_id: uuid.UUID, report_id: uuid.UUID
    ) -> bool:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            raw = await connection.fetchval(
                "SELECT yoao_diagnostics_v1.bug_report_visible_to_actor($1::uuid, $2::uuid)",
                actor_id,
                report_id,
            )
            return bool(raw)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def is_labeling_target_visible(
        self,
        actor_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
    ) -> bool:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            raw = await connection.fetchval(
                "SELECT yoao_labeling_v1.notification_target_visible_to_actor("
                "$1::uuid, $2::text, $3::uuid, $4::uuid, $5::uuid)",
                actor_id,
                target_type,
                target_id,
                project_id,
                task_id,
            )
            return bool(raw)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def job_scopes(
        self, job_ids: set[uuid.UUID]
    ) -> Iterable[tuple[uuid.UUID, uuid.UUID | None, str]]:
        import asyncpg

        if not job_ids:
            return []
        connection = None
        try:
            connection = await self._open()
            rows = await connection.fetch(
                "SELECT id, scope_project_id, kind "
                "FROM yoao_notifications_v1.delivery_job_scopes($1::uuid[])",
                list(job_ids),
            )
            return [(row["id"], row["scope_project_id"], row["kind"]) for row in rows]
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def _pairs(
        self, function_name: str, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        import asyncpg

        if not pairs:
            return set()
        encoded = _encode_json(
            [
                {"project_id": str(project_id), "user_id": str(user_id)}
                for project_id, user_id in pairs
            ]
        )
        connection = None
        try:
            connection = await self._open()
            rows = await connection.fetch(
                f"SELECT project_id, user_id FROM yoao_notifications_v1.{function_name}($1::jsonb)",
                encoded,
            )
            return {(row["project_id"], row["user_id"]) for row in rows}
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            raise NotificationChannelDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def member_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_member_pairs", pairs)

    async def reviewer_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_reviewer_pairs", pairs)

    async def owner_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_owner_pairs", pairs)

    async def super_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_super_pairs", pairs)

    async def global_super_admins(self, user_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        return set()


class NotificationChannelDeliverySessionData:
    """Short committed SQLAlchemy sessions for embedded channel delivery relay."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def _scalar_commit(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> Any:
        async with self.session_factory() as db:
            try:
                async with asyncio.timeout(10):
                    value = await db.scalar(text(sql), params or {})
                    await db.commit()
                    return value
            except (SQLAlchemyError, TimeoutError):
                await db.rollback()
                raise NotificationChannelDeliveryUnavailable() from None
            except BaseException:
                await db.rollback()
                raise

    async def _rows_readonly(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        async with self.session_factory() as db:
            try:
                async with asyncio.timeout(10):
                    result = await db.execute(text(sql), params or {})
                    return list(result.all())
            except (SQLAlchemyError, TimeoutError):
                raise NotificationChannelDeliveryUnavailable() from None

    async def channel_delivery_policy(self) -> str:
        raw = await self._scalar_commit(
            "SELECT yoao_notifications_v1.channel_delivery_policy()"
        )
        return _check_policy_reply(raw)

    async def claim_deliveries(
        self,
        worker_id: uuid.UUID,
        *,
        limit: int = 16,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        payload = _encode_json(
            {
                "schema": CHANNEL_DELIVERY_PROTOCOL,
                "worker_id": str(worker_id),
                "limit": limit,
                "lease_seconds": lease_seconds,
            }
        )
        raw = await self._scalar_commit(
            "SELECT yoao_notifications_v1.claim_channel_deliveries(CAST(:p AS pg_catalog.jsonb))",
            {"p": payload},
        )
        return _decode_claim_reply(raw)

    async def finish_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        outcome: str,
        reason: str | None = None,
        provider_message_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> bool:
        payload = _encode_json(
            _build_finish_payload(
                delivery_id=delivery_id,
                worker_id=worker_id,
                lease_id=lease_id,
                outcome=outcome,
                reason=reason,
                provider_message_id=provider_message_id,
                retry_after_seconds=retry_after_seconds,
            )
        )
        raw = await self._scalar_commit(
            "SELECT yoao_notifications_v1.finish_channel_delivery(CAST(:p AS pg_catalog.jsonb))",
            {"p": payload},
        )
        return _decode_finish_reply(raw)

    async def is_bug_report_visible(
        self, actor_id: uuid.UUID, report_id: uuid.UUID
    ) -> bool:
        rows = await self._rows_readonly(
            "SELECT yoao_diagnostics_v1.bug_report_visible_to_actor("
            "CAST(:actor_id AS pg_catalog.uuid), CAST(:report_id AS pg_catalog.uuid))",
            {"actor_id": str(actor_id), "report_id": str(report_id)},
        )
        return bool(rows and rows[0][0])

    async def is_labeling_target_visible(
        self,
        actor_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
    ) -> bool:
        rows = await self._rows_readonly(
            "SELECT yoao_labeling_v1.notification_target_visible_to_actor("
            "CAST(:actor_id AS pg_catalog.uuid), "
            "CAST(:target_type AS pg_catalog.text), "
            "CAST(:target_id AS pg_catalog.uuid), "
            "CAST(:project_id AS pg_catalog.uuid), "
            "CAST(:task_id AS pg_catalog.uuid))",
            {
                "actor_id": str(actor_id),
                "target_type": target_type,
                "target_id": str(target_id),
                "project_id": str(project_id) if project_id is not None else None,
                "task_id": str(task_id) if task_id is not None else None,
            },
        )
        return bool(rows and rows[0][0])

    async def job_scopes(
        self, job_ids: set[uuid.UUID]
    ) -> Iterable[tuple[uuid.UUID, uuid.UUID | None, str]]:
        if not job_ids:
            return []
        rows = await self._rows_readonly(
            "SELECT id, scope_project_id, kind "
            "FROM yoao_notifications_v1.delivery_job_scopes(CAST(:job_ids AS pg_catalog.uuid[]))",
            {"job_ids": list(job_ids)},
        )
        return [(row[0], row[1], row[2]) for row in rows]

    async def _pairs(
        self, function_name: str, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        if not pairs:
            return set()
        encoded = _encode_json(
            [
                {"project_id": str(project_id), "user_id": str(user_id)}
                for project_id, user_id in pairs
            ]
        )
        rows = await self._rows_readonly(
            f"SELECT project_id, user_id FROM yoao_notifications_v1.{function_name}("
            "CAST(:pairs AS pg_catalog.jsonb))",
            {"pairs": encoded},
        )
        return {(row[0], row[1]) for row in rows}

    async def member_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_member_pairs", pairs)

    async def reviewer_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_reviewer_pairs", pairs)

    async def owner_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_owner_pairs", pairs)

    async def super_pairs(
        self, pairs: set[tuple[uuid.UUID, uuid.UUID]]
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        return await self._pairs("delivery_super_pairs", pairs)

    async def global_super_admins(self, user_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        return set()
