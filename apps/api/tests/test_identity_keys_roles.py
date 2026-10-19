"""Exact function-only Identity API-key LOGIN against the owned verifier DB."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity API-key LOGIN acceptance requires the owned module-data verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_KEYS_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_key_login_has_only_two_functions_and_no_private_or_ddl_access():
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
            "aap_identity_keys_v1.key_policy()",
            "aap_identity_keys_v1.execute_key_command(jsonb)",
        }
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
                  AND namespace.nspname <> 'aap_identity_keys_v1'
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
                  AND (pg_catalog.has_table_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
                    OR pg_catalog.has_any_column_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'))
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
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind='S'
                  AND (pg_catalog.has_sequence_privilege(current_user,relation.oid,'USAGE')
                    OR pg_catalog.has_sequence_privilege(current_user,relation.oid,'SELECT')
                    OR pg_catalog.has_sequence_privilege(current_user,relation.oid,'UPDATE'))
            )
            """
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.pg_has_role(current_user,'identity_code','USAGE')"
        )

        policy = json.loads(
            await connection.fetchval("SELECT aap_identity_keys_v1.key_policy()")
        )
        assert policy == {
            "schema": "aap-identity-keys-policy/v1",
            "derivation": "aap-identity-api-keys/v1",
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_keys_v1.execute_key_command('{}'::jsonb)"
            )
        )
        assert invalid["error"]["code"] == "invalid_request"

        for sql in (
            "SELECT key_hash FROM public.api_keys LIMIT 1",
            "SELECT * FROM public.identity_key_operation_receipts LIMIT 1",
            "CREATE TEMP TABLE identity_key_forbidden(id integer)",
            "SET ROLE identity_code",
            "SELECT aap_identity_v1.authentication_policy()",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()
