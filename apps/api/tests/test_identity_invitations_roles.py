"""Exact function-only Identity invitation and Mail v2 LOGIN boundaries."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


def _dsn(environment_key: str, role_name: str) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity invitation LOGIN acceptance requires the owned verifier")
    value = os.environ.get(environment_key)
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    assert parsed.username == role_name
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def _assert_role(
    *,
    environment_key: str,
    role_name: str,
    allowed_schema: str,
    allowed_functions: set[str],
    policy_sql: str,
    policy_value: dict,
    command_sql: str,
    forbidden_sql: tuple[str, ...],
) -> None:
    connection = await asyncpg.connect(_dsn(environment_key, role_name))
    try:
        assert await connection.fetchval("SELECT current_user") == role_name
        functions = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '('
                   || pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
            WHERE left(namespace.nspname,4)='aap_'
              AND function.prokind IN ('f','p')
              AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
            """
        )
        assert {row["signature"] for row in functions} == allowed_functions
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE left(namespace.nspname,4)='aap_'
                  AND namespace.nspname <> $1
                  AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE')
            )
            """,
            allowed_schema,
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
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_proc AS function ON function.pronamespace=namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND left(namespace.nspname,4)<>'aap_'
                  AND function.prokind IN ('f','p') AND function.prosecdef
                  AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
            )
            """
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
        )

        policy = json.loads(await connection.fetchval(policy_sql))
        assert policy == policy_value
        invalid = json.loads(await connection.fetchval(command_sql))
        assert invalid["ok"] is False
        assert invalid["error"]["code"] == "invalid_request"
        for sql in forbidden_sql:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_identity_invitation_login_has_only_three_functions_and_no_table_access():
    await _assert_role(
        environment_key="AAP_MODULE_DATA_IDENTITY_INVITATION_URL",
        role_name="identity_invitation_client",
        allowed_schema="aap_identity_invitations_v1",
        allowed_functions={
            "aap_identity_invitations_v1.apply_invitation_command(jsonb)",
            "aap_identity_invitations_v1.invitation_policy()",
            "aap_identity_invitations_v1.prepare_invitation_command(jsonb)",
        },
        policy_sql="SELECT aap_identity_invitations_v1.invitation_policy()",
        policy_value={
            "schema": "aap-identity-invitations-policy/v1",
            "policy": "aap-identity-invitations/v1",
        },
        command_sql=(
            "SELECT aap_identity_invitations_v1.prepare_invitation_command('{}'::jsonb)"
        ),
        forbidden_sql=(
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.user_invitations LIMIT 1",
            "SELECT * FROM public.identity_invitation_operation_receipts LIMIT 1",
            "SELECT * FROM public.identity_credential_mail_outbox LIMIT 1",
            "SELECT * FROM public.projects LIMIT 1",
            "SELECT * FROM public.project_members LIMIT 1",
            "SELECT * FROM public.task_batches LIMIT 1",
            "SELECT aap_identity_invitations_v1.lock_invitation_mail_scope(gen_random_uuid(),'"
            + "0" * 64
            + "')",
            "SELECT aap_workspace_v1.invitation_projects(ARRAY[]::uuid[])",
            "SELECT aap_identity_mail_v2.mail_policy()",
            "CREATE TEMP TABLE identity_invitation_forbidden(id integer)",
            "SET ROLE identity_code",
        ),
    )


@pytest.mark.asyncio
async def test_mail_v2_login_has_only_four_functions_and_no_table_access():
    await _assert_role(
        environment_key="AAP_MODULE_DATA_IDENTITY_MAIL_V2_URL",
        role_name="identity_mail_v2_client",
        allowed_schema="aap_identity_mail_v2",
        allowed_functions={
            "aap_identity_mail_v2.claim_mail(jsonb)",
            "aap_identity_mail_v2.finish_mail(jsonb)",
            "aap_identity_mail_v2.mail_policy()",
            "aap_identity_mail_v2.renew_mail(jsonb)",
        },
        policy_sql="SELECT aap_identity_mail_v2.mail_policy()",
        policy_value={
            "schema": "aap-identity-mail-policy/v2",
            "policy": "aap-identity-mail/v2",
        },
        command_sql="SELECT aap_identity_mail_v2.claim_mail('{}'::jsonb)",
        forbidden_sql=(
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.user_invitations LIMIT 1",
            "SELECT * FROM public.identity_credential_mail_outbox LIMIT 1",
            "SELECT * FROM public.identity_invitation_operation_receipts LIMIT 1",
            "SELECT aap_identity_invitations_v1._failure('invalid_request')",
            "SELECT aap_identity_invitations_v1.invitation_policy()",
            "SELECT aap_identity_invitations_v1.lock_invitation_mail_scope(gen_random_uuid(),'"
            + "0" * 64
            + "')",
            "CREATE TEMP TABLE identity_mail_v2_forbidden(id integer)",
            "SET ROLE identity_code",
        ),
    )
