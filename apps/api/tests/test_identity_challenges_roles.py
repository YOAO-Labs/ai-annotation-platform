"""Exact RECOVERY, MAIL and MAIL_SETTINGS LOGIN capabilities."""

from __future__ import annotations

import os
import json

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn(environment_name: str) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity recovery LOGIN acceptance requires the owned module-data verifier"
        )
    value = os.environ.get(environment_name)
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def _assert_only_functions(
    environment_name: str, expected: set[str]
) -> asyncpg.Connection:
    connection = await asyncpg.connect(_dsn(environment_name))
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
    assert {row["signature"] for row in actual} == expected
    assert not await connection.fetchval(
        "SELECT has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
    )
    assert not await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
              AND namespace.nspname <> ALL($1::text[])
              AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE')
        )
        """,
        [
            "aap_identity_recovery_v1"
            if environment_name != "AAP_MODULE_DATA_IDENTITY_MAIL_SETTINGS_URL"
            else "aap_settings_v1"
        ],
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
    return connection


@pytest.mark.asyncio
async def test_recovery_login_has_only_four_functions_and_no_direct_access():
    connection = await _assert_only_functions(
        "AAP_MODULE_DATA_IDENTITY_RECOVERY_URL",
        {
            "aap_identity_recovery_v1.recovery_policy()",
            "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
            "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
            "aap_identity_recovery_v1.read_registration_status(jsonb)",
        },
    )
    try:
        policy = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_recovery_v1.recovery_policy()"
            )
        )
        assert policy == {
            "schema": "aap-identity-recovery-policy/v1",
            "policy": "aap-identity-recovery/v1",
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_recovery_v1.prepare_recovery_command('{}'::jsonb)"
            )
        )
        assert invalid["schema"] == "aap-identity-recovery-native/v1"
        assert invalid["error"]["code"] == "invalid_request"
        for sql in (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.password_reset_tokens LIMIT 1",
            "SELECT * FROM public.email_verification_tokens LIMIT 1",
            "SELECT * FROM public.identity_recovery_operation_receipts LIMIT 1",
            "SELECT * FROM public.identity_credential_mail_outbox LIMIT 1",
            "SELECT aap_identity_recovery_v1.mail_policy()",
            "SELECT aap_settings_v1.mail_delivery_policy('{}'::jsonb)",
            "SELECT aap_audit_v1.record_identity_recovery_event('register_open',NULL,'{}','{}')",
            "CREATE TEMP TABLE recovery_forbidden(id integer)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mail_login_has_only_four_functions_and_cannot_read_settings():
    connection = await _assert_only_functions(
        "AAP_MODULE_DATA_IDENTITY_MAIL_URL",
        {
            "aap_identity_recovery_v1.mail_policy()",
            "aap_identity_recovery_v1.claim_mail(jsonb)",
            "aap_identity_recovery_v1.renew_mail(jsonb)",
            "aap_identity_recovery_v1.finish_mail(jsonb)",
        },
    )
    try:
        policy = json.loads(
            await connection.fetchval("SELECT aap_identity_recovery_v1.mail_policy()")
        )
        assert policy == {
            "schema": "aap-identity-recovery-mail-policy/v1",
            "policy": "aap-identity-recovery-mail/v1",
        }
        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_identity_recovery_v1.claim_mail('{}'::jsonb)"
            )
        )
        assert invalid["schema"] == "aap-identity-recovery-mail-native/v1"
        assert invalid["error"]["code"] == "invalid_request"
        for sql in (
            "SELECT * FROM public.identity_credential_mail_outbox LIMIT 1",
            "SELECT * FROM public.users LIMIT 1",
            "SELECT aap_settings_v1.mail_delivery_policy('{}'::jsonb)",
            "SELECT aap_identity_recovery_v1.prepare_recovery_command('{}'::jsonb)",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mail_settings_login_can_call_only_the_settings_snapshot():
    connection = await _assert_only_functions(
        "AAP_MODULE_DATA_IDENTITY_MAIL_SETTINGS_URL",
        {"aap_settings_v1.mail_delivery_policy(jsonb)"},
    )
    try:
        fallback = {
            "smtp_host": "",
            "smtp_port": 0,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
        }
        policy = json.loads(
            await connection.fetchval(
                "SELECT aap_settings_v1.mail_delivery_policy($1::jsonb)",
                json.dumps(fallback),
            )
        )
        assert set(policy) == set(fallback)
        assert type(policy["smtp_host"]) is str
        assert type(policy["smtp_port"]) is int
        assert type(policy["smtp_password"]) is str
        for sql in (
            "SELECT * FROM public.system_settings LIMIT 1",
            "SELECT * FROM public.users LIMIT 1",
            "SELECT aap_settings_v1.identity_recovery_policy('{}'::jsonb)",
            "SELECT aap_identity_recovery_v1.mail_policy()",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()
