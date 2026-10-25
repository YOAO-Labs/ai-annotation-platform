"""Exact function-only Identity deactivation and Notifications publisher roles."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url


_CLIENTS = (
    (
        "AAP_MODULE_DATA_IDENTITY_DEACTIVATION_URL",
        "identity_deactivation_client",
        "aap_identity_deactivation_v1",
        {
            "aap_identity_deactivation_v1.deactivation_policy()",
            "aap_identity_deactivation_v1.execute_deactivation_command(jsonb)",
        },
    ),
    (
        "AAP_MODULE_DATA_IDENTITY_DEACTIVATION_OPERATOR_URL",
        "identity_deactivation_operator",
        "aap_identity_deactivation_v1",
        {
            "aap_identity_deactivation_v1.due_policy()",
            "aap_identity_deactivation_v1.process_due_deactivations(jsonb)",
        },
    ),
    (
        "AAP_MODULE_DATA_IDENTITY_NOTIFICATION_PUBLISH_URL",
        "notification_publish_client",
        "aap_notifications_v1",
        {
            "aap_notifications_v1.identity_delivery_policy()",
            "aap_notifications_v1.claim_identity_deliveries(jsonb)",
            "aap_notifications_v1.finish_identity_delivery(jsonb)",
        },
    ),
)


def _dsn(environment_key: str) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity deactivation LOGIN acceptance requires the owned verifier"
        )
    value = os.environ.get(environment_key)
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment_key", "role_name", "allowed_schema", "allowed_functions"), _CLIENTS
)
async def test_deactivation_and_publisher_logins_have_exact_function_surfaces(
    environment_key: str,
    role_name: str,
    allowed_schema: str,
    allowed_functions: set[str],
):
    connection = await asyncpg.connect(_dsn(environment_key))
    try:
        assert await connection.fetchval("SELECT current_user") == role_name
        actual = await connection.fetch(
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
        assert {row["signature"] for row in actual} == allowed_functions
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
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
                  AND function.prokind IN ('f','p')
                  AND function.prosecdef
                  AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
            )
            """
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
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

        if role_name == "identity_deactivation_client":
            assert json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.deactivation_policy()"
                )
            ) == {
                "schema": "aap-identity-deactivation-policy/v1",
                "policy": "aap-identity-deactivation/v1",
            }
            invalid = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.execute_deactivation_command('{}'::jsonb)"
                )
            )
            assert invalid["error"]["code"] == "invalid_request"
        elif role_name == "identity_deactivation_operator":
            assert json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.due_policy()"
                )
            ) == {
                "schema": "aap-identity-deactivation-due-policy/v1",
                "policy": "aap-identity-deactivation-due/v1",
            }
            invalid = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.process_due_deactivations('{}'::jsonb)"
                )
            )
            assert invalid["error"]["code"] == "invalid_request"
        else:
            assert json.loads(
                await connection.fetchval(
                    "SELECT aap_notifications_v1.identity_delivery_policy()"
                )
            ) == {
                "schema": "aap-notifications-delivery-policy/v1",
                "policy": "aap-notifications-delivery/v1",
            }
            invalid = json.loads(
                await connection.fetchval(
                    "SELECT aap_notifications_v1.claim_identity_deliveries('{}'::jsonb)"
                )
            )
            assert invalid["error"]["code"] == "invalid_request"

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_deactivation_operation_receipts LIMIT 1",
            "SELECT * FROM public.identity_deactivation_due_receipts LIMIT 1",
            "SELECT * FROM public.identity_deactivation_requests LIMIT 1",
            "SELECT * FROM public.notification_delivery_outbox LIMIT 1",
            "SELECT * FROM public.notifications LIMIT 1",
            "SELECT aap_identity_v1.lock_super_admin_invariant()",
            "CREATE TEMP TABLE deactivation_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for sql in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
    finally:
        await connection.close()
