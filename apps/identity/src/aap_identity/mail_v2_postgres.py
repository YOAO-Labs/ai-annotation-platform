"""Function-only PostgreSQL client for the versioned mail worker."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from uuid import UUID

from .contract import IdentityUnavailable
from .mail_v2_contract import (
    MAIL_V2_NATIVE_PROTOCOL,
    MAIL_V2_POLICY,
    MailV2ClaimRequest,
    MailV2FinishRequest,
    MailV2Lease,
    MailV2PolicyReply,
    MailV2RenewRequest,
    decode_mail_v2_accepted_reply,
    decode_mail_v2_claim_reply,
    decode_mail_v2_policy,
    native_mail_v2_command_json,
)

_MAIL_V2_CAPABILITIES = {
    "aap_identity_mail_v2.mail_policy()",
    "aap_identity_mail_v2.claim_mail(jsonb)",
    "aap_identity_mail_v2.renew_mail(jsonb)",
    "aap_identity_mail_v2.finish_mail(jsonb)",
}


class _RollbackAndReturn(Exception):
    def __init__(self, value: bool):
        self.value = value


class MailV2PostgresData:
    """Call only the four versioned MAIL capabilities."""

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
            server_settings={"application_name": "aap-identity-mail-v2"},
        )

    async def _close(self, connection) -> None:
        if connection is None:
            return
        try:
            await asyncio.shield(connection.close(timeout=2))
        except Exception:
            if not asyncio.current_task().cancelling():
                raise IdentityUnavailable() from None

    @staticmethod
    def _json_value(raw):
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError, UnicodeError):
            raise IdentityUnavailable() from None

    async def _execute(self, function: str, request, decode: Callable[[Any], Any]):
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    f"SELECT aap_identity_mail_v2.{function}($1::jsonb)",
                    native_mail_v2_command_json(request),
                )
                result = decode(self._json_value(raw))
                return result
        except _RollbackAndReturn as rollback:
            return rollback.value
        except IdentityUnavailable:
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
            raise IdentityUnavailable() from None
        finally:
            await self._close(connection)

    @staticmethod
    async def _assert_capabilities(connection) -> None:
        try:
            rows = await connection.fetch(
                """
                SELECT namespace.nspname, function.proname,
                       pg_catalog.oidvectortypes(function.proargtypes) AS args,
                       function.prosecdef
                FROM pg_catalog.pg_proc AS function
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = function.pronamespace
                WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND function.prokind IN ('f', 'p', 'a', 'w')
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
            if actual != _MAIL_V2_CAPABILITIES:
                raise IdentityUnavailable()
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
                      )
                )
                """
            )
            if has_table_access is not False:
                raise IdentityUnavailable()
        except IdentityUnavailable:
            raise
        except Exception:
            raise IdentityUnavailable() from None

    async def mail_policy(self) -> MailV2PolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_mail_v2.mail_policy()"
                )
                policy = decode_mail_v2_policy(self._json_value(raw))
                if policy.policy != MAIL_V2_POLICY:
                    raise IdentityUnavailable()
                await self._assert_capabilities(connection)
                return policy
        except IdentityUnavailable:
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
            raise IdentityUnavailable() from None
        finally:
            await self._close(connection)

    async def claim_mail(
        self,
        worker_id: UUID,
        *,
        lease_seconds: int = 60,
        delivery_id: UUID | None = None,
    ) -> MailV2Lease | None:
        try:
            request = MailV2ClaimRequest(
                schema=MAIL_V2_NATIVE_PROTOCOL,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                delivery_id=delivery_id,
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None

        def decode(value):
            reply = decode_mail_v2_claim_reply(value)
            lease = reply.data.delivery
            if lease is not None and (
                lease.worker_id != request.worker_id
                or (request.delivery_id is not None and lease.id != request.delivery_id)
            ):
                raise IdentityUnavailable()
            return lease

        return await self._execute("claim_mail", request, decode)

    async def renew_mail(self, lease: MailV2Lease) -> bool:
        try:
            request = MailV2RenewRequest(
                schema=MAIL_V2_NATIVE_PROTOCOL,
                delivery_id=lease.id,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None

        def decode(value):
            reply = decode_mail_v2_accepted_reply(value)
            if not reply.data.accepted:
                raise _RollbackAndReturn(False)
            return True

        return await self._execute("renew_mail", request, decode)

    async def finish_mail(
        self,
        lease: MailV2Lease,
        *,
        outcome: str,
        reason: str,
        retry_delay_seconds: int | None,
    ) -> bool:
        try:
            request = MailV2FinishRequest(
                schema=MAIL_V2_NATIVE_PROTOCOL,
                delivery_id=lease.id,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
                outcome=outcome,
                reason=reason,
                retry_delay_seconds=retry_delay_seconds,
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None

        def decode(value):
            reply = decode_mail_v2_accepted_reply(value)
            if not reply.data.accepted:
                raise _RollbackAndReturn(False)
            return True

        return await self._execute("finish_mail", request, decode)
