from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


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
