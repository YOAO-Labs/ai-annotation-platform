"""Workspace membership native LOGIN stays behind three closed RPCs."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.asyncio


def _membership_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Workspace membership LOGIN acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_MEMBERSHIP_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_membership_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def test_workspace_membership_client_has_only_native_rpcs_and_no_raw_access(
    test_engine,
):
    connection = await asyncpg.connect(_membership_dsn(test_engine))
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "workspace_membership_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'aap_workspace_membership_v1'
              AND function.prokind = 'f'
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY function.proname, pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_workspace_membership_v1.apply_membership_command(jsonb)",
            "aap_workspace_membership_v1.membership_policy()",
            "aap_workspace_membership_v1.prepare_membership_command(jsonb)",
        ]
        policy = await connection.fetchval(
            "SELECT aap_workspace_membership_v1.membership_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-workspace-membership-native/v1",
            "opaque_text_codec": "json-string-ascii/v1",
            "operations": [
                "add_member",
                "preview_role_change",
                "change_role",
                "remove_member",
            ],
        }

        invalid = (
            "NULL::jsonb",
            "'null'::jsonb",
            "'[]'::jsonb",
            "'{}'::jsonb",
            '\'{"schema":"aap-workspace-membership/v1","extra":true}\'::jsonb',
        )
        expected_failure = {
            "schema": "aap-workspace-membership-native/v1",
            "ok": False,
            "error": {
                "status_code": 400,
                "detail": "invalid_request",
                "headers": None,
                "rollback": False,
            },
        }
        for command in invalid:
            reply = await connection.fetchval(
                "SELECT aap_workspace_membership_v1.prepare_membership_command("
                + command
                + ")"
            )
            assert json.loads(reply) == expected_failure
            apply_reply = await connection.fetchval(
                "SELECT aap_workspace_membership_v1.apply_membership_command("
                + command
                + ")"
            )
            assert json.loads(apply_reply)["ok"] is False

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.projects LIMIT 1",
            "SELECT * FROM public.project_members LIMIT 1",
            "SELECT * FROM public.workspace_membership_operation_receipts LIMIT 1",
            "SELECT aap_identity_v1.read_workspace_membership_accounts('{}'::uuid[])",
            "SELECT aap_labeling_v1.read_membership_work(NULL,NULL,NULL,NULL)",
            "SELECT aap_labeling_v1._membership_python_repr('null'::jsonb)",
            "SELECT aap_audit_v1.record_workspace_membership_event(NULL,NULL,NULL,NULL,NULL)",
            "SELECT aap_workspace_membership_v1._failure(400,'null'::jsonb,NULL,false)",
            "CREATE TEMP TABLE workspace_membership_forbidden(id integer)",
            "SET ROLE workspace_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()
