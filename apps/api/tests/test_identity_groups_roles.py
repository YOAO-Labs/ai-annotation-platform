"""Exact function-only Identity group LOGIN and cross-module boundaries."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity group LOGIN acceptance requires the owned module-data verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_GROUP_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_group_login_has_only_two_functions_and_no_direct_access():
    connection = await asyncpg.connect(_dsn())
    try:
        actual = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '('
                   || pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
            WHERE left(namespace.nspname,4)='aap_'
              AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
            """
        )
        assert {row["signature"] for row in actual} == {
            "aap_identity_groups_v1.group_policy()",
            "aap_identity_groups_v1.execute_group_command(jsonb)",
        }
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
                  AND namespace.nspname <> 'aap_identity_groups_v1'
                  AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE')
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind IN ('r','p','v','m','f')
                  AND (pg_catalog.has_table_privilege(current_user,relation.oid,
                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                    OR pg_catalog.has_any_column_privilege(current_user,relation.oid,
                           'SELECT,INSERT,UPDATE,REFERENCES'))
            )
            """
        )
        assert not await connection.fetchval(
            "SELECT has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'CREATE')
            )
            """
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.pg_has_role(current_user,'identity_code','USAGE')"
        )

        policy = json.loads(
            await connection.fetchval("SELECT aap_identity_groups_v1.group_policy()")
        )
        assert policy == {
            "schema": "aap-identity-groups-policy/v1",
            "policy": "aap-identity-groups/v1",
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_groups_v1.execute_group_command('{}'::jsonb)"
            )
        )
        assert invalid["error"]["code"] == "invalid_request"

        for sql in (
            "SELECT * FROM public.groups LIMIT 1",
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_group_operation_receipts LIMIT 1",
            "SELECT * FROM public.audit_logs LIMIT 1",
            "SELECT aap_identity_groups_v1.resolve_invitation_group(gen_random_uuid(),'secret')",
            "SELECT aap_identity_groups_v1._group_scope(gen_random_uuid(),ARRAY[]::uuid[],false)",
            "SELECT aap_workspace_v1.lock_group_assignment_scope(gen_random_uuid(),ARRAY[]::uuid[],false)",
            "SELECT aap_audit_v1.record_identity_group_event('group.create',NULL,'{}','{}')",
            "CREATE TEMP TABLE identity_group_forbidden(id integer)",
            "SET ROLE identity_code",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()
