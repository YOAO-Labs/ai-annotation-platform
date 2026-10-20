"""Exact function-only Identity account and presence LOGINS."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn(environment_name: str) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity account/presence LOGIN acceptance requires the owned module-data verifier"
        )
    value = os.environ.get(environment_name)
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_account_login_has_only_three_functions_and_no_direct_access():
    connection = await asyncpg.connect(_dsn("AAP_MODULE_DATA_IDENTITY_ACCOUNTS_URL"))
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
            "aap_identity_accounts_v1.account_policy()",
            "aap_identity_accounts_v1.prepare_account_command(jsonb)",
            "aap_identity_accounts_v1.apply_account_command(jsonb)",
        }
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
                  AND namespace.nspname <> 'aap_identity_accounts_v1'
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
            await connection.fetchval(
                "SELECT aap_identity_accounts_v1.account_policy()"
            )
        )
        assert policy == {
            "schema": "aap-identity-accounts-policy/v1",
            "policy": "aap-identity-accounts/v1",
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_accounts_v1.prepare_account_command('{}'::jsonb)"
            )
        )
        assert invalid["error"]["code"] == "invalid_request"

        for sql in (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_account_operation_receipts LIMIT 1",
            "SELECT * FROM public.audit_logs LIMIT 1",
            "SELECT aap_audit_v1.record_identity_account_event('update_name',NULL,'{}','{}')",
            "SELECT aap_identity_accounts_v1._failure('invalid_request',NULL,NULL)",
            "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline(NULL::timestamptz)",
            "CREATE TEMP TABLE identity_account_forbidden(id integer)",
            "SET ROLE identity_code",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_identity_presence_login_has_only_its_operator_function():
    connection = await asyncpg.connect(_dsn("AAP_MODULE_DATA_IDENTITY_PRESENCE_URL"))
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
            "aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamp with time zone)"
        }
        assert await connection.fetchval(
            """
            SELECT pg_catalog.has_function_privilege(
                current_user,
                (SELECT function.oid FROM pg_catalog.pg_proc AS function JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='aap_identity_accounts_v1' AND function.proname='mark_inactive_accounts_offline'),
                'EXECUTE'
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
        for sql in (
            "SELECT aap_identity_accounts_v1.account_policy()",
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_account_operation_receipts LIMIT 1",
            "SELECT * FROM public.audit_logs LIMIT 1",
            "CREATE TEMP TABLE identity_presence_forbidden(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
        with pytest.raises(asyncpg.PostgresError) as invalid_cutoff:
            await connection.fetchval(
                "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline(NULL::timestamptz)"
            )
        assert invalid_cutoff.value.sqlstate == "22023"
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_account_and_presence_logins_do_not_inherit_old_identity_or_audit_access():
    connection = await asyncpg.connect(_dsn("AAP_MODULE_DATA_IDENTITY_ACCOUNTS_URL"))
    try:
        roles = [
            "identity_account_client",
            "identity_presence_client",
            "identity_session_client",
            "identity_auth_client",
            "identity_key_client",
        ]
        rows = await connection.fetch(
            """
            SELECT role.rolname,
                   pg_catalog.has_schema_privilege(role.oid,'aap_audit_v1','USAGE')
                       AS audit_schema,
                   pg_catalog.has_function_privilege(
                       role.oid,
                       (SELECT function.oid FROM pg_catalog.pg_proc AS function JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='aap_audit_v1' AND function.proname='record_identity_account_event'),
                       'EXECUTE'
                   ) AS audit_function,
                   pg_catalog.has_function_privilege(
                       role.oid,
                       (SELECT function.oid FROM pg_catalog.pg_proc AS function JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='aap_identity_accounts_v1' AND function.proname='account_policy'),
                       'EXECUTE'
                   ) AS account_function,
                   pg_catalog.has_function_privilege(
                       role.oid,
                       (SELECT function.oid FROM pg_catalog.pg_proc AS function JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='aap_identity_accounts_v1' AND function.proname='mark_inactive_accounts_offline'),
                       'EXECUTE'
                   ) AS presence_function,
                   pg_catalog.has_table_privilege(
                       role.oid,'public.audit_logs',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                   ) OR pg_catalog.has_any_column_privilege(
                       role.oid,'public.audit_logs','SELECT,INSERT,UPDATE,REFERENCES'
                   ) AS audit_table,
                   pg_catalog.has_table_privilege(
                       role.oid,'public.users',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                   ) OR pg_catalog.has_any_column_privilege(
                       role.oid,'public.users','SELECT,INSERT,UPDATE,REFERENCES'
                   ) AS users_table
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = ANY($1::text[])
            """,
            roles,
        )
        actual = {row["rolname"]: row for row in rows}
        assert set(actual) == set(roles)
        for row in actual.values():
            assert not row["audit_schema"]
            assert not row["audit_function"]
            assert not row["audit_table"]
            assert not row["users_table"]
        assert actual["identity_account_client"]["account_function"] is True
        assert actual["identity_presence_client"]["account_function"] is False
        assert actual["identity_presence_client"]["presence_function"] is True
        for old_role in (
            "identity_session_client",
            "identity_auth_client",
            "identity_key_client",
        ):
            assert not actual[old_role]["account_function"]
            assert not actual[old_role]["presence_function"]
        assert await connection.fetchval(
            """
            SELECT pg_catalog.has_function_privilege(
                'identity_code',
                (SELECT function.oid FROM pg_catalog.pg_proc AS function JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='aap_audit_v1' AND function.proname='record_identity_account_event'),
                'EXECUTE'
            )
            """
        )
    finally:
        await connection.close()
