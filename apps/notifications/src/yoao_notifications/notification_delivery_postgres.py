"""Function-only PostgreSQL client for Notifications-owned delivery leases."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from .notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
    NOTIFICATION_DELIVERY_PROTOCOL,
    NotificationDeliveryUnavailable,
    NotificationClaimRequest,
    NotificationDeliveryPolicyReply,
    NotificationFinishRequest,
    NotificationLease,
    decode_notification_claim_reply,
    decode_notification_delivery_policy,
    decode_notification_finish_reply,
    native_notification_command_json,
)

_DELIVERY_CAPABILITIES = {
    "aap_notifications_v1.identity_delivery_policy()",
    "aap_notifications_v1.claim_identity_deliveries(jsonb)",
    "aap_notifications_v1.finish_identity_delivery(jsonb)",
}


class NotificationDeliveryPostgresData:
    """The publisher login can call only its policy, claim and finish functions."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: float = 5,
        command_timeout: float = 10,
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _open(self):
        import asyncpg

        return await asyncpg.connect(
            self.dsn,
            timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            server_settings={"application_name": "aap-notification-publisher"},
        )

    async def _close(self, connection) -> None:
        if connection is None:
            return
        try:
            await asyncio.shield(connection.close(timeout=2))
        except Exception:
            if not asyncio.current_task().cancelling():
                raise NotificationDeliveryUnavailable() from None

    @staticmethod
    def _decode_json(raw):
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError, UnicodeError):
            raise NotificationDeliveryUnavailable() from None

    @staticmethod
    async def _assert_capabilities(connection) -> None:
        import asyncpg

        try:
            rows = await connection.fetch(
                """
                SELECT namespace.nspname,
                       function.proname,
                       pg_catalog.oidvectortypes(function.proargtypes) AS args,
                       function.prosecdef
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
                      OR function.prosecdef
                  )
                  AND pg_catalog.has_function_privilege(
                      current_user, function.oid, 'EXECUTE'
                  )
                ORDER BY namespace.nspname, function.proname, args
                """
            )
            actual = {
                f"{row['nspname']}.{row['proname']}({row['args']})" for row in rows
            }
            if actual != _DELIVERY_CAPABILITIES:
                raise NotificationDeliveryUnavailable()
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
                              current_user, relation.oid, 'SELECT'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'INSERT'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'UPDATE'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'DELETE'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'TRUNCATE'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'REFERENCES'
                          )
                          OR pg_catalog.has_table_privilege(
                              current_user, relation.oid, 'TRIGGER'
                          )
                          OR pg_catalog.has_any_column_privilege(
                              current_user, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                          )
                      )
                )
                """
            )
            if has_table_access is not False:
                raise NotificationDeliveryUnavailable()
        except NotificationDeliveryUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise NotificationDeliveryUnavailable() from None

    async def delivery_policy(self) -> NotificationDeliveryPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_notifications_v1.identity_delivery_policy()"
                )
                await self._assert_capabilities(connection)
                policy = decode_notification_delivery_policy(self._decode_json(raw))
                if policy.policy != NOTIFICATION_DELIVERY_POLICY:
                    raise NotificationDeliveryUnavailable()
                return policy
        except NotificationDeliveryUnavailable:
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
            raise NotificationDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def _call(self, function: str, request, decode: Callable[[Any], Any]):
        import asyncpg

        connection = None
        try:
            encoded = native_notification_command_json(request)
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    f"SELECT aap_notifications_v1.{function}($1::jsonb)", encoded
                )
                value = self._decode_json(raw)
                # Validate the complete owner reply and its request binding before
                # the claim/finish transaction is allowed to commit.
                result = decode(value)
                return result
        except NotificationDeliveryUnavailable:
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
            raise NotificationDeliveryUnavailable() from None
        finally:
            await self._close(connection)

    async def claim_deliveries(
        self,
        worker_id: UUID,
        *,
        limit: int = 16,
        lease_seconds: int = 60,
    ) -> list[NotificationLease]:
        try:
            request = NotificationClaimRequest(
                schema=NOTIFICATION_DELIVERY_PROTOCOL,
                worker_id=worker_id,
                limit=limit,
                lease_seconds=lease_seconds,
            )
        except (ValueError, TypeError):
            raise NotificationDeliveryUnavailable() from None

        def decode(value):
            reply = decode_notification_claim_reply(value)
            items = reply.data.items
            if len(items) > request.limit:
                raise NotificationDeliveryUnavailable()
            ids = [item.id for item in items]
            lease_ids = [item.lease_id for item in items]
            if len(ids) != len(set(ids)) or len(lease_ids) != len(set(lease_ids)):
                raise NotificationDeliveryUnavailable()
            if any(item.worker_id != request.worker_id for item in items):
                raise NotificationDeliveryUnavailable()
            return items

        return await self._call("claim_identity_deliveries", request, decode)

    async def finish_delivery(
        self,
        lease: NotificationLease,
        *,
        outcome: str,
        reason: str,
    ) -> bool:
        try:
            request = NotificationFinishRequest(
                schema=NOTIFICATION_DELIVERY_PROTOCOL,
                notification_id=lease.id,
                worker_id=lease.worker_id,
                lease_id=lease.lease_id,
                outcome=outcome,
                reason=reason,
            )
        except (ValueError, TypeError):
            raise NotificationDeliveryUnavailable() from None

        def decode(value):
            reply = decode_notification_finish_reply(value)
            return reply.data.accepted

        return await self._call("finish_identity_delivery", request, decode)
