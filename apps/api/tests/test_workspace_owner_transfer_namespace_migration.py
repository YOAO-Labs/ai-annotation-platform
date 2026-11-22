"""Qualify the populated bo0459 -> bo0460 owner-transfer namespace cutover.

This is a controlled migration fixture, not a claim that an old production
topology was qualified. It builds the real historical chain to bo0459, seeds a
valid account/project world, and executes the old RPC through an exactly owned
two-function legacy LOGIN. The test then proves each fixed preflight refusal,
preserves the destination collision, upgrades, reuses the checked-in current
grant installers for the qualified new owner/client, and replays real receipts
through the new LOGIN.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.models.project import Project
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from yoao_owner_data.postgres import (
    FunctionRoleUnavailable,
    verify_function_only_role,
)

ROOT = Path(__file__).resolve().parents[3]
API_ROOT = Path(__file__).resolve().parents[1]
OLD_SCHEMA = "aap_workspace_owner_transfer_v1"
NEW_SCHEMA = "yoao_workspace_owner_transfer_v1"
LEGACY_LOGIN = "workspace_owner_transfer_client"
NEW_LOGIN = "yoao_workspace_owner_transfer_client"
WIRE_SCHEMA = "aap-workspace-owner-transfer/v1"
RECEIPTS = "public.workspace_owner_transfer_operation_receipts"
_FUNCTIONS = (
    "_failure(integer,jsonb,boolean)",
    "owner_transfer_policy()",
    "transfer_project_owner(jsonb)",
)
_MODULE_GRANT_VARIABLES = {
    "migration_role": "owner",
    "identity_owner": "identity_code",
    "workspace_owner": "workspace_code",
    "assets_owner": "assets_code",
    "labeling_owner": "labeling_code",
    "jobs_owner": "jobs_code",
    "learning_owner": "learning_code",
    "inference_owner": "inference_code",
    "insights_owner": "insights_code",
    "diagnostics_owner": "diagnostics_code",
    "export_owner": "export_code",
    "audit_owner": "audit_code",
    "settings_owner": "settings_code",
    "notifications_owner": "notifications_code",
    "workflow_role": "yoao_append_client",
    "asset_cleanup_role": "yoao_asset_cleanup_client",
    "mask_gc_role": "yoao_labeling_mask_gc_client",
    "inference_registry_ingress_role": "yoao_inference_registry_ingress_client",
    "inference_routing_role": "yoao_inference_routing_client",
}
_OWNER_TRANSFER_GRANT_VARIABLES = {
    "migration_role": "owner",
    "identity_owner": "identity_code",
    "workspace_owner": "workspace_code",
    "workspace_owner_transfer_role": NEW_LOGIN,
}

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]


def _config(url) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return config


def _owner_tool_url():
    value = make_url(settings.effective_migration_database_url)
    assert value.username == "owner"
    assert value.password, "the verified migration owner credentials are required"
    return value


def _owner_dsn(database: str) -> str:
    return (
        _owner_tool_url()
        .set(database=database, drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def _psql_script(database: str, sql: str, variables: dict[str, str]) -> None:
    owner = _owner_tool_url()
    _verify_postgres(owner.set(database=database))
    command_line = ["docker", "exec", "-i", "-e", "PGPASSWORD"]
    command_line.extend(
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
            "owner",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
        ]
    )
    for name, value in variables.items():
        assert value is not None
        command_line.extend(["-v", f"{name}={value}"])
    command_line.extend(["-f", "-"])
    environment = dict(os.environ)
    environment["PGPASSWORD"] = owner.password
    result = subprocess.run(
        command_line,
        input=sql,
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
    )
    if result.returncode:
        evidence = (
            ROOT
            / ".worktree"
            / f"owner-transfer-installer-{database}-{uuid.uuid4().hex[:12]}.stderr"
        )
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(result.stderr, encoding="utf-8")
        evidence.chmod(0o600)
        pytest.fail(f"checked-in grant installer failed; stderr retained at {evidence}")


def _sqlstate(value: BaseException) -> str | None:
    seen: set[int] = set()
    pending: list[BaseException | None] = [value]
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        state = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if isinstance(state, str):
            return state
        pending.extend(
            candidate
            for candidate in (
                getattr(error, "orig", None),
                error.__cause__,
                error.__context__,
            )
            if isinstance(candidate, BaseException)
        )
    return None


async def _function_rows(connection, schema: str) -> dict[str, dict]:
    rows = (
        (
            await connection.execute(
                text(
                    "SELECT proc.oid, proc.proname, "
                    "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') "
                    "AS args, pg_catalog.pg_get_userbyid(proc.proowner) AS owner, "
                    "proc.prosecdef, proc.proconfig, proc.provolatile, "
                    "proc.proparallel, proc.prokind, "
                    "proc.prorettype::pg_catalog.regtype::text AS returns, "
                    "language.lanname AS language "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = proc.pronamespace "
                    "JOIN pg_catalog.pg_language AS language "
                    "ON language.oid = proc.prolang "
                    "WHERE namespace.nspname = :schema AND proc.prokind = 'f'"
                ),
                {"schema": schema},
            )
        )
        .mappings()
        .all()
    )
    return {f"{row['proname']}({row['args']})": dict(row) for row in rows}


async def _function_acl(connection, oid) -> set[tuple]:
    rows = (
        await connection.execute(
            text(
                "SELECT COALESCE(role.rolname, 'PUBLIC') AS grantee, "
                "entry.privilege_type, entry.is_grantable "
                "FROM pg_catalog.pg_proc AS proc "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "proc.proacl, pg_catalog.acldefault('f', proc.proowner))) AS entry "
                "LEFT JOIN pg_catalog.pg_roles AS role ON role.oid = entry.grantee "
                "WHERE proc.oid = CAST(:oid AS pg_catalog.oid)"
            ),
            {"oid": oid},
        )
    ).all()
    return {tuple(row) for row in rows}


async def _schema_acl(connection, schema: str) -> set[tuple]:
    rows = (
        await connection.execute(
            text(
                "SELECT COALESCE(role.rolname, 'PUBLIC') AS grantee, "
                "entry.privilege_type, entry.is_grantable "
                "FROM pg_catalog.pg_namespace AS namespace "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner)"
                ")) AS entry "
                "LEFT JOIN pg_catalog.pg_roles AS role ON role.oid = entry.grantee "
                "WHERE namespace.nspname = :schema"
            ),
            {"schema": schema},
        )
    ).all()
    return {tuple(row) for row in rows}


async def _table_state(connection, operation_id) -> dict:
    relation = (
        (
            await connection.execute(
                text(
                    "SELECT relation.oid, relation.relkind::text AS relkind "
                    "FROM pg_catalog.pg_class AS relation "
                    "WHERE relation.oid = CAST(:receipts AS pg_catalog.regclass)"
                ),
                {"receipts": RECEIPTS},
            )
        )
        .mappings()
        .one()
    )
    acl = (
        await connection.execute(
            text(
                "SELECT COALESCE(role.rolname, 'PUBLIC') AS grantee, "
                "entry.privilege_type, entry.is_grantable "
                "FROM pg_catalog.pg_class AS relation "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "relation.relacl, pg_catalog.acldefault('r', relation.relowner)"
                ")) AS entry "
                "LEFT JOIN pg_catalog.pg_roles AS role ON role.oid = entry.grantee "
                "WHERE relation.oid = CAST(:receipts AS pg_catalog.regclass)"
            ),
            {"receipts": RECEIPTS},
        )
    ).all()
    receipt = await connection.scalar(
        text(
            "SELECT to_jsonb(receipt)::text "
            "FROM public.workspace_owner_transfer_operation_receipts AS receipt "
            "WHERE receipt.operation_id = :operation_id"
        ),
        {"operation_id": operation_id},
    )
    count = await connection.scalar(
        text("SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts")
    )
    return {
        "oid": relation["oid"],
        "kind": relation["relkind"],
        "acl": {tuple(row) for row in acl},
        "receipt": receipt,
        "count": count,
    }


async def _project_state(connection, project_id) -> tuple:
    row = (
        await connection.execute(
            text(
                "SELECT owner_id, updated_at FROM public.projects WHERE id = :project_id"
            ),
            {"project_id": project_id},
        )
    ).one()
    return row[0], row[1]


async def _owner_project_state(engine, project_id) -> tuple:
    async with engine.connect() as connection:
        return await _project_state(connection, project_id)


async def _boundary_state(connection, schema: str, project_id, operation_id) -> dict:
    functions = await _function_rows(connection, schema)
    assert set(functions) == set(_FUNCTIONS)
    return {
        "functions": functions,
        "function_acl": {
            signature: await _function_acl(connection, row["oid"])
            for signature, row in functions.items()
        },
        "schema_acl": await _schema_acl(connection, schema),
        "receipts": await _table_state(connection, operation_id),
        "project": await _project_state(connection, project_id),
    }


async def _assert_quiescent(engine, database: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                    "WHERE datname = :database AND pid <> pg_backend_pid() "
                    "AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                ),
                {"database": database},
            )
        if count == 0:
            return
        assert time.monotonic() < deadline, "unexpected scratch database sessions"
        await asyncio.sleep(0.1)


async def _assert_preflight_unchanged(
    engine,
    *,
    database: str,
    project_id,
    operation_id,
    baseline: dict,
    legacy_login: bool,
    destination_present: bool = False,
) -> None:
    await _assert_quiescent(engine, database)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            == "bo0459"
        )
        assert (
            await connection.scalar(
                text("SELECT to_regnamespace(:name)"), {"name": OLD_SCHEMA}
            )
            is not None
        )
        present = await connection.scalar(
            text("SELECT to_regnamespace(:name)"), {"name": NEW_SCHEMA}
        )
        assert (present is not None) is destination_present
        assert (
            await connection.scalar(
                text(
                    "SELECT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = :name"
                ),
                {"name": LEGACY_LOGIN},
            )
            is legacy_login
        )
        assert (
            await _boundary_state(connection, OLD_SCHEMA, project_id, operation_id)
            == baseline
        )


async def _grab_new_login(engine, database: str):
    dsn_url = make_url(os.environ["AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL"])
    assert dsn_url.username == NEW_LOGIN
    assert (dsn_url.host, dsn_url.port) == (engine.url.host, engine.url.port)
    dsn = dsn_url.set(database=database, drivername="postgresql").render_as_string(
        hide_password=False
    )
    connection = await asyncpg.connect(dsn)
    assert await connection.fetchval("SELECT current_user") == NEW_LOGIN
    return connection


def _legacy_dsn(engine, database: str, password: str) -> str:
    value = make_url(os.environ["AAP_MODULE_DATA_WORKSPACE_OWNER_TRANSFER_URL"])
    assert (value.host, value.port) == (engine.url.host, engine.url.port)
    return value.set(
        username=LEGACY_LOGIN,
        password=password,
        database=database,
        drivername="postgresql",
    ).render_as_string(hide_password=False)


async def test_workspace_owner_transfer_namespace_migration_preserves_populated_boundary(
    test_engine,
):
    _verify_postgres(test_engine.url)
    assert test_engine.url.username == "owner"
    assert test_engine.url.database.endswith("_test")
    owner = make_url(settings.effective_migration_database_url)
    assert (owner.host, owner.port, owner.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )

    database = f"aap_owner_transfer_ns_{uuid.uuid4().hex[:16]}_test"
    ownership = f"workspace-owner-transfer-namespace:{uuid.uuid4()}"
    target = test_engine.url.set(database=database)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    scratch = create_async_engine(target, poolclass=NullPool, hide_parameters=True)
    maker = async_sessionmaker(scratch, expire_on_commit=False)
    created = False
    db_oid = None
    db_owner_oid = None
    db_owner_name = None
    role_created = False
    role_oid = None
    legacy_password = secrets.token_urlsafe(32)
    legacy_connection = None
    new_connection = None
    operation_id = uuid.uuid4()
    later_operation_id = uuid.uuid4()
    actor_id, first_owner_id, second_owner_id, project_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    request = {
        "schema": WIRE_SCHEMA,
        "operation_id": str(operation_id),
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "new_owner_id": str(first_owner_id),
    }
    later_request = {
        "schema": WIRE_SCHEMA,
        "operation_id": str(later_operation_id),
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "new_owner_id": str(second_owner_id),
    }
    try:
        async with test_engine.connect() as source:
            assert await source.scalar(
                text(
                    "SELECT rolsuper OR rolcreatedb FROM pg_catalog.pg_roles "
                    "WHERE rolname = current_user"
                )
            ), "forward migration qualification requires a disposable database owner"
            acl = (
                await source.execute(
                    text(
                        "SELECT COALESCE(role.rolname, 'PUBLIC'), "
                        "entry.privilege_type, entry.is_grantable "
                        "FROM pg_catalog.pg_database AS database "
                        "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                        "database.datacl, pg_catalog.acldefault('d', database.datdba)"
                        ")) AS entry "
                        "LEFT JOIN pg_catalog.pg_roles AS role "
                        "ON role.oid = entry.grantee "
                        "WHERE database.datname = current_database()"
                    )
                )
            ).all()
        async with admin.connect() as connection:
            await connection.execute(
                text(f'CREATE DATABASE "{database}" TEMPLATE template0')
            )
            created = True
            identity = (
                (
                    await connection.execute(
                        text(
                            "SELECT database.oid, database.datdba, "
                            "pg_catalog.pg_get_userbyid(database.datdba) AS owner "
                            "FROM pg_catalog.pg_database AS database "
                            "WHERE database.datname = :name"
                        ),
                        {"name": database},
                    )
                )
                .mappings()
                .one()
            )
            db_oid = identity["oid"]
            db_owner_oid = identity["datdba"]
            db_owner_name = identity["owner"]
            assert db_owner_name == test_engine.url.username == "owner"
            await connection.execute(
                text(f"COMMENT ON DATABASE \"{database}\" IS '{ownership}'")
            )
            quote = connection.dialect.identifier_preparer.quote_identifier
            await connection.execute(
                text(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC')
            )
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(
                        f'GRANT {privilege} ON DATABASE "{database}" TO {role}{suffix}'
                    )
                )

        config = _config(target)
        await asyncio.to_thread(command.upgrade, config, "bo0459")
        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == "bo0459"
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": OLD_SCHEMA}
                )
                is not None
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": NEW_SCHEMA}
                )
                is None
            )

        async with scratch.begin() as connection:
            await connection.execute(
                text(
                    """
                    DO $public$
                    DECLARE namespace record;
                    BEGIN
                        FOR namespace IN
                            SELECT nspname FROM pg_catalog.pg_namespace
                            WHERE pg_catalog.left(nspname, 4) = 'aap_'
                               OR pg_catalog.left(nspname, 5) = 'yoao_'
                        LOOP
                            EXECUTE pg_catalog.format(
                                'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I '
                                'FROM PUBLIC',
                                namespace.nspname
                            );
                        END LOOP;
                    END;
                    $public$;
                    """
                )
            )

        async with admin.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT rolcanlogin FROM pg_catalog.pg_roles "
                        "WHERE rolname = :name"
                    ),
                    {"name": NEW_LOGIN},
                )
                is True
            ), "the explicit new owner-transfer LOGIN is not provisioned"
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = :name"
                    ),
                    {"name": LEGACY_LOGIN},
                )
                == 0
            ), "the retired legacy owner-transfer LOGIN already exists"

        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=actor_id,
                        email=f"owner-transfer-ns-actor-{actor_id.hex}@example.test",
                        name="Owner transfer namespace administrator",
                        password_hash="fixture-only-workspace-owner-transfer-ns",
                        role="super_admin",
                        is_active=True,
                    ),
                    User(
                        id=first_owner_id,
                        email=f"owner-transfer-ns-first-{first_owner_id.hex}@example.test",
                        name="Owner transfer namespace first owner",
                        password_hash="fixture-only-workspace-owner-transfer-ns",
                        role="project_admin",
                        is_active=True,
                    ),
                    User(
                        id=second_owner_id,
                        email=f"owner-transfer-ns-second-{second_owner_id.hex}@example.test",
                        name="Owner transfer namespace second owner",
                        password_hash="fixture-only-workspace-owner-transfer-ns",
                        role="project_admin",
                        is_active=True,
                    ),
                ]
            )
            await session.flush()
            await session.execute(
                text(
                    "UPDATE identity_account_presence SET status = 'online' "
                    "WHERE user_id = ANY(:ids)"
                ),
                {"ids": [actor_id, first_owner_id, second_owner_id]},
            )
            session.add(
                Project(
                    id=project_id,
                    display_id=f"WM-{project_id.hex[:16]}",
                    name="Workspace owner transfer namespace migration",
                    type_label="Image",
                    type_key="image",
                    owner_id=actor_id,
                )
            )

        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f'CREATE ROLE "{LEGACY_LOGIN}" LOGIN PASSWORD '
                    f"'{legacy_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            role_created = True
            role_oid = await connection.scalar(
                text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :name"),
                {"name": LEGACY_LOGIN},
            )
            assert role_oid is not None
        async with scratch.begin() as connection:
            await connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO "{LEGACY_LOGIN}"')
            )
            await connection.execute(
                text(f'GRANT USAGE ON SCHEMA "{OLD_SCHEMA}" TO "{LEGACY_LOGIN}"')
            )
            await connection.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION "{OLD_SCHEMA}".owner_transfer_policy() '
                    f'TO "{LEGACY_LOGIN}"'
                )
            )
            await connection.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION "{OLD_SCHEMA}".'
                    f'transfer_project_owner(jsonb) TO "{LEGACY_LOGIN}"'
                )
            )

        legacy_connection = await asyncpg.connect(
            _legacy_dsn(test_engine, database, legacy_password)
        )
        try:
            assert (
                await legacy_connection.fetchval("SELECT current_user") == LEGACY_LOGIN
            )
            await verify_function_only_role(
                legacy_connection,
                schema=OLD_SCHEMA,
                functions={
                    ("owner_transfer_policy", ""),
                    ("transfer_project_owner", "jsonb"),
                },
                role=LEGACY_LOGIN,
            )
            assert (
                await legacy_connection.fetchval(
                    f"SELECT {OLD_SCHEMA}.owner_transfer_policy()"
                )
                is not None
            )

            old_reply = json.loads(
                await legacy_connection.fetchval(
                    f"SELECT {OLD_SCHEMA}.transfer_project_owner($1::jsonb)",
                    json.dumps(request),
                )
            )
            assert old_reply["ok"] is True
            assert old_reply["data"]["changed"] is True
            assert old_reply["data"]["previous_owner_id"] == str(actor_id)
        finally:
            await legacy_connection.close()
            legacy_connection = None

        async with scratch.connect() as connection:
            receipt_json = await connection.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.workspace_owner_transfer_operation_receipts AS receipt "
                    "WHERE receipt.operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt_json is not None
            receipt = json.loads(receipt_json)
            assert (
                receipt["request_hash"]
                == hashlib.sha256(
                    (
                        f"{WIRE_SCHEMA}|{operation_id}|{actor_id}|{project_id}|"
                        f"{first_owner_id}"
                    ).encode()
                ).hexdigest()
            )
            assert receipt["result"] == old_reply
            baseline = await _boundary_state(
                connection, OLD_SCHEMA, project_id, operation_id
            )
            assert baseline["project"][0] == first_owner_id
            assert (
                LEGACY_LOGIN,
                "EXECUTE",
                False,
            ) in baseline["function_acl"]["owner_transfer_policy()"]
            assert (
                LEGACY_LOGIN,
                "EXECUTE",
                False,
            ) in baseline["function_acl"]["transfer_project_owner(jsonb)"]
            assert (LEGACY_LOGIN, "USAGE", False) in baseline["schema_acl"]
            assert not any(
                entry[0] == LEGACY_LOGIN
                for entry in baseline["function_acl"]["_failure(integer,jsonb,boolean)"]
            )

        await _assert_quiescent(scratch, database)
        with pytest.raises(Exception) as enabled_refusal:
            await asyncio.to_thread(command.upgrade, config, "bo0460")
        assert _sqlstate(enabled_refusal.value) == "55006"
        await _assert_preflight_unchanged(
            scratch,
            database=database,
            project_id=project_id,
            operation_id=operation_id,
            baseline=baseline,
            legacy_login=True,
        )

        legacy_connection = await asyncpg.connect(
            _legacy_dsn(test_engine, database, legacy_password)
        )
        try:
            await legacy_connection.fetchval(
                f"SELECT {OLD_SCHEMA}.owner_transfer_policy()"
            )
            async with admin.connect() as connection:
                await connection.execute(text(f'ALTER ROLE "{LEGACY_LOGIN}" NOLOGIN'))
            assert await legacy_connection.fetchval("SELECT 1") == 1
            with pytest.raises(Exception) as writer_refusal:
                await asyncio.to_thread(command.upgrade, config, "bo0460")
            assert _sqlstate(writer_refusal.value) == "55006"
        finally:
            await legacy_connection.close()
            legacy_connection = None
        await _assert_preflight_unchanged(
            scratch,
            database=database,
            project_id=project_id,
            operation_id=operation_id,
            baseline=baseline,
            legacy_login=False,
        )

        async with scratch.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE FUNCTION "{OLD_SCHEMA}".cutover_probe() '
                    "RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$"
                )
            )
        with pytest.raises(Exception) as shape_refusal:
            await asyncio.to_thread(command.upgrade, config, "bo0460")
        assert _sqlstate(shape_refusal.value) == "55000"
        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == "bo0459"
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regprocedure(:signature)"),
                    {"signature": f"{OLD_SCHEMA}.cutover_probe()"},
                )
                is not None
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT to_jsonb(receipt)::text "
                        "FROM public.workspace_owner_transfer_operation_receipts "
                        "AS receipt WHERE receipt.operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
                == receipt_json
            )
            assert await _project_state(connection, project_id) == baseline["project"]
        async with scratch.begin() as connection:
            await connection.execute(
                text(f'DROP FUNCTION "{OLD_SCHEMA}".cutover_probe()')
            )
        await _assert_preflight_unchanged(
            scratch,
            database=database,
            project_id=project_id,
            operation_id=operation_id,
            baseline=baseline,
            legacy_login=False,
        )

        async with admin.connect() as connection:
            await connection.execute(text(f'ALTER ROLE "{LEGACY_LOGIN}" CREATEROLE'))
        with pytest.raises(Exception) as privileged_refusal:
            await asyncio.to_thread(command.upgrade, config, "bo0460")
        assert _sqlstate(privileged_refusal.value) == "42501"
        async with admin.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT rolcreaterole FROM pg_catalog.pg_roles "
                        "WHERE rolname = :name"
                    ),
                    {"name": LEGACY_LOGIN},
                )
                is True
            )
        await _assert_preflight_unchanged(
            scratch,
            database=database,
            project_id=project_id,
            operation_id=operation_id,
            baseline=baseline,
            legacy_login=False,
        )
        async with admin.connect() as connection:
            await connection.execute(text(f'ALTER ROLE "{LEGACY_LOGIN}" NOCREATEROLE'))

        marker = uuid.uuid4().hex
        async with scratch.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{NEW_SCHEMA}"'))
            await connection.execute(
                text(
                    f'CREATE TABLE "{NEW_SCHEMA}".cutover_sentinel (marker text NOT NULL)'
                )
            )
            await connection.execute(
                text(
                    f'INSERT INTO "{NEW_SCHEMA}".cutover_sentinel (marker) '
                    "VALUES (:marker)"
                ),
                {"marker": marker},
            )
        with pytest.raises(Exception) as collision_refusal:
            await asyncio.to_thread(command.upgrade, config, "bo0460")
        assert _sqlstate(collision_refusal.value) == "42P06"
        await _assert_preflight_unchanged(
            scratch,
            database=database,
            project_id=project_id,
            operation_id=operation_id,
            baseline=baseline,
            legacy_login=False,
            destination_present=True,
        )
        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text(f'SELECT marker FROM "{NEW_SCHEMA}".cutover_sentinel')
                )
                == marker
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_proc AS proc "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid = proc.pronamespace "
                        "WHERE namespace.nspname = :schema"
                    ),
                    {"schema": NEW_SCHEMA},
                )
                == 0
            )
        async with scratch.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{NEW_SCHEMA}" CASCADE'))

        await _assert_quiescent(scratch, database)
        await asyncio.to_thread(command.upgrade, config, "bo0460")
        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == "bo0460"
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": OLD_SCHEMA}
                )
                is None
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": NEW_SCHEMA}
                )
                is not None
            )
            upgraded = await _boundary_state(
                connection, NEW_SCHEMA, project_id, operation_id
            )
            assert upgraded["project"] == baseline["project"]
            assert upgraded["receipts"]["oid"] == baseline["receipts"]["oid"]
            assert upgraded["receipts"]["kind"] == "r"
            assert upgraded["receipts"]["acl"] == baseline["receipts"]["acl"]
            assert upgraded["receipts"]["count"] == 1
            assert upgraded["receipts"]["receipt"] == receipt_json
            assert upgraded["schema_acl"] == {
                entry for entry in baseline["schema_acl"] if entry[0] != LEGACY_LOGIN
            }
            assert all(entry[0] != "PUBLIC" for entry in upgraded["schema_acl"])
            for signature in _FUNCTIONS:
                before = baseline["functions"][signature]
                after = upgraded["functions"][signature]
                assert after["oid"] == before["oid"]
                for field in (
                    "owner",
                    "prosecdef",
                    "proconfig",
                    "provolatile",
                    "proparallel",
                    "prokind",
                    "returns",
                    "language",
                ):
                    assert after[field] == before[field]
                assert upgraded["function_acl"][signature] == {
                    entry
                    for entry in baseline["function_acl"][signature]
                    if entry[0] != LEGACY_LOGIN
                }
                assert all(
                    entry[0] != "PUBLIC"
                    for entry in upgraded["function_acl"][signature]
                )
            assert not await connection.scalar(
                text("SELECT pg_catalog.has_schema_privilege(:role, :schema, 'USAGE')"),
                {"role": LEGACY_LOGIN, "schema": NEW_SCHEMA},
            )
            assert not await connection.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    ":role, CAST(:signature AS pg_catalog.regprocedure), 'EXECUTE')"
                ),
                {
                    "role": LEGACY_LOGIN,
                    "signature": f"{NEW_SCHEMA}.transfer_project_owner(jsonb)",
                },
            )

        await asyncio.to_thread(command.upgrade, config, "head")
        head = ScriptDirectory.from_config(config).get_current_head()
        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
                == head
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": OLD_SCHEMA}
                )
                is None
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regnamespace(:name)"), {"name": NEW_SCHEMA}
                )
                is not None
            )

        module_grants = (ROOT / "infra/docker/module-data-grants.sql").read_text(
            encoding="utf-8"
        )
        transfer_grants = (
            ROOT / "infra/docker/workspace-owner-transfer-grants.sql"
        ).read_text(encoding="utf-8")
        module_variables = dict(_MODULE_GRANT_VARIABLES)
        module_variables["expected_database"] = database
        transfer_variables = dict(_OWNER_TRANSFER_GRANT_VARIABLES)
        transfer_variables["expected_database"] = database
        _psql_script(database, module_grants, module_variables)
        _psql_script(database, transfer_grants, transfer_variables)

        async with scratch.connect() as connection:
            installed = await _function_rows(connection, NEW_SCHEMA)
            assert set(installed) == set(_FUNCTIONS)
            assert all(row["owner"] == "workspace_code" for row in installed.values())
            assert await connection.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege("
                    "'workspace_code', :schema, 'USAGE')"
                ),
                {"schema": NEW_SCHEMA},
            )
            assert not await connection.scalar(
                text(
                    "SELECT pg_catalog.has_schema_privilege("
                    "'workspace_code', :schema, 'CREATE')"
                ),
                {"schema": NEW_SCHEMA},
            )

        before_replay = await _owner_project_state(scratch, project_id)
        new_connection = await _grab_new_login(test_engine, database)
        try:
            await verify_function_only_role(
                new_connection,
                schema=NEW_SCHEMA,
                functions={
                    ("owner_transfer_policy", ""),
                    ("transfer_project_owner", "jsonb"),
                },
                role=NEW_LOGIN,
            )
            assert json.loads(
                await new_connection.fetchval(
                    f"SELECT {NEW_SCHEMA}.owner_transfer_policy()"
                )
            ) == {"schema": WIRE_SCHEMA}

            replay = json.loads(
                await new_connection.fetchval(
                    f"SELECT {NEW_SCHEMA}.transfer_project_owner($1::jsonb)",
                    json.dumps(request),
                )
            )
            assert replay == old_reply
            assert await _owner_project_state(scratch, project_id) == before_replay
            assert before_replay[0] == first_owner_id

            later_reply = json.loads(
                await new_connection.fetchval(
                    f"SELECT {NEW_SCHEMA}.transfer_project_owner($1::jsonb)",
                    json.dumps(later_request),
                )
            )
            assert later_reply["ok"] is True
            assert later_reply["data"]["changed"] is True
            assert later_reply["data"]["previous_owner_id"] == str(first_owner_id)
            after_later = await _owner_project_state(scratch, project_id)
            assert after_later[0] == second_owner_id
            assert after_later[1] >= before_replay[1]

            stale_replay = json.loads(
                await new_connection.fetchval(
                    f"SELECT {NEW_SCHEMA}.transfer_project_owner($1::jsonb)",
                    json.dumps(request),
                )
            )
            assert stale_replay == old_reply
            assert await _owner_project_state(scratch, project_id) == after_later
        finally:
            await new_connection.close()
            new_connection = None

        async with scratch.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM public."
                        "workspace_owner_transfer_operation_receipts "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": later_operation_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT to_jsonb(receipt)::text "
                        "FROM public.workspace_owner_transfer_operation_receipts "
                        "AS receipt WHERE receipt.operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
                == receipt_json
            )

        with pytest.raises(
            asyncpg.InvalidAuthorizationSpecificationError
        ) as denied_login:
            await asyncpg.connect(_legacy_dsn(test_engine, database, legacy_password))
        assert denied_login.value.sqlstate == "28000"

        owner_connection = await asyncpg.connect(_owner_dsn(database))
        try:
            assert (
                await owner_connection.fetchval(
                    "SELECT rolsuper FROM pg_catalog.pg_roles "
                    "WHERE rolname = current_user"
                )
                is True
            )
            await owner_connection.execute(f'SET ROLE "{LEGACY_LOGIN}"')
            try:
                assert (
                    await owner_connection.fetchval("SELECT current_user")
                    == LEGACY_LOGIN
                )
                with pytest.raises(FunctionRoleUnavailable):
                    await verify_function_only_role(
                        owner_connection,
                        schema=OLD_SCHEMA,
                        functions={
                            ("owner_transfer_policy", ""),
                            ("transfer_project_owner", "jsonb"),
                        },
                        role=LEGACY_LOGIN,
                    )
                with pytest.raises(asyncpg.InsufficientPrivilegeError) as retired:
                    await owner_connection.fetchval(
                        f"SELECT {NEW_SCHEMA}.owner_transfer_policy()"
                    )
                assert retired.value.sqlstate == "42501"
                with pytest.raises(asyncpg.InvalidSchemaNameError) as absent:
                    await owner_connection.fetchval(
                        f"SELECT {OLD_SCHEMA}.owner_transfer_policy()"
                    )
                assert absent.value.sqlstate == "3F000"
            finally:
                await owner_connection.execute("RESET ROLE")
            assert await owner_connection.fetchval("SELECT current_user") == "owner"
        finally:
            await owner_connection.close()
    finally:
        if legacy_connection is not None:
            await legacy_connection.close()
        if new_connection is not None:
            await new_connection.close()
        await scratch.dispose()
        try:
            if created:
                async with admin.connect() as connection:
                    identity = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT database.oid, database.datdba, "
                                    "pg_catalog.pg_get_userbyid(database.datdba) AS owner, "
                                    "pg_catalog.shobj_description(database.oid, 'pg_database') "
                                    "AS marker "
                                    "FROM pg_catalog.pg_database AS database "
                                    "WHERE database.datname = :name"
                                ),
                                {"name": database},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    assert identity is not None, "created database identity is missing"
                    assert identity["oid"] == db_oid, "created database OID changed"
                    assert identity["datdba"] == db_owner_oid, (
                        "created database owner changed"
                    )
                    assert (
                        identity["owner"] == db_owner_name == test_engine.url.username
                    )
                    assert identity["marker"] == ownership, (
                        "created database ownership marker changed"
                    )
                    deadline = time.monotonic() + 10
                    while True:
                        sessions = await connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                                "WHERE datname = :name AND backend_type IS DISTINCT "
                                "FROM 'autovacuum worker'"
                            ),
                            {"name": database},
                        )
                        if sessions == 0:
                            break
                        assert time.monotonic() < deadline, (
                            "scratch database still has sessions"
                        )
                        await asyncio.sleep(0.1)
                    await connection.execute(text(f'DROP DATABASE "{database}"'))
                    if role_created:
                        assert role_oid is not None, (
                            "created role identity was not recorded"
                        )
                        observed_role_oid = await connection.scalar(
                            text(
                                "SELECT oid FROM pg_catalog.pg_roles "
                                "WHERE rolname = :name"
                            ),
                            {"name": LEGACY_LOGIN},
                        )
                        assert observed_role_oid == role_oid, "created role OID changed"
                        await connection.execute(text(f'DROP ROLE "{LEGACY_LOGIN}"'))
        finally:
            await admin.dispose()
