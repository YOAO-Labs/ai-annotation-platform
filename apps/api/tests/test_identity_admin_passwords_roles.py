from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.admin_password_postgres import AdminPasswordPostgresData
from aap_identity.contract import IdentityUnavailable
from app.config import settings


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity administrator-password LOGIN acceptance requires the owned verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_ADMIN_PASSWORD_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_admin_password_client"
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_admin_password_login_has_only_its_two_functions():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_admin_password_client"
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
            """
        )
        assert {row["signature"] for row in rows} == {
            "aap_identity_admin_passwords_v1.admin_password_policy()",
            "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
        }
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_admin_passwords_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_admin_passwords_v1', 'CREATE')"
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
                    'aap_identity_admin_passwords_v1','public'
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
                      pg_catalog.has_table_privilege(current_user, relation.oid, 'SELECT')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'INSERT')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'UPDATE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'DELETE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'REFERENCES')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'TRIGGER')
                      OR pg_catalog.has_any_column_privilege(
                          current_user, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
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
            "SELECT aap_identity_admin_passwords_v1.admin_password_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-identity-admin-passwords-policy/v1",
            "policy": "aap-identity-admin-passwords/v1",
        }
        malformed = await connection.fetchval(
            "SELECT aap_identity_admin_passwords_v1.execute_admin_password_command('{}'::jsonb)"
        )
        assert json.loads(malformed) == {
            "schema": "aap-identity-admin-passwords-native/v1",
            "ok": False,
            "error": {
                "code": "invalid_request",
                "status_code": 400,
                "detail": None,
            },
        }
        for value in (None, "null", "[]", "1", '"scalar"'):
            invalid = await connection.fetchval(
                "SELECT aap_identity_admin_passwords_v1.execute_admin_password_command($1::jsonb)",
                value,
            )
            assert json.loads(invalid) == {
                "schema": "aap-identity-admin-passwords-native/v1",
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "status_code": 400,
                    "detail": None,
                },
            }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_admin_password_reset_receipts LIMIT 1",
            "SELECT aap_workspace_v1.admin_password_target_in_owned_project(NULL::uuid,NULL::uuid)",
            "CREATE TEMP TABLE admin_password_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_admin_password_policy_rejects_an_extra_security_definer_function():
    dsn = _dsn()
    function_name = f"identity_admin_password_probe_{uuid.uuid4().hex}"
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE sql IMMUTABLE SECURITY DEFINER AS $$ SELECT 1 $$"
                )
            )
            await connection.execute(
                text(f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC")
            )
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO identity_admin_password_client"
                )
            )

        with pytest.raises(IdentityUnavailable):
            await AdminPasswordPostgresData(dsn).admin_password_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_public_invoker_helper_cannot_write_private_identity_tables():
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
                    'admin-reset-probe'
                )
                """
            )
    finally:
        await connection.close()
