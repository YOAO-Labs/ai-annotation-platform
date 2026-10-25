"""The Workspace owner-transfer LOGIN has only its two native functions."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_workspace.owner_transfer_contract import OwnerTransferUnavailable
from aap_workspace.owner_transfer_postgres import OwnerTransferPostgresData
from app.config import settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace owner-transfer LOGIN verifier",
    ),
]


def _transfer_dsn(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_owner_transfer_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _owner_engine(test_engine):
    owner = make_url(settings.effective_migration_database_url)
    client = make_url(_transfer_dsn(test_engine))
    assert (owner.host, owner.port, owner.database) == (
        client.host,
        client.port,
        client.database,
    )
    return create_async_engine(owner, hide_parameters=True, pool_pre_ping=True)


async def test_owner_transfer_client_has_exact_functions_and_no_raw_access(test_engine):
    connection = await asyncpg.connect(_transfer_dsn(test_engine))
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "workspace_owner_transfer_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'aap_workspace_owner_transfer_v1'
              AND function.prokind = 'f'
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY function.proname, pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_workspace_owner_transfer_v1.owner_transfer_policy()",
            "aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)",
        ]
        policy = await connection.fetchval(
            "SELECT aap_workspace_owner_transfer_v1.owner_transfer_policy()"
        )
        assert json.loads(policy) == {"schema": "aap-workspace-owner-transfer/v1"}

        invalid = json.loads(
            await connection.fetchval(
                "SELECT aap_workspace_owner_transfer_v1.transfer_project_owner($1::jsonb)",
                '{"schema":"aap-workspace-owner-transfer/v1","extra":true}',
            )
        )
        assert invalid == {
            "schema": "aap-workspace-owner-transfer/v1",
            "ok": False,
            "error": {
                "status_code": 400,
                "detail": "invalid_request",
                "rollback": False,
            },
        }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.projects LIMIT 1",
            "SELECT * FROM public.workspace_owner_transfer_operation_receipts LIMIT 1",
            "SELECT aap_identity_v1.lock_accounts_update('{}'::uuid[], false)",
            "SELECT aap_workspace_membership_v1._has_exact_keys('{}'::jsonb, ARRAY[]::text[])",
            "SELECT aap_workspace_owner_transfer_v1._failure(400, 'null'::jsonb, false)",
            "CREATE TEMP TABLE workspace_owner_transfer_forbidden(id integer)",
            "SET ROLE workspace_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def test_owner_transfer_readiness_rejects_raw_table_grant(test_engine):
    dsn = _transfer_dsn(test_engine)
    owner_engine = _owner_engine(test_engine)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("GRANT SELECT ON public.users TO workspace_owner_transfer_client")
            )
        with pytest.raises(OwnerTransferUnavailable):
            await OwnerTransferPostgresData(dsn).owner_transfer_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "REVOKE SELECT ON public.users FROM workspace_owner_transfer_client"
                )
            )
        await owner_engine.dispose()
    assert await OwnerTransferPostgresData(dsn).owner_transfer_policy() == {
        "schema": "aap-workspace-owner-transfer/v1"
    }


async def test_owner_transfer_readiness_rejects_extra_public_function(test_engine):
    dsn = _transfer_dsn(test_engine)
    owner_engine = _owner_engine(test_engine)
    function_name = f"workspace_owner_transfer_probe_{uuid.uuid4().hex}"
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$"
                )
            )
            await connection.execute(
                text(f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC")
            )
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO workspace_owner_transfer_client"
                )
            )
        with pytest.raises(OwnerTransferUnavailable):
            await OwnerTransferPostgresData(dsn).owner_transfer_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()
    assert await OwnerTransferPostgresData(dsn).owner_transfer_policy() == {
        "schema": "aap-workspace-owner-transfer/v1"
    }


async def test_owner_transfer_readiness_rejects_database_temp(test_engine):
    dsn = _transfer_dsn(test_engine)
    database = make_url(dsn).database
    assert database is not None
    owner_engine = _owner_engine(test_engine)
    quoted_database = owner_engine.sync_engine.dialect.identifier_preparer.quote(
        database
    )
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"GRANT TEMP ON DATABASE {quoted_database} "
                    "TO workspace_owner_transfer_client"
                )
            )
        with pytest.raises(OwnerTransferUnavailable):
            await OwnerTransferPostgresData(dsn).owner_transfer_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"REVOKE TEMP ON DATABASE {quoted_database} "
                    "FROM workspace_owner_transfer_client"
                )
            )
        await owner_engine.dispose()
    assert await OwnerTransferPostgresData(dsn).owner_transfer_policy() == {
        "schema": "aap-workspace-owner-transfer/v1"
    }
