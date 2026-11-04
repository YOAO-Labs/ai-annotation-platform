"""Function-only PostgreSQL binding for Identity offboarding commands."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .contract import IdentityUnavailable
from .offboarding_contract import (
    NativeOffboardingApplyCommand,
    NativeOffboardingPrepareCommand,
    OffboardingPolicyReply,
    decode_native_offboarding_command,
    decode_native_offboarding_reply,
    decode_offboarding_policy,
)


def _json_result(raw):
    if isinstance(raw, (str, bytes, bytearray, memoryview)):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise IdentityUnavailable() from None
    return raw


class OffboardingPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def prepare_offboarding_command(self, payload: dict | bytes) -> dict:
        command = decode_native_offboarding_command(payload)
        if not isinstance(command, NativeOffboardingPrepareCommand):
            raise IdentityUnavailable()
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_offboarding_v1.prepare_offboarding_command("
                "$1::jsonb)",
                encoded,
            )
            reply = decode_native_offboarding_reply(command, _json_result(raw))
            return reply.model_dump(mode="json", by_alias=True)
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None

    async def apply_offboarding_command(self, payload: dict | bytes) -> dict:
        command = decode_native_offboarding_command(payload)
        if not isinstance(command, NativeOffboardingApplyCommand):
            raise IdentityUnavailable()
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_offboarding_v1.apply_offboarding_command("
                "$1::jsonb)",
                encoded,
            )
            reply = decode_native_offboarding_reply(command, _json_result(raw))
            return reply.model_dump(mode="json", by_alias=True)
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class OffboardingPostgresData:
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
                server_settings={"application_name": "aap-identity-offboarding"},
            )
            async with connection.transaction():
                yield OffboardingPostgresTransaction(connection)
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
                       pg_catalog.oidvectortypes(function.proargtypes) AS args
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
                      return_type.typnamespace =
                          'pg_catalog'::pg_catalog.regnamespace
                      AND return_type.typname IN ('trigger', 'event_trigger')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_depend AS dependency
                      WHERE dependency.classid =
                          'pg_catalog.pg_proc'::pg_catalog.regclass
                        AND dependency.objid = function.oid
                        AND dependency.refclassid =
                            'pg_catalog.pg_extension'::pg_catalog.regclass
                        AND dependency.deptype = 'e'
                  )
                  AND (
                      pg_catalog.left(namespace.nspname, 4) = 'aap_'
                      OR function.prosecdef
                      OR EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(
                              COALESCE(
                                  function.proacl,
                                  pg_catalog.acldefault('f', function.proowner)
                              )
                          ) AS acl
                          WHERE acl.privilege_type = 'EXECUTE'
                            AND acl.grantee <> 0
                            AND pg_catalog.pg_has_role(
                                current_user, acl.grantee, 'USAGE'
                            )
                      )
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

            invalid_schema_access = await connection.fetchval(
                """
                SELECT
                    NOT pg_catalog.has_schema_privilege(
                        current_user, 'aap_identity_offboarding_v1', 'USAGE'
                    )
                    OR pg_catalog.has_schema_privilege(
                        current_user, 'aap_identity_offboarding_v1', 'CREATE'
                    )
                    OR pg_catalog.has_schema_privilege(
                        current_user, 'public', 'CREATE'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_namespace AS namespace
                        WHERE namespace.nspname NOT IN (
                            'pg_catalog', 'information_schema',
                            'aap_identity_offboarding_v1', 'public'
                        )
                          AND namespace.nspname NOT LIKE 'pg_toast%'
                          AND namespace.nspname NOT LIKE 'pg_temp_%'
                          AND (
                              pg_catalog.has_schema_privilege(
                                  current_user, namespace.oid, 'USAGE'
                              )
                              OR pg_catalog.has_schema_privilege(
                                  current_user, namespace.oid, 'CREATE'
                              )
                          )
                    )
                    OR pg_catalog.has_database_privilege(
                        current_user, current_database(), 'CREATE,TEMP'
                    )
                """
            )
            if invalid_schema_access is not False:
                raise IdentityUnavailable()

            has_relation_access = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname NOT IN (
                        'pg_catalog', 'information_schema'
                    )
                      AND namespace.nspname NOT LIKE 'pg_toast%'
                      AND namespace.nspname NOT LIKE 'pg_temp_%'
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
            if has_relation_access is not False:
                raise IdentityUnavailable()

            has_sequence_access = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname NOT IN (
                        'pg_catalog', 'information_schema'
                    )
                      AND namespace.nspname NOT LIKE 'pg_toast%'
                      AND namespace.nspname NOT LIKE 'pg_temp_%'
                      AND relation.relkind = 'S'
                      AND (
                          pg_catalog.has_sequence_privilege(
                              current_user, relation.oid, 'USAGE'
                          )
                          OR pg_catalog.has_sequence_privilege(
                              current_user, relation.oid, 'SELECT'
                          )
                          OR pg_catalog.has_sequence_privilege(
                              current_user, relation.oid, 'UPDATE'
                          )
                      )
                )
                """
            )
            if has_sequence_access is not False:
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

    async def offboarding_policy(self) -> OffboardingPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-offboarding"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_offboarding_v1.offboarding_policy()"
                )
                await self._assert_capabilities(
                    connection,
                    {
                        "aap_identity_offboarding_v1.offboarding_policy()",
                        "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)",
                        "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)",
                    },
                )
                return decode_offboarding_policy(_json_result(raw))
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
