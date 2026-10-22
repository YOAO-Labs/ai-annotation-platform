"""Exercise Registry deletion with the ordinary runtime's restricted GPU ACL."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _assert_pg_error(connection, statement, *args, sqlstate: str) -> None:
    with pytest.raises(asyncpg.PostgresError) as caught:
        async with connection.transaction():
            await connection.execute(statement, *args)
    assert caught.value.sqlstate == sqlstate


async def _insert_registry(connection, backend_id, *, gpu_resource_id=None) -> None:
    await connection.execute(
        """
        INSERT INTO public.ml_backend_registry (
            id, name, url, state, is_interactive, auth_method, extra_params,
            gpu_resource_id, vram_budget_mb, eviction_priority, source
        ) VALUES (
            $1, $2, $3, 'disconnected', false, 'none', '{}'::jsonb,
            $4, $5, 0, 'manual'
        )
        """,
        backend_id,
        f"gpu-delete-noop-{backend_id}",
        f"http://gpu-delete-noop-{backend_id}.example.test",
        gpu_resource_id,
        2048 if gpu_resource_id is not None else None,
    )


async def test_registry_delete_skips_only_missing_fence_rows(test_engine) -> None:
    await assert_owned_test_database(test_engine)
    owner_url = make_url(test_engine.url).set(drivername="postgresql")
    assert owner_url.database and owner_url.database.endswith(("_test", "_e2e"))
    connection = await asyncpg.connect(
        owner_url.render_as_string(hide_password=False),
        timeout=5,
        command_timeout=10,
    )
    no_fence_id, historical_fence_id, managed_id = (uuid.uuid4() for _ in range(3))
    transaction = connection.transaction()
    await transaction.start()
    try:
        function_config = await connection.fetchrow(
            """
            SELECT prosecdef, proconfig
            FROM pg_catalog.pg_proc
            WHERE oid = 'public.sync_gpu_backend_membership()'::regprocedure
            """
        )
        assert function_config["prosecdef"] is False
        search_path = next(
            setting.partition("=")[2]
            for setting in function_config["proconfig"]
            if setting.startswith("search_path=")
        )
        assert tuple(part.strip() for part in search_path.split(",")) == (
            "pg_catalog",
            "public",
            "pg_temp",
        )

        await _insert_registry(connection, no_fence_id)
        await _insert_registry(connection, historical_fence_id)
        await connection.execute(
            "INSERT INTO public.gpu_backend_fences (backend_registry_id) VALUES ($1)",
            historical_fence_id,
        )
        await _insert_registry(
            connection,
            managed_id,
            gpu_resource_id=f"node-delete-noop/GPU-{managed_id.hex[:8]}",
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_fences "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == 1
        )
        await connection.execute(
            "UPDATE public.gpu_backend_fences SET runtime_epoch_high_water = 1 "
            "WHERE backend_registry_id = $1",
            managed_id,
        )

        await connection.execute('SET LOCAL ROLE "identity_api_consumer"')
        assert (
            await connection.fetchval("SELECT current_user") == "identity_api_consumer"
        )
        privileges = await connection.fetchrow(
            """
            SELECT has_table_privilege(current_user,
                       'public.ml_backend_registry', 'DELETE') AS registry_delete,
                   has_table_privilege(current_user,
                       'public.gpu_backend_fences', 'DELETE') AS fence_delete
            """
        )
        assert privileges["registry_delete"] is True
        assert privileges["fence_delete"] is False

        await _assert_pg_error(
            connection,
            "DELETE FROM public.gpu_backend_fences WHERE false",
            sqlstate="42501",
        )

        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_fences "
                "WHERE backend_registry_id = $1",
                no_fence_id,
            )
            == 0
        )
        assert (
            await connection.execute(
                "DELETE FROM public.ml_backend_registry WHERE id = $1", no_fence_id
            )
            == "DELETE 1"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.ml_backend_registry WHERE id = $1",
                no_fence_id,
            )
            == 0
        )

        await _assert_pg_error(
            connection,
            "DELETE FROM public.ml_backend_registry WHERE id = $1",
            historical_fence_id,
            sqlstate="42501",
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.ml_backend_registry WHERE id = $1",
                historical_fence_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_fences "
                "WHERE backend_registry_id = $1",
                historical_fence_id,
            )
            == 1
        )

        await _assert_pg_error(
            connection,
            "DELETE FROM public.ml_backend_registry WHERE id = $1",
            managed_id,
            sqlstate="23514",
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.ml_backend_registry WHERE id = $1",
                managed_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT state FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == "pending"
        )
        assert (
            await connection.fetchval(
                "SELECT runtime_epoch_high_water FROM public.gpu_backend_fences "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == 1
        )
    finally:
        await transaction.rollback()
        await connection.close()
