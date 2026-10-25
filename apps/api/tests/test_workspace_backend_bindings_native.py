"""Owned PostgreSQL acceptance for fixed Workspace backend-binding cleanup."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.project import Project
from app.db.models.user import User
from app.services.workspace_backend_bindings import (
    clear_primary_backend_binding_for_pool,
)
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_FUNCTION = "aap_workspace_v1.clear_primary_backend_bindings(uuid)"
_FUNCTION_CALL = "aap_workspace_v1.clear_primary_backend_bindings"
_FIXED_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect_inference(test_engine):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    await connection.execute("SET ROLE inference_code")
    assert await connection.fetchval("SELECT current_user") == "inference_code"
    return connection


async def _seed(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = {
        "actor_ids": [],
        "project_ids": [],
        "pool_ids": [],
        "projects": {},
    }
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"workspace-binding-{uuid.uuid4().hex}@example.test",
                "Workspace Binding Owner",
            )
            world["actor_ids"].append(actor.id)

            target_pool = MLBackendServicePool(
                name=f"Workspace binding target {uuid.uuid4().hex}",
                enabled=False,
                legacy_instance_id=None,
            )
            other_pool = MLBackendServicePool(
                name=f"Workspace binding other {uuid.uuid4().hex}",
                enabled=False,
                legacy_instance_id=None,
            )
            db.add_all((target_pool, other_pool))
            await db.flush()
            target_pool_id = target_pool.id
            other_pool_id = other_pool.id
            world["pool_ids"].extend((target_pool_id, other_pool_id))

            for label, pool_id in (
                ("target_a", target_pool_id),
                ("target_b", target_pool_id),
                ("other", other_pool_id),
                ("unbound", None),
            ):
                project = await create_project(
                    db,
                    owner_id=actor.id,
                    name=f"Workspace binding {label}",
                )
                project.ml_backend_pool_id = pool_id
                project.updated_at = _FIXED_TIME
                world["projects"][label] = project.id
                world["project_ids"].append(project.id)
            world.update(
                actor_id=actor.id,
                target_pool_id=target_pool_id,
                other_pool_id=other_pool_id,
            )
        return maker, world
    except BaseException:
        await _cleanup(maker, world)
        raise


async def _snapshot(test_engine, world):
    connection = await asyncpg.connect(_owner_dsn(test_engine), timeout=5)
    try:
        rows = await connection.fetch(
            "SELECT id, ml_backend_pool_id, updated_at, name "
            "FROM public.projects WHERE id = ANY($1::uuid[])",
            world["project_ids"],
        )
        return {
            row["id"]: (
                row["ml_backend_pool_id"],
                row["updated_at"],
                row["name"],
            )
            for row in rows
        }
    finally:
        await connection.close()


async def test_native_clear_is_match_scoped_timestamped_and_caller_transactional(
    test_engine,
):
    maker, world = await _seed(test_engine)
    connection = None
    try:
        original = await _snapshot(test_engine, world)
        connection = await _connect_inference(test_engine)

        transaction = connection.transaction()
        await transaction.start()
        cleared = await connection.fetchval(
            f"SELECT {_FUNCTION_CALL}($1::uuid)", world["target_pool_id"]
        )
        assert cleared == 2
        assert (
            await connection.fetchval(
                f"SELECT {_FUNCTION_CALL}($1::uuid)", uuid.uuid4()
            )
            == 0
        )
        # Another connection cannot observe writes before the caller commits.
        assert await _snapshot(test_engine, world) == original
        await transaction.rollback()
        assert await _snapshot(test_engine, world) == original

        transaction = connection.transaction()
        await transaction.start()
        transaction_time = await connection.fetchval("SELECT pg_catalog.now()")
        assert (
            await connection.fetchval(
                f"SELECT {_FUNCTION_CALL}($1::uuid)", world["target_pool_id"]
            )
            == 2
        )
        await transaction.commit()

        committed = await _snapshot(test_engine, world)
        assert committed[world["projects"]["target_a"]] == (
            None,
            transaction_time,
            original[world["projects"]["target_a"]][2],
        )
        assert committed[world["projects"]["target_b"]] == (
            None,
            transaction_time,
            original[world["projects"]["target_b"]][2],
        )
        assert (
            committed[world["projects"]["other"]]
            == original[world["projects"]["other"]]
        )
        assert (
            committed[world["projects"]["unbound"]]
            == original[world["projects"]["unbound"]]
        )

        transaction = connection.transaction()
        await transaction.start()
        assert (
            await connection.fetchval(
                f"SELECT {_FUNCTION_CALL}($1::uuid)", uuid.uuid4()
            )
            == 0
        )
        await transaction.commit()
        assert await _snapshot(test_engine, world) == committed
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup(maker, world)


async def test_inference_code_has_only_the_fixed_binding_write(test_engine):
    await assert_owned_test_database(test_engine)
    connection = await asyncpg.connect(_owner_dsn(test_engine), timeout=5)
    try:
        metadata = await connection.fetchrow(
            """SELECT pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      function.prosecdef AS security_definer,
                      function.proconfig AS settings,
                      pg_catalog.has_function_privilege(
                          'inference_code', function.oid, 'EXECUTE'
                      ) AS inference_execute,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_execute
               FROM pg_catalog.pg_proc AS function
               WHERE function.oid = pg_catalog.to_regprocedure($1)""",
            _FUNCTION,
        )
        assert metadata is not None
        assert metadata["owner"] == "workspace_code"
        assert metadata["security_definer"] is True
        assert metadata["settings"] is not None
        assert any(
            setting == "search_path=pg_catalog, pg_temp"
            for setting in metadata["settings"]
        )
        assert metadata["inference_execute"] is True
        assert metadata["public_execute"] is False
        workspace_functions = await connection.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = 'aap_workspace_v1'
                 AND function.prokind = 'f'
                 AND pg_catalog.has_function_privilege(
                     'inference_code', function.oid, 'EXECUTE'
                 )
               ORDER BY function.proname, pg_catalog.oidvectortypes(function.proargtypes)"""
        )
        assert [row["signature"] for row in workspace_functions] == [
            "aap_workspace_v1.clear_primary_backend_bindings(uuid)",
            "aap_workspace_v1.lock_project_reference(uuid)",
            "aap_workspace_v1.project_access_step(uuid, uuid, text)",
        ]
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "'inference_code', 'public.projects', 'SELECT')"
            )
            is False
        )
        assert (
            await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "'inference_code', 'public.projects', 'UPDATE')"
            )
            is False
        )

        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE inference_code")
            for statement in (
                "SELECT id FROM public.projects LIMIT 1",
                "UPDATE public.projects SET ml_backend_pool_id = NULL WHERE false",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.transaction():
                        await connection.execute(statement)
            with pytest.raises(asyncpg.PostgresError) as caught:
                async with connection.transaction():
                    await connection.fetchval(f"SELECT {_FUNCTION_CALL}(NULL::uuid)")
            assert caught.value.sqlstate == "22023"
            assert (
                await connection.fetchval(
                    f"SELECT {_FUNCTION_CALL}($1::uuid)", uuid.uuid4()
                )
                == 0
            )
    finally:
        await connection.close()


async def test_api_adapter_keeps_autoflush_and_no_autoflush_in_outer_transaction(
    test_engine,
):
    maker, world = await _seed(test_engine)
    try:
        original = await _snapshot(test_engine, world)
        pending_default_id = None
        async with maker() as db:
            transaction = await db.begin()
            unrelated = await db.get(Project, world["projects"]["other"])
            assert unrelated is not None
            unrelated.name = "pending unrelated change"
            pending_default = await create_project(
                db, owner_id=world["actor_id"], name="Pending default autoflush"
            )
            pending_default.ml_backend_pool_id = world["target_pool_id"]
            pending_default_id = pending_default.id

            await clear_primary_backend_binding_for_pool(db, world["target_pool_id"])

            rows = (
                await db.execute(
                    select(Project.id, Project.ml_backend_pool_id, Project.name).where(
                        Project.id.in_([*world["project_ids"], pending_default_id])
                    )
                )
            ).all()
            current = {row.id: (row.ml_backend_pool_id, row.name) for row in rows}
            assert current[pending_default_id][0] is None
            assert current[world["projects"]["target_a"]][0] is None
            assert current[world["projects"]["target_b"]][0] is None
            assert current[world["projects"]["other"]] == (
                world["other_pool_id"],
                "pending unrelated change",
            )
            await transaction.rollback()

        assert await _snapshot(test_engine, world) == original
        assert pending_default_id is not None
        assert not await _project_exists(test_engine, pending_default_id)

        pending_disabled_id = None
        async with maker() as db:
            transaction = await db.begin()
            pending_disabled = await create_project(
                db, owner_id=world["actor_id"], name="Pending no-autoflush"
            )
            pending_disabled.ml_backend_pool_id = world["target_pool_id"]
            pending_disabled_id = pending_disabled.id

            with db.no_autoflush:
                await clear_primary_backend_binding_for_pool(
                    db, world["target_pool_id"]
                )
                rows = (
                    await db.execute(
                        select(
                            Project.id,
                            Project.ml_backend_pool_id,
                        ).where(
                            Project.id.in_([*world["project_ids"], pending_disabled_id])
                        )
                    )
                ).all()
                current = {row.id: row.ml_backend_pool_id for row in rows}
                assert current[pending_disabled_id] is None
                assert current[world["projects"]["target_a"]] is None
                assert current[world["projects"]["target_b"]] is None

            await db.flush()
            persisted = (
                await db.execute(
                    select(Project.id, Project.ml_backend_pool_id).where(
                        Project.id == pending_disabled_id
                    )
                )
            ).one()
            assert persisted.ml_backend_pool_id == world["target_pool_id"]
            await transaction.rollback()

        assert await _snapshot(test_engine, world) == original
        assert not await _project_exists(test_engine, pending_disabled_id)
    finally:
        await _cleanup(maker, world)


async def _project_exists(test_engine, project_id):
    connection = await asyncpg.connect(_owner_dsn(test_engine), timeout=5)
    try:
        return await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM public.projects WHERE id = $1::uuid)",
            project_id,
        )
    finally:
        await connection.close()


async def _cleanup(maker, world) -> None:
    async with maker.begin() as db:
        if world["project_ids"]:
            await db.execute(
                Project.__table__.delete().where(Project.id.in_(world["project_ids"]))
            )
        if world["pool_ids"]:
            await db.execute(
                MLBackendServicePool.__table__.delete().where(
                    MLBackendServicePool.id.in_(world["pool_ids"])
                )
            )
        if world["actor_ids"]:
            await db.execute(
                User.__table__.delete().where(User.id.in_(world["actor_ids"]))
            )
