"""Exact function-only LOGIN boundary for native Identity briefs reads."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.contract import IdentityUnavailable
from aap_identity.briefs_postgres import BriefsPostgresData
from app.config import settings


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity briefs LOGIN acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_BRIEFS_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_briefs_client"
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_briefs_login_has_only_two_functions_and_no_raw_access():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_briefs_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            JOIN pg_catalog.pg_type AS return_type
              ON return_type.oid = function.prorettype
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND function.prokind IN ('f','p','a','w')
              AND NOT (
                  return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
                  AND return_type.typname IN ('trigger','event_trigger')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                    AND dependency.objid = function.oid
                    AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
                    AND dependency.deptype = 'e'
              )
              AND (
                  pg_catalog.left(namespace.nspname,4)='aap_'
                  OR function.prosecdef
                  OR EXISTS (
                      SELECT 1 FROM pg_catalog.aclexplode(COALESCE(
                          function.proacl,
                          pg_catalog.acldefault('f', function.proowner)
                      )) AS acl
                      WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                        AND pg_catalog.pg_has_role(
                            current_user, acl.grantee, 'USAGE'
                        )
                  )
              )
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY namespace.nspname, function.proname,
                     pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_identity_briefs_v1.briefs_policy()",
            "aap_identity_briefs_v1.execute_briefs_command(jsonb)",
        ]
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_briefs_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_briefs_v1', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user, "
            "current_database(), 'CREATE,TEMP')"
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname NOT IN (
                    'pg_catalog','information_schema',
                    'aap_identity_briefs_v1','public'
                )
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND (pg_catalog.has_schema_privilege(
                           current_user, namespace.oid, 'USAGE'
                       ) OR pg_catalog.has_schema_privilege(
                           current_user, namespace.oid, 'CREATE'
                       ))
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation
                  ON relation.relnamespace = namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind IN ('r','p','v','m','f')
                  AND (
                      pg_catalog.has_table_privilege(
                          current_user, relation.oid,
                          'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                      ) OR pg_catalog.has_any_column_privilege(
                          current_user, relation.oid,
                          'SELECT,INSERT,UPDATE,REFERENCES'
                      )
                  )
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS sequence
                  ON sequence.relnamespace = namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND sequence.relkind = 'S'
                  AND (pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'USAGE'
                       ) OR pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'SELECT'
                       ) OR pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'UPDATE'
                       ))
            )
            """
        )

        policy = await connection.fetchval(
            "SELECT aap_identity_briefs_v1.briefs_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-identity-briefs-policy/v1",
            "policy": "aap-identity-briefs/v1",
        }
        malformed = (
            "NULL::jsonb",
            "'null'::jsonb",
            "'[]'::jsonb",
            "'{}'::jsonb",
            '\'{"schema":"aap-identity-briefs-native/v1","operation":"get_user_briefs","payload":{"user_ids":[true]}}\'::jsonb',
            '\'{"schema":"aap-identity-briefs-native/v1","operation":"get_user_briefs","extra":1,"payload":{"user_ids":[]}}\'::jsonb',
        )
        for command in malformed:
            raw = await connection.fetchval(
                "SELECT aap_identity_briefs_v1.execute_briefs_command(" + command + ")"
            )
            assert json.loads(raw) == {
                "schema": "aap-identity-briefs-native/v1",
                "ok": False,
                "error": {"code": "invalid_request"},
            }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.project_members LIMIT 1",
            "SELECT aap_workspace_v1.identity_brief_project_roles(NULL::uuid,'{}'::uuid[])",
            "SELECT aap_identity_briefs_v1._failure()",
            "CREATE TEMP TABLE identity_briefs_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_briefs_readiness_rejects_raw_user_privilege():
    dsn = _dsn()
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("GRANT SELECT ON public.users TO identity_briefs_client")
            )
        with pytest.raises(IdentityUnavailable):
            await BriefsPostgresData(dsn).briefs_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("REVOKE SELECT ON public.users FROM identity_briefs_client")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_briefs_readiness_rejects_extra_public_function_grant():
    dsn = _dsn()
    function_name = f"identity_briefs_probe_{uuid.uuid4().hex}"
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$"
                )
            )
            await connection.execute(
                text(f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC")
            )
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO identity_briefs_client"
                )
            )

        with pytest.raises(IdentityUnavailable):
            await BriefsPostgresData(dsn).briefs_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_briefs_readiness_rejects_database_temp_privilege():
    dsn = _dsn()
    database = make_url(dsn).database
    assert database is not None
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    quoted_database = owner_engine.sync_engine.dialect.identifier_preparer.quote(
        database
    )
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"GRANT TEMP ON DATABASE {quoted_database} "
                    "TO identity_briefs_client"
                )
            )
        with pytest.raises(IdentityUnavailable):
            await BriefsPostgresData(dsn).briefs_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"REVOKE TEMP ON DATABASE {quoted_database} "
                    "FROM identity_briefs_client"
                )
            )
        await owner_engine.dispose()

    assert (
        await BriefsPostgresData(dsn).briefs_policy()
    ).policy == "aap-identity-briefs/v1"
