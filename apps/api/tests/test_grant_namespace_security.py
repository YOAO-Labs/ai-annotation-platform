"""Real grant-script checks for both module namespace prefixes.

Every test applies a checked-in installer from ``infra/docker`` through ``psql``
against the disposable PostgreSQL database owned by the module-data verifier.
Owner DDL and owner ``psql`` use the migration connection, which is proven to
match ``test_engine`` and to live in the verified acceptance container. Probes
use per-run UUID schema names and per-test LOGIN roles; only objects created by
the test are dropped afterwards.
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
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

ROOT = Path(__file__).resolve().parents[3]
GRANTS = ROOT / "infra/docker"
ROLE_GATE = "AAP_MODULE_DATA_ROLE_ACCEPTANCE"
ROLE_PATTERN = r"n5s_[a-z0-9_]{1,48}"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv(ROLE_GATE) != "1",
        reason="requires the owned disposable module-data PostgreSQL verifier",
    ),
]


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


def _assert_guard_rejected(result: subprocess.CompletedProcess) -> None:
    """A rejecting installer must fail through its own PL/pgSQL guard."""
    assert result.returncode != 0, result.stderr
    # psql VERBOSITY=verbose prefixes the SQLSTATE; P0001 is RAISE EXCEPTION.
    assert "P0001" in result.stderr, result.stderr


def _assert_insufficient_privilege(result: subprocess.CompletedProcess) -> None:
    assert result.returncode != 0, result.stderr
    assert "42501" in result.stderr, result.stderr


@pytest_asyncio.fixture
async def migration_url(test_engine) -> URL:
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    if not os.environ.get("MIGRATION_DATABASE_URL"):
        pytest.fail("the verifier did not provide the migration owner connection")
    url = make_url(settings.effective_migration_database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("the migration connection is not PostgreSQL")
    _verify_postgres(url)
    observed = (url.host, url.port, url.database)
    expected = (test_engine.url.host, test_engine.url.port, test_engine.url.database)
    if (
        url.host != "127.0.0.1"
        or observed != expected
        or not url.username
        or not url.password
    ):
        pytest.fail(
            "the migration connection does not match the verified test database"
        )
    return url


@pytest_asyncio.fixture
async def owner_engine(migration_url: URL):
    engine = create_async_engine(
        migration_url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _psql_command(*, username: str, database: str):
    command = ["docker", "exec", "-i", "-e", "PGPASSWORD"]
    command.extend(
        [
            os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            username,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "VERBOSITY=verbose",
        ]
    )
    return command


async def _psql(
    url: URL,
    *,
    sql: str,
    variables: dict[str, str] | None = None,
    username: str,
    password: str,
):
    command = _psql_command(username=username, database=url.database)
    for name, value in (variables or {}).items():
        command.extend(("-v", f"{name}={value}"))
    return await asyncio.to_thread(
        subprocess.run,
        command,
        env={**os.environ, "PGPASSWORD": password},
        input=sql,
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _apply(migration_url: URL, script: str, variables: dict[str, str]):
    variables = {
        "expected_database": migration_url.database,
        "migration_role": migration_url.username,
        **variables,
    }
    return await _psql(
        migration_url,
        sql=(GRANTS / script).read_text(),
        variables=variables,
        username=migration_url.username,
        password=migration_url.password,
    )


async def _execute(engine, statements: list[str]) -> None:
    async with engine.begin() as db:
        for statement in statements:
            await db.execute(text(statement))


async def _row(engine, sql: str, **params):
    async with engine.connect() as db:
        return (await db.execute(text(sql), params)).mappings().one()


async def _scalar(engine, sql: str, **params):
    async with engine.connect() as db:
        return await db.scalar(text(sql), params)


@asynccontextmanager
async def _login(engine, prefix: str):
    role = f"n5s_{prefix}_{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    assert re.fullmatch(ROLE_PATTERN, role)
    # The deployment profile assigns the shared user-performance MV to the
    # application role. Preserve its pre-existing owner so cleaning up this
    # per-test LOGIN cannot drop an object installed by the migrations.
    maintenance_owner = await _scalar(
        engine,
        "SELECT pg_catalog.pg_get_userbyid(relation.relowner) "
        "FROM pg_catalog.pg_class AS relation "
        "WHERE relation.oid=pg_catalog.to_regclass('public.mv_user_perf_daily')",
    )
    await _execute(
        engine,
        [
            f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        ],
    )
    try:
        yield role, password
    finally:
        statements = []
        if maintenance_owner is not None:
            owner = engine.dialect.identifier_preparer.quote_identifier(
                maintenance_owner
            )
            statements.append(
                f"ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO {owner}"
            )
        statements.extend((f"DROP OWNED BY {role}", f"DROP ROLE {role}"))
        await _execute(engine, statements)


@asynccontextmanager
async def _schema(engine, name: str):
    await _execute(engine, [f"CREATE SCHEMA {name}"])
    try:
        yield name
    finally:
        await _execute(engine, [f"DROP SCHEMA {name} CASCADE"])


async def test_catalog_classifier_revokes_new_namespace_and_rejects_column_acl(
    owner_engine, migration_url
):
    probe = f"yoao_n5s_{_suffix()}_v1"
    neighbor = f"n5s_neighbor_{_suffix()}"
    async with _login(owner_engine, "catalog") as (role, password):
        async with _schema(owner_engine, probe):
            async with _schema(owner_engine, neighbor):
                await _execute(
                    owner_engine,
                    [
                        f"CREATE TABLE {probe}.probe_rows (id integer, name text)",
                        f"CREATE SEQUENCE {probe}.probe_seq",
                        f"CREATE FUNCTION {probe}.probe_read() RETURNS integer "
                        "LANGUAGE sql AS 'SELECT 1'",
                        f"REVOKE ALL ON FUNCTION {probe}.probe_read() FROM PUBLIC",
                        f"GRANT USAGE, CREATE ON SCHEMA {probe} TO {role}",
                        f"GRANT SELECT, INSERT ON {probe}.probe_rows TO {role}",
                        # An independent column ACL inside the classified
                        # namespace: the installer must revoke it, not reject it.
                        f"GRANT UPDATE (name) ON {probe}.probe_rows TO {role}",
                        f"GRANT USAGE ON SEQUENCE {probe}.probe_seq TO {role}",
                        f"GRANT EXECUTE ON FUNCTION {probe}.probe_read() TO {role}",
                        f"CREATE TABLE {neighbor}.probe_rows (id integer, name text)",
                        f"GRANT USAGE ON SCHEMA {neighbor} TO {role}",
                    ],
                )
                variables = {
                    "assets_owner": "assets_code",
                    "asset_catalog_role": role,
                }
                before = await _catalog_state(owner_engine, role, probe, neighbor)
                assert dict(before) == {
                    "usage": True,
                    "create": True,
                    "table_access": True,
                    "column_update": True,
                    "sequence_access": True,
                    "function_access": True,
                    "neighbor_usage": True,
                    "neighbor_table_access": False,
                    "neighbor_column_access": False,
                    "approved_usage": False,
                    "approved_function": False,
                }
                baseline = await _apply(
                    migration_url, "assets-catalog-grants.sql", variables
                )
                assert baseline.returncode == 0, baseline.stderr
                revoked = await _catalog_state(owner_engine, role, probe, neighbor)
                assert dict(revoked) == {
                    "usage": False,
                    "create": False,
                    "table_access": False,
                    "column_update": False,
                    "sequence_access": False,
                    "function_access": False,
                    "neighbor_usage": True,
                    "neighbor_table_access": False,
                    "neighbor_column_access": False,
                    "approved_usage": True,
                    "approved_function": True,
                }
                # The classifier's table revoke drops the probe column ACL too,
                # per SQL spec. A table outside the classifier keeps its ACL,
                # and only the installer's own column inventory can reject it.
                await _execute(
                    owner_engine,
                    [f"GRANT UPDATE (name) ON {neighbor}.probe_rows TO {role}"],
                )
                injected = await _catalog_state(owner_engine, role, probe, neighbor)
                assert injected["neighbor_column_access"] is True
                assert injected["neighbor_table_access"] is False
                rejected = await _apply(
                    migration_url, "assets-catalog-grants.sql", variables
                )
                _assert_guard_rejected(rejected)
                rolled_back = await _catalog_state(owner_engine, role, probe, neighbor)
                assert dict(rolled_back) == {
                    **dict(revoked),
                    "neighbor_column_access": True,
                }
                await _execute(
                    owner_engine,
                    [f"REVOKE UPDATE (name) ON {neighbor}.probe_rows FROM {role}"],
                )
                recovered = await _apply(
                    migration_url, "assets-catalog-grants.sql", variables
                )
                assert recovered.returncode == 0, recovered.stderr
                final = await _catalog_state(owner_engine, role, probe, neighbor)
                assert dict(final) == dict(revoked)
                # The install grants CONNECT; the login can open a session but
                # cannot resolve or read the private module namespace.
                connected = await _psql(
                    migration_url,
                    sql="SELECT 1;",
                    username=role,
                    password=password,
                )
                assert connected.returncode == 0, connected.stderr
                assert connected.stdout.strip() == "1"
                denied = await _psql(
                    migration_url,
                    sql=f"SELECT count(*) FROM {probe}.probe_rows;",
                    username=role,
                    password=password,
                )
                _assert_insufficient_privilege(denied)
                denied_neighbor = await _psql(
                    migration_url,
                    sql=f"SELECT count(*) FROM {neighbor}.probe_rows;",
                    username=role,
                    password=password,
                )
                _assert_insufficient_privilege(denied_neighbor)


async def _catalog_state(engine, role: str, probe: str, neighbor: str):
    return await _row(
        engine,
        """
        SELECT
            pg_catalog.has_schema_privilege(:role, :probe, 'USAGE') AS usage,
            pg_catalog.has_schema_privilege(:role, :probe, 'CREATE') AS create,
            pg_catalog.has_table_privilege(
                :role, :rows, 'SELECT,INSERT') AS table_access,
            pg_catalog.has_any_column_privilege(
                :role, :rows, 'UPDATE') AS column_update,
            pg_catalog.has_sequence_privilege(
                :role, :sequence, 'USAGE') AS sequence_access,
            pg_catalog.has_function_privilege(
                :role, :function, 'EXECUTE') AS function_access,
            pg_catalog.has_schema_privilege(
                :role, :neighbor, 'USAGE') AS neighbor_usage,
            pg_catalog.has_table_privilege(
                :role, :neighbor_rows,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ) AS neighbor_table_access,
            pg_catalog.has_any_column_privilege(
                :role, :neighbor_rows,
                'SELECT,INSERT,UPDATE,REFERENCES'
            ) AS neighbor_column_access,
            pg_catalog.has_schema_privilege(
                :role, 'yoao_assets_v1', 'USAGE') AS approved_usage,
            pg_catalog.has_function_privilege(
                :role,
                'yoao_assets_v1.list_catalog_datasets'
                '(uuid,text,text,boolean,integer,bigint)',
                'EXECUTE') AS approved_function
        """,
        role=role,
        probe=probe,
        rows=f"{probe}.probe_rows",
        sequence=f"{probe}.probe_seq",
        function=f"{probe}.probe_read()",
        neighbor=neighbor,
        neighbor_rows=f"{neighbor}.probe_rows",
    )


async def test_audit_read_rejects_new_namespace_schema_usage(
    owner_engine, migration_url
):
    probe = f"yoao_n5s_audit_{_suffix()}_v1"
    async with _login(owner_engine, "audit") as (role, _password):
        variables = {
            "audit_owner": "audit_code",
            "api_role": "legacy_runtime",
            "read_client": role,
        }
        baseline = await _apply(migration_url, "audit-read-grants.sql", variables)
        assert baseline.returncode == 0, baseline.stderr
        async with _schema(owner_engine, probe):
            await _execute(
                owner_engine,
                [
                    f"GRANT USAGE ON SCHEMA {probe} TO {role}",
                    # A deficit the rejecting install would repair only on commit.
                    f"REVOKE SELECT ON yoao_audit_v1.audit_events FROM {role}",
                ],
            )
            before = await _audit_state(owner_engine, role, probe)
            assert dict(before) == {
                "probe_usage": True,
                "audit_usage": True,
                "audit_read": False,
            }
            rejected = await _apply(migration_url, "audit-read-grants.sql", variables)
            _assert_guard_rejected(rejected)
            after = await _audit_state(owner_engine, role, probe)
            # The guard aborted the installer: the deficit it would have repaired
            # is still present, and the probe schema was never touched.
            assert dict(after) == dict(before)
        recovered = await _apply(migration_url, "audit-read-grants.sql", variables)
        assert recovered.returncode == 0, recovered.stderr
        final = await _row(
            owner_engine,
            """
            SELECT
                pg_catalog.to_regnamespace(:probe) IS NULL AS probe_absent,
                pg_catalog.has_schema_privilege(
                    :role, 'yoao_audit_v1', 'USAGE') AS audit_usage,
                pg_catalog.has_table_privilege(
                    :role, 'yoao_audit_v1.audit_events', 'SELECT') AS audit_read
            """,
            role=role,
            probe=probe,
        )
        assert dict(final) == {
            "probe_absent": True,
            "audit_usage": True,
            "audit_read": True,
        }


async def _audit_state(engine, role: str, probe: str):
    return await _row(
        engine,
        """
        SELECT
            pg_catalog.has_schema_privilege(
                :role, :probe, 'USAGE') AS probe_usage,
            pg_catalog.has_schema_privilege(
                :role, 'yoao_audit_v1', 'USAGE') AS audit_usage,
            pg_catalog.has_table_privilege(
                :role, 'yoao_audit_v1.audit_events', 'SELECT') AS audit_read
        """,
        role=role,
        probe=probe,
    )


async def test_identity_briefs_inventory_sees_public_invoker_new_namespace(
    owner_engine, migration_url
):
    role = "yoao_identity_briefs_client"
    probe = f"yoao_n5s_{_suffix()}_v1"
    function = f"{probe}.probe_policy()"
    variables = {
        "identity_owner": "identity_code",
        "workspace_owner": "workspace_code",
        "identity_briefs_role": role,
    }
    baseline = await _apply(migration_url, "identity-briefs-grants.sql", variables)
    assert baseline.returncode == 0, baseline.stderr
    async with _schema(owner_engine, probe):
        await _execute(
            owner_engine,
            [
                f"CREATE FUNCTION {function} RETURNS jsonb "
                "LANGUAGE sql AS 'SELECT ''{}''::jsonb'",
                f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC",
            ],
        )
        shape = await _function_shape(owner_engine, role, function)
        # The installer revokes every explicit role ACL in its own transaction,
        # so at verification time only the PUBLIC EXECUTE grant keeps this
        # SECURITY INVOKER function in the executable inventory. SECURITY
        # DEFINER and explicit-role ACL clauses cannot classify it.
        assert dict(shape) == {
            "invoker": True,
            "executable": True,
            "public_execute": True,
        }
        rejected = await _apply(migration_url, "identity-briefs-grants.sql", variables)
        _assert_guard_rejected(rejected)
        # The rejecting install rolled back; its revoke never committed.
        assert await _explicit_execute_grants(owner_engine, role, function) == 1
        approved = await _row(
            owner_engine,
            """
            SELECT
                pg_catalog.has_function_privilege(
                    :role,
                    'yoao_identity_briefs_v1.briefs_policy()',
                    'EXECUTE') AS policy,
                pg_catalog.has_function_privilege(
                    :role,
                    'yoao_identity_briefs_v1.execute_briefs_command(jsonb)',
                    'EXECUTE') AS command
            """,
            role=role,
        )
        assert dict(approved) == {"policy": True, "command": True}
    recovered = await _apply(migration_url, "identity-briefs-grants.sql", variables)
    assert recovered.returncode == 0, recovered.stderr


async def _function_shape(engine, role: str, function: str):
    return await _row(
        engine,
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


async def _explicit_execute_grants(engine, role: str, function: str):
    return await _scalar(
        engine,
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


async def test_identity_consumer_scopes_new_namespace_revocation(
    owner_engine, migration_url
):
    identity_probe = f"yoao_identity_n5s_{_suffix()}_v1"
    workspace_probe = f"yoao_workspace_n5s_{_suffix()}_v1"
    function = f"{identity_probe}.probe_read()"
    async with _login(owner_engine, "consumer") as (role, password):
        async with _schema(owner_engine, identity_probe):
            async with _schema(owner_engine, workspace_probe):
                await _execute(
                    owner_engine,
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
                        # The verifier's installation profile revokes CONNECT
                        # from PUBLIC. This throwaway role needs the database
                        # access a live API role already has, so the boundary
                        # asserted below is the object ACL and not login.
                        f'GRANT CONNECT ON DATABASE "{migration_url.database}" '
                        f"TO {role}",
                    ],
                )
                result = await _apply(
                    migration_url,
                    "identity-consumer-grants.sql",
                    {"identity_owner": "identity_code", "consumer_role": role},
                )
                assert result.returncode == 0, result.stderr
                identity_state = await _row(
                    owner_engine,
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
                    owner_engine,
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
                # Identity-only recognition must not widen to another namespace.
                assert dict(workspace_state) == {
                    "usage": True,
                    "table_read": True,
                }
                approved = await _row(
                    owner_engine,
                    """
                    SELECT
                        pg_catalog.has_function_privilege(
                            :role,
                            'yoao_identity_v1.read_actor_facts(uuid)',
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
                # The scoped role can connect and read the namespace it keeps;
                # the Identity probe is then denied at the object boundary.
                retained = await _psql(
                    migration_url,
                    sql=f"SELECT count(*) FROM {workspace_probe}.probe_rows;",
                    username=role,
                    password=password,
                )
                assert retained.returncode == 0, retained.stderr
                assert retained.stdout.strip() == "0"
                direct = await _psql(
                    migration_url,
                    sql=f"SELECT count(*) FROM {identity_probe}.probe_rows;",
                    username=role,
                    password=password,
                )
                _assert_insufficient_privilege(direct)


async def test_identity_deactivation_classifies_new_namespace_as_module(
    owner_engine, migration_url
):
    role = "yoao_identity_deactivation_client"
    probe = f"yoao_n5s_{_suffix()}_v1"
    function = f"{probe}.probe_policy()"
    variables = {
        "identity_owner": "identity_code",
        "audit_owner": "audit_code",
        "notifications_owner": "notifications_code",
        "identity_deactivation_role": role,
        "identity_deactivation_operator_role": "yoao_identity_deactivation_operator",
        "notification_publish_role": "yoao_notification_publish_client",
    }
    baseline = await _apply(
        migration_url, "identity-deactivation-grants.sql", variables
    )
    assert baseline.returncode == 0, baseline.stderr
    async with _schema(owner_engine, probe):
        await _execute(
            owner_engine,
            [
                f"CREATE FUNCTION {function} RETURNS jsonb "
                "LANGUAGE sql AS 'SELECT ''{}''::jsonb'",
                f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC",
            ],
        )
        shape = await _function_shape(owner_engine, role, function)
        # Both the explicit role ACL and the PUBLIC grant exist here. The
        # installer revokes the explicit ACL first, so only the PUBLIC grant
        # remains when its allowlist runs; the module-prefix predicate is what
        # keeps the SECURITY INVOKER function in that inventory.
        assert dict(shape) == {
            "invoker": True,
            "executable": True,
            "public_execute": True,
        }
        rejected = await _apply(
            migration_url, "identity-deactivation-grants.sql", variables
        )
        _assert_guard_rejected(rejected)
        assert await _explicit_execute_grants(owner_engine, role, function) == 1
        approved = await _row(
            owner_engine,
            """
            SELECT
                pg_catalog.has_function_privilege(
                    :role,
                    'yoao_identity_deactivation_v1.deactivation_policy()',
                    'EXECUTE') AS policy,
                pg_catalog.has_function_privilege(
                    :role,
                    'yoao_identity_deactivation_v1'
                    '.execute_deactivation_command(jsonb)',
                    'EXECUTE') AS command
            """,
            role=role,
        )
        assert dict(approved) == {"policy": True, "command": True}
    recovered = await _apply(
        migration_url, "identity-deactivation-grants.sql", variables
    )
    assert recovered.returncode == 0, recovered.stderr


async def test_workspace_pipeline_rejects_new_namespace_private_access(
    owner_engine, migration_url
):
    lan_probe = f"yoao_workspace_n5s_lan_{_suffix()}_v1"
    profile_probe = f"yoao_workspace_n5s_profile_{_suffix()}_v1"
    other_probe = f"yoao_n5s_other_{_suffix()}_v1"
    function = f"{profile_probe}.probe_read()"
    async with _login(owner_engine, "runtime") as (role, password):
        async with _login(owner_engine, "cleanup") as (cleanup, _cleanup):
            async with _schema(owner_engine, lan_probe):
                # The development runtime is configured by lan-production-grants
                # before the fresh API/worker profile below, so this storage must
                # already exist when that installer runs.
                await _execute(
                    owner_engine,
                    [
                        f"CREATE TABLE {lan_probe}.probe_rows (id integer, name text)",
                        f"CREATE SEQUENCE {lan_probe}.probe_seq",
                        f"GRANT USAGE, CREATE ON SCHEMA {lan_probe} TO {role}",
                        f"GRANT SELECT, INSERT, UPDATE "
                        f"ON {lan_probe}.probe_rows TO {role}",
                        f"GRANT SELECT (name) ON {lan_probe}.probe_rows TO {role}",
                        f"GRANT USAGE ON SEQUENCE {lan_probe}.probe_seq TO {role}",
                    ],
                )
                lan_before = await _lan_workspace_state(owner_engine, role, lan_probe)
                assert dict(lan_before) == {
                    "create": True,
                    "table_access": True,
                    "column_read": True,
                    "sequence_access": True,
                }
                lan = await _apply(
                    migration_url,
                    "lan-production-grants.sql",
                    {"application_role": role, "asset_cleanup_role": cleanup},
                )
                assert lan.returncode == 0, lan.stderr
                lan_after = await _lan_workspace_state(owner_engine, role, lan_probe)
                assert dict(lan_after) == {
                    "create": False,
                    "table_access": False,
                    "column_read": False,
                    "sequence_access": False,
                }
                consumer = await _apply(
                    migration_url,
                    "identity-consumer-grants.sql",
                    {"identity_owner": "identity_code", "consumer_role": role},
                )
                assert consumer.returncode == 0, consumer.stderr
                async with _schema(owner_engine, profile_probe):
                    async with _schema(owner_engine, other_probe):
                        await _execute(
                            owner_engine,
                            [
                                f"CREATE TABLE {profile_probe}.probe_rows "
                                "(id integer, name text)",
                                f"CREATE SEQUENCE {profile_probe}.probe_seq",
                                f"CREATE FUNCTION {function} RETURNS integer "
                                "LANGUAGE sql AS 'SELECT 1'",
                                f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC",
                                f"GRANT USAGE, CREATE ON SCHEMA "
                                f"{profile_probe} TO {role}",
                                f"GRANT SELECT, INSERT, UPDATE "
                                f"ON {profile_probe}.probe_rows TO {role}",
                                f"GRANT SELECT (name) "
                                f"ON {profile_probe}.probe_rows TO {role}",
                                f"GRANT USAGE ON SEQUENCE "
                                f"{profile_probe}.probe_seq TO {role}",
                                f"GRANT EXECUTE ON FUNCTION {function} TO {role}",
                                f"CREATE TABLE {other_probe}.probe_rows (id integer)",
                                f"GRANT USAGE ON SCHEMA {other_probe} TO {role}",
                                f"GRANT SELECT ON {other_probe}.probe_rows TO {role}",
                                # Mirror the existing profile setup: broad
                                # legacy grants that the profile must remove.
                                f"GRANT SELECT(id), UPDATE(name) "
                                f"ON public.projects TO {role}",
                                f"GRANT SELECT ON yoao_workspace_v1"
                                ".project_catalog_configs "
                                f"TO {role}",
                                f"GRANT EXECUTE ON FUNCTION "
                                "yoao_workspace_v1._project_delete_failure(text) "
                                f"TO {role}",
                                f"GRANT CREATE ON SCHEMA yoao_workspace_v1 TO {role}",
                            ],
                        )
                        before = await _workspace_state(
                            owner_engine, role, profile_probe
                        )
                        assert dict(before) == {
                            "usage": True,
                            "create": True,
                            "table_access": True,
                            "column_read": True,
                            "sequence_access": True,
                            "function_access": True,
                        }
                        result = await _apply(
                            migration_url,
                            "workspace-consumer-grants.sql",
                            {
                                "workspace_owner": "workspace_code",
                                "consumer_role": role,
                            },
                        )
                        assert result.returncode == 0, result.stderr
                        after = await _workspace_state(
                            owner_engine, role, profile_probe
                        )
                        assert dict(after) == {
                            "usage": False,
                            "create": False,
                            "table_access": False,
                            "column_read": False,
                            "sequence_access": False,
                            "function_access": False,
                        }
                        other = await _row(
                            owner_engine,
                            """
                            SELECT
                                pg_catalog.has_schema_privilege(
                                    :role, :probe, 'USAGE') AS usage,
                                pg_catalog.has_table_privilege(
                                    :role, :rows, 'SELECT') AS table_read
                            """,
                            role=role,
                            probe=other_probe,
                            rows=f"{other_probe}.probe_rows",
                        )
                        # Workspace-only cleanup must not sweep another
                        # new-prefix namespace.
                        assert dict(other) == {
                            "usage": True,
                            "table_read": True,
                        }
                        profile = await _row(
                            owner_engine,
                            """
                            SELECT
                                pg_catalog.has_schema_privilege(
                                    :role, 'yoao_workspace_v1',
                                    'CREATE') AS create,
                                pg_catalog.has_table_privilege(
                                    :role, 'yoao_workspace_v1.project_facts',
                                    'SELECT') AS facts_read,
                                pg_catalog.has_table_privilege(
                                    :role, 'public.projects',
                                    'SELECT') AS projects,
                                pg_catalog.has_any_column_privilege(
                                    :role, 'public.projects',
                                    'SELECT') AS projects_column
                            """,
                            role=role,
                        )
                        assert dict(profile) == {
                            "create": False,
                            "facts_read": True,
                            "projects": False,
                            "projects_column": False,
                        }
                        denied = await _psql(
                            migration_url,
                            sql=(f"SELECT count(*) FROM {profile_probe}.probe_rows;"),
                            username=role,
                            password=password,
                        )
                        _assert_insufficient_privilege(denied)
                        retained = await _psql(
                            migration_url,
                            sql=(f"SELECT count(*) FROM {other_probe}.probe_rows;"),
                            username=role,
                            password=password,
                        )
                        assert retained.returncode == 0, retained.stderr
                        assert retained.stdout.strip() == "0"


async def _lan_workspace_state(engine, role: str, probe: str):
    return await _row(
        engine,
        """
        SELECT
            pg_catalog.has_schema_privilege(:role, :probe, 'CREATE') AS create,
            pg_catalog.has_table_privilege(
                :role, :rows,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ) AS table_access,
            pg_catalog.has_any_column_privilege(
                :role, :rows, 'SELECT') AS column_read,
            pg_catalog.has_sequence_privilege(
                :role, :sequence, 'USAGE,SELECT,UPDATE') AS sequence_access
        """,
        role=role,
        probe=probe,
        rows=f"{probe}.probe_rows",
        sequence=f"{probe}.probe_seq",
    )


async def _workspace_state(engine, role: str, probe: str):
    return await _row(
        engine,
        """
        SELECT
            pg_catalog.has_schema_privilege(:role, :probe, 'USAGE') AS usage,
            pg_catalog.has_schema_privilege(:role, :probe, 'CREATE') AS create,
            pg_catalog.has_table_privilege(
                :role, :rows,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ) AS table_access,
            pg_catalog.has_any_column_privilege(
                :role, :rows, 'SELECT') AS column_read,
            pg_catalog.has_sequence_privilege(
                :role, :sequence, 'USAGE,SELECT,UPDATE') AS sequence_access,
            pg_catalog.has_function_privilege(
                :role, :function, 'EXECUTE') AS function_access
        """,
        role=role,
        probe=probe,
        rows=f"{probe}.probe_rows",
        sequence=f"{probe}.probe_seq",
        function=f"{probe}.probe_read()",
    )
