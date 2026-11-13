"""Both module namespace prefixes enforce the same closed capability inventory.

The Workspace owner-transfer LOGIN is the real restricted client. Each case owns
uniquely named schemas, relations, sequences and roles, removes only the objects
it created, and asserts the accepted baseline before mutating anything. A PUBLIC
SECURITY INVOKER function in either namespace must enter the compared set even
though the client holds no schema USAGE for it.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from app.config import settings
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from yoao_owner_data.postgres import FunctionRoleUnavailable, verify_function_only_role

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace owner-transfer LOGIN verifier",
    ),
]

_ROLE = "yoao_workspace_owner_transfer_client"
_SCHEMA = "yoao_workspace_owner_transfer_v1"
_FUNCTIONS = {
    ("owner_transfer_policy", ""),
    ("transfer_project_owner", "jsonb"),
}


def _client_dsn(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL")
    assert value
    parsed = make_url(value)
    assert parsed.host == "127.0.0.1"
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == _ROLE
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    _verify_postgres(parsed)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _owner_dsn(client_dsn: str) -> str:
    client = make_url(client_dsn)
    owner = make_url(settings.effective_migration_database_url)
    assert (owner.host, owner.port, owner.database) == (
        client.host,
        client.port,
        client.database,
    )
    _verify_postgres(owner)
    return owner.set(drivername="postgresql").render_as_string(hide_password=False)


def _probe_schema(prefix: str) -> str:
    return f"{prefix}n5_owner_data_probe_{uuid.uuid4().hex}_v1"


def _probe_object(label: str) -> str:
    return f"n5_owner_data_probe_{label}_{uuid.uuid4().hex}"


@asynccontextmanager
async def _owner(dsn: str):
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
    try:
        yield connection
    finally:
        await connection.close()


async def _assert_inventory_matches(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
    try:
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection, schema=_SCHEMA, functions=_FUNCTIONS, role=_ROLE
        )
    finally:
        await connection.close()


async def _assert_inventory_rejected(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
    try:
        assert await connection.fetchval("SELECT current_user") == _ROLE
        with pytest.raises(FunctionRoleUnavailable):
            await verify_function_only_role(
                connection, schema=_SCHEMA, functions=_FUNCTIONS, role=_ROLE
            )
    finally:
        await connection.close()


@pytest.mark.parametrize("prefix", ["aap_", "yoao_"])
async def test_public_invoker_function_is_rejected_under_either_namespace(
    test_engine, prefix
):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    schema = _probe_schema(prefix)
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await owner.execute(
                f'CREATE FUNCTION "{schema}".probe_public_invoker() '
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _assert_inventory_matches(dsn)


@pytest.mark.parametrize("prefix", ["aap_", "yoao_"])
async def test_security_definer_function_is_rejected_under_either_namespace(
    test_engine, prefix
):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    schema = _probe_schema(prefix)
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await owner.execute(
                f'CREATE FUNCTION "{schema}".probe_security_definer() '
                "RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
            )
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _assert_inventory_matches(dsn)


@pytest.mark.parametrize("prefix", ["aap_", "yoao_"])
async def test_explicit_function_grant_is_rejected_under_either_namespace(
    test_engine, prefix
):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    schema = _probe_schema(prefix)
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await owner.execute(
                f'CREATE FUNCTION "{schema}".probe_granted() '
                "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
            )
            await owner.execute(
                f'REVOKE ALL ON FUNCTION "{schema}".probe_granted() FROM PUBLIC'
            )
            await owner.execute(
                f'GRANT EXECUTE ON FUNCTION "{schema}".probe_granted() TO {_ROLE}'
            )
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _assert_inventory_matches(dsn)


async def test_raw_table_grant_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    table = _probe_object("table")
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE TABLE public."{table}" (id integer PRIMARY KEY)')
        try:
            await owner.execute(f'GRANT SELECT ON TABLE public."{table}" TO {_ROLE}')
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP TABLE IF EXISTS public."{table}"')
    await _assert_inventory_matches(dsn)


async def test_column_grant_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    table = _probe_object("column")
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE TABLE public."{table}" (id integer)')
        try:
            await owner.execute(f'GRANT SELECT (id) ON TABLE public."{table}" TO {_ROLE}')
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP TABLE IF EXISTS public."{table}"')
    await _assert_inventory_matches(dsn)


async def test_sequence_grant_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    sequence = _probe_object("sequence")
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SEQUENCE public."{sequence}"')
        try:
            await owner.execute(
                f'GRANT USAGE ON SEQUENCE public."{sequence}" TO {_ROLE}'
            )
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SEQUENCE IF EXISTS public."{sequence}"')
    await _assert_inventory_matches(dsn)


async def test_schema_create_grant_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    schema = _probe_schema("yoao_")
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await owner.execute(f'GRANT CREATE ON SCHEMA "{schema}" TO {_ROLE}')
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _assert_inventory_matches(dsn)


@pytest.mark.parametrize("prefix", ["aap_", "yoao_"])
async def test_new_namespace_schema_usage_is_still_rejected(test_engine, prefix):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    schema = _probe_schema(prefix)
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await owner.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {_ROLE}')
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _assert_inventory_matches(dsn)


async def test_database_temp_grant_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    database = make_url(dsn).database
    assert database is not None and re.fullmatch(r"[A-Za-z0-9_]+", database)
    async with _owner(_owner_dsn(dsn)) as owner:
        assert (
            await owner.fetchval(
                "SELECT pg_catalog.has_database_privilege("
                "$1::name, current_database(), 'TEMP')",
                _ROLE,
            )
            is False
        )
        await owner.execute(f'GRANT TEMP ON DATABASE "{database}" TO {_ROLE}')
        try:
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(
                f'REVOKE TEMP ON DATABASE "{database}" FROM {_ROLE}'
            )
    await _assert_inventory_matches(dsn)


async def test_role_membership_escalation_is_still_rejected(test_engine):
    dsn = _client_dsn(test_engine)
    await _assert_inventory_matches(dsn)
    role = _probe_object("role")
    async with _owner(_owner_dsn(dsn)) as owner:
        await owner.execute(f'CREATE ROLE "{role}" NOLOGIN NOINHERIT')
        try:
            await owner.execute(f'GRANT "{role}" TO {_ROLE}')
            await _assert_inventory_rejected(dsn)
        finally:
            await owner.execute(f'REVOKE "{role}" FROM {_ROLE}')
            await owner.execute(f'DROP ROLE IF EXISTS "{role}"')
    await _assert_inventory_matches(dsn)
