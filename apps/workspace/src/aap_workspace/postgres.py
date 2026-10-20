"""Function-only Workspace binding; prepare and apply share one transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

from .contract import (
    MEMBERSHIP_NATIVE_TEXT_CODEC,
    MembershipUnavailable,
    MembershipInvalidRequest,
    NativeMembershipApplyRequest,
    decode_membership_request,
    decode_membership_policy,
    decode_native_membership_reply,
    native_membership_command_json,
)

_NATIVE_SCHEMA = "aap-workspace-membership-native/v1"
_OPERATIONS = {
    "add_member",
    "preview_role_change",
    "change_role",
    "remove_member",
}


def _result(value):
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        try:
            return json.loads(bytes(value) if isinstance(value, memoryview) else value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MembershipUnavailable() from None
    return value


class MembershipPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def _command(self, function: str, payload: dict):
        try:
            if function == "apply_membership_command":
                payload = NativeMembershipApplyRequest.model_validate_json(
                    json.dumps(payload, ensure_ascii=True, allow_nan=False)
                ).model_dump(mode="json", by_alias=True)
            request = decode_membership_request(
                payload["request"]
                if function == "apply_membership_command"
                else payload
            )
            raw = await self.connection.fetchval(
                f"SELECT aap_workspace_membership_v1.{function}($1::jsonb)",
                native_membership_command_json(payload),
            )
            reply = decode_native_membership_reply(
                request, raw.encode("utf-8") if isinstance(raw, str) else raw
            )
            return reply.model_dump(mode="json", by_alias=True)
        except (
            MembershipInvalidRequest,
            KeyError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            raise MembershipUnavailable() from None

    async def prepare_membership_command(self, payload: dict):
        return await self._command("prepare_membership_command", payload)

    async def apply_membership_command(self, payload: dict):
        return await self._command("apply_membership_command", payload)


class MembershipPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 30
    ):
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _connect(self):
        import asyncpg

        try:
            return await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-workspace-membership"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise MembershipUnavailable() from None

    @asynccontextmanager
    async def transaction(self, *, write: bool):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=not write):
                try:
                    policy = await connection.fetchval(
                        "SELECT aap_workspace_membership_v1.membership_policy()"
                    )
                except asyncpg.PostgresError:
                    raise MembershipUnavailable() from None
                decode_membership_policy(policy)
                yield MembershipPostgresTransaction(connection)
        except MembershipUnavailable:
            raise
        except asyncpg.PostgresError as error:
            state = error.sqlstate or ""
            if state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                raise MembershipUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError, ValueError, TypeError):
            raise MembershipUnavailable() from None
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
                        raise MembershipUnavailable() from None

    async def membership_policy(self) -> dict:
        """Reject a broad or incomplete credential before marking an owner ready."""
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            raw = _result(
                await connection.fetchval(
                    "SELECT aap_workspace_membership_v1.membership_policy()"
                )
            )
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema", "operations", "opaque_text_codec"}
                or raw["schema"] != _NATIVE_SCHEMA
                or raw["opaque_text_codec"] != MEMBERSHIP_NATIVE_TEXT_CODEC
                or not isinstance(raw["operations"], list)
                or not all(isinstance(value, str) for value in raw["operations"])
                or len(raw["operations"]) != len(_OPERATIONS)
                or set(raw["operations"]) != _OPERATIONS
            ):
                raise MembershipUnavailable()
            functions = await connection.fetch(
                """
                SELECT namespace.nspname || '.' || function.proname || '(' ||
                       pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
                FROM pg_catalog.pg_proc AS function
                JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
                JOIN pg_catalog.pg_type AS return_type ON return_type.oid=function.prorettype
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND function.prokind IN ('f','p','a','w')
                  AND NOT (return_type.typnamespace='pg_catalog'::pg_catalog.regnamespace
                      AND return_type.typname IN ('trigger','event_trigger'))
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_depend AS dependency
                      WHERE dependency.classid='pg_catalog.pg_proc'::pg_catalog.regclass
                        AND dependency.objid=function.oid
                        AND dependency.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass
                        AND dependency.deptype='e')
                  AND (pg_catalog.left(namespace.nspname,4)='aap_'
                    OR function.prosecdef
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                            pg_catalog.acldefault('f',function.proowner))) AS acl
                        WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                          AND pg_catalog.pg_has_role(current_user,acl.grantee,'USAGE')))
                  AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
                """
            )
            expected = {
                f"aap_workspace_membership_v1.{name}({arguments})"
                for name, arguments in (
                    ("membership_policy", ""),
                    ("prepare_membership_command", "jsonb"),
                    ("apply_membership_command", "jsonb"),
                )
            }
            if {row["signature"] for row in functions} != expected:
                raise MembershipUnavailable()
            denied = await connection.fetchval(
                """
                SELECT current_user <> 'workspace_membership_client'
                    OR current_user <> session_user
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=current_user
                        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members
                        WHERE member=current_user::pg_catalog.regrole)
                    OR pg_catalog.has_database_privilege(current_user,current_database(),'CREATE,TEMP')
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                          AND namespace.nspname NOT LIKE 'pg_toast%'
                          AND namespace.nspname NOT LIKE 'pg_temp_%'
                          AND (pg_catalog.has_schema_privilege(current_user,namespace.oid,'CREATE')
                            OR (namespace.nspname NOT IN ('public','aap_workspace_membership_v1')
                              AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE'))))
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_class AS relation
                        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
                        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                          AND namespace.nspname NOT LIKE 'pg_toast%'
                          AND namespace.nspname NOT LIKE 'pg_temp_%'
                          AND ((relation.relkind IN ('r','p','v','m','f')
                            AND (pg_catalog.has_table_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                              OR pg_catalog.has_any_column_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
                            OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(current_user,relation.oid,'SELECT,UPDATE,USAGE'))))
                """
            )
            if denied is not False:
                raise MembershipUnavailable()
            return raw
        except MembershipUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise MembershipUnavailable() from None
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
                        raise MembershipUnavailable() from None
