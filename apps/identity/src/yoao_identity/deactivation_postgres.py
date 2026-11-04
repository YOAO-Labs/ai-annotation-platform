"""Function-only PostgreSQL bindings for Identity deactivation commands."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .contract import IdentityUnavailable
from .deactivation_contract import (
    DeactivationDueFailure,
    DeactivationDuePolicyReply,
    DeactivationDueReply,
    DeactivationDueRequest,
    DeactivationPolicyReply,
    NativeDeactivationCommand,
    decode_deactivation_due_policy,
    decode_deactivation_due_reply,
    decode_deactivation_policy,
    decode_native_deactivation_command,
    decode_native_deactivation_due_reply,
    native_deactivation_due_command,
)


class DeactivationPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def execute_deactivation_command(self, payload: dict | bytes) -> dict:
        command: NativeDeactivationCommand = decode_native_deactivation_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_deactivation_v1.execute_deactivation_command("
                "$1::jsonb)",
                encoded,
            )
            return json.loads(raw) if isinstance(raw, str) else raw
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class DeactivationPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-self-deactivation"},
            )
            async with connection.transaction():
                yield DeactivationPostgresTransaction(connection)
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    if not asyncio.current_task().cancelling():
                        raise IdentityUnavailable() from None

    @staticmethod
    async def _assert_capabilities(connection, expected: set[str]) -> None:
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
                JOIN pg_catalog.pg_type AS return_type
                  ON return_type.oid = function.prorettype
                WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND function.prokind IN ('f', 'p', 'a', 'w')
                  AND NOT (
                      return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
                      AND return_type.typname IN ('trigger', 'event_trigger')
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
            if actual != expected:
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
                          OR pg_catalog.has_any_column_privilege(
                              current_user, relation.oid,
                              'SELECT,INSERT,UPDATE,REFERENCES'
                          )
                      )
                )
                """
            )
            if has_table_access is not False:
                raise IdentityUnavailable()
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None

    async def deactivation_policy(self) -> DeactivationPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-self-deactivation"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.deactivation_policy()"
                )
                await DeactivationPostgresData._assert_capabilities(
                    connection,
                    {
                        "aap_identity_deactivation_v1.deactivation_policy()",
                        "aap_identity_deactivation_v1.execute_deactivation_command(jsonb)",
                    },
                )
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_deactivation_policy(value)
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    if not asyncio.current_task().cancelling():
                        raise IdentityUnavailable() from None


class DeactivationDuePostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def process_due_deactivations(
        self, request: DeactivationDueRequest
    ) -> DeactivationDueReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-deactivation-due"},
            )
            async with connection.transaction():
                command = native_deactivation_due_command(request)
                encoded = json.dumps(
                    command.model_dump(mode="json", by_alias=True),
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.process_due_deactivations("
                    "$1::jsonb)",
                    encoded,
                )
                native = decode_native_deactivation_due_reply(
                    request, json.loads(raw) if isinstance(raw, str) else raw
                )
                # Validate the exact public projection before transaction commit.
                return decode_deactivation_due_reply(
                    request,
                    {
                        "schema": "aap-identity-deactivation-due/v1",
                        "ok": True,
                        "operation_id": str(native.operation_id),
                        "processed": native.processed,
                        "examined": native.examined,
                        "blocked_last_super_admin": native.blocked_last_super_admin,
                        "has_more": native.has_more,
                    },
                )
        except (DeactivationDueFailure, IdentityUnavailable):
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    if not asyncio.current_task().cancelling():
                        raise IdentityUnavailable() from None

    async def due_policy(self) -> DeactivationDuePolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-deactivation-due"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.due_policy()"
                )
                await DeactivationPostgresData._assert_capabilities(
                    connection,
                    {
                        "aap_identity_deactivation_v1.due_policy()",
                        "aap_identity_deactivation_v1.process_due_deactivations(jsonb)",
                    },
                )
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_deactivation_due_policy(value)
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    if not asyncio.current_task().cancelling():
                        raise IdentityUnavailable() from None
