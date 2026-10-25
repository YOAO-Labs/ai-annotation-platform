"""Exact function-only Identity session LOGIN and cross-module role fences."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity session LOGIN acceptance requires the owned module-data verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_SESSIONS_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_session_login_has_only_three_functions_and_no_direct_data_access():
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
            "aap_identity_sessions_v1.session_policy()",
            "aap_identity_sessions_v1.read_login_candidate(text)",
            "aap_identity_sessions_v1.execute_session_command(jsonb)",
        }
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
                  AND namespace.nspname <> 'aap_identity_sessions_v1'
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
                "SELECT aap_identity_sessions_v1.session_policy()"
            )
        )
        assert policy == {
            "schema": "aap-identity-sessions-policy/v1",
            "issuer": "aap-identity/v1",
            "algorithm": "HS256",
            "refresh_grace_seconds": 604800,
        }
        candidate = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_sessions_v1.read_login_candidate($1)",
                "identity-session-missing@example.test",
            )
        )
        assert candidate == {
            "schema": "aap-identity-login-candidate/v1",
            "item": None,
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_sessions_v1.execute_session_command('{}'::jsonb)"
            )
        )
        assert invalid["error"]["code"] == "invalid_request"

        for sql in (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT key_hash FROM public.api_keys LIMIT 1",
            "SELECT * FROM public.identity_session_operation_receipts LIMIT 1",
            "SELECT * FROM public.audit_logs LIMIT 1",
            "SELECT aap_audit_v1.record_identity_session_event('logout',NULL,'{}','null')",
            "SELECT aap_identity_v1.authentication_policy()",
            "SELECT aap_identity_sessions_v1._failure('invalid_request',NULL)",
            "CREATE TEMP TABLE identity_session_forbidden(id integer)",
            "SET ROLE identity_code",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_session_auth_and_key_logins_cannot_call_audit_or_access_audit_tables():
    connection = await asyncpg.connect(_dsn())
    try:
        audit_function_oid = await connection.fetchval(
            "SELECT operation.oid FROM pg_catalog.pg_proc AS operation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=operation.pronamespace "
            "WHERE namespace.nspname='aap_audit_v1' "
            "AND operation.proname='record_identity_session_event' "
            "AND pg_catalog.oidvectortypes(operation.proargtypes)='text, jsonb, jsonb, jsonb'"
        )
        assert type(audit_function_oid) is int
        rows = await connection.fetch(
            """
            SELECT role.rolname,
                   pg_catalog.has_schema_privilege(role.oid,'aap_audit_v1','USAGE')
                       AS audit_schema,
                   pg_catalog.has_function_privilege(
                       role.oid,
                       $2::oid,
                       'EXECUTE'
                   ) AS audit_function,
                   pg_catalog.has_table_privilege(
                       role.oid,'public.audit_logs',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                   ) OR pg_catalog.has_any_column_privilege(
                       role.oid,'public.audit_logs','SELECT,INSERT,UPDATE,REFERENCES'
                   ) AS audit_table
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = ANY($1::text[])
            """,
            [
                "identity_session_client",
                "identity_auth_client",
                "identity_key_client",
            ],
            audit_function_oid,
        )
        actual = {row["rolname"]: row for row in rows}
        assert set(actual) == {
            "identity_session_client",
            "identity_auth_client",
            "identity_key_client",
        }
        for row in actual.values():
            assert not row["audit_schema"]
            assert not row["audit_function"]
            assert not row["audit_table"]
        assert await connection.fetchval(
            """
            SELECT pg_catalog.has_function_privilege(
                'identity_code',
                $1::oid,
                'EXECUTE'
            )
            """,
            audit_function_oid,
        )
    finally:
        await connection.close()
