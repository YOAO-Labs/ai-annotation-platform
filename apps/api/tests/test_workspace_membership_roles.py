"""The Workspace membership client refuses extra SQL capabilities."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_workspace.contract import MembershipUnavailable
from aap_workspace.postgres import MembershipPostgresData
from app.config import settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace membership LOGIN verifier",
    ),
]


def _membership_dsn(test_engine) -> str:
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


def _owner_engine(test_engine):
    owner = make_url(settings.effective_migration_database_url)
    client = make_url(_membership_dsn(test_engine))
    assert (owner.host, owner.port, owner.database) == (
        client.host,
        client.port,
        client.database,
    )
    return create_async_engine(owner, hide_parameters=True, pool_pre_ping=True)


async def test_workspace_membership_readiness_rejects_raw_table_grant(test_engine):
    dsn = _membership_dsn(test_engine)
    owner_engine = _owner_engine(test_engine)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("GRANT SELECT ON public.users TO workspace_membership_client")
            )
        with pytest.raises(MembershipUnavailable):
            await MembershipPostgresData(dsn).membership_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("REVOKE SELECT ON public.users FROM workspace_membership_client")
            )
        await owner_engine.dispose()
    assert (await MembershipPostgresData(dsn).membership_policy())[
        "schema"
    ] == "aap-workspace-membership-native/v1"


async def test_workspace_membership_readiness_rejects_extra_public_function(
    test_engine,
):
    dsn = _membership_dsn(test_engine)
    owner_engine = _owner_engine(test_engine)
    function_name = f"workspace_membership_probe_{uuid.uuid4().hex}"
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
                    "TO workspace_membership_client"
                )
            )
        with pytest.raises(MembershipUnavailable):
            await MembershipPostgresData(dsn).membership_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()
    assert (await MembershipPostgresData(dsn).membership_policy())[
        "schema"
    ] == "aap-workspace-membership-native/v1"


async def test_workspace_membership_readiness_rejects_database_temp(test_engine):
    dsn = _membership_dsn(test_engine)
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
                    "TO workspace_membership_client"
                )
            )
        with pytest.raises(MembershipUnavailable):
            await MembershipPostgresData(dsn).membership_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"REVOKE TEMP ON DATABASE {quoted_database} "
                    "FROM workspace_membership_client"
                )
            )
        await owner_engine.dispose()
    assert (await MembershipPostgresData(dsn).membership_policy())[
        "schema"
    ] == "aap-workspace-membership-native/v1"
