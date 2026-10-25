"""Workspace configuration LOGIN exposes only its two closed NativeSQL calls."""

from __future__ import annotations

import json
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.asyncio


def _configuration_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Workspace configuration LOGIN acceptance requires the owned verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_CONFIGURATION_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_configuration_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def test_workspace_configuration_client_has_only_the_policy_and_step_rpcs(
    test_engine,
):
    connection = await asyncpg.connect(_configuration_dsn(test_engine))
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "workspace_configuration_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'aap_workspace_configuration_v1'
              AND function.prokind = 'f'
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY function.proname, pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_workspace_configuration_v1.configuration_policy()",
            "aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])",
        ]
        policy = await connection.fetchval(
            "SELECT aap_workspace_configuration_v1.configuration_policy()"
        )
        assert json.loads(policy) == {"schema": "aap-workspace-configuration/v1"}

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.projects LIMIT 1",
            "SELECT * FROM public.project_members LIMIT 1",
            "SELECT * FROM public.project_templates LIMIT 1",
            "SELECT * FROM public.workspace_configuration_operation_states LIMIT 1",
            "SELECT * FROM public.workspace_configuration_operation_attempts LIMIT 1",
            "SELECT * FROM public.workspace_configuration_operation_receipts LIMIT 1",
            "SELECT aap_workspace_configuration_v1._has_exact_keys('{}'::jsonb, ARRAY[]::text[])",
            "SELECT aap_workspace_configuration_v1._configuration_actor(NULL, false, NULL)",
            "SELECT aap_identity_v1.lock_accounts_share('{}'::uuid[], true)",
            "SELECT aap_assets_v1.workspace_configuration_dataset_rows(NULL)",
            "SELECT aap_labeling_v1.workspace_configuration_task_count(NULL)",
            "SELECT aap_inference_v1.workspace_configuration_backend(NULL)",
            "CREATE TEMP TABLE workspace_configuration_forbidden(id integer)",
            "SET ROLE workspace_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()
