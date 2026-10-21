"""Owned pg_dump/restore acceptance for the Workspace backend-binding command."""

from __future__ import annotations

import os
import stat
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool
from tests.test_workspace_backend_bindings_native import (
    _FUNCTION,
    _cleanup,
    _connect_inference,
    _seed,
    _snapshot,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def _function_metadata(engine) -> dict:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                                  function.proacl::text AS acl,
                                  function.prosecdef AS security_definer,
                                  function.proconfig::text AS configuration,
                                  function.provolatile::text AS volatility,
                                  pg_catalog.pg_get_function_result(function.oid) AS result,
                                  pg_catalog.pg_get_functiondef(function.oid) AS definition,
                                  pg_catalog.has_function_privilege(
                                      'inference_code', function.oid, 'EXECUTE'
                                  ) AS inference_can_execute,
                                  EXISTS (
                                      SELECT 1
                                      FROM pg_catalog.aclexplode(COALESCE(
                                          function.proacl,
                                          pg_catalog.acldefault('f', function.proowner)
                                      )) AS acl
                                      WHERE acl.grantee = 0
                                        AND acl.privilege_type = 'EXECUTE'
                                  ) AS public_can_execute
                           FROM pg_catalog.pg_proc AS function
                           WHERE function.oid = pg_catalog.to_regprocedure(:signature)"""
                    ),
                    {"signature": _FUNCTION},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _assert_clone_owner(engine, clone: str, ownership: str) -> None:
    _verify_postgres(engine.url)
    async with engine.connect() as connection:
        current_user, database, can_create = (
            await connection.execute(
                text(
                    "SELECT current_user, current_database(), "
                    "pg_catalog.has_schema_privilege(current_user, "
                    "'aap_workspace_v1', 'CREATE')"
                )
            )
        ).one()
    assert database == clone and database.endswith("_test")
    assert current_user not in {
        "legacy_runtime",
        "inference_code",
        "workspace_backend_binding_client",
    }
    assert can_create
    async with engine.connect() as connection:
        marker = await connection.scalar(
            text(
                "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=:name"
            ),
            {"name": clone},
        )
    assert marker == ownership


async def test_workspace_backend_binding_command_survives_owned_database_restore(
    test_engine, tmp_path
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    maker, world = await _seed(test_engine)
    clone = f"aap_workspace_backend_bindings_restore_{uuid.uuid4().hex[:10]}_test"
    assert len(clone.encode("utf-8")) <= 63 and clone.endswith("_test")
    ownership = f"workspace-backend-bindings-restore:{uuid.uuid4()}"
    quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
    snapshot = tmp_path / "workspace-backend-bindings.dump"
    database_created = False
    restored_engine = None
    inference = None
    admin = None

    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        assert await _database_head(test_engine) == "0276"
        source_function = await _function_metadata(test_engine)
        assert source_function["owner"] == "workspace_code"
        assert source_function["security_definer"] is True
        assert source_function["result"] == "bigint"
        assert source_function["volatility"] == "v"
        assert "search_path=pg_catalog, pg_temp" in source_function["configuration"]
        assert "TimeZone=UTC" in source_function["configuration"]
        assert "DateStyle=ISO, YMD" in source_function["configuration"]
        assert "extra_float_digits=3" in source_function["configuration"]
        assert source_function["inference_can_execute"] is True
        assert source_function["public_can_execute"] is False
        source_rows = await _snapshot(test_engine, world)
        assert set(source_rows) == set(world["project_ids"])
        assert source_rows[world["projects"]["target_a"]][0] == world["target_pool_id"]
        assert source_rows[world["projects"]["target_b"]][0] == world["target_pool_id"]
        assert source_rows[world["projects"]["other"]][0] == world["other_pool_id"]
        assert source_rows[world["projects"]["unbound"]][0] is None

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600

        _verify_postgres(test_engine.url)
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE {quoted_clone} TEMPLATE template0")
            )
            database_created = True
            await connection.execute(
                text(f"COMMENT ON DATABASE {quoted_clone} IS '{ownership}'")
            )
            await connection.execute(
                text(f"REVOKE ALL ON DATABASE {quoted_clone} FROM PUBLIC")
            )
            async with test_engine.connect() as source:
                source_acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_catalog.pg_database AS database "
                            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                            "database.datacl, pg_catalog.acldefault('d',database.datdba)"
                            ")) AS entry "
                            "LEFT JOIN pg_catalog.pg_roles AS role "
                            "ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = connection.dialect.identifier_preparer.quote
            for grantee, privilege, grantable in source_acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                grant_option = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(
                        f"GRANT {privilege} ON DATABASE {quoted_clone} "
                        f"TO {role}{grant_option}"
                    )
                )
        _verify_postgres(test_engine.url)
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )

        restored_engine = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        await _assert_clone_owner(restored_engine, clone, ownership)
        assert await _database_head(restored_engine) == "0276"
        assert await _function_metadata(restored_engine) == source_function
        assert await _snapshot(restored_engine, world) == source_rows

        inference = await _connect_inference(restored_engine)
        async with inference.transaction():
            assert (
                await inference.fetchval(
                    "SELECT aap_workspace_v1.clear_primary_backend_bindings($1::uuid)",
                    world["target_pool_id"],
                )
                == 2
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with inference.transaction():
                await inference.fetchval("SELECT id FROM public.projects LIMIT 1")

        clone_rows = await _snapshot(restored_engine, world)
        assert clone_rows[world["projects"]["target_a"]][0] is None
        assert clone_rows[world["projects"]["target_b"]][0] is None
        assert (
            clone_rows[world["projects"]["other"]]
            == source_rows[world["projects"]["other"]]
        )
        assert (
            clone_rows[world["projects"]["unbound"]]
            == source_rows[world["projects"]["unbound"]]
        )
        assert await _snapshot(test_engine, world) == source_rows
    finally:
        if inference is not None:
            await inference.close()
        if restored_engine is not None:
            await restored_engine.dispose()
        try:
            if database_created:
                _verify_postgres(test_engine.url)
                async with admin.connect() as connection:
                    marker = await connection.scalar(
                        text(
                            "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                            "FROM pg_catalog.pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    assert marker == ownership
                    active = await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=:name "
                            "AND pid <> pg_catalog.pg_backend_pid() "
                            "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                        ),
                        {"name": clone},
                    )
                    assert not active
                    await connection.execute(text(f"DROP DATABASE {quoted_clone}"))
        finally:
            if admin is not None:
                await admin.dispose()
            try:
                await _cleanup(maker, world)
            finally:
                snapshot.unlink(missing_ok=True)
