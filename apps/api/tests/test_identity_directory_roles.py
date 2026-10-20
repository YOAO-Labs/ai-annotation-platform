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
from aap_identity.directory_postgres import DirectoryPostgresData
from app.config import settings


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity directory LOGIN acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_DIRECTORY_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_directory_client"
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_directory_login_has_only_two_functions_and_no_table_access():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_directory_client"
        )
        functions = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '('
                   || pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE left(namespace.nspname,4)='aap_'
              AND function.prokind IN ('f','p')
              AND function.prorettype NOT IN (
                  'trigger'::pg_catalog.regtype,
                  'event_trigger'::pg_catalog.regtype
              )
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            """
        )
        assert {row["signature"] for row in functions} == {
            "aap_identity_directory_v1.directory_policy()",
            "aap_identity_directory_v1.execute_directory_command(jsonb)",
        }
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_directory_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_directory_v1', 'CREATE')"
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE left(namespace.nspname,4)='aap_'
                  AND namespace.nspname <> 'aap_identity_directory_v1'
                  AND pg_catalog.has_schema_privilege(
                      current_user, namespace.oid, 'USAGE,CREATE'
                  )
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
                  AND (pg_catalog.has_table_privilege(
                           current_user, relation.oid,
                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                       ) OR pg_catalog.has_any_column_privilege(
                           current_user, relation.oid,
                           'SELECT,INSERT,UPDATE,REFERENCES'
                       ))
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
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user, "
            "current_database(), 'CREATE,TEMP')"
        )

        policy = await connection.fetchval(
            "SELECT aap_identity_directory_v1.directory_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-identity-directory-policy/v1",
            "policy": "aap-identity-directory/v1",
        }
        invalid = await connection.fetchval(
            "SELECT aap_identity_directory_v1.execute_directory_command('{}'::jsonb)"
        )
        assert json.loads(invalid) == {
            "schema": "aap-identity-directory-native/v1",
            "ok": False,
            "error": {"code": "invalid_request", "status_code": 400, "detail": None},
        }
        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_directory_export_receipts LIMIT 1",
            "SELECT aap_workspace_v1.identity_directory_owned_member_ids(NULL::uuid)",
            "SELECT aap_identity_directory_v1._scope_user_ids(NULL::uuid,'super_admin','export_users','{}'::jsonb,true)",
            "CREATE TEMP TABLE identity_directory_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_directory_policy_rejects_explicit_public_function_grant():
    dsn = _dsn()
    function_name = f"identity_directory_probe_{uuid.uuid4().hex}"
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
                    "TO identity_directory_client"
                )
            )

        with pytest.raises(IdentityUnavailable):
            await DirectoryPostgresData(dsn).directory_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_public_invoker_helper_cannot_write_private_tables():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval(
            "SELECT pg_catalog.has_function_privilege(current_user, "
            "'public.record_mask_annotation_revision(uuid,uuid,uuid,integer,jsonb,text)', "
            "'EXECUTE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_table_privilege(current_user, "
            "'public.mask_annotation_revisions', 'INSERT')"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(
                """
                SELECT public.record_mask_annotation_revision(
                    pg_catalog.gen_random_uuid(),
                    pg_catalog.gen_random_uuid(),
                    pg_catalog.gen_random_uuid(),
                    1,
                    '{}'::jsonb,
                    'directory-role-probe'
                )
                """
            )
    finally:
        await connection.close()
