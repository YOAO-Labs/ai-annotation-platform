"""Real grant-script checks for both module namespace prefixes.

Every test applies a checked-in installer from ``infra/docker`` through ``psql``
against the disposable PostgreSQL database owned by the module-data verifier and
asserts effective privileges on real LOGIN roles. The probes create only
throwaway schemas and per-test LOGIN roles, and remove them afterwards, so the
rest of the suite sees the verifier's post-install state.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]
GRANTS = ROOT / "infra/docker"
CONTAINER_ENV = "AAP_MODULE_DATA_PG_CONTAINER_ID"
CONTAINER_PATTERN = r"[0-9a-f]{64}"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.getenv(CONTAINER_ENV),
        reason="requires the owned disposable module-data PostgreSQL verifier",
    ),
]


def _container() -> str:
    value = os.environ.get(CONTAINER_ENV, "")
    if re.fullmatch(CONTAINER_PATTERN, value) is None:
        pytest.fail("the module-data verifier supplied an invalid PostgreSQL container")
    return value


def _command(*, username, password, database):
    command = ["docker", "exec", "-i"]
    if password is not None:
        command.extend(("-e", f"PGPASSWORD={password}"))
    command.extend([_container(), "psql", "-X", "-q", "-A", "-t"])
    if password is not None:
        command.extend(("-h", "127.0.0.1", "-p", "5432"))
    command.extend(("-U", username, "-d", database, "-v", "ON_ERROR_STOP=1"))
    return command


async def _psql(
    test_engine,
    *,
    sql: str,
    variables: dict[str, str] | None = None,
    username: str | None = None,
    password: str | None = None,
):
    command = _command(
        username=username or test_engine.url.username,
        password=password,
        database=test_engine.url.database,
    )
    for name, value in (variables or {}).items():
        command.extend(("-v", f"{name}={value}"))
    return await asyncio.to_thread(
        subprocess.run,
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _apply(test_engine, script: str, variables: dict[str, str]):
    variables = {
        "expected_database": test_engine.url.database,
        "migration_role": test_engine.url.username,
        **variables,
    }
    return await _psql(
        test_engine, sql=(GRANTS / script).read_text(), variables=variables
    )


async def _execute(test_engine, statements: list[str]) -> None:
    async with test_engine.begin() as db:
        for statement in statements:
            await db.execute(text(statement))


async def _row(test_engine, sql: str, **params):
    async with test_engine.connect() as db:
        return (await db.execute(text(sql), params)).mappings().one()


async def _scalar(test_engine, sql: str, **params):
    async with test_engine.connect() as db:
        return await db.scalar(text(sql), params)


@asynccontextmanager
async def _login(test_engine):
    role = "n5s_" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    assert re.fullmatch(r"n5s_[0-9a-f]{12}", role)
    await _execute(
        test_engine,
        [
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        ],
    )
    try:
        yield role, password
    finally:
        await _execute(test_engine, [f"DROP OWNED BY {role}", f"DROP ROLE {role}"])


@asynccontextmanager
async def _schema(test_engine, name: str):
    await _execute(test_engine, [f"CREATE SCHEMA {name}"])
    try:
        yield name
    finally:
        await _execute(test_engine, [f"DROP SCHEMA IF EXISTS {name} CASCADE"])


async def test_catalog_grants_revoke_new_namespace_storage_and_function_excess(
    test_engine,
):
    probe = "yoao_n5s_catalog_probe_v1"
    neighbor = "n5s_catalog_neighbor"
    async with _login(test_engine) as (role, password):
        async with _schema(test_engine, probe):
            async with _schema(test_engine, neighbor):
                await _execute(
                    test_engine,
                    [
                        f"CREATE TABLE {probe}.probe_rows (id integer, name text)",
                        f"CREATE SEQUENCE {probe}.probe_seq",
                        f"CREATE FUNCTION {probe}.probe_read() RETURNS integer "
                        "LANGUAGE sql AS 'SELECT 1'",
                        f"REVOKE ALL ON FUNCTION {probe}.probe_read() FROM PUBLIC",
                        f"GRANT USAGE, CREATE ON SCHEMA {probe} TO {role}",
                        f"GRANT SELECT, INSERT ON {probe}.probe_rows TO {role}",
                        f"GRANT USAGE ON SEQUENCE {probe}.probe_seq TO {role}",
                        f"GRANT EXECUTE ON FUNCTION {probe}.probe_read() TO {role}",
                        f"GRANT USAGE ON SCHEMA {neighbor} TO {role}",
                    ],
                )
                before = await _row(
                    test_engine,
                    """
                    SELECT
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'USAGE') AS usage,
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'CREATE') AS create,
                        pg_catalog.has_table_privilege(
                            :role, :rows, 'SELECT,INSERT') AS table_access,
                        pg_catalog.has_any_column_privilege(
                            :role, :rows, 'SELECT') AS column_access,
                        pg_catalog.has_sequence_privilege(
                            :role, :sequence, 'USAGE') AS sequence_access,
                        pg_catalog.has_function_privilege(
                            :role, :function, 'EXECUTE') AS function_access
                    """,
                    role=role,
                    probe=probe,
                    rows=f"{probe}.probe_rows",
                    sequence=f"{probe}.probe_seq",
                    function=f"{probe}.probe_read()",
                )
                assert dict(before) == {
                    "usage": True,
                    "create": True,
                    "table_access": True,
                    "column_access": True,
                    "sequence_access": True,
                    "function_access": True,
                }
                result = await _apply(
                    test_engine,
                    "assets-catalog-grants.sql",
                    {"assets_owner": "assets_code", "asset_catalog_role": role},
                )
                assert result.returncode == 0, result.stderr
                after = await _row(
                    test_engine,
                    """
                    SELECT
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'USAGE') AS usage,
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'CREATE') AS create,
                        pg_catalog.has_table_privilege(
                            :role, :rows, 'SELECT,INSERT') AS table_access,
                        pg_catalog.has_any_column_privilege(
                            :role, :rows, 'SELECT') AS column_access,
                        pg_catalog.has_sequence_privilege(
                            :role, :sequence, 'USAGE') AS sequence_access,
                        pg_catalog.has_function_privilege(
                            :role, :function, 'EXECUTE') AS function_access,
                        pg_catalog.has_schema_privilege(
                            :role, :neighbor, 'USAGE') AS neighbor_usage,
                        pg_catalog.has_schema_privilege(
                            :role, 'aap_assets_v1', 'USAGE') AS approved_usage,
                        pg_catalog.has_function_privilege(
                            :role,
                            'aap_assets_v1.list_catalog_datasets'
                            '(uuid,text,text,boolean,integer,bigint)',
                            'EXECUTE') AS approved_function
                    """,
                    role=role,
                    probe=probe,
                    rows=f"{probe}.probe_rows",
                    sequence=f"{probe}.probe_seq",
                    function=f"{probe}.probe_read()",
                    neighbor=neighbor,
                )
                assert dict(after) == {
                    "usage": False,
                    "create": False,
                    "table_access": False,
                    "column_access": False,
                    "sequence_access": False,
                    "function_access": False,
                    "neighbor_usage": True,
                    "approved_usage": True,
                    "approved_function": True,
                }
                denied = await _psql(
                    test_engine,
                    sql=f"SELECT count(*) FROM {probe}.probe_rows;",
                    username=role,
                    password=password,
                )
                assert denied.returncode != 0
                assert "permission denied" in denied.stderr


async def test_audit_read_denies_new_namespace_schema_usage_atomically(test_engine):
    probe = "yoao_n5s_audit_probe_v1"
    async with _login(test_engine) as (role, _password):
        variables = {
            "audit_owner": "audit_code",
            "api_role": "legacy_runtime",
            "read_client": role,
        }
        first = await _apply(test_engine, "audit-read-grants.sql", variables)
        assert first.returncode == 0, first.stderr
        async with _schema(test_engine, probe):
            await _execute(
                test_engine,
                [
                    f"GRANT USAGE ON SCHEMA {probe} TO {role}",
                    # A deficit the failing re-application would repair only on commit.
                    f"REVOKE SELECT ON aap_audit_v1.audit_events FROM {role}",
                ],
            )
            state = await _row(
                test_engine,
                """
                SELECT
                    pg_catalog.has_schema_privilege(
                        :role, :probe, 'USAGE') AS probe_usage,
                    pg_catalog.has_schema_privilege(
                        :role, 'aap_audit_v1', 'USAGE') AS audit_usage,
                    pg_catalog.has_table_privilege(
                        :role, 'aap_audit_v1.audit_events', 'SELECT') AS audit_read
                """,
                role=role,
                probe=probe,
            )
            assert dict(state) == {
                "probe_usage": True,
                "audit_usage": True,
                "audit_read": False,
            }
            second = await _apply(test_engine, "audit-read-grants.sql", variables)
            assert second.returncode != 0
            assert (
                "Audit read client has a foreign module schema grant" in second.stderr
            )
            after = await _row(
                test_engine,
                """
                SELECT
                    pg_catalog.has_schema_privilege(
                        :role, :probe, 'USAGE') AS probe_usage,
                    pg_catalog.has_schema_privilege(
                        :role, 'aap_audit_v1', 'USAGE') AS audit_usage,
                    pg_catalog.has_table_privilege(
                        :role, 'aap_audit_v1.audit_events', 'SELECT') AS audit_read
                """,
                role=role,
                probe=probe,
            )
            # The denial aborted the whole installer: its GRANT never committed and
            # its revokes never committed either.
            assert dict(after) == {
                "probe_usage": True,
                "audit_usage": True,
                "audit_read": False,
            }


async def test_identity_briefs_inventory_detects_public_invoker_new_namespace_function(
    test_engine,
):
    role = "identity_briefs_client"
    probe = "yoao_n5s_briefs_probe_v1"
    function = f"{probe}.probe_policy()"
    async with _schema(test_engine, probe):
        await _execute(
            test_engine,
            [
                f"CREATE FUNCTION {probe}.probe_policy() RETURNS jsonb "
                "LANGUAGE sql AS 'SELECT ''{}''::jsonb'",
                f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC",
            ],
        )
        shape = await _row(
            test_engine,
            """
            SELECT
                NOT function.prosecdef AS invoker,
                pg_catalog.has_function_privilege(
                    :role, function.oid, 'EXECUTE') AS executable,
                EXISTS (
                    SELECT 1 FROM pg_catalog.aclexplode(function.proacl) AS acl
                    WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                ) AS public_execute
            FROM pg_catalog.pg_proc AS function
            WHERE function.oid = pg_catalog.to_regprocedure(:function)
            """,
            role=role,
            function=function,
        )
        # The gap this guards: neither SECURITY DEFINER nor an explicit role ACL,
        # only the default PUBLIC EXECUTE grant.
        assert dict(shape) == {
            "invoker": True,
            "executable": True,
            "public_execute": True,
        }
        result = await _apply(
            test_engine,
            "identity-briefs-grants.sql",
            {
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "identity_briefs_role": role,
            },
        )
        assert result.returncode != 0
        assert "Identity briefs LOGIN has unexpected functions" in result.stderr
        explicit = await _scalar(
            test_engine,
            """
            SELECT count(*)
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                function.proacl) AS acl
            WHERE function.oid = pg_catalog.to_regprocedure(:function)
              AND acl.grantee = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role)
              AND acl.privilege_type = 'EXECUTE'
            """,
            role=role,
            function=function,
        )
        # The rejected installer rolled back its revoke of this explicit grant.
        assert explicit == 1
        approved = await _row(
            test_engine,
            """
            SELECT
                pg_catalog.has_function_privilege(
                    :role,
                    'aap_identity_briefs_v1.briefs_policy()',
                    'EXECUTE') AS policy,
                pg_catalog.has_function_privilege(
                    :role,
                    'aap_identity_briefs_v1.execute_briefs_command(jsonb)',
                    'EXECUTE') AS command
            """,
            role=role,
        )
        assert dict(approved) == {"policy": True, "command": True}


async def test_identity_consumer_scopes_new_namespace_revocation(test_engine):
    identity_probe = "yoao_identity_n5s_probe_v1"
    workspace_probe = "yoao_workspace_n5s_probe_v1"
    function = f"{identity_probe}.probe_read()"
    async with _login(test_engine) as (role, _password):
        async with _schema(test_engine, identity_probe):
            async with _schema(test_engine, workspace_probe):
                await _execute(
                    test_engine,
                    [
                        f"CREATE TABLE {identity_probe}.probe_rows "
                        "(id integer, name text)",
                        f"CREATE FUNCTION {function} RETURNS integer "
                        "LANGUAGE sql AS 'SELECT 1'",
                        f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC",
                        f"GRANT USAGE ON SCHEMA {identity_probe} TO {role}",
                        f"GRANT SELECT ON {identity_probe}.probe_rows TO {role}",
                        f"GRANT SELECT (id) ON {identity_probe}.probe_rows TO {role}",
                        f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                        f"CREATE TABLE {workspace_probe}.probe_rows (id integer)",
                        f"GRANT USAGE ON SCHEMA {workspace_probe} TO {role}",
                        f"GRANT SELECT ON {workspace_probe}.probe_rows TO {role}",
                    ],
                )
                result = await _apply(
                    test_engine,
                    "identity-consumer-grants.sql",
                    {"identity_owner": "identity_code", "consumer_role": role},
                )
                assert result.returncode == 0, result.stderr
                identity_state = await _row(
                    test_engine,
                    """
                    SELECT
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'USAGE') AS usage,
                        pg_catalog.has_table_privilege(
                            :role, :rows, 'SELECT') AS table_read,
                        pg_catalog.has_any_column_privilege(
                            :role, :rows, 'SELECT') AS column_read,
                        pg_catalog.has_function_privilege(
                            :role, :function, 'EXECUTE') AS function_access
                    """,
                    role=role,
                    probe=identity_probe,
                    rows=f"{identity_probe}.probe_rows",
                    function=function,
                )
                assert dict(identity_state) == {
                    "usage": False,
                    "table_read": False,
                    "column_read": False,
                    "function_access": False,
                }
                workspace_state = await _row(
                    test_engine,
                    """
                    SELECT
                        pg_catalog.has_schema_privilege(
                            :role, :probe, 'USAGE') AS usage,
                        pg_catalog.has_table_privilege(
                            :role, :rows, 'SELECT') AS table_read
                    """,
                    role=role,
                    probe=workspace_probe,
                    rows=f"{workspace_probe}.probe_rows",
                )
                # Identity-only recognition must not widen to other new-prefix
                # namespaces.
                assert dict(workspace_state) == {
                    "usage": True,
                    "table_read": True,
                }
                approved = await _row(
                    test_engine,
                    """
                    SELECT
                        pg_catalog.has_function_privilege(
                            :role,
                            'aap_identity_v1.read_actor_facts(uuid)',
                            'EXECUTE') AS read_actor,
                        (
                            SELECT count(*)
                            FROM pg_catalog.pg_proc AS function
                            JOIN pg_catalog.pg_namespace AS namespace
                              ON namespace.oid = function.pronamespace
                            WHERE namespace.nspname ~
                                  '^(aap|yoao)_identity(_[a-z0-9]+)*_v[0-9]+$'
                              AND function.prokind IN ('f', 'p')
                              AND pg_catalog.has_function_privilege(
                                  :role, function.oid, 'EXECUTE')
                        ) AS identity_functions
                    """,
                    role=role,
                )
                assert dict(approved) == {
                    "read_actor": True,
                    "identity_functions": 8,
                }


async def test_identity_deactivation_allowlist_classifies_new_namespace_as_module(
    test_engine,
):
    role = "identity_deactivation_client"
    probe = "yoao_n5s_deactivation_probe_v1"
    function = f"{probe}.probe_policy()"
    async with _schema(test_engine, probe):
        await _execute(
            test_engine,
            [
                f"CREATE FUNCTION {probe}.probe_policy() RETURNS jsonb "
                "LANGUAGE sql AS 'SELECT ''{}''::jsonb'",
                f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC",
            ],
        )
        result = await _apply(
            test_engine,
            "identity-deactivation-grants.sql",
            {
                "identity_owner": "identity_code",
                "audit_owner": "audit_code",
                "notifications_owner": "notifications_code",
                "identity_deactivation_role": role,
                "identity_deactivation_operator_role": (
                    "identity_deactivation_operator"
                ),
                "notification_publish_role": "notification_publish_client",
            },
        )
        assert result.returncode != 0
        # The new prefix belongs to the module allowlist, not to the fallback
        # SECURITY DEFINER classifier.
        assert (
            "Unexpected function allowlist for identity_deactivation_client"
            in result.stderr
        )
        assert "Unexpected non-module SECURITY DEFINER capability" not in result.stderr
        explicit = await _scalar(
            test_engine,
            """
            SELECT count(*)
            FROM pg_catalog.pg_proc AS function
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                function.proacl) AS acl
            WHERE function.oid = pg_catalog.to_regprocedure(:function)
              AND acl.grantee = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role)
              AND acl.privilege_type = 'EXECUTE'
            """,
            role=role,
            function=function,
        )
        assert explicit == 1


async def test_development_runtime_loses_new_namespace_workspace_storage(test_engine):
    probe = "yoao_workspace_n5s_probe_v1"
    async with _login(test_engine) as (role, password):
        async with _schema(test_engine, probe):
            await _execute(
                test_engine,
                [
                    f"CREATE TABLE {probe}.probe_projects "
                    "(id uuid, owner_id uuid, name text)",
                    f"CREATE SEQUENCE {probe}.probe_display_seq",
                    f"GRANT USAGE, CREATE ON SCHEMA {probe} TO {role}",
                    f"GRANT SELECT, INSERT, UPDATE ON {probe}.probe_projects TO {role}",
                    f"GRANT SELECT (name) ON {probe}.probe_projects TO {role}",
                    f"GRANT USAGE ON SEQUENCE {probe}.probe_display_seq TO {role}",
                ],
            )
            before = await _row(
                test_engine,
                """
                SELECT
                    pg_catalog.has_schema_privilege(:role, :probe, 'CREATE') AS create,
                    pg_catalog.has_table_privilege(
                        :role, :rows, 'SELECT,INSERT,UPDATE') AS table_access,
                    pg_catalog.has_any_column_privilege(
                        :role, :rows, 'SELECT') AS column_access,
                    pg_catalog.has_sequence_privilege(
                        :role, :sequence, 'USAGE') AS sequence_access
                """,
                role=role,
                probe=probe,
                rows=f"{probe}.probe_projects",
                sequence=f"{probe}.probe_display_seq",
            )
            assert dict(before) == {
                "create": True,
                "table_access": True,
                "column_access": True,
                "sequence_access": True,
            }
            result = await _apply(
                test_engine,
                "lan-production-grants.sql",
                {
                    "application_role": role,
                    "asset_cleanup_role": "asset_cleanup_client",
                },
            )
            assert result.returncode == 0, result.stderr
            after = await _row(
                test_engine,
                """
                SELECT
                    pg_catalog.has_schema_privilege(:role, :probe, 'CREATE') AS create,
                    pg_catalog.has_table_privilege(
                        :role, :rows, 'SELECT,INSERT,UPDATE') AS table_access,
                    pg_catalog.has_any_column_privilege(
                        :role, :rows, 'SELECT') AS column_access,
                    pg_catalog.has_sequence_privilege(
                        :role, :sequence, 'USAGE') AS sequence_access
                """,
                role=role,
                probe=probe,
                rows=f"{probe}.probe_projects",
                sequence=f"{probe}.probe_display_seq",
            )
            assert dict(after) == {
                "create": False,
                "table_access": False,
                "column_access": False,
                "sequence_access": False,
            }
            denied = await _psql(
                test_engine,
                sql=f"SELECT count(*) FROM {probe}.probe_projects;",
                username=role,
                password=password,
            )
            assert denied.returncode != 0
            assert "permission denied" in denied.stderr
